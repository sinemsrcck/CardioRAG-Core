
"""
makaleler tablosuna eklenen PDF makaleleri testi.

Çalıştırma:
    python -m evaluation.test_makaleler

Ön koşul: python -m src.article_metadata --write --tam-metin temiz
Bu test yazımı bir kez daha çalıştırır (aynı değerlerle upsert) ve satır
sayısının artmadığını, hocanın satırlarının değişmediğini doğrular.
"""

import sys

from src.article_metadata import (
    INTERPRETIVE_COLUMNS,
    MANAGED_COLUMNS,
    build_all,
    professor_rows_fingerprint,
    write_records,
)
from src.database import get_connection


# Hocanın 5 satırının, ilk --write'tan ÖNCE alınmış parmak izi
# (bütün sütunlar, makale_id sırasıyla, SHA-256).
PROFESSOR_BASELINE = (5, "433d90bef85bc651fc975ad6234b36c4214e0e35f7cf98722f3289526f946ad2")

EXPECTED_OUR_ROWS = 6
KNOWN_DOIS = {
    "who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf": "10.29271/jcpsp.2020.09.906",
    "who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf": "10.26719/emhj.20.141",
}


def _text(value):
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


def our_rows(cursor):
    columns = ("makale_id",) + MANAGED_COLUMNS + INTERPRETIVE_COLUMNS
    cursor.execute(
        f"SELECT {', '.join(f'm.`{c}`' for c in columns)}, b.filename, b.page_count "
        "FROM makaleler AS m "
        "LEFT JOIN pdf_belgeleri AS b ON b.document_id = m.document_id "
        "WHERE m.document_id IS NOT NULL ORDER BY b.filename"
    )
    rows = []
    for values in cursor.fetchall():
        row = dict(zip(columns + ("_filename", "_page_count"), (_text(v) for v in values)))
        rows.append(row)
    return rows


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    conn = get_connection()
    conn.autocommit = True
    cursor = conn.cursor()

    # 1) Hocanın satırları ilk yazımdan önceki haliyle birebir aynı.
    check("hocanın satırları (önce)", PROFESSOR_BASELINE[1][:16] + "…",
          professor_rows_fingerprint(cursor)[1][:16] + "…",
          professor_rows_fingerprint(cursor) == PROFESSOR_BASELINE)

    # 2) Bizim satırlarımız.
    rows = our_rows(cursor)
    check("document_id dolu satır", EXPECTED_OUR_ROWS, len(rows))
    check("hepsi pdf_belgeleri'de", EXPECTED_OUR_ROWS,
          sum(r["_filename"] is not None for r in rows))
    cursor.execute(
        "SELECT COUNT(*) FROM pdf_belgeleri AS b "
        "WHERE b.durum = 'tamamlandi' AND NOT EXISTS "
        "(SELECT 1 FROM makaleler AS m WHERE m.document_id = b.document_id)"
    )
    check("makaleler'de karşılığı olmayan belge", 0, cursor.fetchone()[0])
    check("document_id tekrar", 0, len(rows) - len({r["document_id"] for r in rows}))

    by_file = {r["_filename"]: r for r in rows}
    for filename, doi in KNOWN_DOIS.items():
        check(f"DOI {doi}", doi, (by_file.get(filename) or {}).get("doi"))

    check("makale_id = DOI-<doi>", EXPECTED_OUR_ROWS,
          sum(r["makale_id"] == f"DOI-{r['doi'].lower()}" for r in rows if r["doi"]))
    check("baglanti = https://doi.org/<doi>", EXPECTED_OUR_ROWS,
          sum(r["baglanti"] == f"https://doi.org/{r['doi']}" for r in rows if r["doi"]))
    check("pdf_sayfa = page_count", EXPECTED_OUR_ROWS,
          sum(int(r["pdf_sayfa"]) == int(r["_page_count"]) for r in rows))
    check("yorum alanları NULL", EXPECTED_OUR_ROWS,
          sum(all(r[c] is None for c in INTERPRETIVE_COLUMNS) for r in rows))
    check("ozet_tr yalnızca TK'de", "TK", ", ".join(sorted(
        {r["_filename"].split("-", 1)[0] for r in rows if r["ozet_tr"]})))

    empty_abstracts = [r["_filename"] for r in rows if not r["abstract_en"]]
    check("abstract_en boş olanlar (bilgi)", "-",
          ", ".join(empty_abstracts) or "yok", True)

    # 3) Veritabanı içeriği = PDF'ten yeniden üretilen kayıtlar.
    built = build_all(conn, "temiz")
    expected = {b["record"]["document_id"]: b["record"] for b in built}
    differing = [
        f"{r['_filename'][:20]}.{c}"
        for r in rows for c in ("makale_id",) + MANAGED_COLUMNS
        if str(r[c]) != str(expected[r["document_id"]][c])
    ]
    check("içerik = PDF'ten üretilen", "aynı", ", ".join(differing) or "aynı")

    # 4) İkinci yazım: satır sayısı artmamalı, hocanın satırları değişmemeli.
    cursor.execute("SELECT COUNT(*) FROM makaleler")
    total_before = cursor.fetchone()[0]
    result = write_records(conn, built)
    cursor.execute("SELECT COUNT(*) FROM makaleler")
    total_after = cursor.fetchone()[0]

    check("2. yazım: eklenen", 0, result["inserted"])
    check("2. yazım: güncellenen", EXPECTED_OUR_ROWS, result["updated"])
    check("2. yazım: makaleler toplam", total_before, total_after)
    check("hocanın satırları (sonra)", PROFESSOR_BASELINE[1][:16] + "…",
          professor_rows_fingerprint(cursor)[1][:16] + "…",
          professor_rows_fingerprint(cursor) == PROFESSOR_BASELINE)

    cursor.close()
    conn.close()

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<22}  {'Bulunan':<44}  Sonuç")
    print("-" * (width + 78))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:22]:<22}  {act[:44]:<44}  {'OK' if passed else 'HATA'}")

    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
