
"""
MySQL bağlantısı ve şema kurulumu (kardiyo_tam_metin).

Bağlantı bilgileri proje kökündeki .env dosyasından okunur
(bkz. .env.example). Şifre koda yazılmaz.

Kullanım:
    python -m src.database --check   # salt okunur durum raporu
    python -m src.database --init    # şemayı kur (tekrar çalıştırılabilir)
"""

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import mysql.connector
import numpy as np
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
        "durum", "islenen_sayfa_sayisi", "hata_mesaji",
        "olusturulma_zamani", "guncellenme_zamani",
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
        "tani_tr", "tani_en", "icd10", "kanit_turu", "kanit_sayfa_bitis",
        "birlestirme", "cikarma_yontemi", "taslak_notu", "query_id", "satir_hash",
    ),
    "rag_sorgulari": (
        "query_id", "question", "answer", "llm_model", "embedding_model",
        "reranker_model", "evidence_status", "evidence_score",
        "guard_reason", "latency_ms", "created_at",
        "rerank_method", "citation_check",
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
        "aciklama", "created_at", "kayit_turu", "ham_cikti",
    ),
}

CORE_VIEWS = ("v_tani_tedavi", "v_sonuclar")
VIEWS_FILE = PROJECT_ROOT / "sql" / "views_core.sql"

# makaleler tablosuna eklenecek sütunlar: (ad, tanım). Hepsi NULL olabilir,
# bu yüzden mevcut satırların verisi değişmez.
MAKALELER_NEW_COLUMNS = (
    ("document_id", f"CHAR(64) CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    ("doi", f"VARCHAR(255) CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    ("abstract_en", f"LONGTEXT CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    # ozet_tr'nin kaynağı: 'orijinal' = PDF'teki Türkçe özet (TK makaleleri),
    # 'llm_ceviri' = abstract_en'in LLM çevirisi (src/load_translations.py).
    # Hocanın satırlarında NULL kalır.
    ("ozet_tr_kaynak",
     f"ENUM('orijinal', 'llm_ceviri') CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
    ("ozet_tr_model", f"VARCHAR(100) CHARACTER SET {CHARSET} COLLATE {COLLATION} NULL"),
)

# Yeni tablolara sonradan eklenen sütunlar (ilk kurulumdan sonra gelenler).
# CREATE TABLE IF NOT EXISTS var olan tabloyu değiştirmediği için bunlar
# information_schema kontrolüyle eklenir. Yalnızca ekleme; silme yok.
CORE_COLUMN_MIGRATIONS = {
    "pdf_belgeleri": (
        ("hata_mesaji", "TEXT NULL AFTER islenen_sayfa_sayisi"),
    ),
    "klinik_bulgular": (
        ("tani_tr", "VARCHAR(255) NULL"),
        ("tani_en", "VARCHAR(255) NULL"),
        ("icd10", "VARCHAR(64) NULL"),
        ("kanit_turu", "ENUM('metin', 'tablo') NULL"),
        ("kanit_sayfa_bitis", "INT UNSIGNED NULL"),
        ("birlestirme", "ENUM('sayfa', 'ara_blok') NULL"),
        ("cikarma_yontemi", "ENUM('rag', 'referans_taslak', 'manuel') NULL"),
        ("taslak_notu", "TEXT NULL"),
        ("query_id", "CHAR(36) NULL"),
        ("satir_hash", "CHAR(64) NULL"),
    ),
    "rag_sorgulari": (
        ("rerank_method", "VARCHAR(32) NULL"),
        ("citation_check", "JSON NULL"),
    ),
    "halusinasyon_degerlendirme": (
        ("kayit_turu", "ENUM('soru_cevap', 'klinik_cikarim') NOT NULL DEFAULT 'soru_cevap'"),
        ("ham_cikti", "JSON NULL"),
    ),
}

# Ad ile kontrol edilip eksikse eklenen kısıtlar (yalnızca ekleme).
CORE_CONSTRAINT_MIGRATIONS = {
    "klinik_bulgular": (
        ("uq_klinik_bulgular_satir_hash", "ADD UNIQUE KEY uq_klinik_bulgular_satir_hash (satir_hash)"),
        ("fk_klinik_bulgular_sorgu",
         "ADD CONSTRAINT fk_klinik_bulgular_sorgu FOREIGN KEY (query_id) "
         "REFERENCES rag_sorgulari (query_id) ON DELETE RESTRICT ON UPDATE CASCADE"),
    ),
    "halusinasyon_degerlendirme": (
        ("chk_halusinasyon_soru_tipi",
         "ADD CONSTRAINT chk_halusinasyon_soru_tipi "
         "CHECK (kayit_turu <> 'soru_cevap' OR soru_tipi IS NOT NULL)"),
    ),
}

# VARCHAR genişletmeleri: (sütun, tanım, en az uzunluk). Yalnızca mevcut
# uzunluk küçükse uygulanır; veri kaybı olmaz.
CORE_WIDEN_MIGRATIONS = {
    "klinik_bulgular": (
        ("uygulama_yolu", "VARCHAR(255) NULL", 255),
        ("sure", "VARCHAR(255) NULL", 255),
        ("deger", "VARCHAR(255) NULL", 255),
        ("ci95", "VARCHAR(255) NULL", 255),
        ("p", "VARCHAR(255) NULL", 255),
    ),
}

# NOT NULL -> NULL genişletmeleri (veri kaybı olmaz; yalnızca kısıt gevşer).
# Soru-cevap kayıtlarında soru_tipi zorunluluğu chk_halusinasyon_soru_tipi ile korunur.
CORE_NULLABLE_MIGRATIONS = {
    "halusinasyon_degerlendirme": (
        ("soru_tipi", "ENUM('cevaplanabilir', 'cevaplanamaz', 'celiskili') NULL"),
    ),
}

VECTOR_DTYPE = "float32"
# Vektörler little-endian float32 olarak saklanır (numpy '<f4').
_NUMPY_VECTOR_DTYPE = np.dtype("<f4")


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


# Hoca tablolarında "hocanın satırı" tanımı. makaleler'e bizim eklediğimiz
# satırların document_id'si doludur; hocanınkiler NULL.
PROFESSOR_ROW_FILTERS = {"makaleler": "document_id IS NULL"}


def count_professor_rows(cursor, table):
    """Hoca tablosundaki yalnızca hocaya ait satırların sayısı."""
    where = PROFESSOR_ROW_FILTERS.get(table)
    sql = f"SELECT COUNT(*) FROM `{table}`" + (f" WHERE {where}" if where else "")
    cursor.execute(sql)
    return cursor.fetchone()[0]


def professor_row_label(table):
    where = PROFESSOR_ROW_FILTERS.get(table)
    return f"{table} ({where})" if where else table


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

        migratable = {c for c, _ in CORE_COLUMN_MIGRATIONS.get(table, ())}
        missing = [
            c for c in expected
            if c not in actual and c not in migratable
        ]
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


def _add_missing_columns(cursor, table, columns):
    """Tabloya eksik sütunları ekler; var olanları atlar."""
    existing = set(get_table_columns(cursor, table))
    added = []

    for column, definition in columns:
        if column in existing:
            continue

        cursor.execute(
            f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
        )
        added.append(f"{table}.{column}")

    return added


def _constraint_exists(cursor, table, name):
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s
        """,
        (table, name),
    )
    return cursor.fetchone()[0] > 0


def _add_missing_constraints(cursor):
    """CORE_CONSTRAINT_MIGRATIONS'taki eksik kısıtları ekler."""
    added = []
    for table, constraints in CORE_CONSTRAINT_MIGRATIONS.items():
        for name, clause in constraints:
            if not _constraint_exists(cursor, table, name):
                cursor.execute(f"ALTER TABLE `{table}` {clause}")
                added.append(f"{table}.{name}")
    return added


def _apply_nullable_migrations(cursor):
    """NOT NULL sütunları NULL'a açar (yalnızca gevşetme; veri değişmez)."""
    changed = []
    for table, columns in CORE_NULLABLE_MIGRATIONS.items():
        for column, definition in columns:
            cursor.execute(
                """
                SELECT IS_NULLABLE FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
                """,
                (table, column),
            )
            row = cursor.fetchone()
            if row and _as_text(row[0]) == "NO":
                cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {definition}")
                changed.append(f"{table}.{column} NULL")
    for table, columns in CORE_WIDEN_MIGRATIONS.items():
        lengths = _column_max_lengths(cursor, table)
        for column, definition, minimum in columns:
            if column in lengths and lengths[column] is not None and lengths[column] < minimum:
                cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {definition}")
                changed.append(f"{table}.{column} -> {minimum}")
    return changed


def _column_max_lengths(cursor, table):
    """Metin sütunlarının azami karakter uzunlukları {sütun: uzunluk}."""
    cursor.execute(
        """
        SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table,),
    )
    return {_as_text(name): length for name, length in cursor.fetchall()}


def _add_makaleler_columns(cursor):
    """makaleler tablosuna eksik sütunları ekler; var olanları atlar."""
    if not table_exists(cursor, "makaleler"):
        raise RuntimeError(
            "makaleler tablosu bulunamadı; MYSQL_DATABASE ayarını kontrol edin."
        )

    added = _add_missing_columns(cursor, "makaleler", MAKALELER_NEW_COLUMNS)
    return [name.split(".", 1)[1] for name in added]


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

        migrated = []
        for table, columns in CORE_COLUMN_MIGRATIONS.items():
            migrated += _add_missing_columns(cursor, table, columns)
        migrated += _apply_nullable_migrations(cursor)
        migrated += _add_missing_constraints(cursor)

        # VIEW'lar sütun geçişlerinden sonra (yeni sütunlara dayanırlar).
        for statement in _split_sql_statements(Path(VIEWS_FILE).read_text(encoding="utf-8")):
            cursor.execute(statement)

        added = _add_makaleler_columns(cursor)
        warning = _makale_id_type_warning(cursor)

        connection.commit()
        cursor.close()

        if warning:
            print(f"[UYARI] {warning}")

        return {
            "added_makaleler_columns": added,
            "migrated_columns": migrated,
            "warning": warning,
        }

    finally:
        if own_connection:
            connection.close()


# ---------------------------------------------------------------------------
# Yazma fonksiyonları
#
# Bu fonksiyonlar commit ETMEZ. Bir PDF'in bütün yazımı çağıranın açtığı tek
# transaction içinde yapılır (bkz. src/ingest.py): hata olursa hepsi geri alınır.
# ---------------------------------------------------------------------------

def _as_text(value):
    """utf8mb4_bin sütunları bazı sürücülerde bytes döner; str'ye çevirir."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def _page_status(raw_text, cleaned_text):
    """Ham ve temizlenmiş sayfa metnine göre pdf_isleme_durumu.durum."""
    if not raw_text.strip():
        return "bos_sayfa"
    if cleaned_text is None:
        return "kaynakca_silindi"
    if len(cleaned_text) < len(raw_text):
        return "kismen_kaynakca"
    return "islendi"


def save_document(conn, raw_document, cleaned_document):
    """
    pdf_belgeleri'ye upsert, pdf_isleme_durumu'na her ham sayfa için bir satır.
    Döndürür: {sayfa_durumu: adet}.
    """
    raw_pages = raw_document["pages"]
    page_count = raw_document["page_count"]

    if raw_document["document_id"] != cleaned_document["document_id"]:
        raise ValueError("Ham ve temizlenmiş belge farklı document_id taşıyor.")
    page_numbers = [page["page_number"] for page in raw_pages]
    if page_numbers != list(range(1, page_count + 1)):
        raise ValueError(
            f"Ham sayfalar eksik/sırasız: {len(raw_pages)} sayfa, "
            f"page_count={page_count}"
        )

    cleaned_by_page = {
        page["page_number"]: page["text"]
        for page in cleaned_document["pages"]
    }

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO pdf_belgeleri
            (document_id, filename, page_count, processing_version,
             durum, islenen_sayfa_sayisi, hata_mesaji)
        VALUES (%s, %s, %s, %s, 'tamamlandi', %s, NULL) AS yeni
        ON DUPLICATE KEY UPDATE
            filename = yeni.filename,
            page_count = yeni.page_count,
            processing_version = yeni.processing_version,
            durum = yeni.durum,
            islenen_sayfa_sayisi = yeni.islenen_sayfa_sayisi,
            hata_mesaji = NULL
        """,
        (
            raw_document["document_id"],
            raw_document["filename"],
            page_count,
            cleaned_document["processing_version"],
            page_count,
        ),
    )

    rows = []
    distribution = {}
    for page in raw_pages:
        cleaned_text = cleaned_by_page.get(page["page_number"])
        status = _page_status(page["text"], cleaned_text)
        distribution[status] = distribution.get(status, 0) + 1
        rows.append((
            raw_document["document_id"],
            page["page_number"],
            len(page["text"]),
            len(cleaned_text or ""),
            status,
        ))

    cursor.executemany(
        """
        INSERT INTO pdf_isleme_durumu
            (document_id, page_number, ham_karakter, temiz_karakter,
             durum, hata_mesaji)
        VALUES (%s, %s, %s, %s, %s, NULL) AS yeni
        ON DUPLICATE KEY UPDATE
            ham_karakter = yeni.ham_karakter,
            temiz_karakter = yeni.temiz_karakter,
            durum = yeni.durum,
            hata_mesaji = NULL
        """,
        rows,
    )
    cursor.close()

    return distribution


def mark_document_failed(conn, document_id, filename, error_message,
                         page_count=0, processing_version="bilinmiyor"):
    """
    Başarısız PDF'i pdf_belgeleri'ye 'hatali' olarak yazar.
    Daha önce kaydı varsa yalnızca durum ve hata mesajı güncellenir;
    önceki başarılı işlemenin sürüm bilgisi korunur.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO pdf_belgeleri
            (document_id, filename, page_count, processing_version,
             durum, islenen_sayfa_sayisi, hata_mesaji)
        VALUES (%s, %s, %s, %s, 'hatali', 0, %s) AS yeni
        ON DUPLICATE KEY UPDATE
            durum = 'hatali',
            hata_mesaji = yeni.hata_mesaji
        """,
        (document_id, filename, page_count, processing_version,
         error_message[:60000]),
    )
    cursor.close()


def _existing_ids(conn, table, chunk_ids, embedding_model=None):
    """Verilen chunk_id'lerden tabloda zaten olanları döndürür."""
    chunk_ids = list(chunk_ids)
    if not chunk_ids:
        return set()

    cursor = conn.cursor()
    found = set()
    for start in range(0, len(chunk_ids), 500):
        batch = chunk_ids[start:start + 500]
        placeholders = ", ".join(["%s"] * len(batch))
        sql = f"SELECT chunk_id FROM `{table}` WHERE chunk_id IN ({placeholders})"
        params = list(batch)
        if embedding_model is not None:
            sql += " AND embedding_model = %s"
            params.append(embedding_model)
        cursor.execute(sql, params)
        found.update(_as_text(row[0]) for row in cursor.fetchall())
    cursor.close()
    return found


def get_existing_embedding_ids(conn, chunk_ids, embedding_model):
    """(chunk_id, embedding_model) çifti zaten kayıtlı olan chunk_id'ler."""
    return _existing_ids(conn, "chunk_embeddings", chunk_ids, embedding_model)


def save_chunks(conn, chunks):
    """
    pdf_chunks'a yazar. chunk_id metnin hash'ini içerdiği için var olan
    chunk_id yeniden yazılmaz (çift kayıt oluşmaz).
    Döndürür: {"new": n, "existing": m}.
    """
    for chunk in chunks:
        digest = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        if digest != chunk["text_sha256"]:
            raise ValueError(f"text_sha256 uyuşmuyor: {chunk['chunk_id']}")

    existing = _existing_ids(conn, "pdf_chunks", (c["chunk_id"] for c in chunks))
    new_chunks = [c for c in chunks if c["chunk_id"] not in existing]

    if new_chunks:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO pdf_chunks
                (chunk_id, document_id, page_number, chunk_index, text,
                 text_sha256, char_count, processing_version,
                 chunk_size, chunk_overlap)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    c["chunk_id"], c["document_id"], c["page_number"],
                    c["chunk_index"], c["text"], c["text_sha256"],
                    c["char_count"], c["processing_version"],
                    c["chunk_size"], c["chunk_overlap"],
                )
                for c in new_chunks
            ],
        )
        cursor.close()

    return {"new": len(new_chunks), "existing": len(chunks) - len(new_chunks)}


def _normalized_float32(vectors):
    """Vektörleri doğrular, L2-normalize eder ve float32'ye çevirir."""
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("Embedding matrisi iki boyutlu olmalı.")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("Embedding içinde NaN/inf var.")

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Sıfır uzunluklu embedding normalize edilemez.")

    return (matrix / norms).astype(_NUMPY_VECTOR_DTYPE)


def save_embeddings(conn, chunk_ids, vectors, embedding_model):
    """
    chunk_embeddings'e normalize float32 vektörleri BLOB olarak yazar.
    (chunk_id, embedding_model) zaten varsa o satır atlanır.
    Döndürür: {"new": n, "existing": m}.
    """
    chunk_ids = list(chunk_ids)
    if not chunk_ids:
        return {"new": 0, "existing": 0}

    matrix = _normalized_float32(vectors)
    if len(matrix) != len(chunk_ids):
        raise ValueError("chunk_ids ve vectors sayısı farklı.")
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("chunk_ids içinde tekrar var.")
    dimension = matrix.shape[1]

    # Aynı model için bütün vektörler aynı boyutta olmalı.
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT dimension FROM chunk_embeddings WHERE embedding_model = %s",
        (embedding_model,),
    )
    stored_dimensions = {row[0] for row in cursor.fetchall()}
    if stored_dimensions - {dimension}:
        raise ValueError(
            f"{embedding_model} için kayıtlı boyut {stored_dimensions}, "
            f"yeni boyut {dimension}."
        )

    existing = get_existing_embedding_ids(conn, chunk_ids, embedding_model)
    rows = [
        (chunk_id, embedding_model, dimension, VECTOR_DTYPE, vector.tobytes())
        for chunk_id, vector in zip(chunk_ids, matrix)
        if chunk_id not in existing
    ]

    if rows:
        cursor.executemany(
            """
            INSERT INTO chunk_embeddings
                (chunk_id, embedding_model, dimension, vector_dtype, vector)
            VALUES (%s, %s, %s, %s, %s)
            """,
            rows,
        )
    cursor.close()

    return {"new": len(rows), "existing": len(chunk_ids) - len(rows)}


# ---------------------------------------------------------------------------
# Okuma: RAG için chunk + embedding
# ---------------------------------------------------------------------------

def load_chunks_for_rag(embedding_model="minilm", processing_version=None,
                        chunk_size=None, chunk_overlap=None, connection=None):
    """
    RAG için chunk listesi döndürür. Her eleman src/chunking.py'nin ürettiği
    alanların tamamını ve ek olarak 'embedding' (np.float32) ile
    'embedding_model' alanlarını içerir.

    - Yalnızca durum = 'tamamlandi' olan belgeler.
    - processing_version verilmezse her belge için pdf_belgeleri'deki
      (en son başarılı işlenen) sürüm kullanılır.
    - Bir belgede aynı sürümde birden fazla chunk_size/overlap varsa ve
      parametrelerle seçilmemişse hata verilir (sürümler karışmaz).
    - Seçilen chunk'lardan birinin bu model için embedding'i yoksa hata
      verilir (eksik küme sessizce döndürülmez).
    """
    own_connection = connection is None
    if own_connection:
        connection = get_connection()

    try:
        cursor = connection.cursor()
        sql = """
            SELECT b.document_id, b.filename,
                   c.chunk_id, c.page_number, c.chunk_index, c.text,
                   c.text_sha256, c.char_count, c.processing_version,
                   c.chunk_size, c.chunk_overlap,
                   e.embedding_model, e.dimension, e.vector_dtype, e.vector
            FROM pdf_belgeleri AS b
            JOIN pdf_chunks AS c
              ON c.document_id = b.document_id
            LEFT JOIN chunk_embeddings AS e
              ON e.chunk_id = c.chunk_id AND e.embedding_model = %s
            WHERE b.durum = 'tamamlandi'
              AND c.processing_version = COALESCE(%s, b.processing_version)
        """
        params = [embedding_model, processing_version]
        if chunk_size is not None:
            sql += " AND c.chunk_size = %s"
            params.append(chunk_size)
        if chunk_overlap is not None:
            sql += " AND c.chunk_overlap = %s"
            params.append(chunk_overlap)
        sql += " ORDER BY b.filename, c.page_number, c.chunk_index"

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        if own_connection:
            connection.close()

    configurations = {}
    positions = set()
    missing_embeddings = []
    dimensions = set()
    chunks = []

    for (document_id, filename, chunk_id, page_number, chunk_index, text,
         text_sha256, char_count, version, size, overlap,
         model, dimension, dtype, blob) in rows:
        document_id = _as_text(document_id)
        chunk_id = _as_text(chunk_id)
        text = _as_text(text)

        configurations.setdefault(document_id, set()).add((size, overlap))

        position = (document_id, page_number, chunk_index)
        if position in positions:
            raise ValueError(
                f"Aynı belge/sayfa/sıra için birden fazla chunk: {position}"
            )
        positions.add(position)

        if blob is None:
            missing_embeddings.append(chunk_id)
            continue
        if _as_text(dtype) != VECTOR_DTYPE:
            raise ValueError(f"Desteklenmeyen vector_dtype: {dtype}")

        vector = np.frombuffer(bytes(blob), dtype=_NUMPY_VECTOR_DTYPE)
        if len(vector) != dimension:
            raise ValueError(f"Vektör uzunluğu dimension ile uyuşmuyor: {chunk_id}")
        dimensions.add(dimension)

        if hashlib.sha256(text.encode("utf-8")).hexdigest() != _as_text(text_sha256):
            raise ValueError(f"text_sha256 uyuşmuyor: {chunk_id}")

        chunks.append({
            "chunk_id": chunk_id,
            "document_id": document_id,
            "filename": _as_text(filename),
            "processing_version": _as_text(version),
            "chunk_size": size,
            "chunk_overlap": overlap,
            "text_sha256": _as_text(text_sha256),
            "page_number": page_number,
            "chunk_index": chunk_index,
            "text": text,
            "char_count": char_count,
            "embedding": vector.astype(np.float32),
            "embedding_model": _as_text(model),
        })

    mixed = {d: c for d, c in configurations.items() if len(c) > 1}
    if mixed:
        raise ValueError(
            "Aynı processing_version içinde birden fazla chunk_size/overlap var; "
            f"chunk_size ve chunk_overlap verin: {mixed}"
        )
    if missing_embeddings:
        raise ValueError(
            f"{len(missing_embeddings)} chunk için '{embedding_model}' "
            "embedding'i yok; önce python -m src.ingest çalıştırın."
        )
    if len(dimensions) > 1:
        raise ValueError(f"'{embedding_model}' için farklı boyutlar: {dimensions}")

    return chunks


# ---------------------------------------------------------------------------
# RAG sorgu kaydı: answer_question sonucu -> rag_sorgulari + rag_sorgu_kaynaklari
# ---------------------------------------------------------------------------

def _optional_float(value):
    """numpy/float/None -> float veya None (DOUBLE sütunlar için)."""
    return None if value is None else float(value)


def _rag_source_rows(result):
    """
    debug'daki aday chunk'lardan rag_sorgu_kaynaklari satırları üretir.

    - retrieval_score: debug.retrieved_hits[*].rrf_score
    - rerank_score   : debug.reranked_hits[*].rerank_score
    - source_rank    : yeniden sıralama sonrası sıra (1'den başlar); reranker'dan
                       geçmemiş aday varsa retrieval sırası ile onların arkasına eklenir.
    - used_in_context: chunk, sources (LLM bağlamına giren kaynaklar) içindeyse 1.
    Aynı chunk birden fazla gelirse tek satır (ilk görülen sıra korunur).
    """
    debug = result.get("debug") or {}
    retrieved = debug.get("retrieved_hits") or []
    reranked = debug.get("reranked_hits") or []
    sources = result.get("sources") or []

    retrieval_scores, rerank_scores, order = {}, {}, []

    for hit in reranked:
        chunk_id = hit["chunk_id"]
        rerank_scores.setdefault(chunk_id, hit.get("rerank_score"))
        if chunk_id not in order:
            order.append(chunk_id)
    for hit in retrieved:
        chunk_id = hit["chunk_id"]
        retrieval_scores.setdefault(chunk_id, hit.get("rrf_score"))
        if chunk_id not in order:
            order.append(chunk_id)

    # Bağlama giren ama debug'da görünmeyen kaynak (beklenmez) yine kaydedilir.
    for source in sources:
        chunk_id = source["chunk_id"]
        retrieval_scores.setdefault(chunk_id, source.get("rrf_score"))
        rerank_scores.setdefault(chunk_id, source.get("rerank_score"))
        if chunk_id not in order:
            order.append(chunk_id)

    used = {source["chunk_id"] for source in sources}
    return [
        {
            "chunk_id": chunk_id,
            "retrieval_score": _optional_float(retrieval_scores.get(chunk_id)),
            "rerank_score": _optional_float(rerank_scores.get(chunk_id)),
            "source_rank": rank,
            "used_in_context": chunk_id in used,
        }
        for rank, chunk_id in enumerate(order, start=1)
    ]


def save_rag_result(result, question, llm_model=None, embedding_model=None,
                    reranker_model=None, connection=None):
    """
    src.rag.answer_question sonucunu rag_sorgulari ve rag_sorgu_kaynaklari
    tablolarına kaydeder; yeni query_id'yi (UUID4) döndürür.

    Kullanım (Sinem için):

        from src.database import load_chunks_for_rag, save_rag_result
        from src.rag import answer_question, DEFAULT_LLM

        chunks = load_chunks_for_rag("minilm")
        result = answer_question(question, chunks, embedding_model="minilm")
        query_id = save_rag_result(result, question, llm_model=DEFAULT_LLM)

    Kurallar:
    - Bütün aday chunk'lar pdf_chunks'ta olmalı; biri bile yoksa hiçbir şey
      yazılmadan ValueError.
    - Tek transaction. connection verilmezse kendi bağlantısını açar ve
      commit eder. connection verilirse COMMIT ETMEZ (çağıran yönetir); yazımı
      bir SAVEPOINT içinde yapar, hata olursa yalnızca kendi yazdıklarını
      geri alıp hatayı yükseltir. Bu durumda bağlantı autocommit kapalı
      olmalı ya da açık bir transaction içinde olmalıdır.
    - Model adları önce sonuçtan, yoksa parametrelerden alınır; ikisi de
      yoksa NULL. answer_question sonucu LLM model adını içermez, bu yüzden
      llm_model parametreyle verilmelidir. LLM kapalıysa (llm_enabled=False)
      llm_model NULL yazılır.
    """
    if not question or not str(question).strip():
        raise ValueError("Soru boş olamaz.")

    evidence = result.get("evidence_check") or {}
    llm_enabled = result.get("llm_enabled", True)
    query_row = {
        "query_id": str(uuid.uuid4()),
        "question": question,
        "answer": result.get("answer"),
        "llm_model": (result.get("llm_model") or llm_model) if llm_enabled else None,
        "embedding_model": result.get("embedding_model") or embedding_model,
        "reranker_model": result.get("reranker_model") or reranker_model,
        "evidence_status": "sufficient" if evidence.get("sufficient") else "insufficient",
        "evidence_score": _optional_float(result.get("top_rerank_score")),
        "guard_reason": evidence.get("reason"),
        "latency_ms": int(round(float(result.get("response_time_seconds") or 0) * 1000)),
        "rerank_method": result.get("rerank_method"),
        "citation_check": (json.dumps(result["citation_check"], ensure_ascii=False)
                           if result.get("citation_check") is not None else None),
    }
    if not query_row["embedding_model"]:
        # rag_sorgulari.embedding_model NOT NULL
        raise ValueError("embedding_model sonuçta yok ve parametre olarak verilmedi.")

    source_rows = _rag_source_rows(result)

    own_connection = connection is None
    conn = get_connection() if own_connection else connection
    if not own_connection and conn.autocommit and not conn.in_transaction:
        raise ValueError(
            "Verilen bağlantı autocommit modunda ve açık transaction yok; "
            "yazım atomik olamaz. autocommit=False kullanın veya start_transaction() çağırın."
        )

    cursor = conn.cursor()
    try:
        # FK ön kontrolü: hiçbir şey yazmadan önce.
        chunk_ids = [row["chunk_id"] for row in source_rows]
        found = _existing_ids(conn, "pdf_chunks", chunk_ids)
        missing = [c for c in chunk_ids if c not in found]
        if missing:
            raise ValueError(
                f"{len(missing)} chunk_id pdf_chunks'ta yok; hiçbir şey yazılmadı: "
                f"{missing[:3]}"
            )

        if not own_connection:
            cursor.execute("SAVEPOINT save_rag_result")
        try:
            cursor.execute(
                """
                INSERT INTO rag_sorgulari
                    (query_id, question, answer, llm_model, embedding_model,
                     reranker_model, evidence_status, evidence_score,
                     guard_reason, latency_ms, rerank_method, citation_check)
                VALUES (%(query_id)s, %(question)s, %(answer)s, %(llm_model)s,
                        %(embedding_model)s, %(reranker_model)s, %(evidence_status)s,
                        %(evidence_score)s, %(guard_reason)s, %(latency_ms)s,
                        %(rerank_method)s, %(citation_check)s)
                """,
                query_row,
            )
            if source_rows:
                cursor.executemany(
                    """
                    INSERT INTO rag_sorgu_kaynaklari
                        (query_id, chunk_id, retrieval_score, rerank_score,
                         source_rank, used_in_context)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (query_row["query_id"], r["chunk_id"], r["retrieval_score"],
                         r["rerank_score"], r["source_rank"], r["used_in_context"])
                        for r in source_rows
                    ],
                )
        except Exception:
            if own_connection:
                conn.rollback()
            else:
                cursor.execute("ROLLBACK TO SAVEPOINT save_rag_result")
            raise

        if own_connection:
            conn.commit()
        else:
            cursor.execute("RELEASE SAVEPOINT save_rag_result")
        return query_row["query_id"]

    finally:
        cursor.close()
        if own_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Klinik bulgu kaydı: doğrulanmış satırlar -> klinik_bulgular,
# reddedilenler -> halusinasyon_degerlendirme
# ---------------------------------------------------------------------------

CIKARMA_YONTEMLERI = ("rag", "referans_taslak", "manuel")
CLINICAL_VALIDATION_VERSION = "clinical_validation-v1"

KLINIK_INSERT_COLUMNS = (
    "makale_id", "document_id", "chunk_id", "study_name", "hasta_grubu", "hasta_sayisi",
    "mudahale_tr", "mudahale_en", "mudahale_turu", "comparator", "doz", "uygulama_yolu",
    "sure", "sonuc_olcutu_tr", "outcome_type", "etki_turu", "deger", "unit", "ci95", "p",
    "yon", "kanit_sayfa", "kanit_cumle", "review_status", "tani_tr", "tani_en", "icd10",
    "kanit_turu", "kanit_sayfa_bitis", "birlestirme", "cikarma_yontemi", "taslak_notu",
    "query_id", "satir_hash",
)
# satir_hash'e girmeyenler: kimliği belirlemeyen / ortamdan gelen alanlar.
_HASH_EXCLUDED = {"chunk_id", "query_id", "satir_hash", "review_status", "makale_id", "taslak_notu"}


def _clean(value):
    """Boş string -> None; diğerleri olduğu gibi."""
    return None if value is None or (isinstance(value, str) and not value.strip()) else value


def _optional_int(value):
    value = _clean(value)
    return None if value is None else int(value)


def clinical_row_hash(row, cikarma_yontemi):
    """Kanıt + alanların SHA-256'sı (tekrar yazımda çoğaltmayı önler)."""
    payload = {
        column: "" if _clean(row.get(column)) is None else str(row.get(column))
        for column in KLINIK_INSERT_COLUMNS if column not in _HASH_EXCLUDED
    }
    payload["cikarma_yontemi"] = cikarma_yontemi
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _claim_text(row):
    if _clean(row.get("claim_text")):
        return row["claim_text"]
    parts = [row.get(k) for k in ("mudahale_tr", "doz", "hasta_grubu", "sonuc_olcutu_tr", "deger")]
    return " | ".join(str(p) for p in parts if _clean(p)) or None


def _transaction_scope(connection, savepoint):
    """save_* fonksiyonları için bağlantı/SAVEPOINT yönetimi (bağlam yöneticisi)."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        own = connection is None
        conn = get_connection() if own else connection
        if not own and conn.autocommit and not conn.in_transaction:
            raise ValueError(
                "Verilen bağlantı autocommit modunda ve açık transaction yok; "
                "yazım atomik olamaz. autocommit=False kullanın veya start_transaction() çağırın."
            )
        cursor = conn.cursor()
        try:
            if not own:
                cursor.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn, cursor
            except Exception:
                if own:
                    conn.rollback()
                else:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                raise
            if own:
                conn.commit()
            else:
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            cursor.close()
            if own:
                conn.close()

    return scope()


def save_clinical_findings(rows, cikarma_yontemi, query_id=None, connection=None):
    """
    Klinik bulgu satırlarını denetleyip yazar.

    rows: klinik_bulgular sütun adlarıyla sözlükler (en az document_id,
          kanit_sayfa, kanit_cumle, mudahale_turu). İsteğe bağlı 'claim_text'
          (LLM'in iddiası) reddedilenlerde kullanılır.
    cikarma_yontemi: 'rag' | 'referans_taslak' | 'manuel' ('verified' vb. -> hata).

    - Her satır src.clinical_validation.validate_row'dan geçer.
    - Geçenler klinik_bulgular'a review_status='pending' ile yazılır; aynı
      satir_hash zaten varsa atlanır (çoğaltma yok). chunk_id verilmemişse
      kanıtın geçtiği güncel chunk'tan doldurulur.
    - Geçemeyenler döndürülür; query_id verildiyse ayrıca
      halusinasyon_degerlendirme'ye yazılır (kayit_turu='klinik_cikarim',
      evaluation_method='automated', review_status='pending', ham_cikti=satır).
    - review_status='verified' ile gelen satır kabul edilmez (otomatik satır
      doğrudan doğrulanmış olamaz); hiçbir şey yazılmadan ValueError.
    - Bağlantı davranışı save_rag_result ile aynı (verilirse COMMIT yok, SAVEPOINT).

    Döndürür: {"yazilan", "zaten_var", "reddedilen": [{"satir", "hata_turu",
               "aciklama"}], "halusinasyon_kaydi"}
    """
    from src.clinical_validation import find_chunk_id, load_validation_context, validate_row

    if cikarma_yontemi not in CIKARMA_YONTEMLERI:
        raise ValueError(f"cikarma_yontemi {CIKARMA_YONTEMLERI} içinden olmalı: '{cikarma_yontemi}'")
    rows = [dict(r) for r in rows]
    for row in rows:
        if _clean(row.get("review_status")) not in (None, "pending"):
            raise ValueError("Satırlar doğrudan 'verified' (ya da pending dışı) yazılamaz.")

    with _transaction_scope(connection, "save_clinical_findings") as (conn, cursor):
        if query_id is not None:
            cursor.execute("SELECT COUNT(*) FROM rag_sorgulari WHERE query_id = %s", (query_id,))
            if cursor.fetchone()[0] == 0:
                raise ValueError(f"query_id rag_sorgulari'nda yok: {query_id}")

        ctx = load_validation_context(conn)
        max_lengths = _column_max_lengths(cursor, "klinik_bulgular")
        accepted, rejected, seen = [], [], set()
        for row in rows:
            ok, hata_turu, aciklama = validate_row(row, ctx)
            if ok:
                too_long = [c for c in KLINIK_INSERT_COLUMNS
                            if isinstance(row.get(c), str) and max_lengths.get(c)
                            and len(row[c]) > max_lengths[c]]
                if too_long:
                    ok, hata_turu = False, "deger_kumesi_disi"
                    aciklama = f"sütun sınırını aşan alan: {too_long}"
            if not ok:
                rejected.append({"satir": row, "hata_turu": hata_turu, "aciklama": aciklama})
                continue
            record = {column: _clean(row.get(column)) for column in KLINIK_INSERT_COLUMNS}
            record.update(
                makale_id=record["makale_id"] or ctx.makale_by_doc.get(row["document_id"]),
                chunk_id=record["chunk_id"] or find_chunk_id(row, ctx),
                hasta_sayisi=_optional_int(row.get("hasta_sayisi")),
                kanit_sayfa=int(row["kanit_sayfa"]),
                kanit_sayfa_bitis=_optional_int(row.get("kanit_sayfa_bitis")),
                review_status="pending",
                cikarma_yontemi=cikarma_yontemi,
                query_id=query_id,
                satir_hash=clinical_row_hash(row, cikarma_yontemi),
            )
            if record["satir_hash"] not in seen:
                seen.add(record["satir_hash"])
                accepted.append(record)

        existing = set()
        if accepted:
            hashes = [r["satir_hash"] for r in accepted]
            placeholders = ", ".join(["%s"] * len(hashes))
            cursor.execute(f"SELECT satir_hash FROM klinik_bulgular WHERE satir_hash IN ({placeholders})",
                           hashes)
            existing = {_as_text(r[0]) for r in cursor.fetchall()}
        new_rows = [r for r in accepted if r["satir_hash"] not in existing]

        if new_rows:
            columns = ", ".join(f"`{c}`" for c in KLINIK_INSERT_COLUMNS)
            values = ", ".join(["%s"] * len(KLINIK_INSERT_COLUMNS))
            cursor.executemany(
                f"INSERT INTO klinik_bulgular ({columns}) VALUES ({values})",
                [tuple(r[c] for c in KLINIK_INSERT_COLUMNS) for r in new_rows],
            )

        logged = 0
        if query_id is not None and rejected:
            cursor.executemany(
                """
                INSERT INTO halusinasyon_degerlendirme
                    (query_id, kayit_turu, soru_tipi, claim_text, supporting_chunk_id,
                     hata_turu, evaluation_method, review_status, evaluation_version,
                     aciklama, ham_cikti)
                VALUES (%s, 'klinik_cikarim', NULL, %s, %s, %s, 'automated', 'pending',
                        %s, %s, %s)
                """,
                [
                    (query_id, _claim_text(r["satir"]),
                     r["satir"].get("chunk_id") if r["satir"].get("chunk_id") in ctx.chunk_ids else None,
                     r["hata_turu"], CLINICAL_VALIDATION_VERSION, r["aciklama"],
                     json.dumps(r["satir"], ensure_ascii=False, default=str))
                    for r in rejected
                ],
            )
            logged = len(rejected)

    return {
        "yazilan": len(new_rows),
        "zaten_var": len(accepted) - len(new_rows),
        "reddedilen": rejected,
        "halusinasyon_kaydi": logged,
    }


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
