"""PDF yükleyici: satır sonunda bölünen sayısal aralıkların tiresi korunur. API/veritabanı yok."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from src.pdf_loader import (PDF_PROCESSING_VERSION, load_pdf, numeric_line_break_pairs,
                            restore_numeric_hyphens)

PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')


class RestoreNumericHyphens(unittest.TestCase):
    def test_line_broken_range_restored(self):
        raw = 'AUC=0.821, CI95=0.682-\n0.889, p<001).'
        text, restored = restore_numeric_hyphens('AUC=0.821, CI95=0.6820.889, p<001).', raw)
        self.assertEqual(text, 'AUC=0.821, CI95=0.682-0.889, p<001).')
        self.assertEqual(restored, [{'kind': 'numeric_hyphen', 'text': '0.682-0.889'}])

    def test_only_hyphen_inserted_digits_unchanged(self):
        source = 'pages 373498. and 0.6820.889'
        text, _ = restore_numeric_hyphens(source, 'pages 373-\n498. and 0.682-\n0.889')
        self.assertEqual(text.replace('-', ''), source)
        self.assertEqual(text, 'pages 373-498. and 0.682-0.889')

    def test_ambiguous_or_missing_left_untouched(self):
        raw = 'CI 1.2-\n3.4'
        for text in ('1.23.4 and 1.23.4', 'no joined value', '1.2-3.4 already kept', '11.23.4', '1.23.45'):
            self.assertEqual(restore_numeric_hyphens(text, raw)[0], text, text)

    def test_repeated_pair_restored_when_counts_match(self):
        raw = '1.2-\n3.4 x\nand 1.2-\n3.4'
        self.assertEqual(restore_numeric_hyphens('1.23.4 x and 1.23.4', raw)[0], '1.2-3.4 x and 1.2-3.4')

    def test_word_hyphenation_not_affected(self):
        self.assertEqual(numeric_line_break_pairs('The Kolmogorov-\nSmirnov test\nC-pep-\ntide 12'), [])
        self.assertEqual(numeric_line_break_pairs(None), [])


class LoaderIntegration(unittest.TestCase):
    def test_generated_pdf_range_at_line_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'range.pdf'
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((72, 400), 'The HCR cut-off was 2.82 (AUC=0.821, CI95=0.682-')
                page.insert_text((72, 414), '0.889, p<0.001) in the ischemic event group.')
                doc.save(path)
            document = load_pdf(path)
        text = document['pages'][0]['text']
        self.assertIn('0.682-0.889', text)
        self.assertNotIn('0.6820.889', text)
        self.assertTrue(document['processing_version'].startswith('markdown-rotated-v4'))

    def test_version_bumped(self):
        self.assertEqual(PDF_PROCESSING_VERSION, 'markdown-rotated-v4')

    @unittest.skipUnless(PDF.exists(), 'pilot PDF yok')
    def test_real_pilot_pdf_abstract_ci(self):
        pages = {p['page_number']: p for p in load_pdf(PDF)['pages']}
        self.assertIn('CI95=0.682-0.889, p<001', pages[1]['text'])
        self.assertNotIn('0.6820.889', pages[1]['text'])
        self.assertEqual([r['text'] for r in pages[1]['restored_numeric_hyphens']], ['0.682-0.889'])
        # Kaynak yazım hatası "p<001" PDF'te böyle yazılı; yükleyici düzeltmez.
        self.assertEqual(pages[5]['text'].count('p<001'), 3)
        # Harfli heceleme bilinen sınırlama: değiştirilmedi.
        self.assertIn('KolmogorovSmirnov', pages[3]['text'])


if __name__ == '__main__':
    unittest.main()
