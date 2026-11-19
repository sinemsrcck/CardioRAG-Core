"""Source-grounded validation benchmark. No LLM, SQL writes or model downloads.

Default: frozen sources + hand-curated provisional answers. Optional local
retrieval uses existing SQL embeddings and cached models with offline mode.
Human-confirmed metrics and provisional diagnostics are always separate.
"""
import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/validation_cases.json"
LABELS = {"supported", "unsupported", "partially_supported", "uncertain"}


def fingerprint(item):
    payload = {k: v for k, v in item.items() if k != "review"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def approved(item):
    review = item.get("review") or {}
    try:
        reviewed_at = datetime.fromisoformat(review.get("reviewed_at") or "")
    except (TypeError, ValueError):
        return False
    return (review.get("status") == "human_verified" and review.get("source") == "human"
            and bool(str(review.get("reviewer_id") or "").strip()) and reviewed_at.utcoffset() is not None
            and review.get("reviewed_case_sha256") == fingerprint(item))


def load_dataset(path=DATASET):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = data["sources"]
    for key, source in sources.items():
        if hashlib.sha256(source["source_text"].encode()).hexdigest() != source["text_sha256"]:
            raise ValueError("Frozen source hash mismatch: " + key)
        if source["evidence_quote"] not in source["source_text"]:
            raise ValueError("Source quote mismatch: " + key)
    seen = set()
    for item in data["cases"]:
        if item["case_id"] in seen or item["expected_label"] not in LABELS:
            raise ValueError("Duplicate case or invalid label")
        seen.add(item["case_id"])
        if item["expected_accept"] != (item["expected_label"] == "supported"):
            raise ValueError("Only fully supported answers may be accepted")
        if item["language"] not in ("tr", "en") or not item["rationale"]:
            raise ValueError("Language and independent label rationale required")
        for ref in item["evidence_refs"]:
            if any(ref[k] != sources[ref["source_id"]][k] for k in
                   ("text_sha256", "document_id", "chunk_id", "page_number", "processing_version")):
                raise ValueError("Evidence binding mismatch")
        numbers = [ref["citation_number"] for ref in item["evidence_refs"]]
        if len(numbers) != len(set(numbers)) or any(type(n) is not int or n < 1 for n in numbers):
            raise ValueError("Distinct positive citation numbers required")
        if item["review"]["status"] not in ("provisional", "human_verified"):
            raise ValueError("Explicit label review status required")
        if item["review"]["status"] == "human_verified" and not approved(item):
            raise ValueError("Human approval is missing or stale")
    corpus_ids = {s["chunk_id"] for s in sources.values()}
    qids = set()
    group_splits = {}
    for item in data["cases"] + data["retrieval_questions"]:
        if item.get("split") in ("calibration", "test"):
            keys = [item["group_id"]] + [ref["chunk_id"] for ref in item.get("evidence_refs", [])]
            keys += list(item.get("judgments", {}))
            for key in keys:
                if key in group_splits and group_splits[key] != item["split"]:
                    raise ValueError("Related group/passage crosses calibration/test split")
                group_splits[key] = item["split"]
    for question in data["retrieval_questions"]:
        if question["question_id"] in qids or not set(question["judgments"]) <= corpus_ids:
            raise ValueError("Duplicate question or unknown gold chunk")
        qids.add(question["question_id"])
        if question["review"]["status"] == "human_verified" and not approved(question):
            raise ValueError("Missing/stale human gold approval")
    return data


def _ratio(a, b):
    return a / b if b else None


def confusion(observations):
    """Positive = fully supported answer accepted; uncertain is reject-policy only."""
    rows = [r for r in observations if type(r.get("predicted_accept")) is bool]
    if not rows:
        return {"computed": False, "n": 0, "reason": "no_eligible_observations",
                **{k: None for k in ("tp", "fp", "tn", "fn", "precision", "recall", "f1",
                                     "false_accept_rate", "false_reject_rate")}}
    tp = sum(r["expected_accept"] and r["predicted_accept"] for r in rows)
    fp = sum(not r["expected_accept"] and r["predicted_accept"] for r in rows)
    tn = sum(not r["expected_accept"] and not r["predicted_accept"] for r in rows)
    fn = sum(r["expected_accept"] and not r["predicted_accept"] for r in rows)
    return {"computed": True, "n": len(rows), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": _ratio(tp, tp+fp), "recall": _ratio(tp, tp+fn),
            "f1": _ratio(2*tp, 2*tp+fp+fn), "false_accept_rate": _ratio(fp, fp+tn),
            "false_reject_rate": _ratio(fn, fn+tp)}


def evaluate_cases(data):
    from src.citation_validator import validate_citations
    from src.claim_validator import validate_claims
    from src.evidence_guard import check_evidence
    observations = []
    for case in data["cases"]:
        sources = [{**data["sources"][ref["source_id"]], "number": ref["citation_number"]}
                   for ref in case["evidence_refs"]]
        row = {"case_id": case["case_id"], "language": case["language"], "group_id": case["group_id"],
               "component": case["component"], "expected_label": case["expected_label"],
               "expected_accept": case["expected_accept"], "human_verified": approved(case),
               "rationale": case["rationale"], "predicted_accept": None, "claim_evaluated": False}
        if case["component"] == "evidence_guard":
            hits = [{**s, "text": case.get("context_excerpt", s["source_text"])} for s in sources]
            if case.get("context_excerpt") and any(case["context_excerpt"] not in s["source_text"] for s in sources):
                raise ValueError("Fault-injection excerpt is not from the real source")
            guard = check_evidence(hits, question=case["question"], enforce_keyword_overlap=False)
            row.update(predicted_accept=guard["sufficient"], status="evidence_sufficient" if guard["sufficient"]
                       else "insufficient_evidence", reason=guard["reason"])
        else:
            citations = validate_citations(case["answer"], sources)
            row["citation_check"] = citations
            if not citations["valid"]:
                row.update(predicted_accept=False, status="citation_validation_failed")
            else:
                claims = validate_claims(case["answer"], sources, question=case["question"])
                row.update(predicted_accept=claims["claims_verified"], claim_evaluated=True,
                           status="accepted" if claims["claims_verified"] else "claim_validation_failed",
                           claim_verification=claims)
        row["policy_agreement"] = row["predicted_accept"] == row["expected_accept"]
        observations.append(row)
    def metrics(rows):
        return {"answer_acceptance_policy": confusion(rows),
                "claim_validator_only": confusion([r for r in rows if r["claim_evaluated"]]),
                "decisive_claim_labels_only": confusion([r for r in rows if r["claim_evaluated"]
                                                         and r["expected_label"] != "uncertain"]),
                "evidence_guard_only": confusion([r for r in rows if r["component"] == "evidence_guard"]),
                "by_language": {lang: confusion([r for r in rows if r["language"] == lang
                                                and r["claim_evaluated"]]) for lang in ("tr", "en")}}
    return {"positive_class": "accept_fully_supported_answer",
            "uncertain_label_policy": "reject; not evidence that the claim is factually false",
            "measurement_scope": "prewritten_answers_with_frozen_sources; no LLM generation or retrieval in this component",
            "human_verified_metrics": metrics([r for r in observations if r["human_verified"]]),
            "provisional_diagnostics_NOT_ground_truth": metrics([r for r in observations if not r["human_verified"]]),
            "observations": observations}


def ranking_metrics(ranked, judgments, k=5, gold_complete=False):
    """Unjudged chunks are never silently graded zero; denominator is explicit."""
    if type(k) is not int or k <= 0:
        raise ValueError("Positive integer k required")
    if any(type(g) is not int or g not in (0, 1, 2) for g in judgments.values()):
        raise ValueError("Relevance grades must be 0, 1 or 2")
    unique = list(dict.fromkeys(ranked))
    top = unique[:k]
    relevant = {cid for cid, grade in judgments.items() if grade > 0}
    first = next((i for i, cid in enumerate(unique, 1) if cid in relevant), None)
    complete = gold_complete and all(cid in judgments for cid in top)
    gains = [2 ** judgments[cid] - 1 for cid in top] if complete else []
    dcg = sum(g / math.log2(i+2) for i, g in enumerate(gains))
    ideal = sorted((2**g-1 for g in judgments.values()), reverse=True)[:k]
    idcg = sum(g / math.log2(i+2) for i, g in enumerate(ideal))
    recall = _ratio(len(relevant.intersection(top)), len(relevant))
    return {"k": k, "annotated_gold_recall_at_k": recall,
            "annotated_gold_rr": 1/first if first else 0.0 if relevant else None,
            "recall_at_k": recall if gold_complete else None,
            "reciprocal_rank": (1/first if first else 0.0) if gold_complete and relevant else None,
            "ndcg_at_k": _ratio(dcg, idcg) if complete else None,
            "unjudged_top_k": sum(cid not in judgments for cid in top),
            "reason": None if complete and relevant else "incomplete_gold_or_unjudged_pool_or_no_positive_gold"}


def run_local_retrieval(data, models=("minilm", "bge-m3")):
    """Same MiniLM embeddings across rerankers. Existing cache only, SQL SELECT only."""
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    from src.database import get_connection, load_chunks_for_rag
    from src.rag import answer_question
    cache = Path(os.getenv("HF_HUB_CACHE", str(Path.home() / ".cache/huggingface/hub")))
    paths = {"minilm": "models--cross-encoder--ms-marco-MiniLM-L-6-v2",
             "bge-m3": "models--BAAI--bge-reranker-v2-m3"}
    results = {}
    corpus = None
    for model in models:
        result = {"embedding_model": "minilm", "reranker_model": model, "use_llm": False, "runs": []}
        results[model] = result
        if model not in paths or not (cache / paths[model]).is_dir():
            result.update(status="not_available", reason="reranker_not_in_local_cache; no_download_attempted")
            continue
        try:
            if corpus is None:
                conn = get_connection()
                try:
                    conn.start_transaction(readonly=True)
                    corpus = load_chunks_for_rag("minilm", connection=conn)
                    conn.rollback()
                finally:
                    conn.close()
            by_id = {c["chunk_id"]: c for c in corpus}
            for source in data["sources"].values():
                actual = by_id.get(source["chunk_id"])
                if not actual or any(actual[k] != source[k] for k in
                        ("document_id", "page_number", "processing_version", "text_sha256")):
                    raise ValueError("Frozen evaluation source is not in active SQL corpus")
            result["active_chunk_count"] = len(corpus)
            for question in data["retrieval_questions"]:
                start = time.perf_counter()
                response = answer_question(question["question"], corpus, embedding_model="minilm",
                    reranker_model=model, candidate_k=20, top_k=5, use_llm=False)
                stages = {"retrieved": response["debug"]["retrieved_hits"],
                          "reranked": response["debug"]["reranked_hits"], "context": response["sources"]}
                result["runs"].append({"question_id": question["question_id"], "language": question["language"],
                    "query_normalization": response["debug"].get("query_normalization"),
                    "human_verified": approved(question), "status": response["status"],
                    "elapsed_seconds": round(time.perf_counter()-start, 3),
                    "stages": {stage: {"chunk_ids": [h["chunk_id"] for h in hits],
                        "metrics": ranking_metrics([h["chunk_id"] for h in hits], question["judgments"],
                                                   gold_complete=question["gold_complete"])}
                               for stage, hits in stages.items()}})
            result["status"] = "measured"
        except Exception as exc:
            result.update(status="error_or_unavailable", reason=type(exc).__name__)
    return results


def build_report(data, model_results=None):
    models = model_results or {m: {"status": "not_run", "reason": "local_retrieval_not_requested", "runs": []}
                              for m in ("minilm", "bge-m3")}
    for result in models.values():
        result["aggregates"] = {}
        for lang in ("tr", "en"):
            result["aggregates"][lang] = {}
            for stage in ("retrieved", "reranked", "context"):
                runs = [r for r in result.get("runs", []) if r["language"] == lang]
                def mean(key, human=False):
                    values = [r["stages"][stage]["metrics"][key] for r in runs
                              if (not human or r["human_verified"])
                              and r["stages"][stage]["metrics"][key] is not None]
                    return {"value": sum(values)/len(values) if values else None, "n": len(values)}
                result["aggregates"][lang][stage] = {
                    "human_verified_recall_at_k": mean("recall_at_k", True),
                    "human_verified_mrr": mean("reciprocal_rank", True),
                    "human_verified_ndcg_at_k": mean("ndcg_at_k", True),
                    "provisional_annotated_gold_recall_at_k": mean("annotated_gold_recall_at_k"),
                    "provisional_annotated_gold_mrr": mean("annotated_gold_rr")}
    return {"created_at": datetime.now(timezone.utc).isoformat(), "dataset_version": data["schema_version"],
            "dataset_sha256": hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "dataset_size": len(data["cases"]), "label_counts": dict(Counter(c["expected_label"] for c in data["cases"])),
            "review_counts": dict(Counter(c["review"]["status"] for c in data["cases"])),
            "language_counts": dict(Counter(c["language"] for c in data["cases"])),
            "case_catalog": [{"case_id": case["case_id"], "language": case["language"],
                "question": case["question"], "answer": case["answer"], "expected_label": case["expected_label"],
                "expected_accept": case["expected_accept"], "review_status": case["review"]["status"],
                "rationale": case["rationale"], "sources": [
                    {"citation_number": ref["citation_number"], **{k: data["sources"][ref["source_id"]][k]
                     for k in ("filename", "document_id", "page_number", "chunk_id", "text_sha256",
                               "processing_version", "evidence_quote")}}
                    for ref in case["evidence_refs"]]} for case in data["cases"]],
            "validation": evaluate_cases(data), "retrieval_models": models,
            "limitations": ["all provisional labels require independent expert review",
                "no end-to-end generated-answer performance measured", "no threshold fitted or applied",
                "gold retrieval pools are incomplete; unjudged chunks are not negatives",
                "shared passages/language variants are dependent; no held-out generalization claim"]}


def markdown(report):
    lines = ["# RAG validation evaluation", "", "Provisional diagnostics are NOT human-approved ground truth.",
        "No LLM/API calls. No SQL writes. Answers are curated evaluation examples, not real model outputs.",
        "", f"Cases: {report['dataset_size']}; labels: {report['label_counts']}; review: {report['review_counts']}.",
        "", "Positive class: accept a fully supported answer. Partial/uncertain cases require rejection by policy.",
        "Uncertain does not mean factually false. Citation/guard failures are excluded from claim-only metrics.",
        "", "| Metric | Human-confirmed claim labels | Provisional claim diagnostics |", "|---|---:|---:|"]
    validation = report["validation"]
    primary = validation["human_verified_metrics"]["claim_validator_only"]
    provisional = validation["provisional_diagnostics_NOT_ground_truth"]["claim_validator_only"]
    for name in ("n", "tp", "fp", "tn", "fn", "precision", "recall", "f1", "false_accept_rate", "false_reject_rate"):
        def fmt(value):
            return "not computable" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
        lines.append(f"| {name} | {fmt(primary[name])} | {fmt(provisional[name])} |")
    lines += ["", "## Model execution", "", "| Reranker | Embedding | Status | Questions measured |", "|---|---|---|---:|"]
    for model, result in report["retrieval_models"].items():
        lines.append(f"| {model} | {result.get('embedding_model', 'not run')} | {result['status']} | {len(result.get('runs', []))} |")
    lines += ["", "## Provisional full acceptance-policy diagnostics", "",
              "| Component | n | TP | FP | TN | FN |", "|---|---:|---:|---:|---:|---:|"]
    for name in ("answer_acceptance_policy", "evidence_guard_only", "decisive_claim_labels_only"):
        value = validation["provisional_diagnostics_NOT_ground_truth"][name]
        lines.append("| " + name + " | " + " | ".join(str(value[k]) for k in ("n", "tp", "fp", "tn", "fn")) + " |")
    lines += ["", "## Observed retrieval diagnostics against known annotated chunks", "",
              "| Model | Language | Stage | Annotated Recall@5 | Annotated MRR | Questions |",
              "|---|---|---|---:|---:|---:|"]
    for model, result in report["retrieval_models"].items():
        for lang, stages in result["aggregates"].items():
            for stage, values in stages.items():
                recall = values["provisional_annotated_gold_recall_at_k"]
                mrr = values["provisional_annotated_gold_mrr"]
                if recall["n"]:
                    lines.append(f"| {model} | {lang} | {stage} | {recall['value']:.4f} | {mrr['value']:.4f} | {recall['n']} |")
    lines += ["", "Recall@k/MRR require complete gold annotations for corpus-wide interpretation. nDCG@k also requires judged top-k results.",
        "Annotated-gold diagnostics, when present in JSON, concern only explicitly listed positive chunks.",
        "", "## Case decisions", "", "| Case | Language | Component | Provisional label | Observed status | Policy agreement |",
        "|---|---|---|---|---|---|"]
    for row in validation["observations"]:
        lines.append(f"| {row['case_id']} | {row['language']} | {row['component']} | {row['expected_label']} | {row['status']} | {row['policy_agreement']} |")
    lines += ["", "## Source-grounded review appendix", "",
              "Every answer below is a constructed evaluation example, including deliberate errors. All labels remain provisional."]
    for case in report["case_catalog"]:
        lines += ["", f"### {case['case_id']} — {case['review_status']}", "",
                  f"Question ({case['language']}): {case['question']}", "",
                  f"Evaluation answer: {case['answer']}", "",
                  f"Expected label: {case['expected_label']}; expected decision: {'accept' if case['expected_accept'] else 'reject'}.",
                  "", "Rationale: " + case["rationale"]]
        for source in case["sources"]:
            lines += ["", f"Source [{source['citation_number']}]: {source['filename']}, physical page {source['page_number']}.",
                      "", "Chunk: " + source["chunk_id"], "", "Source quote: " + source["evidence_quote"]]
        if not case["sources"]:
            lines += ["", "Provided sources: none (controlled missing-evidence scenario)."]
    lines += ["", "## Limitations", ""] + ["- " + line for line in report["limitations"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evaluation/reports")
    parser.add_argument("--run-local-retrieval", action="store_true")
    args = parser.parse_args()
    data = load_dataset(args.dataset)
    report = build_report(data, run_local_retrieval(data) if args.run_local_retrieval else None)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "rag_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (args.output_dir / "rag_validation.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"cases": report["dataset_size"], "labels": report["label_counts"],
                      "models": {m: r["status"] for m, r in report["retrieval_models"].items()}}, ensure_ascii=True))


if __name__ == "__main__":
    main()
