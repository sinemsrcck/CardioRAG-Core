"""Offline human review packages. Never infer human approval from provisional labels.

Run: python -m evaluation.human_ground_truth
Produces versioned development review files; no SQL/model/API access.
"""
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from evaluation.evaluate_validation import load_dataset, evaluate_cases, confusion, ranking_metrics

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"supported", "unsupported", "partial", "uncertain"}
EVALUATION_NAME = "researcher-reviewed, source-grounded pilot evaluation"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def binding(row):
    return digest({k: v for k, v in row.items() if k != "human_review"})


def empty_review():
    return dict(status="pending_review", source=None, reviewer_id=None, reviewed_at=None,
                reviewed_item_sha256=None, decision=None, rationale=None,
                relevance_judgments={}, gold_complete=False)


def build_review_package(data):
    sources = data["sources"]
    def evidence(refs):
        return [{**copy.deepcopy(sources[r["source_id"]]),
                 "citation_number": r["citation_number"]} for r in refs]
    claims = []
    for c in data["cases"]:
        claims.append(dict(item_id=c["case_id"], group_id=c["group_id"], language=c["language"],
            question=c["question"], claim=c["answer"], component=c["component"],
            evidence=evidence(c["evidence_refs"]),
            provisional=dict(label=c["expected_label"], rationale=c["rationale"]),
            original_item_sha256=digest(c), human_review=empty_review()))
    retrieval = []
    for q in data["retrieval_questions"]:
        retrieval.append(dict(item_id=q["question_id"], group_id=q["group_id"], language=q["language"],
            question=q["question"], provisional=dict(judgments=copy.deepcopy(q["judgments"]),
                gold_complete=q["gold_complete"]), original_item_sha256=digest(q),
            evidence=[copy.deepcopy(s) for s in sources.values() if s["chunk_id"] in q["judgments"]],
            human_review=empty_review()))
    return dict(schema_version="human-rag-review-v1", dataset_sha256=digest(data),
        purpose="development_review_only_NOT_independent_test", claims=claims, retrieval=retrieval,
        instructions={"claim_decisions": sorted(LABELS), "relevance_grades": {"0": "not relevant",
            "1": "partial support", "2": "direct support"},
            "approval": "Human must set approved, source=human, reviewer_id, timezone-aware reviewed_at, rationale and reviewed_item_sha256=binding(row) after completing review.",
            "gold_complete": "true only after judging the full eligible corpus; unjudged chunks are not negatives",
            "source_scope": "Frozen chunks, not a fresh active-SQL integrity check. Additional judgments require full source metadata and exact quote in evidence."})


def validate_approvals(package, data):
    expected = build_review_package(data)
    if package["dataset_sha256"] != expected["dataset_sha256"]:
        raise ValueError("Stale dataset binding")
    approved = {"claims": [], "retrieval": []}
    for kind in approved:
        originals = {r["item_id"]: r for r in expected[kind]}
        seen = set()
        for row in package[kind]:
            key = row["item_id"]
            if key in seen or key not in originals:
                raise ValueError("Duplicate or unknown review item")
            seen.add(key)
            original = originals[key]
            # Retrieval evidence can be extended for human relevance annotation.
            fields = set(original) - {"human_review"} - ({"evidence"} if kind == "retrieval" else set())
            if any(row.get(k) != original[k] for k in fields):
                raise ValueError("Changed question/claim/provisional/source binding")
            review = row["human_review"]
            if kind == "claims":
                case = next(c for c in data["cases"] if c["case_id"] == key)
                required_context = case.get("context_excerpt")
                if (required_context is not None and
                        ("context_excerpt" in row or package.get("preparation")
                         or review.get("review_role") == "researcher")
                        and row.get("context_excerpt") != required_context):
                    raise ValueError("Evaluation context changed or missing")
            if review["status"] == "pending_review":
                continue
            if review["status"] != "approved":
                raise ValueError("Unknown review status")
            try:
                stamp = datetime.fromisoformat(review.get("reviewed_at") or "")
            except (TypeError, ValueError) as exc:
                raise ValueError("Review timestamp required") from exc
            if (review.get("source") != "human" or not str(review.get("reviewer_id") or "").strip()
                    or not str(review.get("rationale") or "").strip() or stamp.utcoffset() is None
                    or review.get("reviewed_item_sha256") != binding(row)):
                raise ValueError("Missing or stale human approval provenance")
            if kind == "claims":
                if review.get("decision") not in LABELS:
                    raise ValueError("Invalid human claim decision")
            else:
                judgments = review.get("relevance_judgments")
                if not isinstance(judgments, dict) or not judgments or type(review.get("gold_complete")) is not bool:
                    raise ValueError("Explicit human relevance judgments required")
                by_id = {}
                for s in row["evidence"]:
                    if s["chunk_id"] in by_id or not all(s.get(k) for k in
                            ("filename", "document_id", "chunk_id", "processing_version", "page_number")):
                        raise ValueError("Invalid retrieval source metadata")
                    if digest_text(s["source_text"]) != s["text_sha256"] or not s["evidence_quote"] or s["evidence_quote"] not in s["source_text"]:
                        raise ValueError("Retrieval source hash/quote mismatch")
                    by_id[s["chunk_id"]] = s
                if not set(judgments) <= set(by_id) or any(type(g) is not int or g not in (0, 1, 2) for g in judgments.values()):
                    raise ValueError("Unknown source or invalid relevance grade")
            approved[kind].append(copy.deepcopy(row))
        if seen != set(originals):
            raise ValueError("Review package omitted examples")
    return approved


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate_human(package, data, model_results=None):
    approved = validate_approvals(package, data)
    observations = {r["case_id"]: r for r in evaluate_cases(data)["observations"]}
    rows = [{**observations[r["item_id"]], "expected_accept": r["human_review"]["decision"] == "supported"}
            for r in approved["claims"]]
    report = dict(evaluation_name=EVALUATION_NAME,
        scope=EVALUATION_NAME + "; development set; NOT independent test",
        clinical_expert_validation=False, clinical_certification=False,
        positive_class="accept fully source-supported answer",
        uncertain_policy="reject policy, not proof of factual falsity",
        denominators={"precision": "TP+FP", "recall_and_false_reject": "TP+FN",
                      "false_accept": "FP+TN", "f1": "2TP+FP+FN"},
        approved_claim_count=len(rows), approved_retrieval_count=len(approved["retrieval"]),
        answer_policy=confusion(rows),
        claim_validator=confusion([r for r in rows if r["claim_evaluated"]]), retrieval={})
    for model in ("minilm", "bge-m3"):
        measured = (model_results or {}).get(model, {})
        runs = measured.get("runs", []) if measured.get("status") == "measured" else []
        by_id = {r["question_id"]: r for r in runs}
        result = {"status": measured.get("status", "not_run"), "by_language": {}}
        for language in ("tr", "en"):
            metrics = []
            for q in approved["retrieval"]:
                if q["language"] != language or q["item_id"] not in by_id:
                    continue
                run = by_id[q["item_id"]]
                review = q["human_review"]
                metrics.append(ranking_metrics(run["stages"]["reranked"]["chunk_ids"],
                    review["relevance_judgments"], gold_complete=review["gold_complete"]))
            def average(key):
                values = [m[key] for m in metrics if m[key] is not None]
                return {"value": sum(values)/len(values) if values else None, "n": len(values)}
            result["by_language"][language] = {"reviewed_measured_queries": len(metrics),
                "recall_at_5": average("recall_at_k"), "mrr": average("reciprocal_rank"),
                "ndcg_at_5": average("ndcg_at_k"),
                "annotated_only_recall_at_5": average("annotated_gold_recall_at_k"),
                "annotated_only_mrr": average("annotated_gold_rr")}
        report["retrieval"][model] = result
    return report


def researcher_review(package, data, item_id, *, action, reviewer_id,
                      decision=None, rationale=None, reviewed_at=None):
    """Explicit in-memory review interface; never saves or auto-accepts proposals.

    accept requires an explicit decision matching the proposal; change supplies
    a different decision. defer leaves pending, without an approval attestation.
    No physician credential is required. Callers must persist separately.
    """
    if action not in ("accept", "change", "defer"):
        raise ValueError("Action must be accept, change or defer")
    if not str(reviewer_id or "").strip():
        raise ValueError("Researcher identity required")
    validate_approvals(package, data)
    result = copy.deepcopy(package)
    matches = [r for r in result["claims"] if r["item_id"] == item_id]
    if len(matches) != 1:
        raise ValueError("Unknown claim ID")
    row = matches[0]
    case = next(c for c in data["cases"] if c["case_id"] == item_id)
    if case.get("context_excerpt") is not None:
        row["context_excerpt"] = case["context_excerpt"]
    if case["component"] == "evidence_guard":
        row["scenario_type"] = "evidence_guard_scenario"
    if action == "defer":
        row["human_review"] = empty_review()
        row["human_review"].update(review_role="researcher", deferred_by=reviewer_id,
                                    deferral_reason=rationale)
    else:
        if decision not in LABELS or not str(rationale or "").strip():
            raise ValueError("Explicit source-grounded decision and rationale required")
        proposal = row.get("proposed_final_decision")
        if action == "accept" and proposal != decision:
            raise ValueError("Accept requires an explicit decision matching the proposal")
        if action == "change" and proposal == decision:
            raise ValueError("Change must differ from the proposal; use accept")
        row["human_review"] = {**empty_review(), "status": "approved", "source": "human",
            "review_role": "researcher", "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at, "decision": decision, "rationale": rationale,
            "reviewed_item_sha256": binding(row)}
    if "expected_bindings" in result:
        result["expected_bindings"]["claims"][item_id] = binding(row)
    validate_approvals(result, data)
    return result


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def export_approved(package, data, path):
    """Explicit export after human review; refuses to overwrite previous versions."""
    records = validate_approvals(package, data)
    archive = {"schema_version": "human-rag-approved-v1", "dataset_sha256": digest(data),
               "scope": "development_only", **records}
    write_new(Path(path), archive)
    return archive


def evaluate_approved(archive, data, model_results=None):
    if archive.get("schema_version") != "human-rag-approved-v1" or archive.get("dataset_sha256") != digest(data):
        raise ValueError("Invalid approved archive binding")
    package = build_review_package(data)
    for kind in ("claims", "retrieval"):
        records = archive[kind]
        ids = [r["item_id"] for r in records]
        if len(ids) != len(set(ids)) or not set(ids) <= {r["item_id"] for r in package[kind]}:
            raise ValueError("Unknown/duplicate approved item")
        by_id = {r["item_id"]: r for r in records}
        if any(r["human_review"]["status"] != "approved" for r in records):
            raise ValueError("Approved archive contains unapproved records")
        package[kind] = [copy.deepcopy(by_id.get(r["item_id"], r)) for r in package[kind]]
    return evaluate_human(package, data, model_results)


def review_markdown(package):
    lines = ["# İnsan incelemesi — geliştirme kümesi", "",
        "44 iddia / 14 retrieval sorusu. Hiçbir provisional etiket insan onayı değildir.",
        "Kararlar JSON dosyasında doldurulur. supported / unsupported / partial / uncertain.",
        "Kaynak metinleri donmuş değerlendirme chunk'larıdır; güncel SQL doğrulaması yapılmadı.", ""]
    for kind in ("claims", "retrieval"):
        for row in package[kind]:
            lines += ["## " + row["item_id"], "", row["question"], ""]
            if kind == "claims":
                lines += ["İddia: " + row["claim"], ""]
            lines += ["Provisional (onay değildir): " + json.dumps(row["provisional"], ensure_ascii=False),
                      "", "İnsan kararı / gerekçe: **bekliyor**", ""]
            if not row["evidence"]:
                lines += ["Kaynak yok: eksik kanıt senaryosu; kaynak uydurulmadı.", ""]
            for s in row["evidence"]:
                lines += [f"- PDF: {s['filename']} — fiziksel sayfa {s['page_number']}",
                    f"- Chunk: `{s['chunk_id']}`", "", "> " + s["evidence_quote"].replace("\n", "\n> "), ""]
    return "\n".join(lines)


def prepare():
    data = load_dataset()
    folder = ROOT / "evaluation/human_review/v1"
    package = build_review_package(data)
    write_new(folder / "review_pending.json", package)
    with (folder / "review_pending.md").open("x", encoding="utf-8") as handle:
        handle.write(review_markdown(package))
    write_new(folder / "approved_labels.json", {"schema_version": "human-rag-approved-v1",
        "dataset_sha256": digest(data), "claims": [], "retrieval": [],
        "note": "Empty until explicit human decisions are validated; no automatic approval."})
    known = {s["filename"] for s in data["source_manifest"]}
    extra = sorted(p.name for p in (ROOT / "data/pdfs").glob("*.pdf") if p.name not in known)
    write_new(folder / "independent_test_manifest.json", {"status": "not_ready",
        "development_documents": sorted(known), "additional_pdf_candidates_unreviewed": extra,
        "claims": [], "retrieval": [], "reason": "No reviewed document-disjoint test examples. Existing 44 claims and 14 queries remain development data."})
    report = evaluate_human(package, data)
    write_new(folder / "human_metrics.json", report)
    return folder


if __name__ == "__main__":
    print(prepare())
