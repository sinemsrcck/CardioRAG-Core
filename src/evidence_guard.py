
def check_evidence(
    ranked_chunks,
    min_score=None,
    min_text_length=80,
):
    """
    Kaynakların temel uygunluğunu kontrol eder.

    min_score:
        Kalibre edilmiş Cross-Encoder skor eşiği.
        None ise skor eşiği uygulanmaz.
    """
    if not ranked_chunks:
        return {
            "sufficient": False,
            "reason": "no_retrieval_results",
        }

    valid = [
        chunk for chunk in ranked_chunks
        if len(chunk.get("text", "").strip()) >= min_text_length
    ]

    if not valid:
        return {
            "sufficient": False,
            "reason": "no_usable_evidence",
        }

    if min_score is not None:
        relevant = [
            chunk for chunk in valid
            if chunk.get("rerank_score") is not None
            and chunk["rerank_score"] >= min_score
        ]

        if not relevant:
            return {
                "sufficient": False,
                "reason": "below_relevance_threshold",
            }

        valid = relevant

    return {
        "sufficient": True,
        "reason": "passed_basic_checks",
        "chunks": valid,
    }
