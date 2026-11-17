
"""
save_rag_result testi: answer_question sonucu -> rag_sorgulari + rag_sorgu_kaynaklari.

Çalıştırma:
    python -m evaluation.test_rag_kayit

- Gerçek akış: load_chunks_for_rag("minilm") + src.rag.answer_question(use_llm=False).
- Reranker modeli (bge-reranker-v2-m3) bilgisayarda yok ve İNDİRİLMEZ: test süresince
  src.rag.rerank, chunk'ın semantic_score'unu rerank_score olarak veren bir yer
  tutucuyla değiştirilir (yalnızca bellekte; rag.py değişmez). Retrieval, evidence
  guard, kaynak seçimi ve sonuç sözlüğü gerçek koddan gelir.
- Bütün yazımlar tek transaction içinde yapılır ve sonunda GERİ ALINIR;
  tablolarda deneme verisi kalmaz.
"""

import copy
import json
import os
import sys

# Model indirmeyi kesin olarak engelle (önbellekte olmayan model hata verir).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import src.rag as rag  # noqa: E402
from src.database import (  # noqa: E402
    count_rows,
    get_connection,
    load_chunks_for_rag,
    save_rag_result,
)


QUESTION = "Which anticoagulant is recommended for patients with atrial fibrillation?"
LLM_MODEL = "Qwen/Qwen3-8B"
STUB_RERANKER = "test-stub-semantic"
TABLES = ("rag_sorgulari", "rag_sorgu_kaynaklari")


def stub_rerank(question, chunks, model_name=None, top_k=5, batch_size=8):
    """Model yüklemeden rerank: semantic_score'u rerank_score olarak kullanır."""
    ranked = []
    for chunk in chunks:
        item = chunk.copy()
        item["rerank_score"] = float(chunk.get("semantic_score") or 0.0)
        ranked.append(item)
    ranked.sort(key=lambda c: c["rerank_score"], reverse=True)
    return ranked[:top_k]


def counts(cursor):
    return {t: count_rows(cursor, t) for t in TABLES}


def fetch_query(cursor, query_id):
    cursor.execute(
        "SELECT question, answer, llm_model, embedding_model, reranker_model, "
        "evidence_status, evidence_score, guard_reason, latency_ms, rerank_method, citation_check "
        "FROM rag_sorgulari WHERE query_id = %s", (query_id,))
    row = cursor.fetchone()
    keys = ("question", "answer", "llm_model", "embedding_model", "reranker_model",
            "evidence_status", "evidence_score", "guard_reason", "latency_ms",
            "rerank_method", "citation_check")
    return dict(zip(keys, row)) if row else None


def fetch_sources(cursor, query_id):
    cursor.execute(
        "SELECT chunk_id, retrieval_score, rerank_score, source_rank, used_in_context "
        "FROM rag_sorgu_kaynaklari WHERE query_id = %s ORDER BY source_rank", (query_id,))
    return [
        {"chunk_id": bytes(c).decode() if isinstance(c, (bytes, bytearray)) else c,
         "retrieval_score": r, "rerank_score": s, "source_rank": k, "used": bool(u)}
        for c, r, s, k, u in cursor.fetchall()
    ]


def close(a, b, tol=1e-9):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= tol)


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    # Başlangıç sayıları (ayrı bağlantı).
    probe = get_connection()
    cur = probe.cursor()
    initial = counts(cur)
    probe.rollback()
    probe.close()

    # Gerçek akış, yer tutucu reranker ile.
    rag.rerank = stub_rerank
    chunks = load_chunks_for_rag("minilm")
    result = rag.answer_question(QUESTION, chunks, embedding_model="minilm",
                                 reranker_model=STUB_RERANKER, use_llm=False)
    check("answer_question status", "retrieval_only", result["status"])
    candidates = {h["chunk_id"] for h in result["debug"]["retrieved_hits"]} | \
                 {h["chunk_id"] for h in result["debug"]["reranked_hits"]}

    conn = get_connection()  # autocommit kapalı: her şey tek transaction
    cur = conn.cursor()
    try:
        # 1) Olmayan chunk_id -> hata, hiçbir şey yazılmamalı
        bad = copy.deepcopy(result)
        bad["debug"]["retrieved_hits"].append(
            {"chunk_id": "YOK_" + "0" * 60, "document_id": None, "filename": None, "rrf_score": 0.1})
        try:
            save_rag_result(bad, QUESTION, llm_model=LLM_MODEL, connection=conn)
            check("olmayan chunk_id -> ValueError", "ValueError", "hata yok", False)
        except ValueError as exc:
            check("olmayan chunk_id -> ValueError", "ValueError", "ValueError", True)
            check("hata mesajı", "hiçbir şey yazılmadı", str(exc)[:60],
                  "hiçbir şey yazılmadı" in str(exc))
        check("hata sonrası satır sayısı", initial, counts(cur))

        # 2) Gerçek sonuç
        query_id = save_rag_result(result, QUESTION, llm_model=LLM_MODEL, connection=conn)
        after = counts(cur)
        check("rag_sorgulari +1", initial["rag_sorgulari"] + 1, after["rag_sorgulari"])
        check("rag_sorgu_kaynaklari = aday sayısı",
              initial["rag_sorgu_kaynaklari"] + len(candidates), after["rag_sorgu_kaynaklari"])

        q = fetch_query(cur, query_id)
        check("query_id UUID uzunluğu", 36, len(query_id))
        check("answer (LLM kapalı)", None, q["answer"])
        check("llm_model (LLM kapalı)", None, q["llm_model"])
        check("embedding_model", "minilm", q["embedding_model"])
        check("reranker_model", STUB_RERANKER, q["reranker_model"])
        check("evidence_status", "sufficient", q["evidence_status"])
        check("guard_reason", result["evidence_check"]["reason"], q["guard_reason"])
        check("evidence_score = top_rerank_score", result["top_rerank_score"], q["evidence_score"],
              close(result["top_rerank_score"], q["evidence_score"]))
        check("latency_ms", round(result["response_time_seconds"] * 1000), q["latency_ms"])
        check("rerank_method", result["rerank_method"], q["rerank_method"])
        check("citation_check (LLM kapalı)", None, q["citation_check"])

        rows = fetch_sources(cur, query_id)
        used = {r["chunk_id"] for r in rows if r["used"]}
        check("used_in_context=1 olanlar = sources",
              sorted(s["chunk_id"][-8:] for s in result["sources"]),
              sorted(c[-8:] for c in used),
              used == {s["chunk_id"] for s in result["sources"]})
        check("used_in_context=1 sayısı", len(result["sources"]), len(used))

        rrf = {h["chunk_id"]: h["rrf_score"] for h in result["debug"]["retrieved_hits"]}
        rr = {h["chunk_id"]: h["rerank_score"] for h in result["debug"]["reranked_hits"]}
        order = [h["chunk_id"] for h in result["debug"]["reranked_hits"]]
        check("retrieval_score = rrf_score", "hepsi",
              "hepsi" if all(close(r["retrieval_score"], rrf.get(r["chunk_id"])) for r in rows) else "fark var")
        check("rerank_score = rerank_score", "hepsi",
              "hepsi" if all(close(r["rerank_score"], rr.get(r["chunk_id"])) for r in rows) else "fark var")
        check("source_rank = rerank sırası", "hepsi",
              "hepsi" if [r["chunk_id"] for r in rows][:len(order)] == order else "fark var")

        # 3) Yetersiz kanıt yolu (gerçek akış, yüksek eşik)
        weak = rag.answer_question(QUESTION, chunks, embedding_model="minilm",
                                   reranker_model=STUB_RERANKER, use_llm=False,
                                   min_relevance_score=1e9)
        weak_id = save_rag_result(weak, QUESTION, llm_model=LLM_MODEL, connection=conn)
        wq = fetch_query(cur, weak_id)
        check("yetersiz: evidence_status", "insufficient", wq["evidence_status"])
        check("yetersiz: guard_reason", "below_relevance_threshold", wq["guard_reason"])
        check("yetersiz: answer = guard mesajı", weak["answer"], wq["answer"])
        check("yetersiz: used_in_context=1", 0, sum(r["used"] for r in fetch_sources(cur, weak_id)))

        # 4) Tekrarlanan chunk -> tek satır; LLM açık -> llm_model parametreden
        dup = copy.deepcopy(result)
        dup["debug"]["retrieved_hits"] += dup["debug"]["retrieved_hits"][:3]
        dup["debug"]["reranked_hits"] += dup["debug"]["reranked_hits"][:3]
        dup["llm_enabled"] = True
        dup["answer"] = "Örnek yanıt [1]."
        dup["citation_check"] = rag.validate_citations(dup["answer"], dup["sources"])
        dup_id = save_rag_result(dup, QUESTION, llm_model=LLM_MODEL, connection=conn)
        check("tekrarlanan chunk -> tek satır", len(candidates), len(fetch_sources(cur, dup_id)))
        dq = fetch_query(cur, dup_id)
        check("LLM açık: llm_model parametreden", LLM_MODEL, dq["llm_model"])
        stored = json.loads(dq["citation_check"]) if dq["citation_check"] else None
        check("citation_check JSON = sonuç", dup["citation_check"], stored)

        # 5) Yazım ortasında hata -> SAVEPOINT'e dönülmeli (rag_sorgulari satırı da geri alınır)
        before_partial = counts(cur)
        broken = copy.deepcopy(result)
        broken["debug"]["reranked_hits"][0]["rerank_score"] = float("nan")  # 2. INSERT'te hata
        try:
            save_rag_result(broken, QUESTION, llm_model=LLM_MODEL, connection=conn)
            check("yazım ortasında hata", "hata", "hata yok", False)
        except Exception as exc:  # noqa: BLE001 - sürücü hatası beklenir
            check("yazım ortasında hata", "hata", type(exc).__name__, True)
        check("kısmi yazım geri alındı (SAVEPOINT)", before_partial, counts(cur))

        # 6) Autocommit bağlantı reddedilmeli
        auto = get_connection()
        auto.autocommit = True
        try:
            save_rag_result(result, QUESTION, llm_model=LLM_MODEL, connection=auto)
            check("autocommit bağlantı reddi", "ValueError", "hata yok", False)
        except ValueError:
            check("autocommit bağlantı reddi", "ValueError", "ValueError", True)
        finally:
            auto.close()

    finally:
        conn.rollback()  # deneme verisi kalmasın
        cur.close()
        conn.close()

    probe = get_connection()
    cur = probe.cursor()
    final = counts(cur)
    probe.rollback()
    probe.close()
    check("rollback sonrası satır sayıları = başlangıç", initial, final)

    width = max(len(r[0]) for r in results)
    print(f"\nreranker: yer tutucu ({STUB_RERANKER}); retrieval ve answer_question gerçek")
    print(f"{'Kontrol':<{width}}  {'Beklenen':<28}  {'Bulunan':<28}  Sonuç")
    print("-" * (width + 70))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:28]:<28}  {act[:28]:<28}  {'OK' if passed else 'HATA'}")

    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
