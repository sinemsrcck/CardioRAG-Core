
"""
MySQL şema testi (kardiyo_tam_metin).

Çalıştırma:
    python -m evaluation.test_database_schema

Gereken: proje kökünde .env (bkz. .env.example).
"""

import sys

from src.database import (
    COLLATION,
    CORE_TABLE_COLUMNS,
    CORE_VIEWS,
    MAKALELER_NEW_COLUMNS,
    count_professor_rows,
    get_connection,
    get_table_columns,
    init_schema,
    professor_row_label,
)


# Hoca tablolarındaki satır sayıları: şema kurulumu bunları değiştirmemeli.
# makaleler için yalnızca document_id IS NULL satırlar sayılır (bizim
# makalelerimiz hariç). Hocanın eski verisi temizlendi (README, kurulum
# adım 2): makaleler/tani_tedavi/sonuclar/semptom_iliskileri'de hocanın
# satırı yok; icd10_who ve tkd_kurallari referans olarak duruyor.
EXPECTED_PROTECTED_ROWS = {
    "makaleler": 0,
    "tani_tedavi": 0,
    "sonuclar": 0,
    "semptom_iliskileri": 0,
    "icd10_who": 43,
    "tkd_kurallari": 15,
}


def table_collation(cursor, table):
    cursor.execute(
        """
        SELECT TABLE_COLLATION
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def main():
    results = []  # (kontrol, beklenen, bulunan, geçti_mi)

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), passed))

    connection = get_connection()

    try:
        cursor = connection.cursor()

        # Hoca tablolarının şema kurulumundan ÖNCEKİ satır sayıları.
        before = {t: count_professor_rows(cursor, t) for t in EXPECTED_PROTECTED_ROWS}

        # Tekrar çalıştırılabilirlik: iki kez.
        for run in (1, 2):
            try:
                info = init_schema(connection)
                added = ", ".join(info["added_makaleler_columns"]) or "-"
                check(f"init_schema #{run}", "hatasız", f"hatasız (eklenen: {added})", True)
            except Exception as exc:
                check(f"init_schema #{run}", "hatasız", f"HATA: {exc}", False)

        # Yeni 8 tablo, sütunları ve collation'ı.
        for table, expected_columns in CORE_TABLE_COLUMNS.items():
            actual_columns = get_table_columns(cursor, table)
            missing = [c for c in expected_columns if c not in actual_columns]

            check(
                f"{table}: sütunlar",
                f"{len(expected_columns)} sütun",
                (f"{len(actual_columns)} sütun" if not missing
                 else f"eksik: {', '.join(missing)}" if actual_columns
                 else "tablo yok"),
                bool(actual_columns) and not missing,
            )
            check(f"{table}: collation", COLLATION, table_collation(cursor, table))

        # Hoca formatındaki VIEW'lar.
        for view in CORE_VIEWS:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (view,))
            check(f"VIEW {view}", "var", "var" if cursor.fetchone()[0] else "yok")

        # makaleler'e eklenen 3 sütun.
        makaleler_columns = get_table_columns(cursor, "makaleler")
        for column, _ in MAKALELER_NEW_COLUMNS:
            check(
                f"makaleler.{column}",
                "var",
                "var" if column in makaleler_columns else "yok",
            )

        # Hoca tablolarının satır sayıları değişmemiş olmalı.
        for table, expected in EXPECTED_PROTECTED_ROWS.items():
            after = count_professor_rows(cursor, table)
            check(
                f"{professor_row_label(table)}: satır sayısı",
                expected,
                f"{after} (önce {before[table]})",
                after == expected == before[table],
            )

        cursor.close()

    finally:
        connection.close()

    # Sonuç tablosu.
    width = max(len(r[0]) for r in results)
    print()
    print(f"{'Kontrol':<{width}}  {'Beklenen':<12}  {'Bulunan':<36}  Sonuç")
    print("-" * (width + 62))
    for name, expected, actual, passed in results:
        print(
            f"{name:<{width}}  {expected:<12}  {actual:<36}  "
            f"{'OK' if passed else 'HATA'}"
        )

    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)

    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
