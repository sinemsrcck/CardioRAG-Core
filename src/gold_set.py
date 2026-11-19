"""
Gold set: örneklem, etiketleme dosyası (xlsx) ve insan etiketlerinin okunması.

Gold set İNSAN tarafından doğrulanır. Kod hiçbir satırı "dogrulandi" yapmaz:
üretilen xlsx'te her satır durum='taslak' ile başlar; referans taslaktan gelen
değerler "referans_taslak (doğrulanmadı)" diye işaretlenir. Yalnızca insanın
durum='dogrulandi' yaptığı (ve hatasız) adaylar metriğe girer.

Birim = aday cümlesi (aday_id). Bir adayın BÜTÜN bulguları etiketlenir; bulgusu
olmayan aday "bulgu_yok = EVET" ile geçerlidir. Gold değerleri aday cümlesinden
birebir kopyalanır (span); referans önerileri taslak_referans sütunundadır.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.clinical_validation import ETKI_TURU, MUDAHALE_TURU
from src.data_quality import norm_mudahale, norm_text, norm_unit, norm_value

GOLD_SCHEMA = "gold-set-v1"
GOLD_FILE = Path("data/clinical/gold_set.json")
DEFAULT_XLSX = Path("data/review/gold/gold_etiketleme.xlsx")
DEFAULT_N, DEFAULT_SEED = 60, 42
REFERENCE_SOURCE = "referans_taslak (doğrulanmadı)"
# dogrulandi: YALNIZCA insanın elle kontrol ettiği adaylar. llm_etiketli: yapay zekânın
# (PDF'e bakarak) doldurduğu, insan doğrulaması OLMAYAN adaylar. Kod ve yapay zekâ
# hiçbir zaman "dogrulandi" yazmaz; iki küme değerlendirmede asla karıştırılmaz.
DURUMLAR = ("taslak", "dogrulandi", "llm_etiketli", "atla")
LLM_LABEL_WARNING = ("yapay zekâ etiketli, insan doğrulaması yok; sonuç sistem-etiketleyici uyumudur, "
                     "gerçek doğruluk değildir")


# ---------------------------------------------------------------------------
# Alan listesi — TEK YER. Yeni alan: buraya bir satır; aktif=True olunca metriğe girer.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoldField:
    ad: str
    tur: str            # mudahale | metin | deger | birim | enum | tamsayi
    ham: tuple          # LLM bulgusundaki alan(lar); () = sistem çıkarmıyor
    satir: tuple        # klinik_bulgular sütun(lar)ı (ilk dolu olan)
    aktif: bool         # False -> "değerlendirilemez (sistem çıkarmıyor)"
    kanit: bool         # True -> değer aday cümlesinde birebir geçmeli (uyarı)
    xlsx: bool = True
    secenekler: tuple = ()
    aciklama: str = ""


GOLD_FIELDS = (
    GoldField("mudahale", "mudahale", ("diagnostic_method", "drug", "procedure"), ("mudahale_en", "mudahale_tr"),
              True, True, aciklama="Tek müdahale: ilaç, girişimsel işlem veya tanı yöntemi (cümleden birebir)."),
    GoldField("mudahale_turu", "enum", ("<türetilir>",), ("mudahale_turu",), True, False,
              secenekler=tuple(sorted(MUDAHALE_TURU)), aciklama="tedavi / tanı testi / arka plan tedavisi."),
    GoldField("tani", "metin", ("diagnosis",), ("tani_en", "tani_tr"), True, True),
    GoldField("hasta_grubu", "metin", ("patient_group",), ("hasta_grubu",), True, True),
    GoldField("comparator", "metin", ("comparison",), ("comparator",), True, True),
    GoldField("doz", "deger", ("dose", "dose_unit"), ("doz",), True, True, aciklama="Doz ve birimi (ör. 100 mg)."),
    GoldField("sure", "deger", ("treatment_duration",), ("sure",), True, True),
    GoldField("deger", "deger", ("numeric_value",), ("deger",), True, True),
    GoldField("unit", "birim", ("value_unit",), ("unit",), True, True),
    GoldField("sonuc", "metin", ("clinical_outcome",), ("sonuc_olcutu_tr",), True, True),
    GoldField("p", "deger", ("p_value",), ("p",), True, True),
    GoldField("ci95", "deger", ("confidence_interval",), ("ci95",), True, True),
    GoldField("hasta_sayisi", "tamsayi", ("sample_size",), ("hasta_sayisi",), True, True,
              aciklama="Hasta/katılımcı sayısı (tam sayı; cümledeki sayı)."),
    GoldField("etki_turu", "enum", ("effect_measure",), ("etki_turu",), True, False,
              secenekler=tuple(sorted(v for v in ETKI_TURU if v))),
    GoldField("icd10", "metin", (), ("icd10",), False, False, xlsx=False),
)
FIELD_BY_NAME = {f.ad: f for f in GOLD_FIELDS}
XLSX_FIELDS = tuple(f for f in GOLD_FIELDS if f.xlsx)
ACTIVE_FIELDS = tuple(f for f in GOLD_FIELDS if f.aktif)
FIELD_LIST_VERSION = "alanlar-" + hashlib.sha256(json.dumps(
    [[f.ad, f.tur, f.ham, f.satir, f.aktif, f.kanit, f.xlsx, f.secenekler] for f in GOLD_FIELDS]
).encode("utf-8")).hexdigest()[:10]


def normalize(field, value):
    """data_quality normalizasyonu; boş -> None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value = str(value)
    if field.tur == "mudahale":
        result = norm_mudahale(value)
    elif field.tur == "deger":
        result = norm_value(value)
    elif field.tur == "birim":
        result = norm_unit(value)
    elif field.tur == "tamsayi":
        result = value.strip()
    elif field.tur == "enum":
        result = value.strip()
    else:
        result = norm_text(value)
    return result or None


def cell_text(value):
    """xlsx hücresi -> metin (Excel sayıya çevirdiyse geri)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def sentence_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Örneklem
# ---------------------------------------------------------------------------

def _rank(seed, aday_id):
    return hashlib.sha256(f"{seed}|{aday_id}".encode("utf-8")).hexdigest()


def _stratum(candidate):
    return (candidate["dosya"], candidate["kaynak_turu"])


def allocate(sizes, k):
    """Katman kontenjanları: k ≥ katman sayısıysa her katmana ≥1; kalan orantılı (en büyük kalan)."""
    strata = sorted(sizes)
    k = min(k, sum(sizes.values()))
    quota = {s: 0 for s in strata}
    if k <= 0:
        return quota
    if k >= len(strata):
        for s in strata:
            quota[s] = 1
    remaining = k - sum(quota.values())
    while remaining > 0:
        room = {s: sizes[s] - quota[s] for s in strata if sizes[s] > quota[s]}
        total = sum(room.values())
        shares = {s: remaining * room[s] / total for s in room}
        floors = {s: min(int(shares[s]), room[s]) for s in room}
        for s, v in floors.items():
            quota[s] += v
        remaining -= sum(floors.values())
        order = sorted(room, key=lambda s: (-(shares[s] - int(shares[s])), s))
        for s in order:
            if remaining <= 0:
                break
            if quota[s] < sizes[s]:
                quota[s] += 1
                remaining -= 1
    return quota


def stratified_pick(candidates, k, seed):
    groups = defaultdict(list)
    for c in candidates:
        groups[_stratum(c)].append(c)
    quota = allocate({s: len(v) for s, v in groups.items()}, k)
    picked = []
    for s in sorted(groups):
        ranked = sorted(groups[s], key=lambda c: _rank(seed, c["aday_id"]))
        picked += ranked[:quota[s]]
    return picked


def draw_sample(to_send_candidates, reference_ids, n=DEFAULT_N, seed=DEFAULT_SEED):
    """~2/3 referansla eşleşen adaylardan, ~1/3 diğerlerinden; katman = (dosya, kaynak_turu)."""
    pool_r = [c for c in to_send_candidates if c["aday_id"] in reference_ids]
    pool_o = [c for c in to_send_candidates if c["aday_id"] not in reference_ids]
    n = min(n, len(pool_r) + len(pool_o))
    n_r = min(round(2 * n / 3), len(pool_r))
    n_o = min(n - n_r, len(pool_o))
    n_r = min(n - n_o, len(pool_r))
    sample = [dict(c, _havuz="R") for c in stratified_pick(pool_r, n_r, seed)]
    sample += [dict(c, _havuz="O") for c in stratified_pick(pool_o, n_o, seed)]
    return sorted(sample, key=lambda c: (c["dosya"], c["sayfa"], c["aday_id"]))


# ---------------------------------------------------------------------------
# Referans taslaktan önceden doldurma
# ---------------------------------------------------------------------------

REFERENCE_COLUMNS = {
    "mudahale": ("mudahale_en", "mudahale_tr"), "mudahale_turu": ("mudahale_turu",),
    "tani": ("tani_en", "tani_tr"), "hasta_grubu": ("hasta_grubu",), "comparator": ("comparator",),
    "doz": ("doz",), "sure": ("sure",), "deger": ("deger",), "unit": ("unit",),
    "sonuc": ("sonuc_olcutu_tr",), "p": ("p",), "ci95": ("ci95",), "hasta_sayisi": ("hasta_sayisi",),
    "etki_turu": ("etki_turu",),
}


def in_sentence(value, sentence):
    return bool(value) and norm_text(value) in norm_text(sentence)


def prefill_from_reference(reference_row, sentence):
    """(hücreler, öneriler): birebir geçen değer hücreye, geçmeyen öneri olarak.

    enum alanları (mudahale_turu, etki_turu) cümlede geçmez; taslak değer olarak hücreye yazılır.
    """
    cells, suggestions = {}, []
    for field in XLSX_FIELDS:
        values = [cell_text(reference_row.get(c)) for c in REFERENCE_COLUMNS.get(field.ad, ())]
        values = [v for v in values if v]
        if not values:
            continue
        if field.tur == "enum":
            if values[0] in field.secenekler:
                cells[field.ad] = values[0]
            continue
        verbatim = next((v for v in values if in_sentence(v, sentence)), None)
        if verbatim:
            cells[field.ad] = verbatim
        else:
            suggestions.append(f"{field.ad}: {values[0]}")
    return cells, suggestions


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------

LEAD_COLUMNS = ("aday_id", "dosya", "sayfa", "kaynak_turu", "aday_cumlesi", "bulgu_no")
TAIL_COLUMNS = ("bulgu_yok", "durum", "kaynak", "taslak_referans", "not")
LABEL_COLUMNS = LEAD_COLUMNS + tuple(f.ad for f in XLSX_FIELDS) + TAIL_COLUMNS

HOWTO = [
    "Gold set etiketleme — nasıl doldurulur",
    "",
    "1. 'Etiketler' sayfasında her satır bir BULGUDUR. Bir adayın (aday_id) bütün bulgularını etiketleyin.",
    "2. Değerleri aday cümlesinden BİREBİR kopyalayın (çeviri, özet veya çıkarım yok).",
    "3. Bir bulgu = tek müdahale (ilaç, girişimsel işlem veya tanı yöntemi). İki müdahale varsa iki satır.",
    "4. Yeni bulgu: satırı kopyalayıp altına yapıştırın, bulgu_no'yu artırın (aday_id aynı kalır).",
    "5. Bulgusu olmayan aday: bir satırda alanları boş bırakın, bulgu_yok = EVET.",
    "6. durum: taslak (henüz bakılmadı) / dogrulandi (İNSAN kontrol etti, doğru) / llm_etiketli (yapay zekâ",
    "   doldurdu, insan doğrulaması yok) / atla (değerlendirme dışı). Bir adayın bütün satırları aynı durumda olmalı.",
    "   'dogrulandi' yalnızca elle kontrol eden insan tarafından yazılır; kod ve yapay zekâ asla yazmaz.",
    "   'dogrulandi' ve 'llm_etiketli' adaylar ayrı ayrı değerlendirilir, asla karıştırılmaz.",
    "7. Sarı hücreler referans taslaktan önceden doldurulmuştur (kaynak = referans_taslak (doğrulanmadı));",
    "   kontrol etmeden 'dogrulandi' yapmayın. taslak_referans sütunu cümlede birebir geçmeyen önerilerdir.",
    "8. mudahale_turu ve etki_turu açılır listeden seçilir. p ve ci95 cümleden birebir; hasta_sayisi tam sayı.",
    "9. Bitince: python -m src.evaluate_extraction --gold-yukle <bu dosya>",
    "",
    "Alanlar:",
]


def build_workbook(sample, reference_by_aday, existing_gold=None, meta=None):
    """openpyxl çalışma kitabı. Var olan gold_set.json'daki etiketler ve durumlar korunur."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    existing = {a["aday_id"]: a for a in (existing_gold or {}).get("adaylar", [])}
    wb = Workbook()
    howto = wb.active
    howto.title = "Nasıl doldurulur"
    for line in HOWTO:
        howto.append([line])
    for f in XLSX_FIELDS:
        howto.append([f"  {f.ad}: {f.aciklama or ('cümleden birebir' if f.kanit else 'listeden seçin')}"
                      + ("" if f.aktif else "  [sistem henüz çıkarmıyor]")])
    howto.append([])
    for key, value in (meta or {}).items():
        howto.append([f"{key}: {value}"])
    howto.column_dimensions["A"].width = 120

    sheet = wb.create_sheet("Etiketler")
    sheet.append(list(LABEL_COLUMNS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    yellow = PatternFill("solid", fgColor="FFF2CC")
    col = {name: i + 1 for i, name in enumerate(LABEL_COLUMNS)}
    counts = {"aday": 0, "satir": 0, "onceden_doldurulmus_satir": 0, "korunan_aday": 0}

    candidates = {c["aday_id"]: c for c in sample}
    for entry in existing.values():  # örneklem dışına düşmüş etiketli adaylar kaybolmasın
        candidates.setdefault(entry["aday_id"], {"aday_id": entry["aday_id"], "dosya": entry.get("dosya"),
                                                 "sayfa": entry.get("sayfa"), "kaynak_turu": entry.get("kaynak_turu"),
                                                 "cumle": entry.get("cumle", ""), "_havuz": entry.get("havuz", "")})
    for aday_id in sorted(candidates, key=lambda a: (str(candidates[a]["dosya"]), candidates[a]["sayfa"] or 0, a)):
        c = candidates[aday_id]
        counts["aday"] += 1
        base = {"aday_id": aday_id, "dosya": c["dosya"], "sayfa": c["sayfa"], "kaynak_turu": c["kaynak_turu"],
                "aday_cumlesi": c["cumle"]}
        rows = []
        if aday_id in existing:
            counts["korunan_aday"] += 1
            entry = existing[aday_id]
            findings = entry.get("bulgular") or [{}]
            for number, finding in enumerate(findings, start=1):
                rows.append(({**base, "bulgu_no": number, **{k: v for k, v in finding.items() if v is not None},
                              "bulgu_yok": "EVET" if entry.get("bulgu_yok") else "", "durum": entry.get("durum", "taslak"),
                              "kaynak": entry.get("kaynak", ""), "not": entry.get("not", "")}, set()))
        elif reference_by_aday.get(aday_id):
            for number, ref in enumerate(reference_by_aday[aday_id], start=1):
                cells, suggestions = prefill_from_reference(ref, c["cumle"])
                rows.append(({**base, "bulgu_no": number, **cells, "durum": "taslak", "kaynak": REFERENCE_SOURCE,
                              "taslak_referans": "; ".join(suggestions)}, set(cells)))
        else:
            rows.append(({**base, "bulgu_no": 1, "durum": "taslak", "kaynak": "boş"}, set()))
        for values, prefilled in rows:
            sheet.append([values.get(name, "") for name in LABEL_COLUMNS])
            counts["satir"] += 1
            if prefilled:
                counts["onceden_doldurulmus_satir"] += 1
            for name in prefilled:
                sheet.cell(row=sheet.max_row, column=col[name]).fill = yellow
            sheet.cell(row=sheet.max_row, column=col["aday_cumlesi"]).alignment = Alignment(wrap_text=True, vertical="top")

    last = max(sheet.max_row, 2) + 500
    def letter(name):
        from openpyxl.utils import get_column_letter
        return get_column_letter(col[name])
    validations = [("durum", DURUMLAR), ("bulgu_yok", ("EVET",))]
    validations += [(f.ad, f.secenekler) for f in XLSX_FIELDS if f.secenekler]
    for name, options in validations:
        dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
        dv.add(f"{letter(name)}2:{letter(name)}{last}")
        sheet.add_data_validation(dv)
    widths = {"aday_id": 20, "dosya": 22, "sayfa": 6, "kaynak_turu": 9, "aday_cumlesi": 70, "bulgu_no": 8,
              "taslak_referans": 40, "not": 30, "kaynak": 24}
    for name in LABEL_COLUMNS:
        sheet.column_dimensions[letter(name)].width = widths.get(name, 16)
    sheet.freeze_panes = "G2"
    sheet.auto_filter.ref = f"A1:{letter(LABEL_COLUMNS[-1])}{sheet.max_row}"

    overview = wb.create_sheet("Adaylar")
    overview.append(["aday_id", "dosya", "sayfa", "kaynak_turu", "havuz", "katman"])
    for aday_id in sorted(candidates):
        c = candidates[aday_id]
        overview.append([aday_id, c["dosya"], c["sayfa"], c["kaynak_turu"],
                         {"R": "referans eşleşmeli", "O": "diğer"}.get(c.get("_havuz"), c.get("_havuz") or ""),
                         f"{c['dosya']} / {c['kaynak_turu']}"])
    meta_sheet = wb.create_sheet("_meta")
    meta_sheet["A1"] = json.dumps(meta or {}, ensure_ascii=False)
    meta_sheet.sheet_state = "hidden"
    return wb, counts


def read_workbook(path):
    """(satırlar, meta) — başlık adlarına göre okur (sütun sırası değişse de çalışır)."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    sheet = wb["Etiketler"]
    rows = list(sheet.iter_rows(values_only=True))
    header = [cell_text(h) for h in rows[0]]
    records = []
    for number, values in enumerate(rows[1:], start=2):
        record = {name: cell_text(values[i]) if i < len(values) else "" for i, name in enumerate(header) if name}
        if record.get("aday_id"):
            record["_satir"] = number
            records.append(record)
    meta = {}
    if "_meta" in wb.sheetnames:
        try:
            meta = json.loads(wb["_meta"]["A1"].value or "{}")
        except ValueError:
            meta = {}
    havuz = {}
    if "Adaylar" in wb.sheetnames:
        for values in list(wb["Adaylar"].iter_rows(values_only=True))[1:]:
            if values and values[0]:
                havuz[cell_text(values[0])] = {"referans eşleşmeli": "R", "diğer": "O"}.get(cell_text(values[4]), "")
    return records, meta, havuz


# ---------------------------------------------------------------------------
# Okuma + doğrulama -> gold_set.json
# ---------------------------------------------------------------------------

def validate_labels(records, candidates_by_id, to_send_ids, havuz_by_id=None):
    """Satırları aday bazında toplar; hata (metriğe girmez) ve uyarıları ekler."""
    grouped = defaultdict(list)
    for record in records:
        grouped[record["aday_id"]].append(record)
    entries = []
    for aday_id in sorted(grouped):
        rows = sorted(grouped[aday_id], key=lambda r: (r.get("bulgu_no") or "", r["_satir"]))
        errors, warnings = [], []
        candidate = candidates_by_id.get(aday_id)
        if candidate is None:
            errors.append("aday_id mevcut adaylarda yok")
        elif aday_id not in to_send_ids:
            warnings.append("aday LLM'e gönderilmiyor (toplu işte ayrılmış); sistem çıktısı olmayacak")
        statuses = {(r.get("durum") or "taslak") for r in rows}
        status = statuses.pop() if len(statuses) == 1 else "tutarsiz"
        if status == "tutarsiz":
            errors.append(f"satırların durumu farklı: {sorted({r.get('durum') or 'taslak' for r in rows})}")
        elif status not in DURUMLAR:
            errors.append(f"geçersiz durum: '{status}'")
        empty = [r for r in rows if not any(r.get(f.ad) for f in XLSX_FIELDS)]
        no_finding = any((r.get("bulgu_yok") or "").upper() == "EVET" for r in rows)
        findings = []
        for r in rows:
            values = {f.ad: (r.get(f.ad) or None) for f in GOLD_FIELDS}
            if not any(values.values()):
                continue
            label = f"satır {r['_satir']}"
            if not values["mudahale"]:
                errors.append(f"{label}: mudahale boş (bir bulgu = tek müdahale)")
            for f in XLSX_FIELDS:
                v = values.get(f.ad)
                if not v:
                    continue
                if f.secenekler and v not in f.secenekler:
                    errors.append(f"{label}: {f.ad} '{v}' izinli değerlerden değil")
                if f.tur == "tamsayi" and not v.isdigit():
                    warnings.append(f"{label}: {f.ad} '{v}' tam sayı değil")
                if f.kanit and candidate and not in_sentence(v, candidate["cumle"]):
                    warnings.append(f"{label}: {f.ad} '{v}' aday cümlesinde birebir geçmiyor")
            findings.append(values)
        if no_finding and findings:
            errors.append("bulgu_yok = EVET ama bulgu alanları dolu")
        if status in ("dogrulandi", "llm_etiketli") and not findings and not no_finding:
            errors.append(f"{status} aday boş: bulgu yoksa bulgu_yok = EVET işaretleyin")
        first = rows[0]
        entries.append({
            "aday_id": aday_id,
            "dosya": candidate["dosya"] if candidate else first.get("dosya"),
            "makale_id": candidate.get("makale_id") if candidate else None,
            "sayfa": candidate["sayfa"] if candidate else first.get("sayfa"),
            "kaynak_turu": candidate["kaynak_turu"] if candidate else first.get("kaynak_turu"),
            "havuz": (havuz_by_id or {}).get(aday_id, ""),
            "cumle": candidate["cumle"] if candidate else first.get("aday_cumlesi"),
            "cumle_sha256": sentence_hash(candidate["cumle"]) if candidate else None,
            "durum": status, "bulgu_yok": no_finding and not findings,
            "bulgular": [] if no_finding else findings,
            "kaynak": first.get("kaynak", ""), "not": " | ".join(r["not"] for r in rows if r.get("not")),
            "hatalar": errors, "uyarilar": warnings + ([f"{len(empty)} boş satır yok sayıldı"]
                                                       if empty and findings else []),
        })
    return entries


def evaluable_entries(gold, durum="dogrulandi"):
    """Tek bir kümenin hatasız adayları: 'dogrulandi' (insan) veya 'llm_etiketli' (yapay zekâ)."""
    if durum not in ("dogrulandi", "llm_etiketli"):
        raise ValueError("değerlendirme kümesi 'dogrulandi' veya 'llm_etiketli' olmalı")
    return [a for a in (gold or {}).get("adaylar", []) if a["durum"] == durum and not a["hatalar"]]


def gold_hash(gold, durum="dogrulandi"):
    payload = [{"aday_id": a["aday_id"], "bulgu_yok": a["bulgu_yok"], "bulgular": a["bulgular"],
                "cumle_sha256": a["cumle_sha256"]}
               for a in sorted(evaluable_entries(gold, durum), key=lambda a: a["aday_id"])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build_gold(entries, meta, source_name):
    status_counts = defaultdict(int)
    for e in entries:
        status_counts[e["durum"]] += 1
    gold = {
        "meta": {
            "schema": GOLD_SCHEMA, "alan_listesi_surumu": FIELD_LIST_VERSION,
            "insan_dogrulamasi": "yalnız durum=dogrulandi ve hatasız adaylar 'insan_dogrulanmis' metriğine girer; "
                                 "llm_etiketli adaylar ayrı kümededir (" + LLM_LABEL_WARNING + ")",
            "kaynak_dosya": source_name, "olusturma": datetime.now().isoformat(timespec="seconds"),
            "ornekleme": {k: meta.get(k) for k in ("seed", "n", "pipeline_version", "candidate_id_version")},
            "durum_sayilari": dict(sorted(status_counts.items())),
            "hatali_aday": sum(bool(e["hatalar"]) for e in entries),
            "uyari_sayisi": sum(len(e["uyarilar"]) for e in entries),
        },
        "adaylar": entries,
    }
    gold["meta"]["gold_hash"] = gold_hash(gold)
    gold["meta"]["llm_etiketli_hash"] = gold_hash(gold, "llm_etiketli")
    return gold


def load_gold(path=GOLD_FILE):
    path = Path(path)
    if not path.exists():
        return None
    gold = json.loads(path.read_text(encoding="utf-8"))
    if gold.get("meta", {}).get("schema") != GOLD_SCHEMA:
        raise ValueError(f"Gold şeması beklenen değil: {gold.get('meta', {}).get('schema')}")
    return gold


def save_gold(gold, path=GOLD_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
