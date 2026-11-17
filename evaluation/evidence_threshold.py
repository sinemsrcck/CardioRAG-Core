"""Offline evaluation only. No threshold is selected during report collection.

Reviewed chunk labels: direct_support, partial_support, topic_only, no_support,
uncertain (legacy irrelevant/ambiguous labels are also accepted).
Automated textual verification is excluded until explicit human approval and
calibration_eligible=True; it is not clinical ground truth.
Only verified direct_support / irrelevant pairs enter binary score metrics.
A verified gold_review_status means the question's sufficient support sets have
been reviewed. Each set is a list of exact chunk identities; alternatives are
separate sets. Page candidates never substitute for reviewed chunk identities.
"""
import json
import math


def json_list(value):
    result = json.loads(value) if isinstance(value, str) else (value or [])
    if not isinstance(result, list):
        raise ValueError("Expected a JSON list")
    return result


def identity(item):
    values = (item.get("document_id"), item.get("page_number"), item.get("chunk_id"))
    return tuple(str(v) for v in values) if all(v is not None and str(v) for v in values) else None


def validate_splits(records):
    """Block leakage by question, group, passage group, chunk, or candidate page.

    Page-level grouping is deliberately conservative. Unassigned rows may be
    collected for review, but cannot enter threshold fitting or held-out metrics.
    """
    parents = {}
    assignments = []
    def root(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = root(parents[key])
        return parents[key]
    for row in records:
        split = row.get("split", "unassigned")
        if split not in {"calibration", "test", "unassigned"}:
            raise ValueError("Invalid split")
        keys = [("question", str(row["id"]))] if row.get("id") is not None else []
        if row.get("question"):
            keys.append(("text", row["question"]))
        if row.get("question_group"):
            keys.append(("group", row["question_group"]))
        keys += [("passage_group", group) for group in json_list(row.get("evidence_group_ids_json"))]
        for field in ("candidate_evidence_json", "evidence_matches_json"):
            for evidence in json_list(row.get(field)):
                if evidence.get("document_id") and evidence.get("page_number") is not None:
                    keys.append(("page", str(evidence["document_id"]), str(evidence["page_number"])))
                if identity(evidence):
                    keys.append(("chunk", *identity(evidence)))
        if not keys:
            raise ValueError("Question or evidence group identity required")
        anchor = keys[0]
        for key in keys:
            parents[root(key)] = root(anchor)
        assignments.append((anchor, split))
    seen = {}
    for anchor, split in assignments:
        if split == "unassigned":
            continue
        component = root(anchor)
        if component in seen and seen[component] != split:
            raise ValueError("Calibration/test leakage in connected question/evidence group")
        seen[component] = split


def _rows(records, model, split):
    records = list(records)
    validate_splits(records)
    if model not in {"minilm", "bge-m3"}:
        raise ValueError("Specify minilm or bge-m3 separately")
    if split not in {"calibration", "test"}:
        raise ValueError("Explicit calibration or test split required")
    rows = [r for r in records if r.get("reranker_model") == model and r.get("split") == split]
    settings = {tuple(str(r.get(key)) for key in (
        "embedding_model", "rerank_method", "candidate_k", "top_k",
        "max_chunks_per_document", "min_relevance_score",
    )) for r in rows}
    if len(settings) > 1:
        raise ValueError("Do not pool different retrieval/evaluation configurations")
    return rows


def _judgments(row):
    judgments = {}
    for item in json_list(row.get("evidence_matches_json")):
        key = identity(item)
        # Missing provenance must never turn an automatic/legacy label into gold.
        approved = (item.get("review_status") == "verified"
                    and item.get("human_review_status") == "verified"
                    and item.get("calibration_eligible") is True)
        if key and approved:
            label = item.get("support_level")
            label = {"topic_only": "irrelevant", "no_support": "irrelevant",
                     "uncertain": "ambiguous"}.get(label, label)
            if key in judgments and judgments[key] != label:
                raise ValueError("Conflicting verified chunk labels")
            judgments[key] = label
    return judgments


def _finite(value):
    try:
        return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def _pairs(rows):
    pairs, excluded = [], 0
    for row in rows:
        labels = _judgments(row)
        seen = set()
        for hit in json_list(row.get("reranked_candidates_json")):
            key = identity(hit)
            if key in seen:
                continue
            seen.add(key)
            label = labels.get(key)
            if row.get("status") == "error" or label not in {"direct_support", "irrelevant"} or not _finite(hit.get("rerank_score")):
                excluded += 1
                continue
            pairs.append((label == "direct_support", float(hit["rerank_score"])))
    return pairs, excluded


def _rate(numerator, denominator):
    return {"value": numerator / denominator if denominator else None,
            "numerator": numerator, "denominator": denominator,
            "reason": None if denominator else "insufficient_verified_labels"}


def _confusion(pairs, threshold):
    tp = sum(label and score >= threshold for label, score in pairs)
    fn = sum(label and score < threshold for label, score in pairs)
    fp = sum(not label and score >= threshold for label, score in pairs)
    tn = sum(not label and score < threshold for label, score in pairs)
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "false_accept_rate": _rate(fp, fp + tn),
            "false_reject_rate": _rate(fn, fn + tp)}


def select_threshold(records, reranker_model):
    """Explicit opt-in fitting, calibration split only; never applies an RAG threshold."""
    rows = _rows(records, reranker_model, "calibration")
    pairs, excluded = _pairs(rows)
    if not any(label for label, _ in pairs) or not any(not label for label, _ in pairs):
        return {"threshold": None, "reason": "both_verified_classes_required",
                "reranker_model": reranker_model, "excluded_pairs": excluded}
    scores = sorted({score for _, score in pairs})
    candidates = [math.nextafter(scores[0], -math.inf)]
    candidates += [a / 2 + b / 2 for a, b in zip(scores, scores[1:])]
    candidates += [math.nextafter(scores[-1], math.inf)]
    candidates = [t for t in candidates if math.isfinite(t)]
    def rank(t):
        c = _confusion(pairs, t)
        balanced = 1 - (c["false_accept_rate"]["value"] + c["false_reject_rate"]["value"]) / 2
        return balanced, -c["fp"]
    best = max(candidates, key=rank)
    return {"threshold": best, "reranker_model": reranker_model,
            "split": "calibration", "balanced_accuracy": rank(best)[0],
            "pair_count": len(pairs), "excluded_pairs": excluded}


def evaluate_threshold(records, threshold, reranker_model, split="test"):
    """Score a caller-supplied fixed threshold; does not fit or run RAG."""
    if not _finite(threshold):
        raise ValueError("A finite, externally supplied threshold is required")
    rows = _rows(records, reranker_model, split)
    pairs, excluded = _pairs(rows)
    return {"reranker_model": reranker_model, "split": split,
            "threshold": float(threshold), "unit": "verified_question_chunk_pair",
            "excluded_pairs": excluded, **_confusion(pairs, float(threshold))}


def evaluate_source_metrics(records, reranker_model, split="test", k=5):
    """Report observed retrieval/rerank/context metrics, without fitting a threshold.

    Hit@k requires reviewed gold support sets. Precision@k requires every
    returned top-k chunk to have a binary verified judgment; partial/unknown
    pools are excluded instead of being silently treated as negative.
    Coverage requires a complete sufficient support set in the actual context.
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError("k must be a positive integer")
    rows = _rows(records, reranker_model, split)
    stages = {}
    for field in ("retrieved_candidates_json", "reranked_candidates_json", "selected_chunks_json"):
        hits = hit_total = relevant = returned = eligible_precision = 0
        for row in rows:
            if row.get("status") == "error":
                continue
            labels = _judgments(row)
            # Duplicate hits must not inflate Precision@k or consume gold ranks.
            candidates, seen = [], set()
            for candidate in json_list(row.get(field)):
                key = identity(candidate)
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                candidates.append(candidate)
                if len(candidates) == k:
                    break
            keys = [identity(c) for c in candidates]
            gold = _support_sets(row, labels)
            if gold is not None:
                positives = set().union(*gold) if gold else set()
                hits += bool(positives.intersection(keys))
                hit_total += 1
            if keys and all(labels.get(key) in {"direct_support", "irrelevant"} for key in keys):
                relevant += sum(labels[key] == "direct_support" for key in keys)
                returned += len(keys)
                eligible_precision += 1
        stages[field] = {"hit_at_k": _rate(hits, hit_total),
                         "precision_at_k": _rate(relevant, returned),
                         "precision_aggregation": "micro_over_returned_top_k",
                         "precision_eligible_questions": eligible_precision,
                         "precision_excluded_questions": len(rows) - eligible_precision,
                         "hit_excluded_questions": len(rows) - hit_total}
    covered = total = excluded = 0
    for row in rows:
        if (row.get("answerability_review_status") != "verified"
                or row.get("human_review_status") != "verified"
                or row.get("answerability_label") != "answerable"):
            excluded += 1
            continue
        labels = _judgments(row)
        gold = _support_sets(row, labels)
        if not gold:
            excluded += 1
            continue
        total += 1
        keys = {identity(c) for c in json_list(row.get("selected_chunks_json"))}
        gate_open = row.get("status") in {"retrieval_only", "generated"}
        covered += gate_open and any(group.issubset(keys) for group in gold)
    gate_pairs = []
    for row in rows:
        if (row.get("status") in {"retrieval_only", "generated", "insufficient_evidence"}
                and row.get("answerability_review_status") == "verified"
                and row.get("human_review_status") == "verified"
                and row.get("answerability_label") in {"answerable", "unanswerable"}):
            gate_pairs.append((row["answerability_label"] == "answerable",
                               float(row.get("status") in {"retrieval_only", "generated"})))
    return {"reranker_model": reranker_model, "split": split, "k": k,
            "question_gate": {"unit": "verified_question", **_confusion(gate_pairs, 0.5)},
            "question_gate_excluded": len(rows) - len(gate_pairs),
            "stages": stages, "answerable_coverage": _rate(covered, total),
            "coverage_excluded_questions": excluded,
            "error_count": sum(r.get("status") == "error" for r in rows)}


def _support_sets(row, labels):
    if (row.get("answerability_review_status") != "verified"
            or row.get("human_review_status") != "verified"
            or row.get("answerability_label") != "answerable"
            or row.get("gold_review_status") != "verified"):
        return None
    groups = json_list(row.get("sufficient_support_sets_json"))
    result = []
    for group in groups:
        if not isinstance(group, list) or not group:
            raise ValueError("Support sets must be nonempty lists of exact chunk identities")
        keys = {identity(item) for item in group}
        if None in keys or any(labels.get(key) != "direct_support" for key in keys):
            raise ValueError("Support sets require verified direct-support chunk matches")
        result.append(keys)
    return result or None


def analyze_thresholds(results, reranker_model=None):
    """Legacy entry point, now requiring explicit model and verified pair labels."""
    if reranker_model is None:
        return {"threshold": None, "reason": "explicit_model_and_verified_pairs_required"}
    return select_threshold(results, reranker_model)


def audit_review_status(questions):
    """Partition existing annotations without modifying labels or choosing splits.

    Question approval and exact chunk approval are independent. A verified text
    excerpt is not human gold unless explicit human approval AND pair eligibility
    are present. Partial/uncertain pairs remain outside binary score calibration.
    """
    questions = list(questions)
    validate_splits(questions)
    report = {}
    for split in ("calibration", "test", "unassigned"):
        rows = [r for r in questions if r.get("split", "unassigned") == split]
        counts = {"questions": len(rows), "human_answerable": 0, "human_unanswerable": 0,
                  "human_other": 0, "pending_questions": 0, "pairs": 0,
                  "approved_positive_pairs": 0, "approved_negative_pairs": 0,
                  "approved_other_pairs": 0, "pending_or_ineligible_pairs": 0,
                  "automated_verified_pairs": 0, "question_ids": [],
                  "pending_question_ids": [], "pending_pair_references": []}
        for row in rows:
            counts["question_ids"].append(row["id"])
            approved = (row.get("answerability_review_status") == "verified"
                        and row.get("human_review_status") == "verified")
            label = row.get("answerability_label")
            if approved and label in {"answerable", "unanswerable"}:
                counts["human_" + label] += 1
            elif approved and label in {"partially_answerable", "ambiguous"}:
                counts["human_other"] += 1
            else:
                counts["pending_questions"] += 1
                counts["pending_question_ids"].append(row["id"])
            judgments = _judgments(row)
            for item in json_list(row.get("evidence_matches_json")):
                counts["pairs"] += 1
                approved_pair = (identity(item) is not None
                                 and item.get("review_status") == "verified"
                                 and item.get("human_review_status") == "verified"
                                 and item.get("calibration_eligible") is True)
                if approved_pair:
                    label = judgments[identity(item)]
                    key = ("approved_positive_pairs" if label == "direct_support" else
                           "approved_negative_pairs" if label == "irrelevant" else "approved_other_pairs")
                    counts[key] += 1
                else:
                    counts["pending_or_ineligible_pairs"] += 1
                    counts["pending_pair_references"].append({"question_id": row["id"],
                                                             "identity": identity(item)})
                if item.get("review_status") == "verified" and item.get("human_review_status") != "verified":
                    counts["automated_verified_pairs"] += 1
        report[split] = counts
    return report


def calibration_readiness(questions, records=(), *, k=5, thresholds=None):
    """Offline per-model audit and observed metrics; never fit/apply a threshold.

    `records` are already collected model runs. Optional thresholds are externally
    supplied model-specific values for evaluation only. Two scored classes are
    necessary, not sufficient proof of sample size or production readiness.
    """
    questions, records = list(questions), list(records)
    validate_splits(questions + records)
    thresholds = {} if thresholds is None else thresholds
    if not isinstance(thresholds, dict) or set(thresholds) - {"minilm", "bge-m3"}:
        raise ValueError("Supply thresholds separately by supported reranker model")
    report = {"production_threshold_applied": False, "models": {}}
    for model in ("minilm", "bge-m3"):
        applicable = [q for q in questions if not json_list(q.get("reranker_models_json"))
                      or model in json_list(q.get("reranker_models_json"))]
        model_report = {"annotation_audit": audit_review_status(applicable), "splits": {}}
        for split in ("calibration", "test"):
            rows = _rows(records, model, split)
            pairs, excluded = _pairs(rows)
            positives = sum(label for label, _ in pairs)
            negatives = len(pairs) - positives
            if model in thresholds:
                threshold_metrics = evaluate_threshold(records, thresholds[model], model, split)
            else:
                reason = "insufficient_verified_scored_labels" if not positives or not negatives else "model_threshold_not_supplied"
                threshold_metrics = {"threshold": None, "reason": reason,
                                     "false_accept_rate": {"value": None, "numerator": None,
                                                           "denominator": negatives, "reason": reason},
                                     "false_reject_rate": {"value": None, "numerator": None,
                                                           "denominator": positives, "reason": reason}}
            model_report["splits"][split] = {
                "scored_questions": len(rows), "positive_scored_pairs": positives,
                "negative_scored_pairs": negatives, "excluded_scored_pairs": excluded,
                "both_scored_classes_present": bool(positives and negatives),
                "threshold_evaluation": threshold_metrics,
                "source_metrics": evaluate_source_metrics(records, model, split, k=k),
            }
        report["models"][model] = model_report
    return report
