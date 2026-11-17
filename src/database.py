
"""
MySQL bağlantısı ve şema kurulumu (kardiyo_tam_metin).

Bağlantı bilgileri proje kökündeki .env dosyasından okunur
(bkz. .env.example). Şifre koda yazılmaz.

Kullanım:
    python -m src.database --check   # salt okunur durum raporu
    python -m src.database --init    # şemayı kur (tekrar çalıştırılabilir)
"""

import os
import re
import sys
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "sql" / "schema_core.sql"

load_dotenv(PROJECT_ROOT / ".env")

CHARSET = "utf8mb4"
COLLATION = "utf8mb4_bin"

# Hocanın verdiği tablolar: asla silinmez, verisi değiştirilmez.
PROTECTED_TABLES = (
    "icd10_who",
    "makaleler",
    "semptom_iliskileri",
    "sonuclar",
    "tani_tedavi",
    "tkd_kurallari",
)

# sql/schema_core.sql'deki yeni tablolar ve sütunları (oluşturma sırasıyla).
CORE_TABLE_COLUMNS = {
    "pdf_belgeleri": (
        "document_id", "filename", "page_count", "processing_version",
        "durum", "islenen_sayfa_sayisi", "olusturulma_zamani",
        "guncellenme_zamani",
    ),
    "pdf_isleme_durumu": (
        "document_id", "page_number", "ham_karakter", "temiz_karakter",
        "durum", "hata_mesaji", "islenme_zamani",
    ),
    "pdf_chunks": (
        "chunk_id", "document_id", "page_number", "chunk_index", "text",
        "text_sha256", "char_count", "processing_version", "chunk_size",
        "chunk_overlap", "olusturulma_zamani",
    ),
    "chunk_embeddings": (
        "chunk_id", "embedding_model", "dimension", "vector_dtype",
        "vector", "olusturulma_zamani",
    ),
    "klinik_bulgular": (
        "id", "makale_id", "document_id", "chunk_id", "study_name",
        "hasta_grubu", "hasta_sayisi", "mudahale_tr", "mudahale_en",
        "mudahale_turu", "comparator", "doz", "uygulama_yolu", "sure",
        "sonuc_olcutu_tr", "outcome_type", "etki_turu", "deger", "unit",
        "ci95", "p", "yon", "kanit_sayfa", "kanit_cumle", "review_status",
    ),
    "rag_sorgulari": (
        "query_id", "question", "answer", "llm_model", "embedding_model",
        "reranker_model", "evidence_status", "evidence_score",
        "guard_reason", "latency_ms", "created_at",
    ),
    "rag_sorgu_kaynaklari": (
        "query_id", "chunk_id", "retrieval_score", "rerank_score",
        "source_rank", "used_in_context",
    ),
    "halusinasyon_degerlendirme": (
        "id", "query_id", "soru_tipi", "claim_text", "supporting_chunk_id",
        "citation_correct", "claim_supported", "hata_turu",
        "uygulanan_onlem", "onceki_sonuc", "sonraki_sonuc", "duzeldi_mi",
        "evaluation_method", "review_status", "evaluation_version",
        "aciklama", "created_at",
    ),
}

# makaleler tablosuna eklenecek sütunlar: (ad, tanım). Hepsi NULL olabilir,
# bu yüzden mevcut satırların verisi değişmez.
MAKALELER_NEW_COLUMNS = (
    ("document_id", f"CHAR(64) CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    ("doi", f"VARCHAR(255) CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    ("abstract_en", f"LONGTEXT CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
)


def get_db_config():
    """Bağlantı ayarlarını ortam değişkenlerinden (.env) döndürür."""
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE", "kardiyo_tam_metin"),
    }


def get_connection():
    """kardiyo_tam_metin veritabanına yeni bir bağlantı açar."""
    return mysql.connector.connect(
        **get_db_config(),
        charset=CHARSET,
        collation=COLLATION,
        use_unicode=True,
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# information_schema yardımcıları (salt okunur)
# ---------------------------------------------------------------------------

def get_table_columns(cursor, table):
    """Tablonun sütun adlarını döndürür; tablo yoksa boş liste."""
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    return [row[0] for row in cursor.fetchall()]


def table_exists(cursor, table):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table,),
    )
    return cursor.fetchone()[0] > 0


def count_rows(cursor, table):
    """Tablodaki gerçek satır sayısı (COUNT(*), tahmini değil)."""
    cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
    return cursor.fetchone()[0]


def inspect_schema_state(connection):
    """
    Salt okunur durum raporu: yeni tablolar var mı, satır sayıları,
    eksik sütunlar ve makaleler'deki yeni sütunlar.
    """
    cursor = connection.cursor()

    tables = {}
    for table, expected in CORE_TABLE_COLUMNS.items():
        if not table_exists(cursor, table):
            tables[table] = {"exists": False}
            continue

        actual = get_table_columns(cursor, table)
        tables[table] = {
            "exists": True,
            "rows": count_rows(cursor, table),
            "missing_columns": [c for c in expected if c not in actual],
            "extra_columns": [c for c in actual if c not in expected],
        }

    makaleler_columns = get_table_columns(cursor, "makaleler")
    cursor.close()

    return {
        "tables": tables,
        "makaleler_new_columns": {
            column: column in makaleler_columns
            for column, _ in MAKALELER_NEW_COLUMNS
        },
    }


# ---------------------------------------------------------------------------
# Şema kurulumu
# ---------------------------------------------------------------------------

def _split_sql_statements(sql_text):
    """SQL dosyasını komutlara böler ('--' ile başlayan satırlar atılır)."""
    without_comments = "\n".join(
        line for line in sql_text.splitlines()
        if not line.lstrip().startswith("--")
    )

    return [
        statement.strip()
        for statement in re.split(r";\s*(?:\n|$)", without_comments)
        if statement.strip()
    ]


def _assert_no_stale_tables(cursor):
    """
    CREATE TABLE IF NOT EXISTS, eski tanımla kalmış bir tabloyu sessizce
    atlar. Bunu önlemek için eksik sütunlu tablo varsa dururuz.
    Hiçbir tablo otomatik olarak silinmez.
    """
    stale = []

    for table, expected in CORE_TABLE_COLUMNS.items():
        actual = get_table_columns(cursor, table)
        if not actual:
            continue

        missing = [c for c in expected if c not in actual]
        if missing:
            stale.append(
                f"{table} ({count_rows(cursor, table)} satır) "
                f"eksik sütunlar: {', '.join(missing)}"
            )

    if stale:
        raise RuntimeError(
            "Eski tanımla kalmış tablo(lar) bulundu; otomatik olarak "
            "değiştirilmedi:\n  - " + "\n  - ".join(stale)
        )


def _add_makaleler_columns(cursor):
    """makaleler tablosuna eksik sütunları ekler; var olanları atlar."""
    if not table_exists(cursor, "makaleler"):
        raise RuntimeError(
            "makaleler tablosu bulunamadı; MYSQL_DATABASE ayarını kontrol edin."
        )

    existing = set(get_table_columns(cursor, "makaleler"))
    added = []

    for column, definition in MAKALELER_NEW_COLUMNS:
        if column in existing:
            continue

        cursor.execute(
            f"ALTER TABLE makaleler ADD COLUMN `{column}` {definition}"
        )
        added.append(column)

    return added


def _makale_id_type_warning(cursor):
    """klinik_bulgular.makale_id ile makaleler.makale_id aynı tipte mi?"""
    cursor.execute(
        """
        SELECT TABLE_NAME, COLUMN_TYPE, COLLATION_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND COLUMN_NAME = 'makale_id'
          AND TABLE_NAME IN ('makaleler', 'klinik_bulgular')
        """
    )
    types = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    if len(types) == 2 and types["makaleler"] != types["klinik_bulgular"]:
        return (
            "makale_id tipi uyuşmuyor: "
            f"makaleler={types['makaleler']}, "
            f"klinik_bulgular={types['klinik_bulgular']}"
        )
    return None


def init_schema(connection=None, schema_file=SCHEMA_FILE):
    """
    sql/schema_core.sql'deki tabloları oluşturur ve makaleler tablosuna
    yeni sütunları ekler. Tekrar çalıştırılabilir; hiçbir şey silmez.

    Not: MySQL'de CREATE TABLE / ALTER TABLE örtük commit yapar.
    """
    own_connection = connection is None
    if own_connection:
        connection = get_connection()

    try:
        cursor = connection.cursor()
        _assert_no_stale_tables(cursor)

        sql_text = Path(schema_file).read_text(encoding="utf-8")
        for statement in _split_sql_statements(sql_text):
            cursor.execute(statement)

        added = _add_makaleler_columns(cursor)
        warning = _makale_id_type_warning(cursor)

        connection.commit()
        cursor.close()

        if warning:
            print(f"[UYARI] {warning}")

        return {"added_makaleler_columns": added, "warning": warning}

    finally:
        if own_connection:
            connection.close()


def _print_state(state):
    print(f"{'Tablo':<28} {'Durum':<8} {'Satır':>6}  Eksik sütun")
    print("-" * 70)
    for table, info in state["tables"].items():
        if not info["exists"]:
            print(f"{table:<28} {'yok':<8} {'-':>6}  -")
            continue
        missing = ", ".join(info["missing_columns"]) or "-"
        print(f"{table:<28} {'var':<8} {info['rows']:>6}  {missing}")

    print("\nmakaleler yeni sütunları:")
    for column, present in state["makaleler_new_columns"].items():
        print(f"  {column:<12} {'var' if present else 'yok'}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    conn = get_connection()
    try:
        if mode == "--init":
            print(init_schema(conn))
        _print_state(inspect_schema_state(conn))
    finally:
        conn.close()
