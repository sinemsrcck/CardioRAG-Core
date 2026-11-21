"""kanit_parcalari (çok parçalı kanıt) ve v3.1 regresyon testleri.

API çağrısı ve veritabanı bağlantısı yoktur. Gerçek PDF/v3 testleri dosyalar yoksa atlanır."""
import json
import unittest
from collections import Counter
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from src.pilot_clinical import check_evidence_parts, import_records, validate_records

PAGES = {
    1: 'Intro text. The patients were evaluated by two experienced',
    2: 'cardiologists using an echo device. The ratio is inversely proportional to C-pep-',
    3: 'tide level. Events: stroke in 5 patients, ischemia in 7 patients',
    4: '**TABLE 1:** Baseline\n|Age|62|58|0.002|',
    5: ('(1.88%) and other events. ROC cut-off 2.72, with 70% sensitivity and 68% \n\n'
        '**FIGURE 1:** ROC curve.\n\nAbbreviations. \n\nspecificity (AUC=0.692) (Figure 1). More text.'),
}
ECHO = [{'sayfa': 1, 'metin': 'The patients were evaluated by two experienced'},
        {'sayfa': 2, 'metin': 'cardiologists using an echo device.'}]
HYPHEN = [{'sayfa': 2, 'metin': 'The ratio is inversely proportional to C-pep-'},
          {'sayfa': 3, 'metin': 'tide level.'}]
SKIP = [{'sayfa': 3, 'metin': 'Events: stroke in 5 patients, ischemia in 7 patients'},
        {'sayfa': 5, 'metin': '(1.88%) and other events.'}]
FIGURE = [{'sayfa': 5, 'metin': 'ROC cut-off 2.72, with 70% sensitivity and 68%'},
          {'sayfa': 5, 'metin': 'specificity (AUC=0.692) (Figure 1).'}]


def rec(parts, fact, **kw):
    base = dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=parts[0]['metin'], sayfa=parts[0]['sayfa'], sayfa_bitis=parts[-1]['sayfa'],
                birlestirme=check_evidence_parts(parts, PAGES), kanit_parcalari=parts, kanit_turu='metin')
    base.update(kw)
    return base


def errors_of(*records, pages=PAGES):
    return validate_records(list(records), pages)[1]


class ValidParts(unittest.TestCase):
    def test_four_split_kinds_accepted(self):
        cases = [(ECHO, 'two experienced', 'sayfa'), (HYPHEN, 'C-pep-', 'sayfa'),
                 (SKIP, 'ischemia in 7 patients', 'ara_blok'), (FIGURE, 'AUC=0.692', 'ara_blok')]
        for parts, fact, joined in cases:
            record = rec(parts, fact)
            self.assertEqual(record['birlestirme'], joined)
            self.assertEqual(errors_of(record), [], fact)

    def test_number_in_later_part_verified(self):
        self.assertEqual(errors_of(rec(SKIP, 'ischemia in 7 patients', deger='1.88', unit='%')), [])
        self.assertIn('kanıt parçalarında yok', errors_of(rec(SKIP, 'ischemia in 7 patients', deger='1.99'))[0]['hata'])

    def test_same_parts_different_facts_kept_exact_repeat_rejected(self):
        a, b = rec(FIGURE, 'cut-off 2.72'), rec(FIGURE, 'AUC=0.692')
        good, errors = validate_records([a, b, dict(a)], PAGES)
        self.assertEqual(len(good), 2)
        self.assertTrue(errors[0]['hata'].startswith('tekrar'))


class RejectedParts(unittest.TestCase):
    def assertRejected(self, record, needle, pages=PAGES):
        errors = errors_of(record, pages=pages)
        self.assertEqual(len(errors), 1, record)
        self.assertIn(needle, errors[0]['hata'])

    def test_no_stitched_quote(self):
        # Parçalar birleştirilerek tek alıntı oluşturulamaz.
        stitched = ECHO[0]['metin'] + ' ' + ECHO[1]['metin']
        self.assertRejected(rec(ECHO, 'two experienced', kanit_cumle=stitched), 'ilk kanıt parçası')
        no_parts = dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en='two experienced',
                        kanit_cumle=stitched, sayfa=1, sayfa_bitis=2)
        self.assertRejected(no_parts, 'birebir bulunamadı')

    def test_fact_spanning_parts_rejected(self):
        self.assertRejected(rec(ECHO, 'two experienced cardiologists'), 'tek bir kanıt parçasında')

    def test_part_must_be_exact_on_its_own_page(self):
        parts = [ECHO[0], {'sayfa': 2, 'metin': 'cardiologists  using an echo device.'}]
        self.assertRejected(dict(rec(ECHO, 'two experienced'), kanit_parcalari=parts), 's.2 metninde')
        wrong_page = [ECHO[0], {'sayfa': 3, 'metin': ECHO[1]['metin']}]
        self.assertRejected(dict(rec(ECHO, 'two experienced'), kanit_parcalari=wrong_page), 's.3 metninde')

    def test_continuity_required(self):
        not_page_end = [{'sayfa': 1, 'metin': 'The patients were evaluated'}, ECHO[1]]
        not_page_start = [ECHO[0], {'sayfa': 2, 'metin': 'using an echo device.'}]
        reversed_order = [ECHO[1], ECHO[0]]
        skip_two = [{'sayfa': 2, 'metin': HYPHEN[0]['metin']}, {'sayfa': 5, 'metin': SKIP[1]['metin']}]
        no_caption = [{'sayfa': 5, 'metin': '(1.88%) and other events.'}, {'sayfa': 5, 'metin': 'ROC cut-off 2.72'}]
        for parts in (not_page_end, not_page_start, reversed_order, skip_two, no_caption):
            with self.assertRaisesRegex(ValueError, 'kesintisiz değil'):
                check_evidence_parts(parts, PAGES)

    def test_skipped_page_must_be_table_or_figure(self):
        pages = {**PAGES, 4: 'Ordinary prose page.'}
        with self.assertRaisesRegex(ValueError, 'kesintisiz değil'):
            check_evidence_parts(SKIP, pages)

    def test_structure_and_metadata(self):
        base = rec(ECHO, 'two experienced')
        self.assertRejected(dict(base, kanit_parcalari=ECHO[:1]), '2-3 parçalı')
        self.assertRejected(dict(base, kanit_parcalari=[dict(ECHO[0], not_='x'), ECHO[1]]), 'yalnızca sayfa ve metin')
        self.assertRejected(dict(base, birlestirme='ara_blok'), "'sayfa' olmalı")
        self.assertRejected(dict(base, sayfa_bitis=1), 'sayfa_bitis')
        self.assertRejected(dict(base, kanit_turu='tablo'), 'yalnızca metin')
        single = dict(bilgi_turu='tani', klinik_bilgi_en='Intro', kanit_cumle='Intro text.', sayfa=1,
                      birlestirme='sayfa')
        self.assertRejected(single, 'yalnızca kanit_parcalari ile')


class ImportUnchanged(unittest.TestCase):
    """import_records değişmedi: çok parçalı kayıtlar veritabanına bağlanmadan açık hatayla durur."""

    def test_multi_part_records_blocked_before_db(self):
        fake = ModuleType('src.database')
        fake.get_connection = lambda: self.fail('Veritabanına bağlanılmamalı')
        with patch.dict('sys.modules', {'src.database': fake}):
            with self.assertRaisesRegex(ValueError, 'Çok sayfalı kanıt'):
                import_records({'document_id': 'a' * 64, 'filename': 'x.pdf'},
                               [rec(ECHO, 'two experienced', review_status='verified')])
            with self.assertRaisesRegex(ValueError, 'kanit_parcalari'):
                import_records({'document_id': 'a' * 64, 'filename': 'x.pdf'},
                               [rec(FIGURE, 'AUC=0.692', review_status='verified')])


PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')
V3 = Path('data/review/pilot/pilot_findings_v3.json')
V31 = Path('data/review/pilot/pilot_findings_v3_1.json')
LIMITATIONS = (
    'Since this study was designed retrospectively, full control over the data could not be achieved, and the '
    'limitations of our study include potential gaps in patient records.',
    'Additionally, the generalizability of the study results is limited due to the inability to access patient '
    'records from other centers.',
    'Since our study is a retrospective analysis, additional control over potential confounding effects related to '
    'the presence of DM could not be achieved.',
    'In future studies, using methods such as Propensity Score Matching to equalize groups with and without DM in '
    'larger sample sizes would be beneficial for enhancing comparability.',
    'Within the limitations of our study, it seems to be single-centered. It needs to be supported by other '
    'multi-center studies with a larger number of patients.',
)
AGE_ROW = '|emograpcs eatures<br>Age (years)|62±10.3|58.2±11.2|0.002|'


@unittest.skipUnless(PDF.exists() and V3.exists() and V31.exists(), 'pilot PDF / v3 / v3.1 yok')
class PilotV31Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.pilot_clinical import pdf_pages
        cls.doc, cls.pages = pdf_pages(PDF)
        cls.v3 = json.loads(V3.read_text(encoding='utf-8'))
        cls.v31 = json.loads(V31.read_text(encoding='utf-8'))
        cls.multi = [r for r in cls.v31 if 'kanit_parcalari' in r]

    def by_fact(self, data, needle):
        return [r for r in data if needle in r['klinik_bilgi_en']]

    def test_v31_valid_and_not_verified(self):
        good, errors = validate_records(self.v31, self.pages)
        # PDF yükleyici v4: özetteki "0.682-0.889" artık PDF'teki gibi tireli. #6'nın kanıtı v3
        # yükleyicisinin bozduğu "0.6820.889" metnini taşıyor; kayıt sessizce değiştirilmez, açıkça düşer.
        self.assertEqual(len(good), 189)
        self.assertEqual([e['satir'] for e in errors], [6])
        self.assertIn('0.6820.889', self.v31[5]['kanit_cumle'])
        self.assertFalse(any('review_status' in r for r in self.v31))

    def test_no_clinical_fact_lost_and_only_split_records_changed(self):
        facts = lambda data: Counter((r['bilgi_turu'], r['baglam'], r['klinik_bilgi_en']) for r in data)
        self.assertEqual(facts(self.v3), facts(self.v31))
        changed = [(a, b) for a, b in zip(self.v3, self.v31) if a != b]
        self.assertEqual(len(changed), 6)
        self.assertEqual(len(self.multi), 6)
        for old, new in changed:
            self.assertTrue(any(old['kanit_cumle'] in p['metin'] for p in new['kanit_parcalari']))

    def test_every_part_exact_on_own_page_and_no_stitched_quote(self):
        for record in self.multi:
            for part in record['kanit_parcalari']:
                self.assertIn(part['metin'], self.pages[part['sayfa']])
            self.assertEqual(record['kanit_cumle'], record['kanit_parcalari'][0]['metin'])
        for fake in ('experienced cardiologists', 'experiencedcardiologists', 'C-peptide level', '7 patients (1.88%)'):
            self.assertFalse(any(fake in r['kanit_cumle'] for r in self.v31), fake)

    def test_age_row_kept_and_mapped_by_pilot_tablo(self):
        for data in (self.v3, self.v31):
            self.assertEqual([r['sayfa'] for r in data if r['kanit_cumle'] == AGE_ROW], [4])
        import src.pilot_tablo as pt
        records, _ = pt.load_records(V31)
        transfer = pt.enrich(records, self.pages, self.doc['document_id'])['aktarim']
        age = [r for r in transfer if r.get('sonuc_olcutu_tr') == 'Age' and json.loads(r['taslak_notu'])['tablo_no'] == 1]
        self.assertEqual([(r['hasta_grubu'], r['deger'], r['unit'], r['p']) for r in age],
                         [('Ischemic event', '62±10.3', 'years', '0.002'),
                          ('No ischemic event', '58.2±11.2', 'years', '0.002')])

    def test_limitations_kept(self):
        for data in (self.v3, self.v31):
            quotes = {r['kanit_cumle'] for r in data if r['sayfa'] == 7}
            for sentence in LIMITATIONS:
                self.assertIn(sentence, self.pages[7])
                self.assertIn(sentence, quotes)

    def test_c_peptide_roc_complete(self):
        records = self.by_fact(self.v31, 'C peptide') + self.by_fact(self.v31, 'AUC=0.692')
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual((record['sayfa'], record['sayfa_bitis'], record['birlestirme']), (5, 5, 'ara_blok'))
            text = [p['metin'] for p in record['kanit_parcalari']]
            for value in ('2.72', '70% sensitivity', '68%'):
                self.assertIn(value, text[0])
            for value in ('AUC=0.692', 'CI95=0.512-0.753'):
                self.assertIn(value, text[1])

    def test_mesenteric_percentage_verifiable(self):
        [record] = self.by_fact(self.v31, 'mesenteric ischemia')
        self.assertEqual((record['sayfa'], record['sayfa_bitis'], record['birlestirme']), (3, 5, 'ara_blok'))
        self.assertEqual((record['deger'], record['unit']), ('1.88', '%'))
        self.assertTrue(record['kanit_parcalari'][1]['metin'].startswith('(1.88%)'))
        self.assertTrue(self.pages[4].lstrip().startswith('**TABLE 1:**'))

    def test_page_boundary_records(self):
        echo = self.by_fact(self.v31, 'two experienced') + self.by_fact(self.v31, 'EPIQ 7')
        self.assertEqual({(r['sayfa'], r['sayfa_bitis'], r['birlestirme']) for r in echo}, {(2, 3, 'sayfa')})
        [hcr] = self.by_fact(self.v31, 'inversely proportional')
        self.assertEqual([p['metin'] for p in hcr['kanit_parcalari']][1], 'tide level.')
        self.assertEqual((hcr['sayfa'], hcr['sayfa_bitis'], hcr['birlestirme']), (6, 7, 'sayfa'))


if __name__ == '__main__':
    unittest.main()
