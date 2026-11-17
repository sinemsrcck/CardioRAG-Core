
"""
makaleler tablosuna eklenen PDF makaleleri testi.

Çalıştırma:
    python -m evaluation.test_makaleler

Ön koşul:
    python -m src.article_metadata --write --tam-metin temiz
    python -m src.load_translations
Bu test yazımı bir kez daha çalıştırır (aynı değerlerle upsert) ve satır
sayısının artmadığını, LLM çevirilerinin silinmediğini ve hocanın
satırlarının değişmediğini doğrular.
"""

import hashlib
import json
import sys

from src.article_metadata import (
    INTERPRETIVE_COLUMNS,
    MANAGED_COLUMNS,
    OZET_TR_COLUMNS,
    _as_text,
    build_all,
    columns_to_write,
    professor_rows_fingerprint,
    write_records,
)
from src.database import get_connection
from src.load_translations import (
    DEFAULT_FILE,
    apply_plan,
    build_plan,
    load_entries,
    run as run_translations,
)


# Hocanın 5 satırının parmak izi (bütün sütunlar, makale_id sırasıyla, SHA-256).
# ozet_tr_kaynak/ozet_tr_model sütunları eklenince SELECT * değiştiği için
# yenilendi; veri aynı: bu iki sütun hariç alınan iz, ilk --write'tan ÖNCE
# alınmış izle (PROFESSOR_BASELINE_OLD_COLUMNS) birebir aynı.
PROFESSOR_BASELINE = (5, "76302767d206ab75ca9eb84f0b13399e61848f64014de7e9a4725ef7d85f57d2")
PROFESSOR_BASELINE_OLD_COLUMNS = (
    5, "433d90bef85bc651fc975ad6234b36c4214e0e35f7cf98722f3289526f946ad2")
NEW_OZET_COLUMNS = ("ozet_tr_kaynak", "ozet_tr_model")

EXPECTED_OUR_ROWS = 6
EXPECTED_LLM_ROWS = 4
EXPECTED_LLM_MODEL = "gpt-6-luna"
KNOWN_DOIS = {
    "who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf": "10.29271/jcpsp.2020.09.906",
    "who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf": "10.26719/emhj.20.141",
}


def _text(value):
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def our_rows(cursor):
    columns = ("makale_id",) + MANAGED_COLUMNS + INTERPRETIVE_COLUMNS
    cursor.execute(
        f"SELECT {', '.join(f'm.`{c}`' for c in columns)}, b.filename, b.page_count "
        "FROM makaleler AS m "
        "LEFT JOIN pdf_belgeleri AS b ON b.document_id = m.document_id "
        "WHERE m.document_id IS NOT NULL ORDER BY b.filename"
    )
    rows = []
    for values in cursor.fetchall():
        row = dict(zip(columns + ("_filename", "_page_count"), (_text(v) for v in values)))
        rows.append(row)
    return rows


def professor_rows_old_columns(cursor):
    """Hocanın satırları: yeni ozet sütunları hariç parmak izi + yeni sütunlar NULL mu."""
    cursor.execute("SELECT * FROM makaleler WHERE document_id IS NULL ORDER BY makale_id")
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, (_as_text(v) for v in row))) for row in cursor.fetchall()]
    payload = [{c: (None if v is None else str(v)) for c, v in row.items()
                if c not in NEW_OZET_COLUMNS} for row in rows]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    new_all_null = all(row[c] is None for row in rows for c in NEW_OZET_COLUMNS)
    return (len(rows), digest), new_all_null


def ozet_snapshot(rows):
    return {r["document_id"]: tuple(r[c] for c in OZET_TR_COLUMNS) for r in rows}


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    conn = get_connection()
    conn.autocommit = True
    cursor = conn.cursor()

    # 1) Hocanın satırları ilk yazımdan önceki haliyle birebir aynı.
    check("hocanın satırları (önce)", PROFESSOR_BASELINE[1][:16] + "…",
          professor_rows_fingerprint(cursor)[1][:16] + "…",
          professor_rows_fingerprint(cursor) == PROFESSOR_BASELINE)
    old_print, new_all_null = professor_rows_old_columns(cursor)
    check("hocanın satırları (yeni sütunlar hariç) = ilk iz", PROFESSOR_BASELINE_OLD_COLUMNS[1][:16] + "…",
          old_print[1][:16] + "…", old_print == PROFESSOR_BASELINE_OLD_COLUMNS)
    check("hocanın satırlarında ozet_tr_kaynak/model NULL", True, new_all_null)

    # 2) Bizim satırlarımız.
    rows = our_rows(cursor)
    check("document_id dolu satır", EXPECTED_OUR_ROWS, len(rows))
    check("hepsi pdf_belgeleri'de", EXPECTED_OUR_ROWS,
          sum(r["_filename"] is not None for r in rows))
    cursor.execute(
        "SELECT COUNT(*) FROM pdf_belgeleri AS b "
        "WHERE b.durum = 'tamamlandi' AND NOT EXISTS "
        "(SELECT 1 FROM makaleler AS m WHERE m.document_id = b.document_id)"
    )
    check("makaleler'de karşılığı olmayan belge", 0, cursor.fetchone()[0])
    check("document_id tekrar", 0, len(rows) - len({r["document_id"] for r in rows}))

    by_file = {r["_filename"]: r for r in rows}
    for filename, doi in KNOWN_DOIS.items():
        check(f"DOI {doi}", doi, (by_file.get(filename) or {}).get("doi"))

    check("makale_id = DOI-<doi>", EXPECTED_OUR_ROWS,
          sum(r["makale_id"] == f"DOI-{r['doi'].lower()}" for r in rows if r["doi"]))
    check("baglanti = https://doi.org/<doi>", EXPECTED_OUR_ROWS,
          sum(r["baglanti"] == f"https://doi.org/{r['doi']}" for r in rows if r["doi"]))
    check("pdf_sayfa = page_count", EXPECTED_OUR_ROWS,
          sum(int(r["pdf_sayfa"]) == int(r["_page_count"]) for r in rows))
    check("yorum alanları NULL", EXPECTED_OUR_ROWS,
          sum(all(r[c] is None for c in INTERPRETIVE_COLUMNS) for r in rows))
    check("ozet_tr_kaynak='orijinal' yalnızca TK'de", "TK", ", ".join(sorted(
        {r["_filename"].split("-", 1)[0] for r in rows if r["ozet_tr_kaynak"] == "orijinal"})))
    check("'orijinal' satırlarda model NULL", True,
          all(r["ozet_tr_model"] is None for r in rows if r["ozet_tr_kaynak"] == "orijinal"))
    llm_rows = [r for r in rows if r["ozet_tr_kaynak"] == "llm_ceviri"]
    check("ozet_tr_kaynak='llm_ceviri' satır", EXPECTED_LLM_ROWS, len(llm_rows))
    check(f"llm_ceviri satırlarında model = {EXPECTED_LLM_MODEL}", EXPECTED_LLM_ROWS,
          sum(r["ozet_tr_model"] == EXPECTED_LLM_MODEL for r in llm_rows))
    check("llm_ceviri satırları TK dışı", True,
          all(not r["_filename"].startswith("TK-") for r in llm_rows))
    translations = {e["document_id"]: e["ozet_tr"].strip() for e in load_entries(DEFAULT_FILE)}
    check("llm_ceviri ozet_tr = çeviri dosyası", EXPECTED_LLM_ROWS,
          sum(r["ozet_tr"] == translations.get(r["document_id"]) for r in llm_rows))
    check("ozet_tr dolu, kaynağı belirsiz satır", 0,
          sum(r["ozet_tr"] is not None and r["ozet_tr_kaynak"] is None for r in rows))
    rerun = run_translations(DEFAULT_FILE, dry_run=True)
    check("load_translations tekrar: hepsi zaten_ayni", EXPECTED_LLM_ROWS,
          sum(p["durum"] == "zaten_ayni" for p in rerun["plans"]))

    empty_abstracts = [r["_filename"] for r in rows if not r["abstract_en"]]
    check("abstract_en boş olanlar (bilgi)", "-",
          ", ".join(empty_abstracts) or "yok", True)

    # 3) Veritabanı içeriği = PDF'ten yeniden üretilen kayıtlar (yalnızca
    #    article_metadata'nın o satıra yazdığı sütunlar; çeviriler hariç).
    built = build_all(conn, "temiz")
    expected = {b["record"]["document_id"]: b["record"] for b in built}
    differing = [
        f"{r['_filename'][:20]}.{c}"
        for r in rows for c in ("makale_id",) + columns_to_write(expected[r["document_id"]])
        if str(r[c]) != str(expected[r["document_id"]][c])
    ]
    check("içerik = PDF'ten üretilen", "aynı", ", ".join(differing) or "aynı")

    # 4) İkinci yazım: satır sayısı artmamalı, hocanın satırları değişmemeli.
    cursor.execute("SELECT COUNT(*) FROM makaleler")
    total_before = cursor.fetchone()[0]
    result = write_records(conn, built)
    cursor.execute("SELECT COUNT(*) FROM makaleler")
    total_after = cursor.fetchone()[0]

    check("2. yazım: eklenen", 0, result["inserted"])
    check("2. yazım: güncellenen", EXPECTED_OUR_ROWS, result["updated"])
    check("2. yazım: makaleler toplam", total_before, total_after)
    conn.commit()  # yeni anlık görüntü
    after_rows = our_rows(cursor)
    check("2. yazım: ozet_tr/kaynak/model (çeviriler dahil) değişmedi", "aynı",
          "aynı" if ozet_snapshot(after_rows) == ozet_snapshot(rows) else "farklı")
    check("2. yazım sonrası llm_ceviri satır", EXPECTED_LLM_ROWS,
          sum(r["ozet_tr_kaynak"] == "llm_ceviri" for r in after_rows))
    check("hocanın satırları (sonra)", PROFESSOR_BASELINE[1][:16] + "…",
          professor_rows_fingerprint(cursor)[1][:16] + "…",
          professor_rows_fingerprint(cursor) == PROFESSOR_BASELINE)

    # 5) Senaryo (transaction + ROLLBACK, veritabanı değişmez): çeviriler --init'ten
    #    önce yüklenmiş, ozet_tr dolu ama ozet_tr_kaynak/model NULL. Metni JSON'la
    #    birebir aynı olanlar yalnızca etiketlenmeli; farklı olan reddedilmeli.
    conn.commit()
    snapshot_before = ozet_snapshot(our_rows(cursor))
    llm_ids = sorted(r["document_id"] for r in after_rows if r["ozet_tr_kaynak"] == "llm_ceviri")
    changed_id = llm_ids[0]
    scenario = get_connection()
    scenario.autocommit = True
    scenario.start_transaction()
    scur = scenario.cursor()
    try:
        scenario_fp = professor_rows_fingerprint(scur)
        scur.execute(
            "UPDATE makaleler SET ozet_tr_kaynak = NULL, ozet_tr_model = NULL "
            f"WHERE document_id IN ({', '.join(['%s'] * len(llm_ids))}) "
            "AND document_id IS NOT NULL", llm_ids)
        scur.execute(
            "UPDATE makaleler SET ozet_tr = CONCAT(ozet_tr, ' (farklı)') "
            "WHERE document_id = %s AND document_id IS NOT NULL", (changed_id,))

        plans = {p["document_id"]: p for p in build_plan(scur, load_entries(DEFAULT_FILE))}
        same_ids = [d for d in llm_ids if d != changed_id]
        check("senaryo: aynı metin + kaynak NULL -> etiketlenecek", len(same_ids),
              sum(plans[d]["durum"] == "etiketlenecek" for d in same_ids))
        check("senaryo: farklı metin + kaynak NULL -> reddedildi", "reddedildi",
              plans[changed_id]["durum"])
        written = apply_plan(scenario, scur, list(plans.values()))
        check("senaryo: yazılan (yalnızca etiket)", len(same_ids), written)

        srows = {r["document_id"]: r for r in our_rows(scur)}
        check("senaryo: etiketlenenlerde kaynak=llm_ceviri, model dolu", len(same_ids),
              sum(srows[d]["ozet_tr_kaynak"] == "llm_ceviri"
                  and srows[d]["ozet_tr_model"] == EXPECTED_LLM_MODEL for d in same_ids))
        check("senaryo: etiketlenenlerin metni değişmedi", len(same_ids),
              sum(srows[d]["ozet_tr"] == translations[d] for d in same_ids))
        check("senaryo: farklı metinli satıra dokunulmadı", True,
              srows[changed_id]["ozet_tr"] == translations[changed_id] + " (farklı)"
              and srows[changed_id]["ozet_tr_kaynak"] is None
              and srows[changed_id]["ozet_tr_model"] is None)
        replans = build_plan(scur, load_entries(DEFAULT_FILE))
        check("senaryo: tekrar planla -> zaten_ayni", len(same_ids),
              sum(p["durum"] == "zaten_ayni" for p in replans))
        check("senaryo: hocanın satırları (transaction içinde)", True,
              professor_rows_fingerprint(scur) == scenario_fp == PROFESSOR_BASELINE)
    finally:
        scenario.rollback()
        scur.close()
        scenario.close()
    conn.commit()  # yeni anlık görüntü
    check("senaryo sonrası ROLLBACK: ozet sütunları eski haliyle", "aynı",
          "aynı" if ozet_snapshot(our_rows(cursor)) == snapshot_before else "farklı")

    cursor.close()
    conn.close()

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<22}  {'Bulunan':<44}  Sonuç")
    print("-" * (width + 78))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:22]:<22}  {act[:44]:<44}  {'OK' if passed else 'HATA'}")

    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
