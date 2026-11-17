"""Mock-only translation tests; no API, SQL, model or file writes."""
import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.abstract_translation import (
    MODEL, AbstractTranslationError, prepare_abstract_translations, validate_translation,
)


class AbstractTranslationTests(unittest.TestCase):
    def setUp(self):
        self.source = "Results: 20 patients; 5% had events (p<0.001)."
        self.translation = "Bulgular: 20 hasta; %5'inde olay görüldü (p<0,001)."
        self.article = {"makale_id": "a1", "document_id": "d1", "abstract_en": self.source, "ozet_tr": None}
        self.client = Mock()
        self.client.with_options.return_value = self.client
        self.client.chat.completions.create.return_value = self.response()

    def response(self, *, model=MODEL, finish="stop", content=None, refusal=None):
        return SimpleNamespace(model=model, choices=[SimpleNamespace(
            finish_reason=finish,
            message=SimpleNamespace(content=self.translation if content is None else content, refusal=refusal))])

    def test_prepares_four_results_without_mutating_sources(self):
        articles = [{**self.article, "makale_id": f"a{i}", "document_id": f"d{i}"} for i in range(4)]
        original = copy.deepcopy(articles)
        prepared = prepare_abstract_translations(articles, client=self.client)
        self.assertEqual(articles, original)
        self.assertEqual(len(prepared), 4)
        self.assertEqual(self.client.chat.completions.create.call_count, 4)
        self.client.with_options.assert_called_once_with(max_retries=0, timeout=90.0)
        for result in prepared:
            self.assertEqual(result["ozet_tr"], self.translation)
            self.assertEqual(result["abstract_en"], self.source)
            self.assertEqual(result["abstract_sha256"], hashlib.sha256(self.source.encode()).hexdigest())
            self.assertEqual(result["review_status"], "pending_review")
        for call in self.client.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["model"], "gpt-6-luna")
            self.assertEqual(call.kwargs["max_completion_tokens"], 4096)
            self.assertNotIn("max_tokens", call.kwargs)
            self.assertEqual(call.kwargs["messages"][1]["content"], self.source)
        self.client.close.assert_not_called()

    def test_empty_input_does_not_create_client(self):
        self.assertEqual(prepare_abstract_translations([]), [])

    def test_existing_turkish_abstract_or_missing_metadata_prevents_all_calls(self):
        for field, value in (("ozet_tr", "Existing summary"), ("makale_id", ""),
                             ("document_id", None), ("abstract_en", " ")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare_abstract_translations([self.article, {**self.article, field: value}], client=self.client)
        missing = dict(self.article)
        missing.pop("ozet_tr")
        with self.assertRaises(ValueError):
            prepare_abstract_translations([missing], client=self.client)
        self.client.chat.completions.create.assert_not_called()

    def test_duplicate_sources_rejected_before_any_call(self):
        for extra in (dict(self.article), {**self.article, "makale_id": "a2"},
                      {**self.article, "document_id": "d2"}):
            with self.assertRaises(ValueError):
                prepare_abstract_translations([self.article, extra], client=self.client)
        self.client.chat.completions.create.assert_not_called()

    def test_api_failure_stops_batch_without_retry_and_preserves_prior_result(self):
        self.client.chat.completions.create.side_effect = [self.response(), RuntimeError("private API details")]
        articles = [{**self.article, "makale_id": f"a{i}", "document_id": f"d{i}"} for i in range(3)]
        with self.assertRaises(AbstractTranslationError) as caught:
            prepare_abstract_translations(articles, client=self.client)
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.assertEqual(len(caught.exception.prepared), 1)
        self.assertEqual(caught.exception.article_id, "a1")
        self.assertNotIn("private", str(caught.exception))

    def test_truncated_empty_refused_or_wrong_model_not_accepted(self):
        for response in (self.response(finish="length"), self.response(content=" "),
                         self.response(model="other-model"), self.response(refusal="refused")):
            self.client.chat.completions.create.return_value = response
            with self.assertRaises(AbstractTranslationError):
                prepare_abstract_translations([self.article], client=self.client)

    def test_changed_or_missing_numbers_and_comparisons_rejected(self):
        for text in (self.translation.replace("20", "21"), self.translation.replace("0,001", "0,01"),
                     self.translation.replace("<", ">"), self.translation.replace("%", ""),
                     self.translation + " 2026", "Short abstract without results."):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_translation(self.source, text)

    def test_decimal_and_comparison_typography_normalization(self):
        self.assertEqual(validate_translation(self.source, self.translation), self.translation)
        self.assertEqual(validate_translation("Result ≤7 days, 2%.", "Sonuç <=7 gün, %2."),
                         "Sonuç <=7 gün, %2.")

    def test_invalid_limits_rejected_without_requests(self):
        for budget in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                prepare_abstract_translations([self.article], client=self.client, max_completion_tokens=budget)
        for timeout in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                prepare_abstract_translations([self.article], client=self.client, timeout=timeout)
        self.client.chat.completions.create.assert_not_called()

    def test_factory_reused_and_owned_client_closed(self):
        from src import rag

        with patch.object(rag, "get_llm_client", return_value=self.client) as factory:
            prepare_abstract_translations([self.article])
        factory.assert_called_once_with()
        self.client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
