
from pathlib import Path
import numpy as np

from src.pdf_loader import load_all_pdfs
from src.pdf_processing import remove_document_references
from src.chunking import chunk_all_documents
from src.embeddings import embed_documents
from src.rag import answer_question

PDF_DIR = Path("data/pdfs")

QUESTION = "Atriyal fibrilasyonun inme riskiyle ilişkisi nedir?"

def main():
    # 1. PDF'leri oku
    documents = load_all_pdfs(PDF_DIR)

    if not documents:
        raise RuntimeError("Okunabilir PDF bulunamadı.")

    print(f"\nToplam PDF: {len(documents)}")

    # 2. Kaynakçaları temizle
    cleaned_documents = [
        remove_document_references(document)
        for document in documents
    ]

    print("\n========== KAYNAKÇA TEMİZLEME KONTROLÜ ==========")

    for original, cleaned in zip(documents, cleaned_documents):
        original_pages = len(original["pages"])
        cleaned_pages = len(cleaned["pages"])

        print(
            f"{original['filename']} | "
            f"Önce: {original_pages} sayfa | "
            f"Sonra: {cleaned_pages} sayfa"
        )

        if "pubM-Atrial Fibrillation" in original["filename"]:
            original_numbers = {
                page["page_number"]
                for page in original["pages"]
            }

            cleaned_numbers = {
                page["page_number"]
                for page in cleaned["pages"]
            }

            print("\n========== SAYFA KONTROLÜ ==========")
            print("Çıkarılan sayfalar:", sorted(original_numbers - cleaned_numbers))
            print("Korunan sayfalar:", sorted(cleaned_numbers))

            for page in cleaned["pages"]:
                if page["page_number"] in (12, 13, 19, 20, 21):
                    print(
                        f"Sayfa {page['page_number']} | "
                        f"{len(page['text'])} karakter"
                    )
    # 3. Chunk oluştur
    chunks = chunk_all_documents(cleaned_documents)

    if not chunks:
        raise RuntimeError("PDF'lerden chunk üretilemedi.")

    print(f"\nToplam chunk: {len(chunks)}")

    # 4. Gerçek embedding oluştur
    texts = [chunk["text"] for chunk in chunks]

    vectors = embed_documents(
        texts,
        model_name="minilm",
        batch_size=16,
    )

    for chunk, vector in zip(chunks, vectors):
        chunk["embedding"] = np.asarray(
            vector,
            dtype=np.float32,
        )

    # 5. RAG akışını çalıştır
    result = answer_question(
        question=QUESTION,
        chunks=chunks,
        embedding_model="minilm",
        reranker_model="minilm",
        candidate_k=20,
        top_k=5,
        use_llm=False,
    )
    # 6. Sonuçları göster
    print("\n========== RAG TEST ==========")
    print("Soru:", QUESTION)
    print("Durum:", result["status"])
    print("Bulunan kaynak:", result["selected_count"])
    print("En yüksek reranker skoru:", result["top_rerank_score"])

    for source in result["sources"]:
        print(f"\n[{source['number']}] {source['filename']}")
        print("Sayfa:", source["page_number"])
        print("Rerank skoru:", source["rerank_score"])

        chunk = next(
            (
                item for item in chunks
                if item["chunk_id"] == source["chunk_id"]
            ),
            None,
        )

        if chunk:
            print("METİN:", chunk["text"][:700])

        print("-" * 60)

    print("\nKanıt kontrolü:", result["evidence_check"])


if __name__ == "__main__":
    main()