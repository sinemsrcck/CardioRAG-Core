"""Per-failure regressions and adversarial controls. No SQL/API/model execution."""
import copy
import hashlib
import re
import unittest
from unittest.mock import Mock, patch

from evaluation.evaluate_validation import evaluate_cases, load_dataset
from evaluation.compare_stage4 import eligible
from evaluation.test_claim_validator import source
from src.claim_validator import parse_claim, validate_claims
from src.query_normalization import controlled_translation, retrieval_queries, reranking_query, QUERY_POLICIES


class OriginalFalseRejectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset()
        cls.rows = {r["case_id"]: r for r in evaluate_cases(cls.data)["observations"]}

    def fixed(self, cid):
        self.assertTrue(self.rows[cid]["predicted_accept"], self.rows[cid])

    def test_AF02_controlled_detection_translation(self): self.fixed("AF02")
    def test_ROC01_spaced_auc(self): self.fixed("ROC01")
    def test_ROC02_reporting_suffix(self): self.fixed("ROC02")
    def test_ROC03_plr_caption_keeps_its_own_numbers(self): self.fixed("ROC03")
    def test_POP01_reviewed_is_not_review_article(self): self.fixed("POP01")
    def test_POP02_reversed_count_word_order(self): self.fixed("POP02")
    def test_HB01_ci95_and_scoped_bad_p(self): self.fixed("HB01")
    def test_HB02_question_context_and_reporting(self): self.fixed("HB02")
    def test_TRP02_time_between_assay_words(self): self.fixed("TRP02")
    def test_TRP07_bounded_final_cohort(self): self.fixed("TRP07")
    def test_CAD02_full_guideline_argument_tuple(self): self.fixed("CAD02")
    def test_OHCA02_search_absence_not_event_absence(self): self.fixed("OHCA02")

    def test_TRP01_untyped_plus_minus_remains_rejected(self):
        self.assertFalse(self.rows["TRP01"]["predicted_accept"])
        self.assertIn("ambiguous_statistics_or_origin",
                      self.rows["TRP01"]["claim_verification"]["claims"][0]["reasons"])

    def test_all_existing_negative_partial_and_uncertain_cases_stay_rejected(self):
        for row in self.rows.values():
            if not row["expected_accept"]:
                self.assertFalse(row["predicted_accept"], row["case_id"])

    def test_every_successful_evidence_quote_is_still_in_the_original_chunk(self):
        cases = {c["case_id"]: c for c in self.data["cases"]}
        for cid, row in self.rows.items():
            refs = {r["citation_number"]: self.data["sources"][r["source_id"]]
                    for r in cases[cid]["evidence_refs"]}
            for claim in row.get("claim_verification", {}).get("claims", []):
                for match in claim["matches"]:
                    actual = refs[match["source_number"]]
                    self.assertIn(match["evidence_quote"], actual["source_text"])
                    self.assertEqual(match["text_sha256"], hashlib.sha256(actual["source_text"].encode()).hexdigest())


class ControlledFrameNegativeTests(unittest.TestCase):
    def test_question_cannot_erase_diabetes_restriction(self):
        sources = [source("Among diabetic AF patients, ELR AUC=0.633.")]
        self.assertFalse(validate_claims("ELR AUC=0.633 [1].", sources,
            question="What is the ELR AUC in AF patients?")["claims_verified"])

    def test_explicit_question_can_supply_missing_identical_scope(self):
        sources = [source("Among diabetic AF patients, ELR AUC=0.633.")]
        self.assertTrue(validate_claims("ELR AUC=0.633 [1].", sources,
            question="What is the ELR AUC among diabetic AF patients?")["claims_verified"])

    def test_question_cannot_override_wrong_measurement_interval(self):
        s = source("Delta hs troponin I (1-3h) AUC=0.838.")
        self.assertFalse(validate_claims("Delta hs troponin I (0-1h) AUC=0.838 [1].", [s],
            question="What is the 1-3h delta hs troponin I AUC?")["claims_verified"])

    def test_repaired_auc_layout_cannot_swap_biomarker(self):
        s = source("**A)** ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;")
        self.assertFalse(validate_claims("PLR AUC=0.633 [1].", [s])["claims_verified"])

    def test_malformed_p_is_still_ambiguous_and_not_changed_to_point001(self):
        fields = parse_claim("ELR AUC=0.633, p<001")
        self.assertIn("p", fields["ambiguous_metrics"])
        self.assertNotEqual(fields["metrics"]["p"][1], "0.001")
        self.assertFalse(validate_claims("ELR p<0.001 [1].", [source("ELR p<001.")])["claims_verified"])

    def test_detection_translation_cannot_add_clinical_benefit(self):
        s = source("AF can be detected with wearable devices.")
        self.assertFalse(validate_claims("AF giyilebilir cihazlarla tespit edilebilir ve inme riskini azaltir [1].", [s])["claims_verified"])

    def test_secondary_cohort_is_not_accepted_as_the_current_study(self):
        s = source("Keller et al. evaluated troponin. Patients were excluded from the study. The final evaluation was made on 114 patients.")
        self.assertFalse(validate_claims("Troponin çalışmasında 114 hasta değerlendirilmiştir [1].", [s])["claims_verified"])

    def test_negated_guideline_prefix_does_not_support_positive_recommendation(self):
        s = source("It is not true that the guideline for the evaluation and diagnosis of chest pain classifies FFR-CT as a class IIa recommendation for intermediate-high risk patients presenting with acute or stable chest pain and exhibiting coronary stenosis of 40% to 90% in a proximal or middle coronary segment on CCTA.")
        claim = "Orta-yüksek riskli akut veya stabil göğüs ağrılı ve CCTA proksimal/orta segmentinde %40–90 stenoz bulunan hastalarda FFR-CT Sınıf IIa önerisi olarak aktarılmıştır. [1]"
        self.assertFalse(validate_claims(claim, [s])["claims_verified"])


class ControlledQueryTests(unittest.TestCase):
    def test_all_seven_english_queries_are_unchanged(self):
        for q in load_dataset()["retrieval_questions"]:
            if q["language"] == "en":
                self.assertEqual(controlled_translation(q["question"]), q["question"])

    def test_no_statistics_are_invented(self):
        result = controlled_translation("Kritik karotis darlığında ELR ROC sonucu nedir?")
        self.assertEqual(re.findall(r"\d+", result), [])
        self.assertNotIn("plr", result.lower())

    def test_negation_and_numeric_intervals_survive(self):
        q = "Diyabeti olmayan AF hastalarında 1–3 saat, p<0,001 ve AUC 0,633 nedir?"
        result = controlled_translation(q)
        self.assertIn("without diabetes", result)
        self.assertEqual(re.findall(r"\d+(?:[,.]\d+)?", q), re.findall(r"\d+(?:[,.]\d+)?", result))
        self.assertIn("1–3", result)

    def test_carotid_is_not_coronary(self):
        carotid = controlled_translation("Karotis darlığı çalışmasındaki hasta sayısı nedir?")
        coronary = controlled_translation("Koroner arter hastalığının tanısında hangi yöntemler kullanılır?")
        self.assertIn("carotid", carotid)
        self.assertNotIn("coronary", carotid)
        self.assertIn("coronary", coronary)

    def test_bge_policy_stays_original_and_separate(self):
        q = "Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?"
        with patch.dict(QUERY_POLICIES, {"minilm": "rewrite", "bge-m3": "original"}):
            self.assertNotEqual(reranking_query(q, "minilm"), q)
            self.assertEqual(reranking_query(q, "bge-m3"), q)
            self.assertEqual(retrieval_queries(q, "bge-m3"), [q])

    def test_rewrite_uses_one_query_expand_keeps_original(self):
        q = "Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?"
        with patch.dict(QUERY_POLICIES, {"minilm": "rewrite"}):
            self.assertEqual(len(retrieval_queries(q, "minilm")), 1)
        with patch.dict(QUERY_POLICIES, {"minilm": "expand"}):
            self.assertEqual(retrieval_queries(q, "minilm")[0], q)
            self.assertEqual(len(retrieval_queries(q, "minilm")), 2)
            self.assertEqual(reranking_query(q, "minilm"), q)

    def test_reranker_receives_normalized_query_but_source_text_is_unchanged(self):
        from src.reranker import rerank
        model = Mock();model.predict.return_value = [1.0]
        chunk = {"chunk_id": "mock", "text": "AF can be detected with wearable devices."}
        q = "Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?"
        with patch.dict(QUERY_POLICIES, {"minilm": "rewrite"}), patch("src.reranker.get_reranker", return_value=model):
            output = rerank(q, [chunk], model_name="minilm")
        self.assertEqual(model.predict.call_args.args[0][0], (controlled_translation(q), chunk["text"]))
        self.assertEqual(output[0]["text"], chunk["text"])

    def test_expand_fuses_rank_lists_without_losing_source_metadata(self):
        from src.retrieval import hybrid_search
        chunk = {"chunk_id": "mock", "document_id": "mock-doc", "text_sha256": "mock-hash", "processing_version": "mock-v1"}
        with patch.dict(QUERY_POLICIES, {"minilm": "expand"}), \
             patch("src.retrieval.semantic_search", return_value=[{**chunk, "semantic_score": .5}]) as semantic, \
             patch("src.retrieval.bm25_search", return_value=[{**chunk, "bm25_score": 1.}]) as bm25:
            output = hybrid_search("Atriyal fibrilasyon nedir?", [chunk])
        self.assertEqual(semantic.call_count, 2)
        self.assertEqual(bm25.call_count, 2)
        self.assertEqual(output[0]["processing_version"], "mock-v1")
        self.assertEqual(output[0]["text_sha256"], "mock-hash")


class CandidateAdoptionTests(unittest.TestCase):
    def report(self, tr, en, fp=0):
        def metrics(values):
            return {"reranked": {"provisional_annotated_gold_recall_at_k": {"value": values[0]},
                                  "provisional_annotated_gold_mrr": {"value": values[1]}}}
        return {"dataset_sha256": "synthetic-test-only", "validation": {"provisional_diagnostics_NOT_ground_truth":
            {"claim_validator_only": {"fp": fp}}}, "retrieval_models": {"minilm": {"aggregates": {"tr": metrics(tr), "en": metrics(en)}}}}

    def test_english_regression_blocks_adoption(self):
        self.assertFalse(eligible(self.report((.2, .2), (.7, .7)), self.report((.6, .6), (.6, .6))))

    def test_false_accept_increase_blocks_adoption(self):
        self.assertFalse(eligible(self.report((.2, .2), (.7, .7)), self.report((.6, .6), (.7, .7), fp=1)))

    def test_improvement_without_regressions_can_be_considered(self):
        self.assertTrue(eligible(self.report((.2, .2), (.7, .7)), self.report((.6, .6), (.7, .7))))


if __name__ == "__main__":
    unittest.main()
