"""Controlled clinical query translation, no answers, scores or model fallback.

Policies are independent per model. English queries are left byte-for-byte
unchanged. Negation, numeric values and measurement intervals are retained.
"""
import re
import unicodedata

# Selected by the frozen provisional Stage 4 comparison, not a calibrated score
# threshold. Set minilm="original" for an explicit baseline/rollback run.
# BGE-M3 stays unchanged until independently measured with local model files.
QUERY_POLICIES = {"minilm": "rewrite", "bge-m3": "original"}
ANCHORS = (
    (r"atriyal fibrilasyon", "atrial fibrillation"),
    (r"\baf hastalarinda", "patients with AF"),
    (r"karotis (?:arter )?darlig(?:indaki|inda|inin|i)?", "carotid stenosis"),
    (r"koroner arter hastalig(?:inin|inda|i)?", "coronary artery disease"),
    (r"akut miyokard enfarktusu", "acute myocardial infarction"),
    (r"troponin degisimi", "delta troponin"),
    (r"guven araligi", "confidence interval"),
    (r"ohca derlemesinde", "OHCA scoping review"),
    (r"\bbahreyn\b", "Bahrain"),
)
PHRASES = (
    (r"hangi yontemlerle tespit edilebilir", "detection methods"),
    (r"hasta ve kontrol sayilari", "patient and control counts"),
    (r"calismasindaki", "study"), (r"roc sonuclari", "ROC results"),
    (r"hangi oneriyle kullanilir", "guideline recommendation"),
    (r"literatur bulunmus mudur", "literature identified"),
    (r"nasil degerlendirilir", "assessment"), (r"tanisinda", "diagnosis"),
    (r"diyabeti olmayan", "without diabetes"), (r"diyabetli", "diabetic"),
    (r"\bolmayan\b", "without"), (r"\bdegil\b", "not"), (r"\bharic\b", "excluding"),
    (r"\bonce\b", "before"), (r"\bsonra\b", "after"),
    (r"\bkritik\b", "critical"), (r"\bve\b", "and"),
    (r"\bicin\b", "for"), (r"\b(?:nedir|nelerdir)\b", ""),
)


def controlled_translation(question):
    folded = "".join(c for c in unicodedata.normalize("NFKD", question.casefold().replace("ı", "i"))
                     if not unicodedata.combining(c))
    if not any(re.search(pattern, folded) for pattern, _ in ANCHORS):
        return question
    for pattern, replacement in ANCHORS + PHRASES:
        folded = re.sub(pattern, replacement, folded)
    return re.sub(r"\s+", " ", folded).strip()


def policy(model_name):
    mode = QUERY_POLICIES.get(model_name, "original")
    if mode not in ("original", "expand", "rewrite"):
        raise ValueError("Unknown query normalization policy")
    return mode


def retrieval_queries(question, model_name):
    mode = policy(model_name)
    translated = controlled_translation(question) if mode != "original" else question
    if mode == "rewrite":
        return [translated]
    return list(dict.fromkeys((question, translated)))


def reranking_query(question, model_name):
    return controlled_translation(question) if policy(model_name) == "rewrite" else question
