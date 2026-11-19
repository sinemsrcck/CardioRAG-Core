"""Offline RAG/archive/SQL-bundle checks. Synthetic fixtures, never real results."""
import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from src import database
from src.rag_persistence import evaluation_rows, fingerprint, save_rag_bundle, VERSION
from src.run_rag import run, main

TEXT = ("AF can be detected incidentally during clinical encounters, with wearable devices, "
        "or through interrogation of cardiac implanted electronic devices.")


def fixture(status="generated", label="supported"):
    answer = TEXT + " [1]"
    source = dict(number=1, filename="synthetic-fixture.pdf", document_id="synthetic-doc",
        chunk_id="synthetic-chunk", page_number=2, source_text=TEXT,
        text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(), processing_version="mock-v1")
    claim_text = TEXT.rstrip(".")
    claim = dict(claim_index=1, claim_text_sha256=hashlib.sha256(claim_text.encode()).hexdigest(),
        citations=[1], status=label, reasons=[] if label == "supported" else ["mock_test_reason"],
        matches=[dict(chunk_id=source["chunk_id"], evidence_quote=TEXT)] if label == "supported" else [])
    return dict(query_id=str(uuid.uuid4()), question="Synthetic AF test question", status=status,
        answer=answer if status == "generated" else None,
        audit={"generated_answer": answer if status not in ("retrieval_only", "insufficient_evidence", "generation_failed") else None},
        sources=[] if status == "insufficient_evidence" else [source],
        citation_check=None if status in ("retrieval_only", "insufficient_evidence", "generation_failed") else {"valid": status != "citation_validation_failed", "used_citations": [1]},
        claim_verification={"scope": "mock source-grounded fixture, not expert approval",
            "claims": [claim] if status in ("generated", "claim_validation_failed") else []},
        claims_verified=status == "generated", response_time_seconds=0.125, retrieved_count=1,
        embedding_model="minilm", reranker_model="minilm", rerank_method="cross_encoder",
        llm_enabled=status != "retrieval_only", llm_model="mock-test-model" if status != "retrieval_only" else None,
        evidence_check={"sufficient": status != "insufficient_evidence", "reason": "mock_test"},
        debug={"retrieved_hits": [{"chunk_id": "synthetic-chunk", "rrf_score": 0.02}],
               "reranked_hits": [{"chunk_id": "synthetic-chunk", "rerank_score": -0.4}]},
        run_provenance={"kind": "mock_test", "real_llm": False})


class RAGBundleTests(unittest.TestCase):
    def setUp(self):
        self.result = fixture()
        self.conn = Mock(autocommit=False, in_transaction=False)
        self.cursor = self.conn.cursor.return_value
        self.sql = ""
        self.old = None
        self.old_rows = []
        self.source_version = "mock-v1"
        self.fail_evaluation = False
        def execute(sql, params=None):
            self.sql = sql
            if self.fail_evaluation and "INSERT INTO halusinasyon_degerlendirme" in sql:
                raise RuntimeError("synthetic database failure")
        self.cursor.execute.side_effect = execute
        def one():
            if "FOR UPDATE" in self.sql:
                return self.old
            if "SELECT c.document_id,c.page_number" in self.sql:
                return ("synthetic-doc", 2, self.source_version, TEXT, hashlib.sha256(TEXT.encode()).hexdigest(),
                        "mock-v1", "tamamlandi", "synthetic-fixture.pdf")
            return None
        self.cursor.fetchone.side_effect = one
        self.cursor.fetchall.side_effect = lambda: self.old_rows if "SELECT ham_cikti" in self.sql else [("synthetic-chunk",)]
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("Real MySQL forbidden")))

    def save(self, result=None):
        return save_rag_bundle(result or self.result, question_type="cevaplanabilir",
                               connection=self.conn, allow_test_data=True)

    def test_supported_claim(self):
        row = evaluation_rows(self.result, "cevaplanabilir")[0]
        self.assertEqual(row["claim_supported"], "supported")
        self.assertEqual(row["supporting_chunk_id"], "synthetic-chunk")
        self.assertEqual(row["review_status"], "pending")

    def test_unsupported_claim_kept_only_in_audit(self):
        r = fixture("claim_validation_failed", "not_supported")
        row = evaluation_rows(r, "cevaplanabilir")[0]
        self.assertEqual(row["claim_supported"], "not_supported")
        self.assertIsNotNone(row["claim_text"])
        self.assertIsNone(r["answer"])

    def test_uncertain_is_sql_null(self):
        row = evaluation_rows(fixture("claim_validation_failed", "uncertain"), "cevaplanabilir")[0]
        self.assertIsNone(row["claim_supported"])
        self.assertEqual(row["hata_turu"], "claim_uncertain")

    def test_partial_maps_without_changing_semantics(self):
        self.assertEqual(evaluation_rows(fixture("claim_validation_failed", "partial"),
                                         "cevaplanabilir")[0]["claim_supported"], "partially_supported")

    def test_gate_citation_and_generation_events_are_distinct(self):
        for status in ("citation_validation_failed", "insufficient_evidence", "generation_failed", "retrieval_only"):
            with self.subTest(status=status):
                row = evaluation_rows(fixture(status), "cevaplanabilir")[0]
                self.assertIsNone(row["claim_supported"])
                self.assertIn(status, row["hata_turu"])

    def test_requires_answerability_not_inferred_from_guard(self):
        with self.assertRaises(ValueError):
            evaluation_rows(self.result, None)

    def test_non_generated_event_does_not_score_abstention_citations(self):
        for status in ("retrieval_only", "insufficient_evidence", "generation_failed"):
            with self.subTest(status=status):
                result = fixture(status)
                result["citation_check"] = {"valid": False}
                row = evaluation_rows(result, "cevaplanamaz")[0]
                self.assertIsNone(row["citation_correct"])
                self.assertIsNone(row["claim_supported"])
                self.assertIsNone(row["claim_text"])
                self.assertEqual(result["citation_check"], {"valid": False})

    def test_unknown_claim_status_rejected(self):
        with self.assertRaises(ValueError):
            evaluation_rows(fixture("claim_validation_failed", "expert_verified"), "cevaplanabilir")

    def test_invalid_match_source_rejected(self):
        self.result["claim_verification"]["claims"][0]["matches"][0]["chunk_id"] = "absent"
        with self.assertRaises(ValueError):
            self.save()
        self.cursor.execute.assert_not_called()

    def test_rejected_user_answer_is_forbidden(self):
        r = fixture("claim_validation_failed", "uncertain")
        r["answer"] = "Unsafe answer"
        with self.assertRaises(ValueError):
            evaluation_rows(r, "cevaplanabilir")

    def test_real_writers_share_transaction(self):
        saved = self.save()
        statements = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO rag_sorgulari" in s for s in statements))
        self.assertIn("INSERT INTO rag_sorgu_kaynaklari", self.cursor.executemany.call_args.args[0])
        self.assertTrue(any("INSERT INTO halusinasyon_degerlendirme" in s for s in statements))
        self.assertIn("RELEASE SAVEPOINT save_rag_bundle", statements)
        self.assertEqual(saved["status"], "pending_caller_commit")
        self.conn.commit.assert_not_called()

    def test_evaluation_failure_rolls_back_all_bundle(self):
        self.fail_evaluation = True
        with self.assertRaises(RuntimeError):
            self.save()
        statements = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertIn("ROLLBACK TO SAVEPOINT save_rag_bundle", statements)
        self.conn.commit.assert_not_called()

    def test_same_uuid_and_content_idempotent(self):
        self.old = (self.result["query_id"],)
        digest = fingerprint({"result": database.public_rag_payload(self.result), "question_type": "cevaplanabilir", "version": VERSION})
        self.old_rows = [(json.dumps({"result_sha256": digest}),)]
        self.assertEqual(self.save()["status"], "already_recorded")
        self.assertFalse(any("INSERT" in c.args[0] for c in self.cursor.execute.call_args_list))

    def test_duplicate_uuid_different_content_rejected(self):
        self.old = (self.result["query_id"],)
        self.old_rows = [(json.dumps({"result_sha256": "different"}),)]
        with self.assertRaises(ValueError):
            self.save()

    def test_stale_source_rejected_before_query_insert(self):
        self.source_version = "old"
        with self.assertRaises(ValueError):
            self.save()
        self.assertFalse(any("INSERT INTO rag_sorgulari" in c.args[0] for c in self.cursor.execute.call_args_list))

    def test_source_text_hash_mismatch_blocks_all_writes(self):
        self.result["sources"][0]["source_text"] += " changed"
        with self.assertRaises(ValueError):
            self.save()
        self.cursor.execute.assert_not_called()

    def test_autocommit_connection_cannot_split_bundle(self):
        self.conn.autocommit = True
        with self.assertRaises(ValueError):
            self.save()
        self.cursor.execute.assert_not_called()

    def test_latency_and_ranking_not_invented(self):
        for key in ("response_time_seconds", "debug"):
            r = copy.deepcopy(self.result)
            del r[key]
            with self.assertRaises(ValueError):
                self.save(r)

    def test_mock_cannot_be_saved_as_real(self):
        with self.assertRaises(ValueError):
            save_rag_bundle(self.result, question_type="cevaplanabilir", connection=self.conn)

    def test_archive_audit_redacts_secrets_and_retains_bindings(self):
        r = fixture("claim_validation_failed", "uncertain")
        r["audit"]["generated_answer"] += " sk-synthetic-secret"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mock.json"
            database.archive_rag_result(r, path)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("sk-synthetic-secret", raw)
            saved = json.loads(raw)
            self.assertIsNone(saved["answer"])
            self.assertEqual(saved["sources"], r["sources"])
            self.assertEqual(saved["run_provenance"]["kind"], "mock_test")
            with self.assertRaises(FileExistsError):
                database.archive_rag_result(r, path)


class RAGEntryTests(unittest.TestCase):
    def test_offline_af_troponin_and_unrelated_question(self):
        troponin = "Delta troponin I change between 1-3 hours was assessed for acute myocardial infarction (AMI). " * 2
        for question, text, expected in (
            ("Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?", TEXT, "retrieval_only"),
            ("Troponin değişimi akut miyokard enfarktüsü tanısında nasıl değerlendirilir?", troponin, "retrieval_only"),
            ("Mars surface temperature?", TEXT, "insufficient_evidence")):
            source = fixture()["sources"][0]
            hit = {**source, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "rrf_score": 0.02, "rerank_score": -0.4}
            with self.subTest(question=question), tempfile.TemporaryDirectory() as tmp, \
                 patch("src.rag.hybrid_search", return_value=[hit]), \
                 patch("src.rag.rerank", return_value=[hit]), \
                 patch("src.rag.get_llm_client", side_effect=AssertionError("No real API")), \
                 patch("src.database.get_connection", side_effect=AssertionError("No real SQL")):
                result, path = run(question, chunks=[hit], archive_dir=tmp,
                                   provenance={"kind": "mock_test", "real_llm": False})
                self.assertEqual(result["status"], expected)
                self.assertNotIn("audit", result)
                self.assertTrue(path.exists())
                self.assertEqual(result["reranker_model"], "minilm")

    def test_rejected_answer_archived_but_not_returned(self):
        source = fixture()["sources"][0]
        hit = {**source, "text": TEXT, "rrf_score": 0.02, "rerank_score": -0.4}
        with tempfile.TemporaryDirectory() as tmp, patch("src.rag.hybrid_search", return_value=[hit]), \
             patch("src.rag.rerank", return_value=[hit]), patch("src.rag.generate_answer", return_value="Unsafe clinical claim [99]."):
            result, path = run("How can atrial fibrillation be detected?", chunks=[hit], archive_dir=tmp,
                               use_llm=True, provenance={"kind": "mock_test", "real_llm": False})
            self.assertEqual(result["status"], "citation_validation_failed")
            self.assertIsNone(result["answer"])
            self.assertNotIn("audit", result)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["audit"]["generated_answer"], "Unsafe clinical claim [99].")

    def test_failed_claim_decisions_preserve_audit_without_user_answer(self):
        source = fixture()["sources"][0]
        hit = {**source, "text": TEXT, "rrf_score": 0.02, "rerank_score": -0.4}
        for label in ("not_supported", "uncertain"):
            verification = {"claims_verified": False, "all_claims_supported": False,
                "scope": "synthetic mocked evaluation", "claims":
                    fixture("claim_validation_failed", label)["claim_verification"]["claims"]}
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp, \
                 patch("src.rag.hybrid_search", return_value=[hit]), \
                 patch("src.rag.rerank", return_value=[hit]), \
                 patch("src.rag.generate_answer", return_value=TEXT + " [1]"), \
                 patch("src.rag.validate_claims", return_value=verification):
                result, path = run("How can atrial fibrillation be detected?", chunks=[hit],
                    archive_dir=tmp, use_llm=True, provenance={"kind": "mock_test", "real_llm": False})
                self.assertEqual(result["status"], "claim_validation_failed")
                self.assertIsNone(result["answer"])
                self.assertNotIn("audit", result)
                saved = json.loads(path.read_text(encoding="utf-8"))
                row = evaluation_rows(saved, "cevaplanabilir")[0]
                self.assertIsNotNone(row["claim_text"])
                self.assertEqual(row["claim_supported"], "not_supported" if label == "not_supported" else None)

    def test_sql_source_loading_uses_readonly_transaction(self):
        response = fixture("retrieval_only")
        conn = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch("src.database.get_connection", return_value=conn), \
             patch("src.database.load_chunks_for_rag", return_value=[]) as load, \
             patch("src.rag.answer_question", return_value=response):
            run("Synthetic offline question", archive_dir=tmp, provenance={"kind": "mock_test"})
        conn.start_transaction.assert_called_once_with(readonly=True)
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        self.assertEqual(load.call_args.args[0], "minilm")

    def test_cli_requires_separate_sql_write_flag(self):
        with self.assertRaises(SystemExit), patch("src.rag_persistence.save_rag_bundle") as save:
            main(["persist", "--archive", "unused.json", "--question-type", "cevaplanabilir"])
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
