"""Reference boundaries with explicit section structure, not keyword matches."""
import re

REFERENCE_PROCESSING_VERSION = "references-v2"
REFERENCE_HEADINGS = {
    "references", "reference", "bibliography", "works cited", "literature cited",
    "kaynak\u00e7a", "kaynaklar", "referanslar",
}
POST_REFERENCE_HEADINGS = {
    "tables", "figures", "figure legends", "table legends", "appendix", "appendices",
    "supplementary", "supplementary material", "supplementary information",
    "supplemental material", "acknowledgments", "acknowledgements",
    "author contributions",
}
CAPTION = re.compile(
    r"^(?:table|figure|fig\.|box)\s*(?:\d+[A-Za-z]?|[A-Z])(?:[.:]\s*|\s+|$)",
    re.IGNORECASE,
)
# Vancouver/MDPI reference entries, including a wrapped reference following a title.
NUMBERED_ENTRY = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s+\S")


def strip_markdown(line):
    return re.sub(r"[#*_>]", "", line).strip()


def normalize_heading(line):
    heading = re.sub(r"\s+", " ", strip_markdown(line)).casefold()
    heading = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", heading)
    return heading.strip(" :.\u2013\u2014-")


def _marked_heading(line):
    line = line.strip()
    return bool(re.match(r"^#{1,6}\s+", line) or re.fullmatch(r"\*\*[^*]+\*\*", line))


def is_reference_end(line):
    """A possible section caption; callers must also check surrounding structure."""
    plain = strip_markdown(line)
    if NUMBERED_ENTRY.match(plain) or plain.startswith("|"):
        return False
    heading = normalize_heading(line)
    return (heading in POST_REFERENCE_HEADINGS or bool(CAPTION.match(plain))
            or (_marked_heading(line) and bool(re.match(
                r"^(?:appendix|supplementary|supplemental)\b", plain, re.I)))
            or bool(re.match(r"^(?:box|appendix|supplementary|supplemental)\s*[.:]\s*\S", plain, re.I)))


def _section_start(lines, index):
    line = lines[index]
    if not is_reference_end(line):
        return False
    # A title embedded in a reference is not a section, even after a line wrap.
    # A pipe row can itself belong to a reference; it is not a boundary.
    # Stop only at the next structurally marked section.
    for following in lines[index + 1:]:
        if not following.strip():
            continue
        if NUMBERED_ENTRY.match(strip_markdown(following)):
            return False
        if _marked_heading(following) and is_reference_end(following):
            break
    if _marked_heading(line):
        return True
    # Unmarked captions must be visually separated, not citation continuations.
    before_blank = index == 0 or not lines[index - 1].strip()
    after_blank = index + 1 == len(lines) or not lines[index + 1].strip()
    next_is_table = index + 1 < len(lines) and lines[index + 1].lstrip().startswith("|")
    return before_blank and (after_blank or next_is_table)


def find_reference_span(text, min_position_ratio=0.3):
    """Return the exact removable character span, leaving following content intact."""
    if not text:
        return None
    lines = text.splitlines(keepends=True)
    position = 0
    start = None
    for index, line in enumerate(lines):
        if start is None:
            if (normalize_heading(line) in REFERENCE_HEADINGS
                    and position / max(len(text), 1) >= min_position_ratio):
                start = position
        elif _section_start(lines, index):
            return start, position
        position += len(line)
    return None if start is None else (start, len(text))


def remove_references(text, min_position_ratio=0.3):
    if not text or not text.strip():
        return ""
    span = find_reference_span(text, min_position_ratio)
    if span is None:
        return text.strip()
    start, end = span
    return (text[:start] + text[end:]).strip()


def remove_document_references(document, min_position_ratio=0.3):
    # Version the cleaner independently; do not silently reuse legacy artifacts.
    version = document.get("processing_version", "legacy-extraction")
    if not version.endswith("|" + REFERENCE_PROCESSING_VERSION):
        version += "|" + REFERENCE_PROCESSING_VERSION
    document = {**document, "processing_version": version}
    pages = document.get("pages", [])
    full_text = "\n".join(page["text"] for page in pages)
    span = find_reference_span(full_text, min_position_ratio)
    if span is None:
        return document
    ref_start, ref_end = span
    new_pages = []
    position = 0
    for page in pages:
        page_text = page["text"]
        page_start, page_end = position, position + len(page_text)
        if ref_start >= page_end or ref_end <= page_start:
            retained = page_text
        else:
            retained = (page_text[:max(0, ref_start - page_start)]
                        + page_text[max(0, ref_end - page_start):])
        if retained.strip():
            new_pages.append({**page, "text": retained.strip()})
        position = page_end + 1
    return {**document, "pages": new_pages}
