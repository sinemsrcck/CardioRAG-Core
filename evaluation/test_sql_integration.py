"""Current main SQL writers with mocked connections; no network/model calls."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from src import database
from src.clinical_candidates import collect_candidates
from src.clinical_extraction import FIELDS
from src.clinical_validation import ValidationContext
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
        self.ctx = ValidationContext(pages={"doc1": {1: self.text}},
                                     makale_by_doc={"doc1": "article1"},
                                     chunks={("doc1", 1): [("new:chunk", self.text)]},
                                     chunk_ids={"new:chunk"})
        self.conn = Mock(autocommit=False, in_transaction=False)
        self.cursor = self.conn.cursor.return_value
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = (1,)
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No real MySQL")))
        self.enterContext(patch("src.database._column_max_lengths", return_value={}))
        self.enterContext(patch("src.clinical_validation.load_validation_context", return_value=self.ctx))
        self.client = Mock()
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"findings": [self.finding]})))])

    def run_clinical(self, *, connection=True, reviews=True, query_id=None):
        kwargs = {"client": self.client, "model": "mock-llm", "source_language": "en",
                  "query_id": query_id}
        if reviews:
            kwargs["reviewed_fields_by_candidate"] = {self.candidate["aday_id"]: {"mudahale_turu": "tedavi"}}
        if connection:
            kwargs["connection"] = self.conn
        return extract_and_save_clinical_findings(self.articles, **kwargs)

    def rag_result(self, status="generated"):
        sufficient = status != "insufficient_evidence"
        hit = {"chunk_id": "new:chunk", "rrf_score": 0.02, "rerank_score": -1.0}
        return {"question": "Question?", "answer": None if status == "retrieval_only" else "Answer",
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
        result = self.run_clinical(reviews=False)
        self.assertEqual(result["sql_result"]["yazilan"], 0)
        self.assertEqual(result["sql_result"]["reddedilen"][0]["hata_turu"], "deger_kumesi_disi")
        self.cursor.executemany.assert_not_called()

    def test_rejection_audit_linked_to_query_is_pending(self):
        result = self.run_clinical(reviews=False, query_id="query-id")
        self.assertEqual(result["sql_result"]["halusinasyon_kaydi"], 1)
        self.assertIn("'automated', 'pending'", self.cursor.executemany.call_args.args[0])

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
        row = to_clinical_sql_row({**self.extracted, "makale_id": "wrong-article"})
        with self.assertRaises(ValueError):
            _check_source(row, self.ctx)

    def test_mult_page_metadata_preserved_without_inventing_chunk(self):
        finding = {**self.extracted, "sayfa_bitis": 2, "birlestirme": "sayfa"}
        row = to_clinical_sql_row(finding)
        self.assertEqual(row["kanit_sayfa_bitis"], 2)
        self.assertEqual(row["birlestirme"], "sayfa")
        self.assertIsNone(row["chunk_id"])
        self.assertIsNone(row["mudahale_turu"])
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
                 patch("src.database._existing_ids", return_value={"new:chunk"}):
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
             patch("src.database._existing_ids", return_value={"new:chunk"}):
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
             patch("src.database._existing_ids", return_value={"new:chunk"}):
            result = answer_question_with_sql("atrial fibrillation", connection=self.conn, use_llm=False)
        self.assertEqual(result["status"], "retrieval_only")
        self.assertEqual(result["sources"][0]["chunk_id"], "new:chunk")
        self.assertEqual(result["sql_persistence"]["status"], "pending_caller_commit")


if __name__ == "__main__":
    unittest.main()
