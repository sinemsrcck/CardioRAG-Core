
"""
MySQL bağlantısı ve şema kurulumu (kardiyo_tam_metin).

Bağlantı bilgileri proje kökündeki .env dosyasından okunur
(bkz. .env.example). Şifre koda yazılmaz.

Kullanım:
    python -m src.database --check   # salt okunur durum raporu
    python -m src.database --init    # şemayı kur (tekrar çalıştırılabilir)
"""

import hashlib
import os
import re
import sys
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

# Yeni tablolara sonradan eklenen sütunlar (ilk kurulumdan sonra gelenler).
# CREATE TABLE IF NOT EXISTS var olan tabloyu değiştirmediği için bunlar
# information_schema kontrolüyle eklenir. Yalnızca ekleme; silme yok.
CORE_COLUMN_MIGRATIONS = {
    "pdf_belgeleri": (
        ("hata_mesaji", "TEXT NULL AFTER islenen_sayfa_sayisi"),
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
