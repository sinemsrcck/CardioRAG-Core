"""Metrics and frozen PDF cases; no models, APIs or SQL connections."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evaluation.evaluate_validation import (DATASET, approved, build_report, confusion,
    evaluate_cases, fingerprint, load_dataset, markdown, ranking_metrics, run_local_retrieval)


class ValidationEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()
        cls.report = build_report(cls.data)

    def setUp(self):
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No SQL")))
        self.enterContext(patch("src.rag.get_llm_client", side_effect=AssertionError("No API")))

    def test_dataset_is_separate_and_all_labels_are_provisional(self):
        self.assertGreaterEqual(len(self.data["cases"]), 30)
        self.assertTrue(all(c["review"]["status"] == "provisional" for c in self.data["cases"]))
        self.assertFalse(any(approved(c) for c in self.data["cases"]))
        self.assertEqual({c["language"] for c in self.data["cases"]}, {"tr", "en"})
        self.assertEqual(len({s["document_id"] for s in self.data["sources"].values()}), 6)

    def test_all_four_labels_and_required_failure_dimensions_present(self):
        self.assertEqual({c["expected_label"] for c in self.data["cases"]},
                         {"supported", "unsupported", "partially_supported", "uncertain"})
        tags = {t for c in self.data["cases"] for t in c["tags"]}
        self.assertTrue({"auc", "p", "ci", "patient_group", "time", "wrong_biomarker",
                         "source_type", "wrong_citation"} <= tags)

    def test_no_human_approved_metrics_are_fabricated(self):
        report = self.report["validation"]
        primary = report["human_verified_metrics"]["claim_validator_only"]
        self.assertFalse(primary["computed"])
        self.assertIsNone(primary["precision"])
        self.assertIsNone(primary["tp"])
        self.assertTrue(report["provisional_diagnostics_NOT_ground_truth"]["claim_validator_only"]["computed"])

    def test_confusion_denominators_and_positive_class(self):
        rows = [{"expected_accept": a, "predicted_accept": p} for a, p in
                [(True, True), (False, True), (False, False), (True, False)]]
        result = confusion(rows)
        self.assertEqual([result[k] for k in ("tp", "fp", "tn", "fn")], [1, 1, 1, 1])
        self.assertEqual(result["precision"], .5)
        self.assertEqual(result["recall"], .5)
        self.assertEqual(result["f1"], .5)
        self.assertEqual(result["false_accept_rate"], .5)
        self.assertEqual(result["false_reject_rate"], .5)

    def test_missing_predictions_are_excluded_not_rejected(self):
        result = confusion([{"expected_accept": True, "predicted_accept": None}])
        self.assertFalse(result["computed"])
        self.assertIsNone(result["fn"])

    def test_zero_denominator_is_not_a_perfect_score(self):
        result = confusion([{"expected_accept": False, "predicted_accept": False}])
        self.assertIsNone(result["precision"])
        self.assertIsNone(result["recall"])
        self.assertIsNone(result["f1"])

    def test_human_approval_requires_identity_time_and_fingerprint(self):
        case = copy.deepcopy(self.data["cases"][0])
        case["review"] = {"status": "human_verified", "source": "human", "reviewer_id": "synthetic-test-reviewer",
            "reviewed_at": "2026-11-19T00:00:00+03:00", "reviewed_case_sha256": fingerprint(case)}
        self.assertTrue(approved(case))
        case["answer"] += " altered"
        self.assertFalse(approved(case))

    def test_stale_source_text_is_rejected(self):
        data = copy.deepcopy(self.data)
        next(iter(data["sources"].values()))["source_text"] += " tampered"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json";path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):load_dataset(path)

    def test_related_language_variants_cannot_cross_splits(self):
        data = copy.deepcopy(self.data)
        data["cases"][0]["split"] = "calibration"
        data["cases"][1]["split"] = "test"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json";path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "crosses"):load_dataset(path)

    def test_recall_mrr_ndcg_and_duplicate_hits(self):
        metrics = ranking_metrics(["bad", "a", "a", "b"], {"bad": 0, "a": 2, "b": 1}, k=3, gold_complete=True)
        self.assertEqual(metrics["recall_at_k"], 1)
        self.assertEqual(metrics["reciprocal_rank"], .5)
        self.assertGreater(metrics["ndcg_at_k"], 0)
        self.assertLess(metrics["ndcg_at_k"], 1)

    def test_unjudged_chunks_are_not_negative_ndcg_labels(self):
        metrics = ranking_metrics(["unknown", "a"], {"a": 2}, gold_complete=False)
        self.assertIsNone(metrics["ndcg_at_k"])
        self.assertIsNone(metrics["recall_at_k"])
        self.assertIsNone(metrics["reciprocal_rank"])
        self.assertEqual(metrics["annotated_gold_recall_at_k"], 1)
        self.assertEqual(metrics["annotated_gold_rr"], .5)

    def test_no_positive_gold_is_not_a_zero_or_perfect_recall(self):
        result = ranking_metrics([], {}, gold_complete=True)
        self.assertIsNone(result["recall_at_k"])
        self.assertIsNone(result["ndcg_at_k"])

    def test_empty_returned_pool_is_a_measured_miss(self):
        result = ranking_metrics([], {"a": 2}, gold_complete=True)
        self.assertEqual(result["recall_at_k"], 0)
        self.assertEqual(result["reciprocal_rank"], 0)
        self.assertEqual(result["ndcg_at_k"], 0)

    def test_wrong_citations_do_not_become_free_claim_validator_true_negatives(self):
        result = self.report["validation"]
        case = next(r for r in result["observations"] if r["case_id"] == "AF06")
        self.assertFalse(case["claim_evaluated"])
        self.assertEqual(case["status"], "citation_validation_failed")

    def test_missing_sources_and_short_context_are_measured_abstentions(self):
        result = self.report["validation"]
        for case in (r for r in result["observations"] if r["component"] == "evidence_guard"):
            self.assertEqual(case["status"], "insufficient_evidence")
            self.assertTrue(case["policy_agreement"])

    def test_unrun_models_never_receive_fabricated_metrics(self):
        report = self.report
        for model in ("minilm", "bge-m3"):
            self.assertEqual(report["retrieval_models"][model]["status"], "not_run")
            self.assertIsNone(report["retrieval_models"][model]["aggregates"]["tr"]["retrieved"]["human_verified_mrr"]["value"])
        self.assertIn("not computable", markdown(report))
        first_source = next(iter(self.data["sources"].values()))
        self.assertIn(first_source["filename"], markdown(report))
        self.assertIn(first_source["chunk_id"], markdown(report))
        self.assertIn("Expected label", markdown(report))
        json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_stale_corpus_blocks_each_model_before_any_model_call(self):
        connection = Mock()
        with patch("src.database.get_connection", return_value=connection), \
             patch("src.database.load_chunks_for_rag", return_value=[]), \
             patch("pathlib.Path.is_dir", return_value=True), \
             patch("src.rag.answer_question", side_effect=AssertionError("No models")) as run:
            result = run_local_retrieval(self.data)
        run.assert_not_called()
        connection.start_transaction.assert_called_once_with(readonly=True)
        for model in ("minilm", "bge-m3"):
            self.assertEqual(result[model]["reason"], "ValueError")
            self.assertEqual(result[model]["runs"], [])


if __name__ == "__main__":
    unittest.main()
