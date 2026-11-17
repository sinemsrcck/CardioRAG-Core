"""Synthetic human-review fixtures and offline preflight/identity tests only."""
import copy
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from src.clinical_candidates import CANDIDATE_ID_VERSION, candidate_binding, collect_candidates
from src.clinical_review import ClinicalReviewError, build_review_template, validate_clinical_reviews
from src.clinical_extraction import FIELDS, extract_clinical_findings


def bind_fixture(candidate):
    """Rebind a SYNTHETIC fixture after a test changes its source/page."""
    candidate = copy.deepcopy(candidate)
    try:
        binding = candidate_binding(candidate)
    except (TypeError, ValueError):
        return candidate
    candidate.update(aday_id=binding["aday_id"], source_text_sha256=binding["source_text_sha256"],
                     candidate_id_version=CANDIDATE_ID_VERSION)
    return candidate


def mock_approved_reviews(candidates, **overrides):
    """Fake approval provenance for synthetic tests, never real clinical data."""
    reviews = build_review_template(candidates)
    for review in reviews.values():
        review.update(mudahale_turu="tedavi", approval_status="approved", review_source="human",
                      reviewer_id="mock-reviewer", reviewed_at="2026-01-01T00:00:00+00:00")
        review.update(overrides)
    return reviews


class ClinicalReviewTests(unittest.TestCase):
    def setUp(self):
        self.text = "apixaban 5 mg was administered."
        self.articles = [("a.pdf", "article-a", "doc-a", "=== Sayfa 1 ===\n" + self.text),
                         ("b.pdf", "article-b", "doc-b", "=== Sayfa 2 ===\nwarfarin 2 mg was administered.")]
        self.candidates = collect_candidates(self.articles)
        self.reviews = mock_approved_reviews(self.candidates)
        self.client = Mock(api_key="test-only-key")
        self.client.with_options.return_value = self.client
        finding = {field: None for field in FIELDS}
        finding.update(drug="apixaban", dose="5", dose_unit="mg", evidence_quote=self.text)
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"findings": [finding]})))])
        self.enterContext(patch.dict("os.environ", {"CLINICAL_REVIEW_MAP": ""}))

    def test_collection_order_and_subset_do_not_change_ids(self):
        reverse = collect_candidates(list(reversed(self.articles)))
        subset = collect_candidates([self.articles[1]])
        by_doc = {c["document_id"]: c["aday_id"] for c in self.candidates}
        self.assertEqual(by_doc, {c["document_id"]: c["aday_id"] for c in reverse})
        self.assertEqual(subset[0]["aday_id"], by_doc["doc-b"])
        self.assertTrue(subset[0]["aday_id"].startswith("clinical-candidate-v1:"))

    def test_text_document_page_and_span_changes_produce_new_id(self):
        source = self.candidates[0]
        original = candidate_binding(source)["aday_id"]
        for update in ({"cumle": self.text + " "}, {"cumle": "apixaban 2 mg was administered."},
                       {"document_id": "different-doc"}, {"sayfa": 3}, {"sayfa_bitis": 2}):
            self.assertNotEqual(original, candidate_binding({**source, **update})["aday_id"])
        self.assertEqual(original, candidate_binding({**source, "dosya": "renamed.pdf"})["aday_id"])

    def test_template_contains_no_approval_or_classification(self):
        template = build_review_template(self.candidates)
        for entry in template.values():
            self.assertEqual(entry["approval_status"], "pending_review")
            self.assertIsNone(entry["mudahale_turu"])
            self.assertIsNone(entry["reviewer_id"])
        with self.assertRaises(ClinicalReviewError):
            validate_clinical_reviews(self.candidates, template)

    def test_approved_candidate_works_and_finding_stays_pending(self):
        result = extract_clinical_findings([self.candidates[0]], client=self.client, model="gpt-6-luna",
                                           reviewed_fields_by_candidate=self.reviews)
        self.client.chat.completions.create.assert_called_once()
        finding = result["findings"][0]
        self.assertEqual(finding["review_status"], "pending")
        self.assertEqual(finding["source_candidate"]["cumle"], self.text)
        self.assertEqual(finding["classification_review"]["review_source"], "human")

    def test_missing_second_approval_aborts_entire_batch_before_api(self):
        reviews = {self.candidates[0]["aday_id"]: self.reviews[self.candidates[0]["aday_id"]]}
        with self.assertRaises(ClinicalReviewError) as caught:
            extract_clinical_findings(self.candidates, client=self.client, model="gpt-6-luna",
                                      reviewed_fields_by_candidate=reviews)
        self.assertEqual(caught.exception.code, "missing_candidate_approval")
        self.client.with_options.assert_not_called()
        self.client.chat.completions.create.assert_not_called()

    def test_missing_map_prevents_client_factory_initialization(self):
        config = SimpleNamespace(DEFAULT_LLM="gpt-6-luna", get_llm_client=Mock())
        with patch.dict("sys.modules", {"src.rag": config}), self.assertRaises(ClinicalReviewError):
            extract_clinical_findings(self.candidates)
        config.get_llm_client.assert_not_called()

    def test_invalid_classifications_rejected_before_api(self):
        for value in (None, "tedavi ", "diagnosis", "verified", True):
            reviews = copy.deepcopy(self.reviews)
            reviews[self.candidates[1]["aday_id"]]["mudahale_turu"] = value
            with self.subTest(value=value), self.assertRaises(ClinicalReviewError):
                extract_clinical_findings(self.candidates, client=self.client, model="gpt-6-luna",
                                          reviewed_fields_by_candidate=reviews)
        self.client.chat.completions.create.assert_not_called()

    def test_only_three_intervention_values_are_allowed(self):
        for value in ("tedavi", "tanı testi", "arka plan tedavisi"):
            reviews = copy.deepcopy(self.reviews)
            for review in reviews.values():
                review["mudahale_turu"] = value
            self.assertEqual(len(validate_clinical_reviews(self.candidates, reviews)), 2)

    def test_enum_only_legacy_map_is_not_human_approval(self):
        old = {c["aday_id"]: {"mudahale_turu": "tedavi"} for c in self.candidates}
        with self.assertRaises(ClinicalReviewError):
            validate_clinical_reviews(self.candidates, old)
        candidate = dict(self.candidates[0], aday_id="article-a#p1#1")
        with self.assertRaises(ClinicalReviewError):
            validate_clinical_reviews([candidate], self.reviews)

    def test_changed_source_invalidates_old_approval(self):
        changed = bind_fixture({**self.candidates[0], "cumle": "apixaban 2 mg was administered."})
        with self.assertRaises(ClinicalReviewError):
            validate_clinical_reviews([changed], self.reviews)
        forged = dict(self.candidates[0], cumle="apixaban 2 mg was administered.")
        with self.assertRaises(ClinicalReviewError):
            validate_clinical_reviews([forged], self.reviews)

    def test_review_source_bindings_types_and_versions_must_match(self):
        for field, value in (("document_id", "wrong-doc"), ("makale_id", "wrong-article"),
                             ("sayfa", True), ("sayfa_bitis", 99), ("source_text_sha256", "0" * 64),
                             ("schema_version", "clinical-review-v0")):
            reviews = copy.deepcopy(self.reviews)
            reviews[self.candidates[0]["aday_id"]][field] = value
            with self.subTest(field=field), self.assertRaises(ClinicalReviewError):
                validate_clinical_reviews(self.candidates, reviews)

    def test_human_provenance_and_timestamp_are_required(self):
        for update in ({"review_source": "automated"}, {"approval_status": "pending_review"},
                       {"reviewer_id": ""}, {"reviewed_at": None},
                       {"reviewed_at": "2026-01-01T00:00:00"}, {"reviewed_at": "invalid"}):
            reviews = copy.deepcopy(self.reviews)
            reviews[self.candidates[0]["aday_id"]].update(update)
            with self.subTest(update=update), self.assertRaises(ClinicalReviewError):
                validate_clinical_reviews(self.candidates, reviews)

    def test_environment_json_map_and_explicit_map_precedence(self):
        with patch.dict("os.environ", {"CLINICAL_REVIEW_MAP": json.dumps(self.reviews)}):
            self.assertEqual(validate_clinical_reviews(self.candidates), self.reviews)
            with self.assertRaises(ClinicalReviewError):
                validate_clinical_reviews(self.candidates, {})

    def test_invalid_environment_json_is_rejected(self):
        for text in ("not-json", "[]", '{"x":{},"x":{}}'):
            with patch.dict("os.environ", {"CLINICAL_REVIEW_MAP": text}), self.assertRaises(ClinicalReviewError):
                validate_clinical_reviews(self.candidates)

    def test_validation_returns_detached_copy(self):
        validated = validate_clinical_reviews(self.candidates, self.reviews)
        validated[self.candidates[0]["aday_id"]]["mudahale_turu"] = "tanı testi"
        self.assertEqual(self.reviews[self.candidates[0]["aday_id"]]["mudahale_turu"], "tedavi")


if __name__ == "__main__":
    unittest.main()
