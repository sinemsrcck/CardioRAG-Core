
"""
klinik_bulgular yazma / denetim / VIEW testi.

Çalıştırma:
    python -m evaluation.test_klinik_bulgular

Bütün yazımlar tek transaction içinde yapılır ve sonunda GERİ ALINIR:
klinik_bulgular, halusinasyon_degerlendirme ve rag_sorgulari test sonunda
başlangıçtaki gibi kalır. Hoca tablolarına yazılmaz.
"""

import copy
import hashlib
import json
import sys

from src.clinical_draft import build_reference_rows
from src.database import (
    count_professor_rows,
    count_rows,
    get_connection,
    init_schema,
    professor_row_label,
    save_clinical_findings,
    save_rag_result,
)


OUR_TABLES = ("klinik_bulgular", "halusinasyon_degerlendirme", "rag_sorgulari")
PROFESSOR_TABLES = ("makaleler", "tani_tedavi", "sonuclar", "semptom_iliskileri",
                    "icd10_who", "tkd_kurallari")
EXPECTED_PROFESSOR_ROWS = {"makaleler": 5, "tani_tedavi": 392, "sonuclar": 632,
                           "semptom_iliskileri": 18, "icd10_who": 43, "tkd_kurallari": 15}

# Bozuk satırların bağlanacağı üst RAG kaydı. save_rag_result sözleşmesi: çalıştırmanın sabit,
# kanonik UUID query_id'si ve tam retrieval/reranker listeleri zorunlu. Kanıt yetersiz, LLM
# çağrılmamış bir sorgu: aday chunk yok (boş ama eksiksiz listeler), kaynak tablosuna satır yazılmaz.
# UUID sabittir (uuid5, NAMESPACE_URL, "cardiorag-core/evaluation/test_klinik_bulgular"); yazım
# transaction içinde yapılıp geri alındığı için her çalıştırmada aynı değer kullanılabilir.
TEST_QUERY_ID = "57c31d7c-3a03-52ea-9006-f9c7c9a451fc"
TEST_RAG_RESULT = {
    "query_id": TEST_QUERY_ID,
    "answer": None, "llm_enabled": False, "embedding_model": "minilm",
    "evidence_check": {"sufficient": False, "reason": "test"},
    "response_time_seconds": 0, "rerank_method": "test",
    "retrieved_count": 0, "sources": [],
    "debug": {"retrieved_hits": [], "reranked_hits": []},
}


def table_hash(cursor, table, order_by):
    cursor.execute(f"SELECT * FROM `{table}` ORDER BY {order_by}")
    rows = [[None if v is None else str(v) for v in row] for row in cursor.fetchall()]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def scalar(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor.fetchone()[0]


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    conn = get_connection()
    init_schema(conn)  # DDL: yazımlardan önce (örtük commit yapar)
    cur = conn.cursor()

    initial = {t: count_rows(cur, t) for t in OUR_TABLES}
    professor_initial = {t: count_professor_rows(cur, t) for t in PROFESSOR_TABLES}
    makaleler_hash = table_hash(cur, "makaleler", "makale_id")
    conn.rollback()

    try:
        # 1) Referans taslak
        rows, draft_rejected = build_reference_rows(conn)
        check("referans taslak: denetimi geçen", 126, len(rows))
        check("referans taslak: reddedilen", 0, len(draft_rejected))

        r1 = save_clinical_findings(rows, "referans_taslak", connection=conn)
        after1 = count_rows(cur, "klinik_bulgular")
        check("1. yazım: yazilan", len(rows), r1["yazilan"])
        check("1. yazım: reddedilen", 0, len(r1["reddedilen"]))
        check("klinik_bulgular satır", initial["klinik_bulgular"] + len(rows), after1)
        check("hepsi pending", after1,
              scalar(cur, "SELECT COUNT(*) FROM klinik_bulgular WHERE review_status = 'pending'"))
        check("cikarma_yontemi = referans_taslak", len(rows),
              scalar(cur, "SELECT COUNT(*) FROM klinik_bulgular WHERE cikarma_yontemi = 'referans_taslak'"))
        check("chunk_id dolu (taslakla aynı)", sum(bool(r["chunk_id"]) for r in rows),
              scalar(cur, "SELECT COUNT(*) FROM klinik_bulgular WHERE chunk_id IS NOT NULL"))
        check("birleşik kanıtlı satır", sum(bool(r["birlestirme"]) for r in rows),
              scalar(cur, "SELECT COUNT(*) FROM klinik_bulgular WHERE birlestirme IS NOT NULL"))

        r2 = save_clinical_findings(rows, "referans_taslak", connection=conn)
        check("2. yazım: yazilan", 0, r2["yazilan"])
        check("2. yazım: zaten_var", len(rows), r2["zaten_var"])
        check("2. yazım sonrası satır artmadı", after1, count_rows(cur, "klinik_bulgular"))

        # 2) VIEW'lar
        with_outcome = sum(bool(str(r["sonuc_olcutu_tr"]).strip()) for r in rows)
        check("v_tani_tedavi = hoca + bizim", 392 + after1, scalar(cur, "SELECT COUNT(*) FROM v_tani_tedavi"))
        check("v_tani_tedavi kaynak=cardiorag", after1,
              scalar(cur, "SELECT COUNT(*) FROM v_tani_tedavi WHERE kaynak = 'cardiorag'"))
        check("v_sonuclar = hoca + bizim (sonuç ölçütü olan)", 632 + with_outcome,
              scalar(cur, "SELECT COUNT(*) FROM v_sonuclar"))
        check("v_tani_tedavi hoca satırı review_status NULL", 392,
              scalar(cur, "SELECT COUNT(*) FROM v_tani_tedavi WHERE kaynak = 'hoca' AND review_status IS NULL"))

        # 3) Bilerek bozulmuş satırlar -> halusinasyon_degerlendirme
        query_id = save_rag_result(copy.deepcopy(TEST_RAG_RESULT), "TEST: klinik bulgu çıkarımı",
                                   connection=conn)
        check("RAG üst kaydı: sabit query_id döner", TEST_QUERY_ID, query_id)
        check("RAG üst kaydı: kaynak satırı yok", 0,
              scalar(cur, "SELECT COUNT(*) FROM rag_sorgu_kaynaklari WHERE query_id = %s", (query_id,)))

        base = next(r for r in rows if r["kanit_turu"] == "metin" and not r["birlestirme"]
                    and "75" not in r["kanit_cumle"] and not r["doz"])
        wrong_page = copy.deepcopy(base)
        wrong_page["kanit_sayfa"] = int(base["kanit_sayfa"]) + 1
        fake_dose = copy.deepcopy(base)
        fake_dose["doz"] = "75 mg günde bir kez"
        fake_sentence = copy.deepcopy(base)
        fake_sentence["kanit_cumle"] = ("Apixaban 5 mg twice daily reduced all-cause mortality "
                                        "by 50% in every patient with atrial fibrillation.")
        fake_sentence["claim_text"] = "Apiksaban 5 mg günde iki kez mortaliteyi %50 azaltır."
        broken = [wrong_page, fake_dose, fake_sentence]

        before_h = count_rows(cur, "halusinasyon_degerlendirme")
        r3 = save_clinical_findings(broken, "rag", query_id=query_id, connection=conn)
        check("bozuk satırlar: yazilan", 0, r3["yazilan"])
        check("bozuk satırlar: hata_turu",
              ["yanlis_sayfa", "kanitta_olmayan_sayi", "kanit_birebir_degil"],
              [r["hata_turu"] for r in r3["reddedilen"]])
        check("halusinasyon_degerlendirme +3", before_h + 3, count_rows(cur, "halusinasyon_degerlendirme"))
        cur.execute(
            "SELECT hata_turu, kayit_turu, evaluation_method, review_status, soru_tipi, "
            "claim_text, ham_cikti FROM halusinasyon_degerlendirme WHERE query_id = %s ORDER BY id",
            (query_id,))
        logged = cur.fetchall()
        check("kayıtlar: kayit_turu/method/status",
              "klinik_cikarim/automated/pending ×3",
              ", ".join(sorted({f"{k}/{m}/{s}" for _, k, m, s, *_ in logged})) + f" ×{len(logged)}",
              all(k == "klinik_cikarim" and m == "automated" and s == "pending" and st is None
                  for _, k, m, s, st, _, _ in logged) and len(logged) == 3)
        check("kayıtlar: hata_turu", [r["hata_turu"] for r in r3["reddedilen"]],
              [h for h, *_ in logged])
        check("kayıt: LLM iddiası claim_text'te", fake_sentence["claim_text"], logged[2][5])
        check("kayıt: ham_cikti = LLM'in ham satırı", fake_sentence["kanit_cumle"],
              json.loads(logged[2][6])["kanit_cumle"])
        check("bozuk satırlar klinik_bulgular'a girmedi", after1, count_rows(cur, "klinik_bulgular"))

        # 4) Yasaklı durumlar: hiçbir şey yazılmamalı
        snapshot = {t: count_rows(cur, t) for t in OUR_TABLES}
        for name, call in (
            ("cikarma_yontemi='verified' -> hata",
             lambda: save_clinical_findings(rows[:1], "verified", connection=conn)),
            ("review_status='verified' satır -> hata",
             lambda: save_clinical_findings([{**rows[0], "review_status": "verified"}], "rag",
                                            connection=conn)),
            ("olmayan query_id -> hata",
             lambda: save_clinical_findings(broken, "rag", query_id="00000000-0000-0000-0000-000000000000",
                                            connection=conn)),
        ):
            try:
                call()
                check(name, "ValueError", "hata yok", False)
            except ValueError:
                check(name, "ValueError", "ValueError", True)
        check("yasaklı çağrılar sonrası satır sayıları", snapshot, {t: count_rows(cur, t) for t in OUR_TABLES})

        # CHECK: soru-cevap kaydında soru_tipi zorunlu
        try:
            cur.execute("INSERT INTO halusinasyon_degerlendirme (query_id, kayit_turu, soru_tipi, "
                        "evaluation_method) VALUES (%s, 'soru_cevap', NULL, 'automated')", (query_id,))
            check("CHECK: soru_cevap + soru_tipi NULL reddedilir", "hata", "eklendi", False)
        except Exception:  # noqa: BLE001 - sürücü hatası beklenir
            check("CHECK: soru_cevap + soru_tipi NULL reddedilir", "hata", "hata", True)

        # 5) Hoca tabloları değişmedi
        for table in PROFESSOR_TABLES:
            check(f"{professor_row_label(table)}: satır", EXPECTED_PROFESSOR_ROWS[table],
                  count_professor_rows(cur, table),
                  count_professor_rows(cur, table) == professor_initial[table] == EXPECTED_PROFESSOR_ROWS[table])
        check("makaleler hash değişmedi", makaleler_hash[:16], table_hash(cur, "makaleler", "makale_id")[:16])

    finally:
        conn.rollback()
        cur.close()
        conn.close()

    probe = get_connection()
    pcur = probe.cursor()
    final = {t: count_rows(pcur, t) for t in OUR_TABLES}
    probe.rollback()
    probe.close()
    check("rollback sonrası = başlangıç", initial, final)
    check("klinik_bulgular test sonunda boş", 0, final["klinik_bulgular"])
    check("halusinasyon_degerlendirme test sonunda boş", 0, final["halusinasyon_degerlendirme"])

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<34}  {'Bulunan':<34}  Sonuç")
    print("-" * (width + 82))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:34]:<34}  {act[:34]:<34}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
