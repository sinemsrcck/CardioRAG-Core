
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    main()
    unittest.main()
