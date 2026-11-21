"""
Hoca sunum veritabanı (src/sunum_hoca.py, sql/sunum_hoca/) testleri.

Çalıştırma:
    python -m evaluation.test_sunum_hoca

MySQL gerektirir (evaluation/ altındaki diğer sunum testleri gibi; tests/ veritabanına bağlanmayan
unittest testleri içindir). Kurulum DDL olduğu için transaction DIŞINDA çalışır (sunum veritabanında
yalnız CREATE OR REPLACE VIEW; kaynakta yalnız v_sunum_taban view'ı; kaynak tablolara yazılmaz). Örnek veri yalnız transaction içinde yazılır
ve ROLLBACK edilir; sonda kaynak tablo sayıları başlangıçla aynı olmalı.

Kontroller:
- Kurulum iki kez çalışınca view listesi ve tanımları aynı kalır (sunum + kaynaktaki v_sunum_taban).
- Sunum veritabanında yalnız hocaya gösterilen view'lar: '_' ile başlayan view yok, hiçbir view'da teknik
  sütun yok; taban view kaynakta (v_sunum_taban), eski _findings_base kurulumda silinir; kaynaktaki
  diğer view'lar değişmez.
- Sabit satır sayıları (measurements 51, measurements_by_group 102, clinical_outcomes 66, treatments 13,
  general_clinical_findings 44).
- Sıra: clinical_outcomes'ta metin 'study finding' -> Table 2 -> 'literature', grup içinde sayfaya göre;
  general_clinical_findings'te 'literature' en sonda.
- Her view: beklenen sütunlar, teknik sütun yok, satır sayısı doğrudan klinik_bulgular sorgusuyla tutarlı,
  ad sütunlarında '|' ve " n (%)" yok, clinical_context değerleri izinli kümede.
- evidence: tablo satırında "Table N – satır adı" ('|', '<br>', '**', '<sup>' yok), metin satırında değişmedi.
- measurements / measurements_by_group yalnız Table 1 tablo satırları; metin olcum general_clinical_findings'te.
- measurements: grup sırası PDF başlık sırasıyla aynı; 3 gruplu örnek veriyle yan yana view 3 grubu
  gösterir, taban view'da veri kaybı yok.
"""

import json
import re
import sys
from pathlib import Path

from src.sunum_hoca import (BASE_VIEW, EXPECTED_COLUMNS, OLD_BASE_VIEW, TECHNICAL_COLUMNS, database_names,
                            expected_measurement_columns, install, max_group_count)

PDF = Path("data/pdfs/TK-cardiovascular36-2-1.pdf")
CONTEXTS = {"exclusion criterion", "literature", "study population", "study method", "study finding", "recommendation"}
PROTECTED = ("klinik_bulgular", "kalite_isaretleri", "kalite_calistirmalari", "pdf_belgeleri", "makaleler",
             "tani_tedavi", "sonuclar", "semptom_iliskileri", "icd10_who", "tkd_kurallari")
TNO = "CASE WHEN JSON_VALID(taslak_notu) THEN JSON_UNQUOTE(JSON_EXTRACT(taslak_notu, '$.tablo_no')) END"
SNO = "CASE WHEN JSON_VALID(taslak_notu) THEN JSON_UNQUOTE(JSON_EXTRACT(taslak_notu, '$.satir_no')) END"
BASE = "bilgi_turu IS NOT NULL AND review_status = 'verified'"
HOCA_VIEWS = {"measurements", *EXPECTED_COLUMNS}  # sunum veritabanında yalnız bunlar olmalı
# Pilot veride sabit satır sayıları (veri değişirse bilerek güncellenir).
FIXED_COUNTS = {"measurements": 51, "measurements_by_group": 102, "clinical_outcomes": 66, "treatments": 13,
                "general_clinical_findings": 44, "diseases": 20, "diagnostics": 17, "symptoms": 0}

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


def view_state(cur, schema, only=None):
    sql = "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s"
    params = [schema]
    if only:
        sql += " AND TABLE_NAME = %s"
        params.append(only)
    cur.execute(sql + " ORDER BY 1", params)
    return {dec(n): dec(d) for n, d in cur.fetchall()}


def page_num(page):
    return int(re.match(r"\d+", page).group())


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
    for view, n in FIXED_COUNTS.items():
        check(f"{label}{view}: satır sayısı değişmedi (sabit)", n, scalar(cur, f"SELECT COUNT(*) FROM `{target}`.`{view}`"))
    source = dec(scalar(cur, "SELECT DATABASE()"))
    check(f"{label}{BASE_VIEW}: satır sayısı = verified klinik_bulgular", scalar(cur, f"SELECT COUNT(*) FROM klinik_bulgular WHERE {BASE}"),
          scalar(cur, f"SELECT COUNT(*) FROM `{source}`.`{BASE_VIEW}`"))
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
    base = f"`{source}`.`{BASE_VIEW}`"
    tablo = scalar(cur, f"SELECT COUNT(*) FROM {base} WHERE kanit_turu = 'tablo'")
    ok = scalar(cur, f"SELECT COUNT(*) FROM {base} WHERE kanit_turu = 'tablo' AND {TABLE_EVIDENCE} AND NOT {DIRTY}")
    check(f"{label}tablo satırı evidence 'Table N – ad' biçiminde, '|' '<br>' '**' '<sup>' yok", tablo, ok)
    cur.execute(f"SELECT DISTINCT evidence FROM {base} WHERE kanit_turu = 'tablo' AND satir_no = 1 ORDER BY 1")
    check(f"{label}tablo evidence örneği (satır 1)", ["Table 1 – Age (years)", "Table 2 – Age"], [dec(r[0]) for r in cur.fetchall()])
    check(f"{label}tablo evidence <sup> -> ^ (BMI)", 1, scalar(cur, f"SELECT COUNT(DISTINCT evidence) FROM {base} "
                                                                 f"WHERE evidence = 'Table 1 – BMI kg/m^2'"))
    changed = scalar(cur, f"SELECT COUNT(*) FROM {base} AS f JOIN `{source}`.v_pilot_bulgular AS b "
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
    in_gcf = scalar(cur, f"SELECT COUNT(*) FROM `{target}`.general_clinical_findings AS g JOIN {base} AS f "
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


def check_hoca_only(conn, source, target, label=""):
    """Sunum veritabanında yalnız hocaya gösterilen view'lar; taban view kaynakta."""
    cur = conn.cursor()
    names = set(view_state(cur, target))
    check(f"{label}sunum veritabanı: yalnız hoca view'ları", sorted(HOCA_VIEWS), sorted(names))
    check(f"{label}sunum veritabanı: '_' ile başlayan view yok", [], sorted(n for n in names if n.startswith("_")))
    check(f"{label}sunum veritabanı: {OLD_BASE_VIEW} yok", False, OLD_BASE_VIEW in names)
    cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE <> 'VIEW'", (target,))
    check(f"{label}sunum veritabanı: tablo yok (yalnız view)", 0, cur.fetchone()[0])
    cur.execute("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s", (target,))
    tech = sorted({f"{dec(v)}.{dec(c)}" for v, c in cur.fetchall() if dec(c) in TECHNICAL_COLUMNS})
    check(f"{label}sunum veritabanı: hiçbir view'da teknik sütun yok", [], tech)
    check(f"{label}kaynak: {BASE_VIEW} kurulu", [BASE_VIEW], sorted(view_state(cur, source, BASE_VIEW)))
    cur.close()
    conn.rollback()


def check_ordering(conn, target, label=""):
    """clinical_outcomes: metin 'study finding' -> Table 2 -> 'literature'; general_clinical_findings: literature sonda.
    Her grup içinde sayfa artmayan sırada değil (page_sort'a göre)."""
    cur = conn.cursor()
    cur.execute(f"SELECT clinical_context, evidence, page FROM `{target}`.clinical_outcomes")
    rows = [tuple(map(dec, r)) for r in cur.fetchall()]

    def outcome_group(ctx, evidence):
        if re.match(r"^Table 2 – ", evidence):
            return 2
        return {"study finding": 1, "literature": 4}.get(ctx, 3)

    groups = [outcome_group(c, e) for c, e, _ in rows]
    check(f"{label}clinical_outcomes: grup sırası (study finding -> Table 2 -> literature)", True,
          [g for i, g in enumerate(groups) if i == 0 or g != groups[i - 1]], groups == sorted(groups))
    check(f"{label}clinical_outcomes: grup büyüklükleri", {1: 13, 2: 14, 4: 39},
          {g: groups.count(g) for g in sorted(set(groups))})
    bad = [i for i in range(1, len(rows)) if groups[i] == groups[i - 1] and page_num(rows[i][2]) < page_num(rows[i - 1][2])]
    check(f"{label}clinical_outcomes: grup içinde sayfa sırası", [], bad)
    cur.execute(f"SELECT clinical_context, page FROM `{target}`.general_clinical_findings")
    rows = [tuple(map(dec, r)) for r in cur.fetchall()]
    lit = [c == "literature" for c, _ in rows]
    check(f"{label}general_clinical_findings: literature en sonda", True, sum(lit), lit == sorted(lit) and any(lit))
    bad = [i for i in range(1, len(rows)) if lit[i] == lit[i - 1] and page_num(rows[i][1]) < page_num(rows[i - 1][1])]
    check(f"{label}general_clinical_findings: grup içinde sayfa sırası", [], bad)
    cur.close()
    conn.rollback()


def check_old_base_dropped(conn, source, target):
    """Eski kurulumdan kalan _findings_base varsa kurulum siler (tekrar çalıştırılabilir)."""
    cur = conn.cursor()
    cur.execute(f"CREATE OR REPLACE VIEW `{target}`.`{OLD_BASE_VIEW}` AS SELECT 1 AS bulgu_id")
    check(f"eski {OLD_BASE_VIEW}: test için oluşturuldu", True, OLD_BASE_VIEW in view_state(cur, target))
    conn.rollback()
    install(out=lambda *_: None)
    check(f"eski {OLD_BASE_VIEW}: kurulum sildi", False, OLD_BASE_VIEW in view_state(cur, target))
    install(out=lambda *_: None)
    check(f"eski {OLD_BASE_VIEW} yokken kurulum yine çalışır", sorted(HOCA_VIEWS), sorted(view_state(cur, target)))
    cur.close()
    conn.rollback()


def check_three_groups(source, target):
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
        check("3 grup: kurulumun kullandığı en fazla grup sayısı (max_group_count)", 3, max_group_count(conn, source))
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

    conn = get_connection()
    cur = conn.cursor()
    source_views_before = {n: d for n, d in view_state(cur, source).items() if n != BASE_VIEW}
    conn.rollback()
    first = install(out=lambda *_: None)
    state1 = view_state(cur, target)
    base1 = view_state(cur, source, BASE_VIEW)
    conn.rollback()
    second = install(out=lambda *_: None)
    state2 = view_state(cur, target)
    base2 = view_state(cur, source, BASE_VIEW)
    conn.rollback()
    check("kurulum: dosyalar sırayla (00 önce)", True, first["files"], first["files"] == sorted(first["files"])
          and first["files"][0].startswith("00_"))
    check("kurulum iki kez: view listesi aynı", sorted(state1), sorted(state2))
    check("kurulum iki kez: view tanımları aynı", True, state1 == state2)
    check(f"kurulum iki kez: kaynaktaki {BASE_VIEW} tanımı aynı", True, bool(base1) and base1 == base2)
    check("kurulum: beklenen view'lar", sorted(HOCA_VIEWS), sorted(state1))
    check("kurulum: pilotta 2 grup sütunu", 2, second["groups"])
    cur.close()

    check_hoca_only(conn, source, target)
    check_views(conn, target, second["groups"], "")
    check_ordering(conn, target)
    check_group_order(conn, target)
    check_old_base_dropped(conn, source, target)
    conn.close()

    check_three_groups(source, target)
    restored = install(out=lambda *_: None)  # gerçek veriye göre (2 grup) geri kur
    conn = get_connection()
    cur = conn.cursor()
    check("geri kurulum: measurements yine 2 grup", expected_measurement_columns(restored["groups"]),
          columns(cur, target, "measurements"))
    check("geri kurulum: view tanımları ilk kurulumla aynı", True, view_state(cur, target) == state1)
    source_views_after = {n: d for n, d in view_state(cur, source).items() if n != BASE_VIEW}
    check(f"kaynak: {BASE_VIEW} dışındaki view'lar değişmedi", sorted(source_views_before),
          sorted(source_views_after), source_views_before == source_views_after)
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
