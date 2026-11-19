"""Free, bounded local evidence checks. Not clinical truth or expert approval.

Accepted: complete literal sentences and a closed bilingual numeric grammar.
Unsupported language, multi-level tables, inferred
statistics, broad paraphrases and cross-chunk synthesis abstain. No API or SQL.
"""
import hashlib
import re
import unicodedata
from decimal import Decimal

from src.citation_validator import CITATION_PATTERN, extract_citations

SCOPE = "deterministic-local-text-v3_not_scientific_or_expert_verification"
NUM = r"\d+(?:[.,]\d+)?"
ENTITIES = r"\b(?:elr|plr|hba1c|hcr|troponin_i)\b"
ALIASES = {
    "eosinophil-to-lymphocyte ratio": "elr", "eosinophil/lymphocyte ratio": "elr",
    "platelet-to-lymphocyte ratio": "plr", "platelet/lymphocyte ratio": "plr",
    "eozinofil/lenfosit orani": "elr", "trombosit/lenfosit orani": "plr",
    "hb a1c": "hba1c", "high-sensitivity troponin i": "troponin_i",
    "high sensitivity troponin i": "troponin_i", "hs troponin i": "troponin_i",
    "yuksek duyarlilikli troponin i": "troponin_i", "hs-troponin i": "troponin_i",
    "atrial fibrillation": "af", "atriyal fibrilasyon": "af",
    "acute myocardial infarction": "ami", "akut miyokard enfarktusu": "ami",
    "karotis arter darligi": "cas", "carotid artery stenosis": "cas",
    "coronary artery disease": "cad", "koroner arter hastaligi": "cad",
    "ischemic events": "ischemic_event", "iskemik olaylar": "ischemic_event",
    "iskemik olaylari": "ischemic_event", "iskemik olaylarin": "ischemic_event",
    "ischemic stroke": "ischemic_stroke", "iskemik inme": "ischemic_stroke",
    "hasta grubu": "study group", "calisma grubu": "study group",
    "kontrol grubu": "control group", "toplam": "total",
    "diyabeti olmayan": "without diabetes", "non-diabetic": "without diabetes",
    "nondiabetic": "without diabetes", "diyabetli": "diabetic",
    "duyarlilik": "sensitivity", "ozgulluk": "specificity",
    "esik degeri": "cutoff", "kesme degeri": "cutoff", "cut-off": "cutoff",
    "cut off": "cutoff", "esik": "cutoff", "threshold": "cutoff",
    "saat": "hours", "saatte": "hours", "saatlik": "hours",
    "standard error": "se", "standart hata": "se",
    "icin": "for", "ve": "and", "degeri": "value", "bulundu": "found",
    "saptandi": "found", "olarak": "as", "idi": "was",
    "bildirilmistir": "reported", "degerlendirilmistir": "evaluated",
    "kisiden olusmustur": "patients", "bahreyn": "bahrain",
}


def _fold(text):
    text = str(text or "").casefold().replace("ı", "i")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _canon(text):
    text = CITATION_PATTERN.sub("", str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = _fold(text).replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("≤", "<=").replace("≥", ">=")
    text = re.sub(r"\ba\s+u\s+c\b", "auc", text)
    text = re.sub(r"\bci\s*95\b", "95% ci", text)
    for phrase, replacement in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        text = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", replacement, text)
    text = re.sub(r"\b(elr|plr|hba1c|hcr)'(?:nin|in|nun|un)\b", r"\1", text)
    # Reorder only explicitly labelled counts; never infer a group from proximity.
    text = re.sub(rf"\b({NUM})\s+patients\s+were\s+in\s+the\s+(study group|control group)\b",
                  r"\2 n=\1", text)
    text = re.sub(r"\bhs between (\d+\s*-\s*\d+\s*hours) troponin i\b", r"troponin_i \1", text)
    return re.sub(r"\s+", " ", text.replace("**", "")).strip(" \t\n.*•-")


def _origin(text):
    text = _fold(text)
    guideline = bool(re.search(r"guideline|kilavuz|class\s+ii?a|sinif\s+ii?a", text))
    secondary = bool(re.search(r"et\s+al\.|study\s+(?:conducted\s+)?by|previous study|another study|\breview\b|\bderleme\b|meta-analysis|"
                              r"baska calisma|onceki calisma|arkadaslarinin|<sup>\d+|\[\d+\]", text))
    own = bool(re.search(r"this study|our study|present study|original study|bu calismada|bizim calisma", text))
    if sum((guideline, secondary, own)) > 1:
        return "ambiguous"
    return "guideline" if guideline else "secondary" if secondary else "original" if own else "unspecified"


def _sentences(text):
    # Do not split decimals or common English citation abbreviations.
    text = re.sub(r"\bet al\.", lambda m: m[0][:-1] + "<ABBR>", text, flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ\[\d])|\n\s*\n|\n(?=\s*[-*•]\s)", text)
    units = []
    for part in parts:
        part = part.replace("<ABBR>", ".").strip()
        prefix = re.match(r"^(\s*(?:\[[\d,\s]+\]\s*)+)(.*)$", part, re.S)
        if prefix and units:
            units[-1] += " " + prefix[1].strip()
            part = prefix[2]
        if part.strip() and not re.fullmatch(r"[\s.!?]+", part):
            units.append(part.strip())
    return units


def _value(number, count=False):
    if count:
        return str(int(number.replace(",", "").replace(".", "")))
    return str(Decimal(number.replace(",", ".")).normalize())


def parse_claim(text):
    """Closed role-labelled extraction; residual content is deliberately exposed."""
    canonical = _canon(text)
    entities = sorted(set(re.findall(ENTITIES, canonical)))
    metrics, spans, ambiguous = {}, [], False
    ambiguous_metrics = set()

    def add(kind, match, values):
        nonlocal ambiguous
        if kind in metrics and metrics[kind] != list(values):
            ambiguous = True
            ambiguous_metrics.add(kind)
        metrics[kind] = list(values)
        spans.append(match.span())

    patterns = {
        "auc": rf"\bauc\s*(?:value\s*)?(?:of|was|is|=|:)?\s*({NUM})",
        "ci95": rf"(?:95\s*%?\s*(?:ci|ga|confidence interval)|%\s*95\s*(?:ci|ga))\s*[:=]?\s*({NUM})\s*-\s*({NUM})",
        "p": rf"\bp\s*([<>=]{{1,2}})\s*({NUM})",
        "sensitivity": rf"\bsensitivity\s*(?:of|was|is|=|:)?\s*\(?\s*%?\s*({NUM})\s*%?\s*\)?",
        "specificity": rf"\bspecificity\s*(?:of|was|is|=|:)?\s*\(?\s*%?\s*({NUM})\s*%?\s*\)?",
        "cutoff": rf"\bcutoff\s*(?:value\s*)?(?:of|was|is|=|:)?\s*([<>]=?|=)?\s*%?\s*({NUM})\s*%?",
    }
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, canonical):
            if kind == "p":
                # The malformed publication string p<001 must never become p<0.001.
                if re.fullmatch(r"0\d+", match[2]):
                    ambiguous = True
                    ambiguous_metrics.add("p")
                values = (match[1], _value(match[2]))
            elif kind == "cutoff":
                values = (match[1] or "=", _value(match[2]), "percent" if "%" in match[0] else "unspecified")
            else:
                values = tuple(_value(v) for v in match.groups())
            add(kind, match, values)
    for kind in ("sensitivity", "specificity"):
        for match in re.finditer(rf"%?\s*({NUM})\s*%?\s+{kind}\b", canonical):
            add(kind, match, (_value(match[1]),))
    for match in re.finditer(rf"\bauc\s*(?:of|was|is|=|:)?\s*{NUM}\s*±\s*({NUM})", canonical):
        typed = bool(re.search(r"\bse\b", canonical))
        add("standard_error" if typed else "untyped_uncertainty", match, (_value(match[1]),))
        if not typed:
            ambiguous = True
            ambiguous_metrics.add("untyped_uncertainty")
    for kind, label in (("study_count", "study group"), ("control_count", "control group"), ("total_count", "total")):
        for match in re.finditer(rf"\b{label}\s*(?:included|consisted of|n|=|:|\(|\s)*\s*({NUM})\s*(?:patients|hasta|controls)?", canonical):
            if not re.fullmatch(r"\d+|\d{1,3}(?:[.,]\d{3})+", match[1]):
                ambiguous = True
                ambiguous_metrics.add(kind)
            add(kind, match, (_value(match[1], count=True),))
    times = []
    for match in re.finditer(r"\b(\d+)\s*(?:h|hours)?\s*-\s*(\d+)\s*(?:h\b|hours\b)", canonical):
        times.append((int(match[1]), int(match[2])))
        spans.append(match.span())
    diseases = sorted(set(re.findall(r"\b(?:af|ami|cad|cas|ischemic_event|ischemic_stroke|mortality)\b", canonical)))
    populations = []
    for pattern, name in ((r"without diabetes", "without_diabetes"), (r"diabetic|diabetes", "diabetes"),
                          (r"severe|critical|kritik", "severe"), (r"mild|hafif", "mild"),
                          (r"pediatric|children|cocuk", "pediatric"), (r"adult|yetiskin", "adult")):
        if re.search(pattern, canonical) and not (name == "diabetes" and "without_diabetes" in populations):
            populations.append(name)
    for disease in diseases:
        if re.search(r"\b(?:without|no)\s+" + disease + r"\b", canonical):
            populations.append(disease + "_absent")
        elif re.search(r"\b" + disease + r"\s+(?:patients|hastalari|hastalarinda)\b|\bpatients\s+with\s+" + disease + r"\b", canonical):
            populations.append(disease + "_present")
    for label in ("study group", "control group"):
        if label in canonical:
            populations.append(label.replace(" ", "_"))
    residual = list(canonical)
    for start, end in spans:
        residual[start:end] = " " * (end-start)
    residual = "".join(residual)
    residual = re.sub(ENTITIES + r"|\b(?:af|ami|cad|cas|ischemic_event|ischemic_stroke|mortality)\b", " ", residual)
    residual = re.sub(r"\b(?:this study|our study|present study|original study|bu calismada|without diabetes)\b", " ", residual)
    allowed = {"for", "the", "of", "was", "is", "in", "and", "an", "a", "had", "with", "showed", "showing",
        "value", "found", "as", "patients", "patient", "hasta", "hastalari", "hastalarinda", "among", "diabetic", "reported",
        "diabetes", "severe", "critical", "kritik", "mild", "hafif", "adult", "yetiskin", "pediatric", "children", "cocuk",
        "delta", "between", "at", "arasi", "arasinda", "icin", "ve", "deger", "bulunmustur", "saptanmistir", "se"}
    residual_words = [w for w in re.findall(r"[\w]+|[<>±%]", residual) if w not in allowed]
    return {"entities": entities, "metrics": metrics, "diseases": diseases,
        "patient_group": sorted(populations), "comparison_group": "control" if "control_count" in metrics else None,
        "measurement_time": [list(t) for t in sorted(set(times))], "origin": _origin(text),
        "residual": residual_words, "ambiguous": ambiguous,
        "ambiguous_metrics": sorted(ambiguous_metrics),
        "negated": bool(re.search(r"\b(?:not|cannot|never|degil|degildir|saptanmadi|bulunmadi)\b", canonical))}


def _evidence_units(source):
    text = source["source_text"]
    for paragraph in re.split(r"\n\s*\n", text):
        if "|" in paragraph:
            continue  # No guessing at multi-level or flattened table columns.
        origin = _origin(paragraph)
        for sentence in _sentences(paragraph):
            # A publication footnote after punctuation must not glue two
            # sentences together. Slice the original text: quotes stay literal.
            ends = [m.end() for m in re.finditer(
                r"[.!?]<sup>\d+(?:[,\u2013-]\d+)*</sup>(?=\s+[A-Z])", sentence)]
            starts = [0] + ends
            stops = ends + [len(sentence)]
            parts = [sentence[a:b].strip() for a, b in zip(starts, stops) if sentence[a:b].strip()]
            if len(set(re.findall(ENTITIES, _canon(sentence)))) > 1:
                parts = [p for part in parts for p in re.split(r";\s*", part)]
            for part in parts:
                yield part, origin


def _statistic_source(text):
    """Read known prose layouts without changing the stored PDF quotation.

    These are full structural matches, not a stopword bag. Clinical qualifiers
    and every statistic remain in the comparison; unknown layouts stay unknown.
    """
    text = _canon(text)
    figure = re.fullmatch(r"(?:[a-z]\)\s*)?roc curve for (elr|plr|hba1c|hcr) "
        r"(?:showing|with|demonstrating) an? auc of (.+?), "
        r"(?:indicating a (?:moderate discriminative ability|low discriminatory power)|suggesting a moderate predictive value)", text)
    if figure:
        return figure[1] + " auc of " + figure[2]
    roc = re.fullmatch(r"in the roc analysis, the cutoff value of the (hba1c|hcr) value "
        rf"in patients experiencing (.+?) was found to be ({NUM}), with ({NUM})% sensitivity "
        rf"and ({NUM})% specificity \((.+)\) \(figure \d+\)", text)
    if roc:
        return (f"{roc[1]} patients with {roc[2]} cutoff={roc[3]} sensitivity={roc[4]}% "
                f"specificity={roc[5]}% {roc[6]}")
    troponin = re.fullmatch(r"according to the cutoff value, the maximum diagnostic accuracy is delta "
        rf"troponin_i (\d+\s*-\s*\d+\s*hours) ([<>]=?{NUM}%) had a sensitivity "
        rf"\(({NUM})%\) and specificity \(({NUM})%\) with auc of ({NUM}(?:\s*±\s*{NUM})?) \((p\s*[<>=]+\s*{NUM})\)", text)
    if troponin:
        return (f"delta troponin_i {troponin[1]} cutoff{troponin[2]} sensitivity={troponin[3]}% "
                f"specificity={troponin[4]}% auc={troponin[5]} {troponin[6]}")
    return text


def _controlled_fact(text, source_text=""):
    """Small bilingual predicates with all arguments retained. Not free translation."""
    # Publication footnote markers are not clinical quantities. Other numbers,
    # qualifiers and unknown wording must survive the full structural match.
    text = re.sub(r"<sup>\d+(?:[,\u2013-]\d+)*</sup>", "", text)
    text = _canon(text)
    af_prose = (
        ("af_ecg_duration", (
            r"the probability of af detection increases with electrocardiogram \(ecg\) monitoring duration",
            r"(?:elektrokardiyografi \(ekg\) izlemi:\s*)?ekg izlemi uzadikca af saptanma olasiligi artar")),
        ("af_screening_uncertain", (
            r"the benefits of screening for af in the general population to detect asymptomatic af are currently uncertain",
            r"genel populasyonda asemptomatik af['\u2019]yi taramanin yararlari konusunda belirsizlik bulunmaktadir")),
        ("af_incidental_detection", (
            r"(?:klinik basvuru sirasinda tesadufi saptama:\s*)?af, belirti vermese bile klinik degerlendirme sirasinda fark edilebilir",)),
        ("af_implanted_interrogation", (
            r"implante edilebilir kalp cihazlarinin sorgulanmasi:\s*bu cihazlarin kayitlari incelenerek af saptanabilir",)),
    )
    for kind, patterns in af_prose:
        if any(re.fullmatch(p, text) for p in patterns):
            return {"kind": kind}
    detection = re.fullmatch(r"af can be detected (.+)", text) or re.fullmatch(r"af (.+) tespit edilebilir", text)
    if detection:
        tail = detection[1]
        methods = []
        for phrases, key in (
            (("clinical encounters", "klinik karsilasmalarda"), "clinical_encounters"),
            (("wearable devices", "giyilebilir cihazlarla"), "wearable_devices"),
            (("interrogation of cardiac implanted electronic devices",
              "implante kardiyak elektronik cihazlarin sorgulanmasiyla",
              "implante edilmis kardiyak elektronik cihazlarin sorgulanmasiyla"), "implanted_device_interrogation")):
            for phrase in phrases:
                if phrase in tail:
                    tail = tail.replace(phrase, " ")
                    methods.append(key)
        tail = re.sub(r"\b(?:incidentally|during|with|or|through|and|veya|ve|tesadufen)\b", " ", tail)
        if methods and not re.search(r"\w", tail):
            return {"kind": "af_detection", "methods": sorted(set(methods))}
    country = re.fullmatch(r"no literature was identified for ([a-z ]+)", text)
    country_tr = re.fullmatch(r"derleme taramasinda ([a-z ]+) for literatur saptanmamistir", text)
    if country or country_tr:
        if country_tr or re.search(r"scoping review|systematic review", _fold(source_text)):
            return {"kind": "review_search_absence", "country": (country or country_tr)[1]}
    cohort = re.fullmatch(r"troponin calismasinda (\d+) hasta evaluated", text)
    own_cohort = re.fullmatch(r"the final evaluation was made on (\d+) patients", text)
    if cohort or (own_cohort and re.search(r"\btroponin\b", _fold(source_text))
                  and re.search(r"excluded from the study|\bresults\b", _fold(source_text))
                  and _origin(source_text) in ("unspecified", "original")):
        return {"kind": "troponin_cohort_total", "n": (cohort or own_cohort)[1]}
    guideline_en = re.search(r"guideline for the evaluation and diagnosis of chest pain classifies ffr-ct as a class "
        rf"(iia|iib|iii|ii|i) recommendation for intermediate-high risk patients presenting with acute or stable chest pain "
        rf"and exhibiting coronary stenosis of ({NUM})% to ({NUM})% in a proximal or middle coronary segment on ccta$", text)
    guideline_tr = re.fullmatch(r"orta-yuksek riskli akut veya stabil gogus agrili and ccta proksimal/orta segmentinde "
        rf"%({NUM})-({NUM}) stenoz bulunan hastalarda ffr-ct sinif (iia|iib|iii|ii|i) onerisi as aktarilmistir", text)
    if guideline_en or guideline_tr:
        cls, lo, hi = guideline_en.groups() if guideline_en else (guideline_tr[3], guideline_tr[1], guideline_tr[2])
        return {"kind": "ffr_ct_guideline", "class": cls, "stenosis_percent": [_value(lo), _value(hi)],
                "population": "intermediate_high_risk_acute_or_stable_chest_pain",
                "segment": "proximal_or_middle", "imaging": "ccta"}
    return None


def _match(claim, fields, evidence, origin, source_text="", question_fields=None):
    actual = parse_claim(_statistic_source(evidence))
    if fields["ambiguous"] or origin == "ambiguous":
        return "uncertain", "ambiguous_statistics_or_origin"
    claim_fact, evidence_fact = _controlled_fact(claim), _controlled_fact(evidence, source_text)
    if claim_fact and evidence_fact and claim_fact["kind"] == evidence_fact["kind"]:
        if fields["negated"] or actual["negated"]:
            return "uncertain", "negation_outside_literal_scope"
        if claim_fact["kind"] == "af_detection":
            if set(claim_fact["methods"]) <= set(evidence_fact["methods"]):
                return "supported", "controlled_bilingual_detection"
        elif claim_fact == evidence_fact:
            return "supported", "controlled_bilingual_" + claim_fact["kind"]
        return "not_supported", "controlled_fact_arguments_mismatch"
    # Two bounded projections of the existing explicit detection predicate.
    # No co-occurrence, broad synonym bag or arbitrary substring entails a fact.
    detection_methods = {"af_incidental_detection": "clinical_encounters",
                         "af_implanted_interrogation": "implanted_device_interrogation"}
    if (claim_fact and claim_fact["kind"] in detection_methods and evidence_fact
            and evidence_fact["kind"] == "af_detection"
            and not fields["negated"] and not actual["negated"]
            and detection_methods[claim_fact["kind"]] in evidence_fact["methods"]):
        return "supported", "controlled_bilingual_detection_projection"
    if fields["origin"] != "unspecified" and fields["origin"] != origin:
        return "not_supported", "source_origin_mismatch"
    # Exact complete sentences are local quotation support, not clinical truth.
    if _canon(claim) == _canon(evidence):
        return "supported", "complete_literal_sentence"
    if fields["negated"] or actual["negated"]:
        return "uncertain", "negation_outside_literal_scope"
    if not fields["metrics"] or fields["ambiguous"] or fields["residual"]:
        return "uncertain", "outside_closed_numeric_grammar"
    counts = {"total_count", "study_count", "control_count"}
    if (not fields["entities"] and not set(fields["metrics"]) <= counts) or len(fields["entities"]) > 1:
        return "uncertain", "ambiguous_measurement_entity"
    if fields["entities"] != actual["entities"]:
        return "not_supported", "measurement_entity_mismatch"
    for key in ("diseases", "patient_group", "comparison_group", "measurement_time"):
        if fields[key] != actual[key]:
            # An explicitly scoped question may provide missing context, never
            # contradict or erase an assertion in the answer. No statistics are inherited.
            missing = [v for v in actual[key] if v not in fields[key]] if isinstance(actual[key], list) else []
            if (not isinstance(actual[key], list) or any(v not in actual[key] for v in fields[key])
                    or not question_fields or any(v not in question_fields[key] for v in missing)):
                return "not_supported", key + "_mismatch"
    if actual["ambiguous"] and (not actual["ambiguous_metrics"]
            or set(actual["ambiguous_metrics"]).intersection(fields["metrics"])):
        return "uncertain", "ambiguous_source_statistics"
    if actual["residual"]:
        return "uncertain", "source_context_outside_closed_grammar"
    if not set(fields["metrics"]) <= set(actual["metrics"]):
        return "uncertain", "source_metric_labels_missing"
    if any(actual["metrics"].get(k) != v for k, v in fields["metrics"].items()):
        return "not_supported", "labelled_statistic_mismatch"
    return "supported", "closed_numeric_frame"


def validate_claims(answer, sources, *, question=None):
    """Each sentence is checked only against its own citations; fail closed."""
    by_number = {s["number"]: s for s in sources}
    question_fields = parse_claim(question) if question else None
    records = []
    for index, sentence in enumerate(_sentences(answer or ""), 1):
        citations = sorted(set(extract_citations(sentence)))
        claim = CITATION_PATTERN.sub("", sentence).strip().rstrip(".").strip()
        fields = parse_claim(claim)
        matches, reasons, conflicts = [], [], []
        fatal_source = False
        for number in citations:
            source = by_number.get(number)
            if not source or not isinstance(source.get("source_text"), str):
                reasons.append("source_missing")
                fatal_source = True
                continue
            if (any(source.get(k) in (None, "") for k in
                    ("document_id", "chunk_id", "filename", "processing_version"))
                    or type(source.get("page_number")) is not int or source["page_number"] < 1):
                reasons.append("source_metadata_missing")
                fatal_source = True
                continue
            if source.get("review_status") == "pending":
                reasons.append("pending_structured_source_not_allowed")
                fatal_source = True
                continue
            if hashlib.sha256(source["source_text"].encode("utf-8")).hexdigest() != source.get("text_sha256"):
                reasons.append("source_hash_mismatch")
                fatal_source = True
                continue
            for evidence, origin in _evidence_units(source):
                status, reason = _match(claim, fields, evidence, origin, source["source_text"], question_fields)
                reasons.append(reason)
                if reason == "labelled_statistic_mismatch":
                    conflicts.append(number)
                if status == "supported":
                    matches.append({"source_number": number, "chunk_id": source.get("chunk_id"),
                        "document_id": source.get("document_id"), "filename": source.get("filename"),
                        "page_number": source.get("page_number"), "processing_version": source.get("processing_version"),
                        "text_sha256": source["text_sha256"], "evidence_quote": evidence, "source_origin": origin,
                        "source_origin_basis": "explicit_local_text_markers_only",
                        "bound_context": {k: parse_claim(_statistic_source(evidence))[k]
                            for k in ("entities", "diseases", "patient_group", "comparison_group", "measurement_time")},
                        "question_context_considered": bool(question),
                        "controlled_fact": _controlled_fact(claim),
                        "method": reason})
        if fatal_source or (matches and conflicts):
            matches = []
            reasons.append("untrusted_or_conflicting_cited_evidence")
        if not matches:
            if not citations:
                reasons.append("citation_missing")
            folded = _fold(claim)
            if "uyarinin ekg ile dogrulanmasi gerekebilir" in folded:
                reasons.append("additional_confirmation_requirement_unverified")
            cited_text = " ".join(by_number[n].get("source_text", "") for n in citations if n in by_number)
            if ("loop recorder" in folded and "intermittent AF" in cited_text
                    and "ischemic stroke" in cited_text and "iskemik inme" not in folded
                    and "ischemic stroke" not in folded):
                reasons.append("restricted_population_not_preserved")
        status = "supported" if matches else "not_supported" if any(
            reason.endswith("_mismatch") for reason in reasons) else "uncertain"
        record = {"claim_index": index, "claim_text_sha256": hashlib.sha256(claim.encode()).hexdigest(),
            "citations": citations, "fields": fields, "status": status, "matches": matches,
            "reasons": [] if matches else sorted(set(reasons or ["citation_missing"]))}
        if matches:
            record["claim_text"] = claim
        records.append(record)
    passed = bool(records) and all(r["status"] == "supported" for r in records)
    return {"status": "passed" if passed else "failed", "scope": SCOPE,
        "claims_verified": passed, "all_claims_supported": passed,
        "scientific_verification": False, "expert_approved": False, "claims": records,
        "unsupported_features": ["free_translation", "complex_tables", "inferred_statistics", "cross_chunk_synthesis",
                                 "document_level_provenance_not_inferred"]}
