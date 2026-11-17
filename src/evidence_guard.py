
import re


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def check_evidence(
    ranked_chunks,
    min_score=None,
    min_text_length=80,
    question=None,
    min_keyword_overlap=0.25,
    enforce_keyword_overlap=True,
):
    """
    Kaynakların temel kullanılabilirliğini ve
    soruyla sözcüksel ilişkisini kontrol eder.

    NOT:
    Bu kontrol, klinik iddiaların doğruluğunu
    veya kaynak tarafından desteklendiğini
    tek başına kanıtlamaz.

    enforce_keyword_overlap=False, diller arası sorgularda birebir
    sözcük eşleşmesini ret koşulu olmaktan çıkarır. Eski doğrudan
    çağrıların davranışı varsayılan olarak korunur. Bu durumda ilgililik
    elemesi için dışarıdan kalibre edilmiş min_score sağlanmalıdır;
    yalnızca kullanılabilir metin bulunması anlamsal yeterlilik değildir.
    """

    if not ranked_chunks:
        return {
            "sufficient": False,
            "reason": "no_retrieval_results",
            "chunks": [],
        }

    valid = [
        chunk
        for chunk in ranked_chunks
        if len(normalize_text(chunk.get("text"))) >= min_text_length
    ]

    if not valid:
        return {
            "sufficient": False,
            "reason": "no_usable_evidence",
            "chunks": [],
        }

    if min_score is not None:
        valid = [
            chunk
            for chunk in valid
            if chunk.get("rerank_score") is not None
            and chunk["rerank_score"] >= min_score
        ]

        if not valid:
            return {
                "sufficient": False,
                "reason": "below_relevance_threshold",
                "chunks": [],
            }

    if question and enforce_keyword_overlap:
        stopwords = {
            "nedir", "nasıl", "hangi", "nelerdir",
            "ile", "ve", "veya", "bir", "bu",
            "için", "olan", "olarak", "mıdır",
            "midir", "risk", "riski",
            "what", "which", "with", "the",
            "and", "for", "from", "are",
        }

        question_words = {
            word
            for word in re.findall(
                r"\b\w+\b",
                normalize_text(question).lower(),
            )
            if len(word) >= 3 and word not in stopwords
        }

        if question_words:
            relevant = []

            for chunk in valid:
                chunk_words = set(
                    re.findall(
                        r"\b\w+\b",
                        normalize_text(
                            chunk.get("text")
                        ).lower(),
                    )
                )

                overlap = (
                    len(question_words & chunk_words)
                    / len(question_words)
                )

                if overlap >= min_keyword_overlap:
                    relevant.append(chunk)

            if not relevant:
                return {
                    "sufficient": False,
                    "reason": "low_keyword_overlap",
                    "chunks": [],
                }

            valid = relevant

    return {
        "sufficient": True,
        "reason": "passed_relevance_filters",
        "chunks": valid,
    }
