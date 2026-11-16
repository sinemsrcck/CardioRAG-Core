
from collections import defaultdict


def select_diverse_sources(
    hits,
    top_k=5,
    max_chunks_per_document=2,
):
    """
    Rerank sonuçlarından belge çeşitliliğini
    koruyarak en iyi kaynakları seçer.

    hits: İlgililik sırasına göre sıralanmış chunk'lar.
    """
    if top_k <= 0:
        return []

    if max_chunks_per_document <= 0:
        raise ValueError(
            "max_chunks_per_document pozitif olmalı."
        )

    selected = []
    document_counts = defaultdict(int)
    seen_chunks = set()

    for hit in hits:
        chunk_id = hit.get("chunk_id") or hit.get("id")
        document_id = hit.get("document_id")

        if not document_id:
            raise ValueError(
                "Her chunk için document_id gerekli."
            )

        if chunk_id is None:
            raise ValueError(
                "Her chunk için chunk_id gerekli."
            )

        if chunk_id in seen_chunks:
            continue

        if document_counts[document_id] >= max_chunks_per_document:
            continue

        selected.append(hit)
        seen_chunks.add(chunk_id)
        document_counts[document_id] += 1

        if len(selected) >= top_k:
            break

    return selected
