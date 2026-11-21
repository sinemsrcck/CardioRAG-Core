"""
Pilot gösterim VIEW'ı (v_pilot_bulgular) testleri.

Çalıştırma:
    python -m evaluation.test_pilot_gosterim

VIEW yoksa src.sunum.ensure_schema ile transaction DIŞINDA kurulur (DDL örtük commit
yapar; python -m src.sunum --kur ile aynı). Sonra tek transaction içinde
src.pilot_clinical.import_records(connection=conn) ile seçilmiş pilot kayıtları
(test kopyasında review_status='verified') yazılır, VIEW, kaynak_bul ve kalite bağı aynı
bağlantıyla okunur, sonda ROLLBACK. Tablo satır sayıları başta ve sonda aynı olmalı.
"""

import json
import sys
import tempfile
from pathlib import Path

PILOT_DIR = Path("data/review/pilot")
PILOT_PDF = Path("data/pdfs/TK-cardiovascular36-2-1.pdf")
TABLES = ("klinik_bulgular", "kalite_isaretleri", "kalite_calistirmalari", "cikarim_reddedilenler", "hata_onlem",
          "pdf_belgeleri", "makaleler", "tani_tedavi", "sonuclar", "semptom_iliskileri", "icd10_who", "tkd_kurallari")
JSON_COLUMNS = ("tablo_no", "satir_no", "alt_bolum", "analiz", "grup_n", "comparator_n", "deger_bicimi")
TEXT_SIRA = {6: "study_finding", 1: "literature_background"}  # pilot_findings_v2.json, 1 tabanlı
# Çok parçalı (pilot_findings_v3_2.json): ekokardiyografi s.2→3 ve "C-pep-" / "tide level." s.6→7.
MULTI_PICK = (lambda r: "kanit_parcalari" in r and r["sayfa"] == 2, lambda r: "kanit_parcalari" in r and r["sayfa"] == 6)
NEW_COLUMNS = ("kanit_sayfa_bitis", "birlestirme", "kanit_parca_sayisi", "kanit_gosterim")

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def db_counts():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        out = {}
        for table in TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = cursor.fetchone()[0]
        conn.rollback()
        return out
    finally:
        conn.close()


def test_records():
    """HbA1c iki grup, Table 2 HCR univariate + multivariate, bir ilaç satırı, iki metin kaydı,
    iki çok parçalı kayıt (v3.2)."""
    transfer = json.loads((PILOT_DIR / "pilot_tablo_aktarim.json").read_text(encoding="utf-8"))
    findings = json.loads((PILOT_DIR / "pilot_findings_v2.json").read_text(encoding="utf-8"))
    picked = []
    for item in transfer:
        note = json.loads(item["taslak_notu"])
        key = (note["tablo_no"], note["degisken"])
        if key in ((1, "HbA1c"), (2, "HCR")) or (key == (1, "Acetylsalicylic acid")
                                                  and item["hasta_grubu"] == "Ischemic event"):
            picked.append(dict(item))
    texts = [dict(findings[n - 1]) for n in TEXT_SIRA]
    v32 = json.loads((PILOT_DIR / "pilot_findings_v3_2.json").read_text(encoding="utf-8"))
    multi = [dict(next(r for r in v32 if wanted(r))) for wanted in MULTI_PICK]
    return [dict(r, review_status="verified") for r in picked + texts + multi]


def rows_as_dicts(cursor):
    names = [d[0] for d in cursor.description]
    decode = lambda v: bytes(v).decode() if isinstance(v, (bytes, bytearray)) else v  # noqa: E731
    return [{n: decode(v) for n, v in zip(names, row)} for row in cursor.fetchall()]


def test_view():
    from src.data_quality import Options, run_quality
    from src.database import get_connection
    from src.pilot_clinical import import_records, pdf_pages
    from src.sunum import ensure_schema, installed_objects

    conn = get_connection()
    try:
        names = {name for name, _ in installed_objects(conn)[0]}
        if "v_pilot_bulgular" not in names:
            ensure_schema(conn)  # DDL: transaction dışında (= python -m src.sunum --kur)
            names = {name for name, _ in installed_objects(conn)[0]}
        cursor = conn.cursor()
        cursor.execute("SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME = 'v_pilot_bulgular'")
        kind = cursor.fetchone()
        cursor.close()
        conn.rollback()
    finally:
        conn.close()
    check("v_pilot_bulgular kurulu (VIEW, installed_objects listesinde)", ("VIEW", True),
          (kind[0] if kind else None, "v_pilot_bulgular" in names))
    if not PILOT_PDF.exists():
        check("pilot PDF var", True, False)
        return

    doc, pages = pdf_pages(PILOT_PDF)
    records = test_records()
    before = db_counts()
    conn = get_connection()
    conn.autocommit = False
    conn.start_transaction()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM klinik_bulgular")
        last_id = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM v_pilot_bulgular")
        existing = cursor.fetchone()[0]
        written = import_records(doc, records, connection=conn)
        check("import_records aynı bağlantıyla yazdı (9 kayıt; 2'si çok parçalı)", (9, 9), (len(records), written))

        cursor.execute("SELECT * FROM v_pilot_bulgular WHERE bulgu_id > %s", (last_id,))
        view = rows_as_dicts(cursor)
        cursor.execute("SELECT COUNT(*) FROM v_pilot_bulgular")
        total = cursor.fetchone()[0]
        check("VIEW: pilot satır başına bir satır (9 yeni, toplam = önceki + 9)", (9, existing + 9), (len(view), total))
        check("VIEW: PDF adı ve makale başlığı dolu", (True, {doc["filename"]}),
              (all(r["makale_basligi"] for r in view), {r["filename"] for r in view}))

        def table_rows(tablo, degisken):
            return sorted((r for r in view if r["tablo_no"] == tablo and r["degisken"] == degisken),
                          key=lambda r: (r["hasta_grubu"] or "", r["analiz"] or ""))

        hba1c = table_rows(1, "HbA1c")
        check("HbA1c: iki grup, grup/comparator/değer/p",
              [("Ischemic event", "No ischemic event", "7.83±1.2", "<0.001"),
               ("No ischemic event", "Ischemic event", "6.23±1.2", "<0.001")],
              [(r["hasta_grubu"], r["comparator"], r["deger"], r["p"]) for r in hba1c])
        check("HbA1c: taslak_notu'dan tablo_no / grup_n / comparator_n / alt_bolum / deger_bicimi",
              [(1, 372, 853, "Laboratory findings", "ortalama±SS"), (1, 853, 372, "Laboratory findings", "ortalama±SS")],
              [(r["tablo_no"], r["grup_n"], r["comparator_n"], r["alt_bolum"], r["deger_bicimi"]) for r in hba1c])
        check("HbA1c: Türkçe etiketler (asıl değerler yanında)",
              {("olcum", "Ölçüm", "study_finding", "Çalışmanın bulgusu", "tablo", "Tablo")},
              {(r["bilgi_turu"], r["bilgi_turu_tr"], r["baglam"], r["baglam_tr"], r["kanit_turu"], r["kanit_turu_tr"])
               for r in hba1c})
        hcr = table_rows(2, "HCR")
        check("HCR Table 2: analiz univariate / multivariate, OR, GA, p",
              [("multivariate", "OR", "3.216", "2.334-4.365", "<0.001"),
               ("univariate", "OR", "3.124", "2.213-4.310", "<0.001")],
              [(r["analiz"], r["etki_turu"], r["deger"], r["ci95"], r["p"]) for r in hcr])
        check("HCR Table 2: tablo_no = 2, grup alanları NULL", {(2, None, None, None)},
              {(r["tablo_no"], r["hasta_grubu"], r["grup_n"], r["comparator_n"]) for r in hcr})
        # İlaç satırında ilaç adı ilac (= mudahale_en) sütununda; degisken (= sonuc_olcutu_tr) "kullanım oranı".
        drug = [r for r in view if r["ilac"] == "Acetylsalicylic acid"]
        check("İlaç satırı: ilac / degisken / mudahale_turu / etiket / değer / grup_n",
              [("Acetylsalicylic acid", "kullanım oranı", "arka plan tedavisi", "İlaç", "Medications", "18 (4.83)",
                "n (%)", 372, 1)],
              [(r["ilac"], r["degisken"], r["mudahale_turu"], r["bilgi_turu_tr"], r["alt_bolum"], r["deger"],
                r["deger_bicimi"], r["grup_n"], r["tablo_no"]) for r in drug])
        texts = [r for r in view if r["kanit_turu"] == "metin" and r["kanit_parca_sayisi"] == 1]
        check("iki bağlam aynı listede (filtre yok), Türkçe etiketleri doğru",
              {("literature_background", "Literatür bilgisi"), ("study_finding", "Çalışmanın bulgusu")},
              {(r["baglam"], r["baglam_tr"]) for r in texts})
        check("metin kayıtlarında taslak_notu sütunları NULL", {None},
              {r[c] for r in texts for c in JSON_COLUMNS})
        check("metin kayıtları: klinik_bilgi_en kanıtın içinde, review_status verified", True,
              all(r["klinik_bilgi_en"] in r["kanit_cumle"] and r["review_status"] == "verified" for r in texts))
        cursor.execute("SELECT bulgu_id FROM v_pilot_bulgular")
        order = [r[0] for r in cursor.fetchall() if r[0] > last_id]
        expected_order = [r["bulgu_id"] for r in sorted(view, key=lambda r: (
            r["filename"], r["kanit_sayfa"], -1 if r["tablo_no"] is None else r["tablo_no"], r["bulgu_id"]))]
        check("sıralama: dosya, sayfa, tablo_no (metin önce), kayıt sırası", expected_order, order)

        # Çok parçalı kanıt sütunları (sona eklendi; mevcut sütunların adı/sırası aynı).
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME = 'v_pilot_bulgular' ORDER BY ORDINAL_POSITION")
        columns = [r[0] for r in cursor.fetchall()]
        check("yeni sütunlar sonda, mevcut 36 sütun yerinde", (40, NEW_COLUMNS, "bulgu_id", "kalite_kontrolleri"),
              (len(columns), tuple(columns[-4:]), columns[0], columns[35]))
        multi_src = [r for r in records if "kanit_parcalari" in r]
        multi = sorted((r for r in view if r["kanit_parca_sayisi"] > 1), key=lambda r: r["kanit_sayfa"])
        check("çok parçalı: kanit_sayfa / kanit_sayfa_bitis / birlestirme / kanit_parca_sayisi",
              [(2, 3, "sayfa", 2), (6, 7, "sayfa", 2)],
              [(r["kanit_sayfa"], r["kanit_sayfa_bitis"], r["birlestirme"], r["kanit_parca_sayisi"]) for r in multi])
        check("çok parçalı: kanit_cumle ilk parça (birleştirilmiş alıntı değil)", True,
              all(r["kanit_cumle"] == s["kanit_parcalari"][0]["metin"] for r, s in zip(multi, multi_src)))
        expected_show = [" ".join(f"[s.{p['sayfa']}] {p['metin']}" for p in s["kanit_parcalari"]) for s in multi_src]
        check("kanit_gosterim: parçalar '[s.N]' işaretiyle sırayla (yalnız gösterim)", expected_show,
              [r["kanit_gosterim"] for r in multi])
        check("kanit_gosterim C-pep örneği", "[s.6] HCR rate is directly proportional to HbA1c value and inversely "
              "proportional to C-pep- [s.7] tide level.", multi[1]["kanit_gosterim"])
        single = [r for r in view if r["kanit_parca_sayisi"] == 1]
        check("parçasız kayıtlar: kanit_gosterim = kanit_cumle, parça sayısı 1, bitiş/birleşme NULL",
              (7, True, {None}, {None}),
              (len(single), all(r["kanit_gosterim"] == r["kanit_cumle"] for r in single),
               {r["kanit_sayfa_bitis"] for r in single}, {r["birlestirme"] for r in single}))

        cursor.callproc("kaynak_bul", ("HbA1c",))
        found = {r[0] for res in cursor.stored_results() for r in res.fetchall()}
        with_term = {r["bulgu_id"] for r in view if "hba1c" in r["kanit_cumle"].casefold()}
        check("kaynak_bul pilot satırlarını da buluyor ('HbA1c' -> iki HbA1c tablo satırı + 'C-pep-' parçalı kaydı)",
              (with_term, True, 3), (found & {r["bulgu_id"] for r in view},
                                     {r["bulgu_id"] for r in hba1c} <= found, len(with_term)))
        cursor.callproc("kaynak_bul", ("Acetylsalicylic",))
        found = {r[0] for res in cursor.stored_results() for r in res.fetchall()}
        check("kaynak_bul: ilaç adıyla pilot ilaç satırı", {r["bulgu_id"] for r in drug},
              found & {r["bulgu_id"] for r in view})

        # Kalite bağı: aynı transaction'da --pilot koşusu (en son sürüm olur), VIEW sayıları tabloyla aynı.
        with tempfile.TemporaryDirectory() as tmp:
            summary, flags, _ = run_quality(Options(pilot=True, rapor_dir=Path(tmp)), connection=conn,
                                            out=lambda *_: None, page_loader=lambda path: (doc, pages))
        cursor.execute("SELECT bulgu_id, kalite_hata, kalite_uyari, kalite_bilgi, kalite_en_yuksek FROM v_pilot_bulgular "
                       "WHERE bulgu_id > %s ORDER BY bulgu_id", (last_id,))
        linked = {r[0]: tuple(r[1:]) for r in cursor.fetchall()}
        cursor.execute("SELECT klinik_bulgu_id, SUM(seviye = 'hata'), SUM(seviye = 'uyari'), SUM(seviye = 'bilgi') "
                       "FROM kalite_isaretleri WHERE kontrol_surumu = %s AND klinik_bulgu_id > %s "
                       "GROUP BY klinik_bulgu_id", (summary["kontrol_surumu"], last_id))
        expected = {}
        for bulgu_id, hata, uyari, bilgi in cursor.fetchall():
            hata, uyari, bilgi = int(hata), int(uyari), int(bilgi)
            top = "hata" if hata else "uyari" if uyari else "bilgi" if bilgi else None
            expected[bulgu_id] = (hata, uyari, bilgi, top)
        expected = {b: expected.get(b, (0, 0, 0, None)) for b in linked}
        check("kalite bağı: VIEW sayıları ve en yüksek seviye kalite_isaretleri ile aynı", expected,
              {b: (int(h), int(u), int(i), top) for b, (h, u, i, top) in linked.items()})
        check("kalite bağı: en az bir pilot satırı işaretli (ör. HbA1c birimsiz değer)", True,
              any(top for *_, top in linked.values()) and linked[hba1c[0]["bulgu_id"]][3] == "uyari")
        cursor.close()
    finally:
        conn.rollback()
        conn.close()
    check("ROLLBACK sonrası tablo satır sayıları başlangıçla aynı", before, db_counts())


def main():
    test_view()
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
