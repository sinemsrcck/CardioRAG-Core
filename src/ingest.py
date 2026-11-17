
"""
PDF -> kaynakça temizleme -> chunk -> embedding -> MySQL.

Çalıştırma:
    python -m src.ingest                 # minilm
    python -m src.ingest --model bge-m3

Her PDF tek transaction içinde yazılır. Bir PDF'te hata olursa yalnızca o
PDF'in yazımı geri alınır, pdf_belgeleri'ye 'hatali' + hata mesajı yazılır
ve diğer PDF'ler işlenmeye devam eder. Tekrar çalıştırmak güvenlidir:
var olan chunk'lar ve (chunk_id, model) embedding'leri yeniden yazılmaz,
var olan embedding'ler yeniden hesaplanmaz.
"""

import argparse
import time
import traceback
from pathlib import Path

from src.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, chunk_document
from src.database import (
    get_connection,
    get_existing_embedding_ids,
    init_schema,
    mark_document_failed,
    save_chunks,
    save_document,
    save_embeddings,
)
from src.embeddings import DEFAULT_MODEL, EMBEDDING_MODELS, embed_documents
from src.pdf_loader import PDF_DIR, calculate_file_hash, load_pdf
from src.pdf_processing import remove_document_references


def ingest_pdf(conn, pdf_path, embedding_model, chunk_size, chunk_overlap):
    """Tek bir PDF'i işler ve tek transaction içinde yazar."""
    started = time.perf_counter()

    # 1) Okuma, temizleme, parçalama (veritabanına dokunmaz).
    raw = load_pdf(pdf_path)
    cleaned = remove_document_references(raw)
    chunks = chunk_document(cleaned, chunk_size=chunk_size, overlap=chunk_overlap)

    # 2) Yalnızca eksik embedding'leri hesapla (transaction dışında).
    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    existing = get_existing_embedding_ids(conn, chunk_ids, embedding_model)
    missing = [chunk for chunk in chunks if chunk["chunk_id"] not in existing]
    vectors = (
        embed_documents([c["text"] for c in missing], model_name=embedding_model)
        if missing else []
    )

    # 3) Yazım: tek transaction.
    conn.start_transaction()
    try:
        page_statuses = save_document(conn, raw, cleaned)
        chunk_result = save_chunks(conn, chunks)
        embedding_result = save_embeddings(
            conn, [c["chunk_id"] for c in missing], vectors, embedding_model,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "filename": raw["filename"],
        "status": "tamamlandi",
        "page_count": raw["page_count"],
        "page_statuses": page_statuses,
        "chunks": len(chunks),
        "chunks_new": chunk_result["new"],
        "chunks_existing": chunk_result["existing"],
        "embeddings_new": embedding_result["new"],
        "embeddings_existing": len(existing),
        "seconds": round(time.perf_counter() - started, 1),
    }


def _record_failure(conn, pdf_path, exc):
    """Hatayı ayrı bir transaction'da pdf_belgeleri'ye yazar."""
    message = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
    conn.start_transaction()
    try:
        mark_document_failed(
            conn,
            document_id=calculate_file_hash(pdf_path),
            filename=Path(pdf_path).name,
            error_message=message,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run_ingest(embedding_model=DEFAULT_MODEL, pdf_dir=PDF_DIR,
               chunk_size=DEFAULT_CHUNK_SIZE, chunk_overlap=DEFAULT_CHUNK_OVERLAP,
               verbose=True):
    """Klasördeki bütün PDF'leri işler; PDF başına özet listesi döndürür."""
    if embedding_model not in EMBEDDING_MODELS:
        raise ValueError(f"Bilinmeyen model: {embedding_model}")

    pdf_paths = sorted(Path(pdf_dir).glob("*.pdf"))
    if not pdf_paths:
        raise FileNotFoundError(f"PDF bulunamadı: {pdf_dir}")

    started = time.perf_counter()
    conn = get_connection()
    results = []

    try:
        init_schema(conn)
        # Okumalar anında görünür olsun; yazımlar start_transaction ile açılır.
        conn.autocommit = True

        for pdf_path in pdf_paths:
            try:
                result = ingest_pdf(
                    conn, pdf_path, embedding_model, chunk_size, chunk_overlap,
                )
            except Exception as exc:
                _record_failure(conn, pdf_path, exc)
                result = {"filename": pdf_path.name, "status": "hatali",
                          "error": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            if verbose:
                print(f"[{result['status'].upper()}] {pdf_path.name}")
    finally:
        conn.close()

    total_seconds = round(time.perf_counter() - started, 1)
    if verbose:
        print_summary(results, embedding_model, total_seconds)

    return results


def print_summary(results, embedding_model, total_seconds):
    print()
    print(f"Embedding modeli: {embedding_model}")
    header = (
        f"{'Dosya':<42} {'Durum':<10} {'Sayfa':>5}  {'Sayfa durumları':<46} "
        f"{'Chunk':>5} {'Yeni':>5} {'Var':>5}  {'Emb yeni':>8} {'Emb var':>7} {'Süre':>6}"
    )
    print(header)
    print("-" * len(header))

    totals = {"page_count": 0, "chunks": 0, "chunks_new": 0, "chunks_existing": 0,
              "embeddings_new": 0, "embeddings_existing": 0}

    for r in results:
        if r["status"] != "tamamlandi":
            print(f"{r['filename'][:42]:<42} {'HATALI':<10} {r['error'][:100]}")
            continue
        statuses = ", ".join(f"{k}={v}" for k, v in sorted(r["page_statuses"].items()))
        print(
            f"{r['filename'][:42]:<42} {r['status']:<10} {r['page_count']:>5}  "
            f"{statuses:<46} {r['chunks']:>5} {r['chunks_new']:>5} "
            f"{r['chunks_existing']:>5}  {r['embeddings_new']:>8} "
            f"{r['embeddings_existing']:>7} {r['seconds']:>5}s"
        )
        for key in totals:
            totals[key] += r[key]

    print("-" * len(header))
    failed = sum(r["status"] != "tamamlandi" for r in results)
    print(
        f"Toplam: {len(results)} PDF ({failed} hatalı), {totals['page_count']} sayfa, "
        f"{totals['chunks']} chunk (yeni {totals['chunks_new']}, var "
        f"{totals['chunks_existing']}), embedding yeni {totals['embeddings_new']}, "
        f"var {totals['embeddings_existing']}, süre {total_seconds}s"
    )


def main():
    parser = argparse.ArgumentParser(description="PDF'leri MySQL'e aktarır.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        choices=sorted(EMBEDDING_MODELS))
    parser.add_argument("--pdf-dir", default=str(PDF_DIR))
    args = parser.parse_args()

    run_ingest(embedding_model=args.model, pdf_dir=args.pdf_dir)


if __name__ == "__main__":
    main()
