"""
Veri kalitesi işaretleri testleri.

Çalıştırma:
    python -m evaluation.test_data_quality

A) Veritabanı gerektirmeyenler: normalizasyon, her kontrol için pozitif/negatif
   örnek (sahte satırlar), determinizm, sürüm hash'i, satir_hash çakışması.
B) MySQL gerektirenler: kalite tabloları yoksa transaction DIŞINDA kurulur (DDL
   örtük commit yapar). Sahte klinik_bulgular satırları tek transaction'da eklenir,
   işaretler aynı bağlantıda yazılır, sonra ROLLBACK. JSONL ve --referans modları
   tabloya yazmaz. Sonda klinik_bulgular, kalite tabloları, review_status dağılımı
   ve hoca tabloları başlangıçtakiyle karşılaştırılır.
"""

import hashlib
import json
import random
import sys
import tempfile
from pathlib import Path

from src.data_quality import (
    CHECKS,
    REFERENCE_LABEL,
    Options,
    QualityError,
    SourceContext,
    check_duplicates,
    check_group_consistency,
    check_numeric,
    check_row_consistency,
    check_source,
    config_payload,
    control_version,
    ensure_schema,
    hash_collisions,
    norm_mudahale,
    norm_text,
    norm_unit,
    norm_value,
    parse_numbers,
    run_checks,
    run_quality,
    schema_ready,
)

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def make_row(**fields):
    row = {"makale_id": "M1", "document_id": "D1", "chunk_id": "C1", "kanit_sayfa": 1, "kanit_turu": "metin",
           "kanit_cumle": "metin", "cikarma_yontemi": "manuel", "id": None}
    row.update(fields)
    row["satir_hash"] = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return row


def codes(flags, code=None):
    return [f for f in flags if code is None or f["kontrol_kodu"] == code]


def has(flags, code, level=None):
    return any(f["kontrol_kodu"] == code and (level is None or f["seviye"] == level) for f in flags)


# ---------------------------------------------------------------------------
# A) Veritabanı gerektirmeyenler
# ---------------------------------------------------------------------------

def test_normalization():
    check("norm_text: İ/ı, boşluk, markdown", "ilaç tedavisi ılık", norm_text("  **İLAÇ**   Tedavisi ılık. "))
    check("eşanlamlı: PCI", "pci", norm_mudahale("Percutaneous Coronary Intervention"))
    check("eşanlamlı: NOAC = DOAC", ("doac", "doac"), (norm_mudahale("NOACs"), norm_mudahale("direct oral anticoagulants")))
    check("eşanlamlı: beta-blocker yazımları", {"beta-blocker"},
          {norm_mudahale(x) for x in ("Beta-Blockers", "beta blocker", "β-blocker", "betabloker")})
    check("eşanlamlı: LAAO, CABG", ("laao", "cabg"),
          (norm_mudahale("left atrial appendage occlusion"), norm_mudahale("Coronary artery bypass grafting")))
    check("eşanlamlı değil: apixaban ≠ doac", "apixaban", norm_mudahale("Apixaban"))
    check("birim: μg/day = mcg/d", norm_unit("mcg/d"), norm_unit("μg/day"))
    check("birim: percent -> %", "%", norm_unit("percent"))
    check("değer: '10mg' = '10 mg', '79 %' = '79%'", (norm_value("10 mg"), "79%"), (norm_value("10mg"), norm_value("79 %")))
    check("değer: ondalık virgül", "0.82", norm_value("0,82"))
    nums = parse_numbers("0.682–0.889")
    check("sayı: tireli aralık negatif değil", [0.682, 0.889], [n["value"] for n in nums])
    check("sayı: Unicode eksi", [-0.5], [n["value"] for n in parse_numbers("−0.5")])
    check("sayı: boşluklu eksi ('− 4.4 to − 1.8')", [-4.4, -1.8], [n["value"] for n in parse_numbers("− 4.4 to − 1.8")])
    check("sayı: boşluklu tire aralık ('0.5 - 0.9')", [0.5, 0.9], [n["value"] for n in parse_numbers("0.5 - 0.9")])
    check("ci_ters: negatif GA aralığı yanlış alarm vermez", False,
          has(check_numeric(make_row(deger="absolute difference − 3.0%", ci95="− 4.4 to − 1.8")), "ci_ters"))
    check("sayı: 1,234 belirsiz", True, parse_numbers("1,234")[0]["ambiguous"])
    check("sayı: p<001 baştaki sıfır", True, parse_numbers("p<001")[0]["leading_zero"])
    check("sayı: yüzde", (79.0, True), (parse_numbers("79 %")[0]["value"], parse_numbers("79 %")[0]["percent"]))


def test_numeric():
    cases = [
        ("yuzde_100_ustu", "hata", make_row(deger="120%", sonuc_olcutu_tr="sensitivity"), make_row(deger="79%", sonuc_olcutu_tr="sensitivity")),
        ("yuzde_100_ustu", "uyari", make_row(deger="150%"), make_row(deger="15%")),
        ("p_aralik_disi", "hata", make_row(p="1.5"), make_row(p="0.03")),
        ("p_bicim_supheli", "uyari", make_row(p="p<001"), make_row(p="p<0.001")),
        ("ci_ters", "hata", make_row(ci95="0.9-0.5"), make_row(ci95="0.5–0.9")),
        ("deger_ci_disinda", "uyari", make_row(deger="1.2", unit="x", ci95="0.5-0.9"), make_row(deger="0.7", unit="x", ci95="0.5-0.9")),
        ("oran_sifir_veya_negatif", "hata", make_row(etki_turu="HR", deger="HR 0"), make_row(etki_turu="HR", deger="HR 0.8")),
        ("auc_aralik_disi", "hata", make_row(etki_turu="AUC / C-istatistigi", deger="AUC=1.2"),
         make_row(etki_turu="AUC / C-istatistigi", deger="cut-off 2.82; sensitivity 79%; AUC=0.821")),
        ("negatif_sayi", "uyari", make_row(deger="-5", unit="mmHg"), make_row(deger="-5", unit="mmHg", etki_turu="ortalama fark")),
        ("negatif_sayi", "uyari", make_row(hasta_sayisi="0"), make_row(hasta_sayisi="12")),
        ("deger_birimsiz", "bilgi", make_row(deger="12"), make_row(deger="12", unit="mg")),
        ("deger_birimsiz", "uyari", make_row(deger="12", etki_turu="ortalama fark"), make_row(deger="12", etki_turu="HR")),
        ("doz_birimsiz", "uyari", make_row(doz="10"), make_row(doz="5 μg/day")),
        ("sayi_ayristirilamadi", "bilgi", make_row(deger="high"), make_row(deger="12 mg")),
        ("ondalik_belirsiz", "bilgi", make_row(deger="1,234 mg"), make_row(deger="1,23 mg")),
    ]
    for code, level, positive, negative in cases:
        check(f"{code} [{level}]: pozitif", True, has(check_numeric(positive), code, level))
        check(f"{code} [{level}]: negatif", False, has(check_numeric(negative), code, level))


def test_duplicates():
    a = make_row(mudahale_en="Beta-Blockers", deger="79 %", hasta_grubu="AF patients", kanit_sayfa=3)
    b = make_row(mudahale_en="beta blocker", deger="79%", hasta_grubu=" af  patients", kanit_sayfa=5)
    c = make_row(mudahale_en="beta-blocker", deger="79%", hasta_grubu="AF patients", kanit_sayfa=4, kanit_turu="tablo")
    other = make_row(mudahale_en="beta-blocker", deger="81%", hasta_grubu="AF patients", kanit_sayfa=6)
    m1, m2 = make_row(mudahale_en="Aspirin", kanit_sayfa=2), make_row(mudahale_en="acetylsalicylic acid", kanit_sayfa=7)
    flags = check_duplicates([a, b, c, other, m1, m2])
    same = codes(flags, "tekrar_ayni_bilgi")
    check("tekrar: normalize anahtarla 3 satır bir grupta", {a["satir_hash"], b["satir_hash"], c["satir_hash"]},
          {f["satir_hash"] for f in same})
    check("tekrar: farklı değer gruba girmez", False, other["satir_hash"] in {f["satir_hash"] for f in same})
    rep = next(f for f in same if f["seviye"] == "bilgi")
    check("tekrar: temsilci metin + en küçük sayfa (bilgi), diğerleri uyarı", (a["satir_hash"], 2),
          (rep["satir_hash"], sum(f["seviye"] == "uyari" for f in same)))
    check("tekrar: tek grup_id", 1, len({f["grup_id"] for f in same}))
    check("tekrar_metin_tablo", True, has(flags, "tekrar_metin_tablo", "bilgi"))
    check("tekrar_farkli_sayfa", True, has(flags, "tekrar_farkli_sayfa", "bilgi"))
    check("tekrar_yalniz_mudahale (aspirin = acetylsalicylic acid)", {m1["satir_hash"], m2["satir_hash"]},
          {f["satir_hash"] for f in codes(flags, "tekrar_yalniz_mudahale")})
    check("tekrar_yalniz_mudahale yalnızca bilgi", {"bilgi"}, {f["seviye"] for f in codes(flags, "tekrar_yalniz_mudahale")})
    single = check_duplicates([a, other])
    check("tekrar: negatif (tek üye)", False, has(single, "tekrar_ayni_bilgi"))

    s1 = make_row(mudahale_en="statin", deger="20%", hasta_grubu="patients with atrial fibrillation")
    s2 = make_row(mudahale_en="statin", deger="20%", hasta_grubu="patients with atrial fibrillation aged")
    s3 = make_row(mudahale_en="statin", deger="20%", hasta_grubu="children undergoing surgery")
    benzer = check_duplicates([s1, s2, s3])
    check("tekrar_benzer (Jaccard ≥ 0.8): pozitif", {s1["satir_hash"], s2["satir_hash"]},
          {f["satir_hash"] for f in codes(benzer, "tekrar_benzer")})
    check("tekrar_benzer: yalnızca bilgi", {"bilgi"}, {f["seviye"] for f in codes(benzer, "tekrar_benzer")})
    no_emb = check_duplicates([s1, s3])
    with_emb = check_duplicates([s1, s3], embedder=lambda texts: [[1.0, 0.0] for _ in texts])
    check("tekrar_benzer embedding: yalnız --embedding ile ve bilgi", (False, True, {"bilgi"}),
          (has(no_emb, "tekrar_benzer"), has(with_emb, "tekrar_benzer"),
           {f["seviye"] for f in codes(with_emb, "tekrar_benzer")}))


def test_consistency():
    d1 = make_row(mudahale_en="Metoprolol", doz="50 mg", hasta_grubu="HF")
    d2 = make_row(mudahale_en="metoprolol", doz="100 mg", hasta_grubu="HF")
    d3 = make_row(mudahale_en="metoprolol", doz="50mg", hasta_grubu="HF", kanit_sayfa=9)
    flags = check_group_consistency([d1, d2, d3])
    check("farkli_doz: pozitif (50 mg / 100 mg)", True, has(flags, "farkli_doz", "uyari"))
    check("farkli_doz: negatif (50 mg = 50mg)", False, has(check_group_consistency([d1, d3]), "farkli_doz"))
    n1, n2 = make_row(hasta_grubu="AF patients", hasta_sayisi=120), make_row(hasta_grubu="af patients", hasta_sayisi=95)
    check("farkli_hasta_sayisi: pozitif", True, has(check_group_consistency([n1, n2]), "farkli_hasta_sayisi", "uyari"))
    check("farkli_hasta_sayisi: negatif", False,
          has(check_group_consistency([n1, make_row(hasta_grubu="AF patients", hasta_sayisi=120, kanit_sayfa=4)]),
              "farkli_hasta_sayisi"))
    v1 = make_row(mudahale_en="PCI", hasta_grubu="STEMI", sonuc_olcutu_tr="mortalite", deger="5%")
    v2 = make_row(mudahale_en="percutaneous coronary intervention", hasta_grubu="STEMI", sonuc_olcutu_tr="Mortalite", deger="7%")
    check("farkli_deger: pozitif (bilgi)", True, has(check_group_consistency([v1, v2]), "farkli_deger", "bilgi"))
    check("farkli_deger: sonuç yoksa negatif", False,
          has(check_group_consistency([make_row(mudahale_en="PCI", deger="5%"), make_row(mudahale_en="PCI", deger="7%")]),
              "farkli_deger"))

    def note(**fields):
        base = {f: None for f in ("drug", "procedure", "diagnostic_method", "clinical_outcome")}
        base.update(fields)
        return json.dumps({"extracted_fields": base, "source_language": "en",
                           "otomatik_alanlar": {"mudahale_turu": f"otomatik ({next(k for k, v in fields.items() if v)} alanından)"}})
    ok = make_row(cikarma_yontemi="rag", mudahale_en="ECG", mudahale_turu="tanı testi", taslak_notu=note(diagnostic_method="ECG"))
    bad = make_row(cikarma_yontemi="rag", mudahale_en="ECG", mudahale_turu="tedavi", taslak_notu=note(diagnostic_method="ECG"))
    wrong_value = make_row(cikarma_yontemi="rag", mudahale_en="PCI", mudahale_turu="tedavi", taslak_notu=note(procedure="CABG"))
    check("mudahale_turu_kaynak_uyusmuyor: pozitif (yöntem ama 'tedavi')", True,
          has(check_row_consistency(bad), "mudahale_turu_kaynak_uyusmuyor", "hata"))
    check("mudahale_turu_kaynak_uyusmuyor: pozitif (değer farklı)", True,
          has(check_row_consistency(wrong_value), "mudahale_turu_kaynak_uyusmuyor", "hata"))
    check("mudahale_turu_kaynak_uyusmuyor: negatif", False, has(check_row_consistency(ok), "mudahale_turu_kaynak_uyusmuyor"))
    check("taslak_notu_okunamadi: pozitif (rag, JSON değil)", True,
          has(check_row_consistency(make_row(cikarma_yontemi="rag", taslak_notu="düz metin")), "taslak_notu_okunamadi", "uyari"))
    check("taslak_notu_okunamadi: negatif (rag dışı satır)", False,
          has(check_row_consistency(make_row(cikarma_yontemi="manuel", taslak_notu="düz metin")), "taslak_notu_okunamadi"))
    tr_note = json.loads(note(drug="aspirin"))
    tr_note["source_language"] = "tr"
    dil = make_row(cikarma_yontemi="rag", mudahale_en="aspirin", mudahale_turu="tedavi", taslak_notu=json.dumps(tr_note))
    check("dil_uyusmuyor: pozitif (bilgi)", True, has(check_row_consistency(dil), "dil_uyusmuyor", "bilgi"))
    check("dil_uyusmuyor: negatif", False, has(check_row_consistency(ok), "dil_uyusmuyor"))


def test_source():
    ctx = SourceContext(belgeler={"D1": "v2"}, makale_by_doc={"D1": "M1"},
                        chunks={"C1": ("D1", 1, "Beta blockers reduce heart rate.", "v2"),
                                "C_OLD": ("D1", 1, "Beta blockers reduce heart rate.", "v1"),
                                "C_OTHER": ("D9", 1, "x", "v2")}, validation=None)
    good = make_row(kanit_cumle="Beta blockers  reduce heart rate.", cikarma_yontemi="rag")
    check("kaynak: doğru satırda işaret yok", [], [f["kontrol_kodu"] for f in check_source(good, ctx)])
    cases = [
        ("kaynak_alani_bos", "hata", make_row(kanit_cumle="", cikarma_yontemi="rag", kanit_sayfa=None)),
        ("chunk_id_bos", "hata", make_row(chunk_id="", cikarma_yontemi="rag", kanit_cumle="Beta blockers reduce heart rate.")),
        ("chunk_id_bos", "uyari", make_row(chunk_id="", kanit_cumle="Beta blockers reduce heart rate.")),
        ("belge_yok", "hata", make_row(document_id="D404", chunk_id="", kanit_cumle="x")),
        ("chunk_yok", "hata", make_row(chunk_id="C404", kanit_cumle="x")),
        ("chunk_eski_surum", "uyari", make_row(chunk_id="C_OLD", kanit_cumle="Beta blockers reduce heart rate.")),
        ("chunk_belge_uyusmuyor", "hata", make_row(chunk_id="C_OTHER", kanit_cumle="x")),
        ("kanit_chunkta_yok", "hata", make_row(kanit_cumle="ACE inhibitors reduce mortality.")),
        ("kanit_sayfa_uyusmuyor", "hata", make_row(kanit_sayfa=2, kanit_cumle="Beta blockers reduce heart rate.")),
        ("makale_belge_uyusmuyor", "hata", make_row(makale_id="M2", kanit_cumle="Beta blockers reduce heart rate.")),
    ]
    for code, level, row in cases:
        check(f"{code} [{level}]: pozitif", True, has(check_source(row, ctx), code, level))


def test_determinism_and_version():
    rows = [make_row(mudahale_en=m, deger=d, hasta_grubu=h, doz=z, kanit_sayfa=p, p=pv)
            for m, d, h, z, p, pv in [("PCI", "5%", "STEMI", "", 1, "0.02"), ("pci", "5 %", "stemi", "", 2, "1.4"),
                                       ("aspirin", "12", "", "100 mg", 3, ""), ("Aspirin", "12", "", "75 mg", 4, ""),
                                       ("statin", "150%", "elderly", "10", 5, "p<001")]]
    first = run_checks(rows)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    check("determinizm: sıra değişince aynı işaretler", first, run_checks(shuffled))
    check("determinizm: bütün kontrol kodları tanımlı", True, all(f["kontrol_kodu"] in CHECKS for f in first))
    base = control_version()
    payload = config_payload()
    payload["esanlamli"] = {**payload["esanlamli"], "yeni": ("x",)}
    check("sürüm: eşanlamlı listesi hash'e dahil", True, control_version(payload=payload) != base)
    check("sürüm: embedding seçeneği hash'e dahil", True, control_version(embedding=True) != base)
    check("sürüm: aynı ayar aynı sürüm", base, control_version())


def test_collisions(tmp):
    from src.database import clinical_row_hash

    base_row = {"makale_id": "M1", "document_id": "D1", "kanit_sayfa": 1, "kanit_cumle": "q", "mudahale_en": "PCI",
                "mudahale_turu": "tedavi", "cikarma_yontemi": "rag", "review_status": "pending"}
    variants = [dict(base_row, taslak_notu=json.dumps({"extracted_fields": {"procedure": "PCI", "clinical_outcome": o}}))
                for o in ("mortality", "reinfarction")]
    run = Path(tmp) / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "would_save.jsonl").write_text("\n".join(json.dumps({"satir": v}) for v in variants + variants[:1]),
                                          encoding="utf-8")
    collisions = hash_collisions([], tmp)
    expected_hash = clinical_row_hash(variants[0], "rag")
    check("satir_hash çakışması: aynı hash, farklı İngilizce sonuç", {expected_hash: 2},
          {h: len(v) for h, v in collisions.items()})
    plain = Path(tmp) / "plain"
    (plain / "runs" / "r1").mkdir(parents=True)
    (plain / "runs" / "r1" / "would_save.jsonl").write_text(json.dumps({"satir": variants[0]}), encoding="utf-8")
    check("satir_hash çakışması: negatif", {}, hash_collisions([], plain))


# ---------------------------------------------------------------------------
# B) Veritabanı gerektirenler
# ---------------------------------------------------------------------------

PROTECTED = ("makaleler WHERE document_id IS NULL", "tani_tedavi", "sonuclar", "semptom_iliskileri",
             "icd10_who", "tkd_kurallari")


def db_state():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        state = {}
        for table in ("klinik_bulgular", "kalite_isaretleri", "kalite_calistirmalari", *PROTECTED):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            state[table] = cursor.fetchone()[0]
        cursor.execute("SELECT review_status, COUNT(*) FROM klinik_bulgular GROUP BY review_status ORDER BY review_status")
        state["review_status"] = [tuple(r) for r in cursor.fetchall()]
        cursor.execute("SELECT COALESCE(SUM(CRC32(CONCAT_WS('|', id, satir_hash, review_status))), 0) FROM klinik_bulgular")
        state["klinik_bulgular_parmak_izi"] = int(cursor.fetchone()[0])
        return state
    finally:
        conn.close()


def fake_rows(conn):
    """Gerçek belge/chunk'a bağlı sahte satırlar (yalnızca transaction içinde eklenir)."""
    from src.clinical_validation import norm_ws
    from src.database import KLINIK_INSERT_COLUMNS, clinical_row_hash

    cursor = conn.cursor()
    cursor.execute(
        "SELECT c.chunk_id, c.document_id, c.page_number, c.text, m.makale_id FROM pdf_chunks AS c "
        "JOIN pdf_belgeleri AS b ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
        "JOIN makaleler AS m ON m.document_id = c.document_id ORDER BY c.chunk_id")
    chunks = cursor.fetchall()
    cursor.close()
    (c1, doc, page1, text1, makale), = [chunks[0]]
    other = next(c for c in chunks if c[1] == doc and c[2] != page1)
    c2, _, page2, text2, _ = other
    q1, q2 = norm_ws(text1)[:80], norm_ws(text2)[:80]

    def note(**fields):
        base = {f: None for f in ("drug", "procedure", "diagnostic_method", "clinical_outcome")}
        base.update(fields)
        return json.dumps({"extracted_fields": base, "source_language": "en"})

    common = dict(makale_id=makale, document_id=doc, kanit_turu="metin", cikarma_yontemi="rag",
                  review_status="pending", mudahale_turu="tedavi", hasta_grubu="AF patients")
    specs = {
        "tekrar_1": dict(common, chunk_id=c1, kanit_sayfa=page1, kanit_cumle=q1, mudahale_en="Beta-Blockers",
                         doz="10mg", deger="79 %", taslak_notu=note(drug="Beta-Blockers")),
        "tekrar_2": dict(common, chunk_id=c2, kanit_sayfa=page2, kanit_cumle=q2, mudahale_en="beta blocker",
                         doz="10 mg", deger="79%", taslak_notu=note(drug="beta blocker")),
        "doz_farkli": dict(common, chunk_id=c1, kanit_sayfa=page1, kanit_cumle=q1[:60], mudahale_en="beta-blocker",
                           doz="5 mg", deger="40%", taslak_notu=note(drug="beta-blocker")),
        "kanit_yok": dict(common, chunk_id=c1, kanit_sayfa=page2, kanit_cumle="Bu cümle chunk'ta geçmiyor.",
                          mudahale_en="aspirin", taslak_notu=note(drug="aspirin")),
        "tur_uyusmuyor": dict(common, chunk_id=c1, kanit_sayfa=page1, kanit_cumle=q1[:50], mudahale_en="ECG",
                              taslak_notu=note(diagnostic_method="ECG")),
        "not_bozuk": dict(common, chunk_id=c1, kanit_sayfa=page1, kanit_cumle=q1[:40], mudahale_en="warfarin",
                          taslak_notu="json değil"),
    }
    rows = {}
    for key, spec in specs.items():
        row = {c: spec.get(c) for c in KLINIK_INSERT_COLUMNS}
        row["satir_hash"] = clinical_row_hash(row, "rag")
        rows[key] = row
    return rows


def insert_rows(conn, rows, order):
    from src.database import KLINIK_INSERT_COLUMNS

    cursor = conn.cursor()
    for key in order:
        row = rows[key]
        cursor.execute(f"INSERT INTO klinik_bulgular ({', '.join(KLINIK_INSERT_COLUMNS)}) VALUES "
                       f"({', '.join(['%s'] * len(KLINIK_INSERT_COLUMNS))})", [row[c] for c in KLINIK_INSERT_COLUMNS])
    cursor.close()


def stored_flags(conn, version):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT satir_hash, kontrol_kodu, kategori, seviye, grup_id, aciklama FROM kalite_isaretleri "
                   "WHERE kontrol_surumu = %s ORDER BY satir_hash, kontrol_kodu, grup_id", (version,))
    rows = cursor.fetchall()
    cursor.close()
    return rows


def test_database(tmp):
    from src.database import get_connection

    conn = get_connection()
    try:
        if not schema_ready(conn):
            ensure_schema(conn)  # transaction dışında; kalıcı şema eklemesi (onaylı)
        check("kalite tabloları kurulu", True, schema_ready(conn))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME IN ('v_kalite_ozet', 'v_kalite_bulgu')")
        check("kalite VIEW'ları kurulu", 2, cursor.fetchone()[0])
        cursor.close()
    finally:
        conn.close()

    before = db_state()
    version = control_version()
    runs = []
    for order in (["tekrar_1", "tekrar_2", "doz_farkli", "kanit_yok", "tur_uyusmuyor", "not_bozuk"],
                  ["not_bozuk", "tur_uyusmuyor", "kanit_yok", "doz_farkli", "tekrar_2", "tekrar_1"]):
        conn = get_connection()
        conn.autocommit = False
        conn.start_transaction()
        try:
            rows = fake_rows(conn)
            insert_rows(conn, rows, order)
            summary, flags, _ = run_quality(Options(rapor_dir=Path(tmp) / "rapor"), connection=conn, out=lambda *_: None)
            first = stored_flags(conn, version)
            run_quality(Options(rapor_dir=Path(tmp) / "rapor"), connection=conn, out=lambda *_: None)
            second = stored_flags(conn, version)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM v_kalite_ozet WHERE kontrol_surumu = %s", (version,))
            view_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM klinik_bulgular AS b JOIN kalite_isaretleri AS q "
                           "ON q.klinik_bulgu_id = b.id AND q.satir_hash = b.satir_hash WHERE q.kontrol_surumu = %s",
                           (version,))
            linked = cursor.fetchone()[0]
            cursor.close()
            runs.append((rows, summary, first, second, view_count, linked))
        finally:
            conn.rollback()
            conn.close()

    rows, summary, first, second, view_count, linked = runs[0]
    by = {}
    for f in first:
        by.setdefault(f["satir_hash"], set()).add((f["kontrol_kodu"], f["seviye"]))
    h = {k: r["satir_hash"] for k, r in rows.items()}
    check("DB: işaretler yazıldı", True, summary["veritabanina_yazildi"] and len(first) > 0)
    check("DB: tekrar grubu (normalize, farklı sayfa)", True,
          ("tekrar_ayni_bilgi", "bilgi") in by[h["tekrar_1"]] | by[h["tekrar_2"]]
          and ("tekrar_ayni_bilgi", "uyari") in by[h["tekrar_1"]] | by[h["tekrar_2"]]
          and any(c == "tekrar_farkli_sayfa" for c, _ in by[h["tekrar_1"]]))
    check("DB: farkli_doz (10 mg / 5 mg)", True, ("farkli_doz", "uyari") in by[h["doz_farkli"]])
    check("DB: kanit_chunkta_yok + kanit_sayfa_uyusmuyor", True,
          {("kanit_chunkta_yok", "hata"), ("kanit_sayfa_uyusmuyor", "hata")} <= by[h["kanit_yok"]])
    check("DB: kanıtı chunk'ta olan satırda kanit_chunkta_yok yok", False,
          any(c == "kanit_chunkta_yok" for c, _ in by[h["tekrar_1"]]))
    check("DB: mudahale_turu_kaynak_uyusmuyor", True, ("mudahale_turu_kaynak_uyusmuyor", "hata") in by[h["tur_uyusmuyor"]])
    check("DB: taslak_notu_okunamadi", True, ("taslak_notu_okunamadi", "uyari") in by[h["not_bozuk"]])
    check("DB: işaretler klinik_bulgu_id ile bağlı", len(first), linked)
    check("DB: v_kalite_ozet işaretleri gösteriyor", len(first), view_count)
    check("DB: yeniden çalıştırma eskileri silip aynı sonucu yazar", first, second)
    other_first = runs[1][2]
    check("DB: ters ekleme sırası (farklı id'ler) -> aynı işaretler ve grup_id'ler", first, other_first)

    # JSONL modu: tabloya yazmaz; rapor üretir; çakışma görünür.
    jsonl = Path(tmp) / "would_save.jsonl"
    variants = [dict(rows["tekrar_1"], taslak_notu=json.dumps({"extracted_fields": {
        "drug": "Beta-Blockers", "procedure": None, "diagnostic_method": None, "clinical_outcome": o},
        "source_language": "en"})) for o in ("mortality", "stroke")]
    jsonl.write_text("\n".join(json.dumps({"satir": v}) for v in variants), encoding="utf-8")
    mid = db_state()
    summary, flags, _ = run_quality(Options(jsonl=[str(jsonl)], rapor_dir=Path(tmp) / "rapor"), out=lambda *_: None)
    check("JSONL: tabloya yazılmadı", (False, mid), (summary["veritabanina_yazildi"], db_state()))
    check("JSONL: satir_hash çakışması görünür", (1, True),
          (summary["hash_cakismasi"], any(f["kontrol_kodu"] == "hash_cakismasi_kayip_bulgu" for f in flags)))
    check("JSONL: rapor dosyaları", True,
          all((Path(summary["rapor_dir"]) / n).exists() for n in ("isaretler.csv", "isaretler.json", "gruplar.csv", "ozet.json")))
    offline, _, _ = run_quality(Options(jsonl=[str(jsonl)], offline=True, rapor_dir=Path(tmp) / "rapor"),
                                out=lambda *_: None)
    check("JSONL --offline: kaynak kontrolü yapılmadı", "yapılmadı (--offline)", offline["kaynak_kontrolu"])

    # --referans: yalnızca --dry-run; etiketli; tabloya yazmaz.
    try:
        run_quality(Options(referans=True, rapor_dir=Path(tmp) / "rapor"), out=lambda *_: None)
        got = "çalıştı"
    except QualityError:
        got = "reddedildi"
    check("--referans --dry-run olmadan reddedilir", "reddedildi", got)
    summary, flags, ref_rows = run_quality(Options(referans=True, dry_run=True, rapor_dir=Path(tmp) / "rapor"),
                                           out=lambda *_: None)
    csv_text = (Path(summary["rapor_dir"]) / "isaretler.csv").read_text(encoding="utf-8-sig")
    check("--referans: etiket özet ve raporda", True,
          summary.get("veri_etiketi") == REFERENCE_LABEL and REFERENCE_LABEL in csv_text
          and json.loads((Path(summary["rapor_dir"]) / "ozet.json").read_text(encoding="utf-8"))["veri_etiketi"] == REFERENCE_LABEL)
    check("--referans: tabloya yazılmadı", (False, mid), (summary["veritabanina_yazildi"], db_state()))
    check("--referans: satırlar okundu (bilgi)", "-", f"{len(ref_rows)} satır, {len(flags)} işaret", True)

    after = db_state()
    check("sonda: klinik_bulgular, kalite tabloları, review_status, hoca tabloları değişmedi", before, after)


def main():
    test_normalization()
    test_numeric()
    test_duplicates()
    test_consistency()
    test_source()
    test_determinism_and_version()
    with tempfile.TemporaryDirectory() as tmp:
        test_collisions(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<22}  {'Bulunan':<40}  Sonuç")
    print("-" * (width + 76))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:22]:<22}  {act[:40]:<40}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
