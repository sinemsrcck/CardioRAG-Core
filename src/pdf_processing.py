
import re


REFERENCE_HEADINGS = {
    "references",
    "reference",
    "bibliography",
    "works cited",
    "literature cited",
    "kaynakça",
    "kaynaklar",
    "referanslar",
}

POST_REFERENCE_HEADINGS = {
    "figures",
    "figure legends",
    "tables",
    "supplementary material",
    "supplementary information",
    "supplemental material",
    "appendix",
    "appendices",
    "acknowledgments",
    "acknowledgements",
    "author contributions",
    "frequently asked questions about atrial fibrillation (af)",
}


def normalize_heading(line):
    heading = re.sub(r"\s+", " ", line).strip().lower()

    heading = re.sub(
        r"^\d+(?:\.\d+)*[.)]?\s+",
        "",
        heading,
    )

    heading = re.sub(
        r"^[\s:.\-–—]+|[\s:.\-–—]+$",
        "",
        heading,
    )

    return heading


def is_reference_heading(line):
    return normalize_heading(line) in REFERENCE_HEADINGS


def is_post_reference_heading(line):
    heading = normalize_heading(line)

    if heading in POST_REFERENCE_HEADINGS:
        return True

    if re.match(
        r"^(figure|fig|table)\s*\.?\s*\d+\b",
        heading,
    ):
        return True

    if re.match(r"^box\s*[.:]", heading):
        return True

    return False


def find_reference_range(lines, min_position_ratio=0.35):
    """
    Kaynakça başlangıcını ve varsa bitişini bulur.

    Karakter oranı yerine satır konumu kullanılır.
    Böylece büyük tabloların metin uzunluğu
    tespiti daha az etkiler.
    """
    if not lines:
        return None

    candidates = []

    for index, line in enumerate(lines):
        if is_reference_heading(line):
            candidates.append(index)

    if not candidates:
        return None

    minimum_index = int(len(lines) * min_position_ratio)

    candidates = [
        index
        for index in candidates
        if index >= minimum_index
    ]

    if not candidates:
        return None

    reference_start = candidates[0]
    reference_end = len(lines)

    for index in range(reference_start + 1, len(lines)):
        if is_post_reference_heading(lines[index]):
            reference_end = index
            break

    if reference_end <= reference_start:
        return None

    return reference_start, reference_end


def remove_references(text, min_position_ratio=0.35):
    if not text or not text.strip():
        return ""

    lines = text.splitlines()

    reference_range = find_reference_range(
        lines,
        min_position_ratio=min_position_ratio,
    )

    if reference_range is None:
        return text.strip()

    start, end = reference_range

    retained_lines = lines[:start] + lines[end:]

    return "\n".join(retained_lines).strip()


def remove_document_references(document):
    """
    Kaynakça satırlarını çıkarır.
    Orijinal PDF sayfa numaralarını korur.
    Kaynakça sonrasındaki bölümleri saklar.
    """
    pages = document.get("pages", [])

    if not pages:
        return document

    indexed_lines = []

    for page in pages:
        for line in page["text"].splitlines():
            indexed_lines.append({
                "page_number": page["page_number"],
                "line": line,
            })

    lines = [item["line"] for item in indexed_lines]

    reference_range = find_reference_range(
        lines,
        min_position_ratio=0.35,
    )

    filename = document.get("filename", "PDF")

    if reference_range is None:
        print(
            f"[REFERENCE CLEANER] {filename} | "
            "Kaynakça başlığı tespit edilemedi; "
            "metin değiştirilmedi."
        )
        return document

    start, end = reference_range

    retained_lines = indexed_lines[:start] + indexed_lines[end:]

    lines_by_page = {
        page["page_number"]: []
        for page in pages
    }

    for item in retained_lines:
        lines_by_page[item["page_number"]].append(
            item["line"]
        )

    new_pages = []

    for page in pages:
        page_number = page["page_number"]

        cleaned_text = "\n".join(
            lines_by_page[page_number]
        ).strip()

        if cleaned_text:
            new_pages.append({
                **page,
                "text": cleaned_text,
            })

    removed_pages = sorted(
        set(page["page_number"] for page in pages)
        - set(page["page_number"] for page in new_pages)
    )

    print(
        f"[REFERENCE CLEANER] {filename} | "
        f"Çıkarılan satır: {end - start} | "
        f"Çıkarılan sayfalar: {removed_pages}"
    )

    return {
        **document,
        "pages": new_pages,
    }
