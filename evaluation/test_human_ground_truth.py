import copy
import tempfile
import unittest
from pathlib import Path

from evaluation import human_ground_truth as h
from evaluation.evaluate_validation import load_dataset, ranking_metrics


class HumanReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()

    def setUp(self):
        self.package = h.build_review_package(self.data)

    def approve(self, row, decision="supported"):
        # Synthetic test reviewer only; never exported as project ground truth.
        row["human_review"].update(status="approved", source="human", reviewer_id="mock-reviewer",
            reviewed_at="2026-11-19T12:00:00+03:00", rationale="synthetic fixture", decision=decision)
        row["human_review"]["reviewed_item_sha256"] = h.binding(row)

    def test_counts_and_pending(self):
        self.assertEqual((len(self.package["claims"]), len(self.package["retrieval"])), (44, 14))
        self.assertTrue(all(r["human_review"]["status"] == "pending_review"
                            for k in ("claims", "retrieval") for r in self.package[k]))

    def researcher_action(self, action="accept", decision="supported", **kwargs):
        self.package["claims"][0]["proposed_final_decision"] = "supported"
        return h.researcher_review(self.package, self.data, "AF01", action=action,
            reviewer_id="synthetic-researcher", decision=decision,
            rationale="Synthetic source review fixture", **kwargs)

    def test_researcher_accept_no_physician_required(self):
        result = self.researcher_action(reviewed_at="2026-11-19T15:00:00+03:00")
        self.assertEqual(result["claims"][0]["human_review"]["review_role"], "researcher")
        self.assertEqual(self.package["claims"][0]["human_review"]["status"], "pending_review")
        self.assertEqual(len(h.validate_approvals(result, self.data)["claims"]), 1)

    def test_researcher_changes_decision(self):
        result = self.researcher_action("change", "partial", reviewed_at="2026-11-19T15:00:00+03:00")
        self.assertEqual(result["claims"][0]["human_review"]["decision"], "partial")
        self.assertEqual(result["claims"][0]["provisional"], self.package["claims"][0]["provisional"])

    def test_researcher_defer_excluded(self):
        result = self.researcher_action("defer")
        self.assertEqual(h.validate_approvals(result, self.data)["claims"], [])
        self.assertIsNone(result["claims"][0]["human_review"]["decision"])

    def test_researcher_requires_real_review_metadata(self):
        with self.assertRaises(ValueError):
            self.researcher_action()
        with self.assertRaises(ValueError):
            self.researcher_action(decision=None, reviewed_at="2026-11-19T15:00:00+03:00")

    def test_researcher_gate_context_enforced(self):
        package = copy.deepcopy(self.package)
        package["preparation"] = {"status": "pending"}
        with self.assertRaisesRegex(ValueError, "context"):
            h.validate_approvals(package, self.data)
        gate = next(r for r in package["claims"] if r["item_id"] == "GATE02")
        gate["context_excerpt"] = "AF"
        h.validate_approvals(package, self.data)
        gate["context_excerpt"] = "full PDF text"
        with self.assertRaisesRegex(ValueError, "context"):
            h.validate_approvals(package, self.data)

    def test_researcher_metric_scope(self):
        report = h.evaluate_human(self.package, self.data)
        self.assertEqual(report["evaluation_name"], h.EVALUATION_NAME)
        self.assertFalse(report["clinical_expert_validation"])
        self.assertFalse(report["clinical_certification"])

    def test_provisional_not_human(self):
        self.assertEqual(h.validate_approvals(self.package, self.data), {"claims": [], "retrieval": []})

    def test_source_quote_and_metadata(self):
        for row in self.package["claims"]:
            for s in row["evidence"]:
                self.assertIn(s["evidence_quote"], s["source_text"])
                self.assertTrue(all(s[k] for k in ("filename", "page_number", "chunk_id")))

    def test_explicit_approval(self):
        self.approve(self.package["claims"][0])
        self.assertEqual(len(h.validate_approvals(self.package, self.data)["claims"]), 1)

    def test_missing_provenance_rejected(self):
        self.approve(self.package["claims"][0])
        for field in ("source", "reviewer_id", "reviewed_at", "rationale", "reviewed_item_sha256"):
            with self.subTest(field=field):
                p = copy.deepcopy(self.package)
                p["claims"][0]["human_review"][field] = None
                with self.assertRaises(ValueError):
                    h.validate_approvals(p, self.data)

    def test_changed_claim_rejected(self):
        self.approve(self.package["claims"][0])
        self.package["claims"][0]["claim"] += " changed"
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_stale_dataset_rejected(self):
        self.package["dataset_sha256"] = "stale"
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_unknown_decision_rejected(self):
        self.approve(self.package["claims"][0], "verified")
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_partial_is_reject_policy(self):
        self.approve(self.package["claims"][0], "partial")
        report = h.evaluate_human(self.package, self.data)
        self.assertEqual(report["answer_policy"]["fp"], 1)
        self.assertEqual(report["answer_policy"]["n"], 1)

    def test_no_approved_no_metrics(self):
        report = h.evaluate_human(self.package, self.data)
        self.assertFalse(report["claim_validator"]["computed"])
        self.assertIsNone(report["claim_validator"]["precision"])
        self.assertEqual(report["retrieval"]["bge-m3"]["status"], "not_run")

    def test_retrieval_requires_human_judgments(self):
        self.approve(self.package["retrieval"][0])
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_retrieval_human_grades_and_complete_metrics(self):
        row = self.package["retrieval"][0]
        cid = row["evidence"][0]["chunk_id"]
        row["human_review"].update(relevance_judgments={cid: 2}, gold_complete=True)
        self.approve(row)
        models = {"minilm": {"status": "measured", "runs": [{"question_id": row["item_id"],
            "stages": {"reranked": {"chunk_ids": [cid]}}}]}}
        result = h.evaluate_human(self.package, self.data, models)["retrieval"]["minilm"]["by_language"]["tr"]
        self.assertEqual(result["recall_at_5"], {"value": 1.0, "n": 1})
        self.assertEqual(result["ndcg_at_5"]["value"], 1.0)

    def test_unjudged_not_negative(self):
        m = ranking_metrics(["unknown", "known"], {"known": 2}, gold_complete=False)
        self.assertIsNone(m["recall_at_k"])
        self.assertIsNone(m["ndcg_at_k"])
        self.assertEqual(m["annotated_gold_rr"], 0.5)

    def test_invalid_retrieval_grade(self):
        row = self.package["retrieval"][0]
        row["human_review"]["relevance_judgments"] = {row["evidence"][0]["chunk_id"]: True}
        self.approve(row)
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_unknown_source_rejected(self):
        row = self.package["retrieval"][0]
        row["human_review"]["relevance_judgments"] = {"invented": 2}
        self.approve(row)
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_export_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved.json"
            archive = h.export_approved(self.package, self.data, path)
            self.assertEqual(archive["claims"], [])
            with self.assertRaises(FileExistsError):
                h.export_approved(self.package, self.data, path)

    def test_approved_archive_roundtrip(self):
        self.approve(self.package["claims"][0])
        with tempfile.TemporaryDirectory() as tmp:
            archive = h.export_approved(self.package, self.data, Path(tmp) / "v2.json")
            report = h.evaluate_approved(archive, self.data)
            self.assertEqual(report["approved_claim_count"], 1)
            self.assertEqual(report["answer_policy"]["tp"], 1)

    def test_source_change_invalidates_approval(self):
        row = self.package["retrieval"][0]
        row["human_review"]["relevance_judgments"] = {row["evidence"][0]["chunk_id"]: 2}
        self.approve(row)
        row["evidence"][0]["source_text"] += " mutation"
        # Even recomputing the review binding cannot bypass source text hashing.
        row["human_review"]["reviewed_item_sha256"] = h.binding(row)
        with self.assertRaises(ValueError):
            h.validate_approvals(self.package, self.data)

    def test_confusion_denominators(self):
        metrics = h.confusion([{"expected_accept": a, "predicted_accept": p}
                               for a, p in ((True, True), (True, False), (False, True), (False, False))])
        self.assertEqual([metrics[k] for k in ("tp", "fp", "tn", "fn")], [1, 1, 1, 1])
        self.assertEqual([metrics[k] for k in ("precision", "recall", "f1", "false_accept_rate", "false_reject_rate")], [0.5]*5)

    def test_independent_manifest_not_ready(self):
        import json
        manifest = json.loads((h.ROOT / "evaluation/human_review/v1/independent_test_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "not_ready")
        self.assertEqual(manifest["claims"], [])
        self.assertEqual(len(manifest["development_documents"]), 6)

    def test_duplicate_and_omitted(self):
        for replacement in ([self.package["claims"][0]] * 44, self.package["claims"][:-1]):
            p = copy.deepcopy(self.package)
            p["claims"] = replacement
            with self.assertRaises(ValueError):
                h.validate_approvals(p, self.data)


if __name__ == "__main__":
    unittest.main()
