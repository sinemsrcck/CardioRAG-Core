
from collections import defaultdict


def select_diverse_sources(
    hits,
    top_k=5,
    max_chunks_per_document=2,
):
    """
    En iyi top_k içinden belge sınırını uygular; alt sıralarla doldurmaz.
    İlgililik elemesi çağıranda yapılır. None belge sınırını kaldırır.

    hits: İlgililik sırasına göre sıralanmış chunk'lar.
    """
    if top_k <= 0:
        return []

    if max_chunks_per_document is not None and (
        type(max_chunks_per_document) is not int or max_chunks_per_document <= 0
    ):
        raise ValueError(
            "max_chunks_per_document pozitif tamsayı veya None olmalı."
        )

    selected = []
    document_counts = defaultdict(int)
    seen_chunks = set()

    # Diversity may remove members of the best eligible top_k, but must never
    # promote a lower-ranked tail merely to fill empty context slots.
    candidates = []
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
        seen_chunks.add(chunk_id)
        candidates.append(hit)
        if len(candidates) >= top_k:
            break

    for hit in candidates:
        document_id = hit["document_id"]
        if max_chunks_per_document is not None and document_counts[document_id] >= max_chunks_per_document:
            continue
        selected.append(hit)
        document_counts[document_id] += 1

    return selected
