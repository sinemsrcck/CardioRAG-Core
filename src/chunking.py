
import re
import hashlib


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 250


def clean_text(text):
    """PDF'den çıkarılan metindeki gereksiz boşlukları temizler."""
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text, chunk_size=1500, overlap=250):
    """Metni örtüşmeli parçalara ayırır."""
    if chunk_size <= 0:
        raise ValueError("chunk_size pozitif olmalı.")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            "overlap, 0 ile chunk_size arasında olmalı."
        )

    text = clean_text(text)

    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))

        if end < len(text):
            boundary = text.rfind(" ", start, end)

            if boundary > start + chunk_size // 2:
                end = boundary

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = max(start + 1, end - overlap)

    return chunks


def chunk_document(document, chunk_size=1500, overlap=250):
    """
    PDF sayfalarını ayrı ayrı parçalar.
    Her chunk için belge ve sayfa bilgilerini korur.
    """
    document_id = document["document_id"]
    filename = document["filename"]
    # Legacy embeddings and evidence mappings must be regenerated, never relabeled.
    processing_version = document.get("processing_version", "legacy-unversioned")
    configuration = f"{processing_version}|chunk_size={chunk_size}|overlap={overlap}"
    processing_key = hashlib.sha256(configuration.encode("utf-8")).hexdigest()[:16]

    all_chunks = []

    for page in document["pages"]:
        page_number = page["page_number"]

        page_chunks = split_text(
            page["text"],
            chunk_size=chunk_size,
            overlap=overlap,
        )

        for chunk_index, text in enumerate(page_chunks):
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunk_id = (
                f"{document_id}_v{processing_key}_p{page_number}_c{chunk_index}_t{text_hash}"
            )

            all_chunks.append({
                "chunk_id": chunk_id,
                "document_id": document_id,
                "filename": filename,
                "processing_version": processing_version,
                "chunk_size": chunk_size,
                "chunk_overlap": overlap,
                "text_sha256": text_hash,
                "page_number": page_number,
                "chunk_index": chunk_index,
                "text": text,
                "char_count": len(text),
            })

    return all_chunks


def chunk_all_documents(
    documents,
    chunk_size=DEFAULT_CHUNK_SIZE,
    overlap=DEFAULT_CHUNK_OVERLAP,
):
    """Birden fazla PDF'yi parçalar."""
    all_chunks = []

    for document in documents:
        chunks = chunk_document(
            document,
            chunk_size=chunk_size,
            overlap=overlap,
        )

        all_chunks.extend(chunks)

        print(
            f"[CHUNK] {document['filename']} | "
            f"{len(chunks)} parça"
        )

    return all_chunks
