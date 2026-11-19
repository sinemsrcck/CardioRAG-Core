"""Compare frozen Stage 3 labels with local, offline Stage 4 candidates.

The baseline must be reproduced BEFORE changing production code. This runner
never edits labels or activates a query policy; policy adoption is explicit.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from evaluation.evaluate_validation import DATASET, ROOT, build_report, load_dataset, markdown, run_local_retrieval
from src.query_normalization import QUERY_POLICIES

REPORTS = ROOT / "evaluation/reports"
CAUSES = {
    "AF02": (["Turkish language"], "Detection methods were outside the numeric/literal grammar; closed bilingual detection predicates retain each method."),
    "ROC01": (["PDF formatting", "numeric notation"], "Figure text contains A u C and caption boilerplate; values must remain attached to the ELR clause."),
    "ROC02": (["Turkish language", "PDF formatting"], "Same figure issue plus the neutral Turkish reporting suffix bildirilmiştir."),
    "ROC03": (["PDF formatting", "source matching"], "PLR caption B) and A u C prevented reading its own AUC/CI/p tuple; ELR and PLR remain separate."),
    "POP01": (["source matching"], "The substring review in retrospectively reviewed wrongly marked original methods as secondary and ambiguous."),
    "POP02": (["Turkish language", "sentence structure"], "96 patients were in the study group reverses the count grammar; Turkish kişiden oluşmuştur was unknown."),
    "HB01": (["numeric notation", "source matching"], "CI95 was unparsed; malformed p<001 blocked known AUC/CI; AF/ischemic-event scope was in the question."),
    "HB02": (["Turkish language", "numeric notation"], "Same scoped HbA1c issue plus reporting morphology; malformed p is still NOT repaired."),
    "TRP01": (["numeric notation", "source matching"], "The literal statement contains untyped ±0.066. Its cited chunk does not name SE; keep rejection rather than infer its type."),
    "TRP02": (["PDF formatting", "Turkish language"], "Time inserted between HS and troponin I hid the entity; untyped ± blocked separately explicit AUC/p; table inference remains off."),
    "TRP07": (["Turkish language", "sentence structure"], "A translated cohort-total statement did not match final evaluation was made on 114 patients; bound cohort predicate now checks study context."),
    "CAD02": (["Turkish language", "source matching"], "Literal matching could not map a Turkish IIa guideline statement to a chunk starting mid-organization name; all population/range/class arguments must agree."),
    "OHCA02": (["Turkish language", "source matching"], "Bounded literature-search absence was a free paraphrase; review context and country must agree, never infer absence of OHCA events."),
}
QUERY_CAUSES = {
    "af_detection_tr": "Atriyal/atrial and tespit/detection mismatch; English-trained reranker demoted the correct English detection passage.",
    "tk37_roc_tr": "Karotis/carotid and Turkish ROC phrasing compete with other marker/table chunks; the correct Figure 1 was outside top five.",
    "tk37_population_tr": "The question lacks ELR/PLR and Turkish group-count terms do not match English methods; both known gold passages were absent from top twenty.",
    "tk36_hba1c_tr": "Hybrid retrieval found the correct chunk first but Turkish reranking demoted it; this is mainly a reranker-language issue.",
    "troponin_roc_tr": "Delta/change terminology and a multi-row PDF table reduce rank; the prose result was found but the known table was outside top five.",
    "cad_guideline_tr": "The source was already in top five. Context exclusion is a separate demo Evidence Guard coverage issue, not primarily a retrieval failure.",
    "ohca_review_tr": "Bahreyn/Bahrain and derleme/review vocabulary differ; the correct abstract was demoted by the Turkish reranking query.",
}


def scores(report, lang):
    values = report["retrieval_models"]["minilm"]["aggregates"][lang]["reranked"]
    return (values["provisional_annotated_gold_recall_at_k"]["value"],
            values["provisional_annotated_gold_mrr"]["value"])


def eligible(baseline, candidate):
    before = baseline["validation"]["provisional_diagnostics_NOT_ground_truth"]["claim_validator_only"]
    after = candidate["validation"]["provisional_diagnostics_NOT_ground_truth"]["claim_validator_only"]
    if after["fp"] > before["fp"] or baseline["dataset_sha256"] != candidate["dataset_sha256"]:
        return False
    values = [(scores(baseline, lang), scores(candidate, lang)) for lang in ("tr", "en")]
    return (all(a is not None and b is not None and b+1e-12 >= a for old, new in values
                for a, b in zip(old, new)) and any(b > a+1e-12 for a, b in zip(*values[0])))


def compare(baseline, candidates, data):
    old_obs = {r["case_id"]: r for r in baseline["validation"]["observations"]}
    claim_rows = {name: report["validation"]["provisional_diagnostics_NOT_ground_truth"]["claim_validator_only"]
                  for name, report in {"baseline": baseline, **candidates}.items()}
    accepted = [name for name, report in candidates.items() if eligible(baseline, report)]
    selected = max(accepted, key=lambda name: (*scores(candidates[name], "tr"), name == "rewrite")) if accepted else "original"
    representative = candidates.get(selected) or next(iter(candidates.values()))
    new_obs = {r["case_id"]: r for r in representative["validation"]["observations"]}
    failures = []
    for cid, old in old_obs.items():
        if old["expected_accept"] and not old["predicted_accept"]:
            categories, reason = CAUSES[cid]
            failures.append({"case_id": cid, "categories": categories, "root_cause": reason,
                "before": old["status"], "after": new_obs[cid]["status"],
                "corrected": new_obs[cid]["predicted_accept"]})
    queries = []
    qmap = {q["question_id"]: q for q in data["retrieval_questions"]}
    for run in baseline["retrieval_models"]["minilm"]["runs"]:
        if run["language"] != "tr":
            continue
        qid = run["question_id"]
        after_run = next(r for r in representative["retrieval_models"]["minilm"]["runs"]
                         if r["question_id"] == qid)
        queries.append({"question_id": qid, "question": qmap[qid]["question"], "root_cause": QUERY_CAUSES[qid],
            "before_gold_ranks": {stage: [i for i, cid in enumerate(pool["chunk_ids"], 1)
                 if cid in qmap[qid]["judgments"]] for stage, pool in run["stages"].items()},
            "after_gold_ranks": {stage: [i for i, cid in enumerate(pool["chunk_ids"], 1)
                 if cid in qmap[qid]["judgments"]] for stage, pool in after_run["stages"].items()},
            "query_normalization": after_run.get("query_normalization")})
    return {"dataset_sha256": baseline["dataset_sha256"], "labels_changed": False,
        "label_status": "all_provisional_not_ground_truth", "selected_query_policy": selected,
        "candidate_eligibility": {name: eligible(baseline, report) for name, report in candidates.items()},
        "claim_metrics": claim_rows, "false_rejection_analysis": failures, "turkish_query_analysis": queries,
        "reranked_metrics": {name: {lang: {"annotated_recall_at_5": scores(report, lang)[0],
                                          "annotated_mrr": scores(report, lang)[1]} for lang in ("tr", "en")}
                             for name, report in {"baseline": baseline, **candidates}.items()},
        "scope": "development comparison on the same provisional pilot; not independent held-out validation"}


def comparison_markdown(report):
    lines = ["# Stage 4 comparison", "", report["scope"], "",
        "Labels are unchanged and provisional. No production threshold or expert approval is inferred.", "",
        "| Query policy | TP | FP | TN | FN | Precision | Recall | F1 | False accept | False reject |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in report["claim_metrics"].items():
        lines.append("| " + name + " | " + " | ".join(str(row[k]) for k in
            ("tp", "fp", "tn", "fn", "precision", "recall", "f1", "false_accept_rate", "false_reject_rate")) + " |")
    lines += ["", "| Policy | Language | Annotated Recall@5 | Annotated MRR |", "|---|---|---:|---:|"]
    for name, languages in report["reranked_metrics"].items():
        for lang, value in languages.items():
            lines.append(f"| {name} | {lang} | {value['annotated_recall_at_5']} | {value['annotated_mrr']} |")
    lines += ["", "Selected query policy: " + report["selected_query_policy"], "",
        "## Original 13 false rejections", "", "| Case | Categories | Root cause | Corrected |", "|---|---|---|---|"]
    for row in report["false_rejection_analysis"]:
        lines.append(f"| {row['case_id']} | {', '.join(row['categories'])} | {row['root_cause']} | {row['corrected']} |")
    lines += ["", "## Seven Turkish queries", ""]
    for row in report["turkish_query_analysis"]:
        lines += [f"- {row['question_id']}: {row['root_cause']} Baseline known-gold ranks: {row['before_gold_ranks']}. After: {row['after_gold_ranks']}.",
                  "  Query policy/queries: " + json.dumps(row["query_normalization"], ensure_ascii=False)]
    lines += ["", "BGE-M3 is not downloaded or assigned fabricated scores. Missing complete/human gold pools still prevent corpus-wide nDCG.",
              "The same pilot was used for development and comparison; improvements require independent human-reviewed held-out confirmation."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local-retrieval", action="store_true")
    args = parser.parse_args()
    data = load_dataset()
    baseline = json.loads((REPORTS / "stage4_baseline/rag_validation.json").read_text(encoding="utf-8"))
    protected = json.loads((REPORTS / "stage4_baseline/protected_integrity.json").read_text(encoding="utf-8"))
    for name, digest in protected["files"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise ValueError("Protected dataset/PDF/clinical JSON changed")
    candidates = {}
    original = dict(QUERY_POLICIES)
    try:
        for mode in ("expand", "rewrite"):
            path = REPORTS / ("stage4_" + mode + ".json")
            if args.run_local_retrieval:
                QUERY_POLICIES.update(minilm=mode, **{"bge-m3": "original"})
                print("Measuring offline candidate: " + mode, flush=True)
                models = run_local_retrieval(data)
                report = build_report(data, models)
                report["experiment_query_policy"] = mode
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                path.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
            else:
                report = json.loads(path.read_text(encoding="utf-8"))
            if report["dataset_sha256"] != baseline["dataset_sha256"]:
                raise ValueError("Comparison dataset changed")
            candidates[mode] = report
    finally:
        QUERY_POLICIES.clear(); QUERY_POLICIES.update(original)
    result = compare(baseline, candidates, data)
    (REPORTS / "stage4_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "stage4_comparison.md").write_text(comparison_markdown(result), encoding="utf-8")
    print(json.dumps({"selected_query_policy": result["selected_query_policy"],
                      "eligibility": result["candidate_eligibility"], "reranked_metrics": result["reranked_metrics"]}))


if __name__ == "__main__":
    main()
