
import re


REFERENCE_HEADINGS = {
    "references",
    "bibliography",
    "works cited",
    "literature cited",
    "kaynakça",
    "kaynaklar",
}

# Kaynakça listesinin bittiğini gösteren satır başlangıçları
# (markdown işaretleri temizlendikten sonra).
REFERENCE_END_PATTERN = re.compile(
    r"^(table|figure|box|appendix|supplementary)\b",
    re.IGNORECASE,
)


def strip_markdown(line):
    """Satırdaki markdown işaretlerini (#, *, _, >) temizler."""
    return re.sub(r"[#*_>]", "", line).strip()


def normalize_heading(line):
    """Olası kaynakça başlığını normalleştirir."""
    heading = strip_markdown(line).lower()

    heading = re.sub(
        r"^\d+(?:\.\d+)*[.)]?\s+",
        "",
        heading,
    )

    heading = heading.rstrip(":").strip()

    return heading


def is_reference_end(line):
    """Satır, kaynakçadan sonra gelen yeni bir içerik mi?"""
    if line.lstrip().startswith("|"):
        return True

    return bool(REFERENCE_END_PATTERN.match(strip_markdown(line)))


def find_reference_span(text, min_position_ratio=0.3):
    """
    Kaynakça listesinin (başlangıç, bitiş) karakter konumlarını
    döndürür. Bulunamazsa None döner.

    Başlangıç: kaynakça başlığının bulunduğu satır.
    Bitiş: Table/Figure/Box/Appendix/Supplementary ile başlayan
    ya da markdown tablosu olan ilk satır; yoksa metnin sonu.
    """
    if not text:
        return None

    lines = text.splitlines(keepends=True)
    total_length = max(len(text), 1)
    position = 0
    start = None

    for line in lines:
        if start is None:
            if (
                normalize_heading(line) in REFERENCE_HEADINGS
                and position / total_length >= min_position_ratio
            ):
                start = position

        elif is_reference_end(line):
            return start, position

        position += len(line)

    if start is None:
        return None

    return start, len(text)


def remove_references(text, min_position_ratio=0.3):
    """
    Metindeki kaynakça listesini kaldırır.
    Kaynakçadan sonra gelen tablo/şekil gibi içerik korunur.
    """
    if not text or not text.strip():
        return ""

    span = find_reference_span(text, min_position_ratio)

    if span is None:
        return text.strip()

    start, end = span

    return (text[:start] + text[end:]).strip()


def remove_document_references(document, min_position_ratio=0.3):
    """
    PDF'nin tamamındaki kaynakça listesini bulur ve kaldırır.
    Sayfa numaraları korunur; tamamen boşalan sayfalar çıkarılır.
    """
    pages = document["pages"]

    full_text = "\n".join(
        page["text"] for page in pages
    )

    span = find_reference_span(full_text, min_position_ratio)

    if span is None:
        return document

    ref_start, ref_end = span

    new_pages = []
    position = 0

    for page in pages:
        page_text = page["text"]
        page_start = position
        page_end = position + len(page_text)

        # Sayfanın kaynakça aralığına düşmeyen kısımlarını tut.
        before = page_text[:max(0, ref_start - page_start)]
        after = page_text[max(0, ref_end - page_start):]

        if ref_start >= page_end or ref_end <= page_start:
            retained_text = page_text
        else:
            retained_text = before + after

        retained_text = retained_text.strip()

        if retained_text:
            new_pages.append({
                **page,
                "text": retained_text,
            })

        position = page_end + 1

    return {
        **document,
        "pages": new_pages,
    }
