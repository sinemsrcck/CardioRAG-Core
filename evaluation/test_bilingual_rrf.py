"""Pure offline tests: no SQL, models, translation or API calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluation.evaluate_bilingual_rrf import (
    comparison_groups, evaluate_paired, first_rank, fuse_rankings,
    historical_comparison, hits, index_rows, summary, validate_corpus,
    validate_inputs, write_reports,
)


class BilingualRRFTests(unittest.TestCase):
    def test_rrf_uses_ranks_not_incompatible_raw_scores(self):
        result = fuse_rankings(["a", "b"], ["b", "c"])
        self.assertEqual([r["chunk_id"] for r in result], ["b", "a", "c"])
        self.assertAlmostEqual(result[0]["rrf_score"], 1 / 62 + 1 / 61)
        self.assertEqual(result[0]["language_ranks"], {"tr": 2, "en": 1})

    def test_rrf_ties_do_not_depend_on_language_order(self):
        a = fuse_rankings(["z"], ["a"])
        b = fuse_rankings(["a"], ["z"])
        self.assertEqual([r["chunk_id"] for r in a], ["a", "z"])
        self.assertEqual([r["chunk_id"] for r in a], [r["chunk_id"] for r in b])

    def test_duplicate_rank_entries_rejected(self):
        with self.assertRaises(ValueError):
            fuse_rankings(["a", "a"], [])
        with self.assertRaises(ValueError):
            first_rank(["a", "a"], ["a"])

    def test_invalid_config_and_ranks_rejected(self):
        for k in (0, -1, True):
            with self.subTest(k=k), self.assertRaises(ValueError):
                fuse_rankings([], [], k=k)
        for rank in (0, -1, True, 2.5):
            with self.subTest(rank=rank), self.assertRaises(ValueError):
                hits(rank)

    def test_cutoff_boundaries_and_missing_reference(self):
        self.assertEqual(hits(5), {"hit5": True, "hit20": True})
        self.assertEqual(hits(6), {"hit5": False, "hit20": True})
        self.assertEqual(hits(20), {"hit5": False, "hit20": True})
        self.assertEqual(hits(21), {"hit5": False, "hit20": False})
        self.assertEqual(hits(None), {"hit5": False, "hit20": False})

    def test_all_four_language_groups(self):
        rows = [{"question_id": str(i), "tr": hits(t), "en": hits(e)}
                for i, (t, e) in enumerate(((1, 1), (None, 1), (1, None), (None, None)))]
        self.assertEqual(comparison_groups(rows, 5), {
            "both_success": ["0"], "english_only": ["1"],
            "turkish_only": ["2"], "both_failure": ["3"]})
        self.assertEqual(summary(rows, ("tr", "en"))["tr"]["hit_at_5"], 0.5)

    def inputs(self):
        questions = [{"question_id": str(i), "expected_behavior": "answer",
                      "supporting_chunk_ids": ["gold"], "question": "TR",
                      "category": "direct_information", "assigned_pdf": "synthetic.pdf"}
                     for i in range(48)]
        return {"questions": questions}, {"translations": {str(i): "EN" for i in range(48)}}

    def test_same_48_answerable_ids_required(self):
        dataset, translations = self.inputs()
        q, t = validate_inputs(dataset, translations)
        self.assertEqual(len(q), 48)
        del translations["translations"]["0"]
        with self.assertRaises(ValueError):
            validate_inputs(dataset, translations)

    def test_empty_translation_or_reference_not_invented(self):
        for field in ("translation", "reference"):
            d, t = self.inputs()
            if field == "translation":
                t["translations"]["0"] = " "
            else:
                d["questions"][0]["supporting_chunk_ids"] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_inputs(d, t)

    def test_duplicate_question_identity_rejected(self):
        with self.assertRaises(ValueError):
            index_rows([{"question_id": "q"}, {"question_id": "q"}])

    def test_historical_reference_and_translation_mismatch_rejected(self):
        d, t = self.inputs()
        questions, translated = validate_inputs(d, t)
        tr = {"results": [], "hit_at_5_count": 48, "hit_at_20_count": 48}
        en = {"results": [], "reranker_hit5": 48, "reranker_hit20": 48}
        for identity in questions:
            tr["results"].append({"question_id": identity, "expected_chunk_ids": ["gold"],
                "reranked_chunk_ids": ["gold"] + [str(n) + "x" for n in range(19)],
                "hit_at_5": True, "hit_at_20": True})
            en["results"].append({"question_id": identity, "expected_chunk_ids": ["gold"],
                "original_question": "TR", "translated_question": "EN", "reranker_rank": 1,
                "hit5": True, "hit20": True})
        self.assertEqual(historical_comparison(questions, translated, tr, en)["metrics"]["en"]["hit_at_5_count"], 48)
        bad = copy.deepcopy(en)
        bad["results"][0]["translated_question"] = "invented"
        with self.assertRaises(ValueError):
            historical_comparison(questions, translated, tr, bad)
        bad = copy.deepcopy(en)
        bad["results"][0]["expected_chunk_ids"] = ["different"]
        with self.assertRaises(ValueError):
            historical_comparison(questions, translated, tr, bad)

    def test_paired_mock_runs_two_languages_without_llm(self):
        d, t = self.inputs()
        q = {"0": d["questions"][0]}
        search = Mock(side_effect=lambda query, *a, **kw: [
            {"chunk_id": "gold" if query == "EN" else "other", "text": "Synthetic fixture"}])
        rerank = Mock(side_effect=lambda query, candidates, **kw: candidates)
        with patch("socket.create_connection", side_effect=AssertionError("No network")):
            r = evaluate_paired(q, t["translations"], [], search, rerank)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(rerank.call_count, 2)
        self.assertEqual(r["metrics"]["tr"]["hit_at_5_count"], 0)
        self.assertEqual(r["metrics"]["en"]["hit_at_5_count"], 1)
        self.assertEqual(r["metrics"]["hybrid"]["hit_at_5_count"], 1)

    def test_hit_does_not_claim_full_multi_reference_coverage(self):
        d, t = self.inputs()
        d["questions"][0]["supporting_chunk_ids"] = ["gold", "missing"]
        search = lambda *a, **kw: [{"chunk_id": "gold"}]
        rerank = lambda query, candidates, **kw: candidates
        r = evaluate_paired({"0": d["questions"][0]}, t["translations"], [], search, rerank)
        self.assertTrue(r["results"][0]["hybrid"]["hit5"])
        self.assertFalse(r["results"][0]["all_reference_coverage"]["hybrid"]["5"])

    def test_reranker_cannot_add_an_unretrieved_chunk(self):
        d, t = self.inputs()
        with self.assertRaises(ValueError):
            evaluate_paired({"0": d["questions"][0]}, t["translations"], [],
                lambda *a, **kw: [{"chunk_id": "a"}], lambda *a, **kw: [{"chunk_id": "b"}])

    def test_stale_corpus_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_corpus({"corpus": {"active_chunk_count": 242}}, [], {})

    def test_reports_never_overwrite(self):
        metrics = {"tr": {"evaluated": 1, "hit_at_5_count": 0, "hit_at_20_count": 0,
                           "hit_at_5": 0.0, "hit_at_20": 0.0}}
        report = {"historical": {"metrics": metrics, "groups_at_5": {}, "groups_at_20": {}, "results": []},
                  "paired": {"metrics": metrics, "results": []}}
        with tempfile.TemporaryDirectory() as tmp, patch("evaluation.evaluate_bilingual_rrf.datetime") as clock:
            clock.now.return_value.strftime.return_value = "fixed"
            paths = write_reports(report, tmp)
            original = paths[0].read_bytes()
            with self.assertRaises(FileExistsError):
                write_reports(report, tmp)
            self.assertEqual(paths[0].read_bytes(), original)
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
