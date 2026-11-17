"""Offline clinical extraction tests; no database, models or API calls."""
import copy
import ast
import json
import unittest
import uuid
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.clinical_extraction import (
    FIELDS, DRAFT_COLUMNS, extract_clinical_findings, to_clinical_draft_row,
    to_clinical_draft_item, prepare_rag_save_kwargs,
)
from src.clinical_review import ClinicalReviewError
from evaluation.test_clinical_review import bind_fixture, mock_approved_reviews


def sdk_mock_transport_module():
    """Use the installed SDK's transport for mocks, without adding a dependency."""
    from openai import _base_client

    for name in ("httpx2", "httpx"):
        transport = getattr(_base_client, name, None)
        if transport is not None:
            return transport
    raise AssertionError("Installed SDK has no supported mock HTTP transport")


@lru_cache(maxsize=None)
def branch_contract(filename):
    """Read CURRENT checkout definitions only; no SQL, main or remote ref dependency."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src" / filename).read_text(encoding="utf-8-sig"))
    if filename == "database.py":
        names = {"_optional_float", "_rag_source_rows", "save_rag_result"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {"uuid": uuid, "json": json}
    else:
        nodes = [n for n in tree.body if isinstance(n, (
            ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef))
            and not (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("src."))]
        namespace = dict(branch_contract("clinical_candidates.py")) if filename == "clinical_draft.py" else {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class ClinicalExtractionTests(unittest.TestCase):
    def setUp(self):
        self.text = "Patients with atrial fibrillation received apixaban 5 mg for 6 months."
        self.candidate = {
            "cumle": self.text, "dosya": "example.pdf", "makale_id": "article1",
            "document_id": "doc1", "sayfa": 26, "sayfa_bitis": "",
            "chunk_id": "v2:chunk1", "aday_id": "a1", "kaynak_turu": "metin",
        }
        self.candidate = bind_fixture(self.candidate)
        self.reviews = mock_approved_reviews([self.candidate])
        self.finding = {field: None for field in FIELDS}
        self.finding.update(diagnosis="atrial fibrillation", drug="apixaban",
                            dose="5", dose_unit="mg", treatment_duration="6 months",
                            evidence_quote=self.text)
        self.client = Mock()
        self.client.api_key = "test-only-key"
        self.client.with_options.return_value = self.client

    def run_extract(self, payload=None, candidates=None, content=None, finish="stop", **options):
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=(
                content if content is not None else json.dumps(
                    payload if payload is not None else {"findings": [self.finding]}))))])
        self.client.chat.completions.create.return_value = response
        candidates = [bind_fixture(c) for c in ([self.candidate] if candidates is None else candidates)]
        reviews = {}
        for candidate in candidates:
            try:
                reviews.update(mock_approved_reviews([candidate]))
            except ClinicalReviewError:
                pass  # Let the production preflight reject invalid synthetic input.
        options.setdefault("reviewed_fields_by_candidate", reviews)
        return extract_clinical_findings(
            candidates,
            client=self.client, model=options.pop("model", "mock-model"), **options)

    def test_valid_nulls_metadata_pending(self):
        result = self.run_extract()
        self.assertEqual(result["rejected"], [])
        finding = result["findings"][0]
        self.assertEqual(finding["filename"], "example.pdf")
        self.assertEqual(finding["sayfa"], 26)
        self.assertEqual(finding["chunk_id"], "v2:chunk1")
        self.assertEqual(finding["review_status"], "pending")
        self.assertIsNone(finding["clinical_outcome"])

    def test_empty_input_does_not_call(self):
        self.assertEqual(extract_clinical_findings([]), {"findings": [], "rejected": []})

    def test_empty_candidate_and_missing_metadata(self):
        for key in ("cumle", "dosya", "makale_id", "document_id", "sayfa"):
            with self.subTest(key=key):
                candidate = dict(self.candidate)
                candidate.pop(key)
                with self.assertRaises(ClinicalReviewError):
                    self.run_extract(candidates=[candidate])
        self.client.chat.completions.create.assert_not_called()

    def test_schema_rejects_types_missing_extra_and_metadata(self):
        cases = []
        for field, value in (("dose", 5), ("review_status", "verified"), ("filename", "invented.pdf")):
            item = dict(self.finding)
            item[field] = value
            cases.append({"findings": [item]})
        item = dict(self.finding)
        item.pop("drug")
        cases.extend([{"findings": [item]}, {"findings": {}}, [], {"findings": [], "extra": 1}])
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.run_extract(payload=payload)
                self.assertFalse(result["findings"])
                self.assertTrue(result["rejected"])

    def test_malformed_and_truncated_response(self):
        for content, finish in (("not JSON", "stop"), ("{}", "length"), ("```json\n{}\n```", "stop")):
            self.assertTrue(self.run_extract(content=content, finish=finish)["rejected"])

    def test_quote_and_field_grounding(self):
        for field, value in (("evidence_quote", "invented quote"), ("drug", "warfarin"),
                             ("dose", "10"), ("dose_unit", "")):
            item = dict(self.finding)
            item[field] = value
            self.assertFalse(self.run_extract(payload={"findings": [item]})["findings"])

    def test_no_partial_acceptance_on_invalid_finding(self):
        invalid = dict(self.finding, drug="invented")
        self.assertFalse(self.run_extract(payload={"findings": [self.finding, invalid]})["findings"])

    def test_duplicates_and_distinct_sources(self):
        result = self.run_extract(payload={"findings": [self.finding, self.finding]},
                                  candidates=[self.candidate, dict(self.candidate, aday_id="a2")])
        self.assertEqual(len(result["findings"]), 1)
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)])
        self.assertEqual(len(result["findings"]), 2)

    def test_no_findings_is_valid(self):
        self.assertEqual(self.run_extract(payload={"findings": []}), {"findings": [], "rejected": []})

    def test_api_error_is_reported(self):
        self.client.chat.completions.create.side_effect = RuntimeError("private details")
        result = extract_clinical_findings([self.candidate], client=self.client, model="mock",
                                           reviewed_fields_by_candidate=self.reviews)
        self.assertEqual(result["rejected"][0]["reason"], "RuntimeError")
        self.assertFalse(result["findings"])

    def test_adapter_preserves_pending_quote_and_units(self):
        row = to_clinical_draft_row(self.run_extract()["findings"][0])
        self.assertEqual(row["kanit_cumle"], self.text)
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(json.loads(row["taslak_notu"])["dose_unit"], "mg")
        self.assertEqual(row["icd10"], "")
        with self.assertRaises(ValueError):
            to_clinical_draft_row({"review_status": "verified"})

    def test_missing_chunk_allowed_and_invalid_page_rejected(self):
        candidate = copy.deepcopy(self.candidate)
        candidate.pop("chunk_id")
        self.assertNotIn("chunk_id", self.run_extract(candidates=[candidate])["findings"][0])
        for page in (0, True, "26"):
            with self.assertRaises(ClinicalReviewError):
                self.run_extract(candidates=[dict(candidate, sayfa=page)])

    def test_reuses_project_client_and_model(self):
        self.run_extract()  # Set a mock response without importing RAG/model libraries.
        factory = Mock(return_value=self.client)
        config = SimpleNamespace(DEFAULT_LLM="configured-model", get_llm_client=factory)
        with patch.dict("sys.modules", {"src.rag": config}):
            result = extract_clinical_findings([self.candidate], reviewed_fields_by_candidate=self.reviews)
        factory.assert_called_once_with()
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["model"], "configured-model")
        self.assertTrue(result["findings"])

    def test_client_configuration_failure_is_reported(self):
        config = SimpleNamespace(DEFAULT_LLM="configured-model", get_llm_client=Mock(side_effect=ValueError))
        with patch.dict("sys.modules", {"src.rag": config}):
            result = extract_clinical_findings([self.candidate], reviewed_fields_by_candidate=self.reviews)
        self.assertEqual(result["rejected"][0]["reason"], "ValueError")

    def test_draft_column_contract_and_language(self):
        finding = self.run_extract()["findings"][0]
        finding["clinical_outcome"] = None
        row = to_clinical_draft_row(finding)
        self.assertEqual(tuple(row), tuple(branch_contract("clinical_draft.py")["COLUMNS"]))
        self.assertEqual(tuple(row), DRAFT_COLUMNS)
        self.assertFalse(any(value is None for value in row.values()))
        self.assertEqual(row["tani_en"], "")
        self.assertEqual(row["sonuc_olcutu_tr"], "")
        self.assertEqual(row["doz"], "5 mg")
        english = to_clinical_draft_row(finding, source_language="en")
        self.assertEqual(english["tani_en"], "atrial fibrillation")
        self.assertEqual(english["mudahale_tr"], "")

    def test_draft_invalid_metadata_cross_page_and_status(self):
        finding = self.run_extract()["findings"][0]
        for update in ({"aday_id": None}, {"kaynak_turu": ""}, {"chunk_id": 10},
                       {"sayfa_bitis": 27}, {"birlestirme": "cross-page"},
                       {"review_status": "verified"}, {"document_id": None}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                to_clinical_draft_row({**finding, **update})
        without_chunk = dict(finding)
        without_chunk.pop("chunk_id")
        self.assertEqual(to_clinical_draft_row(without_chunk)["chunk_id"], "")

    def test_real_collector_and_current_validation_in_memory(self):
        candidates_contract = branch_contract("clinical_candidates.py")
        from src.clinical_validation import ValidationContext, validate_row, find_chunk_id
        from src.sql_integration import to_clinical_sql_row
        articles = [("example.pdf", "article1", "doc1", "=== Sayfa 26 ===\n" + self.text)]
        candidates = candidates_contract["collect_candidates"](articles)
        self.assertEqual(len(candidates), 1)
        finding = self.run_extract(candidates=candidates)["findings"][0]
        # Current main validates the extracted quote directly, without the
        # removed build_rows/kesit interface or regenerating a wider quote.
        finding["evidence_quote"] = "apixaban 5 mg for 6 months"
        finding["diagnosis"] = None
        fields = mock_approved_reviews([finding["source_candidate"]], etki_turu="",
                                       yon="belirsiz", outcome_type="")[finding["aday_id"]]
        finding["classification_review"] = fields
        row = to_clinical_sql_row(finding, reviewed_fields=fields, source_language="en")
        ctx = ValidationContext(pages={"doc1": {26: self.text}},
                                makale_by_doc={"doc1": "article1"},
                                chunks={("doc1", 26): [("actual-new-chunk", self.text)]},
                                chunk_ids={"actual-new-chunk"})
        self.assertEqual(validate_row(row, ctx), (True, None, None))
        self.assertEqual(find_chunk_id(row, ctx), "actual-new-chunk")
        self.assertEqual(row["kanit_cumle"], finding["evidence_quote"])
        self.assertEqual(row["review_status"], "pending")

    def test_build_rows_requires_reviewed_enums_and_diagnosis(self):
        finding = self.run_extract()["findings"][0]
        with self.assertRaises(ValueError):
            to_clinical_draft_item(finding, reviewed_fields={})

    def test_gpt_request_parameters_timeout_and_retries_are_explicit(self):
        result = self.run_extract(model="gpt-6-luna", timeout=12.5, max_completion_tokens=4096)
        self.assertTrue(result["findings"])
        self.client.with_options.assert_called_once_with(max_retries=0, timeout=12.5)
        kwargs = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-6-luna")
        self.assertEqual(kwargs["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(json.loads(kwargs["messages"][1]["content"])["candidate_text"], self.text)

    def test_default_api_failure_stops_before_next_candidate(self):
        self.client.chat.completions.create.side_effect = RuntimeError("private details")
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)])
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_reason"], "api_error")
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["findings"], [])

    def test_partial_results_never_enter_normal_findings_after_api_error(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"findings": [self.finding]})))])
        self.client.chat.completions.create.side_effect = [response, RuntimeError("private error")]
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27),
                                              dict(self.candidate, sayfa=28)])
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["partial_findings"]), 1)
        self.assertEqual(result["partial_findings"][0]["review_status"], "pending")
        self.assertNotIn("private error", repr(result))

    def test_invalid_response_also_stops_by_default(self):
        result = self.run_extract(content="not JSON", candidates=[self.candidate, dict(self.candidate, sayfa=27)])
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(result["stop_reason"], "invalid_response")
        self.assertEqual(result["findings"], [])

    def test_call_limit_blocks_extra_requests_and_exposes_partial_results(self):
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)], max_calls=1)
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(result["stop_reason"], "call_limit")
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["partial_findings"]), 1)

    def test_default_limit_is_finite(self):
        candidates = [dict(self.candidate, sayfa=page) for page in range(1, 12)]
        result = self.run_extract(candidates=candidates)
        self.assertEqual(self.client.chat.completions.create.call_count, 10)
        self.assertEqual(result["stop_reason"], "call_limit")

    def test_exact_limit_does_not_discard_completed_run(self):
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)], max_calls=2)
        self.assertEqual(len(result["findings"]), 2)
        self.assertNotIn("stopped", result)

    def test_zero_limit_never_initializes_client(self):
        config = SimpleNamespace(DEFAULT_LLM="gpt-6-luna", get_llm_client=Mock(side_effect=AssertionError("No client")))
        with patch.dict("sys.modules", {"src.rag": config}):
            result = extract_clinical_findings([self.candidate], max_calls=0,
                                               reviewed_fields_by_candidate=self.reviews)
        config.get_llm_client.assert_not_called()
        self.assertEqual(result["api_calls"], 0)
        self.assertEqual(result["stop_reason"], "call_limit")

    def test_invalid_input_aborts_entire_batch_without_consuming_budget(self):
        with self.assertRaises(ClinicalReviewError):
            self.run_extract(candidates=[self.candidate, {}], max_calls=1)
        self.client.chat.completions.create.assert_not_called()

    def test_continuation_requires_explicit_opt_in_and_still_counts_failures(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"findings": [self.finding]})))])
        self.client.chat.completions.create.side_effect = [RuntimeError("private"), response]
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)],
                                  stop_on_error=False, max_calls=2)
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.assertEqual(len(result["findings"]), 1)
        self.client.chat.completions.create.reset_mock()
        self.client.chat.completions.create.side_effect = RuntimeError("private")
        result = self.run_extract(candidates=[self.candidate, dict(self.candidate, sayfa=27)],
                                  stop_on_error=False, max_calls=1)
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(result["stop_reason"], "call_limit")

    def test_missing_credentials_never_attempt_request(self):
        for key in (None, "", "   ", "not-required"):
            with self.subTest(key=key):
                self.client.api_key = key
                result = self.run_extract()
                self.assertEqual(result["stop_reason"], "client_configuration_error")
                self.assertEqual(result["rejected"][0]["error_code"], "missing_api_key")
                self.assertEqual(result["api_calls"], 0)
        self.client.with_options.assert_not_called()
        self.client.chat.completions.create.assert_not_called()

    def test_shared_factory_anonymous_fallback_is_rejected_and_closed(self):
        self.client.api_key = "not-required"
        config = SimpleNamespace(DEFAULT_LLM="gpt-6-luna", get_llm_client=Mock(return_value=self.client))
        with patch.dict("sys.modules", {"src.rag": config}):
            result = extract_clinical_findings([self.candidate], reviewed_fields_by_candidate=self.reviews)
        self.assertEqual(result["stop_reason"], "client_configuration_error")
        self.client.chat.completions.create.assert_not_called()
        self.client.close.assert_called_once_with()

    def test_invalid_safety_options_fail_before_requests(self):
        cases = ([{"timeout": value} for value in (0, -1, True, float("nan"), float("inf"), "60")]
                 + [{"max_calls": value} for value in (-1, True, 1.5)]
                 + [{"max_completion_tokens": value} for value in (0, -1, True, 1.5)]
                 + [{"stop_on_error": "false"}])
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                extract_clinical_findings([self.candidate], client=self.client, model="mock", **options)
        self.client.with_options.assert_not_called()
        self.client.chat.completions.create.assert_not_called()

    def test_errors_do_not_log_or_return_raw_secrets_or_patient_text(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        out, err = StringIO(), StringIO()
        self.client.chat.completions.create.side_effect = RuntimeError("test-only-key PatientPrivateText")
        with redirect_stdout(out), redirect_stderr(err):
            result = self.run_extract()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("test-only-key", repr(result))
        self.assertNotIn("PatientPrivateText", repr(result))

    def test_sdk_retry_override_is_effective_with_mock_http_transport(self):
        from openai import OpenAI

        httpx = sdk_mock_transport_module()
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {"message": "private body", "type": "rate_limit_error"}})
        with OpenAI(api_key="test-only-key", base_url="https://mock.invalid/v1",
                    max_retries=2, timeout=600, http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            result = extract_clinical_findings([self.candidate, self.candidate], client=client,
                                               model="gpt-6-luna", timeout=0.25, reviewed_fields_by_candidate=self.reviews)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].extensions["timeout"]["read"], 0.25)
        self.assertEqual(result["rejected"][0]["http_status"], 429)
        self.assertEqual(result["stop_reason"], "api_error")
        self.assertNotIn("private body", repr(result))

    def test_sdk_timeout_is_sanitized_without_retry(self):
        from openai import OpenAI

        httpx = sdk_mock_transport_module()
        requests = []
        def handler(request):
            requests.append(request)
            raise httpx.ReadTimeout("private timeout data", request=request)
        with OpenAI(api_key="test-only-key", base_url="https://mock.invalid/v1",
                    max_retries=2, http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            result = extract_clinical_findings([self.candidate, self.candidate], client=client,
                                               model="gpt-6-luna", timeout=0.25, reviewed_fields_by_candidate=self.reviews)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["rejected"][0]["reason"], "APITimeoutError")
        self.assertEqual(result["stop_reason"], "api_error")
        self.assertNotIn("private timeout data", repr(result))


class RAGSaveAdapterTests(unittest.TestCase):
    def fixture(self, status="generated"):
        sufficient = status != "insufficient_evidence"
        hit = {"chunk_id": "versioned:1", "rrf_score": 0.04, "rerank_score": -1.2}
        return {"question": "Question?", "answer": None if status == "retrieval_only" else "Answer",
                "status": status, "embedding_model": "minilm", "reranker_model": "bge-m3",
                "llm_enabled": status != "retrieval_only", "sources": [dict(hit)] if sufficient else [],
                "evidence_check": {"sufficient": sufficient, "reason": "guard_reason"},
                "response_time_seconds": 0.123, "top_rerank_score": -1.2,
                "debug": {"retrieved_hits": [dict(hit)], "reranked_hits": [dict(hit)]}}

    def test_writer_receives_correct_contract_and_returns_uuid(self):
        result = self.fixture()
        original = copy.deepcopy(result)
        kwargs = prepare_rag_save_kwargs(result, llm_model="actual-model")
        conn = Mock(autocommit=False)
        namespace = dict(branch_contract("database.py"))
        namespace["_existing_ids"] = Mock(return_value={"versioned:1"})
        namespace["get_connection"] = Mock(side_effect=AssertionError("No real SQL"))
        # Rebind only the writer's globals, keeping the branch function unchanged.
        import types
        writer = types.FunctionType(namespace["save_rag_result"].__code__, namespace,
                                    argdefs=namespace["save_rag_result"].__defaults__)
        query_id = writer(**kwargs, connection=conn)
        self.assertEqual(str(uuid.UUID(query_id)), query_id)
        conn.commit.assert_not_called()
        rows = namespace["_rag_source_rows"](kwargs["result"])
        self.assertTrue(rows[0]["used_in_context"])
        self.assertEqual(rows[0]["rerank_score"], -1.2)  # No score threshold.
        self.assertEqual(result, original)

    def test_no_model_attribution_without_generation(self):
        for status in ("retrieval_only", "insufficient_evidence"):
            result = self.fixture(status)
            kwargs = prepare_rag_save_kwargs(result, llm_model="configured-but-unused")
            self.assertIsNone(kwargs["llm_model"])
            self.assertFalse(kwargs["result"]["llm_enabled"])
            rows = branch_contract("database.py")["_rag_source_rows"](kwargs["result"])
            self.assertEqual(rows[0]["used_in_context"], status == "retrieval_only")

    def test_reject_invalid_contract_before_writer(self):
        updates = [{"question": ""}, {"embedding_model": None}, {"debug": {}},
                   {"status": "unknown"}, {"llm_enabled": None}, {"sources": []},
                   {"response_time_seconds": -1}, {"top_rerank_score": float("nan")},
                   {"evidence_check": {"sufficient": False, "reason": "failure"}}]
        for update in updates:
            with self.subTest(update=update), self.assertRaises(ValueError):
                prepare_rag_save_kwargs({**self.fixture(), **update}, llm_model="actual")
        with self.assertRaises(ValueError):
            prepare_rag_save_kwargs(self.fixture())
        result = self.fixture()
        result["debug"]["retrieved_hits"][0]["chunk_id"] = None
        with self.assertRaises(ValueError):
            prepare_rag_save_kwargs(result, llm_model="actual")

    def test_separate_models_and_candidates_preserved(self):
        for model in ("minilm", "bge-m3"):
            result = self.fixture()
            result["reranker_model"] = model
            result["debug"]["retrieved_hits"].append({"chunk_id": "candidate-only", "rrf_score": 0.01})
            kwargs = prepare_rag_save_kwargs(result, llm_model="actual")
            self.assertEqual(kwargs["result"]["reranker_model"], model)
            rows = branch_contract("database.py")["_rag_source_rows"](kwargs["result"])
            self.assertEqual(len(rows), 2)
            self.assertFalse(rows[1]["used_in_context"])

    def test_current_answer_question_outputs_with_mocked_services(self):
        from src.rag import answer_question

        text = ("Patients with atrial fibrillation received treatment in this study. "
                "These results describe the study population and its clinical outcomes.")
        for use_llm, sufficient in ((True, True), (False, True), (True, False)):
            hit = {"chunk_id": "new:chunk", "document_id": "doc", "filename": "paper.pdf",
                   "page_number": 1, "text": text if sufficient else "short",
                   "rrf_score": 0.03, "rerank_score": -2.0}
            with patch("src.rag.hybrid_search", return_value=[hit]), \
                 patch("src.rag.rerank", return_value=[hit]), \
                 patch("src.rag.generate_answer", return_value="Study result [1].") as generate:
                result = answer_question("Study result?", [hit], use_llm=use_llm)
            kwargs = prepare_rag_save_kwargs(result, llm_model="mock-actual-model")
            self.assertEqual(kwargs["result"]["evidence_check"]["sufficient"], sufficient)
            self.assertEqual(generate.call_count, int(use_llm and sufficient))
            self.assertEqual(kwargs["llm_model"], "mock-actual-model" if use_llm and sufficient else None)


if __name__ == "__main__":
    unittest.main()
