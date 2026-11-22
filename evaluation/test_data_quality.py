"""
Veri kalitesi işaretleri testleri.

Çalıştırma:
    python -m evaluation.test_data_quality

A) Veritabanı gerektirmeyenler: normalizasyon, her kontrol için pozitif/negatif
   örnek (sahte satırlar), determinizm, sürüm hash'i, satir_hash çakışması.
   Pilot satır kontrolleri (check_pilot_row) ve --pilot sürüm ayrımı da burada.
   Troponin tabloları (sentetik kayıtlar, bellekte): Total sütunu, p sütunu etkisi ve tanısal performans
   kayıtlarında iki grup varsayan kontroller yanlış alarm vermez; gerçek tutarsızlık yine yakalanır.
   Grup içi test satırları (karsilastirma_turu = 'grup_ici' ya da notsuz, PDF satırından tanınan) grup çifti
   kontrollerine ve _group_pairs'e girmez; karşı grubu comparator yazmak yine tablo_cift_tutarsiz.
   Ön deneme (Eozinofil, sentetik): <br> alt satırları, tablo içi başlık + birleşik sütunlar ve ortalama±SS (min–maks)
   kayıtları hata / çift işareti vermez, eksiksizlik 16/16; değer karışırsa tablo_cift_tutarsiz.
B) MySQL gerektirenler: kalite tabloları yoksa transaction DIŞINDA kurulur (DDL
   örtük commit yapar). Sahte klinik_bulgular satırları tek transaction'da eklenir,
   işaretler aynı bağlantıda yazılır, sonra ROLLBACK. JSONL ve --referans modları
   tabloya yazmaz. Sonda klinik_bulgular, kalite tabloları, review_status dağılımı
   ve hoca tabloları başlangıçtakiyle karşılaştırılır.
   Pilot: import_records(connection=conn) ile seçilmiş aktarım + metin kayıtları tek
   transaction'da yazılır, --pilot kontrolleri aynı bağlantıyla çalışır, sonra ROLLBACK.
"""

import hashlib
import json
import random
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from src.data_quality import (
    ALL_CHECKS,
    CHECKS,
    PILOT_CHECKS,
    PILOT_WARNING,
    REFERENCE_LABEL,
    Options,
    QualityError,
    SourceContext,
    check_duplicates,
    check_group_consistency,
    check_numeric,
    check_pilot_row,
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


def test_pilot_unit():
    """Pilot satır kontrolleri: pozitif/negatif örnekler (DB'siz, sahte sayfa metni)."""
    page = ("It was observed that the HCR was higher. (1.88%) and chronic event in 20 patients (5.37%). "
            "proportional to C-pep- in 7 patients **FIGURE 1:** ROC curve p<001 |HbA1c|7.83±1.2|6.23±1.2|<0.001|")
    source = {"pages": {1: page}}

    def pilot(**fields):
        base = dict(bilgi_turu="olcum", baglam="study_finding", kanit_turu="metin", cikarma_yontemi="manuel",
                    kanit_cumle="It was observed that the HCR was higher.", chunk_id=None, makale_id=None)
        base.update(fields)
        base.setdefault("klinik_bilgi_en", base["kanit_cumle"])
        return make_row(**base)

    def flags_of(row, src=source):
        return check_pilot_row(row, src)

    good = pilot()
    check("pilot: temiz metin satırında işaret yok", [], [f["kontrol_kodu"] for f in flags_of(good)])
    check("pilot_kanit_sayfada_yok [hata]: pozitif / negatif", (True, False),
          (has(flags_of(pilot(kanit_cumle="Bu cümle sayfada yok.")), "pilot_kanit_sayfada_yok", "hata"),
           has(flags_of(good), "pilot_kanit_sayfada_yok")))
    check("pilot_kanit_sayfada_yok: yanlış sayfa", True,
          has(flags_of(pilot(kanit_sayfa=2)), "pilot_kanit_sayfada_yok", "hata"))
    check("pilot_bilgi_kanitta_yok [hata]: pozitif / negatif", (True, False),
          (has(flags_of(pilot(klinik_bilgi_en="HCR is a marker")), "pilot_bilgi_kanitta_yok", "hata"),
           has(flags_of(pilot(klinik_bilgi_en="the HCR was higher")), "pilot_bilgi_kanitta_yok")))
    check("pilot_pdf_okunamadi [uyari]", True, has(flags_of(good, {"hata": "dosya yok"}), "pilot_pdf_okunamadi", "uyari"))
    for label, quote in (("parantezle başlıyor (#106)", "(1.88%) and chronic event in 20 patients (5.37%)."),
                         ("küçük harfle başlıyor", "proportional to C-pep- in 7 patients"),
                         ("tireyle bitiyor", "proportional to C-pep-")):
        check(f"pilot_sayfa_gecisi_supheli [uyari]: {label}", True,
              has(flags_of(pilot(kanit_cumle=quote, klinik_bilgi_en=quote)), "pilot_sayfa_gecisi_supheli", "uyari"))
    check("pilot_sayfa_gecisi_supheli: düzgün cümlede yok", False, has(flags_of(good), "pilot_sayfa_gecisi_supheli"))
    digit = "1,225 patients with AF included in the study."
    digit_flags = flags_of(pilot(kanit_cumle=digit, klinik_bilgi_en=digit))
    check("pilot_sayfa_gecisi_supheli: rakamla başlıyor ('1,225 patients…') -> bilgi, uyari değil", (True, False),
          (has(digit_flags, "pilot_sayfa_gecisi_supheli", "bilgi"), has(digit_flags, "pilot_sayfa_gecisi_supheli", "uyari")))
    check("pilot_sayfa_gecisi_supheli: noktalamayla başlıyor ('; and …') -> uyari", True,
          has(flags_of(pilot(kanit_cumle="; and in 20 patients.", klinik_bilgi_en="in 20 patients")),
              "pilot_sayfa_gecisi_supheli", "uyari"))
    nopunct = pilot(kanit_cumle="It was observed that the HCR was higher", klinik_bilgi_en="the HCR")
    check("pilot_sayfa_gecisi_supheli [bilgi]: büyük harf, noktalamasız son", True,
          has(flags_of(nopunct), "pilot_sayfa_gecisi_supheli", "bilgi"))
    table = pilot(kanit_turu="tablo", kanit_cumle="|HbA1c|7.83±1.2|6.23±1.2|<0.001|", klinik_bilgi_en="HbA1c")
    check("pilot_sayfa_gecisi_supheli: tablo satırında aranmaz", False, has(flags_of(table), "pilot_sayfa_gecisi_supheli"))
    fig = "in 7 patients **FIGURE 1:** ROC curve"
    check("pilot_kanitta_sekil_tablo [uyari]: pozitif / negatif", (True, False),
          (has(flags_of(pilot(kanit_cumle=fig, klinik_bilgi_en="ROC curve")), "pilot_kanitta_sekil_tablo", "uyari"),
           has(flags_of(good), "pilot_kanitta_sekil_tablo")))
    check("kaynak_p_bicim_bozuk [bilgi]: 'p<001' / 'p<0.001'", (True, False),
          (has(flags_of(pilot(kanit_cumle="ROC curve p<001", klinik_bilgi_en="ROC")), "kaynak_p_bicim_bozuk", "bilgi"),
           has(flags_of(pilot(kanit_cumle="ROC curve p<0.001", klinik_bilgi_en="ROC")), "kaynak_p_bicim_bozuk")))
    note = lambda n: json.dumps({"kaynak": "pilot-tablo-v1", "grup_n": n})  # noqa: E731
    check("pilot_yuzde_uyumsuz [uyari]: 119/372 ≠ %35.00", True,
          has(flags_of(table | {"deger": "119 (35.00)", "taslak_notu": note(372)}), "pilot_yuzde_uyumsuz", "uyari"))
    check("pilot_yuzde_uyumsuz: kesme (31.98) ve yuvarlama (31.99) tolere edilir", (False, False),
          (has(flags_of(table | {"deger": "119 (31.98)", "taslak_notu": note(372)}), "pilot_yuzde_uyumsuz"),
           has(flags_of(table | {"deger": "119 (31.99)", "taslak_notu": note(372)}), "pilot_yuzde_uyumsuz")))
    check("kaynak_ci_p_celiski [bilgi]: CI 1'i kapsıyor, p=0.002", True,
          has(flags_of(table | {"etki_turu": "OR", "deger": "1.254", "ci95": "0.892-1.372", "p": "0.002"}),
              "kaynak_ci_p_celiski", "bilgi"))
    check("kaynak_ci_p_celiski: CI 1'i kapsamıyor, p<0.001 -> yok", False,
          has(flags_of(table | {"etki_turu": "OR", "deger": "3.124", "ci95": "2.213-4.310", "p": "<0.001"}),
              "kaynak_ci_p_celiski"))
    check("kaynak_ci_p_celiski [bilgi]: CI 1'i kapsamıyor, p=0.679", True,
          has(flags_of(table | {"etki_turu": "OR", "deger": "0.852", "ci95": "0.542-0.915", "p": "0.679"}),
              "kaynak_ci_p_celiski", "bilgi"))
    check("deger_ci_disinda [uyari]: yapay CI dışı OR", (True, False),
          (has(check_numeric(make_row(etki_turu="OR", deger="2.500", ci95="0.542-0.915")), "deger_ci_disinda", "uyari"),
           has(check_numeric(make_row(etki_turu="OR", deger="0.852", ci95="0.542-0.915")), "deger_ci_disinda")))
    check("pilot_baglam_tablo [uyari]: pozitif / negatif", (True, False),
          (has(flags_of(table | {"baglam": "literature_background"}), "pilot_baglam_tablo", "uyari"),
           has(flags_of(table), "pilot_baglam_tablo")))
    med = table | {"bilgi_turu": "ilac", "deger": "119 (31.98)", "taslak_notu": note(372)}
    check("pilot_ilac_doz_oran [uyari]: pozitif / negatif", (True, False),
          (has(flags_of(med | {"doz": "100 mg"}), "pilot_ilac_doz_oran", "uyari"), has(flags_of(med), "pilot_ilac_doz_oran")))
    check("pilot_tablo_bos_deger [uyari]: boş / boşluk / rakamsız ('n (%)') pozitif; dolu tablo ve metin negatif",
          (True, True, True, False, False),
          (has(flags_of(table | {"deger": None}), "pilot_tablo_bos_deger", "uyari"),
           has(flags_of(table | {"deger": "  "}), "pilot_tablo_bos_deger", "uyari"),
           has(flags_of(table | {"deger": "n (%)"}), "pilot_tablo_bos_deger", "uyari"),
           has(flags_of(table | {"deger": "7.83±1.2"}), "pilot_tablo_bos_deger"),
           has(flags_of(good), "pilot_tablo_bos_deger")))
    check("pilot: bütün kodlar PILOT_CHECKS'te, CHECKS ile çakışmıyor", (True, set()),
          (all(f["kontrol_kodu"] in ALL_CHECKS for f in flags_of(pilot(kanit_cumle="x-", klinik_bilgi_en="y"))),
           set(PILOT_CHECKS) & set(CHECKS)))
    check("sürüm: --pilot olmadan hash ve payload değişmedi", (False, True),
          ("pilot" in config_payload(), control_version() == control_version(pilot=False)))
    check("sürüm: --pilot ayrı sürüm", True, control_version(pilot=True) != control_version())

    # Çok parçalı kanıt (taslak_notu.kanit_parcalari): her parça kendi sayfasında.
    multi_pages = {6: "Intro. HCR rate is inversely proportional to C-pep-", 7: "tide level. Next sentence.",
                   8: "Text **FIGURE 1:** caption."}
    parts = [{"sayfa": 6, "metin": "HCR rate is inversely proportional to C-pep-"}, {"sayfa": 7, "metin": "tide level."}]

    def parted(parts=parts, **fields):
        base = dict(kanit_cumle=parts[0]["metin"], klinik_bilgi_en="inversely proportional", kanit_sayfa=6,
                    kanit_sayfa_bitis=7, birlestirme="sayfa", taslak_notu=json.dumps({"kanit_parcalari": parts}))
        base.update(fields)
        return pilot(**base)

    resolved = flags_of(parted(), {"pages": multi_pages})
    check("çok parçalı: doğrulanan parçalar -> pilot_cok_parcali_cozuldu [bilgi]; 'C-pep-' sayfa geçişi uyarısı yok",
          ([("pilot_cok_parcali_cozuldu", "bilgi")]), [(f["kontrol_kodu"], f["seviye"]) for f in resolved])
    check("çok parçalı: tek parçalı 'C-pep-' (çözülmemiş) hâlâ uyari", True,
          has(flags_of(pilot(kanit_cumle=parts[0]["metin"], klinik_bilgi_en="inversely proportional", kanit_sayfa=6),
                       {"pages": multi_pages}), "pilot_sayfa_gecisi_supheli", "uyari"))
    check("çok parçalı: bilgi sonraki parçada olabilir", False,
          has(flags_of(parted(klinik_bilgi_en="tide level"), {"pages": multi_pages}), "pilot_bilgi_kanitta_yok"))
    for label, row, needle in (
            ("kanit_cumle ≠ ilk parça", parted(kanit_cumle=parts[0]["metin"] + " tide level."), "ilk parçayla aynı değil"),
            ("bitiş sayfası uyuşmuyor", parted(kanit_sayfa_bitis=6), "son parçanın sayfası"),
            ("parça sayfada yok", parted(taslak_notu=json.dumps({"kanit_parcalari": [parts[0], {"sayfa": 7, "metin": "yok."}]})),
             "s.7 parçası o sayfada birebir yok"),
            ("birlestirme yanlış", parted(birlestirme="ara_blok"), "parçalardan çıkan 'sayfa'"),
            ("yapı bozuk", parted(taslak_notu=json.dumps({"kanit_parcalari": "metin"})), "yapısı bozuk")):
        got = flags_of(row, {"pages": multi_pages})
        check(f"çok parçalı tutarsız ({label}) -> pilot_parca_tutarsiz [hata], çözüldü yok", (True, False, True),
              (has(got, "pilot_parca_tutarsiz", "hata"), has(got, "pilot_cok_parcali_cozuldu"),
               any(needle in f["aciklama"] for f in got if f["kontrol_kodu"] == "pilot_parca_tutarsiz")))
    boundary = pilot(kanit_cumle="proportional to C-pep-tide level.", klinik_bilgi_en="proportional", kanit_sayfa=6,
                     kanit_sayfa_bitis=7)
    check("parçasız iki sayfalı alıntı: sayfa sınırında aranır -> çözüldü, kanıt hatası yok", (True, False),
          (has(flags_of(boundary, {"pages": multi_pages}), "pilot_cok_parcali_cozuldu", "bilgi"),
           has(flags_of(boundary, {"pages": multi_pages}), "pilot_kanit_sayfada_yok")))
    check("parçasız iki sayfalı alıntı sınırda yoksa -> pilot_kanit_sayfada_yok [hata]", True,
          has(flags_of(dict(boundary, kanit_cumle="proportional to C-peptide level."), {"pages": multi_pages}),
              "pilot_kanit_sayfada_yok", "hata"))


TROPONIN_PDF = Path("data/pdfs/who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf")


def test_troponin_tables():
    """Troponin tabloları (sentetik kayıt: her tablo satırı kanıt = ham satır; DB'siz, bellekte): iki grup varsayan
    kontroller Total sütununda, p sütunu etkisinde ve tanısal performans kayıtlarında yanlış alarm vermez."""
    import src.pilot_tablo as pt
    from src.data_quality import run_pilot_checks
    from src.pilot_clinical import pdf_pages

    if not TROPONIN_PDF.exists():
        check("troponin: PDF var (yoksa bu bölüm sınanmaz)", True, False)
        return
    doc, pages = pdf_pages(TROPONIN_PDF)
    tables = pt.parse_tables(pages)
    hams = list(dict.fromkeys((t["sayfa"], r["ham"]) for t in tables for r in t["satirlar"]))
    records = [{"v2_sira": i, "kanit_turu": "tablo", "sayfa": s, "kanit_cumle": h, "bilgi_turu": "olcum",
                "baglam": "study_finding", "klinik_bilgi_en": h} for i, (s, h) in enumerate(hams, 1)]
    transfer = pt.enrich(records, pages, doc["document_id"])["aktarim"]
    rows = [make_row(**{k: r.get(k) for k in ("bilgi_turu", "baglam", "klinik_bilgi_en", "kanit_cumle", "kanit_turu",
                                             *pt.STRUCTURED_FIELDS)},
                     document_id=doc["document_id"], kanit_sayfa=r["sayfa"], chunk_id=None, makale_id=None, v=i)
            for i, r in enumerate(transfer)]
    sources = {doc["document_id"]: {"dosya": doc["filename"], "pages": pages, "tables": pt.parse_tables(pages)}}
    flags, report = run_pilot_checks(rows, sources)
    flags += run_checks(rows, pilot=True)
    got = Counter(f"{f['kontrol_kodu']} [{f['seviye']}]" for f in flags)
    false_alarms = ("tablo_cift_eksik", "tablo_cift_tutarsiz", "pilot_yuzde_uyumsuz", "pilot_tablo_bos_deger",
                    "kategorik_p_yeniden_uretilemedi", "surekli_p_supheli", "tekrar_ayni_bilgi", "tekrar_yalniz_mudahale")
    check("troponin: 84 kayıt; iki grup / Total / tanısal yanlış alarmı yok", (84, {}),
          (len(rows), {k: v for k, v in got.items() if k.split(" ")[0] in false_alarms}))
    check("troponin: kalan işaretler yalnız genel sayısal kontroller (birimsiz değer, gerçek negatif Q1)",
          {"deger_birimsiz [uyari]": 27, "negatif_sayi [uyari]": 2}, dict(got))
    item = report["eksiksizlik"][0]
    check("troponin: eksiksizlik 27/27, eksik grup/analiz yok", (27, 27, 0),
          (item["pdf_tablo_satiri"], item["temsil_edilen"], len(report["eksik_grup_analiz"])))
    # Negatif örnekler: kontroller hâlâ çalışıyor (Total'siz çift p farkı, tanısal kaydın yanlış değeri).
    pair = [r for r in rows if r["klinik_bilgi_en"] == "Age" and r.get("hasta_grubu") in ("ACS Absent", "ACS Present")]
    broken = [dict(r, p="0.500") if r["hasta_grubu"] == "ACS Present" else r for r in pair]
    sens = next(r for r in rows if r["klinik_bilgi_en"] == "Sensitivity")
    bad_rows = [r for r in rows if r not in pair and r is not sens] + broken + [dict(sens, deger="99.9%")]
    bad, _ = run_pilot_checks(bad_rows, sources)
    check("troponin negatif: gerçek grup çiftinde p farkı ve tanısal değer farkı -> tablo_cift_tutarsiz [hata]",
          (True, True),
          (any(f["kontrol_kodu"] == "tablo_cift_tutarsiz" and "çiftte p değerleri farklı" in f["aciklama"] for f in bad),
           any(f["kontrol_kodu"] == "tablo_cift_tutarsiz" and "deger '99.9%'" in f["aciklama"] for f in bad)))
    # Grup içi test satırları (P value for intra groups, Pairwise A-B / A-C / B-C): gruplar arası çift değil.
    def within(r):
        return (json.loads(r["taslak_notu"]) or {}).get("karsilastirma_turu") == "grup_ici"

    inner = [r for r in rows if within(r)]
    check("troponin grup içi: 12 kayıt (4 satır x Total + 2 grup), comparator yok, çift işareti yok",
          (12, {None}, []),
          (len(inner), {r.get("comparator") for r in inner},
           [f["kontrol_kodu"] for f in flags if f["satir_hash"] in {r["satir_hash"] for r in inner}
            and f["kontrol_kodu"] in ("tablo_cift_eksik", "tablo_cift_tutarsiz")]))
    # İnsan doğrulamasındaki biçim: comparator boş ama taslak_notu'da karsilastirma_turu yok -> PDF satırından
    # grup içi tanınır, yine çift işareti yok.
    def strip_note(r):
        note = json.loads(r["taslak_notu"])
        note.pop("karsilastirma_turu", None)
        note.pop("grup_ici_nedeni", None)
        return dict(r, taslak_notu=json.dumps(note, ensure_ascii=False))

    plain = [strip_note(r) if within(r) else r for r in rows]
    plain_flags, _ = run_pilot_checks(plain, sources)
    check("troponin grup içi: karsilastirma_turu notu olmadan da (PDF satırından) çift işareti yok", [],
          [f["aciklama"][:80] for f in plain_flags if f["kontrol_kodu"] in ("tablo_cift_eksik", "tablo_cift_tutarsiz")])
    # Negatif: eski çıktıdaki gibi grup içi satıra karşı grubu comparator yazmak hâlâ hata (PDF'e göre comparator yok).
    inner_hashes = {r["satir_hash"] for r in inner}
    old = [dict(r, comparator="ACS Present" if r.get("hasta_grubu") == "ACS Absent" else "ACS Absent")
           if r["satir_hash"] in inner_hashes and r.get("hasta_grubu") in ("ACS Absent", "ACS Present") else r
           for r in plain]
    old_flags, _ = run_pilot_checks(old, sources)
    wrong = [f for f in old_flags if f["kontrol_kodu"] == "tablo_cift_tutarsiz"]
    check("troponin grup içi negatif: karşı grup comparator olarak yazılırsa tablo_cift_tutarsiz [hata] (8 kayıt, "
          "\"comparator ≠ PDF 'None'\"; karşılıklılık hatası yok)", (8, True, False),
          (len(wrong), all("≠ PDF 'None'" in f["aciklama"] for f in wrong),
           any("karşılıklı değil" in f["aciklama"] for f in wrong)))
    # _group_pairs: grup içi kayıtlar comparator'ları karşılıklı olsa bile istatistik çiftine girmez.
    from src.data_quality import _group_pairs
    untagged = [dict(r, p="<0.001") for r in old if r["satir_hash"] in inner_hashes]
    tagged = [dict(r, taslak_notu=json.dumps(dict(json.loads(r["taslak_notu"]), karsilastirma_turu="grup_ici"),
                                             ensure_ascii=False)) for r in untagged]
    check("troponin grup içi: karsilastirma_turu = grup_ici kayıtlar _group_pairs'e girmez (etiketsiz 4 çift olurdu)",
          (4, 0), (len(list(_group_pairs(untagged))), len(list(_group_pairs(tagged)))))


EOZINOFIL_PDF = Path("data/pdfs/TK-cardiovascular37-2-1.pdf")


def test_on_deneme_tables():
    """Ön deneme düzeltmeleri (Eozinofil, sentetik kayıtlar, bellekte): bölüm adsız <br> alt satırları (T2 Male /
    Female), tablo içindeki başlık + birleşik sütunlar (T3 B / OR / p) ve ortalama±SS (min–maks) (T1) kayıtları
    grup / analiz kontrollerinde yanlış alarm vermez; tablo eksiksizliği tam."""
    import src.pilot_tablo as pt
    from src.data_quality import run_pilot_checks
    from src.pilot_clinical import pdf_pages

    if not EOZINOFIL_PDF.exists():
        check("ön deneme: Eozinofil PDF var (yoksa bu bölüm sınanmaz)", True, False)
        return
    doc, pages = pdf_pages(EOZINOFIL_PDF)
    tables = pt.parse_tables(pages)
    hams = list(dict.fromkeys((t["sayfa"], r["ham"]) for t in tables for r in t["satirlar"]))
    records = [{"v2_sira": i, "kanit_turu": "tablo", "sayfa": s, "kanit_cumle": h, "bilgi_turu": "olcum",
                "baglam": "study_finding", "klinik_bilgi_en": h} for i, (s, h) in enumerate(hams, 1)]
    transfer = pt.enrich(records, pages, doc["document_id"])["aktarim"]
    rows = [make_row(**{k: r.get(k) for k in ("bilgi_turu", "baglam", "klinik_bilgi_en", "kanit_cumle", "kanit_turu",
                                             *pt.STRUCTURED_FIELDS)},
                     document_id=doc["document_id"], kanit_sayfa=r["sayfa"], chunk_id=None, makale_id=None, v=i)
            for i, r in enumerate(transfer)]
    sources = {doc["document_id"]: {"dosya": doc["filename"], "pages": pages, "tables": tables}}
    flags, report = run_pilot_checks(rows, sources)
    flags += run_checks(rows, pilot=True)
    got = Counter(f"{f['kontrol_kodu']} [{f['seviye']}]" for f in flags)
    check("ön deneme Eozinofil: 42 kayıt; hata yok, grup / analiz çifti işareti yok",
          (42, [], []), (len(rows), [k for k in got if k.endswith("[hata]")], [k for k in got if k.startswith("tablo_cift")]))
    item = report["eksiksizlik"][0]
    check("ön deneme Eozinofil: eksiksizlik 16/16 (T1 11, T2 2, T3 3), eksik grup / analiz yok", (16, 16, 0),
          (item["pdf_tablo_satiri"], item["temsil_edilen"], len(report["eksik_grup_analiz"])))
    # Negatif: Male / Female değerleri karışırsa (eski okuma) tablo_cift_tutarsiz yakalar.
    swapped = [dict(r, deger="152 37") if r["klinik_bilgi_en"] == "Male" and r.get("hasta_grubu") == "Total" else r
               for r in rows]
    bad, _ = run_pilot_checks(swapped, sources)
    check("ön deneme Eozinofil negatif: Male Total değeri PDF'ten farklı -> tablo_cift_tutarsiz [hata]", True,
          any(f["kontrol_kodu"] == "tablo_cift_tutarsiz" and "deger '152 37'" in f["aciklama"] for f in bad))


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


PILOT_DIR = Path("data/review/pilot")
PILOT_PDF = Path("data/pdfs/TK-cardiovascular36-2-1.pdf")
PILOT_V32 = PILOT_DIR / "pilot_findings_v3_2.json"
PILOT_V2 = PILOT_DIR / "pilot_findings_v2.json"
# Metin kayıtları kanıta göre seçilir (dosyalar arası sıra numarası değişir). PDF yükleyici v4:
# v2 #7'nin "0.6820.889" kanıtı artık sayfada yok; güncel kayıtlar v3.2'den. Şekil yazılı eski tek
# parçalı kayıt (v2 #109) v4 sayfasında hâlâ birebir olduğu için şekil/tablo uyarısını sınamak için kalır.
PILOT_TEXTS = {
    "hcr_ozet": (PILOT_V32, lambda r: r["kanit_cumle"].startswith("It was observed that the HCR (3.41")),
    "roc_ozet": (PILOT_V32, lambda r: "HCR score" in r["kanit_cumle"] and "CI95=0.682-0.889" in r["kanit_cumle"]),
    "hasta_1225": (PILOT_V32, lambda r: r["kanit_cumle"].startswith("1,225 patients")),
    "roc_sonuc": (PILOT_V32, lambda r: r["kanit_cumle"].startswith(
        "In the ROC analysis, the cut-off value of the HCR value in patients experiencing")),
    "sekil_eski": (PILOT_V2, lambda r: "**FIGURE 1:**" in r["kanit_cumle"]),
    # Çok parçalı (v3.2): v2 #106 (s.3→5 '(1.88%)…'), #152 ('C-pep-' / 'tide level.'), C-peptide ROC (Figure arası).
    "parca_mezenter": (PILOT_V32, lambda r: "kanit_parcalari" in r and "mesenteric" in r["klinik_bilgi_en"]),
    "parca_cpep": (PILOT_V32, lambda r: "kanit_parcalari" in r and r["kanit_cumle"].endswith("C-pep-")),
    "parca_roc": (PILOT_V32, lambda r: "kanit_parcalari" in r and "AUC=0.692" in r["klinik_bilgi_en"]),
}
pilot_distribution = {}


def pilot_texts():
    cache = {}
    out = {}
    for name, (path, wanted) in PILOT_TEXTS.items():
        data = cache.setdefault(path, json.loads(path.read_text(encoding="utf-8")))
        matches = [r for r in data if wanted(r)]
        if not matches:
            raise AssertionError(f"test kaydı bulunamadı: {name} ({path.name})")
        out[name] = dict(matches[0])
    return out


def pilot_test_records():
    """Test kopyası: seçilmiş tablo aktarım kayıtları + birkaç metin kaydı, hepsi review_status='verified'.

    - HbA1c ve HCR: iki grup tam; Glucose: bir grup silinmiş; Female gender: n (%) çifti.
    - Table 2 Age: univariate aynen, multivariate OR'u yapay olarak GA dışına (2.500) çekilmiş.
    """
    transfer = json.loads((PILOT_DIR / "pilot_tablo_aktarim.json").read_text(encoding="utf-8"))
    picked = []
    for item in transfer:
        note = json.loads(item["taslak_notu"])
        key = (note["tablo_no"], note["degisken"])
        if key in ((1, "HbA1c"), (1, "HCR"), (1, "Female gender"), (2, "Age")):
            picked.append(dict(item))
        elif key == (1, "Glucose") and item["hasta_grubu"] == "Ischemic event":
            picked.append(dict(item))  # "No ischemic event" grubu bilerek yok
    for item in picked:
        if item.get("etki_turu") == "OR" and json.loads(item["taslak_notu"]).get("analiz") == "multivariate":
            item["deger"] = "2.500"  # yapay: GA 0.542-0.915 dışında
    texts = list(pilot_texts().values())
    return [dict(r, review_status="verified") for r in picked + texts]


def test_pilot_database(tmp):
    from evaluation.pilot_izolasyon import gercek_pilot_durumu, gercek_pilotu_ayir, guvenlik_kontrolu, korumayi_kaldir
    from src.database import get_connection
    from src.pilot_clinical import import_records, pdf_pages

    if not PILOT_PDF.exists():
        check("pilot DB testi: PDF var", True, False)
        return
    doc, pages = pdf_pages(PILOT_PDF)
    loader = lambda path: (doc, pages)  # noqa: E731 - PDF bir kez okunur
    records = pilot_test_records()
    # Şema hazırlığı (DDL örtük commit yapar): AYRI bağlantıda ve silmeden ÖNCE.
    setup = get_connection()
    try:
        if not schema_ready(setup):
            ensure_schema(setup)
    finally:
        setup.close()
    before = db_state()
    real_pilot = gercek_pilot_durumu()  # ayrı bağlantıdan, silmeden önce
    conn = get_connection()
    conn.autocommit = False
    conn.start_transaction()
    try:
        # Gerçek pilot verisi varsa --pilot koşusu onu da okur ve test kayıtları satir_hash ile atlanırdı:
        # belgenin gerçek pilot satırları (ve CASCADE ile işaretleri) bu transaction içinde ayrılır.
        removed = gercek_pilotu_ayir(conn, doc["document_id"])
        check("pilot DB: gerçek pilot satırları transaction içinde ayrıldı (bilgi)", "-", f"{removed} satır", True)
        # Kapsam kontrolü için gerçek rag satırına güvenilmez: test kendi rag satırını ekler.
        rag = fake_rows(conn)["tekrar_1"]
        insert_rows(conn, {"tekrar_1": rag}, ["tekrar_1"])
        written = import_records(doc, records, connection=conn)
        check("pilot DB: import_records aynı bağlantıyla yazdı", len(records), written)
        plain, _, _ = run_quality(Options(dry_run=True, rapor_dir=Path(tmp) / "rapor"), connection=conn,
                                  out=lambda *_: None)
        plain_ozet = json.loads((Path(plain["rapor_dir"]) / "ozet.json").read_text(encoding="utf-8"))
        check("pilot DB: --pilot yokken uyarı özet ve ozet.json'da", (True, True, False),
              (PILOT_WARNING in plain.get("pilot_uyarisi", ""), PILOT_WARNING in plain_ozet.get("pilot_uyarisi", ""),
               "pilot" in plain))
        lines = []
        from src.data_quality import print_summary
        print_summary(plain, [], out=lines.append)
        check("pilot DB: --pilot yokken uyarı ekrana yazılıyor", True, any(PILOT_WARNING in x for x in lines))

        summary, flags, rows = run_quality(Options(pilot=True, rapor_dir=Path(tmp) / "rapor"), connection=conn,
                                           out=lambda *_: None, page_loader=loader)
        version = control_version(pilot=True)
        stored = stored_flags(conn, version)
        pilot_rows = [r for r in rows if r.get("bilgi_turu")]
        pilot_hashes = {r["satir_hash"] for r in pilot_rows}
        pilot_flags = [f for f in flags if f["satir_hash"] in pilot_hashes]
        check("pilot DB: pilot satırları yüklendi", len(records), len(pilot_rows))
        check("pilot DB: işaretler aynı transaction'da kalite_isaretleri'ne yazıldı", len(flags), len(stored))
        check("pilot DB: rag satırları da kapsamda (tek sürüm)", True,
              len(rows) > len(pilot_rows) and rag["satir_hash"] in {r["satir_hash"] for r in rows})

        by = defaultdict(set)
        for f in pilot_flags:
            by[f["satir_hash"]].add((f["kontrol_kodu"], f["seviye"]))

        def table_rows(tablo, degisken):
            return [r for r in pilot_rows if r.get("taslak_notu") and (lambda n: (n.get("tablo_no"), n.get("degisken"))
                    == (tablo, degisken))(json.loads(r["taslak_notu"]))]

        texts = pilot_texts()

        def text_row(name):
            want = texts[name]
            return next(r for r in pilot_rows if r["kanit_cumle"] == want["kanit_cumle"]
                        and r["klinik_bilgi_en"] == want["klinik_bilgi_en"] and r.get("kanit_turu") == "metin")

        def codes_of(members):
            return {c for m in members for c, _ in by[m["satir_hash"]]}

        hba1c, hcr, glucose = table_rows(1, "HbA1c"), table_rows(1, "HCR"), table_rows(1, "Glucose")
        age2, female = table_rows(2, "Age"), table_rows(1, "Female gender")
        check("pilot DB: HbA1c çifti tam (eksik/tutarsız yok)", (2, False, False),
              (len(hba1c), "tablo_cift_eksik" in codes_of(hba1c), "tablo_cift_tutarsiz" in codes_of(hba1c)))
        check("pilot DB: bir grubu silinmiş Glucose -> tablo_cift_eksik [uyari]", True,
              ("tablo_cift_eksik", "uyari") in by[glucose[0]["satir_hash"]])
        missing_info = [p for p in summary["pilot"]["eksik_grup_analiz"] if p["degisken"] == "Glucose"]
        check("pilot DB: eksik grup raporda 'No ischemic event'", True,
              bool(missing_info) and any("No ischemic event" in e for e in missing_info[0]["eksik"]))
        multi = next(r for r in age2 if json.loads(r["taslak_notu"])["analiz"] == "multivariate")
        uni = next(r for r in age2 if json.loads(r["taslak_notu"])["analiz"] == "univariate")
        check("pilot DB: yapay CI dışı OR -> deger_ci_disinda [uyari] + PDF'le tutarsız [hata]", True,
              {("deger_ci_disinda", "uyari"), ("tablo_cift_tutarsiz", "hata")} <= by[multi["satir_hash"]])
        check("pilot DB: Age univariate CI 1'i kapsıyor ama p=0.002 -> kaynak tutarsızlığı [bilgi]", True,
              ("kaynak_ci_p_celiski", "bilgi") in by[uni["satir_hash"]])
        check("pilot DB: Age univariate CI içinde (deger_ci_disinda yok)", False,
              any(c == "deger_ci_disinda" for c, _ in by[uni["satir_hash"]]))
        check("pilot DB: Female gender n (%) yüzdeleri tutarlı", False, "pilot_yuzde_uyumsuz" in codes_of(female))
        check("pilot DB: 'p<001' (özet ROC, sonuç ROC, eski Figure kaydı, parçalı C-peptide ROC 2. parça) "
              "-> kaynak_p_bicim_bozuk [bilgi]", True,
              all(("kaynak_p_bicim_bozuk", "bilgi") in by[text_row(n)["satir_hash"]]
                  for n in ("roc_ozet", "roc_sonuc", "sekil_eski", "parca_roc")))
        check("pilot DB: 'p<0.001' (HCR özet) -> kaynak_p_bicim_bozuk yok", False,
              any(c == "kaynak_p_bicim_bozuk" for c, _ in by[text_row("hcr_ozet")["satir_hash"]]))
        hcr_dup = [f for f in pilot_flags if f["kontrol_kodu"] == "pilot_tekrar_metin_tablo"]
        check("pilot DB: HCR metin (özet) ↔ Table 1 tekrarı tek grupta bağlı [bilgi]",
              {text_row("hcr_ozet")["satir_hash"], *(r["satir_hash"] for r in hcr)},
              {f["satir_hash"] for f in hcr_dup if f["grup_id"] == hcr_dup[0]["grup_id"]} if hcr_dup else set())
        check("pilot DB: tekrar yalnız bilgi seviyesinde", {"bilgi"},
              {f["seviye"] for f in pilot_flags if f["kategori"] == "tekrar"})
        check("pilot DB: özet ↔ sonuçlar ROC metin tekrarı (v4: '0.682-0.889' iki kayıtta da)", True,
              any(f["kontrol_kodu"] == "pilot_tekrar_metin_metin"
                  and text_row("roc_sonuc")["satir_hash"] in f["ayrinti"]["uyeler"]
                  and "0.889" in f["ayrinti"]["ortak_sayilar"]
                  for f in pilot_flags if f["satir_hash"] == text_row("roc_ozet")["satir_hash"]))
        check("pilot DB: eski tek parçalı Figure yazılı kanıt (v2 #109) -> pilot_kanitta_sekil_tablo [uyari]", True,
              ("pilot_kanitta_sekil_tablo", "uyari") in by[text_row("sekil_eski")["satir_hash"]])
        check("pilot DB: '1,225 patients…' -> sayfa geçişi yalnız bilgi (uyari yok)", (True, False),
              (("pilot_sayfa_gecisi_supheli", "bilgi") in by[text_row("hasta_1225")["satir_hash"]],
               ("pilot_sayfa_gecisi_supheli", "uyari") in by[text_row("hasta_1225")["satir_hash"]]))
        for name, label in (("parca_mezenter", "#106 '(1.88%) …' (s.3→5)"), ("parca_cpep", "#152 'C-pep-' (s.6→7)"),
                            ("parca_roc", "C-peptide ROC (Figure arası)")):
            codes = {c for c, _ in by[text_row(name)["satir_hash"]]}
            check(f"pilot DB: çok parçalı {label} -> çözüldü [bilgi], sayfa geçişi / şekil uyarısı yok",
                  (True, False, False, False),
                  (("pilot_cok_parcali_cozuldu", "bilgi") in by[text_row(name)["satir_hash"]],
                   "pilot_sayfa_gecisi_supheli" in codes, "pilot_kanitta_sekil_tablo" in codes,
                   "pilot_parca_tutarsiz" in codes))
        check("pilot DB: kanıtların hepsi sayfada birebir, bilgi kanıtta", False,
              any(c in ("pilot_kanit_sayfada_yok", "pilot_bilgi_kanitta_yok", "pilot_pdf_okunamadi", "pilot_parca_tutarsiz")
                  for f in pilot_flags for c in [f["kontrol_kodu"]]))
        check("pilot DB: pilot satırlarına chunk temelli kaynak kontrolü uygulanmadı", False,
              any(f["kontrol_kodu"] in ("chunk_id_bos", "dogrulama_artik_gecmiyor") for f in pilot_flags))
        pilot = summary["pilot"]
        missing = {(m["tablo_no"], m["degisken"]) for item in pilot["eksiksizlik"] for m in item["eksik_satirlar"]}
        check("pilot DB: eksiksizlik — temsil edilen 5 tablo satırı, kalanı eksik listesinde",
              (5, pilot["pdf_tablo_satiri"] - 5, False, False),
              (pilot["temsil_edilen"], pilot["eksik_tablo_satiri"], (1, "Glucose") in missing, (2, "Age") in missing))
        ozet = json.loads((Path(summary["rapor_dir"]) / "ozet.json").read_text(encoding="utf-8"))
        isaretler = json.loads((Path(summary["rapor_dir"]) / "isaretler.json").read_text(encoding="utf-8"))
        check("pilot DB: ozet.json ve isaretler.json'da pilot bölümü", (True, True, True),
              ("eksiksizlik" in ozet.get("pilot", {}), "pilot" in isaretler, "pilot_uyarisi" not in ozet))
        pilot_distribution.update({"kod_seviye": Counter(f"{f['kontrol_kodu']} [{f['seviye']}]" for f in pilot_flags),
                                   "kategori_seviye": pilot["kategori_seviye"], "satir": len(pilot_rows),
                                   "isaret": len(pilot_flags), "eksiksizlik": [
                                       {k: v for k, v in i.items() if k in ("dosya", "pdf_tablo_satiri", "temsil_edilen")}
                                       for i in pilot["eksiksizlik"]]})

        # Bozuk parça: SQL'deki taslak_notu.kanit_parcalari transaction içinde bozulur (ROLLBACK ile geri alınır).
        target = text_row("parca_cpep")
        note = json.loads(target["taslak_notu"])
        note["kanit_parcalari"][1]["metin"] = "tide level was changed."
        cursor = conn.cursor()
        cursor.execute("UPDATE klinik_bulgular SET taslak_notu = %s WHERE id = %s",
                       (json.dumps(note, ensure_ascii=False), target["id"]))
        cursor.close()
        _, broken_flags, _ = run_quality(Options(pilot=True, dry_run=True, rapor_dir=Path(tmp) / "rapor"),
                                         connection=conn, out=lambda *_: None, page_loader=loader)
        broken = {(f["kontrol_kodu"], f["seviye"]) for f in broken_flags if f["satir_hash"] == target["satir_hash"]}
        detail = next((f["aciklama"] for f in broken_flags if f["satir_hash"] == target["satir_hash"]
                       and f["kontrol_kodu"] == "pilot_parca_tutarsiz"), "")
        check("pilot DB: bozuk parça (s.7 parçası sayfada yok) -> pilot_parca_tutarsiz [hata], çözüldü yok",
              (True, False, True),
              (("pilot_parca_tutarsiz", "hata") in broken, ("pilot_cok_parcali_cozuldu", "bilgi") in broken,
               "s.7 parçası o sayfada birebir yok" in detail))
    finally:
        conn.rollback()
        korumayi_kaldir(conn)
        conn.close()
        # Güvenlik: ayrı bağlantıdan gerçek pilot satırları başlangıçla aynı mı (değilse IzolasyonHatasi).
        check("pilot DB: güvenlik — gerçek pilot satırları ayrı bağlantıdan başlangıçla aynı", real_pilot,
              guvenlik_kontrolu(real_pilot, "test_data_quality.test_pilot_database"))
    check("pilot DB: ROLLBACK sonrası tablo satır sayıları başlangıçla aynı", before, db_state())


def test_own_transaction_after_select():
    """Gerçek DB yolu: autocommit=False bağlantıda önce SELECT, sonra write_flags(own_transaction=True).

    run_quality kendi bağlantısını açınca (connection=None) önceki SELECT'ler örtük bir
    transaction başlatır; write_flags bunu kapatıp kendi transaction'ını açmalı ve commit etmeli.
    Yalnız benzersiz bir test sürümü yazılır ve sonda silinir; gerçek satırlara dokunulmaz.
    """
    import uuid

    from src.data_quality import write_flags
    from src.database import get_connection

    before = db_state()
    version = f"test-tx-{uuid.uuid4().hex[:12]}"
    fake_hash = hashlib.sha256(version.encode()).hexdigest()
    flags = [{"klinik_bulgu_id": None, "satir_hash": fake_hash, "kontrol_kodu": "test_transaction",
              "kategori": "tekrar", "seviye": "bilgi", "grup_id": "", "aciklama": "transaction testi",
              "ayrinti": {"test": True}}]
    rows = [{"satir_hash": fake_hash}]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # Tabloya dokunan bir SELECT gerekir ("SELECT 1" transaction başlatmaz);
        # run_quality'deki load_db_rows / schema_ready ile aynı durum.
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular")
        cursor.fetchall()
        cursor.close()
        check("own_transaction: SELECT sonrası örtük transaction açık", (False, True),
              (conn.autocommit, conn.in_transaction))
        try:
            write_flags(conn, flags, rows, version, "test kapsamı", own_transaction=True,
                        delete_old_versions=False)
            error = None
        except Exception as exc:  # noqa: BLE001 - hatanın kendisi raporlanır
            error = f"{type(exc).__name__}: {exc}"
        check("own_transaction: SELECT sonrası write_flags hata vermez", None, error)

        other = get_connection()
        try:
            oc = other.cursor()
            oc.execute("SELECT COUNT(*) FROM kalite_isaretleri WHERE kontrol_surumu = %s", (version,))
            flag_count = oc.fetchone()[0]
            oc.execute("SELECT satir_sayisi, isaret_sayisi FROM kalite_calistirmalari WHERE kontrol_surumu = %s",
                       (version,))
            run_row = oc.fetchone()
            other.rollback()
        finally:
            other.close()
        check("own_transaction: yazım commit edildi (başka bağlantıdan görünüyor)", (1, (1, 1)),
              (flag_count, tuple(run_row) if run_row else None))
    finally:
        try:
            conn.rollback()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM kalite_isaretleri WHERE kontrol_surumu = %s", (version,))
            cursor.execute("DELETE FROM kalite_calistirmalari WHERE kontrol_surumu = %s", (version,))
            conn.commit()
            cursor.close()
        finally:
            conn.close()
    check("own_transaction: test verisi temizlendi, tablolar başlangıçla aynı", before, db_state())


def main():
    test_normalization()
    test_numeric()
    test_duplicates()
    test_consistency()
    test_source()
    test_determinism_and_version()
    test_pilot_unit()
    test_troponin_tables()
    test_on_deneme_tables()
    with tempfile.TemporaryDirectory() as tmp:
        test_collisions(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)
    # Bilerek commit eden alt test, gerçek pilot satırlarını transaction içinde silen pilot testinden
    # ÖNCE ve kendi bağlantısında çalışır (silme yapılan bağlantıda hiçbir commit olmaz).
    test_own_transaction_after_select()
    with tempfile.TemporaryDirectory() as tmp:
        test_pilot_database(tmp)

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<22}  {'Bulunan':<40}  Sonuç")
    print("-" * (width + 76))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:22]:<22}  {act[:40]:<40}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    if pilot_distribution:
        print(f"\nPilot test verisi: {pilot_distribution['satir']} satır, {pilot_distribution['isaret']} işaret; "
              f"kategori/seviye {pilot_distribution['kategori_seviye']}; eksiksizlik {pilot_distribution['eksiksizlik']}")
        for code, count in sorted(pilot_distribution["kod_seviye"].items()):
            print(f"  {code:<45} {count}")
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
