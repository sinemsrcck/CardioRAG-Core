"""
Çıkarım değerlendirmesi testleri (gold set + alan bazında P/R/F1).

Çalıştırma:
    python -m evaluation.test_evaluate_extraction

A) Veritabanı gerektirmeyenler: elle hesaplanmış örnek, birebir/gevşek, eşleme,
   "bulgu yok", yalnız dogrulandi, yanıtsız aday, bootstrap, örneklem, xlsx turu.
B) MySQL gerektirenler (yalnız okuma): gerçek adaylarla xlsx üretme/yükleme,
   önbellekten ham vs dogrulanmis, --mock uçtan uca; veritabanı değişmez.
Gold dosyaları geçici klasörlerde test verisidir; gerçek data/clinical/gold_set.json'a dokunulmaz.
"""

import json
import random
import sys
import tempfile
from pathlib import Path

from src.evaluate_extraction import (
    MOCK_LABEL,
    bootstrap,
    compare_candidate,
    evaluate,
    gold_create,
    gold_import,
    load_context,
    parse_args,
    run_evaluation,
    system_outputs,
)
from src.gold_set import (
    ACTIVE_FIELDS,
    REFERENCE_SOURCE,
    allocate,
    build_gold,
    build_workbook,
    draw_sample,
    evaluable_entries,
    read_workbook,
    validate_labels,
)

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def close(a, b, tol=1e-3):
    return a is not None and b is not None and abs(a - b) < tol


def entry(aday_id, bulgular, durum="dogrulandi", hatalar=(), **extra):
    return {"aday_id": aday_id, "dosya": extra.get("dosya", "D.pdf"), "sayfa": 1, "kaynak_turu": extra.get("tur", "metin"),
            "cumle": "c", "cumle_sha256": "x", "durum": durum, "bulgu_yok": not bulgular, "bulgular": bulgular,
            "hatalar": list(hatalar), "uyarilar": []}


def gold_of(*entries):
    return {"meta": {"schema": "gold-set-v1"}, "adaylar": list(entries)}


def out_of(ham, dogrulanmis=None, durum="yanit"):
    return {"durum": durum, "ham": ham, "dogrulanmis": ham if dogrulanmis is None else dogrulanmis}


# ---------------------------------------------------------------------------
# A) Veritabanı gerektirmeyenler
# ---------------------------------------------------------------------------

def test_hand_example():
    # Elle: mudahale TP1 FP1 (warfarin eşlenmedi); doz FP1 FN1 (75 ≠ 100); hasta_grubu TP1.
    # micro TP=2 FP=2 FN=1 -> P=0.5, R=0.667, F1=0.571; macro F1=(0.667+0+1)/3=0.556.
    gold = gold_of(entry("A", [{"mudahale": "aspirin", "doz": "100 mg", "hasta_grubu": "AF"}]), entry("B", []))
    outputs = {"A": out_of([{"mudahale": "Aspirin", "doz": "75 mg", "hasta_grubu": "AF"}, {"mudahale": "warfarin"}]),
               "B": out_of([])}
    res = evaluate(gold, outputs, bootstrap_iterations=0)["seviyeler"]["ham"]["birebir"]["ana"]
    f = res["alanlar"]
    check("el örneği: mudahale TP/FP/FN", (1, 1, 0), (f["mudahale"]["tp"], f["mudahale"]["fp"], f["mudahale"]["fn"]))
    check("el örneği: mudahale P/R/F1 = 0.5 / 1 / 0.667", True,
          close(f["mudahale"]["precision"], 0.5) and close(f["mudahale"]["recall"], 1.0) and close(f["mudahale"]["f1"], 2 / 3))
    check("el örneği: doz TP/FP/FN = 0/1/1, F1 0", (0, 1, 1, 0.0),
          (f["doz"]["tp"], f["doz"]["fp"], f["doz"]["fn"], f["doz"]["f1"]))
    check("el örneği: hasta_grubu 1/0/0, F1 1", (1, 0, 0, 1.0),
          (f["hasta_grubu"]["tp"], f["hasta_grubu"]["fp"], f["hasta_grubu"]["fn"], f["hasta_grubu"]["f1"]))
    check("el örneği: diğer aktif alanlar 0/0 -> null", True,
          all(f[x.ad]["f1"] is None for x in ACTIVE_FIELDS if x.ad not in ("mudahale", "doz", "hasta_grubu")))
    m = res["micro"]
    check("el örneği: micro TP=2 FP=2 FN=1", (2, 2, 1), (m["tp"], m["fp"], m["fn"]))
    check("el örneği: micro P=0.5 R=0.667 F1=0.571", True,
          close(m["precision"], 0.5) and close(m["recall"], 2 / 3) and close(m["f1"], 4 / 7))
    check("el örneği: macro F1=0.556 (3 tanımlı alan)", (True, 3),
          (close(res["macro"]["f1"], (2 / 3 + 0 + 1) / 3), res["macro"]["alan_sayisi"]))
    check("el örneği: bulgu TP1 FP1 FN0", (1, 1, 0), (res["bulgu"]["tp"], res["bulgu"]["fp"], res["bulgu"]["fn"]))
    check("el örneği: aday düzeyi (B doğru negatif)", {"dogru_negatif": 1, "ikisi_dolu": 1}, res["aday"])


def test_modes_and_matching():
    g = [{"mudahale": "aspirin therapy", "hasta_grubu": "patients with AF"}]
    s = [{"mudahale": "aspirin", "hasta_grubu": "AF patients"}]
    strict = compare_candidate(g, s, "birebir")
    loose = compare_candidate(g, s, "gevsek")
    check("birebir: 'aspirin therapy' ≠ 'aspirin' -> eşlenmez", (0, 1, 1),
          (strict["bulgu"]["tp"], strict["bulgu"]["fp"], strict["bulgu"]["fn"]))
    check("gevşek: içeriyor -> eşlenir; hasta_grubu Jaccard 2/3 -> TP", (1, 1),
          (loose["bulgu"]["tp"], loose["alan"]["hasta_grubu"]["tp"]))
    syn = compare_candidate([{"mudahale": "PCI"}], [{"mudahale": "percutaneous coronary intervention"}], "birebir")
    check("birebir: normalizasyon (PCI eşanlamlısı) eşler", 1, syn["bulgu"]["tp"])
    g2 = [{"mudahale": "aspirin", "doz": "100 mg"}]
    s2 = [{"mudahale": "aspirin", "doz": "75 mg"}, {"mudahale": "Aspirin", "doz": "100mg"}]
    best = compare_candidate(g2, s2, "birebir")
    check("eşleme: en çok alan uyuşan sistem bulgusu seçilir", (1, 0, 1),
          (best["alan"]["doz"]["tp"], best["alan"]["doz"]["fn"], best["bulgu"]["fp"]))
    shuffled = compare_candidate(g2, list(reversed(s2)), "birebir")
    check("eşleme: sistem sırası sonucu değiştirmez", best["alan"], shuffled["alan"])
    empty = compare_candidate([], [], "birebir")
    check("bulgu yok + sistem boş: sayım yok", (0, 0, 0, True),
          (empty["bulgu"]["tp"], empty["bulgu"]["fp"], empty["bulgu"]["fn"], empty["gold_bos"] and empty["sistem_bos"]))
    fp = compare_candidate([], [{"mudahale": "warfarin", "doz": "5 mg"}], "birebir")
    check("bulgu yok + sistem bulgu üretti: FP", (1, 1, 1), (fp["bulgu"]["fp"], fp["alan"]["mudahale"]["fp"], fp["alan"]["doz"]["fp"]))


def test_gold_rules():
    taslak = entry("T", [{"mudahale": "x"}], durum="taslak")
    atla = entry("S", [{"mudahale": "x"}], durum="atla")
    hatali = entry("H", [{"mudahale": "x"}], hatalar=["durum tutarsız"])
    ok = entry("A", [{"mudahale": "aspirin"}])
    gold = gold_of(taslak, atla, hatali, ok)
    check("yalnız dogrulandi + hatasız metriğe girer", ["A"], [e["aday_id"] for e in evaluable_entries(gold)])
    outputs = {k: out_of([{"mudahale": "aspirin"}]) for k in "TSHA"}
    base = evaluate(gold, outputs, bootstrap_iterations=0)
    taslak["bulgular"] = [{"mudahale": "çok farklı"}]
    changed = evaluate(gold, outputs, bootstrap_iterations=0)
    check("taslak adayın değeri değişince metrik değişmez", base["seviyeler"], changed["seviyeler"])
    gold2 = gold_of(entry("A", [{"mudahale": "aspirin"}]), entry("M", [{"mudahale": "warfarin"}]))
    out2 = {"A": out_of([{"mudahale": "aspirin"}]), "M": out_of([], durum="onbellekte_yok")}
    r = evaluate(gold2, out2, bootstrap_iterations=0)
    main = r["seviyeler"]["ham"]["birebir"]["ana"]["micro"]
    cons = r["seviyeler"]["ham"]["birebir"]["konservatif"]["micro"]
    check("yanıtsız aday: listede, ana metrikte yok, konservatifte FN", (["M"], 0, 1),
          (r["yanitsiz_adaylar"], main["fn"], cons["fn"]))
    ham_vs = evaluate(gold_of(entry("A", [{"mudahale": "aspirin"}])),
                      {"A": out_of([{"mudahale": "aspirin"}, {"mudahale": "PCI"}], [{"mudahale": "aspirin"}])},
                      bootstrap_iterations=0)["seviyeler"]
    check("ham vs dogrulanmis ayrı raporlanır (ham FP 1, dogrulanmis FP 0)", (1, 0),
          (ham_vs["ham"]["birebir"]["ana"]["bulgu"]["fp"], ham_vs["dogrulanmis"]["birebir"]["ana"]["bulgu"]["fp"]))
    many = [{"alan": {"mudahale": {"tp": random.Random(i).randint(0, 3), "fp": 1, "fn": 1}}} for i in range(20)]
    check("bootstrap: aynı seed aynı aralık", bootstrap(many, 200, 7), bootstrap(many, 200, 7))


def synthetic_candidates():
    files = ["TK-a.pdf", "pubM-b.pdf", "who-c.pdf"]
    out = []
    for i in range(150):
        out.append({"aday_id": f"clinical-candidate-v1:{i:064x}", "dosya": files[i % 3], "makale_id": f"M{i % 3}",
                    "sayfa": i % 7 + 1, "kaynak_turu": "tablo" if i % 10 == 0 else "metin",
                    "cumle": f"Hastaların %{i} kadarında İlaç şeması ≥ 5 µg/gün; ölçüm {i}."})
    return out


def test_sampling():
    cands = synthetic_candidates()
    ref = {c["aday_id"] for c in cands[:40]}
    first = [c["aday_id"] for c in draw_sample(cands, ref, 30, 42)]
    shuffled = cands[:]
    random.Random(3).shuffle(shuffled)
    check("örneklem: aynı seed aynı adaylar", first, [c["aday_id"] for c in draw_sample(cands, ref, 30, 42)])
    check("örneklem: girdi sırası önemsiz", sorted(first), sorted(c["aday_id"] for c in draw_sample(shuffled, ref, 30, 42)))
    check("örneklem: farklı seed farklı adaylar", True, first != [c["aday_id"] for c in draw_sample(cands, ref, 30, 7)])
    sample = draw_sample(cands, ref, 30, 42)
    check("örneklem: 2/3 referans, 1/3 diğer", {"R": 20, "O": 10},
          {h: sum(c["_havuz"] == h for c in sample) for h in "RO"})
    small = draw_sample(cands, {c["aday_id"] for c in cands[:5]}, 30, 42)
    check("örneklem: referans havuzu yetmezse diğerinden tamamlanır", (5, 25, 30),
          (sum(c["_havuz"] == "R" for c in small), sum(c["_havuz"] == "O" for c in small), len(small)))
    strata = {(c["dosya"], c["kaynak_turu"]) for c in cands if c["aday_id"] not in ref}
    check("örneklem: her katmandan en az 1", True,
          strata <= {(c["dosya"], c["kaynak_turu"]) for c in sample if c["_havuz"] == "O"})
    # Elle: her katmana 1 (2), kalan 8 oda oranıyla (a:1, b:49) -> a 0.16, b 7.84 -> b 7 + kalan 1 -> a 1, b 9.
    check("allocate: en az 1 + orantılı (en büyük kalan)", (10, {"a": 1, "b": 9}),
          (sum(allocate({"a": 2, "b": 50}, 10).values()), allocate({"a": 2, "b": 50}, 10)))
    check("allocate: katman tavanı aşılmaz", {"a": 2, "b": 3}, allocate({"a": 2, "b": 3}, 10))


def test_xlsx_roundtrip(tmp):
    from openpyxl import load_workbook

    cands = synthetic_candidates()[:6]
    cands[0]["cumle"] = "Şiddetli İnme riskinde apiksaban 5 mg günde iki kez; hastaların %62'si (≥75 yaş) ılımlı µg."
    sample = [dict(c, _havuz="R" if i < 3 else "O") for i, c in enumerate(cands)]
    ref = {cands[0]["aday_id"]: [{"mudahale_tr": "apiksaban", "doz": "5 mg", "hasta_grubu": "yaşlı hastalar",
                                  "mudahale_turu": "tedavi"}]}
    wb, counts = build_workbook(sample, ref, None, {"seed": 42, "n": 6})
    path = Path(tmp) / "g.xlsx"
    wb.save(path)
    records, meta, havuz = read_workbook(path)
    first = next(r for r in records if r["aday_id"] == cands[0]["aday_id"])
    check("xlsx: Türkçe karakterler korunuyor", cands[0]["cumle"], first["aday_cumlesi"])
    check("xlsx: birebir geçen referans değeri hücrede", ("apiksaban", "5 mg", "tedavi"),
          (first["mudahale"], first["doz"], first["mudahale_turu"]))
    check("xlsx: geçmeyen öneri taslak_referans'ta", "hasta_grubu: yaşlı hastalar", first["taslak_referans"])
    check("xlsx: önceden doldurulan satır taslak ve 'doğrulanmadı'", ("taslak", REFERENCE_SOURCE),
          (first["durum"], first["kaynak"]))
    check("xlsx: hiçbir satır 'dogrulandi' değil", True, all(r["durum"] == "taslak" for r in records))
    check("xlsx: meta ve havuz okundu", (42, "R"), (meta.get("seed"), havuz[cands[0]["aday_id"]]))
    check("xlsx: açılır listeler", True, len(load_workbook(path)["Etiketler"].data_validations.dataValidation) >= 4)
    check("xlsx: sayım", (6, 6, 1), (counts["aday"], counts["satir"], counts["onceden_doldurulmus_satir"]))

    # İnsan etiketlemesini taklit et.
    book = load_workbook(path)
    sheet = book["Etiketler"]
    header = [c.value for c in sheet[1]]
    col = {name: header.index(name) + 1 for name in header}
    by_id = {sheet.cell(row=r, column=col["aday_id"]).value: r for r in range(2, sheet.max_row + 1)}
    r0, r1, r2, r3 = (by_id[c["aday_id"]] for c in cands[:4])
    sheet.cell(row=r0, column=col["durum"]).value = "dogrulandi"
    sheet.cell(row=r0, column=col["hasta_grubu"]).value = "≥75 yaş"
    sheet.cell(row=r1, column=col["durum"]).value = "dogrulandi"
    sheet.cell(row=r1, column=col["bulgu_yok"]).value = "EVET"
    sheet.cell(row=r2, column=col["durum"]).value = "dogrulandi"
    sheet.cell(row=r2, column=col["mudahale"]).value = "cümlede olmayan ilaç"
    sheet.cell(row=r2, column=col["mudahale_turu"]).value = "ilaç"
    sheet.cell(row=r3, column=col["durum"]).value = "dogrulandi"  # boş ve bulgu_yok işaretsiz -> hata
    book.save(path)
    records, meta, havuz = read_workbook(path)
    candidates = {c["aday_id"]: c for c in cands}
    entries = {e["aday_id"]: e for e in validate_labels(records, candidates, set(candidates), havuz)}
    e0, e1, e2, e3 = (entries[c["aday_id"]] for c in cands[:4])
    check("yükleme: doğrulanmış aday hatasız, değerler korunur", ([], "≥75 yaş", "apiksaban"),
          (e0["hatalar"], e0["bulgular"][0]["hasta_grubu"], e0["bulgular"][0]["mudahale"]))
    check("yükleme: bulgu_yok aday", (True, [], []), (e1["bulgu_yok"], e1["bulgular"], e1["hatalar"]))
    check("yükleme: cümlede geçmeyen değer -> uyarı", True, any("birebir geçmiyor" in w for w in e2["uyarilar"]))
    check("yükleme: enum dışı -> hata", True, any("izinli değerlerden değil" in h for h in e2["hatalar"]))
    check("yükleme: boş doğrulanmış aday -> hata", True, any("bulgu_yok" in h for h in e3["hatalar"]))
    bad = validate_labels([dict(records[0], aday_id="clinical-candidate-v1:yok")], candidates, set(candidates))
    check("yükleme: bilinmeyen aday_id -> hata", True, any("mevcut adaylarda yok" in h for h in bad[0]["hatalar"]))
    mixed = [dict(records[0], durum="dogrulandi"), dict(records[0], durum="taslak", _satir=99, bulgu_no="2")]
    check("yükleme: durum tutarsızlığı -> hata", True,
          any("durumu farklı" in h for h in validate_labels(mixed, candidates, set(candidates))[0]["hatalar"]))
    gold = build_gold(list(entries.values()), meta, "g.xlsx")
    check("gold: yalnız hatasız dogrulandi değerlendirilir", sorted([cands[0]["aday_id"], cands[1]["aday_id"]]),
          sorted(e["aday_id"] for e in evaluable_entries(gold)))
    wb2, counts2 = build_workbook(sample, ref, gold, {"seed": 42})
    path2 = Path(tmp) / "g2.xlsx"
    wb2.save(path2)
    again = {r["aday_id"]: r for r in read_workbook(path2)[0]}
    check("yeniden üretim: gold_set.json'daki durumlar korunur", ("dogrulandi", "≥75 yaş", 6),
          (again[cands[0]["aday_id"]]["durum"], again[cands[0]["aday_id"]]["hasta_grubu"], counts2["korunan_aday"]))
    check("CLI: tek eylem zorunlu", True, parse_args(["--degerlendir", "--mock"]).mock)


# ---------------------------------------------------------------------------
# B) Veritabanı gerektirenler (yalnız okuma)
# ---------------------------------------------------------------------------

def db_state():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        state = {}
        for table in ("klinik_bulgular", "kalite_isaretleri", "halusinasyon_degerlendirme",
                      "makaleler WHERE document_id IS NULL", "tani_tedavi", "icd10_who", "tkd_kurallari"):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            state[table] = cursor.fetchone()[0]
        return state
    finally:
        conn.close()


def test_database(tmp):
    from openpyxl import load_workbook
    from src.clinical_pipeline import PIPELINE_VERSION, PIPELINE_FIELDS
    from src.data_quality import load_reference_rows
    from src.run_clinical_batch import write_cache
    from src.clinical_candidates import TREATMENT_REGEX

    before = db_state()
    ctx = load_context()
    reference = load_reference_rows()
    xlsx = Path(tmp) / "gold.xlsx"
    missing_gold = Path(tmp) / "yok.json"
    info = gold_create(30, 42, xlsx, ctx=ctx, reference=reference, gold_path=missing_gold)
    check("gerçek adaylar: xlsx üretildi (30 aday, 20 R / 10 O)", (30, {"R": 20, "O": 10}), (info["aday"], info["havuz"]))
    again = gold_create(30, 42, Path(tmp) / "gold_b.xlsx", ctx=ctx, reference=reference, gold_path=missing_gold)
    check("gerçek adaylar: örneklem deterministik", info["katmanlar"], again["katmanlar"])
    try:
        gold_create(30, 42, xlsx, ctx=ctx, reference=reference, gold_path=missing_gold)
        got = "üzerine yazdı"
    except SystemExit:
        got = "reddetti"
    check("var olan xlsx'in üzerine yazılmaz", "reddetti", got)

    # Üç aday 'dogrulandi' (değerler cümleden birebir); biri bulgu_yok.
    book = load_workbook(xlsx)
    sheet = book["Etiketler"]
    header = [c.value for c in sheet[1]]
    col = {name: header.index(name) + 1 for name in header}
    to_send = ctx[1]
    chosen = []
    for r in range(2, sheet.max_row + 1):
        aday_id = sheet.cell(row=r, column=col["aday_id"]).value
        if aday_id in [c[0] for c in chosen] or aday_id not in to_send:
            continue
        spans = to_send[aday_id]["allowed_quote_spans"]
        match = next((m.group(0) for s in spans for rx in TREATMENT_REGEX.values() for m in [rx.search(s)] if m), None)
        if match:
            chosen.append((aday_id, r, match))
        if len(chosen) == 3:
            break
    for name in [f.ad for f in ACTIVE_FIELDS] + ["p", "ci95", "hasta_sayisi", "etki_turu", "taslak_referans"]:
        for _, r, _ in chosen:
            sheet.cell(row=r, column=col[name]).value = None
    for index, (aday_id, r, match) in enumerate(chosen):
        sheet.cell(row=r, column=col["durum"]).value = "dogrulandi"
        if index == 2:
            sheet.cell(row=r, column=col["bulgu_yok"]).value = "EVET"
        else:
            sheet.cell(row=r, column=col["mudahale"]).value = match
    # Çok bulgulu adayların diğer satırları: boşaltılır, aynı durum (boş satırlar yok sayılır).
    ids = {a for a, _, _ in chosen}
    for r in range(2, sheet.max_row + 1):
        if sheet.cell(row=r, column=col["aday_id"]).value in ids and r not in {x for _, x, _ in chosen}:
            for name in [f.ad for f in ACTIVE_FIELDS] + ["p", "ci95", "hasta_sayisi", "etki_turu"]:
                sheet.cell(row=r, column=col[name]).value = None
            sheet.cell(row=r, column=col["durum"]).value = "dogrulandi"
    book.save(xlsx)
    gold_path = Path(tmp) / "gold_set.json"
    gold = gold_import(xlsx, gold_path, ctx=ctx)
    problems = [(e["aday_id"][-8:], e["hatalar"]) for e in gold["adaylar"] if e["aday_id"] in ids and e["hatalar"]]
    check("gerçek adaylar: yükleme -> gold_set.json (geçici), hatasız", (True, []), (gold_path.exists(), problems))
    evaluable = evaluable_entries(gold)
    check("gerçek adaylar: değerlendirilebilir = 3 dogrulandi (biri bulgu_yok)", (3, 1),
          (len(evaluable), sum(e["bulgu_yok"] for e in evaluable)))
    check("gerçek adaylar: kalanlar taslak, metriğe girmez", len(gold["adaylar"]) - 3,
          sum(e["durum"] == "taslak" for e in gold["adaylar"]))

    mid = db_state()
    metrics = run_evaluation(gold_path=gold_path, source="mock", ctx=ctx, report_dir=Path(tmp) / "rapor",
                             bootstrap_iterations=100, out=lambda *_: None)
    folder = Path(metrics["rapor_dir"])
    check("--mock: etiket ve sürümler raporda", (MOCK_LABEL, PIPELINE_VERSION, gold["meta"]["gold_hash"] if gold else None),
          (metrics["etiket"], metrics["surumler"]["pipeline_version"], metrics["surumler"]["gold_hash"]))
    check("--mock: rapor dosyaları", True,
          all((folder / n).exists() for n in ("metrics.json", "alanlar.csv", "kirilim.csv", "hatalar.csv")))
    check("--mock: değerlendirilemez alanlar ayrı", ["p", "ci95", "hasta_sayisi", "etki_turu", "icd10"],
          metrics["degerlendirilemez_alanlar"])
    check("--mock: değerlendirilen aday", len(evaluable), metrics["degerlendirme"]["degerlendirilen_aday"])

    # Önbellekten: iki bulgulu yanıt; biri güvenli parça dışında -> ham 2, dogrulanmis 1.
    from src.clinical_pipeline import preview_rows, process_response

    def finding_for(i):
        span = i["allowed_quote_spans"][0]
        drug = next((m.group(0) for rx in TREATMENT_REGEX.values() for m in [rx.search(span)] if m), None)
        return {**dict.fromkeys(PIPELINE_FIELDS), "drug": drug, "evidence_quote": span} if drug else None

    def passes(i):  # geçerli bulgu gerçekten validate_row'dan geçiyor mu (önceden kontrol)
        f = finding_for(i)
        if not f:
            return False
        rows, _, _ = process_response(json.dumps({"findings": [f]}), i, ctx[0], model="m")
        return len(preview_rows(rows, i, ctx[0])[0]) == 1
    item = next(i for i in to_send.values() if i["candidate"]["kaynak_turu"] == "metin" and passes(i))
    good = finding_for(item)
    bad = {**good, "evidence_quote": "metinde olmayan alıntı"}
    batch = Path(tmp) / "batch"
    write_cache(batch, PIPELINE_VERSION, "test-model", item["candidate"]["aday_id"],
                json.dumps({"findings": [good, bad]}), "stop", False)
    outputs, info = system_outputs([item["candidate"]["aday_id"], "clinical-candidate-v1:" + "0" * 64], ctx,
                                   source="cache", batch_dir=batch, model="test-model", warn=lambda *_: None)
    out = outputs[item["candidate"]["aday_id"]]
    check("önbellek: ham 2 bulgu, dogrulanmis 1", (2, 1), (len(out["ham"]), len(out["dogrulanmis"])))
    check("önbellek: LLM'e gitmeyen aday 'llm_disi'", "llm_disi", outputs["clinical-candidate-v1:" + "0" * 64]["durum"])
    check("veritabanı değişmedi", (before, mid), (db_state(), db_state()))


def main():
    test_hand_example()
    test_modes_and_matching()
    test_gold_rules()
    test_sampling()
    with tempfile.TemporaryDirectory() as tmp:
        test_xlsx_roundtrip(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<26}  {'Bulunan':<40}  Sonuç")
    print("-" * (width + 80))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:26]:<26}  {act[:40]:<40}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
