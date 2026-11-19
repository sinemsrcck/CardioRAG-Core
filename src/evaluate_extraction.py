"""
Otomatik klinik çıkarımın gold set'e karşı değerlendirilmesi (alan bazında P/R/F1).

    python -m src.evaluate_extraction --gold-olustur [--n 60 --seed 42]
    python -m src.evaluate_extraction --gold-yukle data/review/gold/gold_etiketleme.xlsx
    python -m src.evaluate_extraction --degerlendir --from-cache data/review/clinical_batch
    python -m src.evaluate_extraction --degerlendir --db
    python -m src.evaluate_extraction --degerlendir --mock      # sahte LLM; gerçek performans değil

Yalnızca gold_set.json'da durum='dogrulandi' ve hatasız adaylar değerlendirilir.
Sistem çıktısı iki seviyede: "ham" (LLM yanıtındaki bütün bulgular, doğrulama
öncesi) ve "dogrulanmis" (validate_row'dan geçen, kaydedilecek satırlar).
LLM çağrılmaz (--mock sahte istemci kullanır); veritabanına yazılmaz.

Tanımlar (alan f için, aynı aday içinde eşlenmiş gold g / sistem s bulguları):
  ikisi dolu ve eşit -> TP; ikisi dolu ve farklı -> FP + FN (yanlış değer);
  yalnız s dolu -> FP; yalnız g dolu -> FN; ikisi boş -> sayılmaz.
  Eşlenmemiş sistem bulgusunun dolu alanları FP, eşlenmemiş gold bulgusununkiler FN.
  P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2PR/(P+R); 0/0 -> null (macro'ya girmez).
  micro = aktif alanların TP/FP/FN toplamı; macro = tanımlı F1'lerin ortalaması.
"""

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from src.data_quality import control_version, jaccard
from src.gold_set import (
    ACTIVE_FIELDS,
    DEFAULT_N,
    DEFAULT_SEED,
    DEFAULT_XLSX,
    FIELD_LIST_VERSION,
    GOLD_FIELDS,
    GOLD_FILE,
    build_gold,
    build_workbook,
    draw_sample,
    evaluable_entries,
    load_gold,
    normalize,
    read_workbook,
    save_gold,
    sentence_hash,
    validate_labels,
)

DEFAULT_REPORT_DIR = Path("data/review/degerlendirme")
DEFAULT_BATCH_DIR = Path("data/review/clinical_batch")
LEVELS = ("ham", "dogrulanmis")
MODES = ("birebir", "gevsek")
LOOSE_JACCARD = 0.5
EXHAUSTIVE_LIMIT = 7
MOCK_LABEL = "mock: gerçek performans değildir (sahte LLM)"


# ---------------------------------------------------------------------------
# Sistem bulgusu -> gold alanları
# ---------------------------------------------------------------------------

def _first(source, keys):
    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def ham_to_gold(finding):
    """LLM bulgusu (doğrulama öncesi) -> gold alan sözlüğü. mudahale_turu toplu işteki kuralla türetilir."""
    finding = finding if isinstance(finding, dict) else {}
    out = {}
    for field in GOLD_FIELDS:
        if not field.ham:
            out[field.ad] = None
        elif field.ad == "mudahale_turu":
            out[field.ad] = ("tanı testi" if _first(finding, ("diagnostic_method",))
                             else "tedavi" if _first(finding, ("drug", "procedure")) else None)
        elif field.ad == "doz":
            dose, unit = _first(finding, ("dose",)), _first(finding, ("dose_unit",))
            out[field.ad] = f"{dose} {unit}" if dose and unit else dose
        else:
            out[field.ad] = _first(finding, field.ham)
    return out


def row_to_gold(row):
    """klinik_bulgular satırı (kaydedilecek/kaydedilmiş) -> gold alan sözlüğü."""
    out = {field.ad: _first(row, field.satir) for field in GOLD_FIELDS}
    if not out["sonuc"] and row.get("taslak_notu"):
        try:
            note = json.loads(row["taslak_notu"])
            out["sonuc"] = _first((note.get("extracted_fields") or {}), ("clinical_outcome",))
        except (TypeError, ValueError, AttributeError):
            pass
    return out


# ---------------------------------------------------------------------------
# Eşleme ve sayım
# ---------------------------------------------------------------------------

def normalized(finding):
    return {f.ad: normalize(f, finding.get(f.ad)) for f in GOLD_FIELDS}


def equal(field, a, b, mode):
    if a is None or b is None:
        return False
    if a == b:
        return True
    if mode == "gevsek" and field.tur in ("mudahale", "metin", "deger", "birim"):
        return a in b or b in a or jaccard(a, b) >= LOOSE_JACCARD
    return False


MUDAHALE = next(f for f in GOLD_FIELDS if f.ad == "mudahale")


def best_matching(gold, system, mode, fields=ACTIVE_FIELDS):
    """[(gi, si)] — önce eşleşen çift sayısı, sonra toplam alan uyuşması en büyük; deterministik."""
    allowed = [[equal(MUDAHALE, g["mudahale"], s["mudahale"], mode) for s in system] for g in gold]
    score = [[sum(equal(f, g[f.ad], s[f.ad], mode) for f in fields) for s in system] for g in gold]
    if len(gold) <= EXHAUSTIVE_LIMIT and len(system) <= EXHAUSTIVE_LIMIT:
        best = [None, None]

        def search(i, used, assignment, pairs, total):
            if i == len(gold):
                key = (pairs, total)
                if best[0] is None or key > best[0]:
                    best[0], best[1] = key, list(assignment)
                return
            for j in range(len(system)):
                if j not in used and allowed[i][j]:
                    search(i + 1, used | {j}, assignment + [j], pairs + 1, total + score[i][j])
            search(i + 1, used, assignment + [None], pairs, total)
        search(0, frozenset(), [], 0, 0)
        return [(i, j) for i, j in enumerate(best[1]) if j is not None]
    candidates = sorted(((-score[i][j], i, j) for i in range(len(gold)) for j in range(len(system)) if allowed[i][j]))
    used_g, used_s, pairs = set(), set(), []
    for _, i, j in candidates:
        if i not in used_g and j not in used_s:
            used_g.add(i)
            used_s.add(j)
            pairs.append((i, j))
    return sorted(pairs)


def compare_candidate(gold_raw, system_raw, mode, context=None):
    """Bir aday: alan sayıları, bulgu sayıları, hata kayıtları."""
    gold = [normalized(g) for g in gold_raw]
    order = sorted(range(len(system_raw)), key=lambda k: json.dumps(normalized(system_raw[k]), sort_keys=True,
                                                                    ensure_ascii=False))
    system_raw = [system_raw[k] for k in order]
    system = [normalized(s) for s in system_raw]
    pairs = best_matching(gold, system, mode)
    counts = {f.ad: Counter() for f in ACTIVE_FIELDS}
    errors = []
    matched_g, matched_s = {i for i, _ in pairs}, {j for _, j in pairs}
    full_correct = 0

    def err(kind, field, g_value, s_value):
        errors.append({**(context or {}), "mod": mode, "alan": field, "tur": kind, "gold": g_value, "sistem": s_value})

    for i, j in pairs:
        all_ok = True
        for f in ACTIVE_FIELDS:
            g, s = gold[i][f.ad], system[j][f.ad]
            if g is None and s is None:
                continue
            if g is not None and s is not None and equal(f, g, s, mode):
                counts[f.ad]["tp"] += 1
                continue
            all_ok = False
            if g is not None and s is not None:
                counts[f.ad]["fp"] += 1
                counts[f.ad]["fn"] += 1
                err("yanlis_deger", f.ad, gold_raw[i].get(f.ad), system_raw[j].get(f.ad))
            elif s is not None:
                counts[f.ad]["fp"] += 1
                err("fazla_alan", f.ad, None, system_raw[j].get(f.ad))
            else:
                counts[f.ad]["fn"] += 1
                err("eksik_alan", f.ad, gold_raw[i].get(f.ad), None)
        full_correct += all_ok
    for j, s in enumerate(system):
        if j in matched_s:
            continue
        for f in ACTIVE_FIELDS:
            if s[f.ad] is not None:
                counts[f.ad]["fp"] += 1
        err("fazla_bulgu", "mudahale", None, system_raw[j].get("mudahale"))
    for i, g in enumerate(gold):
        if i in matched_g:
            continue
        for f in ACTIVE_FIELDS:
            if g[f.ad] is not None:
                counts[f.ad]["fn"] += 1
        err("eksik_bulgu", "mudahale", gold_raw[i].get("mudahale"), None)
    finding = Counter(tp=len(pairs), fp=len(system) - len(pairs), fn=len(gold) - len(pairs), tam_dogru=full_correct)
    return {"alan": counts, "bulgu": finding, "hatalar": errors,
            "gold_bos": not gold, "sistem_bos": not system}


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = (2 * p * r / (p + r) if p + r else 0.0) if p is not None and r is not None else None
    return {"precision": p, "recall": r, "f1": f1}


def aggregate(results):
    fields = {f.ad: Counter() for f in ACTIVE_FIELDS}
    finding = Counter()
    candidate = Counter()
    for res in results:
        for name, c in res["alan"].items():
            fields[name].update(c)
        finding.update(res["bulgu"])
        key = ("dogru_negatif" if res["gold_bos"] and res["sistem_bos"]
               else "yanlis_pozitif_aday" if res["gold_bos"] else "kacirilan_aday" if res["sistem_bos"] else "ikisi_dolu")
        candidate[key] += 1
    per_field = {}
    for name, c in fields.items():
        per_field[name] = {"tp": c["tp"], "fp": c["fp"], "fn": c["fn"], **prf(c["tp"], c["fp"], c["fn"])}
    tp, fp, fn = (sum(v[k] for v in per_field.values()) for k in ("tp", "fp", "fn"))
    defined = [v["f1"] for v in per_field.values() if v["f1"] is not None]
    gold_findings = finding["tp"] + finding["fn"]
    return {
        "aday_sayisi": len(results),
        "alanlar": per_field,
        "micro": {"tp": tp, "fp": fp, "fn": fn, **prf(tp, fp, fn)},
        "macro": {"f1": sum(defined) / len(defined) if defined else None, "alan_sayisi": len(defined)},
        "bulgu": {"tp": finding["tp"], "fp": finding["fp"], "fn": finding["fn"],
                  **prf(finding["tp"], finding["fp"], finding["fn"]), "tam_dogru": finding["tam_dogru"],
                  "tam_dogru_orani": finding["tam_dogru"] / gold_findings if gold_findings else None},
        "aday": dict(sorted(candidate.items())),
    }


def bootstrap(results, iterations, seed):
    """Aday bazında yeniden örnekleme; micro F1 için %95 aralık."""
    if not results or iterations <= 0:
        return None
    totals = [tuple(sum(c[k] for c in r["alan"].values()) for k in ("tp", "fp", "fn")) for r in results]
    rng = random.Random(seed)
    scores = []
    for _ in range(iterations):
        tp = fp = fn = 0
        for index in (rng.randrange(len(totals)) for _ in range(len(totals))):
            a, b, c = totals[index]
            tp, fp, fn = tp + a, fp + b, fn + c
        f1 = prf(tp, fp, fn)["f1"]
        if f1 is not None:
            scores.append(f1)
    if not scores:
        return None
    scores.sort()
    return {"iterasyon": iterations, "gecerli": len(scores), "seed": seed,
            "micro_f1_alt": scores[int(0.025 * (len(scores) - 1))],
            "micro_f1_ust": scores[int(round(0.975 * (len(scores) - 1)))]}


# ---------------------------------------------------------------------------
# Sistem çıktısı
# ---------------------------------------------------------------------------

def load_context():
    from src.clinical_candidates import collect_candidates
    from src.run_clinical_batch import prepare

    sources, to_send, _excluded, _ = prepare()
    candidates = collect_candidates(sources.articles)
    return sources, {i["candidate"]["aday_id"]: i for i in to_send}, {c["aday_id"]: c for c in candidates}


def system_outputs(aday_ids, ctx, *, source, batch_dir=DEFAULT_BATCH_DIR, model=None, pipeline_version=None,
                   warn=print):
    """{aday_id: {"durum": yanit|onbellekte_yok|llm_disi|yanit_gecersiz, "ham": [...]|None, "dogrulanmis": [...]}}"""
    from src.clinical_pipeline import (PIPELINE_VERSION, CandidateError, MockLLMClient, build_messages,
                                       parse_llm_json, preview_rows, process_response, response_text)
    from src.run_clinical_batch import cached_models, read_cache

    sources, to_send, _ = ctx
    version = pipeline_version or PIPELINE_VERSION
    outputs, info = {}, {"pipeline_version": version, "kaynak": source}
    if source == "mock":
        model, mock = MockLLMClient.model, MockLLMClient()
    elif source == "cache":
        if not model:
            models = cached_models(batch_dir, version)
            if len(models) != 1:
                raise SystemExit(f"Önbellekte model belirsiz veya yok ({sorted(models)}); --model verin. "
                                 f"Klasör: {Path(batch_dir) / 'responses' / version}")
            model = models.pop()
    info["model"] = model
    db_rows = load_db_rows(version) if source == "db" else None
    for aday_id in aday_ids:
        item = to_send.get(aday_id)
        if source == "db":
            rows = db_rows.get(aday_id, [])
            outputs[aday_id] = {"durum": "yanit" if rows else "kayit_yok", "ham": None,
                                "dogrulanmis": [row_to_gold(r) for r in rows]}
            continue
        if item is None:
            outputs[aday_id] = {"durum": "llm_disi", "ham": [], "dogrulanmis": []}
            continue
        if source == "mock":
            raw = response_text(mock._create(model=model, messages=build_messages(item)))[0]
        else:
            cached = read_cache(batch_dir, version, model, aday_id, warn)
            if cached is None:
                outputs[aday_id] = {"durum": "onbellekte_yok", "ham": [], "dogrulanmis": []}
                continue
            raw = cached["raw_text"]
        try:
            ham = [ham_to_gold(f) for f in parse_llm_json(raw)["findings"]]
            rows, _, _ = process_response(raw, item, sources, model=model)
            passed, _ = preview_rows(rows, item, sources)
            outputs[aday_id] = {"durum": "yanit", "ham": ham, "dogrulanmis": [row_to_gold(r) for r in passed]}
        except CandidateError as exc:
            outputs[aday_id] = {"durum": "yanit_gecersiz", "ham": [], "dogrulanmis": [], "hata": exc.code}
    return outputs, info


def load_db_rows(version):
    """klinik_bulgular rag satırları (yalnız SELECT), taslak_notu.aday_id ve pipeline_version'a göre."""
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM klinik_bulgular WHERE cikarma_yontemi = 'rag'")
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    grouped = defaultdict(list)
    for row in rows:
        row = {k: (bytes(v).decode() if isinstance(v, (bytes, bytearray)) else v) for k, v in row.items()}
        try:
            note = json.loads(row.get("taslak_notu") or "{}")
        except ValueError:
            continue
        if note.get("pipeline_version") == version and note.get("aday_id"):
            grouped[note["aday_id"]].append(row)
    return grouped


# ---------------------------------------------------------------------------
# Değerlendirme
# ---------------------------------------------------------------------------

def evaluate(gold, outputs, *, bootstrap_iterations=1000, seed=DEFAULT_SEED, candidates_by_id=None):
    """Gold + sistem çıktısı -> metrik sözlüğü (ana, konservatif, kırılımlar, hatalar)."""
    entries = evaluable_entries(gold) if gold else []
    excluded = []
    usable = []
    for e in entries:
        current = (candidates_by_id or {}).get(e["aday_id"])
        if candidates_by_id is not None and (current is None or sentence_hash(current["cumle"]) != e["cumle_sha256"]):
            excluded.append({"aday_id": e["aday_id"], "sebep": "aday_metni_degisti_veya_yok"})
            continue
        usable.append(e)
    status = Counter(outputs.get(e["aday_id"], {}).get("durum", "yok") for e in usable)
    answered = [e for e in usable if outputs.get(e["aday_id"], {}).get("durum") in ("yanit", "yanit_gecersiz")]
    missing = [e["aday_id"] for e in usable if e not in answered]

    result = {"degerlendirilen_aday": len(usable), "sistem_durumu": dict(sorted(status.items())),
              "yanitsiz_adaylar": missing, "disarida_kalan": excluded, "seviyeler": {}, "hatalar": []}
    for level in LEVELS:
        if any(outputs.get(e["aday_id"], {}).get(level) is None for e in answered) and answered:
            result["seviyeler"][level] = {"durum": "yok (bu kaynakta bu seviye üretilmez)"}
            continue
        level_out = {}
        for mode in MODES:
            def run(group):
                res = []
                for e in group:
                    ctx = {"seviye": level, "aday_id": e["aday_id"], "dosya": e["dosya"], "sayfa": e["sayfa"],
                           "kaynak_turu": e["kaynak_turu"], "cumle": (e.get("cumle") or "")[:200]}
                    system = outputs.get(e["aday_id"], {}).get(level) or []
                    r = compare_candidate(e["bulgular"], system, mode, ctx)
                    r["_entry"] = e
                    res.append(r)
                return res
            main = run(answered)
            conservative = run(usable)
            block = {"ana": aggregate(main), "konservatif": aggregate(conservative)}
            if mode == "birebir":
                block["bootstrap"] = bootstrap(main, bootstrap_iterations, seed)
                result["hatalar"] += [h for r in main for h in r["hatalar"]]
            breakdown = {}
            for key in ("dosya", "kaynak_turu"):
                groups = defaultdict(list)
                for r in main:
                    groups[r["_entry"][key]].append(r)
                breakdown[key] = {name: {k: v for k, v in aggregate(rs).items() if k in ("aday_sayisi", "micro", "bulgu")}
                                  for name, rs in sorted(groups.items(), key=lambda x: str(x[0]))}
            block["kirilim"] = breakdown
            level_out[mode] = block
        result["seviyeler"][level] = level_out
    result["en_sik_hatalar"] = [{"seviye": s, "alan": a, "tur": t, "sayi": n} for (s, a, t), n in
                                Counter((h["seviye"], h["alan"], h["tur"]) for h in result["hatalar"]).most_common(10)]
    return result


def system_stats(outputs):
    stats = Counter()
    for out in outputs.values():
        stats["aday"] += 1
        stats[out["durum"]] += 1
        stats["ham_bulgu"] += len(out["ham"] or [])
        stats["dogrulanmis_bulgu"] += len(out["dogrulanmis"] or [])
    return dict(stats)


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

def _fmt(value):
    return "-" if value is None else f"{value:.3f}"


def write_report(report_dir, metrics):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    errors = metrics["degerlendirme"].pop("hatalar", [])
    (report_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")
    with (report_dir / "alanlar.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["seviye", "mod", "kume", "alan", "tp", "fp", "fn", "precision", "recall", "f1", "not"])
        for level, block in metrics["degerlendirme"]["seviyeler"].items():
            if "durum" in block:
                writer.writerow([level, "", "", "", "", "", "", "", "", "", block["durum"]])
                continue
            for mode, data in block.items():
                for kume in ("ana", "konservatif"):
                    agg = data[kume]
                    for name, v in agg["alanlar"].items():
                        writer.writerow([level, mode, kume, name, v["tp"], v["fp"], v["fn"], _fmt(v["precision"]),
                                         _fmt(v["recall"]), _fmt(v["f1"]), ""])
                    m = agg["micro"]
                    writer.writerow([level, mode, kume, "MICRO", m["tp"], m["fp"], m["fn"], _fmt(m["precision"]),
                                     _fmt(m["recall"]), _fmt(m["f1"]), ""])
                    writer.writerow([level, mode, kume, "MACRO", "", "", "", "", "", _fmt(agg["macro"]["f1"]), ""])
        for name in metrics["degerlendirilemez_alanlar"]:
            writer.writerow(["", "", "", name, "", "", "", "", "", "", "değerlendirilemez (sistem çıkarmıyor)"])
    with (report_dir / "kirilim.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["seviye", "mod", "kirilim", "deger", "aday", "tp", "fp", "fn", "precision", "recall", "f1"])
        for level, block in metrics["degerlendirme"]["seviyeler"].items():
            for mode, data in ({} if "durum" in block else block).items():
                for key, groups in data["kirilim"].items():
                    for name, agg in groups.items():
                        m = agg["micro"]
                        writer.writerow([level, mode, key, name, agg["aday_sayisi"], m["tp"], m["fp"], m["fn"],
                                         _fmt(m["precision"]), _fmt(m["recall"]), _fmt(m["f1"])])
    with (report_dir / "hatalar.csv").open("w", encoding="utf-8-sig", newline="") as file:
        columns = ["seviye", "mod", "aday_id", "dosya", "sayfa", "kaynak_turu", "alan", "tur", "gold", "sistem", "cumle"]
        writer = csv.DictWriter(file, fieldnames=columns, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        for e in sorted(errors, key=lambda h: (h["seviye"], h["aday_id"], h["alan"], h["tur"])):
            writer.writerow(e)
    return report_dir


def print_summary(metrics, out=print):
    if metrics.get("etiket"):
        out(f"*** {metrics['etiket']} ***")
    s = metrics["surumler"]
    out(f"pipeline_version: {s['pipeline_version']}   model: {s['model']}   gold_hash: {s['gold_hash']}")
    d = metrics["degerlendirme"]
    out(f"Gold: {metrics['gold_ozeti']}   değerlendirilen aday: {d['degerlendirilen_aday']}   "
        f"sistem durumu: {d['sistem_durumu']}")
    where = ("değerlendirilen adaylarda" if d["degerlendirilen_aday"]
             else "gold yok; --gold-olustur ile aynı örneklemin adaylarında")
    out(f"Sistem çıktısı ({where}): {metrics['sistem_ciktisi']}")
    if d["yanitsiz_adaylar"]:
        out(f"Yanıtı olmayan gold adayı: {len(d['yanitsiz_adaylar'])} (ana metrikte yok; konservatif satırda FN)")
    if not d["degerlendirilen_aday"]:
        out("Değerlendirilebilir (dogrulandi, hatasız) gold adayı yok: metrik hesaplanmadı.")
    for level, block in (d["seviyeler"] if d["degerlendirilen_aday"] else {}).items():
        if "durum" in block:
            out(f"[{level}] {block['durum']}")
            continue
        main = block["birebir"]["ana"]
        out(f"[{level} / birebir]  " + "  ".join(f"{n}: P {_fmt(v['precision'])} R {_fmt(v['recall'])} F1 {_fmt(v['f1'])}"
                                                for n, v in main["alanlar"].items()))
        boot = block["birebir"].get("bootstrap")
        out(f"   micro P {_fmt(main['micro']['precision'])} R {_fmt(main['micro']['recall'])} F1 "
            f"{_fmt(main['micro']['f1'])}" + (f" (%95 {_fmt(boot['micro_f1_alt'])}–{_fmt(boot['micro_f1_ust'])})" if boot else "")
            + f"   macro F1 {_fmt(main['macro']['f1'])}   bulgu F1 {_fmt(main['bulgu']['f1'])}   "
            f"gevşek micro F1 {_fmt(block['gevsek']['ana']['micro']['f1'])}   "
            f"konservatif micro F1 {_fmt(block['birebir']['konservatif']['micro']['f1'])}")
    out(f"Değerlendirilemez alanlar (sistem çıkarmıyor): {', '.join(metrics['degerlendirilemez_alanlar'])}")
    out(f"Rapor: {metrics.get('rapor_dir')}")


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

def gold_create(n, seed, output, overwrite=False, ctx=None, reference=None, gold_path=GOLD_FILE):
    from src.clinical_candidates import CANDIDATE_ID_VERSION
    from src.clinical_pipeline import PIPELINE_VERSION
    from src.data_quality import load_reference_rows

    output = Path(output)
    if output.exists() and not overwrite:
        raise SystemExit(f"{output} zaten var; üzerine yazmak için --uzerine-yaz (etiketleriniz gold_set.json'da "
                         "kayıtlıysa yeni dosyaya taşınır).")
    sources, to_send, _ = ctx or load_context()
    rows, _ = reference if reference is not None else load_reference_rows()
    by_aday = defaultdict(list)
    for row in rows:
        by_aday[row["aday_id"]].append(row)
    sample = draw_sample([i["candidate"] for i in to_send.values()], set(by_aday), n, seed)
    meta = {"seed": seed, "n": n, "pipeline_version": PIPELINE_VERSION, "candidate_id_version": CANDIDATE_ID_VERSION,
            "alan_listesi_surumu": FIELD_LIST_VERSION, "olusturma": datetime.now().isoformat(timespec="seconds")}
    workbook, counts = build_workbook(sample, by_aday, load_gold(gold_path), meta)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    strata = Counter((c["_havuz"], c["dosya"], c["kaynak_turu"]) for c in sample)
    return {"dosya": str(output), **counts, "havuz": dict(Counter(c["_havuz"] for c in sample)),
            "katmanlar": {f"{h} | {d} | {k}": v for (h, d, k), v in sorted(strata.items())}}


def gold_import(xlsx, output=GOLD_FILE, ctx=None):
    records, meta, havuz = read_workbook(xlsx)
    _, to_send, candidates = ctx or load_context()
    entries = validate_labels(records, candidates, set(to_send), havuz)
    gold = build_gold(entries, meta, Path(xlsx).name)
    save_gold(gold, output)
    return gold


def run_evaluation(*, gold_path=GOLD_FILE, source="cache", batch_dir=DEFAULT_BATCH_DIR, model=None,
                   pipeline_version=None, bootstrap_iterations=1000, seed=DEFAULT_SEED, n=DEFAULT_N,
                   report_dir=DEFAULT_REPORT_DIR, ctx=None, out=print):
    from src.clinical_pipeline import PROMPT_SHA

    gold = load_gold(gold_path)
    ctx = ctx or load_context()
    entries = evaluable_entries(gold) if gold else []
    ids = [e["aday_id"] for e in entries]
    if not ids:  # gold yoksa sistem istatistiği --gold-olustur ile AYNI örneklem üzerinden (metrik yok)
        from src.data_quality import load_reference_rows
        reference_ids = {r["aday_id"] for r in load_reference_rows()[0]}
        ids = [c["aday_id"] for c in draw_sample([i["candidate"] for i in ctx[1].values()], reference_ids, n, seed)]
    outputs, info = system_outputs(ids, ctx, source=source, batch_dir=batch_dir, model=model,
                                   pipeline_version=pipeline_version, warn=lambda m: out(f"UYARI: {m}"))
    result = evaluate(gold, outputs, bootstrap_iterations=bootstrap_iterations, seed=seed, candidates_by_id=ctx[2])
    status = Counter(a["durum"] for a in (gold or {}).get("adaylar", []))
    metrics = {
        "etiket": MOCK_LABEL if source == "mock" else None,
        "surumler": {"pipeline_version": info["pipeline_version"], "prompt_sha": PROMPT_SHA, "model": info.get("model"),
                     "gold_hash": (gold or {}).get("meta", {}).get("gold_hash"),
                     "gold_schema": (gold or {}).get("meta", {}).get("schema"),
                     "alan_listesi_surumu": FIELD_LIST_VERSION, "normalizasyon_surumu": control_version(),
                     "kaynak": source},
        "gold_ozeti": {"dosya": str(gold_path) if gold else "yok", **dict(sorted(status.items())),
                       "hatali": sum(bool(a["hatalar"]) for a in (gold or {}).get("adaylar", []))},
        "sistem_ciktisi": system_stats(outputs),
        "aktif_alanlar": [f.ad for f in ACTIVE_FIELDS],
        "degerlendirilemez_alanlar": [f.ad for f in GOLD_FIELDS if not f.aktif],
        "tanimlar": {"esleme": "aynı aday içinde normalize müdahale; en çok çift, sonra en çok alan uyuşması",
                     "gevsek": f"biri diğerini içeriyor veya token Jaccard ≥ {LOOSE_JACCARD}",
                     "ana": "yanıtı olan adaylar", "konservatif": "yanıtı olmayanlar boş çıktı sayılır"},
        "degerlendirme": result,
    }
    folder = Path(report_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{source}")
    metrics["rapor_dir"] = str(folder)
    write_report(folder, metrics)
    return metrics


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.evaluate_extraction",
                                     description="Gold set'e karşı alan bazında precision/recall/F1")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--gold-olustur", action="store_true", help="etiketleme xlsx'i üret")
    action.add_argument("--gold-yukle", metavar="XLSX", help="doldurulan xlsx'i okuyup gold_set.json'a kaydet")
    action.add_argument("--degerlendir", action="store_true", help="sistem çıktısını gold'a karşı değerlendir")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--from-cache", metavar="DIR", nargs="?", const=str(DEFAULT_BATCH_DIR),
                        help="toplu iş klasörü (varsayılan data/review/clinical_batch)")
    source.add_argument("--db", action="store_true", help="dogrulanmis seviye klinik_bulgular'dan (yalnız okuma)")
    source.add_argument("--mock", action="store_true", help="sahte LLM ile uçtan uca deneme")
    parser.add_argument("--model")
    parser.add_argument("--pipeline-version", help="varsayılan güncel PIPELINE_VERSION")
    parser.add_argument("--gold", type=Path, default=GOLD_FILE)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--bootstrap", type=int, default=1000, help="0 = kapalı")
    parser.add_argument("--cikti", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--uzerine-yaz", action="store_true")
    parser.add_argument("--rapor-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.gold_olustur:
        info = gold_create(args.n, args.seed, args.cikti, args.uzerine_yaz, gold_path=args.gold)
        print(f"Etiketleme dosyası: {info['dosya']}")
        print(f"Aday: {info['aday']}  satır: {info['satir']}  önceden doldurulmuş satır (taslak): "
              f"{info['onceden_doldurulmus_satir']}  korunan etiketli aday: {info['korunan_aday']}")
        print(f"Havuz: {info['havuz']}")
        for name, count in info["katmanlar"].items():
            print(f"  {name}: {count}")
        return 0
    if args.gold_yukle:
        gold = gold_import(args.gold_yukle, args.gold)
        meta = gold["meta"]
        print(f"Kaydedildi: {args.gold}   durumlar: {meta['durum_sayilari']}   hatalı aday: {meta['hatali_aday']}   "
              f"uyarı: {meta['uyari_sayisi']}   gold_hash: {meta['gold_hash']}")
        for entry in gold["adaylar"]:
            for problem in entry["hatalar"]:
                print(f"  HATA {entry['aday_id'][-12:]}: {problem}")
        return 0
    source = "mock" if args.mock else "db" if args.db else "cache"
    metrics = run_evaluation(gold_path=args.gold, source=source, batch_dir=Path(args.from_cache or DEFAULT_BATCH_DIR),
                             model=args.model, pipeline_version=args.pipeline_version,
                             bootstrap_iterations=args.bootstrap, seed=args.seed, n=args.n, report_dir=args.rapor_dir)
    print_summary(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
