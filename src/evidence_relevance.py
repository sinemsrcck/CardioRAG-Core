"""Bounded bilingual lexical grounding, not semantic/clinical verification.

No scores, topic-specific question branches, models, dataset labels or network.
Unknown vocabulary/intents deliberately abstain. A passing result only permits
the downstream citation/claim gates to evaluate a generated answer.
"""
import re

from src.claim_validator import ALIASES, _canon, _fold, _sentences

SCOPE = "bilingual-local-lexical-grounding-v1_not_entailment_or_clinical_verification"

# General language/measurement vocabulary, not question IDs or article titles.
WORDS = (
    (r"\b(?:primary|main) (?:research )?(?:aim|objective|purpose)s?\b", "purpose"),
    (r"\bcalis\s*ma\w*\b|\bcalisma\w*\b|\barastirma\w*\b", "study"),
    (r"\bamac\w*\b|\baims?\b|\bobjectives?\b|\bpurposes?\b", "purpose"),
    (r"\btasarim\w*\b|\bdesign\b", "design"),
    (r"\byontem\w*\b|\bmethods?\b", "method"),
    (r"\btespit\w*\b|\bsaptan\w*\b|\bdetect\w*\b", "detection"),
    (r"\btani\w*\b|\bdiagnos\w*\b", "diagnosis"),
    (r"\btarama\w*\b|\bscreen\w*\b", "screening"),
    (r"\bdegisim\w*\b|\bdegis\w*\b|\bchanges?\b", "change"),
    (r"\bdegerlendir\w*\b|\bassess\w*\b|\bevaluat\w*\b", "assessment"),
    (r"\bhasta\w*\b|\bpatients?\b", "patients"),
    (r"\bkarsilastir\w*\b|\bcompar\w*\b", "comparison"),
    (r"\bilisk\w*\b|\bassociat\w*\b|\brelationship\b", "association"),
    (r"\brisk\w*\b", "risk"), (r"\binme\w*\b", "stroke"),
    (r"\boran\w*\b|\bratios?\b", "ratio"),
    (r"\bpeptid\b", "peptide"),
    (r"\bduyarlilik\w*\b", "sensitivity"),
    (r"\bozgulluk\w*\b", "specificity"),
    (r"\bsonuc\w*\b|\bresults?\b|\boutcomes?\b", "result"),
    (r"\bderleme\w*\b|\breviews?\b", "review"),
    (r"\bfonksiyonel\b|\bfunctional\b", "functional"),
    (r"\btestler\b|\btests\b", "test"),
    (r"\btablolar?\b|\btablosunda\b|\btables?\b", "table"),
    (r"\bcoronary artery disease\b", "cad"),
)
STOP = set("""what which how why when where is are was were can could would should
    do does did has have had be been being a an the of for in on at to from by with
    without and or as this that these those its their our it used use using through
    during between according about reported described nedir nelerdir nasil hangi
    neden ne kac mi mu midir mudur edilir edilebilir kullanilir olan olarak ile ve
    veya bir bu su icin temel inceleyen bildirilen belirtilen bulunur verilir
    study research paper article clinical patients review purpose design method
    detection diagnosis screening change delta assessment comparison result risk
    association sensitivity specificity auc ci confidence interval value values
    number count counts criteria definition techniques approach methods
    tanimlanmistir tanimlanir tanimi bulgulari tarafindan gore yollarla eksik uygulanmistir verisi islem listelenir listelenen siralanir belirtilir hangileri""".split())


def normalize_bilingual(text):
    text = _fold(text)
    for phrase, replacement in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        text = re.sub(r"(?<!\w)" + re.escape(phrase) +
                      r"(?:nun|nin|unun|inin|un|in|inda|inde|unda|unde)\b", replacement, text)
    text = _canon(text).replace("_", " ")
    # Possessive suffixes do not change a named biomarker/device identity.
    text = re.sub(r"['’](?:nin|n[ui]n|[ui]n)\b", "", text)
    for pattern, replacement in WORDS:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def query_profile(question):
    q = normalize_bilingual(question)
    tokens = set(re.findall(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", q))
    anchors = sorted(t for t in tokens if len(t) > 1 and t not in STOP)
    tasks = []
    for name, pattern in (
        ("purpose", r"\bpurpose\b"), ("design", r"\bdesign\b"),
        ("detection", r"\b(?:detection|diagnosis|screening)\b"),
        ("change", r"\b(?:change|delta)\b"), ("risk", r"\brisk\b"),
        ("sensitivity", r"\bsensitivity\b"), ("specificity", r"\bspecificity\b"),
        ("auc", r"\bauc\b"),
    ):
        if re.search(pattern, q):
            tasks.append(name)
    if not tasks and re.search(r"what is|\bnedir\b", q) and len(anchors) <= 2:
        tasks.append("definition")
    return {"anchors": anchors, "tasks": tasks,
            "numbers": re.findall(r"\d+(?:[.,]\d+)?", q), "normalized_question": q,
            "unverified_premise": bool(re.search(
                r"kabul eder|oncul|assum\w*|accept\w*.*premise|"
                r"(?:kanitladigini|kesin.*kanit)|"
                r"\b(?:prove[sd]?|proven|guarantee\w*|certainly|definitely)\b|"
                r"\d.*(?:dogrular|dogru kabul|confirm|verify)", q))}


TASK_PATTERNS = {
    "purpose": r"\bpurpose\b|designed to|sought to",
    "design": r"retrospect\w*|prospect\w*|cohort|case.control|randomiz\w*|cross.sectional|scoping|kesitsel",
    "detection": r"\b(?:detection|diagnosis|screening)\b|diagnostic|\bassessment for\b",
    "change": r"\b(?:delta|change)\b",
    # Explicit predictive statistics can be useful context for a risk question;
    # this does not turn discrimination into absolute risk or causality.
    "risk": r"\b(?:risk|association|auc)\b|odds ratio|hazard ratio",
    "sensitivity": r"\bsensitivity\b", "specificity": r"\bspecificity\b",
    "auc": r"\bauc\b|area under.*curve",
    "definition": r"\bis (?:a|an)\b|defined|refers to|arrhythmia|disorder|disease",
}


def assess_relevance(question, text):
    """Separate observable local grounding from unmeasured semantic sufficiency."""
    profile = query_profile(question)
    result = {"relevant": False, "scope": SCOPE, "semantic_verified": False,
              "clinical_verified": False, **profile}
    if profile["unverified_premise"]:
        return {**result, "reason": "unverified_question_premise"}
    if not profile["anchors"]:
        return {**result, "reason": "no_specific_subject"}
    # A question with unsupported extra words cannot silently lose its scope.
    for unit in _sentences(str(text or "")):
        p = normalize_bilingual(unit)
        words = set(re.findall(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", p))
        if not set(profile["anchors"]) <= words:
            continue
        if any(n not in re.findall(r"\d+(?:[.,]\d+)?", p) for n in profile["numbers"]):
            continue
        if not all(re.search(TASK_PATTERNS[t], p) for t in profile["tasks"]):
            continue
        if "detection" in profile["tasks"] and re.search(
            r"cannot be (?:detection|diagnosis)|can not be (?:detection|diagnosis)|"
            r"not (?:detection|diagnosis)|does not detect|not a diagnostic", p
        ):
            continue
        return {**result, "relevant": True, "reason": "local_subject_task_grounding"}
    return {**result, "reason": "no_local_subject_task_grounding"}
