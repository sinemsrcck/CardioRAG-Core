"""Collect reviewable retrieval reports without selecting or applying thresholds.

Annotation fields in test_questions.csv:
- answerability_label/review_status: pending until human verification.
- candidate_evidence_json: page hints only, never gold chunk matches.
- evidence_matches_json: exact document_id/page_number/chunk_id, support_level,
  review_status (verified or pending_review), optionally reviewer and rationale.
  Automated verified excerpts retain human_review_status=pending_review and
  calibration_eligible=False. They are expert-review candidates, not gold.
- sufficient_support_sets_json: alternative sufficient sets, each a list of exact
  identities already verified as direct_support in evidence_matches_json.
- split: unassigned, calibration, test. Related groups must stay together.

Use write_report=True explicitly for future CSV export. Default collection and
all local tests are in memory. No model is loaded by importing this module.
"""
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from evaluation.evidence_threshold import (
    evaluate_source_metrics, identity, json_list, validate_splits,
)

OUTPUT_DIR = Path("evaluation/results")
ANNOTATION_FIELDS = (
    "question_group", "split", "answerability_label",
    "answerability_review_status", "proposed_answerability", "gold_review_status",
    "evidence_group_ids_json", "candidate_evidence_json", "evidence_matches_json",
    "sufficient_support_sets_json", "review_notes", "review_origin",
    "human_review_status", "automated_answerability_label",
    "automated_answerability_review_status", "proposed_support_sets_json",
    "requires_multiple_chunks",
    "evidence_refresh_json", "clinical_findings_json",
    "reranker_models_json",
    "annotation_history_json", "scope_proposals_json", "negative_validation_json",
)
JSON_FIELDS = (
    "evidence_group_ids_json", "candidate_evidence_json", "evidence_matches_json",
    "sufficient_support_sets_json", "proposed_support_sets_json",
    "evidence_refresh_json", "clinical_findings_json",
    "reranker_models_json",
    "annotation_history_json", "scope_proposals_json", "negative_validation_json",
)


def answer_question(**kwargs):
    # Lazy import permits metadata review and metric tests without ML imports.
    from src.rag import answer_question as run
    return run(**kwargs)


def validate_annotations(questions):
    ids = set()
    for row in questions:
        question_id = str(row.get("id", ""))
        if not question_id or question_id in ids:
            raise ValueError("Unique nonempty question ids are required")
        ids.add(question_id)
        for field in JSON_FIELDS:
            json_list(row.get(field))
        models = json_list(row.get("reranker_models_json"))
        if len(models) != len(set(models)) or set(models) - {"minilm", "bge-m3"}:
            raise ValueError("Reranker applicability must name separate supported models")
        for match in json_list(row.get("evidence_matches_json")):
            if match.get("review_status") == "verified":
                if not identity(match):
                    raise ValueError("Verified evidence requires document, page and chunk identity")
                if match.get("support_level") not in {
                    "direct_support", "partial_support", "topic_only", "no_support",
                    "uncertain", "irrelevant", "ambiguous",
                }:
                    raise ValueError("Unknown support level")
        if row.get("answerability_review_status") == "verified":
            if row.get("answerability_label") not in {
                "answerable", "partially_answerable", "unanswerable", "ambiguous",
            }:
                raise ValueError("Verified answerability needs an explicit label")
    validate_splits(questions)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def validate_evidence_chunks(questions, chunks):
    """Compare stored annotations with chunks from the full-document pipeline.

    No approval or eligibility is inferred. Nested negative rationale and active
    source references are checked too; archived annotations are deliberately not
    interpreted as current evidence.
    """
    import hashlib
    by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        raise ValueError("Duplicate regenerated chunk ids")

    def check_reference(reference, excerpt=None, full_match=False):
        chunk = by_id.get(reference.get("chunk_id"))
        if chunk is None:
            raise ValueError(f"Stale or unknown chunk id: {reference.get('chunk_id')}")
        for field in ("document_id", "filename", "page_number", "chunk_index", "processing_version"):
            if full_match or field in reference:
                if reference.get(field) != chunk.get(field):
                    raise ValueError(f"Evidence {field} mismatch: {chunk['chunk_id']}")
        digest = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        if digest != chunk["text_sha256"]:
            raise ValueError("Regenerated chunk has an invalid text hash")
        if full_match:
            if reference.get("chunk_text") != chunk["text"] or reference.get("chunk_text_sha256") != digest:
                raise ValueError(f"Stored evidence text/hash mismatch: {chunk['chunk_id']}")
        if excerpt is not None and excerpt not in chunk["text"]:
            raise ValueError(f"Evidence quotation mismatch: {chunk['chunk_id']}")

    primary = contexts = clinical = 0
    for row in questions:
        for match in json_list(row.get("evidence_matches_json")):
            check_reference(match, match.get("supporting_text", ""), full_match=True)
            primary += 1
            for context in match.get("context_evidence", []):
                check_reference(context, context.get("supporting_text", ""), full_match=True)
                contexts += 1
        for field in ("proposed_support_sets_json", "sufficient_support_sets_json"):
            for group in json_list(row.get(field)):
                for reference in group:
                    check_reference(reference)
        for finding in json_list(row.get("clinical_findings_json")):
            if len(finding["sources"]) != len(finding["supporting_text"]):
                raise ValueError("Clinical source/quotation count mismatch")
            for reference, excerpt in zip(finding["sources"], finding["supporting_text"]):
                check_reference(reference, excerpt)
                clinical += 1
        for proposal in json_list(row.get("scope_proposals_json")):
            for chunk_id in proposal.get("source_chunk_ids", []):
                check_reference({"chunk_id": chunk_id})
    return {"primary_matches": primary, "context_matches": contexts, "clinical_sources": clinical}


def _review_hits(hits, chunks_by_id, matches, selected_ids, stage):
    judgments = {identity(m): m for m in matches if identity(m)}
    output = []
    for rank, hit in enumerate(hits, start=1):
        original = chunks_by_id.get(str(hit.get("chunk_id")), {})
        record = {key: original.get(key) for key in (
            "chunk_id", "document_id", "filename", "page_number", "text",
        )}
        record.update(hit)
        label = judgments.get(identity(record), {})
        record.update(stage=stage, rank=rank,
                      selected_for_context=identity(record) in selected_ids,
                      support_level=label.get("support_level", "pending_review"),
                      review_status=label.get("review_status", "pending_review"),
                      review_origin=label.get("review_origin"),
                      human_review_status=label.get("human_review_status", "pending_review"),
                      calibration_eligible=label.get("calibration_eligible", False))
        output.append(record)
    return output


def evaluate_questions(
    questions, chunks, use_llm=False, embedding_model="minilm",
    reranker_model="bge-m3", write_report=False,
):
    """Collect one model's baseline candidates and actual context separately.

    This function never calls threshold selection and never passes an invented
    threshold to RAG. Model execution occurs only when explicitly called.
    """
    if reranker_model not in {"minilm", "bge-m3"}:
        raise ValueError("Evaluate minilm and bge-m3 in separate calls")
    questions = list(questions)
    validate_annotations(questions)
    chunks_by_id = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    if len(chunks_by_id) != len(chunks):
        raise ValueError("Duplicate chunk ids")
    results = []
    for index, item in enumerate(questions, start=1):
        start = time.perf_counter()
        record = {"id": item["id"], "category": item.get("category", ""),
                  "question": item["question"], "expected_behavior": item.get("expected_behavior", "")}
        for field in ANNOTATION_FIELDS:
            default = "[]" if field in JSON_FIELDS else "pending_review"
            if field == "split":
                default = "unassigned"
            record[field] = item.get(field, default)
        record.update(embedding_model=embedding_model, reranker_model=reranker_model,
                      rerank_method="cross_encoder", candidate_k=20, top_k=5,
                      max_chunks_per_document=2, min_relevance_score=None,
                      llm_enabled=use_llm)
        try:
            result = answer_question(
                question=item["question"], chunks=chunks,
                embedding_model=embedding_model, reranker_model=reranker_model,
                use_llm=use_llm,
            )
            matches = json_list(item.get("evidence_matches_json"))
            selected = result["sources"]
            selected_ids = {identity(source) for source in selected}
            debug = result.get("debug", {})
            pools = {}
            for field, hits, stage in (
                ("retrieved_candidates_json", debug.get("retrieved_hits", []), "retrieved"),
                ("reranked_candidates_json", debug.get("reranked_hits", []), "reranked"),
                ("selected_chunks_json", selected, "context"),
            ):
                pools[field] = _dump(_review_hits(hits, chunks_by_id, matches, selected_ids, stage))
            record.update(
                answer=result["answer"], status=result["status"],
                response_time_seconds=result["response_time_seconds"],
                source_count=result["selected_count"],
                unique_article_count=len({s["document_id"] for s in selected}),
                citation_valid=(result["citation_check"]["valid"] if result.get("citation_check") else None),
                sources_json=_dump(selected), evidence_check_json=_dump(result.get("evidence_check")),
                top_rerank_score=result.get("top_rerank_score"), error="", **pools,
            )
        except Exception as exc:
            record.update(answer="", status="error",
                          response_time_seconds=round(time.perf_counter() - start, 3),
                          source_count=0, unique_article_count=0, citation_valid=None,
                          sources_json="[]", evidence_check_json="null", top_rerank_score=None,
                          error=str(exc), retrieved_candidates_json="[]",
                          reranked_candidates_json="[]", selected_chunks_json="[]")
        results.append(record)
        print(f"[{index}/{len(questions)}] {record['status']} | {item['question'][:60]}")
    print(_dump({"reranker_model": reranker_model, "question_count": len(results),
                 "unassigned_count": sum(r["split"] == "unassigned" for r in results),
                 "pending_answerability_count": sum(r["answerability_review_status"] != "verified" for r in results),
                 "error_count": sum(r["status"] == "error" for r in results)}))
    # Collection reports fixed observed pools only. Pending labels yield null metrics.
    for split in ("calibration", "test"):
        print(_dump(evaluate_source_metrics(results, reranker_model, split=split)))
    if write_report and results:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_path = OUTPUT_DIR / f"rag_results_{reranker_model}_{stamp}.csv"
        with output_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
        print(f"Results: {output_path}")
    return results


def load_questions(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as file:
        # Skip leading blank lines without altering multiline quoted CSV fields.
        while True:
            position = file.tell()
            line = file.readline()
            if not line or line.strip():
                file.seek(position)
                break
        questions = list(csv.DictReader(file))
    validate_annotations(questions)
    return questions
