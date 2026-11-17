
import unittest
from unittest.mock import Mock, patch

from src.evidence_guard import check_evidence
from src.rag import answer_question, build_context


TEST_CHUNKS = [
    {
        "chunk_id": 1,
        "document_id": 101,
        "filename": "cardiology_test.pdf",
        "page_number": 2,
        "text": (
            "Atrial fibrillation is associated with an increased "
            "risk of ischemic stroke. Anticoagulation may reduce "
            "stroke risk in appropriately selected patients."
        ),
        "embedding": [0.1, 0.2, 0.3],
    },
    {
        "chunk_id": 2,
        "document_id": 102,
        "filename": "heart_failure_test.pdf",
        "page_number": 3,
        "text": (
            "Heart failure is a clinical syndrome characterized "
            "by symptoms such as dyspnea and fatigue. Diagnosis "
            "requires clinical assessment and supporting tests."
        ),
        "embedding": [0.2, 0.1, 0.4],
    },
]


def fake_hybrid_search(question, chunks, **kwargs):
    return [
        {
            **chunk,
            "rrf_score": 0.03,
        }
        for chunk in chunks
    ]


def fake_rerank(question, chunks, **kwargs):
    return [
        {
            **chunk,
            "rerank_score": 2.0 - index,
        }
        for index, chunk in enumerate(chunks)
    ]


def main():
    with (
        patch("src.rag.hybrid_search", side_effect=fake_hybrid_search),
        patch("src.rag.rerank", side_effect=fake_rerank),
    ):
        result = answer_question(
            question="Atriyal fibrilasyonun inme riski nedir?",
            chunks=TEST_CHUNKS,
            use_llm=False,
        )

    assert result["status"] == "retrieval_only"
    assert result["retrieved_count"] == 2
    assert result["selected_count"] == 2
    assert result["top_rerank_score"] == 2.0
    assert len(result["sources"]) == 2

    print("TEST BAŞARILI")
    print("Durum:", result["status"])
    print("Bulunan kaynak:", result["selected_count"])
    print("En yüksek reranker skoru:", result["top_rerank_score"])


class ProductionGateTests(unittest.TestCase):
    def setUp(self):
        self.search = patch("src.rag.hybrid_search", side_effect=fake_hybrid_search).start()
        self.rank = patch("src.rag.rerank", side_effect=fake_rerank).start()
        self.generate = patch("src.rag.generate_answer", return_value="Supported answer [1].").start()
        self.client = patch("src.rag.get_llm_client", side_effect=AssertionError("API forbidden")).start()
        self.addCleanup(patch.stopall)

    def run_rag(self, chunks=None, **kwargs):
        return answer_question(
            question="Atriyal fibrilasyonun inme riski nedir?",
            chunks=TEST_CHUNKS if chunks is None else chunks,
            **kwargs,
        )

    def assert_rejected(self, result):
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["evidence_check"]["sufficient"])
        self.assertEqual(result["sources"], [])
        self.generate.assert_not_called()
        self.client.assert_not_called()

    def test_empty_sources(self):
        self.assert_rejected(self.run_rag([]))

    def test_short_source(self):
        self.assert_rejected(self.run_rag([{**TEST_CHUNKS[0], "text": "Short."}]))

    def test_irrelevant_source_with_caller_threshold(self):
        unrelated = {**TEST_CHUNKS[0], "text": (
            "Python is a programming language used for software development, "
            "automation, data analysis and many other computing tasks."
        )}
        # Synthetic fixture scores; this is NOT a production calibration.
        self.rank.side_effect = lambda question, chunks, **kw: [
            {**chunk, "rerank_score": -5.0} for chunk in chunks
        ]
        result = self.run_rag([unrelated], min_relevance_score=1.0)
        self.assert_rejected(result)
        self.assertEqual(result["evidence_check"]["reason"], "below_relevance_threshold")

    def test_sufficient_source_generates(self):
        result = self.run_rag([TEST_CHUNKS[0]])
        self.assertEqual(result["status"], "generated")
        self.generate.assert_called_once()
        self.assertIn(TEST_CHUNKS[0]["text"], self.generate.call_args.args[1])

    def test_turkish_question_english_source_retrieval_only(self):
        with patch("src.rag.check_evidence", wraps=check_evidence) as guard:
            result = self.run_rag([TEST_CHUNKS[0]], use_llm=False)
        self.assertEqual(result["status"], "retrieval_only")
        self.assertIsNone(result["answer"])
        self.assertEqual(guard.call_count, 2)
        for call in guard.call_args_list:
            self.assertEqual(call.kwargs["question"], result["question"])
            self.assertFalse(call.kwargs["enforce_keyword_overlap"])
        self.generate.assert_not_called()

    def test_false_sufficiency_blocks_even_with_nonempty_chunks(self):
        with patch("src.rag.check_evidence", return_value={
            "sufficient": False, "reason": "rejected", "chunks": TEST_CHUNKS,
        }):
            self.assert_rejected(self.run_rag())

    def test_context_budget_removes_all_sources(self):
        with patch("src.rag.build_context", side_effect=lambda chunks: build_context(chunks, max_chars=1)):
            self.assert_rejected(self.run_rag())

    def test_empty_context_with_sources_blocks(self):
        _, sources = build_context(TEST_CHUNKS)
        with patch("src.rag.build_context", return_value=("   ", sources)):
            self.assert_rejected(self.run_rag())

    def test_final_guard_checks_only_context_sources(self):
        def first_source_only(chunks):
            return build_context(chunks[:1])
        with (
            patch("src.rag.build_context", side_effect=first_source_only),
            patch("src.rag.check_evidence", wraps=check_evidence) as guard,
        ):
            result = self.run_rag(use_llm=False)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(len(guard.call_args_list[0].args[0]), 2)
        self.assertEqual(len(guard.call_args_list[1].args[0]), 1)

    def test_final_guard_rejection_blocks_generation(self):
        with patch("src.rag.check_evidence", side_effect=[
            {"sufficient": True, "reason": "passed", "chunks": TEST_CHUNKS},
            {"sufficient": False, "reason": "final_rejected", "chunks": TEST_CHUNKS},
        ]):
            self.assert_rejected(self.run_rag())

    def test_legacy_keyword_filter_still_rejects(self):
        result = check_evidence(TEST_CHUNKS, question="Python programming syntax")
        self.assertFalse(result["sufficient"])
        self.assertEqual(result["reason"], "low_keyword_overlap")

    def test_no_uncalibrated_default_threshold(self):
        self.rank.side_effect = lambda question, chunks, **kw: [
            {**chunk, "rerank_score": -5.0} for chunk in chunks
        ]
        result = self.run_rag([TEST_CHUNKS[0]], use_llm=False)
        self.assertEqual(result["status"], "retrieval_only")


class EvaluationInfrastructureTests(unittest.TestCase):
    """Synthetic fixtures only: no PDFs, models, exports or API calls."""
    def setUp(self):
        from contextlib import redirect_stdout
        from io import StringIO
        self.enterContext(redirect_stdout(StringIO()))

    def fixture(self, question_id="a", model="minilm", split="test"):
        import json
        chunk = {"document_id": "doc-" + question_id, "page_number": 1,
                 "chunk_id": "chunk-" + question_id, "rerank_score": 2.0}
        return {
            "id": question_id, "question": "Question " + question_id,
            "question_group": question_id, "split": split, "reranker_model": model,
            "answerability_label": "answerable", "answerability_review_status": "verified",
            "gold_review_status": "verified", "status": "retrieval_only",
            "human_review_status": "verified",
            "evidence_matches_json": json.dumps([
                {**chunk, "review_status": "verified", "support_level": "direct_support",
                 "review_origin": "human_review", "human_review_status": "verified",
                 "calibration_eligible": True}]),
            "sufficient_support_sets_json": json.dumps([[chunk]]),
            "retrieved_candidates_json": json.dumps([chunk]),
            "reranked_candidates_json": json.dumps([chunk]),
            "selected_chunks_json": json.dumps([chunk]),
        }

    def test_csv_preserves_original_questions_and_pending_candidates(self):
        from evaluation.evaluate_rag import load_questions
        from pathlib import Path
        rows = load_questions(Path(__file__).with_name("test_questions.csv"))
        self.assertEqual([str(i) for i in range(1, 11)], [r["id"] for r in rows[:10]])
        self.assertEqual([str(i) for i in range(1, 14)], [r["id"] for r in rows])
        for row in rows:
            self.assertEqual(row["answerability_label"], "pending_review")
            self.assertIn(row["split"], {"calibration", "test", "unassigned"})
            self.assertEqual(row["human_review_status"], "pending_review")
            self.assertEqual(row["gold_review_status"], "pending_review")
        import json
        hints = [h for r in rows for h in json.loads(r["candidate_evidence_json"])]
        self.assertEqual(len(hints), 17)
        self.assertTrue(all(h["chunk_id"] is None and h["review_status"] == "pending_review" for h in hints))

    def test_pending_pairs_do_not_enter_threshold_metrics(self):
        from evaluation.evidence_threshold import evaluate_threshold, select_threshold
        row = self.fixture(split="calibration")
        row["evidence_matches_json"] = "[]"
        row["expected_behavior"] = "answer"
        metric = evaluate_threshold([row], 1.0, "minilm", split="calibration")
        self.assertIsNone(metric["false_reject_rate"]["value"])
        self.assertIsNone(select_threshold([row], "minilm")["threshold"])

    def test_threshold_evaluation_counts_both_error_types(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        pos = self.fixture("p")
        neg = self.fixture("n")
        matches = json.loads(neg["evidence_matches_json"])
        matches[0]["support_level"] = "irrelevant"
        neg["evidence_matches_json"] = json.dumps(matches)
        candidates = json.loads(pos["reranked_candidates_json"])
        candidates[0]["rerank_score"] = -2.0
        pos["reranked_candidates_json"] = json.dumps(candidates)
        metric = evaluate_threshold([pos, neg], 1.0, "minilm")
        self.assertEqual((metric["fp"], metric["fn"]), (1, 1))
        self.assertEqual(metric["false_accept_rate"]["value"], 1.0)
        self.assertEqual(metric["false_reject_rate"]["value"], 1.0)

    def test_partial_and_ambiguous_pairs_excluded(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        for label in ("partial_support", "ambiguous"):
            row = self.fixture()
            matches = json.loads(row["evidence_matches_json"])
            matches[0]["support_level"] = label
            row["evidence_matches_json"] = json.dumps(matches)
            metric = evaluate_threshold([row], 1.0, "minilm")
            self.assertEqual(metric["excluded_pairs"], 1)
            self.assertIsNone(metric["false_accept_rate"]["value"])

    def test_models_are_not_pooled(self):
        from evaluation.evidence_threshold import evaluate_threshold
        rows = [self.fixture(), self.fixture(model="bge-m3")]
        for model in ("minilm", "bge-m3"):
            self.assertEqual(evaluate_threshold(rows, 1.0, model)["tp"], 1)
        with self.assertRaises(ValueError):
            evaluate_threshold(rows, 1.0, "all")

    def test_selection_never_fits_test_labels(self):
        from evaluation.evidence_threshold import select_threshold
        result = select_threshold([self.fixture()], "minilm")
        self.assertIsNone(result["threshold"])

    def test_same_group_across_splits_rejected(self):
        from evaluation.evidence_threshold import validate_splits
        a, b = self.fixture("a", split="calibration"), self.fixture("b")
        b["question_group"] = a["question_group"]
        with self.assertRaises(ValueError):
            validate_splits([a, b])

    def test_same_page_across_splits_rejected(self):
        import json
        from evaluation.evidence_threshold import validate_splits
        a, b = self.fixture("a", split="calibration"), self.fixture("b")
        hint = json.dumps([{"document_id": "shared", "page_number": 2}])
        a["candidate_evidence_json"] = b["candidate_evidence_json"] = hint
        with self.assertRaises(ValueError):
            validate_splits([a, b])

    def test_unassigned_bridge_cannot_hide_leakage(self):
        from evaluation.evidence_threshold import validate_splits
        a, bridge, b = self.fixture("a", split="calibration"), self.fixture("bridge", split="unassigned"), self.fixture("b")
        a["evidence_group_ids_json"] = '["x"]'
        bridge["evidence_group_ids_json"] = '["x", "y"]'
        b["evidence_group_ids_json"] = '["y"]'
        with self.assertRaises(ValueError):
            validate_splits([a, bridge, b])

    def test_source_metrics_with_verified_support(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        metric = evaluate_source_metrics([self.fixture()], "minilm", k=1)
        self.assertEqual(metric["stages"]["reranked_candidates_json"]["hit_at_k"]["value"], 1.0)
        self.assertEqual(metric["stages"]["selected_chunks_json"]["precision_at_k"]["value"], 1.0)
        self.assertEqual(metric["answerable_coverage"]["value"], 1.0)

    def test_hit_does_not_imply_complete_coverage(self):
        import json
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        other = {"document_id": "other", "page_number": 2, "chunk_id": "other"}
        matches = json.loads(row["evidence_matches_json"])
        matches.append({**other, "review_status": "verified", "support_level": "direct_support",
                        "human_review_status": "verified", "calibration_eligible": True})
        row["evidence_matches_json"] = json.dumps(matches)
        row["sufficient_support_sets_json"] = json.dumps([[matches[0], other]])
        metric = evaluate_source_metrics([row], "minilm", k=1)
        self.assertEqual(metric["stages"]["selected_chunks_json"]["hit_at_k"]["value"], 1.0)
        self.assertEqual(metric["answerable_coverage"]["value"], 0.0)

    def test_unreviewed_precision_is_not_zero(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        row["gold_review_status"] = "pending_review"
        row["evidence_matches_json"] = "[]"
        result = evaluate_source_metrics([row], "minilm")
        self.assertIsNone(result["stages"]["reranked_candidates_json"]["precision_at_k"]["value"])
        self.assertIsNone(result["answerable_coverage"]["value"])

    def test_empty_retrieval_is_a_miss_for_verified_gold(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        row["retrieved_candidates_json"] = "[]"
        result = evaluate_source_metrics([row], "minilm")
        self.assertEqual(result["stages"]["retrieved_candidates_json"]["hit_at_k"]["value"], 0.0)

    def test_verified_page_without_chunk_is_rejected(self):
        from evaluation.evaluate_rag import validate_annotations
        row = self.fixture()
        row["evidence_matches_json"] = '[{"document_id":"d","page_number":1,"review_status":"verified","support_level":"direct_support"}]'
        with self.assertRaises(ValueError):
            validate_annotations([row])

    def test_collection_separates_candidates_from_context_without_export(self):
        import json
        from evaluation.evaluate_rag import evaluate_questions
        sources = [{"number": 1, **{k: TEST_CHUNKS[0][k] for k in ("chunk_id", "document_id", "filename", "page_number")}}]
        result = {"sources": sources, "answer": None, "status": "retrieval_only",
                  "response_time_seconds": 0.01, "selected_count": 1, "citation_check": None,
                  "evidence_check": {"sufficient": True}, "top_rerank_score": 2.0,
                  "debug": {"retrieved_hits": fake_hybrid_search("", TEST_CHUNKS),
                            "reranked_hits": fake_rerank("", TEST_CHUNKS)}}
        row = {"id": "1", "question": "Question", "split": "unassigned"}
        with (
            patch("evaluation.evaluate_rag.answer_question", return_value=result) as run,
            patch("pathlib.Path.mkdir", side_effect=AssertionError("No files")),
            patch("evaluation.evidence_threshold.select_threshold", side_effect=AssertionError("No fitting")),
        ):
            records = evaluate_questions([row], TEST_CHUNKS)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["use_llm"])
        self.assertNotIn("min_relevance_score", run.call_args.kwargs)
        candidates = json.loads(records[0]["reranked_candidates_json"])
        self.assertEqual([c["selected_for_context"] for c in candidates], [True, False])
        self.assertEqual(candidates[0]["text"], TEST_CHUNKS[0]["text"])
        self.assertEqual(len(json.loads(records[0]["selected_chunks_json"])), 1)

    def test_different_settings_cannot_be_pooled(self):
        from evaluation.evidence_threshold import evaluate_threshold
        a, b = self.fixture("a"), self.fixture("b")
        a["candidate_k"], b["candidate_k"] = 20, 40
        with self.assertRaises(ValueError):
            evaluate_threshold([a, b], 1.0, "minilm")

    def test_identity_requires_document_page_and_chunk(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        row = self.fixture()
        pool = json.loads(row["reranked_candidates_json"])
        pool[0]["page_number"] = 99
        row["reranked_candidates_json"] = json.dumps(pool)
        result = evaluate_threshold([row], 1.0, "minilm")
        self.assertEqual(result["tp"], 0)
        self.assertEqual(result["excluded_pairs"], 1)

    def test_reviewed_question_gate_reports_false_accept(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        row["answerability_label"] = "unanswerable"
        result = evaluate_source_metrics([row], "minilm")
        self.assertEqual(result["question_gate"]["false_accept_rate"]["value"], 1.0)
        self.assertIsNone(result["question_gate"]["false_reject_rate"]["value"])

    def test_automated_verified_matches_are_not_ground_truth(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        row = self.fixture()
        matches = json.loads(row["evidence_matches_json"])
        matches[0].update(review_origin="automated_text_review",
                          human_review_status="pending_review", calibration_eligible=False)
        row["evidence_matches_json"] = json.dumps(matches)
        result = evaluate_threshold([row], 1.0, "minilm")
        self.assertIsNone(result["false_reject_rate"]["value"])
        self.assertEqual(result["excluded_pairs"], 1)
        matches[0]["human_review_status"] = "verified"
        row["evidence_matches_json"] = json.dumps(matches)
        self.assertEqual(evaluate_threshold([row], 1.0, "minilm")["tp"], 0)
        matches[0]["calibration_eligible"] = True
        row["evidence_matches_json"] = json.dumps(matches)
        self.assertEqual(evaluate_threshold([row], 1.0, "minilm")["tp"], 1)

    def test_stored_excerpts_are_exact_and_exclusions_are_not_support(self):
        import json
        from pathlib import Path
        from evaluation.evaluate_rag import load_questions
        rows = load_questions(Path(__file__).with_name("test_questions.csv"))
        matches = [m for r in rows for m in json.loads(r["evidence_matches_json"])]
        self.assertEqual(len(matches), 46)
        self.assertEqual(sum(m["review_status"] == "verified" for m in matches), 31)
        import hashlib
        refresh = [entry for row in rows for entry in json.loads(row["evidence_refresh_json"])]
        self.assertEqual(len(refresh), 28)
        self.assertTrue(all(entry["status"] == "re_matched_current_text" for entry in refresh))
        historical_ids = {entry["previous_chunk_id"] for entry in refresh}
        self.assertFalse(historical_ids.intersection(m["chunk_id"] for m in matches))
        for m in matches:
            self.assertRegex(m["chunk_id"], r"_v[0-9a-f]{16}_p\d+_c\d+_t[0-9a-f]{64}$")
            self.assertEqual(hashlib.sha256(m["chunk_text"].encode("utf-8")).hexdigest(),
                             m["chunk_text_sha256"])
            self.assertEqual(m["review_origin"], "automated_text_review")
            self.assertEqual(m["human_review_status"], "pending_review")
            self.assertFalse(m["calibration_eligible"])
            if m["review_status"] == "verified":
                self.assertIn(m["support_level"], {"direct_support", "partial_support"})
                self.assertTrue(m["supporting_text"])
                self.assertIn(m["supporting_text"], m["chunk_text"])
                self.assertIn(m["text_quality"], {"readable_prose", "structured_table"})
            if m["text_quality"].endswith("excluded"):
                self.assertEqual(m["supporting_text"], "")
                self.assertEqual(m["review_status"], "pending_review")
        findings = [finding for row in rows for finding in json.loads(row["clinical_findings_json"])]
        self.assertEqual(len(findings), 3)
        current = {m["chunk_id"]: m for m in matches}
        for finding in findings:
            self.assertEqual(finding["human_review_status"], "pending_review")
            self.assertEqual(finding["database_write_status"], "not_written")
            self.assertTrue(finding["patient_group"])
            self.assertTrue(finding["clinical_outcome"])
            self.assertEqual(len(finding["sources"]), len(finding["supporting_text"]))
            for source, excerpt in zip(finding["sources"], finding["supporting_text"]):
                match = current[source["chunk_id"]]
                self.assertEqual(source["page_number"], match["page_number"])
                self.assertIn(excerpt, match["chunk_text"])

    def test_new_negative_vocabulary_requires_human_review(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        for label in ("topic_only", "no_support"):
            row = self.fixture()
            matches = json.loads(row["evidence_matches_json"])
            matches[0]["support_level"] = label
            row["evidence_matches_json"] = json.dumps(matches)
            result = evaluate_threshold([row], 1.0, "minilm")
            self.assertEqual(result["fp"], 1)

    def test_collection_errors_are_reported(self):
        from evaluation.evaluate_rag import evaluate_questions
        with patch("evaluation.evaluate_rag.answer_question", side_effect=RuntimeError("fixture failure")):
            records = evaluate_questions([{"id": "1", "question": "Q", "split": "unassigned"}], [])
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("fixture failure", records[0]["error"])

    def test_verified_label_without_explicit_human_approval_is_excluded(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        row = self.fixture()
        matches = json.loads(row["evidence_matches_json"])
        for key in ("review_origin", "human_review_status", "calibration_eligible"):
            matches[0].pop(key, None)
        row["evidence_matches_json"] = json.dumps(matches)
        result = evaluate_threshold([row], 1.0, "minilm")
        self.assertEqual(result["tp"], 0)
        self.assertEqual(result["excluded_pairs"], 1)
        self.assertIsNone(result["false_reject_rate"]["value"])

    def test_unapproved_question_cannot_enter_question_metrics(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        row["human_review_status"] = "pending_review"
        result = evaluate_source_metrics([row], "minilm")
        self.assertIsNone(result["question_gate"]["false_reject_rate"]["value"])
        self.assertIsNone(result["answerable_coverage"]["value"])

    def test_both_splits_have_candidate_classes_without_promoting_gold(self):
        import json
        from pathlib import Path
        from evaluation.evaluate_rag import load_questions
        from evaluation.evidence_threshold import _judgments
        rows = load_questions(Path(__file__).with_name("test_questions.csv"))
        for split in ("calibration", "test"):
            pool = [r for r in rows if r["split"] == split]
            labels = {r["automated_answerability_label"] for r in pool}
            self.assertTrue({"answerable", "unanswerable_candidate"}.issubset(labels))
        for row in rows:
            self.assertEqual(_judgments(row), {})
            self.assertEqual(set(json.loads(row["reranker_models_json"])), {"minilm", "bge-m3"})
        related = [r for r in rows if r["question_group"] == "carotid_cohort"]
        self.assertEqual(len(related), 3)
        self.assertEqual({r["split"] for r in related if r["split"] != "unassigned"}, {"calibration"})
        dose = next(r for r in rows if r["id"] == "10")
        self.assertEqual(dose["split"], "unassigned")
        self.assertIn("apiksaban", dose["question"])
        self.assertTrue(json.loads(dose["annotation_history_json"]))

    def evidence_fixture(self):
        import json
        from src.chunking import chunk_document
        chunk = chunk_document({
            "document_id": "fixture-doc", "filename": "fixture.pdf", "processing_version": "fixture-v2",
            "pages": [{"page_number": 1, "text": TEST_CHUNKS[0]["text"]}],
        })[0]
        match = {**chunk, "chunk_text": chunk["text"], "chunk_text_sha256": chunk["text_sha256"],
                 "supporting_text": "Atrial fibrillation"}
        return chunk, match, {"evidence_matches_json": json.dumps([match])}

    def test_full_document_validation_rejects_self_consistent_stale_id(self):
        import json
        from evaluation.evaluate_rag import validate_evidence_chunks
        chunk, match, row = self.evidence_fixture()
        self.assertEqual(validate_evidence_chunks([row], [chunk])["primary_matches"], 1)
        match["chunk_id"] = "previous-extraction-id"
        row["evidence_matches_json"] = json.dumps([match])
        with self.assertRaises(ValueError):
            validate_evidence_chunks([row], [chunk])

    def test_full_document_validation_checks_source_coordinates_and_hash(self):
        import json
        from evaluation.evaluate_rag import validate_evidence_chunks
        chunk, match, row = self.evidence_fixture()
        for field, invalid in (("filename", "wrong.pdf"), ("document_id", "wrong-document"),
                               ("page_number", 2), ("chunk_index", 1),
                               ("chunk_text_sha256", "wrong-hash"), ("chunk_text", "different text")):
            row["evidence_matches_json"] = json.dumps([{**match, field: invalid}])
            with self.assertRaises(ValueError, msg=field):
                validate_evidence_chunks([row], [chunk])

    def test_full_document_validation_checks_negative_context_quotes(self):
        import json
        from evaluation.evaluate_rag import validate_evidence_chunks
        chunk, match, row = self.evidence_fixture()
        match["context_evidence"] = [{**match, "supporting_text": "invented long-term outcome"}]
        row["evidence_matches_json"] = json.dumps([match])
        with self.assertRaises(ValueError):
            validate_evidence_chunks([row], [chunk])

    def test_review_audit_current_csv_never_promotes_automated_labels(self):
        from pathlib import Path
        from evaluation.evaluate_rag import load_questions
        from evaluation.evidence_threshold import audit_review_status, calibration_readiness
        questions = load_questions(Path(__file__).with_name("test_questions.csv"))
        audit = audit_review_status(questions)
        for split, count, pairs in (("calibration", 3, 3), ("test", 9, 43), ("unassigned", 1, 0)):
            self.assertEqual(audit[split]["pending_questions"], count)
            self.assertEqual(audit[split]["pending_or_ineligible_pairs"], pairs)
            self.assertEqual(audit[split]["human_answerable"], 0)
            self.assertEqual(audit[split]["human_unanswerable"], 0)
        self.assertEqual(audit["test"]["automated_verified_pairs"], 31)
        report = calibration_readiness(questions)
        self.assertFalse(report["production_threshold_applied"])
        for model in ("minilm", "bge-m3"):
            for split in ("calibration", "test"):
                data = report["models"][model]["splits"][split]
                self.assertFalse(data["both_scored_classes_present"])
                self.assertIsNone(data["threshold_evaluation"]["threshold"])
                self.assertIsNone(data["source_metrics"]["answerable_coverage"]["value"])

    def test_audit_separates_human_labels_and_other_groups(self):
        import json
        from evaluation.evidence_threshold import audit_review_status
        positive = self.fixture("pos", split="calibration")
        negative = self.fixture("neg", split="calibration")
        negative["answerability_label"] = "unanswerable"
        match = json.loads(negative["evidence_matches_json"])[0]
        match["support_level"] = "no_support"
        negative["evidence_matches_json"] = json.dumps([match])
        other = self.fixture("other", split="unassigned")
        other["answerability_label"] = "ambiguous"
        result = audit_review_status([positive, negative, other])
        self.assertEqual(result["calibration"]["human_answerable"], 1)
        self.assertEqual(result["calibration"]["human_unanswerable"], 1)
        self.assertEqual(result["calibration"]["approved_positive_pairs"], 1)
        self.assertEqual(result["calibration"]["approved_negative_pairs"], 1)
        self.assertEqual(result["unassigned"]["human_other"], 1)

    def test_dual_collection_uses_mock_runner_and_keeps_scores_separate(self):
        from evaluation.evaluate_rag import collect_reranker_scores
        from evaluation.evidence_threshold import calibration_readiness
        import json
        row = self.fixture("q")
        row["evidence_matches_json"] = "[]"
        row["gold_review_status"] = "pending_review"
        hit = {**TEST_CHUNKS[0], "chunk_id": "chunk-q", "document_id": "doc-q"}
        def runner(**kwargs):
            score = -3.0 if kwargs["reranker_model"] == "minilm" else 8.0
            return {"sources": [hit], "answer": None, "status": "retrieval_only",
                    "response_time_seconds": 0.01, "selected_count": 1,
                    "debug": {"retrieved_hits": [dict(hit, rrf_score=0.02)],
                              "reranked_hits": [dict(hit, rerank_score=score)]}}
        with patch("evaluation.evaluate_rag.answer_question", side_effect=AssertionError("No real models")), \
             patch("evaluation.evidence_threshold.select_threshold", side_effect=AssertionError("No fitting")), \
             patch("pathlib.Path.mkdir", side_effect=AssertionError("No output files")):
            mock = Mock(side_effect=runner)
            collected = collect_reranker_scores([row], [hit], run_question=mock)
            report = calibration_readiness([row], collected["minilm"] + collected["bge-m3"])
        self.assertEqual(mock.call_count, 2)
        for call in mock.call_args_list:
            self.assertFalse(call.kwargs["use_llm"])
            self.assertNotIn("min_relevance_score", call.kwargs)
        for model, score in (("minilm", -3.0), ("bge-m3", 8.0)):
            self.assertEqual(json.loads(collected[model][0]["reranked_candidates_json"])[0]["rerank_score"], score)
            self.assertEqual(report["models"][model]["splits"]["test"]["scored_questions"], 1)

    def test_readiness_evaluates_only_explicit_model_thresholds(self):
        import json
        from evaluation.evidence_threshold import calibration_readiness
        rows = []
        for model, positive_score, negative_score in (("minilm", -2.0, 2.0), ("bge-m3", 8.0, 2.0)):
            for qid, label, score in (("pos", "direct_support", positive_score), ("neg", "no_support", negative_score)):
                row = self.fixture(qid, model=model)
                matches = json.loads(row["evidence_matches_json"])
                matches[0]["support_level"] = label
                row["evidence_matches_json"] = json.dumps(matches)
                hits = json.loads(row["reranked_candidates_json"])
                hits[0]["rerank_score"] = score
                row["reranked_candidates_json"] = json.dumps(hits)
                if label == "no_support":
                    row.update(answerability_label="unanswerable", sufficient_support_sets_json="[]")
                rows.append(row)
        report = calibration_readiness([], rows, thresholds={"minilm": 1.0, "bge-m3": 5.0})
        mini = report["models"]["minilm"]["splits"]["test"]["threshold_evaluation"]
        bge = report["models"]["bge-m3"]["splits"]["test"]["threshold_evaluation"]
        self.assertEqual((mini["fp"], mini["fn"]), (1, 1))
        self.assertEqual((bge["fp"], bge["fn"]), (0, 0))
        self.assertFalse(report["production_threshold_applied"])
        with self.assertRaises(ValueError):
            calibration_readiness([], rows, thresholds={"shared": 1.0})

    def test_repeated_candidates_do_not_inflate_precision_or_consume_k(self):
        import json
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        relevant = json.loads(row["reranked_candidates_json"])[0]
        irrelevant = {"document_id": "other", "page_number": 2, "chunk_id": "other"}
        matches = json.loads(row["evidence_matches_json"])
        matches.append({**irrelevant, "support_level": "no_support", "review_status": "verified",
                        "human_review_status": "verified", "calibration_eligible": True})
        row["evidence_matches_json"] = json.dumps(matches)
        row["reranked_candidates_json"] = json.dumps([relevant, relevant, irrelevant])
        result = evaluate_source_metrics([row], "minilm", k=2)
        self.assertEqual(result["stages"]["reranked_candidates_json"]["precision_at_k"]["value"], 0.5)

    def test_unknown_run_status_is_not_a_false_reject(self):
        from evaluation.evidence_threshold import evaluate_source_metrics
        row = self.fixture()
        row["status"] = "not_run"
        result = evaluate_source_metrics([row], "minilm")
        self.assertIsNone(result["question_gate"]["false_reject_rate"]["value"])

    def test_bool_scores_cannot_enter_threshold_metrics(self):
        import json
        from evaluation.evidence_threshold import evaluate_threshold
        row = self.fixture()
        hits = json.loads(row["reranked_candidates_json"])
        hits[0]["rerank_score"] = True
        row["reranked_candidates_json"] = json.dumps(hits)
        self.assertEqual(evaluate_threshold([row], 1.0, "minilm")["excluded_pairs"], 1)


if __name__ == "__main__":
    main()
    unittest.main()
