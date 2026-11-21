
from pathlib import Path
import hashlib
import re

import pymupdf
import pymupdf4llm


PDF_DIR = Path("data/pdfs")
# v3: yan yatırılmış (döndürülmüş) tablo sayfaları bellek içinde düzeltilerek okunur.
# v4: satır sonunda bölünen sayısal aralıkların tiresi korunur ("0.682-0.889"; v3'te
#     "0.6820.889"). Metin değiştiği için v3 ile kaydedilmiş belgeler yeniden ingest
#     edilmeden v4 metniyle karıştırılmaz (article_metadata sürüm kontrolü açık hata verir).
PDF_PROCESSING_VERSION = "markdown-rotated-v4"

# pymupdf4llm satır sonu tiresini heceleme sayıp siler; iki tarafı da sayı olan bir
# satır sonu tiresi heceleme olamaz (aralık veya eksi), PDF'teki glif korunmalıdır.
NUMERIC_LINE_END = re.compile(r"(\d+(?:[.,]\d+)*)-[ \t]*$")
NUMERIC_LINE_START = re.compile(r"^[ \t]*(\d+(?:[.,]\d+)*)")

# Bir sayfanın metninin (karakter bazında) en az bu kadarı aynı yönde döndürülmüşse
# sayfa yan yatırılmış sayılır ve düzeltilerek okunur. Kenar bantları ("Author
# Manuscript") ve tek tük döndürülmüş dipnotlar bu eşiğin çok altında kalır.
ROTATED_PAGE_THRESHOLD = 0.8
# Satır yönü (dx, dy) -> metni yataya çeviren döndürme açısı (show_pdf_page).
DEROTATION_ANGLE = {(0, -1): -90, (0, 1): 90}
# Düzeltilmiş sayfada tek başına duran tablo/şekil etiketi: "**Table 2.**", "# **Table 1.**"
CAPTION_LABEL_LINE = re.compile(r"^[#\s]*[*_]*(?:table|figure|box)\s*[\dIVX]+[.:]?[*_\s]*$",
                                re.IGNORECASE)
# Başlığın tek hücreli tablo satırı olarak çıktığı durum: "|**Table 1 Characteristics ...**|"
CAPTION_CELL_ROW = re.compile(r"^\|\s*([*_]*(?:table|figure|box)\s*[\dIVX]+\b[^|]*)\|\s*$",
                              re.IGNORECASE)
SINGLE_SEPARATOR_ROW = re.compile(r"^\|\s*:?-{3,}:?\s*\|\s*$")

# "Table 1", "Figure 2", "Box 3" gibi başlık etiketleri.
CAPTION_PATTERN = re.compile(
    r"^(table|figure|fig\.|box)\s*(\d+)",
    re.IGNORECASE,
)


def calculate_file_hash(pdf_path):
    """PDF içeriğinden benzersiz SHA-256 kimliği üretir."""
    sha256 = hashlib.sha256()

    with open(pdf_path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha256.update(block)

    return sha256.hexdigest()


def _rotated_lines(page):
    """Sayfadaki yatay olmayan (döndürülmüş) metin satırlarını döndürür."""
    lines = []

    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]

            if abs(dx - 1) < 1e-3 and abs(dy) < 1e-3:
                continue

            text = "".join(
                span["text"] for span in line["spans"]
            ).strip()

            if text:
                lines.append(text)

    return lines


def recover_rotated_captions(page, page_text):
    """
    pymupdf4llm, yatay sayfaya döndürülerek yerleştirilmiş
    tabloların başlıklarını ("Table 1." gibi) atlayabiliyor.
    Markdown metninde bulunmayan bu başlıkları ham PDF'ten
    alıp sayfa metninin başına ekler.
    """
    lines = _rotated_lines(page)
    captions = []

    for index, text in enumerate(lines):
        match = CAPTION_PATTERN.match(text)

        if not match:
            continue

        kind, number = match.groups()
        already_present = re.search(
            rf"\b{re.escape(kind)}\s*{number}(?!\d)",
            page_text,
            re.IGNORECASE,
        )

        if already_present:
            continue

        caption = text

        # Etiket tek başına ise ("Table 1.") bir sonraki
        # döndürülmüş satır başlığın kendisidir.
        is_label_only = len(text) <= len(match.group(0)) + 1
        has_next = index + 1 < len(lines)

        if (
            is_label_only
            and has_next
            and not CAPTION_PATTERN.match(lines[index + 1])
        ):
            caption = f"{text} {lines[index + 1]}"

        captions.append(f"**{caption}**")

    if not captions:
        return page_text

    return "\n\n".join(captions + [page_text]).strip()


def _text_key(text):
    """Compare source text with Markdown without rewriting the recovered words."""
    # Yalnızca gerçek etiketler (<br>, <sup>, </sup> ...) silinir; "P < .001 ... >15 s"
    # gibi karşılaştırma işaretleri arasındaki metin silinmez.
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    return re.sub(r"[\W_]+", "", text).casefold()


def rotated_page_direction(page, threshold=ROTATED_PAGE_THRESHOLD):
    """
    Sayfa yan yatırılmış mı? Metnin (karakter bazında) en az `threshold` kadarı
    aynı döndürülmüş yönde ise o yönü (dx, dy), değilse None döndürür.
    page.rotation'a değil, satırların yazım yönüne bakar: bu PDF'lerde sayfa
    döndürülmemiş, tablo sayfaya yan çizilmiştir.
    """
    chars = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                direction = tuple(round(value) for value in line["dir"])
                chars[direction] = chars.get(direction, 0) + len(text)
    total = sum(chars.values())
    rotated = {d: n for d, n in chars.items() if d in DEROTATION_ANGLE}
    if not total or not rotated:
        return None
    direction = max(rotated, key=rotated.get)
    return direction if rotated[direction] / total >= threshold else None


def _derotated_markdown(pdf, page_index, direction):
    """
    Yan yatırılmış sayfayı bellek içinde yataya çevirip pymupdf4llm ile okur.
    Kaynak PDF değişmez; geçici tek sayfalık belge kullanılır.
    """
    page = pdf[page_index]
    with pymupdf.open() as temp:
        upright = temp.new_page(width=page.rect.height, height=page.rect.width)
        upright.show_pdf_page(upright.rect, pdf, page_index, rotate=DEROTATION_ANGLE[direction])
        chunks = pymupdf4llm.to_markdown(temp, page_chunks=True, use_ocr=False,
                                         header=False, footer=False)
    return chunks[0]["text"].strip() if chunks else ""


def _join_caption_labels(text):
    """
    Düzeltilmiş sayfalarda tablo başlığını tek satıra getirir, böylece başlık
    tablonun hemen önünde durur (chunk'lama onu tabloyla birlikte taşır):
    - Tek başına duran "Table N." satırı hemen altındaki başlık satırıyla
      birleştirilir ("**Table 2.**" + "Benefits of ..." -> "**Table 2.** Benefits of ...").
    - Tek hücreli tablo satırı olarak çıkmış başlık ("|**Table 1 ...**|" + "|---|")
      düz satıra çevrilir. Kelimeler değişmez.
    """
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        cell = CAPTION_CELL_ROW.match(line.strip())
        if cell and i + 1 < len(lines) and SINGLE_SEPARATOR_ROW.match(lines[i + 1].strip()):
            out.extend([cell.group(1).strip(), ""])
            i += 2
            continue
        nxt = i + 1
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if (CAPTION_LABEL_LINE.match(line) and nxt < len(lines)
                and not lines[nxt].lstrip().startswith("|")
                and not CAPTION_LABEL_LINE.match(lines[nxt])):
            out.append(f"{line.rstrip()} {lines[nxt].strip()}")
            i = nxt + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def recover_rotated_footnotes(page, page_text):
    """Recover omitted note blocks in PDF reading order, with coordinates.

    Only complete rotated prose blocks with a letter note marker are accepted.
    Table cells, watermarks and arbitrary missing words are not reconstructed.
    The source wording (including qualifiers) is kept, not clinically corrected.
    """
    recovered = []
    present = _text_key(page_text)
    for block in page.get_text("dict")["blocks"]:
        lines = block.get("lines", [])
        if not lines:
            continue
        if any(abs(line["dir"][0] - 1) < 1e-3 and abs(line["dir"][1]) < 1e-3
               for line in lines):
            continue
        text = " ".join(
            "".join(span["text"] for span in line["spans"]).strip()
            for line in lines
        ).strip()
        if (not re.match(r"^[a-z](?=[A-Z])", text)
                or len(text.split()) < 6 or not text.endswith((".", ";", "!", "?"))):
            continue
        # Markdown may separate a superscript marker from the same note body.
        if _text_key(text[1:]) in present:
            continue
        recovered.append({"kind": "rotated_footnote", "text": text,
                          "bbox": list(block["bbox"]), "direction": list(lines[0]["dir"])})
        page_text = "\n\n".join(part for part in (page_text, text) if part)
        present = _text_key(page_text)
    return page_text, recovered


def numeric_line_break_pairs(raw_text):
    """Ham PDF metninde satır sonunda tireyle bölünmüş sayı çiftleri: [("0.682", "0.889"), ...]."""
    lines = raw_text.splitlines() if isinstance(raw_text, str) else []
    pairs = []
    for current, following in zip(lines, lines[1:]):
        left, right = NUMERIC_LINE_END.search(current), NUMERIC_LINE_START.match(following)
        if left and right:
            pairs.append((left.group(1), right.group(1)))
    return pairs


def restore_numeric_hyphens(page_text, raw_text):
    """
    Markdown dönüşümünde silinen sayısal satır sonu tiresini geri koyar.

    Yalnızca ham metinde "sol-<satır sonu>sağ" olarak görülen ve markdown metninde
    birleşik "solsağ" biçiminde, ham metindeki geçiş sayısıyla birebir aynı sayıda bulunan
    çiftler düzeltilir; belirsiz durumda metne dokunulmaz. Rakamlar değişmez, yalnızca
    PDF'te var olan tire geri gelir. Döndürür: (metin, [{"kind", "text"}]).
    """
    restored = []
    for (left, right), count in _counts(numeric_line_break_pairs(raw_text)).items():
        joined = re.compile(rf"(?<![\d.,]){re.escape(left)}(?={re.escape(right)}(?!\d|[.,]\d))")
        matches = list(joined.finditer(page_text))
        if not matches or len(matches) != count:
            continue
        for match in reversed(matches):
            page_text = page_text[:match.end()] + "-" + page_text[match.end():]
        restored.extend({"kind": "numeric_hyphen", "text": f"{left}-{right}"} for _ in matches)
    return page_text, restored


def _counts(items):
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def load_pdf(pdf_path):
    """PDF'yi sayfa bazında markdown olarak okuyup yapılandırılmış veri döndürür."""
    pdf_path = Path(pdf_path)

    document = {
        "document_id": calculate_file_hash(pdf_path),
        "filename": pdf_path.name,
        "processing_version": (f"{PDF_PROCESSING_VERSION}|pymupdf={pymupdf.__version__}"
                               f"|pymupdf4llm={pymupdf4llm.__version__}"),
        "page_count": 0,
        "pages": [],
    }

    page_chunks = pymupdf4llm.to_markdown(
        str(pdf_path),
        page_chunks=True,
        use_ocr=False,
        header=False,
        footer=False,
    )

    with pymupdf.open(pdf_path) as pdf:
        document["page_count"] = len(pdf)

        if len(page_chunks) != len(pdf):
            raise ValueError("Markdown extraction returned an incomplete page collection.")
        for page_index, chunk in enumerate(page_chunks):
            actual_page = chunk.get("metadata", {}).get("page", page_index + 1)
            if actual_page != page_index + 1:
                raise ValueError("Markdown page order does not match physical PDF pages.")
            page_text = chunk["text"].strip()
            # Yan yatırılmış tablo sayfaları düzeltilerek okunur (aksi halde tablo
            # devrik çıkar, başlık ve dipnotların bir kısmı kaybolur).
            direction = rotated_page_direction(pdf[page_index])
            if direction is not None:
                page_text = _join_caption_labels(_derotated_markdown(pdf, page_index, direction))
            page_text, restored = restore_numeric_hyphens(page_text, pdf[page_index].get_text("text"))
            text = recover_rotated_captions(
                pdf[page_index],
                page_text,
            )

            text, recovered = recover_rotated_footnotes(pdf[page_index], text)
            document["pages"].append({
                "page_number": page_index + 1,
                "text": text,
                "recovered_pdf_spans": recovered,
                "restored_numeric_hyphens": restored,
            })

    return document


def load_all_pdfs(pdf_dir=PDF_DIR):
    """Klasördeki bütün PDF dosyalarını yükler."""
    pdf_dir = Path(pdf_dir)

    if not pdf_dir.exists():
        raise FileNotFoundError(
            f"PDF klasörü bulunamadı: {pdf_dir}"
        )

    documents = []

    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        try:
            document = load_pdf(pdf_path)
            documents.append(document)

            print(
                f"[OK] {document['filename']} | "
                f"{document['page_count']} sayfa"
            )

        except Exception as exc:
            print(f"[HATA] {pdf_path.name}: {exc}")

    return documents


if __name__ == "__main__":
    documents = load_all_pdfs()

    print(f"\nToplam okunan PDF: {len(documents)}")

    for document in documents:
        total_chars = sum(
            len(page["text"]) for page in document["pages"]
        )

        print(
            f"{document['filename']} | "
            f"Karakter sayısı: {total_chars}"
        )
