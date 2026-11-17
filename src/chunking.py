
import re
import hashlib


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 250

# Chunk'lama algoritmasının sürümü. processing_version'a eklenir; böylece
# farklı algoritmayla üretilmiş chunk'lar (ve embedding'leri) karışmaz.
CHUNKING_VERSION = "chunking-table-v1"

# Tablonun hemen önündeki başlık satırı: "Table 2.", "**TABLE 1:**", "Table I:", "Box"
TABLE_CAPTION = re.compile(r"^[\W_]*(?:table|tablo|box)\s*[\dIVX]+", re.IGNORECASE)
TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
CONTINUATION_SUFFIX = " (devam)"
CONTINUATION_NO_CAPTION = "(tablo devamı)"


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


# ---------------------------------------------------------------------------
# Tablo farkındalıklı sayfa bölme
# ---------------------------------------------------------------------------

def versioned_document(document):
    """
    processing_version'un sonuna chunking sürümünü ekler (zaten varsa eklemez).
    ingest bunu pdf_belgeleri'ye yazar; chunk_document da aynısını uygular.
    """
    version = document.get("processing_version", "legacy-unversioned")
    suffix = "|" + CHUNKING_VERSION
    if not version.endswith(suffix):
        version += suffix
    return {**document, "processing_version": version}


def _is_table_line(line):
    return line.lstrip().startswith("|")


def _page_segments(text):
    """
    Temizlenmiş sayfa metnini sırayla parçalara ayırır:
      ("metin", str) ya da ("tablo", başlık_satırı, [tablo satırları]).
    Tablonun hemen önündeki boş olmayan satır Table/Box ile başlıyorsa başlık
    sayılır ve metinden alınıp tabloyla birlikte taşınır.
    """
    lines = text.split("\n")
    segments, prose, i = [], [], 0
    while i < len(lines):
        if not _is_table_line(lines[i]):
            prose.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and _is_table_line(lines[j]):
            j += 1
        caption = ""
        k = len(prose) - 1
        while k >= 0 and not prose[k].strip():
            k -= 1
        if k >= 0 and TABLE_CAPTION.match(prose[k].strip()):
            caption = prose[k].strip()
            prose = prose[:k]
        if "".join(prose).strip():
            segments.append(("metin", "\n".join(prose)))
        segments.append(("tablo", caption, [line.strip() for line in lines[i:j]]))
        prose, i = [], j
    if "".join(prose).strip():
        segments.append(("metin", "\n".join(prose)))
    return segments


def _split_long_row(row, budget):
    """Tek başına sığmayan tablo satırını hücre sınırlarından, gerekirse boşluktan böler."""
    pieces, current = [], ""
    for cell in row.strip("|").split("|"):
        candidate = f"{current}|{cell}" if current else f"|{cell}"
        if len(candidate) + 1 <= budget:
            current = candidate
            continue
        if current:
            pieces.append(current + "|")
        current = f"|{cell}"
        while len(current) + 1 > budget:  # tek hücre bile sığmıyor
            cut = current.rfind(" ", 0, budget - 1)
            if cut <= budget // 2:
                cut = budget - 1
            pieces.append(current[:cut])
            current = current[cut:].lstrip()
    if current:
        pieces.append(current if current.endswith("|") else current + "|")
    return pieces


def _table_chunks(caption, rows, chunk_size):
    """
    Tabloyu chunk'lara böler.
    - Başlık + tablo sığıyorsa tek chunk.
    - Sığmıyorsa yalnızca satır aralarından bölünür; her devam parçasının başına
      "<başlık> (devam)" (başlık yoksa "(tablo devamı)"), markdown başlık satırı
      ve ayraç satırı tekrar eklenir.
    - Tek satır sığmıyorsa hücre sınırından (gerekirse boşluktan) bölünür.
    - Başlık satırı chunk_size'ın yarısından uzunsa tekrar edilmez.
    """
    whole = "\n".join(([caption] if caption else []) + rows)
    if len(whole) <= chunk_size:
        return [whole]

    has_header = len(rows) > 1 and bool(TABLE_SEPARATOR.match(rows[1]))
    header = rows[:2] if has_header else []
    body = rows[2:] if has_header else rows
    if len("\n".join(header)) > chunk_size // 2:
        # Başlık tekrar edilemeyecek kadar uzun: hiçbir satır kaybolmasın diye
        # başlık satırı da gövdeye alınır (uzun satırlar aşağıda bölünür).
        # Tek başına içerik taşımayan ayraç satırı atlanır.
        header, body = [], [row for row in rows if not TABLE_SEPARATOR.match(row)]

    chunks, current = [], []

    continuation_label = caption + CONTINUATION_SUFFIX if caption else CONTINUATION_NO_CAPTION

    def prefix():
        label = caption if not chunks else continuation_label
        return [line for line in [label] + header if line]

    def flush():
        chunks.append("\n".join(prefix() + current))

    # Uzun satır parçaları için bütçe: en uzun önek (devam etiketi + başlık).
    long_row_budget = chunk_size - len("\n".join([continuation_label] + header)) - 1

    for row in body:
        if len("\n".join(prefix() + current + [row])) <= chunk_size:
            current.append(row)
            continue
        if current:
            flush()
            current = []
        if len("\n".join(prefix() + [row])) <= chunk_size:
            current.append(row)
            continue
        for piece in _split_long_row(row, long_row_budget):
            current = [piece]
            flush()
        current = []
    if current or not chunks:
        flush()
    return chunks


def split_page(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_CHUNK_OVERLAP):
    """
    Bir sayfayı chunk'lara böler.
    - Markdown tablosu yoksa split_text ile birebir aynı sonuç.
    - Tablo varsa: metin parçaları split_text ile (aynı algoritma ve örtüşme),
      her tablo _table_chunks ile ayrı bölünür; tablo ortasından kesilmez.
    """
    cleaned = clean_text(text)
    if not any(_is_table_line(line) for line in cleaned.split("\n")):
        return split_text(cleaned, chunk_size=chunk_size, overlap=overlap)

    chunks = []
    for segment in _page_segments(cleaned):
        if segment[0] == "metin":
            chunks += split_text(segment[1], chunk_size=chunk_size, overlap=overlap)
        else:
            chunks += _table_chunks(segment[1], segment[2], chunk_size)
    return [chunk for chunk in chunks if chunk.strip()]


def chunk_document(document, chunk_size=1500, overlap=250):
    """
    PDF sayfalarını ayrı ayrı parçalar (tablo farkındalıklı, bkz. split_page).
    Her chunk için belge ve sayfa bilgilerini korur. processing_version'a
    CHUNKING_VERSION eklenir (versioned_document).
    """
    document = versioned_document(document)
    document_id = document["document_id"]
    filename = document["filename"]
    # Legacy embeddings and evidence mappings must be regenerated, never relabeled.
    processing_version = document["processing_version"]
    configuration = f"{processing_version}|chunk_size={chunk_size}|overlap={overlap}"
    processing_key = hashlib.sha256(configuration.encode("utf-8")).hexdigest()[:16]

    all_chunks = []

    for page in document["pages"]:
        page_number = page["page_number"]

        page_chunks = split_page(
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
