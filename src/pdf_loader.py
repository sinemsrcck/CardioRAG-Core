
from pathlib import Path
import hashlib
import re

import pymupdf
import pymupdf4llm


PDF_DIR = Path("data/pdfs")

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


def load_pdf(pdf_path):
    """PDF'yi sayfa bazında markdown olarak okuyup yapılandırılmış veri döndürür."""
    pdf_path = Path(pdf_path)

    document = {
        "document_id": calculate_file_hash(pdf_path),
        "filename": pdf_path.name,
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

        for page_index, chunk in enumerate(page_chunks):
            text = recover_rotated_captions(
                pdf[page_index],
                chunk["text"].strip(),
            )

            document["pages"].append({
                "page_number": page_index + 1,
                "text": text,
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
