
"""
Tablo farkındalıklı chunk'lama testi (MySQL gerektirmez; 6 PDF data/pdfs/ altında olmalı).

Çalıştırma:
    python -m evaluation.test_chunking

Kontroller:
- Tablosuz metin: split_page == split_text (normal metin davranışı değişmedi).
- Sığan tablo tek chunk; başlık satırı (Table N) tabloyla birlikte.
- Uzun tablo yalnızca satır aralarından bölünür; her devam parçası
  "<başlık> (devam)" + başlık satırı + ayraç ile başlar; hiçbir chunk sınırı aşmaz.
- Tek başına sığmayan satır hücre sınırından bölünür, içerik kaybolmaz.
- versioned_document idempotent; chunk'lar CHUNKING_VERSION taşır.
- 6 gerçek PDF: chunk sayısı (242), sınır, boş sayfa yok, tablo satırları bütün,
  tablosuz sayfalar eski algoritmayla birebir aynı.
"""

import sys
from pathlib import Path

from src.chunking import (
    CHUNKING_VERSION,
    CONTINUATION_NO_CAPTION,
    CONTINUATION_SUFFIX,
    DEFAULT_CHUNK_SIZE,
    TABLE_SEPARATOR,
    _page_segments,
    chunk_document,
    clean_text,
    split_page,
    split_text,
    versioned_document,
)
from src.pdf_loader import PDF_DIR, load_pdf
from src.pdf_processing import remove_document_references


EXPECTED_REAL_CHUNKS = 242  # markdown-rotated-v3 + chunking-table-v1 (önce 243)
SIZE = DEFAULT_CHUNK_SIZE


def headerless_chunks(rows, texts, filename, page):
    """
    Tablonun gövde satırlarından birini içeren her chunk başlık satırını da
    içermeli (başlık tekrar edilebilecek uzunluktaysa). Eksik olanları döndürür.
    """
    if len(rows) < 3 or not TABLE_SEPARATOR.match(rows[1]) or len("\n".join(rows[:2])) > SIZE // 2:
        return []
    header, body = rows[0], set(rows[2:])
    return [(filename, page, t.split("\n")[0][:50]) for t in texts
            if body & set(t.split("\n")) and header not in t.split("\n")]


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    # 1) Tablosuz metin: eski algoritmayla birebir aynı
    prose = ("Atrial fibrillation is common. " * 120).strip()
    check("tablosuz metin = split_text", True, split_page(prose) == split_text(prose))

    # 2) Sığan tablo: tek chunk, başlıkla birlikte
    small = ("Intro paragraph.\n\n**Table 1.** Small table\n"
             "|A|B|\n|---|---|\n|1|2|\n|3|4|\n\nAfter table text.")
    chunks = split_page(small)
    table_chunk = [c for c in chunks if "|1|2|" in c]
    check("sığan tablo tek chunk'ta", 1, len(table_chunk))
    check("başlık tabloyla aynı chunk'ta", True,
          bool(table_chunk) and table_chunk[0].startswith("**Table 1.** Small table\n|A|B|"))
    check("tablo öncesi/sonrası metin korunuyor", True,
          any("Intro paragraph." in c for c in chunks) and any("After table text." in c for c in chunks))

    # 3) Uzun tablo: satır aralarından bölünür, devam parçaları başlık taşır
    rows = [f"|row {i}|{i * 1.5:.1f}|value {i}|" for i in range(150)]
    long_table = "**Table 2. Long outcomes**\n|Name|Value|Note|\n|---|---|---|\n" + "\n".join(rows)
    parts = split_page(long_table)
    check("uzun tablo birden fazla chunk", True, len(parts) > 1)
    check("hiçbir chunk sınırı aşmıyor", True, all(len(p) <= SIZE for p in parts))
    check("ilk parça başlıkla başlıyor", True, parts[0].startswith("**Table 2. Long outcomes**\n|Name|"))
    continuation_ok = all(
        p.split("\n")[:3] == ["**Table 2. Long outcomes**" + CONTINUATION_SUFFIX,
                              "|Name|Value|Note|", "|---|---|---|"]
        for p in parts[1:])
    check("devam parçaları: '(devam)' + başlık satırı + ayraç", True, continuation_ok)
    body_rows = [line for p in parts for line in p.split("\n")[3 if p is not parts[0] else 3:]]
    check("bütün satırlar bir kez ve sırayla", rows, body_rows, body_rows == rows)

    # 4) Başlıksız uzun tablo
    no_caption = "|Name|Value|\n|---|---|\n" + "\n".join(f"|r{i}|{i}|" for i in range(400))
    parts = split_page(no_caption)
    check("başlıksız devam etiketi", True,
          len(parts) > 1 and all(p.startswith(CONTINUATION_NO_CAPTION + "\n|Name|Value|") for p in parts[1:]))

    # 5) Tek başına sığmayan satır: hücre sınırından bölünür, içerik kaybolmaz
    cells = [f"cell {i} " + "x" * 90 for i in range(40)]
    huge_row = "|" + "|".join(cells) + "|"
    parts = split_page("|H1|H2|\n|---|---|\n" + huge_row)
    check("uzun satır: sınır aşılmıyor", True, all(len(p) <= SIZE for p in parts))
    check("uzun satır: bütün hücreler korunuyor", True,
          all(any(cell in p for p in parts) for cell in cells))

    # 6) Sürüm
    doc = {"document_id": "d", "filename": "f.pdf", "processing_version": "base-v1",
           "pages": [{"page_number": 1, "text": prose}]}
    once = versioned_document(doc)
    check("versioned_document sürüm ekliyor", "base-v1|" + CHUNKING_VERSION, once["processing_version"])
    check("versioned_document idempotent", once["processing_version"],
          versioned_document(once)["processing_version"])
    check("chunk_document sürümü", once["processing_version"],
          chunk_document(doc)[0]["processing_version"])
    check("zaten sürümlü belge aynı chunk_id'leri verir", True,
          chunk_document(doc) == chunk_document(once))

    # 7) Gerçek 6 PDF
    total, over, empty_pages, broken_rows, changed_tableless = 0, [], [], [], []
    headerless = []
    for path in sorted(Path(PDF_DIR).glob("*.pdf")):
        document = remove_document_references(load_pdf(path))
        doc_chunks = chunk_document(document)
        total += len(doc_chunks)
        over += [c["chunk_id"][-12:] for c in doc_chunks if c["char_count"] > SIZE]
        by_page = {}
        for c in doc_chunks:
            by_page.setdefault(c["page_number"], []).append(c["text"])
        for page in document["pages"]:
            texts = by_page.get(page["page_number"], [])
            if page["text"].strip() and not texts:
                empty_pages.append((path.name, page["page_number"]))
            cleaned = clean_text(page["text"])
            has_table = any(line.lstrip().startswith("|") for line in cleaned.split("\n"))
            if not has_table:
                if texts != split_text(page["text"]):
                    changed_tableless.append((path.name, page["page_number"]))
                continue
            for segment in _page_segments(cleaned):
                if segment[0] != "tablo":
                    continue
                headerless += headerless_chunks(segment[2], texts, path.name, page["page_number"])
                for row in segment[2]:
                    if TABLE_SEPARATOR.match(row):
                        continue
                    whole = any(row in t.split("\n") for t in texts)
                    by_cells = all(any(cell in t for t in texts)
                                   for cell in row.strip("|").split("|") if cell.strip())
                    if not whole and not (len(row) > SIZE // 2 and by_cells):
                        broken_rows.append((path.name, page["page_number"], row[:60]))

    check("gerçek PDF chunk sayısı", EXPECTED_REAL_CHUNKS, total)
    check("gerçek PDF: sınırı aşan chunk", 0, len(over))
    check("gerçek PDF: chunk'sız dolu sayfa", 0, len(empty_pages))
    check("gerçek PDF: bölünmüş/kayıp tablo satırı", 0, len(broken_rows))
    check("gerçek PDF: başlık satırı olmayan tablo parçası", 0, len(headerless))
    check("gerçek PDF: tablosuz sayfalar eskisiyle aynı", 0, len(changed_tableless))

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<30}  {'Bulunan':<30}  Sonuç")
    print("-" * (width + 74))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:30]:<30}  {act[:30]:<30}  {'OK' if passed else 'HATA'}")
    if broken_rows:
        print("\nBölünmüş satırlar:", broken_rows[:5])
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
