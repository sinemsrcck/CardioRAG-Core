
from unittest.mock import patch

from src.rag import answer_question


TEST_CHUNKS = [
    {
        "chunk_id": 1,
        "document_id": 101,
        "filename": "cardiology_test.pdf",
        "page_number": 2,
        "text": (
            "Atrial fibrillation is associated with an increased "
            "risk of ischemic stroke. Anticoagulation may reduce "
            "stroke risk in appropriately selected patients."
        ),
        "embedding": [0.1, 0.2, 0.3],
    },
    {
        "chunk_id": 2,
        "document_id": 102,
        "filename": "heart_failure_test.pdf",
        "page_number": 3,
        "text": (
            "Heart failure is a clinical syndrome characterized "
            "by symptoms such as dyspnea and fatigue. Diagnosis "
            "requires clinical assessment and supporting tests."
        ),
        "embedding": [0.2, 0.1, 0.4],
    },
]


def fake_hybrid_search(question, chunks, **kwargs):
    return [
        {
            **chunk,
            "rrf_score": 0.03,
        }
        for chunk in chunks
    ]


def fake_rerank(question, chunks, **kwargs):
    return [
        {
            **chunk,
            "rerank_score": 2.0 - index,
        }
        for index, chunk in enumerate(chunks)
    ]


def main():
    with (
        patch("src.rag.hybrid_search", side_effect=fake_hybrid_search),
        patch("src.rag.rerank", side_effect=fake_rerank),
    ):
        result = answer_question(
            question="Atriyal fibrilasyonun inme riski nedir?",
            chunks=TEST_CHUNKS,
            use_llm=False,
        )

    assert result["status"] == "retrieval_only"
    assert result["retrieved_count"] == 2
    assert result["selected_count"] == 2
    assert result["top_rerank_score"] == 2.0
    assert len(result["sources"]) == 2

    print("TEST BAŞARILI")
    print("Durum:", result["status"])
    print("Bulunan kaynak:", result["selected_count"])
    print("En yüksek reranker skoru:", result["top_rerank_score"])


if __name__ == "__main__":
    main()
