
import re


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def demo_relevance(question, text):
    """Conservative bilingual demo facets, not calibrated semantic certainty.

    Recognises only the documented AF/CAD/troponin intents. Unsupported intents
    abstain. Facets must coexist in a source paragraph; scores and source count
    are never proof. This does not validate claims or clinical truth.
    """
    query = normalize_text(question).casefold()
    af = r"\baf\b|atrial fibrillation|atriyal fibrilasyon\w*"
    cad = r"\bcad\b|coronary artery disease|koroner arter hastal\w*"
    troponin = r"troponin"
    if re.search(af, query):
        entity = af
        if re.search(r"tespit|saptan|detect|screen", query):
            intent = "af_detection"
            facets = (r"(?:\baf\b|atrial fibrillation)\s+(?:detection|screening|can be detected|is detected|was detected|was diagnosed)",
                      r"ecg|electrocardiogram|wearable|implanted|implantable|loop recorder|pacemaker")
        elif re.search(r"stroke|inme", query):
            intent, facets = "af_stroke", (r"stroke|inme",)
        elif re.search(r"what is|nedir", query):
            intent, facets = "af_definition", (r"arrhythmia|irregular|aritmi|düzensiz",)
        elif query in ("atrial fibrillation", "af", "atriyal fibrilasyon"):
            intent, facets = "af_topic", (r"patient|clinical|risk|arrhythmia|hasta",)
        else:
            return False, "unsupported_intent"
    elif re.search(cad, query) and re.search(r"diagnos|tanı|yöntem|method", query):
        entity, intent = cad, "cad_diagnosis"
        facets = (r"diagnos|detect|screen|testing|tanı", r"\bccta\b|\bcmr\b|\bspect\b|\bpet\b|\becg\b|echocardiograph|angiograph|computed tomography")
    elif re.search(troponin, query) and re.search(r"değiş|change|delta", query):
        entity, intent = troponin, "troponin_delta"
        facets = (r"delta|change|değiş", r"\bami\b|\bmi\b|myocardial infarction|miyokard enfarkt")
    else:
        return False, "unsupported_intent"
    paragraphs = re.split(r"\n\s*\n", str(text or ""))
    supported = any(re.search(entity, normalize_text(p).casefold())
                    and all(re.search(f, normalize_text(p).casefold()) for f in facets)
                    and not re.search(r"cannot be detected|can not be detected|does not detect|not a diagnostic",
                                      normalize_text(p).casefold())
                    for p in paragraphs)
    return supported, intent


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

    enforce_keyword_overlap=False diller arası birebir sözcük zorunluluğunu
    kaldırır; sınırlı demo niyetleri için iki dilli konu/görev kontrolü yine
    zorunludur. Desteklenmeyen sorularda çekimser kalır. min_score verilse
    bile skor tek başına yeterlilik değildir. Bu kontroller klinik iddia
    doğrulaması veya kalibre edilmiş anlamsal yeterlilik ölçümü değildir.
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

    # A usable paragraph or a positive/thresholded score is not sufficient.
    if not question:
        return {"sufficient": False, "reason": "question_required", "chunks": []}
    supported = [chunk for chunk in valid if demo_relevance(question, chunk.get("text"))[0]]
    if not supported:
        return {"sufficient": False, "reason": "no_explicit_demo_support", "chunks": []}
    valid = supported
    return {
        "sufficient": True,
        "reason": "passed_relevance_filters",
        "chunks": valid,
    }
