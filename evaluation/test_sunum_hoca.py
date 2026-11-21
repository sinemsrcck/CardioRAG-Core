"""
Hoca sunum veritabanı (src/sunum_hoca.py, sql/sunum_hoca/) testleri.

Çalıştırma:
    python -m evaluation.test_sunum_hoca

MySQL gerektirir (evaluation/ altındaki diğer sunum testleri gibi; tests/ veritabanına bağlanmayan
unittest testleri içindir). Kurulum DDL olduğu için transaction DIŞINDA çalışır (sunum veritabanında
yalnız CREATE OR REPLACE VIEW; kaynak tablolara yazılmaz). Örnek veri yalnız transaction içinde yazılır
ve ROLLBACK edilir; sonda kaynak tablo sayıları başlangıçla aynı olmalı.

Kontroller:
- Kurulum iki kez çalışınca view listesi ve tanımları aynı kalır.
- Her view: beklenen sütunlar, teknik sütun yok, satır sayısı doğrudan klinik_bulgular sorgusuyla tutarlı,
  ad sütunlarında '|' ve " n (%)" yok, clinical_context değerleri izinli kümede.
- evidence: tablo satırında "Table N – satır adı" ('|', '<br>', '**', '<sup>' yok), metin satırında değişmedi.
- measurements / measurements_by_group yalnız Table 1 tablo satırları; metin olcum general_clinical_findings'te.
- measurements: grup sırası PDF başlık sırasıyla aynı; 3 gruplu örnek veriyle yan yana view 3 grubu
  gösterir, taban view'da veri kaybı yok.
"""

import json
import sys
from pathlib import Path

from src.sunum_hoca import (EXPECTED_COLUMNS, TECHNICAL_COLUMNS, database_names, expected_measurement_columns,
                            install, max_group_count)

PDF = Path("data/pdfs/TK-cardiovascular36-2-1.pdf")
CONTEXTS = {"exclusion criterion", "literature", "study population", "study method", "study finding", "recommendation"}
PROTECTED = ("klinik_bulgular", "kalite_isaretleri", "kalite_calistirmalari", "pdf_belgeleri", "makaleler",
             "tani_tedavi", "sonuclar", "semptom_iliskileri", "icd10_who", "tkd_kurallari")
TNO = "CASE WHEN JSON_VALID(taslak_notu) THEN JSON_UNQUOTE(JSON_EXTRACT(taslak_notu, '$.tablo_no')) END"
SNO = "CASE WHEN JSON_VALID(taslak_notu) THEN JSON_UNQUOTE(JSON_EXTRACT(taslak_notu, '$.satir_no')) END"
BASE = "bilgi_turu IS NOT NULL AND review_status = 'verified'"

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def dec(v):
    return bytes(v).decode() if isinstance(v, (bytes, bytearray)) else v


def scalar(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def counts(cur):
    return {t: scalar(cur, f"SELECT COUNT(*) FROM {t}") for t in PROTECTED}


def view_state(cur, target):
    cur.execute("SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s ORDER BY 1", (target,))
    return {dec(n): dec(d) for n, d in cur.fetchall()}


def columns(cur, target, view):
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION", (target, view))
    return [dec(r[0]) for r in cur.fetchall()]


def expected_counts(cur):
    """Görünüm satır sayıları, doğrudan klinik_bulgular'dan (v_pilot_bulgular / sunum view'ları kullanılmadan)."""
    text = f"{BASE} AND kanit_turu = 'metin'"
    t1 = f"{BASE} AND kanit_turu = 'tablo' AND {TNO} = '1'"
    t2 = f"{BASE} AND kanit_turu = 'tablo' AND {TNO} = '2'"
    distinct_rows = lambda where: scalar(cur, f"SELECT COUNT(DISTINCT document_id, {TNO}, {SNO}) FROM klinik_bulgular WHERE {where}")  # noqa: E731
    olcum_text = scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {text} AND bilgi_turu = 'olcum'")
    trt = "bilgi_turu IN ('ilac', 'doz', 'tedavi')"
    return {
        "_findings_base": scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {BASE}"),
        "measurements_by_group": scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {t1}"),
        "measurements": distinct_rows(t1),
        "treatments": distinct_rows(f"{BASE} AND kanit_turu = 'tablo' AND {trt}")
                      + scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {text} AND {trt}"),
        "clinical_outcomes": scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {t2}")
                             + scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {text} AND bilgi_turu = 'klinik_sonuc'"),
        "general_clinical_findings": scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {text} "
                                                 f"AND bilgi_turu IN ('diger', 'hasta_grubu')") + olcum_text,
    }


def check_views(conn, target, n_groups, label):
    cur = conn.cursor()
    exp_cols = dict(EXPECTED_COLUMNS, measurements=expected_measurement_columns(n_groups))
    for view, cols in exp_cols.items():
        got = columns(cur, target, view)
        check(f"{label}{view}: sütunlar", cols, got)
        check(f"{label}{view}: teknik sütun yok", set(), set(got) & TECHNICAL_COLUMNS)
    expected = expected_counts(cur)
    for view, n in expected.items():
        check(f"{label}{view}: satır sayısı = doğrudan klinik_bulgular sorgusu", n, scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}`"))
    for view, col in (("measurements", "parameter"), ("measurements_by_group", "parameter"), ("treatments", "treatment_name"),
                      ("clinical_outcomes", "finding")):
        bad = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}` WHERE `{col}` LIKE '%%|%%' OR `{col}` LIKE '%% n (%%%%)'")
        check(f"{label}{view}.{col}: '|' ve ' n (%)' yok", 0, bad)
    for view in ("treatments", "clinical_outcomes", "general_clinical_findings"):
        cur.execute(f"SELECT DISTINCT clinical_context FROM `{target}`.`{view}`")
        got = {dec(r[0]) for r in cur.fetchall()}
        check(f"{label}{view}: clinical_context izinli değerler", True, got, got <= CONTEXTS)
    for view in exp_cols:
        nulls = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}` WHERE evidence IS NULL OR source IS NULL OR page IS NULL")
        check(f"{label}{view}: evidence / source / page dolu", 0, nulls)
    check_evidence_rules(cur, target, label)
    cur.close()
    conn.rollback()


DIRTY = "(evidence LIKE '%%|%%' OR evidence LIKE '%%<br>%%' OR evidence LIKE '%%**%%' OR evidence LIKE '%%<sup>%%')"
TABLE_EVIDENCE = "evidence REGEXP '^Table [0-9]+ – .+'"


def check_evidence_rules(cur, target, label=""):
    """Tablo satırı evidence = "Table N – satır adı" (ham markdown/HTML yok); metin evidence değişmedi;
    measurements yalnız tablo satırı; metin olcum general_clinical_findings'te."""
    cur.execute("SELECT DATABASE()")
    source = dec(cur.fetchone()[0])
    tablo = scalar(cur, f"SELECT COUNT(*) FROM `{target}`._findings_base WHERE kanit_turu = 'tablo'")
    ok = scalar(cur, f"SELECT COUNT(*) FROM `{target}`._findings_base WHERE kanit_turu = 'tablo' AND {TABLE_EVIDENCE} AND NOT {DIRTY}")
    check(f"{label}tablo satırı evidence 'Table N – ad' biçiminde, '|' '<br>' '**' '<sup>' yok", tablo, ok)
    cur.execute(f"SELECT DISTINCT evidence FROM `{target}`._findings_base WHERE kanit_turu = 'tablo' AND satir_no = 1 ORDER BY 1")
    check(f"{label}tablo evidence örneği (satır 1)", ["Table 1 – Age (years)", "Table 2 – Age"], [dec(r[0]) for r in cur.fetchall()])
    check(f"{label}tablo evidence <sup> -> ^ (BMI)", 1, scalar(cur, f"SELECT COUNT(DISTINCT evidence) FROM `{target}`._findings_base "
                                                                 f"WHERE evidence = 'Table 1 – BMI kg/m^2'"))
    changed = scalar(cur, f"SELECT COUNT(*) FROM `{target}`._findings_base AS f JOIN `{source}`.v_pilot_bulgular AS b "
                          f"ON b.bulgu_id = f.bulgu_id WHERE f.kanit_turu = 'metin' AND NOT (f.evidence <=> b.kanit_gosterim)")
    check(f"{label}metin satırı evidence değişmedi (= kanit_gosterim)", 0, changed)
    for view in ("measurements", "measurements_by_group", "treatments", "clinical_outcomes", "general_clinical_findings"):
        dirty = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}` WHERE {DIRTY} AND evidence LIKE 'Table %%'")
        check(f"{label}{view}: tablo evidence'ında '|' '<br>' '**' '<sup>' yok", 0, dirty)
    for view in ("measurements", "measurements_by_group"):
        text_rows = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}` WHERE NOT {TABLE_EVIDENCE}")
        check(f"{label}{view}: metin satırı yok (hepsi Table 1)", 0, text_rows)
        check(f"{label}{view}: hepsi Table 1", 0, scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}` WHERE evidence NOT LIKE 'Table 1 – %%'"))
    check(f"{label}measurements: her satırda group_1 dolu", 0, scalar(cur, f"SELECT COUNT(*) FROM `{target}`.measurements WHERE group_1 IS NULL"))
    olcum_text = scalar(cur, "SELECT COUNT(*) FROM klinik_bulgular WHERE bilgi_turu = 'olcum' AND kanit_turu = 'metin' AND review_status = 'verified'")
    in_gcf = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.general_clinical_findings AS g JOIN `{target}`._findings_base AS f "
                         f"ON f.klinik_bilgi_en <=> g.clinical_finding AND f.evidence <=> g.evidence "
                         f"WHERE f.kanit_turu = 'metin' AND f.bilgi_turu = 'olcum'")
    check(f"{label}metin olcum kayıtları general_clinical_findings'te", olcum_text, in_gcf)
    cur.execute(f"SELECT clinical_context, COUNT(*) FROM `{target}`.clinical_outcomes GROUP BY 1")
    got = {dec(a): b for a, b in cur.fetchall()}
    check(f"{label}clinical_outcomes: clinical_context dolu ve izinli", True, got, None not in got and set(got) <= CONTEXTS)


def check_group_order(conn, target):
    """Table 1 yan yana sütunlarının sırası PDF başlığındaki grup sütun sırasıyla aynı mı."""
    if not PDF.exists():
        check("grup sırası: PDF var", True, False)
        return
    from src.pilot_clinical import pdf_pages
    from src.pilot_tablo import parse_tables
    _, pages = pdf_pages(PDF)
    table1 = next(t for t in parse_tables(pages) if t["tablo_no"] == 1)
    header = [c["grup"] for c in table1["sutunlar"] if c["rol"] == "grup"]
    cur = conn.cursor()
    cur.execute(f"SELECT group_1, group_2 FROM `{target}`.measurements WHERE group_1 IS NOT NULL")
    rows = [tuple(map(dec, r)) for r in cur.fetchall()]
    cur.execute(f"SELECT COUNT(*) FROM `{target}`.measurements_by_group WHERE group_order IS NOT NULL")
    by_group = cur.fetchone()[0]
    cur.close()
    conn.rollback()
    check("grup sırası: PDF Table 1 başlığı", "2 grup", header, len(header) == 2)
    check("grup sırası: her Table 1 satırında group_1/group_2 = PDF başlık sırası", (len(table1["satirlar"]), {tuple(header)}),
          (len(rows), set(rows)))
    check("measurements_by_group: Table 1'in her grup kaydı ayrı satır", len(table1["satirlar"]) * len(header), by_group)


def check_three_groups(target):
    """3 gruplu örnek: kurulum 3 grup sütunuyla (DDL, transaction dışı), örnek veri transaction içinde + ROLLBACK."""
    from src.database import get_connection
    from src.pilot_clinical import import_records

    install(min_groups=3, out=lambda *_: None)
    conn = get_connection()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        check("3 grup: measurements sütunları (group_1 … group_3)", expected_measurement_columns(3),
              columns(cur, target, "measurements"))
        cur.execute("SELECT document_id, filename FROM pdf_belgeleri WHERE filename = %s", (PDF.name,))
        row = cur.fetchone()
        conn.rollback()
        if not row:
            check("3 grup: pilot PDF pdf_belgeleri'de", True, False)
            return
        doc = {"document_id": dec(row[0]), "filename": dec(row[1])}
        name = "Sunum test parameter"
        note = json.dumps({"kaynak": "test-sunum-hoca", "tablo_no": 1, "satir_no": 9001, "degisken": name})
        groups = [("Group A", "1.1"), ("Group B", "2.2"), ("Group C", "3.3")]
        records = [dict(bilgi_turu="olcum", baglam="study_finding", klinik_bilgi_en=name, sonuc_olcutu_tr=name,
                        kanit_cumle=f"|{name}|1.1|2.2|3.3|0.01|", sayfa=4, kanit_turu="tablo", hasta_grubu=g, deger=v,
                        unit="mg/dL", p="0.01", taslak_notu=note, review_status="verified") for g, v in groups]
        conn.start_transaction()
        written = import_records(doc, records, connection=conn)
        check("3 grup: örnek kayıtlar yazıldı (transaction içinde)", 3, written)
        check("3 grup: kurulumun kullandığı en fazla grup sayısı (max_group_count)", 3, max_group_count(conn, target))
        cur.execute(f"SELECT group_order, group_name, group_value, unit, p_value FROM `{target}`.measurements_by_group "
                    f"WHERE parameter = %s ORDER BY group_order", (name,))
        by_group = [tuple(map(dec, r)) for r in cur.fetchall()]
        check("3 grup: taban view'da 3 satır, sıra ve değerler kayıp yok",
              [(1, "Group A", "1.1", "mg/dL", "0.01"), (2, "Group B", "2.2", "mg/dL", "0.01"), (3, "Group C", "3.3", "mg/dL", "0.01")],
              by_group)
        cur.execute(f"SELECT group_1, group_1_value, group_2, group_2_value, group_3, group_3_value, unit, p_value "
                    f"FROM `{target}`.measurements WHERE parameter = %s", (name,))
        wide = [tuple(map(dec, r)) for r in cur.fetchall()]
        check("3 grup: yan yana view'da tek satır, 3 grup", [("Group A", "1.1", "Group B", "2.2", "Group C", "3.3", "mg/dL", "0.01")], wide)
        cur.execute(f"SELECT COUNT(*) FROM `{target}`.measurements WHERE group_3 IS NOT NULL AND parameter <> %s", (name,))
        check("3 grup: pilot satırlarında group_3 boş (2 gruplu tablo)", 0, cur.fetchone()[0])
        cur.execute(f"SELECT evidence FROM `{target}`.measurements WHERE parameter = %s", (name,))
        check("3 grup: örnek tablo satırı evidence", [(f"Table 1 – {name}",)], [tuple(map(dec, r)) for r in cur.fetchall()])
        cur.close()
    finally:
        conn.rollback()
        conn.close()


def main():
    from src.database import get_connection

    conn = get_connection()
    try:
        source, target = database_names(conn)
        cur = conn.cursor()
        before = counts(cur)
        cur.close()
        conn.rollback()
    finally:
        conn.close()

    first = install(out=lambda *_: None)
    conn = get_connection()
    cur = conn.cursor()
    state1 = view_state(cur, target)
    conn.rollback()
    second = install(out=lambda *_: None)
    state2 = view_state(cur, target)
    conn.rollback()
    check("kurulum: dosyalar sırayla (00 önce)", True, first["files"], first["files"] == sorted(first["files"])
          and first["files"][0].startswith("00_"))
    check("kurulum iki kez: view listesi aynı", sorted(state1), sorted(state2))
    check("kurulum iki kez: view tanımları aynı", True, state1 == state2)
    check("kurulum: beklenen view'lar", True, sorted(state1),
          {"_findings_base", "measurements", *EXPECTED_COLUMNS} <= set(state1))
    check("kurulum: pilotta 2 grup sütunu", 2, second["groups"])
    cur.close()

    check_views(conn, target, second["groups"], "")
    check_group_order(conn, target)
    conn.close()

    check_three_groups(target)
    restored = install(out=lambda *_: None)  # gerçek veriye göre (2 grup) geri kur
    conn = get_connection()
    cur = conn.cursor()
    check("geri kurulum: measurements yine 2 grup", expected_measurement_columns(restored["groups"]),
          columns(cur, target, "measurements"))
    check("geri kurulum: view tanımları ilk kurulumla aynı", True, view_state(cur, target) == state1)
    after = counts(cur)
    check("sonda: kaynak tablo satır sayıları başlangıçla aynı", before, after)
    cur.close()
    conn.rollback()
    conn.close()

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<30}  {'Bulunan':<30}  Sonuç")
    print("-" * (width + 70))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:30]:<30}  {act[:30]:<30}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
