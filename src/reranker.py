
from functools import lru_cache

from sentence_transformers import CrossEncoder


RERANKER_MODELS = {
    "minilm": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "bge-m3": "BAAI/bge-reranker-v2-m3",
}

DEFAULT_RERANKER = "bge-m3"


@lru_cache(maxsize=2)
def get_reranker(model_name=DEFAULT_RERANKER):
    """Reranker modelini yükler ve önbellekte tutar."""
    if model_name not in RERANKER_MODELS:
        raise ValueError(
            f"Bilinmeyen reranker: {model_name}"
        )

    model_id = RERANKER_MODELS[model_name]

    print(f"[RERANKER] Model yükleniyor: {model_id}")

    return CrossEncoder(model_id)


def rerank(
    question,
    chunks,
    model_name=DEFAULT_RERANKER,
    top_k=5,
    batch_size=8,
):
    """
    Retrieval sonuçlarını Cross-Encoder ile yeniden sıralar.

    chunks:
        retrieval.py tarafından döndürülen kayıtlar.

    Her kayıtta 'text' alanı bulunmalıdır.
    """
    if not question.strip():
        raise ValueError("Soru boş olamaz.")

    if top_k <= 0:
        return []

    valid_chunks = [
        chunk for chunk in chunks
        if chunk.get("text")
    ]

    if not valid_chunks:
        return []

    model = get_reranker(model_name)

    pairs = [
        (question, chunk["text"])
        for chunk in valid_chunks
    ]

    scores = model.predict(
        pairs,
        batch_size=batch_size,
        show_progress_bar=False,
    )

    ranked = []

    for chunk, score in zip(valid_chunks, scores):
        result = chunk.copy()
        result["rerank_score"] = float(score)
        ranked.append(result)

    ranked.sort(
        key=lambda item: item["rerank_score"],
        reverse=True,
    )

    return ranked[:top_k]


def rerank_hybrid(
    question,
    chunks,
    model_name=DEFAULT_RERANKER,
    top_k=5,
    rrf_weight=0.3,
    rerank_weight=0.7,
    rrf_k=60,
):
    """
    RRF sıralaması ile Cross-Encoder sıralamasını birleştirir.

    Ham skorlar yerine sıralama pozisyonları kullanılır;
    böylece farklı ölçeklerdeki skorlar doğrudan toplanmaz.
    """
    if rrf_weight < 0 or rerank_weight < 0:
        raise ValueError("Ağırlıklar negatif olamaz.")

    if rrf_weight + rerank_weight == 0:
        raise ValueError("En az bir ağırlık pozitif olmalı.")

    if not chunks or top_k <= 0:
        return []

    # Cross-Encoder bütün adayları sıralar.
    reranked = rerank(
        question,
        chunks,
        model_name=model_name,
        top_k=len(chunks),
    )

    # RRF skorlarına göre orijinal sıralama.
    rrf_ranked = sorted(
        chunks,
        key=lambda item: item.get("rrf_score", 0),
        reverse=True,
    )

    rrf_positions = {
        item["chunk_id"]: rank
        for rank, item in enumerate(rrf_ranked, start=1)
    }

    rerank_positions = {
        item["chunk_id"]: rank
        for rank, item in enumerate(reranked, start=1)
    }

    combined = []

    for chunk in reranked:
        chunk_id = chunk["chunk_id"]

        score = (
            rrf_weight
            / (rrf_k + rrf_positions[chunk_id])
            + rerank_weight
            / (rrf_k + rerank_positions[chunk_id])
        )

        result = chunk.copy()
        result["hybrid_rerank_score"] = score
        combined.append(result)

    combined.sort(
        key=lambda item: item["hybrid_rerank_score"],
        reverse=True,
    )

    return combined[:top_k]
