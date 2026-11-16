
import re

import numpy as np
from rank_bm25 import BM25Okapi

from src.embeddings import embed_query


def tokenize(text):
    """Türkçe ve İngilizce metinler için basit tokenizer."""
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def cosine_scores(query_vector, document_vectors):
    """Sorgu ile belge embedding'leri arasındaki benzerlik."""
    query = np.asarray(query_vector, dtype=np.float32)
    documents = np.asarray(document_vectors, dtype=np.float32)

    if documents.ndim != 2:
        raise ValueError("Embedding matrisi iki boyutlu olmalı.")

    if documents.shape[1] != query.shape[0]:
        raise ValueError("Embedding boyutları uyuşmuyor.")

    query_norm = np.linalg.norm(query)
    document_norms = np.linalg.norm(documents, axis=1)

    denominators = document_norms * query_norm

    return np.divide(
        documents @ query,
        denominators,
        out=np.zeros(len(documents), dtype=np.float32),
        where=denominators > 0,
    )


def semantic_search(question, chunks, model_name="minilm", top_k=20):
    """
    MySQL'den alınmış chunk kayıtları üzerinde semantic search.

    Her chunk:
      chunk_id, document_id, text, embedding
    alanlarını içermeli.
    """
    valid_chunks = [
        chunk for chunk in chunks
        if chunk.get("embedding") is not None
        and chunk.get("text")
    ]

    if not valid_chunks:
        return []

    query_vector = embed_query(question, model_name=model_name)

    vectors = np.asarray(
        [chunk["embedding"] for chunk in valid_chunks],
        dtype=np.float32,
    )

    scores = cosine_scores(query_vector, vectors)
    ranked_indices = np.argsort(-scores)[:top_k]

    results = []

    for idx in ranked_indices:
        chunk = valid_chunks[int(idx)]

        results.append({
            **chunk,
            "id": chunk["chunk_id"],
            "semantic_score": float(scores[idx]),
        })

    return results


def bm25_search(question, chunks, top_k=20):
    """MySQL'den alınan metinler üzerinde BM25 araması."""
    valid_chunks = [
        chunk for chunk in chunks
        if chunk.get("text")
    ]

    if not valid_chunks:
        return []

    tokenized_docs = [
        tokenize(chunk["text"])
        for chunk in valid_chunks
    ]

    bm25 = BM25Okapi(tokenized_docs)
    scores = bm25.get_scores(tokenize(question))

    ranked_indices = np.argsort(-scores)[:top_k]

    results = []

    for idx in ranked_indices:
        if scores[idx] <= 0:
            continue

        chunk = valid_chunks[int(idx)]

        results.append({
            **chunk,
            "id": chunk["chunk_id"],
            "bm25_score": float(scores[idx]),
        })

    return results


def rrf_fusion(semantic_hits, bm25_hits, k=60, top_k=10):
    """Semantic Search ve BM25 sonuçlarını RRF ile birleştirir."""
    scores = {}
    merged = {}

    for hits in (semantic_hits, bm25_hits):
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit["id"]

            scores[chunk_id] = (
                scores.get(chunk_id, 0)
                + 1.0 / (k + rank)
            )

            if chunk_id not in merged:
                merged[chunk_id] = hit.copy()

    ranked_ids = sorted(
        scores,
        key=lambda chunk_id: scores[chunk_id],
        reverse=True,
    )

    results = []

    for chunk_id in ranked_ids[:top_k]:
        hit = merged[chunk_id].copy()
        hit["rrf_score"] = scores[chunk_id]
        results.append(hit)

    return results


def hybrid_search(
    question,
    chunks,
    model_name="minilm",
    top_k=10,
    candidate_k=20,
):
    """
    Semantic Search + BM25 + RRF.

    chunks: MySQL'den okunmuş kayıtlar.
    """
    semantic_hits = semantic_search(
        question,
        chunks,
        model_name=model_name,
        top_k=candidate_k,
    )

    bm25_hits = bm25_search(
        question,
        chunks,
        top_k=candidate_k,
    )

    return rrf_fusion(
        semantic_hits,
        bm25_hits,
        top_k=top_k,
    )
