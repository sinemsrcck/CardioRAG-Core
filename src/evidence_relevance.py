"""Bounded bilingual lexical grounding, not semantic/clinical verification.

No scores, topic-specific question branches, models, dataset labels or network.
Unknown vocabulary/intents deliberately abstain. A passing result only permits
the downstream citation/claim gates to evaluate a generated answer.
"""
import math
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


LEGACY = "legacy"
TIERED = "tiered"
MODES = (LEGACY, TIERED)
TIERED_SCOPE = "bilingual-tiered-lexical-grounding-v1_not_entailment_or_clinical_verification"
# At least half of the general (non-critical) subject words that CAN occur in the
# corpus must be present in the evidence sentence. Not a calibrated score.
MIN_GENERAL_COVERAGE = 0.5
TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
STANDALONE_NUMBER = re.compile(r"(?<![a-z0-9])\d+(?:[.,]\d+)?(?![a-z0-9])")
# Turkish case/possessive suffix written after an apostrophe on a name: HCR'yi, AF'de, ELR'nin.
TR_APOSTROPHE_SUFFIX = re.compile(r"(\w)['’][a-zçğıöşüâî]+", re.IGNORECASE)
# Clinical concept codes produced by the shared bilingual normalization (claim_validator.ALIASES).
CLINICAL_CODES = frozenset({"elr", "plr", "hba1c", "hcr", "troponin", "af", "ami", "cas", "cad", "ischemic"})
# Statistical/task abbreviations are handled as tasks, never as clinical subjects.
TASK_ACRONYMS = frozenset({"roc", "auc", "ci", "or", "hr", "rr", "sd", "se", "iqr"})
# Same concept, different spelling in Turkish and English clinical text.
TIERED_TERMS = ((r"\bekg\b", "ecg"),)
# Patient groups: if the question names one, the same sentence must name the same group.
# "without diabetes" and "diabetic" are different groups (non-diabetic is normalized to "without diabetes").
PATIENT_GROUPS = (
    ("without_diabetes", r"\bwithout diabetes\b"),
    ("diabetic", r"(?<!non-)(?<!non )\bdiabetic\b"),
    ("study_group", r"\bstudy group\b|\b(?:hasta|patients|study) grub\w*"),
    ("control_group", r"\bcontrol group\b|\bcontrols\b|\bkontrol grub\w*"),
    ("pediatric", r"\bp(?:a)?ediatric\w*|\bchild\w*|\bcocuk\w*"),
    ("adult", r"\badults?\b|\byetiskin\w*"),
    ("elderly", r"\belderly\b|\byasli\w*"),
    ("female", r"\bwom[ae]n\b|\bfemales?\b|\bkadin\w*"),
    ("male", r"\bm[ae]n\b|\bmales?\b|\berkek\w*"),
)
_VOCABULARY_CACHE = {}


def tiered_normalize(text):
    """normalize_bilingual + Turkish apostrophe suffixes + a few bilingual spellings."""
    text = TR_APOSTROPHE_SUFFIX.sub(r"\1", str(text or ""))
    text = normalize_bilingual(text)
    for pattern, replacement in TIERED_TERMS:
        text = re.sub(pattern, replacement, text)
    return text


def corpus_vocabulary(texts):
    """Every normalized token of the corpus; used only to recognise words that cannot occur in it."""
    vocabulary = set()
    for text in texts:
        vocabulary.update(TOKEN_RE.findall(tiered_normalize(text)))
    return frozenset(vocabulary)


def vocabulary_for_chunks(chunks):
    """corpus_vocabulary for a chunk list, cached per exact (chunk_id, text) content."""
    key = tuple((chunk.get("chunk_id"), hash(chunk.get("text") or "")) for chunk in chunks)
    if key not in _VOCABULARY_CACHE:
        if len(_VOCABULARY_CACHE) >= 4:
            _VOCABULARY_CACHE.clear()
        _VOCABULARY_CACHE[key] = corpus_vocabulary(chunk.get("text") or "" for chunk in chunks)
    return _VOCABULARY_CACHE[key]


def _acronyms(question):
    """Clinical abbreviations written in the question (HCR, ELR, HbA1c, CCTA, FFR-CT, CYP2C19 …)."""
    found = set()
    for word in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", TR_APOSTROPHE_SUFFIX.sub(r"\1", question)):
        strong = sum(c.isupper() or c.isdigit() for c in word)
        if strong >= 2 and any(c.isupper() for c in word) and not word.isdigit():
            found.update(TOKEN_RE.findall(tiered_normalize(word)))
    return found - TASK_ACRONYMS


def tiered_profile(question, vocabulary=None):
    """Split the question subject into critical concepts, patient groups and general words.

    Critical concepts (clinical abbreviations / concept codes), numbers, patient groups and
    tasks must all occur in ONE evidence sentence. General words need MIN_GENERAL_COVERAGE.
    Only for a Turkish question: a general word that occurs nowhere in the (English) corpus
    cannot be matched by any evidence and is reported as exempt instead of blocking.
    """
    profile = query_profile(TR_APOSTROPHE_SUFFIX.sub(r"\1", question))
    q = tiered_normalize(question)
    tokens = set(TOKEN_RE.findall(q))
    anchors = {t for t in tokens if len(t) > 1 and t not in STOP}
    critical = anchors & (CLINICAL_CODES | _acronyms(question))
    groups = [name for name, pattern in PATIENT_GROUPS if re.search(pattern, q)]
    group_words = {w for name, pattern in PATIENT_GROUPS if name in groups
                   for w in TOKEN_RE.findall(" ".join(re.findall(pattern, q)))}
    general = anchors - critical - group_words
    turkish = bool(re.search(r"[çğıöşüİ]", question)) or bool(re.search(
        r"\b(?:nedir|nelerdir|nasil|hangi|midir|mudur|mi|mu|ile|ve|icin)\b", q))
    exempt = sorted(general - vocabulary) if (vocabulary is not None and turkish) else []
    matchable = sorted(general - set(exempt))
    critical_missing = sorted(critical - vocabulary) if vocabulary is not None else []
    # Standalone numbers only: the "1" in HbA1c or the "19" in CYP2C19 is not a value.
    numbers = STANDALONE_NUMBER.findall(q)
    tasks = list(profile["tasks"])
    # "X nedir?" with a patient group or a value is a value question, not a definition question.
    if "definition" in tasks and (groups or numbers):
        tasks.remove("definition")
    return {**profile, "numbers": numbers, "tasks": tasks,
            "normalized_question": q, "anchors": sorted(anchors), "critical": sorted(critical),
            "patient_groups": groups, "general_matchable": matchable, "exempt_out_of_corpus": exempt,
            "critical_absent_from_corpus": critical_missing, "turkish_question": turkish}


def _assess_tiered(question, text, vocabulary=None):
    profile = tiered_profile(question, vocabulary)
    result = {"relevant": False, "scope": TIERED_SCOPE, "mode": TIERED, "semantic_verified": False,
              "clinical_verified": False, **profile}
    if profile["unverified_premise"]:
        return {**result, "reason": "unverified_question_premise"}
    if not profile["anchors"]:
        return {**result, "reason": "no_specific_subject"}
    if profile["critical_absent_from_corpus"]:
        return {**result, "reason": "critical_term_absent_from_corpus"}
    general = profile["general_matchable"]
    if not profile["critical"] and len(general) < 1:
        return {**result, "reason": "no_matchable_subject"}
    # Without a critical concept, a single generic word is never enough.
    needed = math.ceil(MIN_GENERAL_COVERAGE * len(general)) if general else 0
    if not profile["critical"]:
        needed = max(needed, min(2, len(general)))
    for unit in _sentences(str(text or "")):
        p = tiered_normalize(unit)
        words = set(TOKEN_RE.findall(p))
        if not set(profile["critical"]) <= words:
            continue
        if any(n not in STANDALONE_NUMBER.findall(p) for n in profile["numbers"]):
            continue
        if not all(re.search(dict(PATIENT_GROUPS)[g], p) for g in profile["patient_groups"]):
            continue
        if not all(re.search(TASK_PATTERNS[t], p) for t in profile["tasks"]):
            continue
        if "detection" in profile["tasks"] and re.search(
            r"cannot be (?:detection|diagnosis)|can not be (?:detection|diagnosis)|"
            r"not (?:detection|diagnosis)|does not detect|not a diagnostic", p
        ):
            continue
        matched = sorted(set(general) & words)
        if len(matched) < needed:
            continue
        return {**result, "relevant": True, "reason": "tiered_subject_task_grounding",
                "general_matched": matched, "general_needed": needed}
    return {**result, "reason": "no_local_subject_task_grounding", "general_needed": needed}


def assess_relevance(question, text, mode=LEGACY, vocabulary=None):
    """Separate observable local grounding from unmeasured semantic sufficiency.

    mode="legacy": every subject word in one sentence (original behaviour, unchanged).
    mode="tiered": see tiered_profile; vocabulary = corpus_vocabulary(...) or None.
    """
    if mode not in MODES:
        raise ValueError("Unknown relevance mode")
    if mode == TIERED:
        return _assess_tiered(question, text, vocabulary)
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
