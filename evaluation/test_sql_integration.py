"""Current main SQL writers with mocked connections; no network/model calls."""
import copy
import json
import unittest
import uuid
import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from src import database
from src.clinical_candidates import collect_candidates
from src.clinical_extraction import FIELDS
from src.clinical_validation import ValidationContext, load_validation_context as read_validation_context
from src.clinical_review import ClinicalReviewError
from evaluation.test_clinical_review import bind_fixture, mock_approved_reviews
from src.sql_integration import (
    _check_source, answer_question_with_sql, extract_and_save_clinical_findings,
    to_clinical_sql_row,
)


class SQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.text = ("Patients with atrial fibrillation received apixaban 5 mg for 6 months. "
                     "The study reported clinical outcomes in this patient population.")
        self.articles = [("sample.pdf", "article1", "doc1", "=== Sayfa 1 ===\n" + self.text)]
        self.candidate = collect_candidates(self.articles)[0]
        self.quote = self.candidate["cumle"]
        self.finding = {field: None for field in FIELDS}
        self.finding.update(diagnosis="atrial fibrillation", drug="apixaban", dose="5",
                            dose_unit="mg", treatment_duration="6 months", evidence_quote=self.quote)
        self.extracted = {**self.finding, "filename": "sample.pdf", "makale_id": "article1",
                          "document_id": "doc1", "sayfa": 1, "sayfa_bitis": None,
                          "kaynak_turu": "metin", "birlestirme": "", "review_status": "pending",
                          "aday_id": self.candidate["aday_id"]}
        self.extracted.update(source_candidate=copy.deepcopy(self.candidate),
                              classification_review=mock_approved_reviews([self.candidate])[self.candidate["aday_id"]])
        self.ctx = ValidationContext(pages={"doc1": {1: self.text}},
                                     makale_by_doc={"doc1": "article1"},
                                     chunks={("doc1", 1): [("new:chunk", self.text)]},
                                     chunk_ids={"new:chunk"},
                                     chunk_versions={"new:chunk": "current-v1"},
                                     active_versions={"doc1": "current-v1"})
        self.conn = Mock(autocommit=False, in_transaction=False)
        self.cursor = self.conn.cursor.return_value
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = (1,)
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No real MySQL")))
        self.enterContext(patch("src.database._column_max_lengths", return_value={}))
        self.enterContext(patch("src.clinical_validation.load_validation_context", return_value=self.ctx))
        self.client = Mock()
        self.client.api_key = "test-only-key"
        self.client.with_options.return_value = self.client
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"findings": [self.finding]})))])

    def run_clinical(self, *, connection=True, reviews=True, query_id=None):
        kwargs = {"client": self.client, "model": "mock-llm", "source_language": "en",
                  "query_id": query_id}
        kwargs["reviewed_fields_by_candidate"] = mock_approved_reviews(collect_candidates(self.articles)) if reviews else {}
        if connection:
            kwargs["connection"] = self.conn
        return extract_and_save_clinical_findings(self.articles, **kwargs)

    def rag_result(self, status="generated"):
        sufficient = status != "insufficient_evidence"
        hit = {"chunk_id": "new:chunk", "rrf_score": 0.02, "rerank_score": -1.0}
        return {"query_id": str(uuid.uuid4()), "question": "Question?", "answer": None if status == "retrieval_only" else "Answer",
                "status": status, "sources": [hit] if sufficient else [],
                "evidence_check": {"sufficient": sufficient, "reason": "test"},
                "embedding_model": "minilm", "reranker_model": "bge-m3", "rerank_method": "cross_encoder",
                "llm_enabled": status != "retrieval_only", "response_time_seconds": 0.1,
                "top_rerank_score": -1.0, "citation_check": None,
                "debug": {"retrieved_hits": [hit], "reranked_hits": [hit]}}

    def test_clinical_full_pipeline_real_writer_pending_and_metadata(self):
        result = self.run_clinical()
        self.assertEqual(result["sql_status"], "pending_caller_commit")
        self.assertEqual(result["sql_result"]["yazilan"], 1)
        sql, params = self.cursor.executemany.call_args.args
        self.assertIn("INSERT INTO klinik_bulgular", sql)
        row = dict(zip(database.KLINIK_INSERT_COLUMNS, params[0]))
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(row["chunk_id"], "new:chunk")
        self.assertEqual((row["makale_id"], row["document_id"], row["kanit_sayfa"]), ("article1", "doc1", 1))
        self.assertIsNone(row["hasta_sayisi"])
        self.assertIsNone(row["sonuc_olcutu_tr"])
        self.assertEqual(json.loads(row["taslak_notu"])["filename"], "sample.pdf")
        self.conn.commit.assert_not_called()
        self.conn.close.assert_not_called()

    def test_duplicate_clinical_findings_not_inserted_again(self):
        first = self.run_clinical()
        self.assertEqual(first["sql_result"]["yazilan"], 1)
        values = self.cursor.executemany.call_args.args[1][0]
        digest = dict(zip(database.KLINIK_INSERT_COLUMNS, values))["satir_hash"]
        self.cursor.fetchall.return_value = [(digest,)]
        self.cursor.executemany.reset_mock()
        second = self.run_clinical()
        self.assertEqual(second["sql_result"]["zaten_var"], 1)
        self.assertEqual(second["sql_result"]["yazilan"], 0)
        self.cursor.executemany.assert_not_called()

    def test_missing_classification_rejected_not_guessed(self):
        with self.assertRaises(ClinicalReviewError):
            self.run_clinical(reviews=False)
        self.client.chat.completions.create.assert_not_called()
        self.conn.cursor.assert_not_called()
        self.cursor.executemany.assert_not_called()

    def test_api_failure_with_partial_findings_never_enters_sql_scope(self):
        first_response = self.client.chat.completions.create.return_value
        self.articles.append(("second.pdf", "article2", "doc2", "=== Sayfa 1 ===\n" + self.text))
        self.client.chat.completions.create.side_effect = [first_response, RuntimeError("private API data")]
        result = self.run_clinical()
        self.assertTrue(result["stopped"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["partial_findings"]), 1)
        self.assertEqual(result["sql_status"], "not_attempted")
        self.assertIsNone(result["sql_result"])
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.conn.cursor.assert_not_called()

    def test_sql_validation_rejection_prevents_clinical_and_audit_inserts(self):
        with patch("src.database._column_max_lengths", return_value={"mudahale_en": 1}):
            result = self.run_clinical(query_id="query-id")
        self.assertEqual(result["sql_status"], "error")
        self.assertIsNone(result["sql_result"])
        self.assertTrue(result["adapter_rejected"])
        self.cursor.executemany.assert_not_called()
        self.conn.commit.assert_not_called()
        self.assertIn("ROLLBACK TO SAVEPOINT clinical_llm_pipeline",
                      [c.args[0] for c in self.cursor.execute.call_args_list])

    def test_invalid_late_classification_blocks_api_and_sql_for_entire_batch(self):
        self.articles.append(("second.pdf", "article2", "doc2", "=== Sayfa 1 ===\n" + self.text))
        candidates = collect_candidates(self.articles)
        reviews = mock_approved_reviews(candidates)
        reviews[candidates[1]["aday_id"]]["mudahale_turu"] = "unreviewed"
        with self.assertRaises(ClinicalReviewError):
            extract_and_save_clinical_findings(self.articles, client=self.client, model="mock-llm",
                                               reviewed_fields_by_candidate=reviews, connection=self.conn)
        self.client.with_options.assert_not_called()
        self.client.chat.completions.create.assert_not_called()
        self.conn.cursor.assert_not_called()

    def test_source_failure_in_later_finding_does_not_save_earlier_finding(self):
        self.articles.append(("second.pdf", "article2", "doc2", "=== Sayfa 1 ===\n" + self.text))
        # Both LLM responses validate, but the second document is not in the SQL context.
        result = self.run_clinical()
        self.assertEqual(result["sql_status"], "error")
        self.assertIsNone(result["sql_result"])
        self.cursor.executemany.assert_not_called()
        self.conn.commit.assert_not_called()

    def test_enum_only_adapter_input_is_not_accepted_as_approval(self):
        with self.assertRaises(ClinicalReviewError):
            to_clinical_sql_row(self.extracted, reviewed_fields={"mudahale_turu": "tedavi"})

    def test_partial_extraction_report_is_never_written_even_without_stop_flag(self):
        partial = {"findings": [self.extracted], "rejected": [{"reason": "mock_failure"}]}
        with patch("src.sql_integration.extract_clinical_findings", return_value=partial):
            result = self.run_clinical()
        self.assertEqual(result["sql_status"], "not_attempted")
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["partial_findings"]), 1)
        self.conn.cursor.assert_not_called()

    def test_missing_metadata_and_verified_input_rejected(self):
        for update in ({"document_id": None}, {"filename": None}, {"review_status": "verified"},
                       {"kaynak_turu": None}, {"chunk_id": 1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                to_clinical_sql_row({**self.extracted, **update})

    def test_wrong_chunk_cannot_pass_by_existence_alone(self):
        self.ctx.chunk_ids.add("unrelated:chunk")
        row = to_clinical_sql_row({**self.extracted, "chunk_id": "unrelated:chunk"})
        with self.assertRaises(ValueError):
            _check_source(row, self.ctx)
        with self.assertRaises(ValueError):
            row = to_clinical_sql_row({**self.extracted, "makale_id": "wrong-article"})
            _check_source(row, self.ctx)

    def test_mult_page_metadata_preserved_without_inventing_chunk(self):
        candidate = bind_fixture({**self.candidate, "sayfa_bitis": 2, "birlestirme": "sayfa"})
        finding = {**self.extracted, "aday_id": candidate["aday_id"], "sayfa_bitis": 2, "birlestirme": "sayfa",
                   "source_candidate": candidate,
                   "classification_review": mock_approved_reviews([candidate])[candidate["aday_id"]]}
        row = to_clinical_sql_row(finding)
        self.assertEqual(row["kanit_sayfa_bitis"], 2)
        self.assertEqual(row["birlestirme"], "sayfa")
        self.assertIsNone(row["chunk_id"])
        self.assertEqual(row["mudahale_turu"], "tedavi")
        self.assertIsNone(row["tani_en"])

    def test_sql_failure_rolls_back_only_scopes_and_preserves_extraction(self):
        self.cursor.executemany.side_effect = RuntimeError("database failure")
        result = self.run_clinical()
        self.assertEqual(result["sql_status"], "error")
        self.assertIsNone(result["sql_result"])
        self.assertTrue(result["findings"])
        commands = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertIn("ROLLBACK TO SAVEPOINT save_clinical_findings", commands)
        self.assertIn("ROLLBACK TO SAVEPOINT clinical_llm_pipeline", commands)
        self.conn.commit.assert_not_called()
        self.conn.rollback.assert_not_called()

    def test_owned_transaction_commit_and_rollback(self):
        with patch("src.database.get_connection", return_value=self.conn):
            self.assertEqual(self.run_clinical(connection=False)["sql_status"], "committed")
        self.conn.commit.assert_called_once_with()
        self.conn.close.assert_called_once_with()
        self.conn.reset_mock()
        self.cursor.executemany.side_effect = RuntimeError("database failure")
        with patch("src.database.get_connection", return_value=self.conn):
            self.assertEqual(self.run_clinical(connection=False)["sql_status"], "error")
        self.conn.rollback.assert_called_once_with()
        self.conn.commit.assert_not_called()

    def test_clinical_connection_failure_returns_extracted_findings(self):
        with patch("src.database.get_connection", side_effect=ConnectionError("private details")):
            result = self.run_clinical(connection=False)
        self.assertEqual(result["sql_error"], "ConnectionError")
        self.assertTrue(result["findings"])

    def test_autocommit_external_connection_refused(self):
        self.conn.autocommit = True
        self.conn.in_transaction = False
        result = self.run_clinical()
        self.assertEqual(result["sql_status"], "error")
        self.cursor.executemany.assert_not_called()

    def test_rag_real_writer_correct_llm_attribution_and_no_result_mutation(self):
        for status in ("generated", "retrieval_only", "insufficient_evidence"):
            result = self.rag_result(status)
            original = copy.deepcopy(result)
            with patch("src.rag.answer_question", return_value=result), \
                 patch("src.rag.DEFAULT_LLM", "actual-model"), \
                 patch("src.database._existing_ids", return_value={"new:chunk"}), \
                 patch("src.database._validate_rag_sources"), \
                 patch.object(self.cursor, "fetchone", return_value=None):
                output = answer_question_with_sql("Question?", [], connection=self.conn)
            query = next(c.args[1] for c in reversed(self.cursor.execute.call_args_list)
                         if "INSERT INTO rag_sorgulari" in c.args[0])
            self.assertEqual(query["llm_model"], "actual-model" if status == "generated" else None)
            self.assertEqual(output["answer"], original["answer"])
            self.assertEqual(output["sql_persistence"]["status"], "pending_caller_commit")
            self.assertEqual(result, original)

    def test_rag_logging_connection_failure_never_loses_answer(self):
        result = self.rag_result()
        with patch("src.rag.answer_question", return_value=result), \
             patch("src.database.get_connection", side_effect=ConnectionError("secret")):
            output = answer_question_with_sql("Question?", [])
        self.assertEqual(output["answer"], "Answer")
        self.assertEqual(output["sql_persistence"]["error"], "ConnectionError")
        self.assertNotIn("secret", str(output))

    def test_rag_sql_statement_failure_keeps_answer_and_rolls_back(self):
        self.cursor.executemany.side_effect = RuntimeError("source insert failed")
        with patch("src.rag.answer_question", return_value=self.rag_result()), \
             patch("src.database._existing_ids", return_value={"new:chunk"}), \
                 patch("src.database._validate_rag_sources"), \
                 patch.object(self.cursor, "fetchone", return_value=None):
            output = answer_question_with_sql("Question?", [], connection=self.conn)
        self.assertEqual(output["answer"], "Answer")
        self.assertEqual(output["sql_persistence"]["status"], "error")
        self.assertIn("ROLLBACK TO SAVEPOINT save_rag_result",
                      [c.args[0] for c in self.cursor.execute.call_args_list])

    def test_chunk_load_failure_has_explicit_status_without_fabricated_answer(self):
        with patch("src.database.load_chunks_for_rag", side_effect=ConnectionError), \
             patch("src.rag.answer_question") as run:
            output = answer_question_with_sql("Question?")
        self.assertEqual(output["status"], "data_unavailable")
        self.assertIsNone(output["answer"])
        run.assert_not_called()

    def test_current_sql_chunks_feed_real_retrieval_and_rag(self):
        import hashlib
        vector = np.array([1, 0], dtype=np.float32)
        self.cursor.fetchall.return_value = [(
            "doc1", "sample.pdf", "new:chunk", 1, 0, self.text,
            hashlib.sha256(self.text.encode()).hexdigest(), len(self.text),
            "current-version", 1500, 250, "minilm", 2, database.VECTOR_DTYPE, vector.tobytes())]
        def rerank(question, hits, **kwargs):
            return [{**hit, "rerank_score": -2.0} for hit in hits]
        with patch("src.retrieval.embed_query", return_value=vector), \
             patch("src.rag.rerank", side_effect=rerank), \
             patch("src.rag.generate_answer", side_effect=AssertionError("No LLM")), \
             patch("src.database._existing_ids", return_value={"new:chunk"}), \
                 patch("src.database._validate_rag_sources"), \
                 patch.object(self.cursor, "fetchone", return_value=None):
            result = answer_question_with_sql("atrial fibrillation", connection=self.conn, use_llm=False)
        self.assertEqual(result["status"], "retrieval_only")
        self.assertEqual(result["sources"][0]["chunk_id"], "new:chunk")
        self.assertEqual(result["sources"][0]["source_text"], self.text)
        self.assertEqual(result["sources"][0]["processing_version"], "current-version")
        self.assertEqual(result["sources"][0]["text_sha256"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(result["sql_persistence"]["status"], "pending_caller_commit")


class ClinicalSourceValidationTests(unittest.TestCase):
    """Exercise the real validator and direct writer, without SQL/model calls."""

    def setUp(self):
        self.quote = "Treatment reduced events by 20%."
        self.ctx = ValidationContext(
            pages={"doc": {2: self.quote, 3: "Other page."}},
            makale_by_doc={"doc": "article"},
            chunks={("doc", 2): [("chunk", self.quote)]},
            chunk_ids={"chunk"}, chunk_versions={"chunk": "v2"},
            active_versions={"doc": "v2"},
        )
        self.row = dict(document_id="doc", makale_id="article", chunk_id="chunk",
                        kanit_sayfa=2, kanit_sayfa_bitis=2, kanit_cumle=self.quote,
                        mudahale_turu="tedavi", kanit_turu="metin", review_status="pending")
        self.conn = Mock(autocommit=False, in_transaction=False)
        self.cursor = self.conn.cursor.return_value
        self.cursor.fetchall.return_value = []
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No real SQL")))
        self.enterContext(patch("src.clinical_validation.load_validation_context", return_value=self.ctx))
        self.enterContext(patch("src.database._column_max_lengths", return_value={}))

    def save(self, row):
        return database.save_clinical_findings([row], "rag", connection=self.conn)

    def assert_rejected(self, row):
        result = self.save(row)
        self.assertEqual(result["yazilan"], 0)
        self.assertEqual(len(result["reddedilen"]), 1)
        self.cursor.executemany.assert_not_called()

    def test_current_chunk_direct_write_stays_pending(self):
        self.assertEqual(self.save(self.row)["yazilan"], 1)
        record = dict(zip(database.KLINIK_INSERT_COLUMNS, self.cursor.executemany.call_args.args[1][0]))
        self.assertEqual(record["review_status"], "pending")
        self.assertEqual(record["chunk_id"], "chunk")

    def test_wrong_article_direct_write_rejected(self):
        self.assert_rejected({**self.row, "makale_id": "another-article"})

    def test_wrong_document_direct_write_rejected(self):
        self.assert_rejected({**self.row, "document_id": "another-document"})

    def test_wrong_page_even_with_quote_in_full_text_rejected(self):
        self.ctx.pages["doc"][3] = self.quote
        self.assert_rejected({**self.row, "kanit_sayfa": 3, "kanit_sayfa_bitis": 3})

    def test_wider_page_range_rejected(self):
        self.assert_rejected({**self.row, "kanit_sayfa_bitis": 3})

    def test_invalid_page_types_rejected(self):
        for value in (True, 2.5, "2", 0, None):
            with self.subTest(value=value):
                self.assert_rejected({**self.row, "kanit_sayfa": value})

    def test_missing_chunk_rejected(self):
        self.assert_rejected({**self.row, "chunk_id": "missing"})

    def test_existing_chunk_from_another_document_rejected(self):
        self.ctx.chunks[("other-doc", 2)] = [("other-chunk", self.quote)]
        self.ctx.chunk_ids.add("other-chunk")
        self.ctx.chunk_versions["other-chunk"] = "v2"
        self.ctx.active_versions["other-doc"] = "v2"
        self.assert_rejected({**self.row, "chunk_id": "other-chunk"})

    def test_old_chunk_version_direct_write_rejected(self):
        self.ctx.chunk_versions["chunk"] = "v1"
        self.assert_rejected(self.row)

    def test_stale_declared_version_rejected(self):
        self.assert_rejected({**self.row, "processing_version": "v1"})

    def test_unknown_active_version_rejected(self):
        self.ctx.active_versions.clear()
        self.assert_rejected(self.row)

    def test_quote_in_article_but_not_chunk_rejected(self):
        self.ctx.chunks[("doc", 2)] = [("chunk", "Unrelated text.")]
        self.assert_rejected(self.row)

    def test_empty_quote_rejected(self):
        self.assert_rejected({**self.row, "kanit_cumle": " \n "})

    def test_noncontiguous_excerpt_cannot_bypass_chunk_check(self):
        text = "Treatment reduced intermediate events by 20%."
        self.ctx.pages["doc"][2] = text
        self.ctx.chunks[("doc", 2)] = [("chunk", text)]
        self.assert_rejected({**self.row, "birlestirme": "ara_blok"})

    def test_table_row_in_actual_chunk_is_accepted(self):
        table = "| Outcome | Value |\n|---|---|\n| Treatment | 20% |"
        self.ctx.pages["doc"][2] = table
        self.ctx.chunks[("doc", 2)] = [("chunk", table)]
        row = {**self.row, "kanit_turu": "tablo", "kanit_cumle": "Treatment | 20%"}
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_only_whitespace_normalization_is_allowed(self):
        row = {**self.row, "kanit_cumle": "Treatment\n reduced events by 20%."}
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_markdown_or_punctuation_changes_do_not_match(self):
        for quote in ("Treatment reduced events by 20 percent.", "Treatment **reduced** events by 20%."):
            with self.subTest(quote=quote):
                self.assert_rejected({**self.row, "kanit_cumle": quote})

    def test_missing_id_resolves_only_unique_current_chunk(self):
        self.assertEqual(self.save({**self.row, "chunk_id": None})["yazilan"], 1)
        record = dict(zip(database.KLINIK_INSERT_COLUMNS, self.cursor.executemany.call_args.args[1][0]))
        self.assertEqual(record["chunk_id"], "chunk")

    def test_missing_id_and_no_chunk_rejected(self):
        self.ctx.chunks.clear()
        self.assert_rejected({**self.row, "chunk_id": None})

    def test_ambiguous_automatic_chunk_selection_rejected(self):
        self.ctx.chunks[("doc", 2)].append(("other", self.quote))
        self.ctx.chunk_ids.add("other")
        self.ctx.chunk_versions["other"] = "v2"
        self.assert_rejected({**self.row, "chunk_id": None})

    def test_sql_context_reads_only_active_completed_document_chunks(self):
        conn = Mock()
        cursor = conn.cursor.return_value
        cursor.fetchall.side_effect = [
            [("doc", "article", "=== Sayfa 2 ===\n" + self.quote)], [],
            [("chunk", "doc", 2, self.quote, "v2")],
        ]
        ctx = read_validation_context(conn)
        query = cursor.execute.call_args.args[0]
        self.assertIn("b.processing_version = c.processing_version", query)
        self.assertIn("b.durum = 'tamamlandi'", query)
        self.assertEqual(ctx.chunk_versions, {"chunk": "v2"})
        self.assertEqual(ctx.active_versions, {"doc": "v2"})
        self.assertFalse(any(call.args[0].lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
                             for call in cursor.execute.call_args_list))

    def test_duplicate_article_document_mapping_fails_closed(self):
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = [
            ("doc", "article", "text"), ("doc", "another-article", "text"),
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            read_validation_context(conn)
        conn.cursor.return_value.executemany.assert_not_called()


class ClinicalRelationshipTests(unittest.TestCase):
    setUp = ClinicalSourceValidationTests.setUp
    save = ClinicalSourceValidationTests.save
    assert_rejected = ClinicalSourceValidationTests.assert_rejected

    def use_quote(self, text, **fields):
        self.ctx.pages["doc"][2] = text
        self.ctx.chunks[("doc", 2)] = [("chunk", text)]
        self.row.update(kanit_cumle=text, **fields)
        return self.row

    def test_apixaban_cannot_borrow_warfarin_dose_direct_sql(self):
        row = self.use_quote("Apixaban 5 mg and warfarin 2 mg were administered.",
                             mudahale_en="Apixaban", doz="2 mg")
        self.assert_rejected(row)
        self.assertIn("clinical_relationship_contradicted", self.save(row)["reddedilen"][0]["aciklama"])

    def test_correct_drug_dose_in_comparison_is_supported(self):
        row = self.use_quote("Apixaban 5 mg and warfarin 2 mg were administered.",
                             mudahale_en="Apixaban", doz="5 mg")
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_explicit_dose_phrase_supported(self):
        row = self.use_quote("Apixaban at a dose of 5 mg was administered.",
                             mudahale_en="Apixaban", doz="5 mg")
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_dose_without_unit_does_not_infer_a_unit(self):
        row = self.use_quote("Apixaban 5 mg was administered.", mudahale_en="Apixaban", doz="5")
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_patient_groups_cannot_exchange_outcomes(self):
        row = self.use_quote(
            "Adults received Apixaban 5 mg with reduced events of 20%; "
            "Children received warfarin 2 mg with reduced events of 40%.",
            hasta_grubu="Adults", mudahale_en="Apixaban", doz="5 mg",
            sonuc_olcutu_tr="events", deger="40%")
        self.assert_rejected(row)

    def test_explicit_population_intervention_outcome_observation(self):
        row = self.use_quote("Adults received Apixaban 5 mg with reduced events of 20%.",
                             hasta_grubu="Adults", mudahale_en="Apixaban", doz="5 mg",
                             sonuc_olcutu_tr="events", deger="20%")
        self.assertEqual(self.save(row)["yazilan"], 1)

    def test_unresolved_cross_sentence_relation_needs_review(self):
        row = self.use_quote("Apixaban was discussed. A dose of 5 mg was reported.",
                             mudahale_en="Apixaban", doz="5 mg")
        self.assert_rejected(row)
        self.assertIn("human review required", self.save(row)["reddedilen"][0]["aciklama"])

    def table_row(self, **fields):
        header = "| Patient group | Drug | Dose | Outcome | Value |\n|---|---|---|---|---|\n"
        body = "| Adults | Apixaban | 5 mg | events | 20% |"
        text = header + body + "\n| Children | warfarin | 2 mg | bleeding | 40% |"
        row = self.use_quote(text, kanit_turu="tablo", hasta_grubu="Adults",
                             mudahale_en="Apixaban", doz="5 mg",
                             sonuc_olcutu_tr="events", deger="20%")
        row["kanit_cumle"] = "Adults | Apixaban | 5 mg | events | 20%"
        row.update(fields)
        return row

    def test_labelled_table_row_links_all_fields(self):
        self.assertEqual(self.save(self.table_row())["yazilan"], 1)

    def test_table_dose_cannot_come_from_another_row(self):
        self.assert_rejected(self.table_row(doz="2 mg"))

    def test_table_column_names_cannot_be_ignored(self):
        row = self.table_row()
        text = self.ctx.pages["doc"][2].replace("| Drug | Dose |", "| Dose | Drug |")
        self.ctx.pages["doc"][2] = text
        self.ctx.chunks[("doc", 2)] = [("chunk", text)]
        self.assert_rejected(row)

    def test_unlabelled_table_needs_review(self):
        row = self.table_row()
        text = self.ctx.pages["doc"][2].replace("Patient group", "A").replace("Drug", "B")
        self.ctx.pages["doc"][2] = text
        self.ctx.chunks[("doc", 2)] = [("chunk", text)]
        self.assert_rejected(row)

    def test_extraction_payload_cannot_hide_wrong_relationship(self):
        row = self.use_quote("Apixaban 5 mg and warfarin 2 mg were administered.")
        row["taslak_notu"] = json.dumps({"extracted_fields": {
            "drug": "Apixaban", "dose": "2", "dose_unit": "mg"}})
        self.assert_rejected(row)

    def test_valid_native_fields_cannot_hide_bad_extracted_fields(self):
        row = self.use_quote("Apixaban 5 mg and warfarin 2 mg were administered.",
                             mudahale_en="Apixaban", doz="5 mg")
        row["taslak_notu"] = json.dumps({"extracted_fields": {
            "drug": "Apixaban", "dose": "2", "dose_unit": "mg"}})
        self.assert_rejected(row)

    def test_second_intervention_column_cannot_bypass_check(self):
        row = self.use_quote("Apixaban 5 mg and warfarin 2 mg were administered.",
                             mudahale_en="Apixaban", mudahale_tr="warfarin", doz="5 mg")
        self.assert_rejected(row)

    def test_relationship_status_separates_contradiction_from_uncertainty(self):
        from src.clinical_validation import assess_clinical_relationships
        self.assertEqual(assess_clinical_relationships(
            {"drug": "Apixaban", "dose": "2", "dose_unit": "mg"},
            "Apixaban 5 mg and warfarin 2 mg were administered."), "contradicted")
        self.assertEqual(assess_clinical_relationships(
            {"drug": "Apixaban", "dose": "5", "dose_unit": "mg"},
            "Apixaban was studied. The dose was 5 mg."), "uncertain")

    def test_table_multiple_rows_do_not_establish_cross_row_relationship(self):
        from src.clinical_validation import assess_clinical_relationships
        self.table_row()
        text = self.ctx.pages["doc"][2]
        self.assertEqual(assess_clinical_relationships(
            {"drug": "Apixaban", "dose": "2 mg", "patient_group": "Children"},
            text, text, "tablo"), "contradicted")

    def test_partial_population_word_is_not_a_population_match(self):
        row = self.use_quote("women received Apixaban 5 mg with reduced events of 20%.",
                             hasta_grubu="men", mudahale_en="Apixaban", doz="5 mg",
                             sonuc_olcutu_tr="events", deger="20%")
        self.assert_rejected(row)

    def test_multiple_subjects_without_punctuation_need_review(self):
        row = self.use_quote("Adults had events of 20% children had events of 40%.",
                             hasta_grubu="Adults", sonuc_olcutu_tr="events", deger="40%")
        self.assert_rejected(row)


class TableContextAndResultTests(unittest.TestCase):
    """Synthetic end-to-end extraction: mock API, mock SQL, no real approvals."""
    setUp = ClinicalSourceValidationTests.setUp
    save = ClinicalSourceValidationTests.save
    assert_rejected = ClinicalSourceValidationTests.assert_rejected

    def table_fixture(self):
        self.table = ("**Table 1. Treatments in adults**\n"
                      "| Patient group | Drug | Dose | Note | Outcome | Value |\n"
                      "|---|---|---|---|---|---|\n"
                      "| Adults | apixaban | 5 mg | | bleeding | 2% |")
        self.candidate = collect_candidates([
            ("fixture.pdf", "article", "doc", "=== Sayfa 2 ===\n" + self.table)])[0]
        self.fields = {key: None for key in FIELDS}
        self.fields.update(drug="apixaban", dose="5", dose_unit="mg", patient_group="Adults",
                           clinical_outcome="bleeding", numeric_value="2%",
                           evidence_quote=self.candidate["cumle"])
        self.client = Mock(api_key="test-only-key")
        self.client.with_options.return_value = self.client
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"findings": [self.fields]})))])
        self.ctx.pages["doc"][2] = self.table
        self.ctx.chunks[("doc", 2)] = [("chunk", self.table)]
        return self.candidate

    def extract_table(self):
        from src.clinical_extraction import extract_clinical_findings
        return extract_clinical_findings([self.candidate], client=self.client, model="mock-model",
                    reviewed_fields_by_candidate=mock_approved_reviews([self.candidate]))["findings"][0]

    def test_caption_headers_and_empty_cells_reach_mock_llm_separately(self):
        self.table_fixture()
        finding = self.extract_table()
        message = json.loads(self.client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        context = message["table_context"]
        self.assertEqual(context["caption"], "**Table 1. Treatments in adults**")
        self.assertEqual([c.strip() for c in context["columns"]],
                         ["Patient group", "Drug", "Dose", "Note", "Outcome", "Value"])
        self.assertEqual([c.strip() for c in context["row_cells"]],
                         ["Adults", "apixaban", "5 mg", "", "bleeding", "2%"])
        self.assertEqual(message["candidate_text"], context["data_row"])
        self.assertNotIn("Table 1", finding["evidence_quote"])
        self.assertEqual(finding["source_candidate"]["table_context"], context)

    def test_full_table_flow_to_real_validator_and_mock_writer(self):
        self.table_fixture()
        finding = self.extract_table()
        row = to_clinical_sql_row(finding, source_language="en")
        self.assertIsNone(row["sonuc_olcutu_tr"])
        self.assertEqual(self.save(row)["yazilan"], 1)
        stored = dict(zip(database.KLINIK_INSERT_COLUMNS, self.cursor.executemany.call_args.args[1][0]))
        self.assertEqual(stored["review_status"], "pending")
        self.assertEqual(stored["chunk_id"], "chunk")

    def test_context_change_invalidates_existing_human_fixture_approval_before_api(self):
        from src.clinical_candidates import candidate_binding
        from src.clinical_extraction import extract_clinical_findings
        self.table_fixture()
        reviews = mock_approved_reviews([self.candidate])
        old_id = self.candidate["aday_id"]
        self.candidate["table_context"]["caption"] = "**Table 2. Another population**"
        self.assertNotEqual(old_id, candidate_binding(self.candidate)["aday_id"])
        self.candidate = bind_fixture(self.candidate)
        with self.assertRaises(ClinicalReviewError):
            extract_clinical_findings([self.candidate], client=self.client,
                    reviewed_fields_by_candidate=reviews)
        self.client.chat.completions.create.assert_not_called()

    def test_complex_headers_remain_review_only_even_with_enum(self):
        from src.clinical_review import build_review_template
        self.table_fixture()
        text = self.table.replace("|---|", "|---|", 1).replace(
            "| Patient group", "| Groups | Treatments | | | | |\n| Patient group", 1)
        candidate = collect_candidates([("fixture.pdf", "article", "doc", "=== Sayfa 2 ===\n" + text)])[0]
        self.assertEqual(candidate["table_context"]["status"], "pending_review")
        with self.assertRaises(ClinicalReviewError):
            build_review_template([candidate])

    def test_synthetic_joined_quote_is_rejected(self):
        from src.clinical_extraction import _validate_response
        self.table_fixture()
        self.fields["evidence_quote"] = "Table 1 Treatments " + self.candidate["cumle"]
        with self.assertRaises(ValueError):
            _validate_response({"findings": [self.fields]}, self.candidate["cumle"])

    def test_wrong_context_columns_rejected_against_real_chunk(self):
        self.table_fixture()
        row = to_clinical_sql_row(self.extract_table(), source_language="en")
        note = json.loads(row["taslak_notu"])
        note["table_context"]["columns"][1:3] = list(reversed(note["table_context"]["columns"][1:3]))
        row["taslak_notu"] = json.dumps(note)
        self.assert_rejected(row)

    def test_table_with_empty_first_and_last_cells_keeps_column_count(self):
        from src.clinical_candidates import table_contexts
        ctx = table_contexts("| A | Drug | B |\n|---|---|---|\n|| apixaban 5 mg ||")
        self.assertEqual(ctx["|| apixaban 5 mg ||"]["row_cells"], ["", " apixaban 5 mg ", ""])

    def test_english_result_projection_keeps_original_and_missing_translation(self):
        from src.sql_integration import clinical_result_for_display
        self.table_fixture()
        row = to_clinical_sql_row(self.extract_table(), source_language="en")
        result = clinical_result_for_display(row)
        self.assertEqual(result["original_text"], "bleeding")
        self.assertEqual(result["source_language"], "en")
        self.assertEqual(result["translation_status"], "missing")
        self.assertIsNone(result["translation_text"])
        self.assertIsNone(result["translation_model"])
        self.assertEqual(result["review_status"], "pending")
        self.assertIsNone(row["sonuc_olcutu_tr"])

    def test_legacy_result_notes_are_readable_without_schema_change(self):
        from src.sql_integration import clinical_result_for_display
        for key in ("extracted_fields", "untranslated_fields"):
            row = {"taslak_notu": json.dumps({"source_language": "en", key: {"clinical_outcome": "bleeding"}})}
            self.assertEqual(clinical_result_for_display(row)["original_text"], "bleeding")
            self.assertEqual(clinical_result_for_display(row)["translation_status"], "missing")

    def test_turkish_outcome_adapter_behavior_is_preserved(self):
        from src.sql_integration import clinical_result_for_display
        self.table_fixture()
        # Language metadata is supplied, never automatically inferred from text.
        self.table = self.table.replace("bleeding", "kanama")
        self.candidate = collect_candidates([("fixture.pdf", "article", "doc", "=== Sayfa 2 ===\n" + self.table)])[0]
        self.fields.update(clinical_outcome="kanama", evidence_quote=self.candidate["cumle"])
        self.client.chat.completions.create.return_value.choices[0].message.content = json.dumps({"findings": [self.fields]})
        row = to_clinical_sql_row(self.extract_table(), source_language="tr")
        self.assertEqual(row["sonuc_olcutu_tr"], "kanama")
        result = clinical_result_for_display(row)
        self.assertEqual((result["source_language"], result["translation_status"]), ("tr", "not_required"))

    def test_missing_translation_is_not_displayed_as_completed(self):
        from src.sql_integration import clinical_result_for_display
        from src.clinical_extraction import clinical_result_payload
        payload = clinical_result_payload("bleeding", "en")
        payload.update(translation_status="translated", translation_text=" ")
        result = clinical_result_for_display({"taslak_notu": json.dumps({"clinical_result": payload})})
        self.assertEqual(result["translation_status"], "missing")
        self.assertIsNone(result["translation_text"])

    def test_explicit_stored_translation_retains_model_without_approval(self):
        from src.sql_integration import clinical_result_for_display
        from src.clinical_extraction import clinical_result_payload
        payload = clinical_result_payload("bleeding", "en")
        payload.update(translation_status="translated", translation_text="kanama", translation_model="mock-model")
        result = clinical_result_for_display({"taslak_notu": json.dumps({"clinical_result": payload}),
                                              "review_status": "pending"})
        self.assertEqual((result["original_text"], result["translation_text"]), ("bleeding", "kanama"))
        self.assertEqual(result["translation_model"], "mock-model")
        self.assertEqual(result["review_status"], "pending")

    def test_unknown_language_and_absent_result_are_not_inferred(self):
        from src.sql_integration import clinical_result_for_display
        result = clinical_result_for_display({"taslak_notu": json.dumps({
            "extracted_fields": {"clinical_outcome": "bleeding"}})})
        self.assertIsNone(result["source_language"])
        self.assertEqual(result["translation_status"], "missing")
        self.assertEqual(clinical_result_for_display({})["translation_status"], "not_applicable")

    def test_versioned_result_cannot_hide_unsupported_original_text(self):
        self.table_fixture()
        row = to_clinical_sql_row(self.extract_table(), source_language="en")
        note = json.loads(row["taslak_notu"])
        note["clinical_result"]["original_text"] = "invented clinical benefit"
        row["taslak_notu"] = json.dumps(note)
        self.assert_rejected(row)

    def test_legacy_table_without_context_is_not_automatically_approved(self):
        from src.clinical_review import build_review_template
        self.table_fixture()
        del self.candidate["table_context"]
        self.candidate = bind_fixture(self.candidate)
        with self.assertRaises(ClinicalReviewError):
            build_review_template([self.candidate])

    def test_mixed_candidate_export_serializes_context_as_json_without_files(self):
        import csv
        import io
        from unittest.mock import MagicMock
        from src.clinical_candidates import write_csv
        self.table_fixture()
        path = MagicMock()
        buffer = io.StringIO()
        path.open.return_value.__enter__.return_value = buffer
        prose = {k: v for k, v in self.candidate.items() if k != "table_context"}
        write_csv([prose, self.candidate], path=path)
        records = list(csv.DictReader(io.StringIO(buffer.getvalue()), delimiter=";"))
        self.assertEqual(records[0]["table_context"], "")
        self.assertEqual(json.loads(records[1]["table_context"]), self.candidate["table_context"])


class DiagnosticMethodTests(unittest.TestCase):
    """Synthetic P01 replay only. No real pilot approval, API or database IO."""
    setUp = ClinicalSourceValidationTests.setUp
    save = ClinicalSourceValidationTests.save
    assert_rejected = ClinicalSourceValidationTests.assert_rejected
    quote = ("AF can be detected incidentally during clinical encounters, with wearable devices, "
             "or through interrogation of cardiac implanted electronic devices.")

    def prepare(self, quote=None):
        quote = quote or type(self).quote
        self.ctx.pages["doc"][2] = quote
        self.ctx.chunks[("doc", 2)] = [("chunk", quote)]
        self.row.update(kanit_cumle=quote, mudahale_turu="tan\u0131 testi", tani_en="AF",
                        mudahale_en="wearable devices")
        self.candidate = bind_fixture(dict(dosya="fixture.pdf",makale_id="article",document_id="doc",
            sayfa=2,sayfa_bitis="",birlestirme="",kaynak_turu="metin",cumle=quote,chunk_id="chunk"))
        self.reviews = mock_approved_reviews([self.candidate], mudahale_turu="tan\u0131 testi")

    def finding(self, method="wearable devices", **changes):
        fields = {key:None for key in FIELDS}
        fields.update(diagnosis="AF", diagnostic_method=method,evidence_quote=self.candidate["cumle"])
        fields.update(changes)
        return fields

    def extract(self, payload):
        from src.clinical_extraction import extract_clinical_findings
        client=Mock(api_key="test-only-key");client.with_options.return_value=client
        client.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",message=SimpleNamespace(content=json.dumps({"findings":payload})))])
        self.client=client
        return extract_clinical_findings([self.candidate],client=client,model="mock-model",
                                         reviewed_fields_by_candidate=self.reviews)

    def test_two_p01_methods_are_separate_pending_findings(self):
        from src.clinical_validation import assess_clinical_relationships
        self.prepare()
        methods=["wearable devices","interrogation of cardiac implanted electronic devices"]
        result=self.extract([self.finding(m) for m in methods])
        self.assertEqual([f["diagnostic_method"] for f in result["findings"]],methods)
        rows=[to_clinical_sql_row(f,source_language="en") for f in result["findings"]]
        self.assertEqual(database.save_clinical_findings(rows,"rag",connection=self.conn)["yazilan"],2)
        for row,method in zip(rows,methods):
            self.assertEqual(assess_clinical_relationships(self.finding(method),self.candidate["cumle"]),"supported_local")
            self.assertEqual(row["mudahale_en"],method)
            self.assertEqual(row["mudahale_turu"],"tan\u0131 testi")
            self.assertEqual(row["review_status"],"pending")
            self.assertIsNone(row["sonuc_olcutu_tr"])
            note=json.loads(row["taslak_notu"])
            self.assertEqual(note["extracted_fields"]["diagnostic_method"],method)
            self.assertIsNone(note["extracted_fields"]["drug"])
            self.assertEqual(note["extraction_schema_version"],"clinical-extraction-v2")

    def test_direct_sql_without_json_requires_explicit_diagnostic_link(self):
        self.prepare()
        self.assertEqual(self.save(self.row)["yazilan"],1)

    def test_wrong_disease_method_link_rejected_by_direct_writer(self):
        self.prepare(type(self).quote+" MI is discussed separately.")
        self.assert_rejected({**self.row,"tani_en":"MI"})

    def test_cooccurrence_is_not_a_diagnostic_link(self):
        self.prepare("AF and wearable devices were discussed.")
        self.assert_rejected(self.row)

    def test_negated_diagnostic_link_is_not_accepted(self):
        self.prepare("AF cannot be detected with wearable devices.")
        self.assert_rejected(self.row)

    def test_different_disease_in_next_clause_cannot_donate_method(self):
        self.prepare("AF was discussed. MI can be detected with wearable devices.")
        self.assert_rejected(self.row)

    def test_drug_and_method_cannot_share_one_finding(self):
        from src.clinical_extraction import _validate_response
        self.prepare(type(self).quote+" Apixaban 5 mg was administered.")
        with self.assertRaisesRegex(ValueError,"diagnostic_relationship_contradicted"):
            _validate_response({"findings":[self.finding(drug="Apixaban")]},self.candidate["cumle"])

    def test_unrelated_dose_rejected_even_if_present_in_chunk(self):
        self.prepare(type(self).quote+" Apixaban 5 mg was administered.")
        self.assert_rejected({**self.row,"doz":"5 mg"})

    def test_diagnostic_method_cannot_add_unrelated_clinical_benefit(self):
        self.prepare(type(self).quote+" Treatment reduced mortality.")
        self.assert_rejected({**self.row,"sonuc_olcutu_tr":"mortality"})

    def test_combined_method_list_needs_separate_findings(self):
        self.prepare()
        result=self.extract([self.finding("wearable devices, or through interrogation of cardiac implanted electronic devices")])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["findings"],[])

    def test_clinical_encounters_does_not_become_a_named_test(self):
        self.prepare()
        result=self.extract([self.finding("clinical encounters")])
        self.assertTrue(result["stopped"])

    def test_invented_device_rejected_by_substring_check(self):
        self.prepare()
        self.assertTrue(self.extract([self.finding("12-lead ECG")])["stopped"])

    def test_incorrect_human_classification_is_not_overridden(self):
        self.prepare()
        self.reviews=mock_approved_reviews([self.candidate],mudahale_turu="tedavi")
        finding=self.extract([self.finding()])["findings"][0]
        with self.assertRaises(ValueError):to_clinical_sql_row(finding,source_language="en")
        self.cursor.executemany.assert_not_called()

    def test_direct_writer_cannot_hide_diagnostic_field_under_treatment(self):
        self.prepare()
        row=to_clinical_sql_row(self.extract([self.finding()])["findings"][0],source_language="en")
        row["mudahale_turu"]="tedavi"
        self.assert_rejected(row)

    def test_direct_writer_checks_json_method_against_sql_method(self):
        self.prepare()
        row=to_clinical_sql_row(self.extract([self.finding()])["findings"][0],source_language="en")
        row["mudahale_en"]="interrogation of cardiac implanted electronic devices"
        self.assert_rejected(row)

    def test_unknown_or_turkish_language_not_mislabelled_english(self):
        self.prepare()
        finding=self.extract([self.finding()])["findings"][0]
        for language in (None,"tr"):
            with self.subTest(language=language),self.assertRaises(ValueError):
                to_clinical_sql_row(finding,source_language=language)

    def test_legacy_misused_outcome_is_not_converted_or_approved(self):
        from src.clinical_extraction import _validate_response
        self.prepare()
        old=self.finding(None,clinical_outcome=type(self).quote[3:-1])
        del old["diagnostic_method"]
        original=copy.deepcopy(old)
        normalized=_validate_response({"findings":[old]},type(self).quote)[0]
        self.assertIsNone(normalized["diagnostic_method"])
        self.assertEqual(old,original)
        finding=self.extract([old])["findings"][0]
        row=to_clinical_sql_row(finding,source_language="en")
        self.assertIsNone(row["mudahale_en"])
        self.assert_rejected(row)

    def test_legacy_medication_schema_remains_compatible(self):
        from src.clinical_extraction import _validate_response
        self.prepare("Patients received apixaban 5 mg.")
        old=self.finding(None,diagnosis=None,drug="apixaban",dose="5",dose_unit="mg")
        del old["diagnostic_method"]
        parsed=_validate_response({"findings":[old]},self.candidate["cumle"])[0]
        self.assertEqual(parsed["drug"],"apixaban")
        self.assertIsNone(parsed["diagnostic_method"])

    def test_alternative_explicit_detection_grammar(self):
        self.prepare("ECG detects AF.")
        self.assertEqual(self.save({**self.row,"mudahale_en":"ECG"})["yazilan"],1)

    def test_detection_description_cannot_fill_outcome_in_new_schema(self):
        self.prepare()
        result=self.extract([self.finding(clinical_outcome=type(self).quote[3:-1])])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["findings"],[])

    def test_direct_writer_cannot_treat_single_clause_detection_as_outcome(self):
        self.prepare("AF can be detected with ECG.")
        row={**self.row,"mudahale_en":None,"sonuc_olcutu_tr":"AF can be detected with ECG"}
        self.assert_rejected(row)

    def test_draft_adapter_also_preserves_diagnostic_method(self):
        from src.clinical_extraction import to_clinical_draft_row
        self.prepare()
        finding=self.extract([self.finding()])["findings"][0]
        row=to_clinical_draft_row(finding,source_language="en")
        self.assertEqual(row["mudahale_en"],"wearable devices")
        self.assertEqual(row["review_status"],"pending")
        self.assertEqual(json.loads(row["taslak_notu"])["extracted_fields"]["diagnostic_method"],"wearable devices")

    def test_new_request_schema_names_method_without_relaxing_legacy_keys(self):
        from src.clinical_extraction import FINDING_SCHEMA,EXTRACTION_SCHEMA_VERSION,_validate_response
        self.assertEqual(EXTRACTION_SCHEMA_VERSION,"clinical-extraction-v2")
        self.assertIn("diagnostic_method",FINDING_SCHEMA["required"])
        self.assertFalse(FINDING_SCHEMA["additionalProperties"])
        self.prepare()
        invalid=self.finding();del invalid["drug"]
        with self.assertRaises(ValueError):_validate_response({"findings":[invalid]},self.candidate["cumle"])




class SafeRAGPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.result = {"query_id": str(uuid.uuid4()), "question": "Q", "answer": "A [1]",
            "embedding_model": "minilm", "llm_model": "gpt-6-luna", "response_time_seconds": 1.234,
            "evidence_check": {"sufficient": True}, "citation_check": {"claims_verified": False},
            "sources": [{"chunk_id": "a", "document_id": "doc", "page_number": 2}],
            "retrieved_count": 2, "debug": {"retrieved_hits": [
                {"chunk_id": "a", "rrf_score": .02}, {"chunk_id": "b"}],
                "reranked_hits": [{"chunk_id": "b", "rerank_score": 3.},
                                  {"chunk_id": "a", "rerank_score": -11.}]}}
        self.conn = Mock(autocommit=False, in_transaction=False)
        self.cur = self.conn.cursor.return_value
        self.cur.fetchone.return_value = None
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No SQL")))
        self.enterContext(patch("src.database._existing_ids", return_value={"a", "b"}))

    def save(self):
        return database.save_rag_result(self.result, "Q", connection=self.conn)

    def test_invalid_times_rejected_before_connection(self):
        for value in (None, -1, float("nan"), float("inf"), True, "1", 1e20):
            with self.subTest(value=value):
                self.result["response_time_seconds"] = value
                with self.assertRaises(ValueError): self.save()
        self.conn.cursor.assert_not_called()

    def test_missing_identity_rejected(self):
        self.result.pop("query_id")
        with self.assertRaises(ValueError): self.save()
        self.conn.cursor.assert_not_called()

    def test_incomplete_rank_rejected(self):
        for value in (None, [], [{"chunk_id": "a"}], [{"chunk_id": "a"}, {"chunk_id": "a"}]):
            self.result["debug"]["reranked_hits"] = value
            with self.assertRaises(ValueError): self.save()
        self.conn.cursor.assert_not_called()

    def test_valid_rank_scores_identity_and_citations_preserved(self):
        original = copy.deepcopy(self.result)
        with patch("src.database._validate_rag_sources"):
            self.assertEqual(self.save(), original["query_id"])
        query = next(c.args[1] for c in self.cur.execute.call_args_list if "INSERT INTO rag_sorgulari" in c.args[0])
        self.assertEqual(query["latency_ms"], 1234)
        self.assertEqual(json.loads(query["citation_check"]), original["citation_check"])
        rows = self.cur.executemany.call_args.args[1]
        self.assertEqual(rows[1], (original["query_id"], "a", .02, -11., 2, True))
        self.assertEqual(self.result, original)
        self.conn.commit.assert_not_called()

    def test_duplicate_id_rejected_with_rollback(self):
        self.cur.fetchone.return_value = (self.result["query_id"],)
        with patch("src.database._validate_rag_sources"), self.assertRaisesRegex(ValueError, "Duplicate"):
            self.save()
        self.cur.executemany.assert_not_called()
        self.assertTrue(any("ROLLBACK TO SAVEPOINT" in c.args[0] for c in self.cur.execute.call_args_list))

    def test_partial_insert_failure_rolled_back(self):
        self.cur.executemany.side_effect = RuntimeError("mock failure")
        with patch("src.database._validate_rag_sources"), self.assertRaises(RuntimeError): self.save()
        self.assertTrue(any("ROLLBACK TO SAVEPOINT" in c.args[0] for c in self.cur.execute.call_args_list))

    def test_active_hash_and_metadata_validation(self):
        digest = hashlib.sha256(b"text").hexdigest()
        valid = ["doc", 2, "v1", "text", digest, "v1", "tamamlandi", "a.pdf"]
        self.cur.fetchone.return_value = tuple(valid)
        database._validate_rag_sources(self.conn, self.result, database._rag_source_rows(self.result))
        for index, bad in ((0, "wrong-doc"), (1, 4), (2, "old"), (4, "wrong-hash")):
            row = valid.copy(); row[index] = bad; self.cur.fetchone.return_value = tuple(row)
            with self.assertRaises(ValueError):
                database._validate_rag_sources(self.conn, self.result, database._rag_source_rows(self.result))
        self.cur.fetchone.return_value = None
        with self.assertRaises(ValueError):
            database._validate_rag_sources(self.conn, self.result, database._rag_source_rows(self.result))

    def test_source_failure_prevents_insert(self):
        with patch("src.database._validate_rag_sources", side_effect=ValueError("stale")), self.assertRaises(ValueError):
            self.save()
        self.assertFalse(any("INSERT" in c.args[0] for c in self.cur.execute.call_args_list))

    def test_archive_preserves_result_and_redacts_secrets_without_overwrite(self):
        self.result.update(client=object(), api_key="secret-value")
        self.result["debug"]["authorization"] = "secret-value"
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"LLM_API_KEY": "secret-value"}):
            path = Path(directory) / "run.json"
            database.archive_rag_result(self.result, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["debug"]["reranked_hits"], self.result["debug"]["reranked_hits"])
            self.assertEqual(loaded["query_id"], self.result["query_id"])
            self.assertNotIn("secret-value", path.read_text())
            self.assertNotIn("client", loaded)
            with self.assertRaises(FileExistsError): database.archive_rag_result(self.result, path)

    def test_owned_transaction_rolls_back_on_write_or_commit_failure(self):
        for stage in ("write", "commit"):
            with self.subTest(stage=stage):
                conn = Mock(autocommit=False, in_transaction=False)
                cur = conn.cursor.return_value
                cur.fetchone.return_value = None
                if stage == "write":
                    cur.executemany.side_effect = RuntimeError("mock")
                else:
                    conn.commit.side_effect = RuntimeError("mock")
                with patch("src.database.get_connection", return_value=conn), \
                     patch("src.database._validate_rag_sources"), self.assertRaises(RuntimeError):
                    database.save_rag_result(self.result, "Q")
                conn.start_transaction.assert_called_once()
                conn.rollback.assert_called_once()
                conn.close.assert_called_once()

    def test_archive_loaded_result_keeps_identity_and_real_rank(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            database.archive_rag_result(self.result, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(database._rag_identity_and_latency(loaded),
                             (self.result["query_id"], 1234))
            self.assertEqual(database._rag_source_rows(loaded)[1]["source_rank"], 2)

    def test_archive_can_feed_writer_without_model_configuration(self):
        self.result["status"] = "generated"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            database.archive_rag_result(self.result, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        with patch("src.database._validate_rag_sources"):
            database.save_rag_result(loaded, loaded["question"], connection=self.conn)
        query = next(c.args[1] for c in self.cur.execute.call_args_list if "INSERT INTO rag_sorgulari" in c.args[0])
        self.assertEqual(query["llm_model"], "gpt-6-luna")
        self.assertEqual(query["latency_ms"], 1234)

    def test_non_generated_result_does_not_attribute_configured_model(self):
        self.result.update(status="insufficient_evidence", llm_enabled=True)
        with patch("src.database._validate_rag_sources"):
            self.save()
        query = next(c.args[1] for c in self.cur.execute.call_args_list if "INSERT INTO rag_sorgulari" in c.args[0])
        self.assertIsNone(query["llm_model"])

    def test_invalid_json_number_rejected_before_sql_connection(self):
        self.result["citation_check"]["invalid_number"] = float("nan")
        with self.assertRaises(ValueError):
            self.save()
        self.conn.cursor.assert_not_called()

    def test_archive_rejects_unknown_objects_before_file_creation(self):
        self.result["debug"]["unexpected"] = object()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            with self.assertRaises(ValueError): database.archive_rag_result(self.result, path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
