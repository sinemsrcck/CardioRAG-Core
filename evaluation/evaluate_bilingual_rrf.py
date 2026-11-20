"""Offline TR/EN retrieval comparison. No LLM, translation generation or SQL writes.

Run: python -m evaluation.evaluate_bilingual_rrf
Existing reports are immutable inputs; every invocation creates new report files.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def index_rows(rows):
    result = {}
    for row in rows:
        identity = row["question_id"]
        if identity in result:
            raise ValueError("Duplicate question_id: " + identity)
        result[identity] = row
    return result


def validate_inputs(dataset, translations):
    all_questions = index_rows(dataset["questions"])
    questions = {k: q for k, q in all_questions.items() if q["expected_behavior"] == "answer"}
    if len(questions) != 48:
        raise ValueError("Expected exactly 48 answerable questions")
    translated = translations["translations"]
    if set(translated) != set(questions):
        raise ValueError("Translation/question IDs differ")
    for identity, q in questions.items():
        if not isinstance(translated[identity], str) or not translated[identity].strip():
            raise ValueError("Missing English translation")
        expected = q["supporting_chunk_ids"]
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("Missing or duplicate reference chunks")
    return questions, translated


def first_rank(ranking, expected):
    if len(set(ranking)) != len(ranking):
        raise ValueError("Duplicate chunk in ranking")
    expected = set(expected)
    return next((i for i, cid in enumerate(ranking, 1) if cid in expected), None)


def hits(rank):
    if rank is not None and (type(rank) is not int or rank < 1):
        raise ValueError("Invalid first relevant rank")
    return {"hit5": rank is not None and rank <= 5,
            "hit20": rank is not None and rank <= 20}


def fuse_rankings(tr_ids, en_ids, k=60, limit=20):
    """Equal-weight rank-only RRF; k is a smoothing constant, not a threshold.

    Ties are resolved by chunk ID, independently of language and reference labels.
    """
    if type(k) is not int or k < 1 or type(limit) is not int or limit < 1:
        raise ValueError("Positive integer k and limit required")
    scores, positions = {}, {}
    for language, ranking in (("tr", tr_ids), ("en", en_ids)):
        if len(set(ranking)) != len(ranking):
            raise ValueError("Duplicate chunk in language ranking")
        for rank, cid in enumerate(ranking, 1):
            if not isinstance(cid, str) or not cid:
                raise ValueError("Missing chunk identity")
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            positions.setdefault(cid, {})[language] = rank
    return [{"chunk_id": cid, "rrf_score": scores[cid], "language_ranks": positions[cid]}
            for cid in sorted(scores, key=lambda cid: (-scores[cid], cid))[:limit]]


def summary(rows, names):
    n = len(rows)
    if not n:
        raise ValueError("No evaluated questions")
    return {name: {"evaluated": n,
                   **{f"hit_at_{k}_count": sum(r[name][f"hit{k}"] for r in rows) for k in (5, 20)},
                   **{f"hit_at_{k}": sum(r[name][f"hit{k}"] for r in rows) / n for k in (5, 20)}}
            for name in names}


def comparison_groups(rows, cutoff):
    groups = {key: [] for key in ("both_success", "english_only", "turkish_only", "both_failure")}
    for r in rows:
        tr, en = r["tr"][f"hit{cutoff}"], r["en"][f"hit{cutoff}"]
        key = "both_success" if tr and en else "english_only" if en else "turkish_only" if tr else "both_failure"
        groups[key].append(r["question_id"])
    return groups


def historical_comparison(questions, translated, tr_report, en_report):
    tr, en = index_rows(tr_report["results"]), index_rows(en_report["results"])
    if set(tr) != set(questions) or set(en) != set(questions):
        raise ValueError("Historical reports do not cover the same questions")
    rows = []
    for identity, q in questions.items():
        expected = set(q["supporting_chunk_ids"])
        a, b = tr[identity], en[identity]
        if set(a["expected_chunk_ids"]) != expected or set(b["expected_chunk_ids"]) != expected:
            raise ValueError("Historical reference labels differ")
        if b["original_question"] != q["question"] or b["translated_question"] != translated[identity]:
            raise ValueError("Historical question/translation mismatch")
        if len(a["reranked_chunk_ids"]) != 20:
            raise ValueError("Incomplete historical TR ranking")
        tr_rank = first_rank(a["reranked_chunk_ids"], expected)
        en_rank = b["reranker_rank"]
        th, eh = hits(tr_rank), hits(en_rank)
        if th != {"hit5": a["hit_at_5"], "hit20": a["hit_at_20"]} or eh != {"hit5": b["hit5"], "hit20": b["hit20"]}:
            raise ValueError("Historical flags disagree with recorded ranks")
        rows.append({"question_id": identity, "question_tr": q["question"], "question_en": translated[identity],
                     "category": q["category"], "pdf": q["assigned_pdf"],
                     "tr": {**th, "first_relevant_rank": tr_rank},
                     "en": {**eh, "first_relevant_rank": en_rank}})
    totals = summary(rows, ("tr", "en"))
    if (totals["tr"]["hit_at_5_count"] != tr_report["hit_at_5_count"] or
            totals["tr"]["hit_at_20_count"] != tr_report["hit_at_20_count"] or
            totals["en"]["hit_at_5_count"] != en_report["reranker_hit5"] or
            totals["en"]["hit_at_20_count"] != en_report["reranker_hit20"]):
        raise ValueError("Historical aggregate counts disagree")
    return {"stage": "retrieval_then_reranker", "metrics": totals, "results": rows,
            "groups_at_5": comparison_groups(rows, 5), "groups_at_20": comparison_groups(rows, 20),
            "limitation": "EN full rankings/configuration were not archived; scalar ranks cannot be fused. Live paired evaluation is separate."}


def validate_corpus(dataset, chunks, questions):
    by_id = {c["chunk_id"]: c for c in chunks}
    if len(by_id) != len(chunks) or len(chunks) != dataset["corpus"]["active_chunk_count"]:
        raise ValueError("Active corpus size/identity mismatch")
    manifest = {c["chunk_id"]: c for c in dataset["corpus"]["active_chunk_manifest"]}
    if set(manifest) != set(by_id):
        raise ValueError("Active chunk IDs differ from dataset snapshot")
    for cid, c in by_id.items():
        if hashlib.sha256(c["text"].encode("utf-8")).hexdigest() != c["text_sha256"]:
            raise ValueError("Source text hash mismatch")
        for key in ("document_id", "page_number", "chunk_index", "text_sha256", "processing_version"):
            if c[key] != manifest[cid][key]:
                raise ValueError("Source metadata differs from dataset snapshot")
    for q in questions.values():
        if not set(q["supporting_chunk_ids"]) <= set(by_id):
            raise ValueError("Reference is not an active chunk")
    return by_id


def evaluate_paired(questions, translated, chunks, search, rerank, rrf_k=60, progress=None):
    """Injectable local retrieval functions; no API, guard or answer generation."""
    rows = []
    for identity, q in questions.items():
        row = {"question_id": identity, "category": q["category"], "pdf": q["assigned_pdf"],
               "question_tr": q["question"], "question_en": translated[identity],
               "expected_chunk_ids": q["supporting_chunk_ids"]}
        for lang, query in (("tr", q["question"]), ("en", translated[identity])):
            retrieved = search(query, chunks, model_name="minilm", candidate_k=20, top_k=20)
            ranked = rerank(query, retrieved, model_name="minilm", top_k=len(retrieved))
            ids = [h["chunk_id"] for h in ranked]
            retrieved_ids = [h["chunk_id"] for h in retrieved]
            if len(ids) != len(set(ids)) or set(ids) != set(retrieved_ids):
                raise ValueError("Reranker must preserve the retrieved candidate set")
            rank = first_rank(ids, q["supporting_chunk_ids"])
            row[lang] = {**hits(rank), "first_relevant_rank": rank,
                         "ranked_chunk_ids": ids, "retrieval_chunk_ids": retrieved_ids,
                         "retrieval_hits": hits(first_rank(retrieved_ids, q["supporting_chunk_ids"])),
                         "scores": [{"chunk_id": h["chunk_id"], "rrf_score": h.get("rrf_score"),
                                     "rerank_score": h.get("rerank_score")} for h in ranked]}
        fused = fuse_rankings(row["tr"]["ranked_chunk_ids"], row["en"]["ranked_chunk_ids"], rrf_k)
        ids = [h["chunk_id"] for h in fused]
        rank = first_rank(ids, q["supporting_chunk_ids"])
        row["hybrid"] = {**hits(rank), "first_relevant_rank": rank, "ranked_chunk_ids": ids, "scores": fused}
        # Any-reference Hit is not full evidence coverage for multi-chunk answers.
        row["all_reference_coverage"] = {lang: {str(k): set(q["supporting_chunk_ids"]) <= set(row[lang]["ranked_chunk_ids"][:k])
                                                for k in (5, 20)} for lang in ("tr", "en", "hybrid")}
        rows.append(row)
        if progress:
            progress(identity, len(rows), len(questions))
    return {"metrics": summary(rows, ("tr", "en", "hybrid")), "results": rows,
            "groups_at_5": comparison_groups(rows, 5), "groups_at_20": comparison_groups(rows, 20),
            "by_pdf": {pdf: summary([r for r in rows if r["pdf"] == pdf], ("tr", "en", "hybrid"))
                       for pdf in sorted({r["pdf"] for r in rows})}}


def markdown(report):
    lines = ["# TR / EN / Hybrid RRF değerlendirmesi", "",
             "Provisional kaynak etiketleriyle geliştirme pilotu; bağımsız test veya klinik doğrulama değildir.", "",
             "Pozitif: beklenen chunk kimliklerinden en az birinin ilk k sonuçta bulunması. Payda: 48 answerable soru.",
             "Çoklu kaynak isteyen sorularda Hit, tam yanıt kanıt kapsamı anlamına gelmez.", "",
             "## Önceden kaydedilmiş TR / EN sonuçları", ""]
    def metrics_table(metrics):
        lines.extend(["| Yöntem | Hit@5 | Hit@20 |", "|---|---:|---:|"])
        for name, m in metrics.items():
            lines.append(f"| {name.upper()} | {m['hit_at_5_count']}/{m['evaluated']} ({m['hit_at_5']:.2%}) | {m['hit_at_20_count']}/{m['evaluated']} ({m['hit_at_20']:.2%}) |")
    metrics_table(report["historical"]["metrics"])
    for k in (5, 20):
        lines.extend(["", f"### Kayıtlı sonuç grupları — Hit@{k}", ""])
        for group, ids in report["historical"][f"groups_at_{k}"].items():
            lines.append(f"- {group} ({len(ids)}): " + (", ".join(ids) or "Yok"))
    lines.extend(["", "EN raporunda tam sıralamalar bulunmadığından tarihsel sıralamalar RRF için tahmin edilmedi.",
                  "", "## Aynı koşullarda yeniden ölçülen TR / EN / Hybrid", "",
                  "MiniLM embedding + MiniLM cross-encoder; dil başına 20 aday. Hybrid: iki reranker sırasının eşit ağırlıklı RRF birleşimi, k=60; yeniden rerank yapılmadı.",
                  "TR kolu mevcut kontrollü rewrite politikasını korur; saf, değiştirilmemiş Türkçe sorgu ölçümü değildir.", ""])
    metrics_table(report["paired"]["metrics"])
    lines.extend(["", "## Soru bazında karşılaştırma", "",
                  "Hücreler Hit@5 / Hit@20 biçimindedir; H=başarılı, —=başarısız.", "",
                  "| ID | Türkçe soru | Kayıtlı TR | Kayıtlı EN | Yeni TR | Yeni EN | Hybrid |", "|---|---|---|---|---|---|---|"])
    old = index_rows(report["historical"]["results"])
    def cell(v):
        return ("H" if v["hit5"] else "—") + " / " + ("H" if v["hit20"] else "—")
    for row in report["paired"]["results"]:
        identity = row["question_id"]
        cells = [cell(old[identity][l]) for l in ("tr", "en")] + [cell(row[l]) for l in ("tr", "en", "hybrid")]
        lines.append(f"| {identity} | {row['question_tr'].replace('|', '/')} | " + " | ".join(cells) + " |")
    lines.extend(["", "## Sınırlar", "", "- Etiketler otomatik kaynak bağlı taslaktır; araştırmacı onayı yoktur.",
                  "- Tarihsel raporların tüm çalışma parametreleri kaydedilmemiştir; yeni ölçüm ayrı tutulur.",
                  "- RRF, bir dilin güçlü tekil sonucunu aşağı taşıyabilir; ölçülmeden iyileşme varsayılmaz.",
                  "- Üretim retrieval kuralları değiştirilmedi. LLM, yeni çeviri veya SQL yazımı yapılmadı."])
    return "\n".join(lines) + "\n"


def write_reports(report, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "rag60_bilingual_rrf_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    paths = [output_dir / (stem + extension) for extension in (".json", ".md")]
    if any(p.exists() for p in paths):
        raise FileExistsError("Report collision; refusing to overwrite")
    with paths[0].open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    with paths[1].open("x", encoding="utf-8") as f:
        f.write(markdown(report))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evaluation/reports")
    args = parser.parse_args(argv)
    paths = {"dataset": ROOT / "evaluation/rag_evaluation_questions_v1.json",
             "translations": ROOT / "evaluation/reports/rag60_api_translations.json",
             "tr": ROOT / "evaluation/reports/rag60_true_retrieval_results.json",
             "en": ROOT / "evaluation/reports/rag60_api_translation_retrieval_results.json"}
    hashes = {name: digest(path) for name, path in paths.items()}
    dataset, translation_file = read_json(paths["dataset"]), read_json(paths["translations"])
    questions, translated = validate_inputs(dataset, translation_file)
    historical = historical_comparison(questions, translated, read_json(paths["tr"]), read_json(paths["en"]))
    # No model downloads or remote inference, including implicit Hugging Face access.
    import os
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from src.database import get_connection, load_chunks_for_rag
    from src.retrieval import hybrid_search
    from src.reranker import rerank
    from src.query_normalization import QUERY_POLICIES, retrieval_queries, reranking_query
    conn = get_connection()
    try:
        conn.start_transaction(readonly=True)
        chunks = load_chunks_for_rag("minilm", connection=conn)
    finally:
        conn.rollback()
        conn.close()
    validate_corpus(dataset, chunks, questions)
    paired = evaluate_paired(questions, translated, chunks, hybrid_search, rerank,
                             progress=lambda q, n, total: print(f"{n}/{total} {q}", flush=True))
    for row in paired["results"]:
        row["effective_queries"] = {l: {"retrieval": retrieval_queries(row["question_" + l], "minilm"),
                                           "reranker": reranking_query(row["question_" + l], "minilm")}
                                    for l in ("tr", "en")}
    if hashes != {name: digest(path) for name, path in paths.items()}:
        raise ValueError("Evaluation input changed during run")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "label_status": "provisional",
              "evaluation_scope": "source-grounded development pilot, not independent or expert validation",
              "metric_definition": "Hit@k: any labelled supporting chunk in top k; denominator 48 answerable questions",
              "config": {"embedding_model": "minilm", "reranker_model": "minilm", "candidate_k_per_language": 20,
                         "fusion": "equal-weight RRF of language-specific reranked lists", "rrf_k": 60,
                         "hybrid_top_k": 20, "query_policies": dict(QUERY_POLICIES),
                         "guard_applied": False, "llm_calls": 0, "translation_calls": 0, "sql_writes": 0},
              "corpus_active_chunks": len(chunks), "input_sha256": hashes,
              "historical": historical, "paired": paired}
    out = write_reports(report, args.output_dir)
    print(json.dumps({"reports": [str(p) for p in out], "historical": historical["metrics"],
                      "paired": paired["metrics"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
