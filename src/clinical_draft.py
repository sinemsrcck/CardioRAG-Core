
"""
klinik_bulgular için TASLAK üretir (veritabanına YAZMAZ).

Girdi : data/review/klinik_bulgular_taslak_girdi.json
        (aday cümlelerden seçilmiş, alanları elle doldurulmuş satırlar)
Çıktı : data/review/klinik_bulgular_taslak.csv  (UTF-8 BOM, ayraç ';')

Çalıştırma:
    python -m src.clinical_draft

Her satır yazılmadan önce otomatik denetlenir; geçmeyen satır YAZILMAZ ve
gerekçesiyle raporlanır:
  1. Kanıt cümlesi tam_metin'de kanit_sayfa'da birebir geçmeli
     (metin: boşluklar normalize edilerek; tablo: satır hücre hücre aynı).
     Sayfa sınırını aşan / araya tablo-şekil girmiş birleşik cümleler reddedilir.
  2. Uydurma koruması: serbest metin alanlarındaki her sayı kanıtta geçmeli.
  3. icd10 elle girilmez: yalnızca ICD_BY_TANI_EN'deki açık tanı adlarından
     türetilir ve icd10_who tablosunda bulunmalıdır.
  4. mudahale_turu / etki_turu / yon hocanın değer kümesinden olmalı.
  5. review_status her zaman 'pending'.
"""

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

from src.clinical_candidates import (
    collect_candidates,
    iter_pages,
    load_our_articles,
    strip_markdown,
)
from src.database import get_connection


INPUT_FILE = Path("data/review/klinik_bulgular_taslak_girdi.json")
OUTPUT_FILE = Path("data/review/klinik_bulgular_taslak.csv")

MUDAHALE_TURU = {"tedavi", "tanı testi", "arka plan tedavisi"}
# Hocanın sonuclar tablosundaki değerler.
ETKI_TURU = {"", "OR", "RR", "HR", "IRR", "AUC / C-istatistigi", "oran / yuzde",
             "ortalama fark", "diger", "sayisal deger yok"}
YON = {"", "olumlu", "olumsuz", "notr / anlamsiz", "belirsiz"}
OUTCOME_TYPE = {"", "etkinlik", "güvenlik", "tanısal performans", "tanı kriteri",
                "prognostik değer", "öneri", "uygulama oranı", "çalışma popülasyonu"}

# icd10 yalnızca bu açık tanı adlarından türetilir (kod icd10_who'da olmalı).
ICD_BY_TANI_EN = {
    "Acute myocardial infarction": "I21",
    "ST elevation myocardial infarction": "I21",
    "Non-ST elevation myocardial infarction": "I21",
    "Out-of-hospital cardiac arrest": "I46",
    "Heart failure": "I50",
    "Diabetes mellitus": "E14",
}

# Sayı kontrolü yapılan serbest metin alanları.
NUMBER_CHECKED = ("hasta_grubu", "hasta_sayisi", "mudahale_tr", "mudahale_en",
                  "comparator", "doz", "uygulama_yolu", "sure", "sonuc_olcutu_tr",
                  "deger", "ci95", "p", "study_name")

COLUMNS = (
    "aday_id", "dosya", "makale_id", "document_id", "chunk_id", "study_name",
    "hasta_grubu", "hasta_sayisi", "mudahale_tr", "mudahale_en", "mudahale_turu",
    "comparator", "doz", "uygulama_yolu", "sure", "sonuc_olcutu_tr", "outcome_type",
    "etki_turu", "deger", "unit", "ci95", "p", "yon", "tani_tr", "tani_en", "icd10",
    "kanit_sayfa", "kanit_turu", "kanit_cumle", "review_status", "taslak_notu",
)


def _as_text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def norm_ws(text):
    return re.sub(r"\s+", " ", text).strip()


def table_rows(text):
    """Metindeki markdown tablo satırlarını aday üreticisiyle aynı biçimde verir."""
    rows = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and not re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", stripped):
            cells = [strip_markdown(c) for c in stripped.strip("|").split("|")]
            row = " | ".join(c for c in cells if c)
            if row:
                rows.add(row)
    return rows


def prose(text):
    return norm_ws(strip_markdown(text))


def numbers_in(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


# ---------------------------------------------------------------------------
# Veri (yalnızca okuma)
# ---------------------------------------------------------------------------

def load_context():
    conn = get_connection()
    try:
        articles = load_our_articles(conn)
        cursor = conn.cursor()
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute("SELECT kod FROM icd10_who")
        icd_codes = {_as_text(r[0]) for r in cursor.fetchall()}
        cursor.execute(
            "SELECT c.chunk_id, c.document_id, c.page_number, c.text "
            "FROM pdf_chunks AS c JOIN pdf_belgeleri AS b "
            "  ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
            "WHERE b.durum = 'tamamlandi'"
        )
        chunks = {}
        for chunk_id, document_id, page, text in cursor.fetchall():
            chunks.setdefault((_as_text(document_id), page), []).append(
                (_as_text(chunk_id), _as_text(text)))
        conn.rollback()
        cursor.close()
    finally:
        conn.close()
    return articles, icd_codes, chunks


# ---------------------------------------------------------------------------
# Satır kurma + denetim
# ---------------------------------------------------------------------------

def build_rows(spec, articles, icd_codes, chunks):
    candidates = collect_candidates(articles)
    pages = {(f, p): t for f, _, _, text in articles for p, t in iter_pages(text)}
    file_by_id = {makale_id: f for f, makale_id, _, _ in articles}
    doc_by_id = {makale_id: d for _, makale_id, d, _ in articles}

    missing_codes = {c for c in ICD_BY_TANI_EN.values() if c not in icd_codes}
    if missing_codes:
        raise ValueError(f"ICD eşleme tablosundaki kodlar icd10_who'da yok: {missing_codes}")

    accepted, rejected = [], []

    for n, item in enumerate(spec["satirlar"], start=1):
        label = f"#{n} {item['makale_id'][-18:]} s.{item['sayfa']} '{item['bul'][:45]}'"

        def reject(reason):
            rejected.append((label, reason))

        makale_id, page = item["makale_id"], item["sayfa"]
        if makale_id not in file_by_id:
            reject("makale_id bulunamadı")
            continue
        filename = file_by_id[makale_id]

        matches = [c for c in candidates if c["makale_id"] == makale_id
                   and c["sayfa"] == page and item["bul"] in c["cumle"]]
        if len(matches) != 1:
            reject(f"aday eşleşmesi {len(matches)} (tam 1 olmalı)")
            continue
        candidate = matches[0]

        if candidate["birlestirme"]:
            reject(f"birleşik cümle ({candidate['birlestirme']}): tek sayfada birebir geçmiyor")
            continue

        evidence = candidate["cumle"]
        if "kesit" in item:
            start_text, end_text = item["kesit"]
            i = evidence.find(start_text)
            j = evidence.find(end_text, max(i, 0))
            if i < 0 or j < 0:
                reject("kesit sınırları cümlede yok")
                continue
            evidence = evidence[i:j + len(end_text)]

        page_text = pages[(filename, page)]
        kind = candidate["kaynak_turu"]
        if kind == "tablo":
            verbatim = evidence in table_rows(page_text)
        else:
            verbatim = norm_ws(evidence) in prose(page_text)
        if not verbatim:
            reject("kanıt tam_metin'de kanit_sayfa'da birebir bulunamadı")
            continue

        # Alanlar: makale varsayılanı + satır
        tani_key = item.get("tani", spec["makaleler"][makale_id]["tani"])
        tani = spec["tanilar"][tani_key]
        row = {column: "" for column in COLUMNS}
        row.update({k: v for k, v in item.items() if k in COLUMNS})
        row.update(
            aday_id=candidate["aday_id"], dosya=filename, makale_id=makale_id,
            document_id=doc_by_id[makale_id], kanit_sayfa=page, kanit_turu=kind,
            kanit_cumle=evidence, review_status="pending",
            tani_tr=tani["tr"], tani_en=tani["en"],
            icd10=ICD_BY_TANI_EN.get(tani["en"], ""),
        )
        row["hasta_sayisi"] = str(row["hasta_sayisi"])

        problems = []
        if row["mudahale_turu"] not in MUDAHALE_TURU:
            problems.append(f"mudahale_turu '{row['mudahale_turu']}'")
        if row["etki_turu"] not in ETKI_TURU:
            problems.append(f"etki_turu '{row['etki_turu']}'")
        if row["yon"] not in YON:
            problems.append(f"yon '{row['yon']}'")
        if row["outcome_type"] not in OUTCOME_TYPE:
            problems.append(f"outcome_type '{row['outcome_type']}'")
        evidence_numbers = numbers_in(evidence)
        for field in NUMBER_CHECKED:
            extra = numbers_in(row[field]) - evidence_numbers
            if extra:
                problems.append(f"{field}: kanıtta olmayan sayı {sorted(extra)}")
        if row["hasta_sayisi"] and not row["hasta_sayisi"].isdigit():
            problems.append("hasta_sayisi tam sayı değil")
        if problems:
            reject("; ".join(problems))
            continue

        # chunk_id: kanıtın tamamen içinde geçtiği chunk
        for chunk_id, chunk_text in chunks.get((row["document_id"], page), []):
            hit = (evidence in table_rows(chunk_text) if kind == "tablo"
                   else norm_ws(evidence) in prose(chunk_text))
            if hit:
                row["chunk_id"] = chunk_id
                break

        accepted.append(row)

    return accepted, rejected


def write_csv(rows, path=OUTPUT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows, rejected, articles):
    print(f"\n{'Dosya':<62} {'Satır':>6} {'tedavi':>7} {'tanı t.':>8} {'arka p.':>8} {'Doz':>4}")
    print("-" * 100)
    for filename, *_ in articles:
        own = [r for r in rows if r["dosya"] == filename]
        kinds = Counter(r["mudahale_turu"] for r in own)
        print(f"{filename[:62]:<62} {len(own):>6} {kinds['tedavi']:>7} "
              f"{kinds['tanı testi']:>8} {kinds['arka plan tedavisi']:>8} "
              f"{sum(bool(r['doz']) for r in own):>4}")
    kinds = Counter(r["mudahale_turu"] for r in rows)
    print("-" * 100)
    print(f"{'TOPLAM':<62} {len(rows):>6} {kinds['tedavi']:>7} {kinds['tanı testi']:>8} "
          f"{kinds['arka plan tedavisi']:>8} {sum(bool(r['doz']) for r in rows):>4}")
    print(f"chunk_id dolu: {sum(bool(r['chunk_id']) for r in rows)}/{len(rows)}, "
          f"icd10 dolu: {sum(bool(r['icd10']) for r in rows)}/{len(rows)}, "
          f"kanıt türü: {dict(Counter(r['kanit_turu'] for r in rows))}")

    for filename, *_ in articles:
        own = [r for r in rows if r["dosya"] == filename]
        print(f"\n== {filename}")
        picks = []
        for wanted in ("doz", "tedavi", "tanı testi", None):
            for r in own:
                if r in picks:
                    continue
                if (wanted == "doz" and r["doz"]) or wanted == r["mudahale_turu"] or wanted is None:
                    picks.append(r)
                    break
            if len(picks) == 3:
                break
        for r in picks[:3]:
            fields = [f"{r['mudahale_turu']}: {r['mudahale_tr']}"]
            for key in ("doz", "sure", "hasta_grubu", "comparator", "sonuc_olcutu_tr"):
                if r[key]:
                    fields.append(f"{key}={r[key]}")
            effect = " ".join(x for x in (r["etki_turu"], r["deger"],
                                          f"GA {r['ci95']}" if r["ci95"] else "", r["p"]) if x)
            print(f"  [s.{r['kanit_sayfa']} {r['kanit_turu']}] " + " | ".join(fields))
            if effect:
                print(f"     etki: {effect}" + (f" | yön: {r['yon']}" if r["yon"] else ""))
            print(f"     tanı: {r['tani_tr']} ({r['icd10'] or 'icd10 yok'})")
            text = r["kanit_cumle"] if len(r["kanit_cumle"]) <= 200 else r["kanit_cumle"][:200] + "…"
            print(f"     kanıt: {text}")

    print(f"\nREDDEDİLEN (yazılmadı): {len(rejected)}")
    for label, reason in rejected:
        print(f"  - {label}: {reason}")


def main():
    spec = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    articles, icd_codes, chunks = load_context()
    rows, rejected = build_rows(spec, articles, icd_codes, chunks)
    write_csv(rows)
    print_summary(rows, rejected, articles)
    print(f"\nCSV: {OUTPUT_FILE} ({len(rows)} satır, hepsi review_status=pending). "
          "Veritabanına hiçbir şey yazılmadı.")


if __name__ == "__main__":
    sys.exit(main())
