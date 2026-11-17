
"""
Referans taslaktan (doğrulanmamış) klinik bulgu satırları üretir — YALNIZCA CSV.

Girdi : data/clinical/referans_taslak.json   (git'te takip edilir)
        meta.durum = "dogrulanmamis_referans": insan kontrolünden geçmemiştir;
        RAG çıktısıyla karşılaştırma amacıyla tutulur, klinik_bulgular'a
        doğrudan girmez.
Çıktı : data/review/klinik_bulgular_taslak.csv  (UTF-8 BOM, ayraç ';')

Çalıştırma:
    python -m src.clinical_draft

Bu modülün veritabanına yazma özelliği yoktur (yalnızca okur). Her satır
src.clinical_validation.validate_row ile denetlenir; geçmeyenler raporlanır.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from src.clinical_candidates import collect_candidates, load_our_articles
from src.clinical_validation import find_chunk_id, load_validation_context, validate_row
from src.database import get_connection


REFERENCE_FILE = Path("data/clinical/referans_taslak.json")
OUTPUT_FILE = Path("data/review/klinik_bulgular_taslak.csv")
CIKARMA_YONTEMI = "referans_taslak"

# icd10 elle girilmez: yalnızca bu açık tanı adlarından türetilir
# (kodun icd10_who'da bulunduğunu validate_row denetler).
ICD_BY_TANI_EN = {
    "Acute myocardial infarction": "I21",
    "ST elevation myocardial infarction": "I21",
    "Non-ST elevation myocardial infarction": "I21",
    "Out-of-hospital cardiac arrest": "I46",
    "Heart failure": "I50",
    "Diabetes mellitus": "E14",
}

COLUMNS = (
    "aday_id", "dosya", "makale_id", "document_id", "chunk_id", "study_name",
    "hasta_grubu", "hasta_sayisi", "mudahale_tr", "mudahale_en", "mudahale_turu",
    "comparator", "doz", "uygulama_yolu", "sure", "sonuc_olcutu_tr", "outcome_type",
    "etki_turu", "deger", "unit", "ci95", "p", "yon", "tani_tr", "tani_en", "icd10",
    "kanit_sayfa", "kanit_sayfa_bitis", "birlestirme", "kanit_turu", "kanit_cumle",
    "cikarma_yontemi", "review_status", "taslak_notu",
)


def load_reference(path=REFERENCE_FILE):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("meta", {}).get("durum") != "dogrulanmamis_referans":
        raise ValueError("Referans taslak meta.durum 'dogrulanmamis_referans' olmalı.")
    return data


def build_reference_rows(conn=None, reference=None):
    """
    Referans taslağı klinik_bulgular biçiminde satırlara çevirir ve denetler.
    Döndürür: (geçen_satırlar, reddedilenler[(etiket, hata_turu, aciklama)]).
    """
    reference = reference or load_reference()
    own = conn is None
    conn = conn or get_connection()
    try:
        articles = load_our_articles(conn)
        ctx = load_validation_context(conn)
        if own:
            conn.rollback()
    finally:
        if own:
            conn.close()

    candidates = collect_candidates(articles)
    file_by_id = {makale_id: f for f, makale_id, _, _ in articles}
    doc_by_id = {makale_id: d for _, makale_id, d, _ in articles}

    accepted, rejected = [], []
    for n, item in enumerate(reference["satirlar"], start=1):
        label = f"#{n} {item['makale_id'][-18:]} s.{item['sayfa']} '{item['bul'][:45]}'"
        makale_id, page = item["makale_id"], item["sayfa"]
        if makale_id not in file_by_id:
            rejected.append((label, "kanit_birebir_degil", "makale_id bulunamadı"))
            continue

        matches = [c for c in candidates if c["makale_id"] == makale_id
                   and c["sayfa"] == page and item["bul"] in c["cumle"]]
        if len(matches) != 1:
            rejected.append((label, "kanit_birebir_degil",
                             f"aday eşleşmesi {len(matches)} (tam 1 olmalı)"))
            continue
        candidate = matches[0]

        tani = reference["tanilar"][item.get("tani", reference["makaleler"][makale_id]["tani"])]
        row = {column: "" for column in COLUMNS}
        row.update({k: v for k, v in item.items() if k in COLUMNS})
        row.update(
            aday_id=candidate["aday_id"], dosya=file_by_id[makale_id], makale_id=makale_id,
            document_id=doc_by_id[makale_id], kanit_sayfa=page,
            kanit_sayfa_bitis=candidate["sayfa_bitis"] or "",
            birlestirme=candidate["birlestirme"], kanit_turu=candidate["kaynak_turu"],
            kanit_cumle=candidate["cumle"], tani_tr=tani["tr"], tani_en=tani["en"],
            icd10=ICD_BY_TANI_EN.get(tani["en"], ""),
            cikarma_yontemi=CIKARMA_YONTEMI, review_status="pending",
        )
        row["hasta_sayisi"] = str(row["hasta_sayisi"])

        ok, hata_turu, aciklama = validate_row(row, ctx)
        if not ok:
            rejected.append((label, hata_turu, aciklama))
            continue
        row["chunk_id"] = find_chunk_id(row, ctx) or ""
        accepted.append(row)

    return accepted, rejected


def write_csv(rows, path=OUTPUT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows, rejected):
    by_file = Counter(r["dosya"] for r in rows)
    print(f"\n{'Dosya':<62} {'Satır':>6} {'tedavi':>7} {'tanı t.':>8} {'Doz':>4} {'Birleşik':>9}")
    print("-" * 100)
    for filename in sorted(by_file):
        own = [r for r in rows if r["dosya"] == filename]
        kinds = Counter(r["mudahale_turu"] for r in own)
        print(f"{filename[:62]:<62} {len(own):>6} {kinds['tedavi']:>7} {kinds['tanı testi']:>8} "
              f"{sum(bool(r['doz']) for r in own):>4} {sum(bool(r['birlestirme']) for r in own):>9}")
    print("-" * 100)
    print(f"{'TOPLAM':<62} {len(rows):>6}  chunk_id dolu {sum(bool(r['chunk_id']) for r in rows)}, "
          f"icd10 dolu {sum(bool(r['icd10']) for r in rows)}")
    joined = [r for r in rows if r["birlestirme"]]
    if joined:
        print("\nBirleştirilmiş kanıtla geçen satırlar:")
        for r in joined:
            span = f"s.{r['kanit_sayfa']}" + (f"-{r['kanit_sayfa_bitis']}" if r["kanit_sayfa_bitis"] else "")
            print(f"  [{span} {r['birlestirme']}] {r['mudahale_tr']} | {r['deger'] or r['doz']}")
    print(f"\nREDDEDİLEN: {len(rejected)}")
    for label, hata_turu, aciklama in rejected:
        print(f"  - {label}: {hata_turu} — {aciklama}")


def main():
    rows, rejected = build_reference_rows()
    write_csv(rows)
    print_summary(rows, rejected)
    print(f"\nCSV: {OUTPUT_FILE} ({len(rows)} satır, cikarma_yontemi={CIKARMA_YONTEMI}, "
          "review_status=pending). Veritabanına hiçbir şey yazılmadı.")


if __name__ == "__main__":
    sys.exit(main())
