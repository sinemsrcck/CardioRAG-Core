
from pathlib import Path
import hashlib
import fitz  # PyMuPDF


PDF_DIR = Path("data/pdfs")


def calculate_file_hash(pdf_path):
    """PDF içeriğinden benzersiz SHA-256 kimliği üretir."""
    sha256 = hashlib.sha256()

    with open(pdf_path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha256.update(block)

    return sha256.hexdigest()


def load_pdf(pdf_path):
    """PDF'yi sayfa bazında okuyup yapılandırılmış veri döndürür."""
    pdf_path = Path(pdf_path)

    document = {
        "document_id": calculate_file_hash(pdf_path),
        "filename": pdf_path.name,
        "page_count": 0,
        "pages": [],
    }

    with fitz.open(pdf_path) as pdf:
        document["page_count"] = len(pdf)

        for page_index, page in enumerate(pdf):
            text = page.get_text("text", sort=True).strip()

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
