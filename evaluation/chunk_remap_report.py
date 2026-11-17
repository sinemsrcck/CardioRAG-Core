
"""
Chunk sürümü değişince evaluation/test_questions.csv'deki chunk_id atıfları için
eski -> yeni eşleme raporu (SALT OKUNUR; test_questions.csv DEĞİŞTİRİLMEZ).

Ne zaman çalıştırılır:
    Chunking sürümü değiştikten sonra (ör. chunking-table-v1), main'i çekip
    yeni sürümü veritabanına yazdıktan sonra:

        git pull
        python -m src.ingest
        python -m evaluation.chunk_remap_report

    src.ingest eski chunk'ları silmez; bu betik eski chunk metnini veritabanındaki
    eski sürümden, yeni chunk'ları pdf_belgeleri.processing_version'daki güncel
    sürümden okur. Bu yüzden veritabanında eski sürümün chunk'ları da bulunmalıdır
    (yeni sürümü yazmadan önce eski sürümle ingest yapılmış olmalı).

Çıktı:
    data/review/chunk_remap.csv  (UTF-8 BOM, ayraç ';')  + ekranda özet

Durumlar:
    guncel              atıf zaten güncel sürümdeki bir chunk'a işaret ediyor
    kimlik_degisti      aynı belge+sayfada metni birebir aynı tek bir güncel chunk var
                        -> yeni_chunk_id ile değiştirilebilir
    elle_bakilmali      metin değişmiş (ör. tablolu sayfa); aynı sayfadaki güncel
                        chunk'lar aday olarak, benzerlik oranıyla listelenir
    veritabaninda_yok   chunk_id pdf_chunks'ta hiç yok (daha eski bir sürüm)

Atıfların hangi soru ve sütunda geçtiği 'atif_yerleri' sütunundadır; geçmiş
(annotation_history_json) gibi arşiv sütunlarındaki atıfları değiştirip
değiştirmemek eşleştirmeyi yapanın kararıdır.
"""

import csv
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from src.database import get_connection


QUESTIONS_FILE = Path("evaluation/test_questions.csv")
OUTPUT_FILE = Path("data/review/chunk_remap.csv")
CHUNK_ID = re.compile(r"[0-9a-f]{64}_v[0-9a-f]{16}_p\d+_c\d+_t[0-9a-f]{64}")
COLUMNS = ("eski_chunk_id", "durum", "yeni_chunk_id", "dosya", "document_id", "sayfa",
           "eski_chunk_index", "atif_yerleri", "aday_benzerlik", "eski_metin", "aday_metin")


def _text(value):
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def collect_references(path=QUESTIONS_FILE):
    """{chunk_id: ['soru_id:sütun', ...]} — dosya yalnızca okunur."""
    references = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            for column, value in row.items():
                for chunk_id in set(CHUNK_ID.findall(value or "")):
                    references.setdefault(chunk_id, []).append(f"{row.get('id', '?')}:{column}")
    return references


def build_report(references):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("START TRANSACTION READ ONLY")
        ids = list(references)
        old = {}
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            cursor.execute(
                "SELECT c.chunk_id, c.document_id, c.page_number, c.chunk_index, c.text, "
                "c.processing_version, b.processing_version, b.filename "
                "FROM pdf_chunks AS c LEFT JOIN pdf_belgeleri AS b ON b.document_id = c.document_id "
                f"WHERE c.chunk_id IN ({', '.join(['%s'] * len(batch))})", batch)
            for cid, doc, page, index, text, version, current, filename in cursor.fetchall():
                old[_text(cid)] = {"document_id": _text(doc), "page": page, "index": index,
                                   "text": _text(text), "version": _text(version),
                                   "current_version": _text(current), "filename": _text(filename)}

        pages = {(o["document_id"], o["page"]) for o in old.values()}
        current = {}
        for document_id, page in pages:
            cursor.execute(
                "SELECT c.chunk_id, c.chunk_index, c.text FROM pdf_chunks AS c "
                "JOIN pdf_belgeleri AS b ON b.document_id = c.document_id "
                "  AND b.processing_version = c.processing_version "
                "WHERE c.document_id = %s AND c.page_number = %s ORDER BY c.chunk_index",
                (document_id, page))
            current[(document_id, page)] = [(_text(c), i, _text(t)) for c, i, t in cursor.fetchall()]
        conn.rollback()
    finally:
        conn.close()

    rows = []
    for chunk_id in sorted(references, key=lambda c: (old.get(c, {}).get("filename") or "", c)):
        where = "; ".join(sorted(references[chunk_id]))
        base = {"eski_chunk_id": chunk_id, "atif_yerleri": where}
        info = old.get(chunk_id)
        if info is None:
            rows.append({**base, "durum": "veritabaninda_yok"})
            continue
        base.update(dosya=info["filename"], document_id=info["document_id"],
                    sayfa=info["page"], eski_chunk_index=info["index"])
        if info["version"] == info["current_version"]:
            rows.append({**base, "durum": "guncel", "yeni_chunk_id": chunk_id})
            continue
        candidates = current.get((info["document_id"], info["page"]), [])
        exact = [c for c in candidates if c[2] == info["text"]]
        if len(exact) == 1:
            rows.append({**base, "durum": "kimlik_degisti", "yeni_chunk_id": exact[0][0]})
            continue
        scored = sorted(
            ((SequenceMatcher(None, info["text"], text, autojunk=False).ratio(), cid, text)
             for cid, _, text in candidates),
            reverse=True)
        if not scored:
            rows.append({**base, "durum": "elle_bakilmali", "eski_metin": info["text"]})
        for rank, (ratio, cid, text) in enumerate(scored):
            rows.append({**base, "durum": "elle_bakilmali", "aday_benzerlik": f"{ratio:.2f}",
                         "eski_metin": info["text"] if rank == 0 else "",
                         "yeni_chunk_id": cid, "aday_metin": text})
    return rows


def write_csv(rows, path=OUTPUT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in rows)


def print_summary(references, rows):
    status = {}
    for row in rows:
        status.setdefault(row["eski_chunk_id"], row["durum"])
    counts = {}
    for value in status.values():
        counts[value] = counts.get(value, 0) + 1
    print(f"\ntest_questions.csv: {len(references)} farklı chunk_id atfı")
    for name in ("guncel", "kimlik_degisti", "elle_bakilmali", "veritabaninda_yok"):
        print(f"  {name:<18} {counts.get(name, 0)}")
    manual = [r for r in rows if r["durum"] == "elle_bakilmali" and r.get("eski_metin")]
    if manual:
        print("\nElle bakılmalı (en benzer aday ile):")
        for row in manual:
            print(f"  - {row['dosya'][:40]} s.{row['sayfa']} c{row['eski_chunk_index']} "
                  f"| en iyi aday benzerlik {row.get('aday_benzerlik') or '-'} | {row['atif_yerleri'][:80]}")
            print(f"      eski: {row['eski_metin'][:110]!r}")
    print(f"\nCSV: {OUTPUT_FILE} — test_questions.csv değiştirilmedi.")


def main():
    if not QUESTIONS_FILE.exists():
        sys.exit(f"Bulunamadı: {QUESTIONS_FILE}")
    references = collect_references()
    rows = build_report(references)
    write_csv(rows)
    print_summary(references, rows)


if __name__ == "__main__":
    main()
