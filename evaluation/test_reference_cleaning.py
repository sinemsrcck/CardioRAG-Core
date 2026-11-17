
from pathlib import Path

from src.pdf_loader import load_all_pdfs
from src.pdf_processing import remove_document_references

PDF_DIR = Path("data/pdfs")


def main():
    documents = load_all_pdfs(PDF_DIR)

    if not documents:
        raise RuntimeError("PDF bulunamadı.")

    print(f"\nToplam PDF: {len(documents)}")
    print("\n========== KAYNAKÇA TEMİZLEME RAPORU ==========")

    for original in documents:
        cleaned = remove_document_references(original)

        original_pages = {
            page["page_number"]: page["text"]
            for page in original["pages"]
        }

        cleaned_pages = {
            page["page_number"]: page["text"]
            for page in cleaned["pages"]
        }

        removed_pages = sorted(
            set(original_pages) - set(cleaned_pages)
        )

        original_chars = sum(
            len(text) for text in original_pages.values()
        )

        cleaned_chars = sum(
            len(text) for text in cleaned_pages.values()
        )

        removed_chars = original_chars - cleaned_chars

        changed_pages = sorted(
            page_number
            for page_number in original_pages
            if page_number in cleaned_pages
            and original_pages[page_number] != cleaned_pages[page_number]
        )

        print(f"\nPDF: {original['filename']}")
        print(f"Orijinal sayfa: {len(original_pages)}")
        print(f"Temizlenmiş sayfa: {len(cleaned_pages)}")
        print(f"Tamamen çıkarılan sayfalar: {removed_pages}")
        print(f"Kısmen değişen sayfalar: {changed_pages}")
        print(f"Orijinal karakter: {original_chars}")
        print(f"Temizlenmiş karakter: {cleaned_chars}")
        print(f"Karakter farkı: {removed_chars}")

        if removed_chars > 0:
            print("Durum: METİN DEĞİŞTİ - doğrulama gerekli")
        elif removed_chars == 0 and not changed_pages:
            print("Durum: DEĞİŞİKLİK YOK - kaynakça kontrol edilmeli")
        else:
            print("Durum: BEKLENMEYEN FARK - inceleme gerekli")

        print("-" * 65)


if __name__ == "__main__":
    main()
