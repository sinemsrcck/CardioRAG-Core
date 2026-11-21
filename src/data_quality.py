"""
Klinik bulgularda tekilleştirme ve veri kalitesi işaretleri.

    python -m src.data_quality --kur                     # her veritabanında bir kez
    python -m src.data_quality --dry-run                 # rapor; tabloya yazmaz
    python -m src.data_quality                           # kalite_isaretleri'ni yeniler
    python -m src.data_quality --jsonl runs/X/would_save.jsonl
    python -m src.data_quality --referans --dry-run      # doğrulanmamış referans taslak
    python -m src.data_quality --pilot                   # + bilgi_turu dolu pilot satırları (pdf_pages kontrolleri)

klinik_bulgular'a yalnızca SELECT atılır; hiçbir satır silinmez/değişmez,
review_status'a dokunulmaz. İşaretler türetilmiş veridir: kimlik satir_hash'tir
(veritabanı id'si makineden makineye değişir), grup_id üyelerin satir_hash'inden
türetilir; aynı kontrol sürümüyle aynı girdi aynı işaretleri verir. LLM kullanılmaz;
benzerlik varsayılan olarak kelime Jaccard, isteğe bağlı yerel minilm embedding.
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.clinical_validation import norm_ws

KALITE_FILE = Path(__file__).resolve().parent.parent / "sql" / "kalite_core.sql"
DEFAULT_REPORT_DIR = Path("data/review/kalite")
REFERENCE_LABEL = "referans_taslak: doğrulanmamış veri"

# ---------------------------------------------------------------------------
# Kontrol tanımları (kod -> kategori); seviyeler kontrol içinde belirlenir
# ---------------------------------------------------------------------------

CHECKS = {
    "tekrar_ayni_bilgi": "tekrar", "tekrar_yalniz_mudahale": "tekrar",
    "tekrar_metin_tablo": "tekrar", "tekrar_farkli_sayfa": "tekrar",
    "tekrar_benzer": "tekrar", "hash_cakismasi_kayip_bulgu": "tekrar",
    "yuzde_100_ustu": "sayisal", "p_aralik_disi": "sayisal", "p_bicim_supheli": "sayisal",
    "ci_ters": "sayisal", "deger_ci_disinda": "sayisal", "oran_sifir_veya_negatif": "sayisal",
    "auc_aralik_disi": "sayisal", "negatif_sayi": "sayisal", "deger_birimsiz": "sayisal",
    "doz_birimsiz": "sayisal", "sayi_ayristirilamadi": "sayisal", "ondalik_belirsiz": "sayisal",
    "kaynak_alani_bos": "kaynak", "chunk_id_bos": "kaynak", "belge_yok": "kaynak",
    "chunk_yok": "kaynak", "chunk_eski_surum": "kaynak", "chunk_belge_uyusmuyor": "kaynak",
    "kanit_chunkta_yok": "kaynak", "kanit_sayfa_uyusmuyor": "kaynak",
    "makale_belge_uyusmuyor": "kaynak", "dogrulama_artik_gecmiyor": "kaynak",
    "farkli_doz": "tutarsizlik", "farkli_hasta_sayisi": "tutarsizlik", "farkli_deger": "tutarsizlik",
    "mudahale_turu_kaynak_uyusmuyor": "tutarsizlik", "taslak_notu_okunamadi": "tutarsizlik",
    "dil_uyusmuyor": "tutarsizlik",
    # İstatistiksel tutarlılık (kaynak makalenin raporladığı değerler; kayıtlar değişmez).
    "ci_p_celiskisi": "sayisal", "kategorik_p_yeniden_uretilemedi": "tutarsizlik",
    "surekli_p_supheli": "tutarsizlik", "model_etki_olcusu_celiskisi": "tutarsizlik",
}
# Pilot (bilgi_turu dolu) satırların SQL sonrası kontrolleri. Ayrı sözlük: --pilot verilmeyen
# çalıştırmaların sürüm hash'i değişmesin. Kategoriler mevcut ENUM'un elemanları (şema değişmez).
PILOT_CHECKS = {
    "pilot_pdf_okunamadi": "kaynak", "pilot_kanit_sayfada_yok": "kaynak", "pilot_bilgi_kanitta_yok": "kaynak",
    "pilot_sayfa_gecisi_supheli": "kaynak", "pilot_kanitta_sekil_tablo": "kaynak",
    "tablo_cift_eksik": "tutarsizlik", "tablo_cift_tutarsiz": "tutarsizlik",
    "pilot_yuzde_uyumsuz": "sayisal",
    "kaynak_p_bicim_bozuk": "tutarsizlik", "kaynak_ci_p_celiski": "tutarsizlik",
    "pilot_tekrar_metin_tablo": "tekrar", "pilot_tekrar_metin_metin": "tekrar", "pilot_tekrar_belirsiz": "tekrar",
    "pilot_baglam_tablo": "tutarsizlik", "pilot_ilac_doz_oran": "tutarsizlik",
}
ALL_CHECKS = {**CHECKS, **PILOT_CHECKS}
PILOT_JACCARD = 0.3          # metin ↔ metin tekrarında anahtar kelime örtüşmesi
PILOT_MIN_SHARED_NUMBERS = 2  # metin ↔ metin tekrarında ortak anlamlı sayı
PILOT_SIGNIFICANCE = 0.05
# r2: rakamla başlayan kanıt sayfa geçişi şüphesinde bilgi (parantez/noktalama uyari kalır).
# r3: kaynak_ci_p_celiski ortak ci_p_assessment kuralını kullanır (AUC dahil; '<001' gibi belirsiz p atlanır).
PILOT_RULES_REVISION = 3
DEFAULT_PDF_DIR = Path("data/pdfs")
PILOT_WARNING = ("pilot satırlar var ama --pilot verilmedi; v_kalite_ozet son sürümü gösterdiği için "
                 "pilot işaretleri görünmeyecek")
LEVEL_ORDER = {"hata": 0, "uyari": 1, "bilgi": 2}

# Müdahale eşanlamlıları: YALNIZCA kesin eşdeğerler (sürüm hash'ine dahil).
MUDAHALE_ESANLAMLI = {
    "pci": ("pci", "percutaneous coronary intervention", "percutaneous coronary interventions",
            "perkütan koroner girişim"),
    "cabg": ("cabg", "coronary artery bypass grafting", "coronary artery bypass graft",
             "coronary artery bypass graft surgery", "koroner arter baypas greftleme"),
    "doac": ("doac", "doacs", "noac", "noacs", "direct oral anticoagulant", "direct oral anticoagulants",
             "non-vitamin k antagonist oral anticoagulant", "non-vitamin k antagonist oral anticoagulants"),
    "laao": ("laao", "left atrial appendage occlusion", "sol atriyal apendiks oklüzyonu"),
    "vka": ("vka", "vkas", "vitamin k antagonist", "vitamin k antagonists"),
    "beta-blocker": ("beta-blocker", "beta-blockers", "beta blocker", "beta blockers", "betablocker",
                     "betablockers", "beta-bloker", "beta bloker", "betabloker", "betablokerler"),
    "aspirin": ("aspirin", "acetylsalicylic acid", "asetilsalisilik asit"),
    "ace inhibitor": ("ace inhibitor", "ace inhibitors", "ace-inhibitor", "ace-inhibitors", "acei", "aceis",
                      "angiotensin-converting enzyme inhibitor", "angiotensin-converting enzyme inhibitors"),
}
# Birim yazımları -> tek biçim (sürüm hash'ine dahil). Sıra önemlidir.
UNIT_REPLACEMENTS = (
    (r"[µμ]g\b|\bug\b", "mcg"),
    (r"\bper day\b|\bdaily\b|/\s*gün\b|\bgünlük\b|/\s*d\b", "/day"),
    (r"\bpercent\b|\byüzde\b", "%"),
    (r"\s*/\s*", "/"),
)
DOSE_UNIT = re.compile(r"(?<![a-z])(?:mg|mcg|g|iu|u|ml|mmol|meq|units?)(?![a-z])")
UNIT_TOKEN = re.compile(r"%|(?<![a-z])(?:mg|mcg|g|iu|u|ml|mmol|meq|mmhg|kg|cm|mm|bpm|ng|pg|l|dl|min|h|"
                        r"days?|months?|years?|weeks?|hours?|yıl|ay|gün|hafta)(?![a-z])")
RATIO_TYPES = {"OR", "RR", "HR", "IRR"}
AUC_TYPE = "AUC / C-istatistigi"
UNITLESS_TYPES = RATIO_TYPES | {AUC_TYPE, "oran / yuzde"}
PROPORTION_WORDS = re.compile(r"sensitivit|specificit|accuracy|prevalence|duyarlılı|özgüllü|doğruluk|yaygınlı",
                              re.IGNORECASE)
JACCARD_THRESHOLD = 0.8
EMBEDDING_THRESHOLD = 0.92
# Kontrol MANTIĞI (ayrıştırma, kural koşulları) değişince elle artırılır; listeler,
# eşikler ve eşanlamlılar zaten hash'e kendiliğinden girer.
# r2: boşluklu eksi işareti ("− 4.4") negatif sayılır; "0.5 - 0.9" aralık kalır.
# r3: istatistiksel tutarlılık kontrolleri (GA–p, kategorik/sürekli p yeniden hesaplama, model terimi).
# r4: kaynak tutarsızlıkları en fazla "uyari" (ci_p_celiskisi); --pilot'ta pilot satırların GA–p ve
#     yüzde aritmetiği yalnız pilot kodlarıyla (kaynak_ci_p_celiski, pilot_yuzde_uyumsuz) işaretlenir.
RULES_REVISION = 4
# GA–p çelişkisinde en yakın GA sınırının boş değere (oranlar için 1, log ölçeği; AUC için 0.5)
# uzaklığı bu eşiğin altındaysa farklı GA/test yöntemleri sapma üretebilir: "uyari"; üstünde "hata".
CI_P_SINIR_MARJI = 0.05
# Sürekli değişken: bildirilen p anlamsızken |t| bu eşiğin üstündeyse (ya da p ≤ 0.01 iken |t| alt
# eşiğin altındaysa) "uyari". ± = SS, n = grup başlığı ve Welch t varsayımdır; asla "hata" değildir.
SUREKLI_T_UST = 3.5
SUREKLI_T_ALT = 1.0
SUREKLI_MIN_N = 30


def config_payload(embedding=False, embedding_threshold=EMBEDDING_THRESHOLD, pilot=False):
    payload = {"checks": CHECKS, "esanlamli": MUDAHALE_ESANLAMLI, "birim": UNIT_REPLACEMENTS,
               "jaccard": JACCARD_THRESHOLD, "embedding": embedding,
               "embedding_esik": embedding_threshold if embedding else None,
               "kurallar": f"kalite-v1-r{RULES_REVISION}"}
    if pilot:  # yalnız --pilot: pilot'suz çalıştırmaların hash'i aynı kalır
        payload["pilot"] = {"checks": PILOT_CHECKS, "jaccard": PILOT_JACCARD,
                            "ortak_sayi": PILOT_MIN_SHARED_NUMBERS, "anlamlilik": PILOT_SIGNIFICANCE,
                            "kurallar": f"pilot-r{PILOT_RULES_REVISION}"}
    return payload


def control_version(embedding=False, embedding_threshold=EMBEDDING_THRESHOLD, payload=None, pilot=False):
    payload = payload or config_payload(embedding, embedding_threshold, pilot)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return "kalite-v1-" + digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Normalizasyon
# ---------------------------------------------------------------------------

_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-"})
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_SYNONYM_INDEX = {alias: canonical for canonical, aliases in MUDAHALE_ESANLAMLI.items() for alias in aliases}


def norm_text(value):
    """NFKC + casefold (İ'nin birleşik noktası atılır, ı korunur) + boşluk/noktalama."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"</?(?:sup|sub|b|i|em|strong|mark)>", "", text)
    text = text.replace("**", "").replace("__", "").translate(_DASHES).translate(_QUOTES)
    text = text.casefold().replace("̇", "")
    text = re.sub(r"(?<!\w)_|_(?!\w)", "", text)
    return re.sub(r"\s+", " ", text).strip(" .,;:")


def norm_mudahale(value):
    text = norm_text(value).replace("β", "beta")
    text = re.sub(r"\bbeta\s*-\s*", "beta-", text)
    if text in _SYNONYM_INDEX:
        return _SYNONYM_INDEX[text]
    if text.endswith("s") and text[:-1] in _SYNONYM_INDEX:
        return _SYNONYM_INDEX[text[:-1]]
    return text


def _decimal(text):
    text = text.replace("·", ".")
    return re.sub(r"(?<!\d)(\d+),(\d{1,2})(?!\d)", r"\1.\2", text)


def norm_unit(value):
    text = norm_text(value)
    for pattern, replacement in UNIT_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    return text.strip()


def norm_value(value):
    """Değer/doz: metin + ondalık + birim yazımı + sayı-birim boşluğu."""
    text = _decimal(norm_text(value))
    for pattern, replacement in UNIT_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    text = re.sub(r"(\d)(mg|mcg|g|iu|ml|mmol|meq)\b", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


NUMBER = re.compile(r"(?<![\w.,])(?P<neg>-\s?)?(?P<num>\d+(?:[.,]\d+)?)(?P<pct>\s*%)?")


def parse_numbers(value):
    """[{value, percent, raw, leading_zero, ambiguous}].

    Eksi işareti sayıya bitişik ya da tek boşlukla ayrık olabilir ("− 4.4"). Tireden
    önceki ilk dolu karakter rakam veya % ise tire aralık ayırıcısıdır ("0.5 - 0.9",
    "0.682–0.889"), negatif işaret sayılmaz.
    """
    if value is None:
        return []
    text = unicodedata.normalize("NFKC", str(value)).translate(_DASHES).replace("·", ".")
    numbers = []
    for match in NUMBER.finditer(text):
        raw = match.group("num")
        ambiguous = bool(re.fullmatch(r"\d{1,3},\d{3}", raw))
        number = None if ambiguous else float(raw.replace(",", "."))
        previous = text[:match.start()].rstrip()[-1:]
        negative = bool(match.group("neg")) and not (previous.isdigit() or previous == "%")
        if number is not None and negative:
            number = -number
        numbers.append({"value": number, "percent": bool(match.group("pct")), "raw": raw,
                        "leading_zero": bool(re.fullmatch(r"0\d+", raw)), "ambiguous": ambiguous})
    return numbers


def tokens(text):
    return set(re.findall(r"\w+", norm_text(text)))


def jaccard(a, b):
    a, b = tokens(a), tokens(b)
    return len(a & b) / len(a | b) if a and b else 0.0


# ---------------------------------------------------------------------------
# Satır görünümü
# ---------------------------------------------------------------------------

def _note(row):
    raw = row.get("taslak_notu")
    if not raw:
        return None
    try:
        note = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return note if isinstance(note, dict) else None


def outcome_of(row):
    if row.get("sonuc_olcutu_tr"):
        return row["sonuc_olcutu_tr"]
    note = _note(row) or {}
    fields = note.get("extracted_fields") if isinstance(note.get("extracted_fields"), dict) else {}
    return fields.get("clinical_outcome") or (note.get("clinical_result") or {}).get("original_text")


def mudahale_of(row):
    return row.get("mudahale_en") or row.get("mudahale_tr")


def view(row):
    """Karşılaştırma anahtarları (normalize)."""
    return {
        "makale": row.get("makale_id") or "", "mudahale": norm_mudahale(mudahale_of(row)),
        "doz": norm_value(row.get("doz")), "deger": norm_value(row.get("deger")),
        "unit": norm_unit(row.get("unit")), "hasta": norm_text(row.get("hasta_grubu")),
        "sonuc": norm_text(outcome_of(row)),
    }


def group_id(code, hashes):
    return "G-" + hashlib.sha256((code + "|" + "|".join(sorted(hashes))).encode("utf-8")).hexdigest()[:16]


def _flag(row, code, level, explanation, group="", detail=None):
    return {"satir_hash": row["satir_hash"], "klinik_bulgu_id": row.get("id"), "kontrol_kodu": code,
            "kategori": ALL_CHECKS[code], "seviye": level, "grup_id": group, "aciklama": explanation,
            "ayrinti": detail or {}}


# ---------------------------------------------------------------------------
# a) Tekrar
# ---------------------------------------------------------------------------

def _representative_order(row):
    return (row.get("kanit_turu") != "metin", int(row.get("kanit_sayfa") or 0), row["satir_hash"])


def check_duplicates(rows, embedder=None, embedding_threshold=EMBEDDING_THRESHOLD):
    flags = []
    views = {r["satir_hash"]: view(r) for r in rows}
    exact = defaultdict(list)
    for row in rows:
        v = views[row["satir_hash"]]
        if not v["mudahale"]:
            continue
        exact[(v["makale"], v["mudahale"], v["doz"], v["deger"], v["unit"], v["hasta"], v["sonuc"])].append(row)
    exact_key_of = {}
    for key, members in exact.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=_representative_order)
        hashes = [m["satir_hash"] for m in members]
        for m in members:
            exact_key_of[m["satir_hash"]] = key
        only_mudahale = not any(key[2:])
        code = "tekrar_yalniz_mudahale" if only_mudahale else "tekrar_ayni_bilgi"
        gid = group_id(code, hashes)
        pages = sorted({int(m.get("kanit_sayfa") or 0) for m in members})
        detail = {"uyeler": hashes, "temsilci": hashes[0], "sayfalar": pages}
        for index, m in enumerate(members):
            level = "bilgi" if (only_mudahale or index == 0) else "uyari"
            flags.append(_flag(m, code, level,
                               f"{len(members)} satır aynı bilgiyi taşıyor (sayfa {pages}); "
                               + ("temsilci" if index == 0 else f"temsilci {hashes[0][:12]}"), gid, detail))
        if only_mudahale:
            continue
        kinds = {m.get("kanit_turu") for m in members}
        if {"metin", "tablo"} <= kinds:
            gid2 = group_id("tekrar_metin_tablo", hashes)
            for m in members:
                flags.append(_flag(m, "tekrar_metin_tablo", "bilgi", "aynı bilgi hem metinde hem tabloda", gid2, detail))
        if len(pages) > 1:
            gid3 = group_id("tekrar_farkli_sayfa", hashes)
            for m in members:
                flags.append(_flag(m, "tekrar_farkli_sayfa", "bilgi", f"aynı bilgi farklı sayfalarda: {pages}", gid3, detail))

    # Benzer (yalnızca bilgi): aynı makale+müdahale+değer+birim; hasta/sonuç benzer.
    buckets = defaultdict(list)
    for row in rows:
        v = views[row["satir_hash"]]
        if v["mudahale"] and (v["deger"] or v["doz"]):
            buckets[(v["makale"], v["mudahale"], v["deger"], v["unit"], v["doz"])].append(row)
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda r: r["satir_hash"])
        texts = [views[m["satir_hash"]]["hasta"] + " " + views[m["satir_hash"]]["sonuc"] for m in members]
        vectors = embedder(texts) if embedder else None
        parent = list(range(len(members)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        scores = {}
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i]["satir_hash"], members[j]["satir_hash"]
                if exact_key_of.get(a) is not None and exact_key_of.get(a) == exact_key_of.get(b):
                    continue  # zaten birebir tekrar
                score = jaccard(texts[i], texts[j])
                method = "jaccard"
                if vectors is not None:
                    cosine = float(sum(x * y for x, y in zip(vectors[i], vectors[j])))
                    if cosine >= embedding_threshold and score < JACCARD_THRESHOLD:
                        score, method = cosine, "embedding"
                if score >= (embedding_threshold if method == "embedding" else JACCARD_THRESHOLD):
                    parent[find(i)] = find(j)
                    scores[(a, b)] = (round(score, 3), method)
        components = defaultdict(list)
        for i in range(len(members)):
            components[find(i)].append(members[i])
        for comp in components.values():
            if len(comp) < 2:
                continue
            hashes = sorted(m["satir_hash"] for m in comp)
            gid = group_id("tekrar_benzer", hashes)
            pair_scores = {f"{a[:12]}~{b[:12]}": s for (a, b), s in sorted(scores.items()) if a in hashes and b in hashes}
            for m in comp:
                flags.append(_flag(m, "tekrar_benzer", "bilgi",
                                   f"{len(comp)} satır benzer hasta grubu/sonuç (eşik Jaccard {JACCARD_THRESHOLD}"
                                   + (f", embedding {embedding_threshold}" if embedder else "") + ")",
                                   gid, {"uyeler": hashes, "skorlar": pair_scores}))
    return flags


# ---------------------------------------------------------------------------
# b) Sayısal
# ---------------------------------------------------------------------------

def _labelled(text, labels):
    pattern = r"\b(?:" + "|".join(labels) + r")\b\s*[=:]?\s*(-?\d+(?:[.,]\d+)?)"
    return [float(v.replace(",", ".")) for v in re.findall(pattern, _decimal(str(text or "")))]


def check_numeric(row):
    flags = []
    deger, unit, doz, p = row.get("deger"), row.get("unit"), row.get("doz"), row.get("p")
    etki = (row.get("etki_turu") or "").strip()
    numbers = parse_numbers(deger)
    values = [n["value"] for n in numbers if n["value"] is not None]
    context = " ".join(str(x or "") for x in (deger, unit, outcome_of(row)))

    percent_values = [n["value"] for n in numbers if n["value"] is not None
                      and (n["percent"] or norm_unit(unit) == "%")]
    over = [v for v in percent_values if v > 100]
    if over:
        level = "hata" if PROPORTION_WORDS.search(context) else "uyari"
        flags.append(_flag(row, "yuzde_100_ustu", level, f"yüzde değeri 100'den büyük: {over}",
                           detail={"degerler": over}))

    p_numbers = parse_numbers(p)
    out_of_range = [n["value"] for n in p_numbers if n["value"] is not None and not 0 <= n["value"] <= 1]
    if out_of_range:
        flags.append(_flag(row, "p_aralik_disi", "hata", f"p değeri 0–1 dışında: {out_of_range}"))
    if any(n["leading_zero"] for n in p_numbers):
        flags.append(_flag(row, "p_bicim_supheli", "uyari", f"p biçimi şüpheli (ondalık nokta eksik?): '{p}'"))

    ci = [n["value"] for n in parse_numbers(row.get("ci95")) if n["value"] is not None]
    if len(ci) == 2:
        low, high = ci
        if low > high:
            flags.append(_flag(row, "ci_ters", "hata", f"%95 GA alt sınır üst sınırdan büyük: {low} > {high}"))
        elif len(values) == 1 and not low <= values[0] <= high:
            flags.append(_flag(row, "deger_ci_disinda", "uyari", f"değer {values[0]} GA [{low}, {high}] dışında"))

    if etki in RATIO_TYPES:
        ratios = _labelled(deger, [etki]) or (values if len(values) == 1 else [])
        if any(v <= 0 for v in ratios):
            flags.append(_flag(row, "oran_sifir_veya_negatif", "hata", f"{etki} sıfır veya negatif: {ratios}"))
    if etki == AUC_TYPE:
        aucs = _labelled(deger, ["AUC", "C-statistic", "C-istatistigi"]) or (values if len(values) == 1 else [])
        if any(not 0 <= v <= 1 for v in aucs):
            flags.append(_flag(row, "auc_aralik_disi", "hata", f"AUC 0–1 dışında: {aucs}"))
    negatives = [v for v in values if v < 0]
    if negatives and etki != "ortalama fark" and etki not in RATIO_TYPES:
        flags.append(_flag(row, "negatif_sayi", "uyari", f"negatif değer: {negatives}"))
    if row.get("hasta_sayisi") not in (None, "") and str(row["hasta_sayisi"]).strip() == "0":
        flags.append(_flag(row, "negatif_sayi", "uyari", "hasta_sayisi = 0"))

    if values and not norm_unit(unit) and not UNIT_TOKEN.search(norm_value(deger)) and etki not in UNITLESS_TYPES:
        flags.append(_flag(row, "deger_birimsiz", "bilgi" if not etki else "uyari",
                           "sayısal değer var, birim yok" + (" (etki_turu boş)" if not etki else "")))
    if parse_numbers(doz) and not DOSE_UNIT.search(norm_value(doz)):
        flags.append(_flag(row, "doz_birimsiz", "uyari", f"dozda sayı var, birim yok: '{doz}'"))
    if deger not in (None, "") and not numbers:
        flags.append(_flag(row, "sayi_ayristirilamadi", "bilgi", "değer alanında sayı yok"))
    if any(n["ambiguous"] for n in numbers + p_numbers):
        flags.append(_flag(row, "ondalik_belirsiz", "bilgi", "binlik mi ondalık mı belirsiz (ör. 1,234)"))
    return flags


# ---------------------------------------------------------------------------
# c) Kaynak
# ---------------------------------------------------------------------------

@dataclass
class SourceContext:
    belgeler: dict = field(default_factory=dict)     # document_id -> güncel processing_version
    chunks: dict = field(default_factory=dict)       # chunk_id -> (document_id, page, text, version)
    makale_by_doc: dict = field(default_factory=dict)
    validation: object = None                        # clinical_validation.ValidationContext


def _text(value):
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def load_source_context(conn):
    from src.clinical_validation import load_validation_context

    ctx = SourceContext()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT document_id, processing_version FROM pdf_belgeleri")
        ctx.belgeler = {_text(d): _text(v) for d, v in cursor.fetchall()}
        cursor.execute("SELECT chunk_id, document_id, page_number, text, processing_version FROM pdf_chunks")
        ctx.chunks = {_text(c): (_text(d), int(p), _text(t), _text(v)) for c, d, p, t, v in cursor.fetchall()}
        cursor.execute("SELECT document_id, makale_id FROM makaleler WHERE document_id IS NOT NULL")
        ctx.makale_by_doc = {_text(d): _text(m) for d, m in cursor.fetchall()}
    finally:
        cursor.close()
    ctx.validation = load_validation_context(conn)
    return ctx


def check_source(row, ctx):
    flags = []
    empty = [c for c in ("document_id", "kanit_sayfa", "kanit_cumle") if row.get(c) in (None, "")]
    if empty:
        flags.append(_flag(row, "kaynak_alani_bos", "hata", f"boş kaynak alanı: {empty}"))
    if row.get("chunk_id") in (None, ""):
        flags.append(_flag(row, "chunk_id_bos", "hata" if row.get("cikarma_yontemi") == "rag" else "uyari",
                           "chunk_id boş"))
    doc = row.get("document_id")
    if doc and doc not in ctx.belgeler:
        flags.append(_flag(row, "belge_yok", "hata", "document_id pdf_belgeleri'de yok"))
    if doc and ctx.makale_by_doc.get(doc) and row.get("makale_id") not in (None, "", ctx.makale_by_doc[doc]):
        flags.append(_flag(row, "makale_belge_uyusmuyor", "hata",
                           f"makale_id '{row.get('makale_id')}' ≠ belgenin makalesi '{ctx.makale_by_doc[doc]}'"))
    cid = row.get("chunk_id")
    if cid:
        chunk = ctx.chunks.get(cid)
        if chunk is None:
            flags.append(_flag(row, "chunk_yok", "hata", "chunk_id pdf_chunks'ta yok"))
        else:
            chunk_doc, page, text, version = chunk
            if doc and chunk_doc != doc:
                flags.append(_flag(row, "chunk_belge_uyusmuyor", "hata", "chunk başka bir belgeye ait"))
            if ctx.belgeler.get(chunk_doc) != version:
                flags.append(_flag(row, "chunk_eski_surum", "uyari",
                                   f"chunk sürümü {version} güncel değil ({ctx.belgeler.get(chunk_doc)})"))
            if row.get("kanit_sayfa") not in (None, "") and int(row["kanit_sayfa"]) != page:
                flags.append(_flag(row, "kanit_sayfa_uyusmuyor", "hata",
                                   f"kanit_sayfa {row['kanit_sayfa']} ≠ chunk sayfası {page}"))
            if row.get("kanit_cumle") and norm_ws(row["kanit_cumle"]) not in norm_ws(text):
                flags.append(_flag(row, "kanit_chunkta_yok", "hata", "kanıt cümlesi chunk metninde birebir yok"))
    if ctx.validation is not None and not empty:
        from src.clinical_validation import validate_row
        try:
            ok, error_type, detail = validate_row(row, ctx.validation)
        except Exception as exc:  # noqa: BLE001 - bozuk satır kontrolü düşürmemeli
            ok, error_type, detail = False, type(exc).__name__, "validate_row çalıştırılamadı"
        if not ok:
            flags.append(_flag(row, "dogrulama_artik_gecmiyor", "uyari",
                               f"validate_row: {error_type}: {detail}", detail={"hata_turu": error_type}))
    return flags


# ---------------------------------------------------------------------------
# d) Tutarsızlık
# ---------------------------------------------------------------------------

def check_row_consistency(row):
    flags = []
    if row.get("cikarma_yontemi") != "rag":
        return flags
    note = _note(row)
    fields = note.get("extracted_fields") if note else None
    if not isinstance(fields, dict):
        flags.append(_flag(row, "taslak_notu_okunamadi", "uyari", "taslak_notu JSON değil veya extracted_fields yok"))
        return flags
    filled = [f for f in ("drug", "procedure", "diagnostic_method") if fields.get(f)]
    problems = []
    turu = row.get("mudahale_turu")
    if "diagnostic_method" in filled and turu != "tanı testi":
        problems.append(f"diagnostic_method dolu ama mudahale_turu '{turu}'")
    if {"drug", "procedure"} & set(filled) and turu not in ("tedavi", "arka plan tedavisi"):
        problems.append(f"drug/procedure dolu ama mudahale_turu '{turu}'")
    if len(filled) == 1 and mudahale_of(row) != fields[filled[0]]:
        problems.append(f"mudahale '{mudahale_of(row)}' ≠ {filled[0]} '{fields[filled[0]]}'")
    if len(filled) != 1:
        problems.append(f"müdahale alanı sayısı {len(filled)} (tam 1 olmalı)")
    source = ((note.get("otomatik_alanlar") or {}).get("mudahale_turu") or "")
    match = re.search(r"\((\w+) alanından\)", source)
    if match and filled and match.group(1) not in filled:
        problems.append(f"otomatik_alanlar '{match.group(1)}' diyor, dolu alan {filled}")
    if problems:
        flags.append(_flag(row, "mudahale_turu_kaynak_uyusmuyor", "hata", "; ".join(problems)))
    language = note.get("source_language")
    tr_filled = any(row.get(c) for c in ("tani_tr", "mudahale_tr", "sonuc_olcutu_tr"))
    en_filled = any(row.get(c) for c in ("tani_en", "mudahale_en"))
    if (language == "tr" and en_filled and not tr_filled) or (language == "en" and tr_filled and not en_filled):
        flags.append(_flag(row, "dil_uyusmuyor", "bilgi", f"source_language '{language}' ile dolu alanların dili uyuşmuyor"))
    return flags


def check_group_consistency(rows):
    flags = []
    views = {r["satir_hash"]: view(r) for r in rows}

    def emit(code, level, buckets, value_of, label):
        for key, members in sorted(buckets.items()):
            distinct = sorted({value_of(m) for m in members if value_of(m) not in ("", None)})
            if len(distinct) < 2:
                continue
            involved = sorted((m for m in members if value_of(m) not in ("", None)), key=lambda r: r["satir_hash"])
            hashes = [m["satir_hash"] for m in involved]
            gid = group_id(code, hashes)
            for m in involved:
                flags.append(_flag(m, code, level, f"aynı {label} için farklı değerler: {distinct}", gid,
                                   {"uyeler": hashes, "degerler": distinct}))

    doses, counts, values = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        v = views[row["satir_hash"]]
        if v["mudahale"]:
            doses[(v["makale"], v["mudahale"], v["hasta"])].append(row)
        if v["hasta"]:
            counts[(v["makale"], v["hasta"])].append(row)
        if v["mudahale"] and v["sonuc"]:
            values[(v["makale"], v["mudahale"], v["hasta"], v["sonuc"])].append(row)
    emit("farkli_doz", "uyari", doses, lambda r: views[r["satir_hash"]]["doz"], "makale + müdahale + hasta grubu")
    emit("farkli_hasta_sayisi", "uyari", counts, lambda r: str(r.get("hasta_sayisi") or ""), "makale + hasta grubu")
    emit("farkli_deger", "bilgi", values, lambda r: views[r["satir_hash"]]["deger"],
         "makale + müdahale + hasta grubu + sonuç")
    return flags


# ---------------------------------------------------------------------------
# İstatistiksel tutarlılık (kaynak değerler yalnızca okunur; yorum işarette durur)
# ---------------------------------------------------------------------------

P_BOUND = re.compile(r"^\s*(?:p\s*)?(<=|≤|<|=|>|≥|>=)?\s*(\d*\.\d+|\d+)\s*$", re.IGNORECASE)
CATEGORICAL_VALUE = re.compile(r"^\s*(\d+)\s*\(\s*(\d+(?:\.(\d+))?)\s*%?\s*\)\s*$")
MEAN_SD_VALUE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)\s*$")
COX_TERMS = re.compile(r"\bcox\b|hazard ratio|\(HR\)", re.IGNORECASE)
LOGISTIC_TERMS = re.compile(r"logistic regression|odds ratio", re.IGNORECASE)
HR_RESULT = re.compile(r"\bHR\s*[=:]?\s*\d")
OR_RESULT = re.compile(r"\bOR\s*[=:]?\s*\d|odds ratio", re.IGNORECASE)


def parse_p(text):
    """'<0.001', 'p=0.03', '0.002' -> (işleç, değer); belirsiz biçim ('<001', boş) -> None."""
    if text in (None, ""):
        return None
    match = P_BOUND.match(unicodedata.normalize("NFKC", str(text)).replace("*", ""))
    if not match or re.fullmatch(r"0\d+", match.group(2)):
        return None
    op = {"≤": "<=", "≥": ">=", None: "="}.get(match.group(1), match.group(1))
    value = float(match.group(2))
    return (op, value) if 0 <= value <= 1 else None


def _p_significant(bound, alpha=0.05):
    """True/False; '<0.10' gibi karar verilemeyen sınırlar için None."""
    op, value = bound
    if op in ("<", "<="):
        return True if value <= alpha else None
    if op in (">", ">="):
        return False if value >= alpha else None
    return value < alpha


def _normal_p(z):
    return math.erfc(abs(z) / math.sqrt(2))


def ci_p_assessment(row):
    """Ortak GA–p değerlendirmesi (ci_p_celiskisi ve pilot kaynak_ci_p_celiski aynı kuralı kullanır).

    OR/RR/HR/IRR ve AUC: %95 GA boş değeri (1; AUC için 0.5) içeriyorsa p ≥ 0.05, içermiyorsa p < 0.05
    olmalı. Çelişki yoksa veya p belirsizse ('<001', '<0.10') None; varsa ayrıntı sözlüğü."""
    etki = (row.get("etki_turu") or "").strip()
    if etki not in RATIO_TYPES and etki != AUC_TYPE:
        return None
    ci = [n["value"] for n in parse_numbers(row.get("ci95")) if n["value"] is not None]
    bound = parse_p(row.get("p"))
    if len(ci) != 2 or bound is None or ci[0] > ci[1]:
        return None
    significant = _p_significant(bound)
    low, high = ci
    if significant is None or (etki in RATIO_TYPES and low <= 0):
        return None
    null = 0.5 if etki == AUC_TYPE else 1.0
    includes = low <= null <= high
    if includes != significant:
        return None
    if etki == AUC_TYPE:
        margin = min(abs(low - null), abs(high - null))
    else:
        margin = min(abs(math.log(low)), abs(math.log(high)))
    return {"etki": etki, "low": low, "high": high, "null": null, "includes": includes,
            "significant": significant, "margin": round(margin, 4)}


def _ci_p_detail(row, conflict):
    return {"ci95": row.get("ci95"), "p": row.get("p"), "bos_deger": conflict["null"],
            "sinir_marji": conflict["margin"], "kaynak_tutarsizligi": True,
            "varsayim": "GA ve p aynı modelden; marj küçükse farklı GA/test yöntemi sapması mümkün"}


def check_ci_p(row):
    """Kaynak tutarsızlığıdır, çıkarım hatası değil: belirgin marjda 'uyari', sınırda 'bilgi'."""
    conflict = ci_p_assessment(row)
    if conflict is None:
        return []
    level = "uyari" if conflict["margin"] >= CI_P_SINIR_MARJI else "bilgi"
    side = "içeriyor" if conflict["includes"] else "içermiyor"
    return [_flag(row, "ci_p_celiskisi", level,
                  f"KAYNAK TUTARSIZLIĞI: {conflict['etki']} %95 GA [{conflict['low']}, {conflict['high']}] "
                  f"boş değeri ({conflict['null']:g}) {side} ama p '{row.get('p')}' "
                  f"{'anlamlı' if conflict['significant'] else 'anlamsız'}",
                  detail=_ci_p_detail(row, conflict))]


def _group_pairs(rows):
    """Aynı belge + tablo + satırdaki iki grup kaydı (karşılıklı comparator). Üç+ grup atlanır."""
    buckets = defaultdict(list)
    for row in rows:
        note = _note(row) or {}
        if note.get("tablo_no") is None or note.get("satir_no") is None or not row.get("hasta_grubu"):
            continue
        doc = row.get("document_id") or row.get("makale_id") or ""
        buckets[(doc, str(note["tablo_no"]), str(note["satir_no"]))].append((row, note))
    for members in buckets.values():
        if len(members) != 2:
            continue
        (a, na), (b, nb) = sorted(members, key=lambda m: m[0]["satir_hash"])
        if a.get("comparator") != b.get("hasta_grubu") or b.get("comparator") != a.get("hasta_grubu"):
            continue
        if (a.get("p") or "") != (b.get("p") or ""):
            continue
        yield a, na, b, nb


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chi2_2x2_p(x1, n1, x2, n2, yates):
    total, successes = n1 + n2, x1 + x2
    failures = total - successes
    if not (0 < successes < total):
        return 1.0
    diff = abs(x1 * (n2 - x2) - x2 * (n1 - x1))
    if yates:
        diff = max(0.0, diff - total / 2)
    chi2 = total * diff ** 2 / (n1 * n2 * successes * failures)
    return math.erfc(math.sqrt(chi2 / 2))


def check_statistical_groups(rows, pilot=False):
    """Grup tablosu satır çiftleri: kategorik n (%) ve ortalama±SS için bildirilen p'nin yeniden hesabı.

    pilot=True (--pilot akışı): pilot satırların yüzde aritmetiği pilot_yuzde_uyumsuz ile raporlanır;
    burada ikinci kez işaretlenmez (yalnız ki-kare atlanır)."""
    flags = []
    for a, na, b, nb in _group_pairs(rows):
        bound = parse_p(a.get("p"))
        n1, n2 = _int_or_none(na.get("grup_n")), _int_or_none(nb.get("grup_n"))
        if bound is None or not n1 or not n2:
            continue
        hashes = [a["satir_hash"], b["satir_hash"]]
        cat_a, cat_b = CATEGORICAL_VALUE.match(a.get("deger") or ""), CATEGORICAL_VALUE.match(b.get("deger") or "")
        mean_a, mean_b = MEAN_SD_VALUE.match(a.get("deger") or ""), MEAN_SD_VALUE.match(b.get("deger") or "")
        if cat_a and cat_b:
            report_percent = not (pilot and (is_pilot(a) or is_pilot(b)))
            flags += _check_categorical(a, b, cat_a, cat_b, n1, n2, bound, hashes, report_percent)
        elif mean_a and mean_b and min(n1, n2) >= SUREKLI_MIN_N:
            flags += _check_continuous(a, b, mean_a, mean_b, n1, n2, bound, hashes)
    return flags


def _check_categorical(a, b, cat_a, cat_b, n1, n2, bound, hashes, report_percent=True):
    code = "kategorik_p_yeniden_uretilemedi"
    gid = group_id(code, hashes)
    x1, x2 = int(cat_a.group(1)), int(cat_b.group(1))
    detail = {"uyeler": hashes, "deger": [a.get("deger"), b.get("deger")], "n": [n1, n2], "p": a.get("p"),
              "varsayim": "payda = grup başlığındaki n; 2x2 ki-kare (süreklilik düzeltmeli/düzeltmesiz); "
                          "makalenin belirttiği test kontrol edilmedi"}
    # Yüzde aritmetiği: raporlanan ondalık hassasiyetinde n/N ile tutmalı (yuvarlama ya da kesme:
    # son basamağın bir birimi kadar tolerans; ör. 435/853 = 50.996 -> "50.99").
    mismatched = []
    for (x, n, match) in ((x1, n1, cat_a), (x2, n2, cat_b)):
        decimals = len(match.group(3) or "")
        if x > n or abs(100 * x / n - float(match.group(2))) > 10 ** -decimals + 1e-9:
            mismatched.append(f"{x}/{n}={100 * x / n:.2f}% ≠ {match.group(2)}%")
    if mismatched:
        # Paydalar doğrulanamadı: ki-kare hesaplanmaz. --pilot akışında pilot_yuzde_uyumsuz zaten raporlar.
        if not report_percent:
            return []
        return [_flag(row, code, "uyari", "yüzde, grup n'si ile tutmuyor (eksik veri olabilir): " + "; ".join(mismatched),
                      gid, detail) for row in (a, b)]
    p_plain, p_yates = _chi2_2x2_p(x1, n1, x2, n2, False), _chi2_2x2_p(x1, n1, x2, n2, True)
    low, high = min(p_plain, p_yates), max(p_plain, p_yates)
    detail.update(hesaplanan_p=[round(low, 4), round(high, 4)])
    significant = _p_significant(bound)
    op, value = bound
    if significant is not None and significant != (high < 0.05) and significant != (low < 0.05):
        level, text = "uyari", "anlamlılık sonucu tersine dönüyor"
    elif op == "=" and not (low / 2 <= value <= min(1.0, high * 2)):
        level, text = "bilgi", "sayısal olarak yeniden üretilemedi (sonuç aynı)"
    elif op in ("<", "<=") and low > 2 * value:
        level, text = "bilgi", "sayısal olarak yeniden üretilemedi (sonuç aynı)"
    else:
        return []
    return [_flag(row, code, level, f"bildirilen p '{a.get('p')}', hesaplanan p≈{low:.3g}–{high:.3g}: {text}",
                  gid, detail) for row in (a, b)]


def _check_continuous(a, b, mean_a, mean_b, n1, n2, bound, hashes):
    code = "surekli_p_supheli"
    m1, s1 = float(mean_a.group(1)), float(mean_a.group(2))
    m2, s2 = float(mean_b.group(1)), float(mean_b.group(2))
    se = math.sqrt(s1 ** 2 / n1 + s2 ** 2 / n2)
    if se == 0:
        return []
    t = (m1 - m2) / se
    op, value = bound
    significant = _p_significant(bound)
    if significant is False and abs(t) >= SUREKLI_T_UST:
        text = f"bildirilen p '{a.get('p')}' anlamsız, özetlerden |t|={abs(t):.2f} (p≈{_normal_p(t):.1e})"
    elif significant and value <= 0.01 and abs(t) <= SUREKLI_T_ALT:
        text = f"bildirilen p '{a.get('p')}' çok anlamlı, özetlerden |t|={abs(t):.2f} (p≈{_normal_p(t):.2f})"
    else:
        return []
    gid = group_id(code, hashes)
    detail = {"uyeler": hashes, "deger": [a.get("deger"), b.get("deger")], "n": [n1, n2], "p": a.get("p"),
              "t": round(t, 3), "varsayim": "± = standart sapma; n = grup başlığı (eksik veri bilinmiyor); Welch t"}
    return [_flag(row, code, "uyari", text + "; varsayımlar doğrulanmadı", gid, detail) for row in (a, b)]


def _row_text(row):
    return " ".join(str(row.get(k) or "") for k in ("kanit_cumle", "klinik_bilgi_en", "sonuc_olcutu_tr", "taslak_notu"))


def check_model_terminology(rows):
    """Aynı belgede yöntem Cox/HR ile lojistik/OR terimlerini birlikte kullanıyor ama sonuçlar yalnız birinin
    etki ölçüsüyle raporlanmışsa (ya da hiçbiri) işaretler. İki ölçü de raporlanmışsa meşru kullanım sayılır."""
    flags = []
    by_doc = defaultdict(list)
    for row in rows:
        by_doc[row.get("document_id") or row.get("makale_id") or ""].append(row)
    code = "model_etki_olcusu_celiskisi"
    for doc, members in sorted(by_doc.items()):
        cox, logit, hr_results, or_results = [], [], [], []
        for row in members:
            text, etki = _row_text(row), (row.get("etki_turu") or "").strip()
            if COX_TERMS.search(text) or etki == "HR":
                cox.append(row)
            if LOGISTIC_TERMS.search(text) or etki == "OR":
                logit.append(row)
            if etki == "HR" or HR_RESULT.search(text):
                hr_results.append(row)
            if etki == "OR" or OR_RESULT.search(text):
                or_results.append(row)
        if not cox or not logit or (hr_results and or_results):
            continue
        involved = sorted({r["satir_hash"]: r for r in cox + logit}.values(), key=lambda r: r["satir_hash"])
        hashes = [r["satir_hash"] for r in involved]
        gid = group_id(code, hashes)
        reported = "OR" if or_results else ("HR" if hr_results else "yok")
        detail = {"uyeler": hashes, "cox_hr": [r["satir_hash"] for r in cox],
                  "lojistik_or": [r["satir_hash"] for r in logit], "raporlanan_etki_olcusu": reported,
                  "varsayim": "hangi modelin kullanıldığı bilinmiyor; kaynakta iki terim birlikte geçiyor"}
        for row in involved:
            flags.append(_flag(row, code, "uyari",
                               f"belgede Cox/HR ({len(cox)} kayıt) ve lojistik/OR ({len(logit)} kayıt) birlikte; "
                               f"sonuçlarda raporlanan etki ölçüsü: {reported}", gid, detail))
    return flags
# e) Pilot satırları (bilgi_turu dolu; src.pilot_clinical.import_records aktarımı)
# ---------------------------------------------------------------------------

PILOT_LOW_CONTEXTS = {"literature_background", "recommendation"}
PILOT_MED_TURU = "arka plan tedavisi"
_SUP = re.compile(r"<sup>.*?</sup>", re.IGNORECASE | re.DOTALL)
_P_EXPR = re.compile(r"\bp\s*(?:-?\s*value\s*)?[<=>≤≥]\s*[\d.,]+", re.IGNORECASE)
_P_BROKEN = re.compile(r"\bp\s*[<=>≤≥]\s*0\d+", re.IGNORECASE)
_FIGURE_TABLE = re.compile(r"\*\*\s*(?:FIGURE|TABLE)\s*\d+", re.IGNORECASE)
_N_PCT = re.compile(r"^\s*(\d+)\s*\(\s*(\d+(?:\.\d+)?)\s*%?\s*\)\s*$")
_STOPWORDS = {"with", "that", "this", "were", "which", "from", "have", "been", "than", "also", "there", "their",
              "these", "those", "into", "between", "after", "before", "found", "study", "patients", "group",
              "value", "values", "was", "and", "the"}


def is_pilot(row):
    return bool(row.get("bilgi_turu"))


def pilot_table_key(row):
    """(document_id, tablo_no, satir_no) — pilot_tablo aktarımından gelen satırlar için; yoksa None."""
    note = _note(row) or {}
    if str(note.get("kaynak") or "").startswith("pilot-tablo") and note.get("tablo_no") is not None \
            and note.get("satir_no") is not None:
        return row.get("document_id"), int(note["tablo_no"]), int(note["satir_no"])
    return None


def _num_text(text):
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_DASHES).replace("·", ".")
    return _SUP.sub(" ", text)


def _canon(raw):
    return str(float(raw))


def numbers_in(text):
    """Metindeki bütün sayılar (kanonik dize). <sup> kaynak numaraları hariç."""
    return {_canon(m) for m in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", _num_text(text))}


def significant_numbers(text):
    """Metin ↔ metin tekrarı için: ondalıklı sayılar ve %'li tam sayılar; p ifadeleri ve kaynak no'ları hariç."""
    text = _P_EXPR.sub(" ", _num_text(text))
    out = set()
    for m in re.finditer(r"(?<![\d.])(\d+)(\.\d+)?(\s*%)?", text):
        if m.group(2) or m.group(3):
            out.add(_canon(m.group(1) + (m.group(2) or "")))
    return out


def keywords(text):
    return {w for w in re.findall(r"[^\W\d_][\w-]{3,}", norm_text(_SUP.sub(" ", str(text or ""))))
            if w not in _STOPWORDS}


def _contains_term(text, term):
    term = norm_text(term)
    return bool(term) and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", norm_text(text)) is not None


def check_pilot_row(row, source):
    """Tek pilot satırının kanıt / biçim / bağlam kontrolleri. source: {'pages': {...}} veya {'hata': ...}."""
    flags = []
    quote = row.get("kanit_cumle") or ""
    pages = (source or {}).get("pages")
    if pages is None:
        flags.append(_flag(row, "pilot_pdf_okunamadi", "uyari",
                           f"PDF metni okunamadı: {(source or {}).get('hata', 'belge yok')}; kanıt kontrolü yapılmadı"))
    else:
        page = row.get("kanit_sayfa")
        text = pages.get(int(page)) if page not in (None, "") else None
        if not quote or text is None or quote not in text:
            flags.append(_flag(row, "pilot_kanit_sayfada_yok", "hata",
                               f"kanit_cumle PDF'in {page}. sayfasında (pdf_pages) birebir yok"))
    fact = row.get("klinik_bilgi_en") or row.get("klinik_bilgi")
    if not fact or fact not in quote:
        flags.append(_flag(row, "pilot_bilgi_kanitta_yok", "hata", "klinik_bilgi_en kanıt cümlesinin içinde değil"))

    if row.get("kanit_turu") != "tablo" and quote.strip():
        start = re.sub(r"^[\s*_#>]+", "", quote)
        end = re.sub(r"(?:<sup>[^<]*</sup>|[\s*_])+$", "", quote)
        strong, weak = [], []  # strong -> uyari; yalnız weak -> bilgi
        first = start[:1]
        if first.isalpha() and first.islower():
            strong.append("küçük harfle başlıyor")
        elif first.isdigit():
            weak.append(f"rakamla başlıyor ('{first}')")  # "1,225 patients…" gibi normal cümleler olabilir
        elif first and not first.isalpha():
            strong.append(f"parantez/noktalama ile başlıyor ('{first}')")
        if re.search(r"[^\W\d_]-$", end):
            strong.append("tire/kesik kelimeyle bitiyor")
        if not strong and end and end[-1] not in ".!?:;)\"'%]":
            weak.append("bitiş noktalaması yok")
        reasons, level = strong + weak, "uyari" if strong else "bilgi"
        if reasons:
            flags.append(_flag(row, "pilot_sayfa_gecisi_supheli", level,
                               "sayfa/sütun geçişi şüphesi: " + ", ".join(reasons)))
    if _FIGURE_TABLE.search(quote):
        flags.append(_flag(row, "pilot_kanitta_sekil_tablo", "uyari",
                           f"kanıtta şekil/tablo yazısı var: '{_FIGURE_TABLE.search(quote).group(0)}'"))
    if _P_BROKEN.search(quote):
        flags.append(_flag(row, "kaynak_p_bicim_bozuk", "bilgi",
                           f"KAYNAK TUTARSIZLIĞI: kanıtta bozuk p biçimi '{_P_BROKEN.search(quote).group(0)}' "
                           "(ondalık nokta eksik; kanıt kaynaktan birebir)"))

    note = _note(row) or {}
    match = _N_PCT.match(str(row.get("deger") or ""))
    group_n = note.get("grup_n")
    if match and isinstance(group_n, int) and group_n > 0:
        count, reported = int(match.group(1)), match.group(2)
        decimals = len(reported.split(".")[1]) if "." in reported else 0
        computed = 100 * count / group_n
        unit = 10 ** -decimals
        truncated = int(computed * 10 ** decimals + 1e-9) / 10 ** decimals
        ok = (abs(round(computed, decimals) - float(reported)) < 1e-9 or abs(truncated - float(reported)) < 1e-9
              or abs(computed - float(reported)) <= unit + 1e-9)
        if not ok:
            flags.append(_flag(row, "pilot_yuzde_uyumsuz", "uyari",
                               f"{count}/{group_n} = %{computed:.{decimals + 1}f}, bildirilen %{reported} "
                               f"(tolerans: yuvarlama/kesme veya ±{unit:g})",
                               detail={"n": count, "grup_n": group_n, "hesaplanan": round(computed, 4),
                                       "bildirilen": float(reported)}))

    # GA–p: ci_p_celiskisi ile aynı kural (ci_p_assessment; AUC ve belirsiz p dahil). --pilot akışında
    # pilot satırlar yalnız bu kodu alır (run_checks onları ci_p_celiskisi'nden çıkarır).
    conflict = ci_p_assessment(row)
    if conflict is not None:
        flags.append(_flag(row, "kaynak_ci_p_celiski", "bilgi",
                           f"KAYNAK TUTARSIZLIĞI: {conflict['etki']} %95 GA [{conflict['low']}, {conflict['high']}] "
                           f"{conflict['null']:g}'i {'kapsıyor' if conflict['includes'] else 'kapsamıyor'} "
                           f"ama p={row.get('p')}", detail=_ci_p_detail(row, conflict)))

    if row.get("baglam") in PILOT_LOW_CONTEXTS and row.get("kanit_turu") == "tablo":
        flags.append(_flag(row, "pilot_baglam_tablo", "uyari",
                           f"baglam '{row.get('baglam')}' ama kanit_turu=tablo (tablo çalışmanın kendi verisi)"))
    medication = row.get("bilgi_turu") == "ilac" or row.get("mudahale_turu") == PILOT_MED_TURU
    if medication and str(row.get("doz") or "").strip() and match:
        flags.append(_flag(row, "pilot_ilac_doz_oran", "uyari",
                           f"ilaç satırında doz dolu ('{row.get('doz')}') ama değer n (%) kullanım oranı"))
    return flags


def _pdf_row(tables, key):
    _, tablo_no, satir_no = key
    for table in tables:
        if table["tablo_no"] == tablo_no:
            for row in table["satirlar"]:
                if row["satir_no"] == satir_no:
                    return table, row
    return None, None


def _same(a, b):
    return str(a or "").strip() == str(b or "").strip()


def check_table_groups(pilot_rows, sources):
    """Aynı tablo satırının beklenen grup/analiz satırları SQL'de tam mı ve PDF hücreleriyle tutarlı mı."""
    from src.pilot_tablo import _needle, row_analyses, row_groups

    flags, partial = [], []
    groups = defaultdict(list)
    for row in pilot_rows:
        key = pilot_table_key(row)
        if key:
            groups[key].append(row)
    for key, members in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1], kv[0][2])):
        members = sorted(members, key=lambda r: r["satir_hash"])
        hashes = [m["satir_hash"] for m in members]
        tables = (sources.get(key[0]) or {}).get("tables")
        if tables is None:
            continue  # PDF okunamadı: pilot_pdf_okunamadi zaten işaretli
        table, pdf_row = _pdf_row(tables, key)
        if pdf_row is None:
            gid = group_id("tablo_cift_tutarsiz", hashes)
            for m in members:
                flags.append(_flag(m, "tablo_cift_tutarsiz", "hata",
                                   f"Table {key[1]} satır {key[2]} PDF'te bulunamadı", gid, {"uyeler": hashes}))
            continue
        analyses = row_analyses(table, pdf_row)
        if analyses:
            expected = {a["analiz"]: a for a in analyses}
            unit_of = lambda m: (_note(m) or {}).get("analiz")  # noqa: E731
        else:
            expected = {g["sutun"]: g for g in row_groups(table, pdf_row)}
            unit_of = lambda m: (_note(m) or {}).get("sutun")  # noqa: E731
        present = {unit_of(m) for m in members}
        missing = sorted(set(expected) - present)
        info = {"tablo_no": key[1], "satir_no": key[2], "degisken": pdf_row["degisken"], "uyeler": hashes,
                "beklenen": sorted(expected), "mevcut": sorted(u for u in present if u), "eksik": missing}
        if missing:
            partial.append(dict(info, sayfa=table["sayfa"]))
            gid = group_id("tablo_cift_eksik", hashes)
            for m in members:
                flags.append(_flag(m, "tablo_cift_eksik", "uyari",
                                   f"Table {key[1]} '{pdf_row['degisken']}': eksik {'analiz' if analyses else 'grup'} "
                                   f"{missing} (mevcut {info['mevcut']})", gid, info))
        problems = defaultdict(list)
        for m in members:
            cell = expected.get(unit_of(m))
            if cell is None:
                problems[m["satir_hash"]].append(f"beklenmeyen grup/analiz '{unit_of(m)}'")
                continue
            if _needle(m.get("kanit_cumle")) not in pdf_row["ham"]:
                problems[m["satir_hash"]].append("kanıt bu tablo satırı değil")
            checks = [("deger", "deger"), ("p", "p")] + (
                [("ci95", "ci95")] if analyses else [("hasta_grubu", "grup"), ("comparator", "comparator")])
            for column, cell_key in checks:
                if not _same(m.get(column), cell.get(cell_key)):
                    problems[m["satir_hash"]].append(f"{column} '{m.get(column)}' ≠ PDF '{cell.get(cell_key)}'")
        if not analyses and len(members) > 1:
            if len({str(m.get("p") or "").strip() for m in members}) > 1:
                for m in members:
                    problems[m["satir_hash"]].append("çiftte p değerleri farklı")
            if len(members) == 2:
                a, b = members
                if not (_same(a.get("comparator"), b.get("hasta_grubu")) and _same(b.get("comparator"), a.get("hasta_grubu"))):
                    for m in members:
                        problems[m["satir_hash"]].append("comparator ↔ hasta_grubu karşılıklı değil")
        if problems:
            gid = group_id("tablo_cift_tutarsiz", hashes)
            for m in members:
                if m["satir_hash"] in problems:
                    flags.append(_flag(m, "tablo_cift_tutarsiz", "hata",
                                       f"Table {key[1]} '{pdf_row['degisken']}': " + "; ".join(problems[m["satir_hash"]]),
                                       gid, dict(info, sorunlar={h[:12]: p for h, p in problems.items()})))
    return flags, partial


def check_pilot_duplicates(pilot_rows):
    """Metin ↔ tablo ve metin ↔ metin anlamsal tekrar önerileri (yalnız bilgi; silme yok)."""
    flags, pairs = [], {"metin_tablo": [], "metin_metin": [], "belirsiz": []}
    texts = sorted((r for r in pilot_rows if r.get("kanit_turu") != "tablo"), key=lambda r: r["satir_hash"])
    tables = defaultdict(list)
    for row in pilot_rows:
        key = pilot_table_key(row)
        if key:
            tables[key].append(row)

    def emit(code, members, explanation, detail, bucket):
        hashes = sorted(m["satir_hash"] for m in members)
        gid = group_id(code, hashes)
        for m in members:
            flags.append(_flag(m, code, "bilgi", explanation, gid, dict(detail, uyeler=hashes)))
        pairs[bucket].append(dict(detail, grup_id=gid, uyeler=hashes))

    for key, members in sorted(tables.items(), key=lambda kv: (kv[0][0] or "", kv[0][1], kv[0][2])):
        members = sorted(members, key=lambda r: r["satir_hash"])
        values = [" ".join(str(m.get(c) or "") for c in ("deger", "ci95")).strip() for m in members]
        value_numbers = [numbers_in(v) for v in values]
        variable = (_note(members[0]) or {}).get("degisken") or members[0].get("sonuc_olcutu_tr") or ""
        for text_row in texts:
            if text_row.get("document_id") != key[0]:
                continue
            quote = text_row.get("kanit_cumle") or ""
            available = numbers_in(quote)
            matched = [v for v, nums in zip(values, value_numbers) if nums and nums <= available]
            named = _contains_term(quote, variable)
            detail = {"tablo_no": key[1], "satir_no": key[2], "degisken": variable, "eslesen_degerler": matched,
                      "metin_sayfa": text_row.get("kanit_sayfa")}
            if len(matched) == len(values) and len(values) >= 2 and named:
                emit("pilot_tekrar_metin_tablo", [text_row, *members],
                     f"metin (s.{text_row.get('kanit_sayfa')}) Table {key[1]} '{variable}' değerlerini aynen taşıyor: "
                     f"{matched}", detail, "metin_tablo")
            elif matched and (len(matched) == len(values) or named) and \
                    any(len(n) >= 2 for v, n in zip(values, value_numbers) if v in matched):
                emit("pilot_tekrar_belirsiz", [text_row, *members],
                     f"olası metin ↔ tablo tekrarı (Table {key[1]} '{variable}'; eşleşen {matched}, "
                     f"değişken adı {'var' if named else 'yok'})", dict(detail, tur="metin_tablo"), "belirsiz")

    numbers = {r["satir_hash"]: significant_numbers(r.get("kanit_cumle")) for r in texts}
    words = {r["satir_hash"]: keywords(r.get("kanit_cumle")) for r in texts}
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            if a.get("document_id") != b.get("document_id") or _same(a.get("kanit_cumle"), b.get("kanit_cumle")):
                continue
            shared = numbers[a["satir_hash"]] & numbers[b["satir_hash"]]
            union = words[a["satir_hash"]] | words[b["satir_hash"]]
            score = len(words[a["satir_hash"]] & words[b["satir_hash"]]) / len(union) if union else 0.0
            detail = {"ortak_sayilar": sorted(shared, key=float), "jaccard": round(score, 3),
                      "sayfalar": [a.get("kanit_sayfa"), b.get("kanit_sayfa")]}
            if len(shared) >= PILOT_MIN_SHARED_NUMBERS and score >= PILOT_JACCARD:
                emit("pilot_tekrar_metin_metin", [a, b],
                     f"metin kayıtları aynı içeriği taşıyor (ortak sayı {detail['ortak_sayilar']}, "
                     f"Jaccard {detail['jaccard']})", detail, "metin_metin")
            elif (len(shared) >= PILOT_MIN_SHARED_NUMBERS) or (len(shared) == 1 and score >= PILOT_JACCARD):
                emit("pilot_tekrar_belirsiz", [a, b],
                     f"olası metin ↔ metin tekrarı (ortak sayı {detail['ortak_sayilar']}, Jaccard {detail['jaccard']})",
                     dict(detail, tur="metin_metin"), "belirsiz")
    return flags, pairs


def pilot_completeness(pilot_rows, sources):
    """PDF'teki her tablo veri satırı en az bir pilot SQL satırıyla temsil ediliyor mu (belge başına)."""
    from src.pilot_tablo import _needle

    out = []
    by_doc = defaultdict(list)
    for row in pilot_rows:
        by_doc[row.get("document_id")].append(row)
    for doc in sorted(d for d in by_doc if d):
        source = sources.get(doc) or {}
        rows = by_doc[doc]
        item = {"document_id": doc, "dosya": source.get("dosya"), "pilot_satir": len(rows),
                "metin_satir": sum(r.get("kanit_turu") != "tablo" for r in rows),
                "tablo_satir": sum(r.get("kanit_turu") == "tablo" for r in rows)}
        if source.get("tables") is None:
            item["hata"] = source.get("hata", "PDF okunamadı")
            out.append(item)
            continue
        keys = {pilot_table_key(r) for r in rows}
        table_rows = [r for r in rows if r.get("kanit_turu") == "tablo"]
        per_table, missing = [], []
        for table in source["tables"]:
            represented = 0
            for pdf_row in table["satirlar"]:
                hit = (doc, table["tablo_no"], pdf_row["satir_no"]) in keys or any(
                    int(r.get("kanit_sayfa") or 0) == table["sayfa"] and _needle(r.get("kanit_cumle"))
                    and _needle(r.get("kanit_cumle")) in pdf_row["ham"] for r in table_rows)
                if hit:
                    represented += 1
                else:
                    missing.append({"tablo_no": table["tablo_no"], "sayfa": table["sayfa"],
                                    "satir_no": pdf_row["satir_no"], "alt_bolum": pdf_row["alt_bolum"],
                                    "degisken": pdf_row["degisken"], "ham": pdf_row["ham"]})
            per_table.append({"tablo_no": table["tablo_no"], "sayfa": table["sayfa"],
                              "tablo_basligi": table["tablo_basligi"], "pdf_satir": len(table["satirlar"]),
                              "temsil_edilen": represented, "eksik": len(table["satirlar"]) - represented})
        pages = Counter(int(r.get("kanit_sayfa") or 0) for r in rows if r.get("kanit_turu") != "tablo")
        item.update(tablolar=per_table, pdf_tablo_satiri=sum(t["pdf_satir"] for t in per_table),
                    temsil_edilen=sum(t["temsil_edilen"] for t in per_table), eksik_satirlar=missing,
                    sayfa_basina_metin_kaydi={str(p): pages.get(p, 0) for p in sorted(source["pages"])})
        out.append(item)
    return out


def load_pilot_sources(conn, document_ids, pdf_dir=DEFAULT_PDF_DIR, page_loader=None):
    """{document_id: {dosya, pages, tables} | {dosya, hata}}. PDF hash'i document_id ile tutmalı."""
    from src.pilot_tablo import parse_tables

    if page_loader is None:
        from src.pilot_clinical import pdf_pages as page_loader
    document_ids = sorted(d for d in document_ids if d)
    names = {}
    if document_ids:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT document_id, filename FROM pdf_belgeleri WHERE document_id IN ("
                           + ", ".join(["%s"] * len(document_ids)) + ")", document_ids)
            names = {_text(d): _text(f) for d, f in cursor.fetchall()}
        finally:
            cursor.close()
    sources = {}
    for doc in document_ids:
        name = names.get(doc)
        if not name:
            sources[doc] = {"dosya": None, "hata": "document_id pdf_belgeleri'de yok"}
            continue
        path = Path(pdf_dir) / name
        if not path.exists():
            sources[doc] = {"dosya": name, "hata": f"PDF dosyası yok: {path}"}
            continue
        try:
            loaded, pages = page_loader(path)
        except Exception as exc:  # noqa: BLE001 - okunamayan PDF kontrolü düşürmemeli
            sources[doc] = {"dosya": name, "hata": f"{type(exc).__name__}: {exc}"}
            continue
        if loaded.get("document_id") != doc:
            sources[doc] = {"dosya": name, "hata": "PDF hash'i document_id ile uyuşmuyor"}
            continue
        sources[doc] = {"dosya": name, "pages": pages, "tables": parse_tables(pages)}
    return sources


def run_pilot_checks(rows, sources):
    """(işaretler, rapor). Yalnız bilgi_turu dolu satırlar; mevcut kontrollerden bağımsız."""
    pilot_rows = sorted((r for r in rows if is_pilot(r)), key=lambda r: r["satir_hash"])
    flags = []
    for row in pilot_rows:
        flags += check_pilot_row(row, sources.get(row.get("document_id")))
    group_flags, partial = check_table_groups(pilot_rows, sources)
    duplicate_flags, pairs = check_pilot_duplicates(pilot_rows)
    flags += group_flags + duplicate_flags
    report = {"eksiksizlik": pilot_completeness(pilot_rows, sources), "eksik_grup_analiz": partial,
              "tekrar": pairs, "pilot_satir": len(pilot_rows)}
    return merge_flags(flags), report


def pilot_summary(flags, rows, report, examples=3):
    pilot_hashes = {r["satir_hash"] for r in rows if is_pilot(r)}
    by_hash = {r["satir_hash"]: r for r in rows}
    pilot_flags = [f for f in flags if f["satir_hash"] in pilot_hashes]
    codes = Counter(f["kontrol_kodu"] for f in pilot_flags)
    samples = defaultdict(list)
    for f in pilot_flags:
        if len(samples[f["kontrol_kodu"]]) < examples:
            row = by_hash[f["satir_hash"]]
            samples[f["kontrol_kodu"]].append({
                "satir_hash": f["satir_hash"][:12], "klinik_bulgu_id": f["klinik_bulgu_id"], "seviye": f["seviye"],
                "kanit_sayfa": row.get("kanit_sayfa"), "kanit_turu": row.get("kanit_turu"),
                "bilgi_turu": row.get("bilgi_turu"), "klinik_bilgi_en": (row.get("klinik_bilgi_en") or "")[:120],
                "aciklama": f["aciklama"][:300]})
    complete = report["eksiksizlik"]
    return {
        "eksiksizlik": complete,
        "pdf_tablo_satiri": sum(i.get("pdf_tablo_satiri", 0) for i in complete),
        "temsil_edilen": sum(i.get("temsil_edilen", 0) for i in complete),
        "eksik_tablo_satiri": sum(len(i.get("eksik_satirlar", [])) for i in complete),
        "eksik_grup_analiz": report["eksik_grup_analiz"],
        "pilot_satir": report["pilot_satir"], "pilot_isaret": len(pilot_flags),
        "pilot_isaretli_satir": len({f["satir_hash"] for f in pilot_flags}),
        "kategori_seviye": {c: dict(Counter(f["seviye"] for f in pilot_flags if f["kategori"] == c))
                            for c in sorted({f["kategori"] for f in pilot_flags})},
        "kontrol_kodu": dict(sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "tekrar": {"metin_tablo": len(report["tekrar"]["metin_tablo"]),
                   "metin_metin": len(report["tekrar"]["metin_metin"]),
                   "belirsiz": len(report["tekrar"]["belirsiz"]),
                   "ayrinti": report["tekrar"]},
        "ornekler": dict(sorted(samples.items())),
    }


def pilot_row_count(conn):
    """bilgi_turu dolu satır sayısı (sütun yoksa 0)."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME = 'klinik_bulgular' AND COLUMN_NAME = 'bilgi_turu'")
        if not cursor.fetchone()[0]:
            return 0
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE bilgi_turu IS NOT NULL")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# satir_hash çakışması (toplu iş çıktılarından)
# ---------------------------------------------------------------------------

def hash_collisions(rows, batch_dir=None):
    """{satir_hash: [farklı extracted_fields...]} — aynı hash, farklı taslak_notu alanları."""
    by_hash = defaultdict(dict)
    sources = list(rows)
    if batch_dir:
        from src.database import clinical_row_hash
        for path in sorted(Path(batch_dir).glob("runs/*/would_save.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = dict(json.loads(line)["satir"])
                except (ValueError, KeyError, TypeError):
                    continue
                row["satir_hash"] = row.get("satir_hash") or clinical_row_hash(row, row.get("cikarma_yontemi") or "rag")
                sources.append(row)
    for row in sources:
        note = _note(row) or {}
        fields = note.get("extracted_fields")
        key = json.dumps(fields, ensure_ascii=False, sort_keys=True) if isinstance(fields, dict) else None
        if key is not None:
            by_hash[row["satir_hash"]][key] = fields
    return {h: [v for _, v in sorted(variants.items())] for h, variants in by_hash.items() if len(variants) > 1}


def state_collision_hint(batch_dir):
    """state.jsonl'da kaydedilen bir adayda zaten_var > 0 olan kayıt sayısı."""
    path = Path(batch_dir) / "state.jsonl"
    count = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("durum") == "kaydedildi" and (record.get("zaten_var") or 0) > 0:
                count += 1
    return count


# ---------------------------------------------------------------------------
# Tüm kontroller
# ---------------------------------------------------------------------------

def run_checks(rows, ctx=None, *, collisions=None, embedder=None, embedding_threshold=EMBEDDING_THRESHOLD,
               source_filter=None, pilot=False):
    """source_filter: verilirse chunk temelli kaynak kontrolü yalnız filter(row) doğru olan satırlarda
    (--pilot: pilot satırları kendi pdf_pages kontrollerini alır).

    pilot=True (--pilot): pilot satırların GA–p ve yüzde aritmetiği run_pilot_checks'te
    (kaynak_ci_p_celiski, pilot_yuzde_uyumsuz) raporlanır; burada ikinci kez işaretlenmez."""
    rows = sorted(rows, key=lambda r: r["satir_hash"])
    flags = []
    flags += check_duplicates(rows, embedder, embedding_threshold)
    for row in rows:
        flags += check_numeric(row)
        if not (pilot and is_pilot(row)):
            flags += check_ci_p(row)
        flags += check_row_consistency(row)
        if ctx is not None and (source_filter is None or source_filter(row)):
            flags += check_source(row, ctx)
        if collisions and row["satir_hash"] in collisions:
            variants = collisions[row["satir_hash"]]
            flags.append(_flag(row, "hash_cakismasi_kayip_bulgu", "bilgi",
                               f"aynı satir_hash ile {len(variants)} farklı bulgu üretildi; yalnızca biri kaydedilebilir",
                               detail={"varyantlar": variants}))
    flags += check_group_consistency(rows)
    flags += check_statistical_groups(rows, pilot=pilot)
    flags += check_model_terminology(rows)
    return merge_flags(flags)


def merge_flags(flags):
    """Aynı (satir_hash, kod, grup) işaretlerini birleştirir; deterministik sıra."""
    merged = {}
    for f in flags:
        key = (f["satir_hash"], f["kontrol_kodu"], f["grup_id"])
        if key in merged:
            existing = merged[key]
            if f["aciklama"] not in existing["aciklama"].split("; "):
                existing["aciklama"] += "; " + f["aciklama"]
            if LEVEL_ORDER[f["seviye"]] < LEVEL_ORDER[existing["seviye"]]:
                existing["seviye"] = f["seviye"]
        else:
            merged[key] = dict(f)
    return sorted(merged.values(), key=lambda f: (LEVEL_ORDER[f["seviye"]], f["kategori"], f["kontrol_kodu"],
                                                  f["grup_id"], f["satir_hash"]))


# ---------------------------------------------------------------------------
# Girdiler
# ---------------------------------------------------------------------------

def _row_from_db(columns, values):
    row = {c: _text(v) for c, v in zip(columns, values)}
    return row


def load_db_rows(conn, *, hepsi=False, makale=(), pilot=False):
    sql = "SELECT * FROM klinik_bulgular"
    where, params = [], []
    if not hepsi:
        where.append("(cikarma_yontemi = 'rag' OR bilgi_turu IS NOT NULL)" if pilot else "cikarma_yontemi = 'rag'")
    if makale:
        where.append("makale_id IN (" + ", ".join(["%s"] * len(makale)) + ")")
        params += list(makale)
    if where:
        sql += " WHERE " + " AND ".join(where)
    cursor = conn.cursor()
    try:
        cursor.execute(sql + " ORDER BY satir_hash", params)
        columns = [d[0] for d in cursor.description]
        rows = [_row_from_db(columns, values) for values in cursor.fetchall()]
    finally:
        cursor.close()
    from src.database import clinical_row_hash
    for row in rows:
        row["satir_hash"] = row.get("satir_hash") or clinical_row_hash(row, row.get("cikarma_yontemi") or "rag")
        row["_kaynak"] = "db"
    return rows


def load_jsonl_rows(paths):
    from src.database import clinical_row_hash
    rows = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            row = dict(record.get("satir", record))
            row["satir_hash"] = row.get("satir_hash") or clinical_row_hash(row, row.get("cikarma_yontemi") or "rag")
            row["id"] = None
            row["_kaynak"] = "jsonl"
            rows.append(row)
    return rows


def load_reference_rows():
    """Referans taslağı clinical_draft ile AYNI biçimde satıra çevirir, ama validate_row'a sokmadan.

    clinical_draft.build_reference_rows yalnızca validate_row'dan geçenleri döndürür;
    kalite raporu ise bütün satırları (geçmeyenler dahil) incelemek ister. Salt okuma,
    kendi bağlantısı. Döndürür: (satırlar, {"eslesmeyen": n, "toplam": n}).
    """
    from src.clinical_candidates import collect_candidates, load_our_articles
    from src.clinical_draft import CIKARMA_YONTEMI, COLUMNS, ICD_BY_TANI_EN, load_reference
    from src.clinical_validation import find_chunk_id, load_validation_context
    from src.database import clinical_row_hash, get_connection

    reference = load_reference()
    conn = get_connection()
    try:
        articles = load_our_articles(conn)
        ctx = load_validation_context(conn)
        conn.rollback()
    finally:
        conn.close()
    candidates = collect_candidates(articles)
    file_by_id = {m: f for f, m, _, _ in articles}
    doc_by_id = {m: d for _, m, d, _ in articles}
    rows, unmatched = [], 0
    for item in reference["satirlar"]:
        makale_id, page = item["makale_id"], item["sayfa"]
        matches = [c for c in candidates if c["makale_id"] == makale_id and c["sayfa"] == page
                   and item["bul"] in c["cumle"]]
        if makale_id not in file_by_id or len(matches) != 1:
            unmatched += 1
            continue
        candidate = matches[0]
        tani = reference["tanilar"][item.get("tani", reference["makaleler"][makale_id]["tani"])]
        row = {column: "" for column in COLUMNS}
        row.update({k: v for k, v in item.items() if k in COLUMNS})
        row.update(aday_id=candidate["aday_id"], dosya=file_by_id[makale_id], makale_id=makale_id,
                   document_id=doc_by_id[makale_id], kanit_sayfa=page,
                   kanit_sayfa_bitis=candidate["sayfa_bitis"] or "", birlestirme=candidate["birlestirme"],
                   kanit_turu=candidate["kaynak_turu"], kanit_cumle=candidate["cumle"], tani_tr=tani["tr"],
                   tani_en=tani["en"], icd10=ICD_BY_TANI_EN.get(tani["en"], ""),
                   cikarma_yontemi=CIKARMA_YONTEMI, review_status="pending")
        row["hasta_sayisi"] = str(row["hasta_sayisi"])
        row["chunk_id"] = find_chunk_id(row, ctx) or ""
        row = dict(row, id=None, _kaynak="referans")
        row["satir_hash"] = clinical_row_hash(row, CIKARMA_YONTEMI)
        rows.append(row)
    return rows, {"toplam": len(reference["satirlar"]), "eslesmeyen": unmatched}


def dedupe_identical(rows):
    """Aynı satir_hash + aynı taslak_notu tekrarları (birden fazla koşu) tek satıra iner."""
    seen, unique = set(), []
    for row in rows:
        key = (row["satir_hash"], row.get("taslak_notu") or "")
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ---------------------------------------------------------------------------
# Şema ve yazım
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    """kalite_core.sql'i çalıştırır. DDL örtük COMMIT yapar: açık transaction içinde çağırmayın."""
    from src.database import _split_sql_statements

    cursor = conn.cursor()
    try:
        for statement in _split_sql_statements(KALITE_FILE.read_text(encoding="utf-8")):
            cursor.execute(statement)
    finally:
        cursor.close()


def schema_ready(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME IN ('kalite_isaretleri', 'kalite_calistirmalari')")
        return cursor.fetchone()[0] == 2
    finally:
        cursor.close()


def write_flags(conn, flags, rows, version, scope, *, own_transaction, delete_old_versions=False):
    hashes = sorted({r["satir_hash"] for r in rows})
    cursor = conn.cursor()
    try:
        if own_transaction:
            # get_connection() bağlantıyı autocommit=False açar; run_quality'nin önceki
            # SELECT'leri (load_db_rows, load_source_context, schema_ready) örtük bir
            # salt-okuma transaction'ı başlatmış olur ve start_transaction() "Transaction
            # already in progress" verir. Bağlantı bizim ve o ana kadar yazım yok: kapatmak
            # güvenli. Dış bağlantıda (own_transaction=False) çağıranın transaction'ına dokunulmaz.
            if conn.in_transaction:
                conn.rollback()
            conn.start_transaction()
        for start in range(0, len(hashes), 500):
            part = hashes[start:start + 500]
            cursor.execute("DELETE FROM kalite_isaretleri WHERE kontrol_surumu = %s AND satir_hash IN ("
                           + ", ".join(["%s"] * len(part)) + ")", [version, *part])
        if delete_old_versions:
            cursor.execute("DELETE FROM kalite_isaretleri WHERE kontrol_surumu <> %s", (version,))
        if flags:
            cursor.executemany(
                "INSERT INTO kalite_isaretleri (klinik_bulgu_id, satir_hash, kontrol_kodu, kategori, seviye, "
                "grup_id, aciklama, ayrinti, kontrol_surumu) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(f["klinik_bulgu_id"], f["satir_hash"], f["kontrol_kodu"], f["kategori"], f["seviye"],
                  f["grup_id"], f["aciklama"], json.dumps(f["ayrinti"], ensure_ascii=False, sort_keys=True), version)
                 for f in flags])
        counts = Counter(f"{f['kategori']}/{f['seviye']}" for f in flags)
        cursor.execute(
            "INSERT INTO kalite_calistirmalari (kontrol_surumu, kapsam, son_calisma, satir_sayisi, isaret_sayisi, sayilar) "
            "VALUES (%s, %s, NOW(), %s, %s, %s) ON DUPLICATE KEY UPDATE kapsam = VALUES(kapsam), "
            "son_calisma = VALUES(son_calisma), satir_sayisi = VALUES(satir_sayisi), "
            "isaret_sayisi = VALUES(isaret_sayisi), sayilar = VALUES(sayilar)",
            (version, scope, len(rows), len(flags), json.dumps(dict(sorted(counts.items())), ensure_ascii=False)))
        if own_transaction:
            conn.commit()
    except Exception:
        if own_transaction:
            conn.rollback()
        raise
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

CSV_COLUMNS = ("veri_etiketi", "seviye", "kategori", "kontrol_kodu", "grup_id", "satir_hash", "klinik_bulgu_id",
               "makale_id", "dosya", "kanit_sayfa", "kanit_turu", "mudahale", "doz", "deger", "unit",
               "hasta_grubu", "sonuc", "aciklama", "kanit_cumle", "bilgi_turu", "baglam")


def summarize(flags, rows, version, *, label=None, collisions=None, state_hint=None, source_checked=True, kapsam=""):
    matrix = defaultdict(Counter)
    for f in flags:
        matrix[f["kategori"]][f["seviye"]] += 1
    duplicate_groups = {f["grup_id"] for f in flags if f["kontrol_kodu"] == "tekrar_ayni_bilgi"}
    summary = {
        "kontrol_surumu": version, "kapsam": kapsam, "satir_sayisi": len(rows), "isaret_sayisi": len(flags),
        "isaretli_satir": len({f["satir_hash"] for f in flags}),
        "kategori_seviye": {k: dict(sorted(v.items(), key=lambda x: LEVEL_ORDER[x[0]])) for k, v in sorted(matrix.items())},
        "kontrol_kodu": dict(Counter(f["kontrol_kodu"] for f in flags).most_common()),
        "tekrar_grubu": len(duplicate_groups),
        "hash_cakismasi": len(collisions or {}),
        "kaynak_kontrolu": "yapıldı" if source_checked else "yapılmadı (--offline)",
    }
    if state_hint is not None:
        summary["state_zaten_var_ipucu"] = state_hint
    if label:
        summary["veri_etiketi"] = label
    return summary


def write_reports(report_dir, flags, rows, summary, label=None):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    by_hash = {r["satir_hash"]: r for r in rows}
    with (report_dir / "isaretler.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for f in flags:
            row = by_hash.get(f["satir_hash"], {})
            writer.writerow({
                "veri_etiketi": label or "", "seviye": f["seviye"], "kategori": f["kategori"],
                "kontrol_kodu": f["kontrol_kodu"], "grup_id": f["grup_id"], "satir_hash": f["satir_hash"],
                "klinik_bulgu_id": f["klinik_bulgu_id"] or "", "makale_id": row.get("makale_id"),
                "dosya": row.get("dosya") or "", "kanit_sayfa": row.get("kanit_sayfa"),
                "kanit_turu": row.get("kanit_turu"), "mudahale": mudahale_of(row), "doz": row.get("doz"),
                "deger": row.get("deger"), "unit": row.get("unit"), "hasta_grubu": row.get("hasta_grubu"),
                "sonuc": outcome_of(row), "aciklama": f["aciklama"], "kanit_cumle": (row.get("kanit_cumle") or "")[:300],
                "bilgi_turu": row.get("bilgi_turu") or "", "baglam": row.get("baglam") or "",
            })
    payload = {"veri_etiketi": label, "isaretler": flags}
    if summary.get("pilot"):
        payload["pilot"] = summary["pilot"]
    (report_dir / "isaretler.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                                               encoding="utf-8")
    groups = defaultdict(list)
    for f in flags:
        if f["grup_id"]:
            groups[(f["grup_id"], f["kontrol_kodu"])].append(f["satir_hash"])
    with (report_dir / "gruplar.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["veri_etiketi", "grup_id", "kontrol_kodu", "uye_sayisi", "uyeler"])
        for (gid, code), members in sorted(groups.items()):
            writer.writerow([label or "", gid, code, len(members), " ".join(sorted(members))])
    (report_dir / "ozet.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

@dataclass
class Options:
    hepsi: bool = False
    makale: list = field(default_factory=list)
    jsonl: list = field(default_factory=list)
    jsonl_ile_db: bool = False
    offline: bool = False
    referans: bool = False
    batch_dir: str = None
    dry_run: bool = False
    embedding: bool = False
    esik: float = EMBEDDING_THRESHOLD
    eski_surumleri_sil: bool = False
    rapor_dir: Path = DEFAULT_REPORT_DIR
    pilot: bool = False
    pdf_dir: Path = DEFAULT_PDF_DIR


class QualityError(Exception):
    pass


def run_quality(opts, *, connection=None, embedder=None, out=print, page_loader=None):
    """İşaretleri hesaplar; DB modunda (dry-run değilse) kalite_isaretleri'ni yeniler.

    page_loader: pdf yolu -> (doc, {sayfa: metin}); varsayılan src.pilot_clinical.pdf_pages (yalnız --pilot)."""
    if opts.referans and not opts.dry_run:
        raise QualityError("--referans yalnızca rapor modunda çalışır: --dry-run zorunlu.")
    if opts.referans and opts.jsonl:
        raise QualityError("--referans ve --jsonl birlikte kullanılamaz.")
    if opts.offline and not opts.jsonl:
        raise QualityError("--offline yalnızca --jsonl ile kullanılır.")
    if opts.pilot and (opts.referans or opts.offline):
        raise QualityError("--pilot, --referans / --offline ile kullanılamaz (PDF ve veritabanı gerekir).")
    mode = "referans" if opts.referans else "jsonl" if opts.jsonl else "db"
    label = REFERENCE_LABEL if opts.referans else None
    version = control_version(opts.embedding, opts.esik, pilot=opts.pilot)
    if opts.embedding and embedder is None:
        from src.embeddings import embed_documents
        embedder = lambda texts: embed_documents(texts, model_name="minilm")  # noqa: E731

    conn, own = connection, False
    if conn is None and not opts.offline:
        from src.database import get_connection
        conn, own = get_connection(), True
    reference_info = None
    try:
        if mode == "referans":
            rows, reference_info = load_reference_rows()
        elif mode == "jsonl":
            rows = load_jsonl_rows(opts.jsonl)
            if opts.jsonl_ile_db and conn is not None:
                rows += load_db_rows(conn, hepsi=opts.hepsi, makale=opts.makale, pilot=opts.pilot)
        else:
            rows = load_db_rows(conn, hepsi=opts.hepsi, makale=opts.makale, pilot=opts.pilot)
        if opts.makale:
            rows = [r for r in rows if r.get("makale_id") in opts.makale]
        rows = dedupe_identical(rows)
        ctx = load_source_context(conn) if conn is not None else None
        collisions = hash_collisions(rows, opts.batch_dir)
        flags = run_checks(rows, ctx, collisions=collisions, embedder=embedder, embedding_threshold=opts.esik,
                           source_filter=(lambda r: not is_pilot(r)) if opts.pilot else None, pilot=opts.pilot)
        pilot_report = None
        if opts.pilot:
            sources = load_pilot_sources(conn, {r.get("document_id") for r in rows if is_pilot(r)},
                                         opts.pdf_dir, page_loader)
            pilot_flags, pilot_report = run_pilot_checks(rows, sources)
            flags = merge_flags(flags + pilot_flags)
        if label:
            for f in flags:
                f["veri_etiketi"] = label
        kapsam = (mode if mode == "referans" else f"{mode}; {'hepsi' if opts.hepsi else 'rag'}") + (
            "; pilot" if opts.pilot else "") + (f"; makale={sorted(opts.makale)}" if opts.makale else "")
        summary = summarize(flags, rows, version, label=label, collisions=collisions,
                            state_hint=state_collision_hint(opts.batch_dir) if opts.batch_dir else None,
                            source_checked=ctx is not None, kapsam=kapsam)
        if pilot_report is not None:
            summary["pilot"] = pilot_summary(flags, rows, pilot_report)
        elif conn is not None and mode != "referans":
            waiting = pilot_row_count(conn)
            if waiting:
                summary["pilot_uyarisi"] = f"{waiting} {PILOT_WARNING}"
        if reference_info:
            summary["referans"] = {**reference_info, "satira_donusen": len(rows),
                                   "not": "aday cümlesiyle tam 1 eşleşmesi olmayan satırlar rapora girmez"}
        written = False
        if mode == "db" and not opts.dry_run:
            if not schema_ready(conn):
                raise QualityError("kalite_isaretleri tablosu yok: önce python -m src.data_quality --kur")
            write_flags(conn, flags, rows, version, kapsam, own_transaction=connection is None,
                        delete_old_versions=opts.eski_surumleri_sil)
            written = True
        summary["veritabanina_yazildi"] = written
        summary["mod"] = mode + (" (dry-run)" if opts.dry_run else "")
        report_dir = Path(opts.rapor_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{mode}")
        write_reports(report_dir, flags, rows, summary, label)
        summary["rapor_dir"] = str(report_dir)
        return summary, flags, rows
    finally:
        if own and conn is not None:
            conn.close()


def print_summary(summary, flags, out=print):
    if summary.get("veri_etiketi"):
        out(f"*** {summary['veri_etiketi']} ***")
    if summary.get("pilot_uyarisi"):
        out(f"UYARI: {summary['pilot_uyarisi']}")
    out(f"Kontrol sürümü: {summary['kontrol_surumu']}   mod: {summary['mod']}   kapsam: {summary['kapsam']}")
    out(f"Satır: {summary['satir_sayisi']}   işaret: {summary['isaret_sayisi']}   işaretli satır: "
        f"{summary['isaretli_satir']}   tekrar grubu: {summary['tekrar_grubu']}   "
        f"satir_hash çakışması: {summary['hash_cakismasi']}   kaynak kontrolü: {summary['kaynak_kontrolu']}")
    out(f"{'kategori':<12} {'hata':>5} {'uyari':>6} {'bilgi':>6}")
    for category, levels in summary["kategori_seviye"].items():
        out(f"{category:<12} {levels.get('hata', 0):>5} {levels.get('uyari', 0):>6} {levels.get('bilgi', 0):>6}")
    out("En sık kontroller: " + ", ".join(f"{k} ({v})" for k, v in list(summary["kontrol_kodu"].items())[:5]))
    pilot = summary.get("pilot")
    if pilot and not pilot["pilot_satir"]:
        out("PİLOT EKSİKSİZLİK: SQL'de bilgi_turu dolu satır yok; PDF'e göre eksiksizlik hesaplanamadı "
            "(önce src.pilot_clinical.import_records ile aktarım)")
    elif pilot:
        out(f"PİLOT EKSİKSİZLİK: PDF tablo veri satırı {pilot['pdf_tablo_satiri']}, SQL'de temsil edilen "
            f"{pilot['temsil_edilen']}, eksik {pilot['eksik_tablo_satiri']}; grubu/analizi eksik satır "
            f"{len(pilot['eksik_grup_analiz'])}")
        for item in pilot["eksiksizlik"]:
            if item.get("hata"):
                out(f"  {item.get('dosya') or item['document_id'][:12]}: PDF okunamadı ({item['hata']})")
                continue
            out(f"  {item['dosya']}: pilot satır {item['pilot_satir']} (metin {item['metin_satir']}, "
                f"tablo {item['tablo_satir']}); " + ", ".join(
                    f"Table {t['tablo_no']} {t['temsil_edilen']}/{t['pdf_satir']}" for t in item["tablolar"]))
            for m in item["eksik_satirlar"][:10]:
                out(f"    eksik: Table {m['tablo_no']} satır {m['satir_no']} {m['degisken']}")
            if len(item["eksik_satirlar"]) > 10:
                out(f"    … +{len(item['eksik_satirlar']) - 10} satır (ozet.json)")
        out(f"Pilot: {pilot['pilot_satir']} satır, {pilot['pilot_isaret']} işaret; tekrar metin↔tablo "
            f"{pilot['tekrar']['metin_tablo']}, metin↔metin {pilot['tekrar']['metin_metin']}, "
            f"belirsiz {pilot['tekrar']['belirsiz']}")
        out("Pilot kontrolleri: " + ", ".join(f"{k} ({v})" for k, v in pilot["kontrol_kodu"].items()))
    out(f"Veritabanına yazıldı: {'evet' if summary['veritabanina_yazildi'] else 'hayır'}   rapor: {summary['rapor_dir']}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.data_quality",
                                     description="klinik_bulgular için tekrar/sayısal/kaynak/tutarlılık işaretleri")
    parser.add_argument("--kur", action="store_true", help="kalite tablolarını ve VIEW'ları kur (her veritabanında bir kez)")
    parser.add_argument("--hepsi", action="store_true", help="yalnız rag değil, bütün cikarma_yontemi satırları")
    parser.add_argument("--makale", action="append", default=[], help="yalnızca bu makale_id (tekrarlanabilir)")
    parser.add_argument("--jsonl", action="append", default=[], help="would_save.jsonl (veritabanına yazmaz)")
    parser.add_argument("--jsonl-ile-db", action="store_true", help="JSONL satırlarını kayıtlı satırlarla birlikte değerlendir")
    parser.add_argument("--offline", action="store_true", help="--jsonl ile: veritabanını hiç kullanma (kaynak kontrolü yok)")
    parser.add_argument("--referans", action="store_true", help="doğrulanmamış referans taslak (yalnız --dry-run)")
    parser.add_argument("--batch-dir", help="toplu iş klasörü: satir_hash çakışmalarını say (ör. data/review/clinical_batch)")
    parser.add_argument("--dry-run", action="store_true", help="kalite_isaretleri'ne yazma; yalnızca rapor")
    parser.add_argument("--embedding", action="store_true", help="tekrar_benzer için yerel minilm embedding")
    parser.add_argument("--esik", type=float, default=EMBEDDING_THRESHOLD, help="embedding cosine eşiği (varsayılan 0.92)")
    parser.add_argument("--eski-surumleri-sil", action="store_true", help="diğer kontrol sürümlerinin işaretlerini sil")
    parser.add_argument("--rapor-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--pilot", action="store_true",
                        help="bilgi_turu dolu (pilot) satırları da al ve pdf_pages'e göre pilot kontrollerini çalıştır")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR, help="--pilot: PDF klasörü (varsayılan data/pdfs)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.kur:
        from src.database import get_connection
        conn = get_connection()
        try:
            ensure_schema(conn)
        finally:
            conn.close()
        print("Kuruldu: kalite_isaretleri, kalite_calistirmalari, v_kalite_ozet, v_kalite_bulgu")
        return 0
    opts = Options(hepsi=args.hepsi, makale=args.makale, jsonl=args.jsonl, jsonl_ile_db=args.jsonl_ile_db,
                   offline=args.offline, referans=args.referans, batch_dir=args.batch_dir, dry_run=args.dry_run,
                   embedding=args.embedding, esik=args.esik, eski_surumleri_sil=args.eski_surumleri_sil,
                   rapor_dir=args.rapor_dir, pilot=args.pilot, pdf_dir=args.pdf_dir)
    try:
        summary, flags, _ = run_quality(opts)
    except QualityError as exc:
        print(f"Hata: {exc}")
        return 2
    print_summary(summary, flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
