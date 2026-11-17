
"""
PDF yükleme + kaynakça temizleme testi.

Çalıştırma:
    python -m evaluation.test_pdf_processing
"""

import re
import sys

from src.pdf_loader import load_all_pdfs
from src.pdf_processing import find_reference_span, remove_document_references


ATRIAL_FIBRILLATION_FILE = "pubM-Atrial Fibrillation.pdf"

# Numaralı kaynak satırları:
#   "12. Smith AB, ..."      (Vancouver)
#   "1. Cassar, A.; ..."     (MDPI)
NUMBERED_REFERENCE = re.compile(
    r"^\s*\d{1,3}\.\s+[A-Z][A-Za-z'’\-]+(?:\s[A-Z][A-Za-z'’\-]+)?,?\s[A-Z]{1,3}[,.;]",
    re.MULTILINE,
)
DOI = re.compile(r"\bdoi\b", re.IGNORECASE)

# Markdown tablosu başlık ayracı satırı: |---|---|
TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$", re.MULTILINE)

# Bir pencerede bu kadar numaralı kaynak / doi varsa kaynakça kalmış demektir.
DENSITY_WINDOW = 3000
MAX_NUMBERED_REFS_IN_WINDOW = 3
MAX_DOI_IN_WINDOW = 3


def full_text(document):
    return "\n".join(page["text"] for page in document["pages"])


def max_density(pattern, text, window=DENSITY_WINDOW):
    """Metindeki herhangi bir pencerede en fazla kaç eşleşme var?"""
    positions = [m.start() for m in pattern.finditer(text)]
    best = 0
    left = 0

    for right, pos in enumerate(positions):
        while pos - positions[left] > window:
            left += 1
        best = max(best, right - left + 1)

    return best


def main():
    documents = load_all_pdfs()
    failures = []

    print()
    header = (
        f"{'Dosya':<45} {'Sayfa':>9} {'Karakter':>17} "
        f"{'Kaynakça':>9} {'Sonrası':>8} {'Tablo':>6}"
    )
    print(header)
    print("-" * len(header))

    for document in documents:
        name = document["filename"]
        raw_text = full_text(document)
        span = find_reference_span(raw_text)

        cleaned = remove_document_references(document)
        cleaned_text = full_text(cleaned)

        found = span is not None
        kept_after = found and span[1] < len(raw_text)
        # Her tabloda bir "|---|---|" ayraç satırı vardır.
        table_count = len(TABLE_SEPARATOR.findall(cleaned_text))

        print(
            f"{name[:45]:<45} "
            f"{len(document['pages']):>4}/{len(cleaned['pages']):<4} "
            f"{len(raw_text):>8}/{len(cleaned_text):<8} "
            f"{'evet' if found else 'HAYIR':>9} "
            f"{'evet' if kept_after else '-':>8} "
            f"{table_count:>6}"
        )

        # (3) Kaynakça bulunmuş olmalı.
        if not found:
            failures.append(f"{name}: kaynakça bulunamadı")

        # (1) Numaralı kaynak satırları / doi yoğunluğu kalmamalı.
        ref_density = max_density(NUMBERED_REFERENCE, cleaned_text)
        doi_density = max_density(DOI, cleaned_text)

        if ref_density > MAX_NUMBERED_REFS_IN_WINDOW:
            failures.append(
                f"{name}: temiz metinde numaralı kaynak yoğunluğu "
                f"{ref_density} (/{DENSITY_WINDOW} karakter)"
            )
        if doi_density > MAX_DOI_IN_WINDOW:
            failures.append(
                f"{name}: temiz metinde doi yoğunluğu "
                f"{doi_density} (/{DENSITY_WINDOW} karakter)"
            )

        # Sayfa numaraları korunmalı (artan sırada, orijinalden).
        original_numbers = {p["page_number"] for p in document["pages"]}
        cleaned_numbers = [p["page_number"] for p in cleaned["pages"]]
        if (
            cleaned_numbers != sorted(cleaned_numbers)
            or not set(cleaned_numbers) <= original_numbers
        ):
            failures.append(f"{name}: sayfa numaraları bozulmuş")

        # (2) Atrial Fibrillation: Table 1-3 korunmalı.
        if name == ATRIAL_FIBRILLATION_FILE:
            for number in (1, 2, 3):
                if not re.search(rf"\bTable\s*{number}\b", cleaned_text):
                    failures.append(f"{name}: Table {number} kayboldu")

            pages = [p["page_number"] for p in cleaned["pages"]]
            print(f"    -> korunan sayfalar: {pages}")

    print()

    if failures:
        print(f"BAŞARISIZ ({len(failures)} sorun):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print(f"BAŞARILI: {len(documents)} PDF'in tüm kontrolleri geçti.")


if __name__ == "__main__":
    main()
