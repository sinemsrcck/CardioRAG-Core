
"""
PDF yükleme + kaynakça temizleme testi.

Çalıştırma:
    python -m evaluation.test_pdf_processing
"""

import re
import sys
import argparse
import copy
import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path

from src.pdf_loader import load_all_pdfs, load_pdf, recover_rotated_footnotes, _text_key
from src.pdf_processing import find_reference_span, remove_document_references, remove_references
from src.chunking import chunk_document


ATRIAL_FIBRILLATION_FILE = "pubM-Atrial Fibrillation.pdf"

EXPECTED_PDFS = {
    "pubM-Atrial Fibrillation.pdf": 28,
    "pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf": 34,
    "TK-cardiovascular36-2-1.pdf": 8,
    "TK-cardiovascular37-2-1.pdf": 8,
    "who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf": 6,
    "who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf": 11,
}
AF_NOTES = (
    "fAF progression indicates progression in AF stage (eg, paroxysmal to persistent AF or persistent to permanent).",
    "gExercise training is recommended in patients without AF in the setting of excessive exercise training.",
)


def validate_collection(documents):
    names = [doc["filename"] for doc in documents]
    if len(names) != len(EXPECTED_PDFS) or set(names) != set(EXPECTED_PDFS):
        raise ValueError(f"Expected all six PDFs exactly once, got: {names}")
    for doc in documents:
        expected = EXPECTED_PDFS[doc["filename"]]
        if doc["page_count"] != expected:
            raise ValueError(f"Unexpected physical page count: {doc['filename']}")
        if [p["page_number"] for p in doc["pages"]] != list(range(1, expected + 1)):
            raise ValueError(f"Incomplete or reordered physical pages: {doc['filename']}")
        if not any(p["text"].strip() for p in doc["pages"]):
            raise ValueError(f"Empty PDF extraction: {doc['filename']}")


def content_failures(document, cleaned):
    failures = []
    before, after = full_text(document), full_text(cleaned)
    span = find_reference_span(before)
    if span:
        # All text outside the actual removable reference span must survive.
        for retained in (before[:span[0]], before[span[1]:]):
            if _text_key(retained) not in _text_key(after):
                failures.append(f"{document['filename']}: non-reference content lost")
    if document["filename"] != ATRIAL_FIBRILLATION_FILE:
        return failures
    pages = {p["page_number"]: p for p in cleaned["pages"]}
    for note in AF_NOTES:
        if _text_key(note) not in _text_key(pages.get(26, {}).get("text", "")):
            failures.append("AF physical page 26: missing verbatim note " + note[0])
    # Probe actual table bodies on their physical pages, not mentions in prose.
    expected_content = {
        24: ("Stroke", "2.42", "Heart failure", "4.99", "Lifetime risk"),
        25: ("Weight loss", "210 min per week", "Hypertension", "blood pressure"),
        27: ("RAAFT-1", "A4 study", "CASTLE-AF", "Ablation", "Control", "37.8"),
    }
    for number, terms in expected_content.items():
        text = pages.get(number, {}).get("text", "")
        if not TABLE_SEPARATOR.search(text):
            failures.append(f"AF page {number}: missing structured clinical table")
        for term in terms:
            if _text_key(term) not in _text_key(text):
                failures.append(f"AF page {number}: missing table content {term!r}")
    return failures



# Numaralı kaynak satırları:
#   "12. Smith AB, ..."      (Vancouver)
#   "1. Cassar, A.; ..."     (MDPI)
NUMBERED_REFERENCE = re.compile(
    r"^\s*\d{1,3}\.\s+[A-Z][A-Za-z'’\-]+(?:\s[A-Z][A-Za-z'’\-]+)?,?\s[A-Z]{1,3}[,.;]",
    re.MULTILINE,
)
DOI = re.compile(r"\bdoi\b", re.IGNORECASE)

# Markdown tablosu başlık ayracı satırı: |---|---|
TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$", re.MULTILINE)

# Bir pencerede bu kadar numaralı kaynak / doi varsa kaynakça kalmış demektir.
DENSITY_WINDOW = 3000
MAX_NUMBERED_REFS_IN_WINDOW = 3
MAX_DOI_IN_WINDOW = 3


def full_text(document):
    return "\n".join(page["text"] for page in document["pages"])


def max_density(pattern, text, window=DENSITY_WINDOW):
    """Metindeki herhangi bir pencerede en fazla kaç eşleşme var?"""
    positions = [m.start() for m in pattern.finditer(text)]
    best = 0
    left = 0

    for right, pos in enumerate(positions):
        while pos - positions[left] > window:
            left += 1
        best = max(best, right - left + 1)

    return best


def main(pdf_dir=None):
    documents = load_all_pdfs(pdf_dir) if pdf_dir is not None else load_all_pdfs()
    validate_collection(documents)
    failures = []
    regenerated_chunks = []

    print()
    header = (
        f"{'Dosya':<45} {'Sayfa':>9} {'Karakter':>17} "
        f"{'Kaynakça':>9} {'Sonrası':>8} {'Tablo':>6}"
    )
    print(header)
    print("-" * len(header))

    for document in documents:
        name = document["filename"]
        raw_text = full_text(document)
        span = find_reference_span(raw_text)

        cleaned = remove_document_references(document)
        cleaned_text = full_text(cleaned)
        failures.extend(content_failures(document, cleaned))
        document_chunks = chunk_document(cleaned)
        regenerated_chunks.extend(document_chunks)
        for chunk in document_chunks:
            if f"_v" not in chunk["chunk_id"] or "_t" not in chunk["chunk_id"]:
                failures.append(f"{name}: unversioned chunk identity")
            if chunk["text_sha256"] != hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest():
                failures.append(f"{name}: chunk text hash mismatch")

        found = span is not None
        kept_after = found and span[1] < len(raw_text)
        # Her tabloda bir "|---|---|" ayraç satırı vardır.
        table_count = len(TABLE_SEPARATOR.findall(cleaned_text))

        print(
            f"{name[:45]:<45} "
            f"{len(document['pages']):>4}/{len(cleaned['pages']):<4} "
            f"{len(raw_text):>8}/{len(cleaned_text):<8} "
            f"{'evet' if found else 'HAYIR':>9} "
            f"{'evet' if kept_after else '-':>8} "
            f"{table_count:>6}"
        )

        # (3) Kaynakça bulunmuş olmalı.
        if not found:
            failures.append(f"{name}: kaynakça bulunamadı")

        # (1) Numaralı kaynak satırları / doi yoğunluğu kalmamalı.
        ref_density = max_density(NUMBERED_REFERENCE, cleaned_text)
        doi_density = max_density(DOI, cleaned_text)

        if ref_density > MAX_NUMBERED_REFS_IN_WINDOW:
            failures.append(
                f"{name}: temiz metinde numaralı kaynak yoğunluğu "
                f"{ref_density} (/{DENSITY_WINDOW} karakter)"
            )
        if doi_density > MAX_DOI_IN_WINDOW:
            failures.append(
                f"{name}: temiz metinde doi yoğunluğu "
                f"{doi_density} (/{DENSITY_WINDOW} karakter)"
            )

        # Sayfa numaraları korunmalı (artan sırada, orijinalden).
        original_numbers = {p["page_number"] for p in document["pages"]}
        cleaned_numbers = [p["page_number"] for p in cleaned["pages"]]
        if (
            cleaned_numbers != sorted(cleaned_numbers)
            or not set(cleaned_numbers) <= original_numbers
        ):
            failures.append(f"{name}: sayfa numaraları bozulmuş")

        # (2) Atrial Fibrillation: Table 1-3 korunmalı.
        if name == ATRIAL_FIBRILLATION_FILE:
            for number in (1, 2, 3):
                if not re.search(rf"\bTable\s*{number}\b", cleaned_text):
                    failures.append(f"{name}: Table {number} kayboldu")

            pages = [p["page_number"] for p in cleaned["pages"]]
            print(f"    -> korunan sayfalar: {pages}")

    # The previous self-hash checks could not detect page-only vs full-document
    # Markdown differences. Check the actual stored dataset against this run.
    from evaluation.evaluate_rag import load_questions, validate_evidence_chunks
    try:
        audit = validate_evidence_chunks(
            load_questions(Path(__file__).with_name("test_questions.csv")), regenerated_chunks,
        )
        print("Full-document evidence audit:", audit)
    except ValueError as exc:
        failures.append(str(exc))

    print()

    if failures:
        print(f"BAŞARISIZ ({len(failures)} sorun):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print(f"BAŞARILI: {len(documents)} PDF'in tüm kontrolleri geçti.")



class PDFRegressionTests(unittest.TestCase):
    """Fast local fixtures only. The CLI separately runs the real six-PDF audit."""

    def test_empty_missing_and_duplicate_collection_fail(self):
        with self.assertRaises(ValueError):
            validate_collection([])
        docs = [{"filename": name, "page_count": count,
                 "pages": [{"page_number": i, "text": "content"} for i in range(1, count + 1)]}
                for name, count in EXPECTED_PDFS.items()]
        validate_collection(docs)
        with self.assertRaises(ValueError):
            validate_collection(docs[:-1])
        with self.assertRaises(ValueError):
            validate_collection(docs[:-1] + [docs[0]])
        docs[0]["pages"].pop()
        with self.assertRaises(ValueError):
            validate_collection(docs)

    def test_main_rejects_empty_loader(self):
        with patch.object(
            sys.modules[main.__module__],
            "load_all_pdfs",
            return_value=[]
        ):
            with self.assertRaises(ValueError):
                main()

    def sample_page(self, texts, direction=(0., -1.)):
        from unittest.mock import Mock
        page = Mock()
        page.get_text.return_value = {"blocks": [
            {"bbox": (30., 60., 70., 400.), "lines": [
                {"dir": direction, "spans": [{"text": text}]}]} for text in texts]}
        return page

    def test_missing_notes_are_verbatim_and_have_coordinates(self):
        result, spans = recover_rotated_footnotes(self.sample_page(AF_NOTES), "")
        for note in AF_NOTES:
            self.assertIn(note, result)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["bbox"], [30., 60., 70., 400.])

    def test_notes_are_not_duplicated(self):
        original = "**f** " + AF_NOTES[0][1:]
        result, spans = recover_rotated_footnotes(self.sample_page(AF_NOTES[:1]), original)
        self.assertEqual(result, original)
        self.assertEqual(spans, [])

    def test_watermarks_and_table_cells_not_reconstructed(self):
        page = self.sample_page(["Author Manuscript", "Decreased", "gExercise"])
        result, spans = recover_rotated_footnotes(page, "")
        self.assertEqual((result, spans), ("", []))

    def test_multiline_note_preserves_qualifiers(self):
        page = self.sample_page([])
        page.get_text.return_value = {"blocks": [{"bbox": (1., 2., 3., 4.), "lines": [
            {"dir": (0., -1.), "spans": [{"text": "aTreatment is not recommended"}]},
            {"dir": (0., -1.), "spans": [{"text": "without careful assessment of the patient."}]},
        ]}]}
        result, _ = recover_rotated_footnotes(page, "")
        self.assertIn("not recommended without careful assessment", result)

    def test_citation_title_table_figure_and_pipe_do_not_end_references(self):
        body = "Clinical results and discussion.\n" * 30
        for title in ("Table 2. Outcomes from a cited study.", "Figure 1. A citation title.",
                      "| cited table | DOI |", "**Table 2. A cited title**"):
            text = body + "## References\n1. Smith A. Citation.\n\n" + title
            text += "\n\n2. Jones B. Next citation.\n\n## Appendix\nRetained outcome."
            result = remove_references(text)
            self.assertNotIn("Smith", result)
            self.assertNotIn("Jones", result)
            self.assertNotIn(title, result)
            self.assertIn("Retained outcome.", result)


    def test_table_in_reference_does_not_hide_following_citations(self):
        text = "Clinical body.\n" * 80
        text += "## References\n1. Smith A. Citation.\n\n**Table 2. Cited title**\n\n"
        text += "|Citation metadata|DOI|\n|---|---|\n|a|b|\n2. Jones B. Next citation.\n\n"
        text += "## Supplementary material\n\nActual study outcomes."
        result = remove_references(text)
        self.assertNotIn("Jones", result)
        self.assertNotIn("Citation metadata", result)
        self.assertIn("Actual study outcomes.", result)

    def test_real_post_reference_sections_survive(self):
        body = "Clinical results and discussion.\n" * 30
        for heading in ("## Table 1. Outcomes", "## Tables", "**Figure 2. Outcomes**",
                        "## Figures", "## Figure legends", "## Fig. 1",
                        "### Box. Frequently Asked Questions", "## Supplementary material",
                        "## Supplementary Table 1", "## Appendix", "## Acknowledgments"):
            text = body + "## References\n1. Smith A. Citation.\n\n" + heading
            text += "\n\nClinical outcome content."
            result = remove_references(text)
            self.assertNotIn("Smith", result, heading)
            self.assertIn(heading, result)
            self.assertIn("Clinical outcome content.", result)

    def test_unmarked_standalone_caption_survives(self):
        body = "Clinical results.\n" * 30
        text = body + "References\n1. Smith A. Citation.\n\nTable 1. Outcomes\n\n| A | B |\n|---|---|\n|1|2|"
        self.assertIn("|1|2|", remove_references(text))

    def test_inline_and_early_reference_words_are_not_cutoffs(self):
        text = "References\n" + ("Clinical body references a Table or Figure.\n" * 30)
        self.assertIsNone(find_reference_span(text))

    def test_page_numbers_and_cross_page_content_survive(self):
        doc = {"pages": [
            {"page_number": 4, "text": "Clinical results.\n" * 30 + "## References\n1. Smith A. Citation."},
            {"page_number": 5, "text": "2. Jones B. Citation.\n\n## Table 1. Outcomes\n\n|A|B|\n|---|---|\n|1|2|"},
        ]}
        result = remove_document_references(doc)
        self.assertEqual([p["page_number"] for p in result["pages"]], [4, 5])
        self.assertIn("|1|2|", result["pages"][1]["text"])
        self.assertNotIn("Jones", full_text(result))

    def test_ids_invalidate_legacy_version_text_and_settings(self):
        doc = {"document_id": "abc", "filename": "test.pdf", "processing_version": "v2",
               "pages": [{"page_number": 26, "text": "Clinical finding " * 40}]}
        first = chunk_document(doc)
        self.assertEqual(first, chunk_document(doc))
        self.assertNotEqual(first[0]["chunk_id"], "abc_p26_c0")
        other = copy.deepcopy(doc)
        other["processing_version"] = "v3"
        self.assertNotEqual(first[0]["chunk_id"], chunk_document(other)[0]["chunk_id"])
        other = copy.deepcopy(doc)
        other["pages"][0]["text"] += "Changed finding."
        self.assertNotEqual(first[0]["chunk_id"], chunk_document(other)[0]["chunk_id"])
        self.assertNotEqual(first[0]["chunk_id"], chunk_document(doc, chunk_size=1400)[0]["chunk_id"])

    def test_loader_rejects_incomplete_or_reordered_pages(self):
        from unittest.mock import MagicMock
        pdf = MagicMock()
        pdf.__len__.return_value = 2
        with (patch("src.pdf_loader.calculate_file_hash", return_value="hash"),
              patch("src.pdf_loader.pymupdf.open") as opened,
              patch("src.pdf_loader.pymupdf4llm.to_markdown") as convert):
            opened.return_value.__enter__.return_value = pdf
            convert.return_value = [{"text": "only one page"}]
            with self.assertRaises(ValueError):
                load_pdf("fixture.pdf")
            convert.return_value = [{"text": "wrong order", "metadata": {"page": 2}}, {"text": "second"}]
            with self.assertRaises(ValueError):
                load_pdf("fixture.pdf")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit the expected six PDFs without embeddings or LLM calls")
    parser.add_argument("--pdf-dir", type=Path, default=None)
    args = parser.parse_args()
    main(args.pdf_dir)
