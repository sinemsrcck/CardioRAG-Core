
"""
PDF -> MySQL aktarım testi.

Çalıştırma:
    python -m evaluation.test_ingestion

src.ingest'i (minilm) iki kez çalıştırır ve veritabanı içeriğini,
src.chunking'in o anki çıktısıyla ve yeniden hesaplanan embedding'lerle
karşılaştırır. Gereken: .env ve kurulmuş şema.
"""

import random
import sys

import numpy as np

from src.chunking import CHUNKING_VERSION, chunk_document
from src.database import (
    CORE_TABLE_COLUMNS,
    count_professor_rows,
    count_rows,
    get_connection,
    load_chunks_for_rag,
    professor_row_label,
)
from src.embeddings import embed_documents
from src.ingest import run_ingest
from src.pdf_loader import PDF_DIR, load_pdf
from src.pdf_processing import remove_document_references


EMBEDDING_MODEL = "minilm"
EXPECTED_DIMENSION = 384
EXPECTED_DOCUMENTS = 6
EXPECTED_PAGES = 95
EXPECTED_CHUNKS = 243  # chunking-table-v1 (önceki sürümde 233)

# Güncel sürüm = pdf_belgeleri.processing_version ile eşleşen satırlar.
# Eski sürümlerin chunk/embedding'leri silinmez; yalnızca kullanılmaz.
CURRENT_CHUNKS_SQL = (
    "FROM pdf_chunks AS c JOIN pdf_belgeleri AS b "
    "ON b.document_id = c.document_id AND b.processing_version = c.processing_version"
)

# Hocaya ait satır sayıları (makaleler: yalnızca document_id IS NULL).
EXPECTED_PROTECTED_ROWS = {
    "makaleler": 5,
    "tani_tedavi": 392,
    "sonuclar": 632,
    "semptom_iliskileri": 18,
    "icd10_who": 43,
    "tkd_kurallari": 15,
}

# Dışa dönük chunk alanları: chunking.py çıktısı + embedding alanları.
RAG_FIELDS = {
    "chunk_id", "document_id", "filename", "processing_version", "chunk_size",
    "chunk_overlap", "text_sha256", "page_number", "chunk_index", "text",
    "char_count", "embedding", "embedding_model",
}


def table_counts(cursor):
    """Yeni tablolar + makaleler toplamı: gerçek satır; hoca tabloları: hocanın satırları."""
    counts = {table: count_rows(cursor, table) for table in CORE_TABLE_COLUMNS}
    counts["makaleler (toplam)"] = count_rows(cursor, "makaleler")
    for table in EXPECTED_PROTECTED_ROWS:
        counts[table] = count_professor_rows(cursor, table)
    return counts


def fresh_chunks():
    """src.chunking'in şu anki çıktısı (veritabanından bağımsız)."""
    documents = [load_pdf(p) for p in sorted(PDF_DIR.glob("*.pdf"))]
    chunks = []
    for document in documents:
        chunks += chunk_document(remove_document_references(document))
    return documents, chunks


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    # 1) İki kez çalıştır.
    run1 = run_ingest(EMBEDDING_MODEL, verbose=False)
    conn = get_connection()
    cursor = conn.cursor()
    counts_after_1 = table_counts(cursor)
    conn.commit()

    run2 = run_ingest(EMBEDDING_MODEL, verbose=False)
    conn.commit()  # yeni anlık görüntü (REPEATABLE READ)
    counts_after_2 = table_counts(cursor)

    check("ingest #1: hatasız PDF", EXPECTED_DOCUMENTS,
          sum(r["status"] == "tamamlandi" for r in run1))
    check("ingest #2: hatasız PDF", EXPECTED_DOCUMENTS,
          sum(r["status"] == "tamamlandi" for r in run2))
    check("ingest #2: yeni chunk", 0, sum(r.get("chunks_new", 0) for r in run2))
    check("ingest #2: yeni embedding", 0, sum(r.get("embeddings_new", 0) for r in run2))

    grown = {t: (counts_after_1[t], counts_after_2[t])
             for t in counts_after_1 if counts_after_2[t] != counts_after_1[t]}
    check("ingest #2: satır sayısı değişimi", "yok", grown or "yok")

    # 2) pdf_belgeleri.
    cursor.execute(
        "SELECT durum, COUNT(*), SUM(islenen_sayfa_sayisi = page_count) "
        "FROM pdf_belgeleri GROUP BY durum"
    )
    by_status = {row[0]: (row[1], int(row[2])) for row in cursor.fetchall()}
    check("pdf_belgeleri: satır", EXPECTED_DOCUMENTS, counts_after_2["pdf_belgeleri"])
    check("pdf_belgeleri: tamamlandi", EXPECTED_DOCUMENTS,
          by_status.get("tamamlandi", (0, 0))[0])
    check("pdf_belgeleri: islenen = page_count", EXPECTED_DOCUMENTS,
          by_status.get("tamamlandi", (0, 0))[1])
    cursor.execute("SELECT COUNT(*) FROM pdf_belgeleri WHERE processing_version LIKE %s",
                   ("%|" + CHUNKING_VERSION,))
    check(f"pdf_belgeleri sürümü '|{CHUNKING_VERSION}' ile bitiyor", EXPECTED_DOCUMENTS,
          cursor.fetchone()[0])

    # 3) pdf_isleme_durumu: her PDF'in her sayfası.
    documents, chunks = fresh_chunks()
    page_total = sum(d["page_count"] for d in documents)
    check("PDF sayfa toplamı", EXPECTED_PAGES, page_total)
    check("pdf_isleme_durumu: satır", EXPECTED_PAGES, counts_after_2["pdf_isleme_durumu"])

    incomplete = []
    for document in documents:
        cursor.execute(
            "SELECT page_number FROM pdf_isleme_durumu WHERE document_id = %s "
            "ORDER BY page_number",
            (document["document_id"],),
        )
        pages = [row[0] for row in cursor.fetchall()]
        if pages != list(range(1, document["page_count"] + 1)):
            incomplete.append(document["filename"])
    check("her PDF'in her sayfası var", "hepsi", ", ".join(incomplete) or "hepsi")

    cursor.execute("SELECT durum, COUNT(*) FROM pdf_isleme_durumu GROUP BY durum ORDER BY durum")
    distribution = ", ".join(f"{d}={n}" for d, n in cursor.fetchall())
    check("sayfa durumları (bilgi)", "-", distribution, True)

    # 4) pdf_chunks (güncel sürüm): sayı ve chunk_id kümesi chunking çıktısıyla aynı.
    fresh_ids = {c["chunk_id"] for c in chunks}
    cursor.execute("SELECT c.chunk_id " + CURRENT_CHUNKS_SQL)
    stored_ids = {bytes(r[0]).decode() if isinstance(r[0], (bytes, bytearray)) else r[0]
                  for r in cursor.fetchall()}
    check("chunk_document çıktısı", EXPECTED_CHUNKS, len(chunks))
    check("pdf_chunks (güncel sürüm): satır", EXPECTED_CHUNKS, len(stored_ids))
    check("pdf_chunks (güncel) = chunk_document id'leri", "aynı",
          "aynı" if stored_ids == fresh_ids
          else f"fark {len(stored_ids ^ fresh_ids)}")
    check("pdf_chunks toplam (eski sürümler dahil, bilgi)", "-",
          f"{counts_after_2['pdf_chunks']} (eski: {counts_after_2['pdf_chunks'] - len(stored_ids)})",
          True)

    # 5) chunk_embeddings (güncel sürüm).
    cursor.execute(
        "SELECT COUNT(*), MIN(e.dimension), MAX(e.dimension) "
        + CURRENT_CHUNKS_SQL.replace("FROM pdf_chunks AS c",
                                     "FROM chunk_embeddings AS e JOIN pdf_chunks AS c "
                                     "ON c.chunk_id = e.chunk_id")
        + " WHERE e.embedding_model = %s",
        (EMBEDDING_MODEL,),
    )
    emb_count, min_dim, max_dim = cursor.fetchone()
    check(f"chunk_embeddings ({EMBEDDING_MODEL}, güncel sürüm)", EXPECTED_CHUNKS, emb_count)
    check("embedding dimension", EXPECTED_DIMENSION,
          min_dim if min_dim == max_dim else f"{min_dim}..{max_dim}")
    check("chunk_embeddings toplam (eski sürümler dahil, bilgi)", "-",
          counts_after_2["chunk_embeddings"], True)

    # 6) Hoca tabloları.
    for table, expected in EXPECTED_PROTECTED_ROWS.items():
        check(f"{professor_row_label(table)}: satır", expected, counts_after_2[table])

    cursor.close()
    conn.close()

    # 7) load_chunks_for_rag.
    rag_chunks = load_chunks_for_rag(EMBEDDING_MODEL)
    check("load_chunks_for_rag: eleman", EXPECTED_CHUNKS, len(rag_chunks))
    check("load_chunks_for_rag: id'ler", "aynı",
          "aynı" if {c["chunk_id"] for c in rag_chunks} == fresh_ids else "farklı")

    missing_fields = set().union(*(RAG_FIELDS - set(c) for c in rag_chunks))
    check("alanlar (chunking + embedding)", "tam",
          "tam" if not missing_fields else f"eksik: {sorted(missing_fields)}")

    by_id = {c["chunk_id"]: c for c in chunks}
    differing = [
        c["chunk_id"] for c in rag_chunks
        if any(c[k] != by_id[c["chunk_id"]][k] for k in by_id[c["chunk_id"]])
    ]
    check("alan değerleri = chunk_document", 0, len(differing))

    embeddings = [c["embedding"] for c in rag_chunks]
    check("embedding dtype", "float32",
          ", ".join(sorted({str(e.dtype) for e in embeddings})))
    lengths = {len(e) for e in embeddings}
    check("embedding uzunluğu", EXPECTED_DIMENSION,
          ", ".join(str(n) for n in sorted(lengths)),
          lengths == {EXPECTED_DIMENSION})
    norms = np.array([np.linalg.norm(e) for e in embeddings])
    max_norm_error = float(np.max(np.abs(norms - 1)))
    check("embedding normu ≈ 1", "< 1e-3", f"{max_norm_error:.2e}",
          max_norm_error < 1e-3)

    # 8) Rastgele bir chunk'ı yeniden embed et ve SQL'deki ile karşılaştır.
    sample = random.choice(rag_chunks)
    recomputed = embed_documents([sample["text"]], model_name=EMBEDDING_MODEL)[0]
    cosine = float(
        np.dot(recomputed, sample["embedding"])
        / (np.linalg.norm(recomputed) * np.linalg.norm(sample["embedding"]))
    )
    check(f"yeniden embed cosine (p{sample['page_number']} c{sample['chunk_index']})",
          "> 0.999", f"{cosine:.6f}", cosine > 0.999)

    # Sonuç tablosu.
    width = max(len(r[0]) for r in results)
    print()
    print(f"{'Kontrol':<{width}}  {'Beklenen':<10}  {'Bulunan':<60}  Sonuç")
    print("-" * (width + 82))
    for name, expected, actual, passed in results:
        print(f"{name:<{width}}  {expected:<10}  {actual[:60]:<60}  "
              f"{'OK' if passed else 'HATA'}")

    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
