
import re


REFERENCE_HEADINGS = {
    "references",
    "bibliography",
    "works cited",
    "literature cited",
    "kaynakça",
    "kaynaklar",
}


def normalize_heading(line):
    """Olası kaynakça başlığını normalleştirir."""
    heading = line.strip().lower()

    heading = re.sub(
        r"^\d+(?:\.\d+)*[.)]?\s+",
        "",
        heading,
    )

    heading = heading.rstrip(":").strip()

    return heading


def remove_references(text, min_position_ratio=0.5):
    """
    PDF metninin son bölümünde bulunan
    kaynakçayı kaldırır.
    """
    if not text or not text.strip():
        return ""

    lines = text.splitlines()
    total_length = len(text)
    current_position = 0

    for index, line in enumerate(lines):
        heading = normalize_heading(line)

        position_ratio = (
            current_position / max(total_length, 1)
        )

        if (
            heading in REFERENCE_HEADINGS
            and position_ratio >= min_position_ratio
        ):
            return "\n".join(lines[:index]).strip()

        current_position += len(line) + 1

    return text.strip()


def remove_document_references(document):
    """
    PDF'nin tamamındaki kaynakça başlangıcını bulur.
    Sayfa numaralarını koruyarak temizler.
    """
    pages = document["pages"]

    full_text = "\n".join(
        page["text"] for page in pages
    )

    cleaned_text = remove_references(full_text)

    if len(cleaned_text) == len(full_text):
        return document

    # Temizlenmiş metnin bittiği karakter konumu.
    cutoff = len(cleaned_text)

    new_pages = []
    position = 0

    for page in pages:
        page_text = page["text"]
        page_end = position + len(page_text)

        if position >= cutoff:
            break

        retained_text = page_text[
            :max(0, cutoff - position)
        ].strip()

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
