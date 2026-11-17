
"""
pdf_belgeleri'deki PDF'ler için makaleler tablosuna bibliyografik kayıt.

Çalıştırma:
    python -m src.article_metadata --dry-run
    python -m src.article_metadata --write --tam-metin temiz   # onaydan sonra

Kurallar:
- Her alan PDF'in kendisinden çıkarılır; PDF'te açıkça olmayan alan NULL kalır
  ve raporda "bulunamadı" olarak gösterilir. Değer uydurulmaz.
- Özetler kısaltılmaz/yeniden yazılmaz; yalnızca satır sonu tireleri ve
  boşluklar düzeltilir (birleştirilen kelimeler raporda listelenir).
- Hocanın satırlarına (document_id IS NULL) hiçbir UPDATE/DELETE yapılmaz:
  her UPDATE "document_id = %s AND document_id IS NOT NULL" koşuluyla
  çalışır, ON DUPLICATE KEY UPDATE kullanılmaz ve transaction sonunda hocanın
  satırlarının parmak izi değişmişse her şey geri alınır.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

from src.chunking import versioned_document
from src.database import get_connection
from src.pdf_loader import PDF_DIR, calculate_file_hash, load_pdf
from src.pdf_processing import remove_document_references


# Dosya adı öneki -> makaleler.kaynak (hocanın biçimi: 'PubMed', 'WHO').
SOURCE_BY_PREFIX = {
    "pubM": "PubMed",
    "who": "WHO",
    "TK": "Türkiye Klinikleri",
}

# Hocanın satırlarındaki gibi özetleme/yorum gerektiren alanlar: şimdilik NULL.
INTERPRETIVE_COLUMNS = (
    "ana_bulgu_tr", "konular", "soru_tipleri", "calisma_turu",
    "hasta_grubu", "n", "yas", "cinsiyet",
)

# Bu modülün yazdığı sütunlar (makale_id yalnızca INSERT'te yazılır).
MANAGED_COLUMNS = (
    "document_id", "kaynak", "baslik", "dergi", "yil", "doi", "baglanti",
    "abstract_en", "ozet_tr", "pdf_sayfa", "tam_metin",
)

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>,;]+")
SECTION_LABEL = re.compile(r"^(?:[A-Z][A-Za-z ]{2,40}:|[A-Z][A-Z ]{3,40}—)")
PAGE_MARKER = "\n\n=== Sayfa {n} ===\n\n"
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


# ---------------------------------------------------------------------------
# Metin yardımcıları
# ---------------------------------------------------------------------------

def _as_text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


def clean_extracted(text, corpus, joined_log):
    """
    Satır sonu tirelerini birleştirir, satırları tek paragraf yapar.
    Bölüm etiketiyle başlayan satırların ("Background:", "IMPORTANCE—")
    önündeki satır sonu korunur. Kelimeler değiştirilmez.

    Tire kuralı: aynı kelime metnin başka yerinde tireli geçiyorsa tire
    korunur; iki tarafı da küçük harfse tire kaldırılır; aksi halde korunur.
    """
    # Yumuşak tire (U+00AD): satır sonuyla birlikte kaldırılır.
    text = re.sub(r"­[ \t]*\n[ \t]*", "", text).replace("­", "")
    # Bağlı harfler (ﬁ, ﬂ …) PDF kodlamasıdır; düz harflere açılır.
    for ligature, letters in LIGATURES.items():
        text = text.replace(ligature, letters)

    def join(match):
        left, right = match.group(1), match.group(2)
        hyphenated = f"{left}-{right}"
        if re.search(rf"(?<!\w){re.escape(hyphenated)}(?!\w)", corpus):
            return hyphenated
        if left[-1].islower() and right[0].islower():
            joined_log.append(f"{left}-|{right} -> {left}{right}")
            return left + right
        return hyphenated

    text = re.sub(r"(\w+)-[ \t]*\n[ \t]*(\w+)", join, text)

    lines = [line.strip() for line in text.splitlines()]
    paragraphs = []
    for line in lines:
        if not line:
            continue
        if paragraphs and not SECTION_LABEL.match(line):
            paragraphs[-1] += " " + line
        else:
            paragraphs.append(line)

    return "\n".join(re.sub(r"[ \t]+", " ", p).strip() for p in paragraphs)


def _between(text, start_pattern, end_pattern):
    """start_pattern'den sonra, end_pattern'e kadar olan metin; yoksa None."""
    start = re.search(start_pattern, text)
    if not start:
        return None
    end = re.compile(end_pattern).search(text, start.end())
    if not end:
        return None
    return text[start.end():end.start()]


# ---------------------------------------------------------------------------
# Alan çıkarıcılar
# ---------------------------------------------------------------------------

def extract_title(pdf):
    """
    1) PDF metadata başlığı, birinci sayfanın metninde geçiyorsa;
    2) yoksa birinci sayfadaki en büyük kalın yazı satırları.
    """
    first_page = _normalize(pdf[0].get_text())
    meta_title = (pdf.metadata or {}).get("title") or ""
    if len(meta_title) >= 10 and _normalize(meta_title) in first_page:
        return meta_title.strip(), "PDF metadata başlığı (1. sayfada doğrulandı)"

    spans = []
    for block in pdf[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip() and "Bold" in span["font"]:
                    spans.append((round(span["size"], 1), span["text"].strip()))
    for size in sorted({s for s, _ in spans}, reverse=True):
        title = " ".join(t for s, t in spans if s == size)
        if len(title) >= 20:
            return re.sub(r"\s+", " ", title), f"1. sayfadaki en büyük kalın yazı ({size} pt)"
    return None, "bulunamadı"


def extract_doi(first_pages_text):
    labelled = re.search(
        r"(?:doi:\s*|DOI:\s*|https?://\s*(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s\"<>,;]+)",
        first_pages_text.replace("https://\n", "https://"),
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1).rstrip(".)"), "1-2. sayfadaki etiketli DOI"
    return None, "bulunamadı"


def _jama_manuscript_abstract(pdf):
    """
    NIH/HHS author manuscript (JAMA): özet blokları 'Abstract' başlığından
    sonra aynı girintide devam eder; sayfa üst/alt bilgisi ve kenardaki
    'Author Manuscript' bandı atlanır, farklı girintili ilk metin bloğunda biter.
    """
    abstract_x = None
    started = False
    parts = []
    for page in pdf.pages(0, min(3, len(pdf))):
        height = page.rect.height
        for x0, y0, x1, y1, text, *_ in sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0])):
            text = text.strip()
            if not started:
                if text == "Abstract":
                    started = True
                continue
            # üst/alt bilgi, kenar bandı
            if y0 < 60 or y0 > height - 110 or x0 < 50 or text.startswith("Author Manuscript"):
                continue
            if abstract_x is None:
                abstract_x = x0
            if abs(x0 - abstract_x) > 2:
                return "\n".join(parts) if parts else None
            parts.append(text)
    return None


def extract_publication(raw_text, pdf):
    """
    Yayıncı biçimini tanıyıp dergi, yıl, İngilizce ve Türkçe özeti çıkarır.
    Döndürür: (biçim, {alan: (değer, not)}).
    """
    first = raw_text[:12000]
    found = {}

    if "HHS Public Access" in first and "Author manuscript" in first:
        fmt = "NIH author manuscript (JAMA)"
        m = re.search(r"Published in final edited form as:\s*\n\s*([^\n.]+)\.\s+(\d{4})\b", first)
        if m:
            found["dergi"] = (m.group(1).strip(), "'Published in final edited form as' satırı")
            found["yil"] = (int(m.group(2)), "'Published in final edited form as' satırı")
        found["abstract_en"] = (_jama_manuscript_abstract(pdf), "'Abstract' blokları (girintiye göre)")

    elif "MDPI" in first and "Abstract:" in first:
        fmt = "MDPI"
        m = re.search(r"(?m)^Journal of\s*\n([^\n]+)\n", first)
        if m:
            found["dergi"] = (f"Journal of {m.group(1).strip()}", "1. sayfa dergi başlığı")
        m = re.search(r"Published:\s*\d{1,2}\s+\w+\s+(\d{4})", first)
        if m:
            found["yil"] = (int(m.group(1)), "'Published:' tarihi")
        found["abstract_en"] = (_between(first, r"Abstract:\s*", r"\n\s*Keywords:"), "'Abstract:' … 'Keywords:'")

    elif re.search(r"(?m)^Turkiye Klinikleri J ", first):
        fmt = "Türkiye Klinikleri"
        m = re.search(r"(?m)^(Turkiye Klinikleri J [^.\n]+)\.\s*(\d{4});", first)
        if m:
            found["dergi"] = (m.group(1).strip(), "1. sayfa üst bilgi künyesi")
            found["yil"] = (int(m.group(2)), "1. sayfa üst bilgi künyesi")
        found["abstract_en"] = (_between(first, r"ABS ?TRACT\s*", r"\n\s*Keywords:"), "'ABSTRACT' … 'Keywords:'")
        found["ozet_tr"] = (_between(first, r"ÖZET\s*", r"\n\s*Anah ?tar ?Ke ?li ?me ?ler:"), "'ÖZET' … 'Anahtar Kelimeler:'")

    elif "Journal of the College of Physicians and Surgeons Pakistan" in first:
        fmt = "JCPSP"
        m = re.search(r"(Journal of the College of Physicians and Surgeons Pakistan)\s+(\d{4}),", first)
        if m:
            found["dergi"] = (m.group(1), "1. sayfa üst bilgi")
            found["yil"] = (int(m.group(2)), "1. sayfa üst bilgi")
        found["abstract_en"] = (_between(first, r"(?m)^ABSTRACT\s*\n", r"\nKey Words:"), "'ABSTRACT' … 'Key Words:'")

    elif re.search(r"EMHJ – Vol\.", first):
        fmt = "EMHJ"
        m = re.search(r"Citation:.*?\b(East Mediterr Health J)\.\s*(\d{4});", first, re.S)
        if m:
            found["dergi"] = (m.group(1), "'Citation:' satırı (PDF'te yalnızca kısaltma var)")
            found["yil"] = (int(m.group(2)), "'Citation:' satırı")
        found["abstract_en"] = (_between(first, r"(?m)^Abstract\s*\n", r"\nKey words:"), "'Abstract' … 'Key words:'")

    else:
        fmt = "tanınmadı"

    return fmt, found


# ---------------------------------------------------------------------------
# tam_metin
# ---------------------------------------------------------------------------

def build_full_text(pdf_path, expected_version, variant):
    """
    variant='ham'  : hocanın satırlarındaki gibi PyMuPDF get_text() düz metni,
                     sayfa işaretsiz, kaynakça dahil.
    variant='temiz': bizim hattımızın (load_pdf + remove_document_references)
                     metni, pdf_chunks ile aynı processing_version, sayfa
                     işaretli, kaynakça çıkarılmış.
    """
    if variant == "ham":
        with pymupdf.open(pdf_path) as pdf:
            return "".join(page.get_text() for page in pdf)

    # pdf_belgeleri.processing_version chunking sürümünü de içerir; metin aynıdır.
    cleaned = versioned_document(remove_document_references(load_pdf(pdf_path)))
    if cleaned["processing_version"] != expected_version:
        raise ValueError(
            "Temiz metin sürümü pdf_belgeleri ile uyuşmuyor: "
            f"{cleaned['processing_version']} != {expected_version}. "
            "Önce python -m src.ingest çalıştırın."
        )
    return "".join(
        PAGE_MARKER.format(n=page["page_number"]) + page["text"]
        for page in cleaned["pages"]
    ).strip()


# ---------------------------------------------------------------------------
# Kayıt üretimi
# ---------------------------------------------------------------------------

def propose_makale_id(doi, document_id):
    """
    Önerilen kimlik: hocanın 'WHO-<ISBN>' biçimine benzer şekilde 'DOI-<doi>'.
    PDF'lerde PMID yok. DOI yoksa 'PDF-<document_id ilk 16>'.
    """
    if doi:
        return f"DOI-{doi.lower()}"
    return f"PDF-{document_id[:16]}"


def build_record(document_id, filename, page_count, processing_version, tam_metin_variant):
    pdf_path = Path(PDF_DIR) / filename
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF bulunamadı: {pdf_path}")
    if calculate_file_hash(pdf_path) != document_id:
        raise ValueError(f"PDF içeriği pdf_belgeleri.document_id ile uyuşmuyor: {filename}")

    with pymupdf.open(pdf_path) as pdf:
        raw_text = "".join(page.get_text() for page in pdf)
        first_pages = "".join(pdf[i].get_text() for i in range(min(2, len(pdf))))
        title, title_note = extract_title(pdf)
        fmt, publication = extract_publication(raw_text, pdf)

    doi, doi_note = extract_doi(first_pages)
    prefix = filename.split("-", 1)[0]
    joined = []

    def text_field(name):
        value, note = publication.get(name, (None, "bulunamadı"))
        if value is None:
            return None, "bulunamadı" if note == "bulunamadı" else f"bulunamadı ({note})"
        return clean_extracted(value, raw_text, joined), note

    abstract_en, abstract_note = text_field("abstract_en")
    if prefix == "TK":
        ozet_tr, ozet_note = text_field("ozet_tr")
    else:
        ozet_tr, ozet_note = None, "bulunamadı (PDF'te Türkçe özet yok)"

    record = {
        "makale_id": propose_makale_id(doi, document_id),
        "document_id": document_id,
        "kaynak": SOURCE_BY_PREFIX.get(prefix),
        "baslik": title,
        "dergi": publication.get("dergi", (None,))[0],
        "yil": publication.get("yil", (None,))[0],
        "doi": doi,
        "baglanti": f"https://doi.org/{doi}" if doi else None,
        "abstract_en": abstract_en,
        "ozet_tr": ozet_tr,
        "pdf_sayfa": page_count,
        "tam_metin": build_full_text(pdf_path, processing_version, tam_metin_variant),
        **{column: None for column in INTERPRETIVE_COLUMNS},
    }
    notes = {
        "makale_id": "öneri: PDF'te PMID yok, DOI'den türetildi",
        "document_id": "pdf_belgeleri.document_id (PDF SHA-256'sı doğrulandı)",
        "kaynak": f"dosya adı öneki '{prefix}'" if record["kaynak"] else "bulunamadı (öneki tanınmadı)",
        "baslik": title_note,
        "dergi": publication.get("dergi", (None, "bulunamadı"))[1],
        "yil": publication.get("yil", (None, "bulunamadı"))[1],
        "doi": doi_note,
        "baglanti": "https://doi.org/<doi>" if doi else "bulunamadı (DOI yok)",
        "abstract_en": abstract_note,
        "ozet_tr": ozet_note,
        "pdf_sayfa": "pdf_belgeleri.page_count",
        "tam_metin": f"varyant: {tam_metin_variant}",
        **{column: "şimdilik NULL (yorum gerektirir)" for column in INTERPRETIVE_COLUMNS},
    }
    return {"filename": filename, "format": fmt, "record": record,
            "notes": notes, "dehyphenated": joined}


def build_all(conn, variant="temiz"):
    """pdf_belgeleri'deki bütün tamamlanmış belgeler için kayıt üretir."""
    return [
        build_record(document_id, filename, page_count, version, variant)
        for document_id, filename, page_count, version in load_documents(conn)
    ]


def load_documents(conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT document_id, filename, page_count, processing_version "
        "FROM pdf_belgeleri WHERE durum = 'tamamlandi' ORDER BY filename"
    )
    rows = [tuple(_as_text(v) for v in row) for row in cursor.fetchall()]
    cursor.close()
    return rows


# ---------------------------------------------------------------------------
# Hocanın satırları: parmak izi
# ---------------------------------------------------------------------------

def professor_rows_fingerprint(cursor):
    """document_id IS NULL olan satırların bütün sütunlarının SHA-256'sı."""
    cursor.execute("SELECT * FROM makaleler WHERE document_id IS NULL ORDER BY makale_id")
    columns = [d[0] for d in cursor.description]
    rows = [
        {c: (None if v is None else str(_as_text(v))) for c, v in zip(columns, row)}
        for row in cursor.fetchall()
    ]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Yazma (yalnızca --write)
# ---------------------------------------------------------------------------

def write_records(conn, built):
    """document_id'ye göre upsert; tek transaction; hocanın satırları korunur."""
    conn.autocommit = True
    conn.start_transaction()
    cursor = conn.cursor()
    try:
        before = professor_rows_fingerprint(cursor)
        inserted = updated = 0

        for item in built:
            record = item["record"]
            cursor.execute(
                "SELECT makale_id FROM makaleler "
                "WHERE document_id = %s AND document_id IS NOT NULL FOR UPDATE",
                (record["document_id"],),
            )
            existing = cursor.fetchall()
            if len(existing) > 1:
                raise ValueError(f"Aynı document_id ile birden fazla satır: {record['document_id']}")

            if existing:
                assignments = ", ".join(f"`{c}` = %s" for c in MANAGED_COLUMNS)
                cursor.execute(
                    f"UPDATE makaleler SET {assignments} "
                    "WHERE document_id = %s AND document_id IS NOT NULL",
                    [record[c] for c in MANAGED_COLUMNS] + [record["document_id"]],
                )
                updated += 1
            else:
                columns = ("makale_id",) + MANAGED_COLUMNS
                cursor.execute(
                    f"INSERT INTO makaleler ({', '.join(f'`{c}`' for c in columns)}) "
                    f"VALUES ({', '.join(['%s'] * len(columns))})",
                    [record[c] for c in columns],
                )
                inserted += 1

        after = professor_rows_fingerprint(cursor)
        if after != before:
            raise RuntimeError(f"Hocanın satırları değişti! önce={before} sonra={after}")

        conn.commit()
        return {"inserted": inserted, "updated": updated, "professor_rows": after}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

def _preview(value, limit=300):
    if value is None:
        return "NULL"
    if isinstance(value, str) and len(value) > limit:
        return f"{value[:limit]!r}… (toplam {len(value)} karakter)"
    return repr(value)


def print_report(built, fingerprint, other_variant_lengths):
    for item in built:
        record, notes = item["record"], item["notes"]
        print("=" * 110)
        print(f"{item['filename']}   [biçim: {item['format']}]")
        print("-" * 110)
        for column in ("makale_id",) + MANAGED_COLUMNS + INTERPRETIVE_COLUMNS:
            value = record[column]
            if column == "tam_metin":
                shown = (f"{len(value)} karakter "
                         f"(diğer varyant: {other_variant_lengths[record['document_id']]} karakter)")
            elif column in ("abstract_en", "ozet_tr"):
                shown = _preview(value)
            elif column in INTERPRETIVE_COLUMNS:
                continue
            else:
                shown = _preview(value, 120)
            print(f"  {column:<12} {shown}")
            print(f"  {'':<12}   ↳ {notes[column]}")
        print(f"  {'diğerleri':<12} {', '.join(INTERPRETIVE_COLUMNS)} = NULL (yorum gerektirir)")
        if item["dehyphenated"]:
            print(f"  tire birleştirme: {'; '.join(item['dehyphenated'])}")

    print("=" * 110)
    missing = [(i["filename"], c) for i in built for c in ("baslik", "dergi", "yil", "doi", "abstract_en")
               if i["record"][c] is None]
    print("Bulunamayan zorunlu-olmayan alanlar:", missing or "yok")
    ids = [i["record"]["makale_id"] for i in built]
    print("makale_id uzunlukları:", [len(x) for x in ids], "(sınır 64)")
    print(f"Hocanın satırları: {fingerprint[0]} satır, parmak izi {fingerprint[1]}")


def main():
    parser = argparse.ArgumentParser(description="makaleler tablosuna PDF bibliyografyası.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--tam-metin", choices=("temiz", "ham"), default=None,
                        help="tam_metin kaynağı (--write için zorunlu)")
    args = parser.parse_args()

    if args.write and not args.tam_metin:
        parser.error("--write için --tam-metin temiz|ham açıkça seçilmeli.")
    variant = args.tam_metin or "temiz"
    other = "ham" if variant == "temiz" else "temiz"

    conn = get_connection()
    try:
        documents = load_documents(conn)
        if not documents:
            sys.exit("pdf_belgeleri'de tamamlanmış belge yok; önce python -m src.ingest.")

        built, other_lengths = [], {}
        for document_id, filename, page_count, version in documents:
            built.append(build_record(document_id, filename, page_count, version, variant))
            other_lengths[document_id] = len(build_full_text(Path(PDF_DIR) / filename, version, other))

        cursor = conn.cursor()
        fingerprint = professor_rows_fingerprint(cursor)
        cursor.close()
        conn.rollback()

        print_report(built, fingerprint, other_lengths)

        ids = [b["record"]["makale_id"] for b in built]
        if len(set(ids)) != len(ids) or any(len(i) > 64 for i in ids):
            sys.exit("makale_id tekrar ediyor ya da 64 karakteri aşıyor.")

        if args.dry_run:
            print("\nDRY-RUN: veritabanına hiçbir şey yazılmadı.")
            return

        result = write_records(conn, built)
        print(f"\nYAZILDI: {result}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
