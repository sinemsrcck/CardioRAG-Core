
from src.embeddings import embed_documents
from src.rag import answer_question


texts = [
    (
        "Atrial fibrillation is associated with an increased "
        "risk of ischemic stroke. Anticoagulant treatment can "
        "reduce stroke risk in appropriately selected patients."
    ),
    (
        "Heart failure is a clinical syndrome associated with "
        "dyspnea, fatigue, and reduced exercise tolerance. "
        "Diagnosis involves clinical evaluation and testing."
    ),
    (
        "Python is a programming language used for software "
        "development, automation, and data analysis."
    ),
]

vectors = embed_documents(texts, model_name="minilm")

chunks = [
    {
        "chunk_id": index + 1,
        "document_id": index + 1,
        "filename": f"test_document_{index + 1}.pdf",
        "page_number": 1,
        "text": text,
        "embedding": vector,
    }
    for index, (text, vector) in enumerate(zip(texts, vectors))
]

result = answer_question(
    question="Atriyal fibrilasyonun inme riski nedir?",
    chunks=chunks,
    embedding_model="minilm",
    reranker_model="minilm",
    candidate_k=3,
    top_k=3,
    use_llm=False,
)

print("\n=== GERÇEK RETRIEVAL TESTİ ===")
print("Durum:", result["status"])
print("Bulunan kaynak:", result["selected_count"])

for source in result["sources"]:
    print(
        source["filename"],
        "| Rerank skoru:",
        source["rerank_score"],
    )
