"""kanit_parcalari (çok parçalı kanıt) ve v3.1 regresyon testleri.

API çağrısı ve veritabanı bağlantısı yoktur. Gerçek PDF/v3 testleri dosyalar yoksa atlanır."""
import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from src.pilot_clinical import check_evidence_parts, fact_in_parts, import_records, validate_records

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

    def test_fact_spanning_parts_only_at_page_word_boundary(self):
        # Kabul: 'sayfa' bağlı ardışık iki sayfa, kelime sınırı, tek boşluk, eşleşme sınırı geçiyor.
        self.assertEqual(errors_of(rec(ECHO, 'two experienced cardiologists')), [])
        self.assertTrue(fact_in_parts('experienced cardiologists using', ECHO, PAGES))
        # Red: birleştirme biçimi farklı, sınırın ötesine taşan metin, tireli kelime, ara_blok bağı.
        for parts, fact in ((ECHO, 'experiencedcardiologists'), (ECHO, 'experienced  cardiologists'),
                            (ECHO, 'two experienced cardiologists using an echo device. The ratio'),
                            (HYPHEN, 'C-pep- tide'), (HYPHEN, 'C-peptide'), (HYPHEN, 'C-pep-tide'),
                            (FIGURE, '68% specificity'), (SKIP, '7 patients (1.88%)')):
            self.assertRejected(rec(parts, fact), 'tek bir kanıt parçasında')
        # Red: kaydın bir bağı 'ara_blok' ise (birlestirme = 'ara_blok') 'sayfa' bağı üzerinden de birleştirilmez.
        mixed_pages = {1: 'Intro. alpha beta', 2: 'gamma delta \n\n**FIGURE 1:** Caption.\n\n epsilon end.'}
        mixed = [{'sayfa': 1, 'metin': 'alpha beta'}, {'sayfa': 2, 'metin': 'gamma delta'},
                 {'sayfa': 2, 'metin': 'epsilon end.'}]
        self.assertEqual(check_evidence_parts(mixed, mixed_pages), 'ara_blok')
        mixed_rec = dict(bilgi_turu='diger', klinik_bilgi_en='beta gamma', kanit_cumle='alpha beta', sayfa=1,
                         sayfa_bitis=2, birlestirme='ara_blok', kanit_parcalari=mixed, kanit_turu='metin')
        self.assertRejected(mixed_rec, 'tek bir kanıt parçasında', pages=mixed_pages)
        self.assertEqual(errors_of(dict(mixed_rec, klinik_bilgi_en='gamma delta'), pages=mixed_pages), [])
        # Red: parçalar kesintisiz değilse (önceki parça sayfa sonu değil) sınır araması yapılmaz.
        not_page_end = [{'sayfa': 1, 'metin': 'The patients were evaluated'}, ECHO[1]]
        self.assertFalse(fact_in_parts('evaluated cardiologists', not_page_end, PAGES))
        self.assertFalse(fact_in_parts('', ECHO, PAGES))
        self.assertFalse(fact_in_parts(None, ECHO, PAGES))

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


class FakeCursor:
    """import_records'un SQL çağrılarını yakalar (gerçek veritabanı yok)."""
    COLUMNS = ('bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'document_id', 'kanit_sayfa',
               'kanit_sayfa_bitis', 'birlestirme', 'kanit_cumle', 'kanit_turu', 'satir_hash', 'review_status',
               'cikarma_yontemi', 'taslak_notu', 'deger', 'unit', 'p')

    def __init__(self, inserts):
        self.inserts, self.rowcount, self._result = inserts, 0, None

    def execute(self, sql, params=()):
        if sql.startswith('SELECT filename'):
            self._result = [('x.pdf',)]
        elif sql.startswith('SHOW COLUMNS'):
            self._result = [(c,) for c in self.COLUMNS]
        else:
            columns = [c.strip('`') for c in sql.split('(', 1)[1].split(')', 1)[0].split(', ')]
            self.inserts.append(dict(zip(columns, params)))
            self.rowcount = 1

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.inserts = []

    def cursor(self):
        return FakeCursor(self.inserts)


class ImportMultipart(unittest.TestCase):
    """import_records çok parçalı/çok sayfalı kaydı yazar (eski ImportUnchanged yerine).

    Önceki test "SQL'e aktarılmaz" bekliyordu: SQL uyumu yokken bu kayıtlar bilerek durduruluyordu.
    Uyum eklendi (kanit_sayfa_bitis / birlestirme sütunları, parçalar taslak_notu'da); artık kayıt
    yazımdan önce validate_records ile aynı kurallarla denetleniyor. Tutarsız kayıt ve sayfası
    verilmemiş çok parçalı kayıt hâlâ veritabanına bağlanmadan durur."""
    DOC = {'document_id': 'a' * 64, 'filename': 'x.pdf'}

    def write(self, records, **kw):
        conn = FakeConnection()
        self.assertEqual(import_records(self.DOC, records, connection=conn, **kw), len(records))
        return conn.inserts

    def test_multi_part_written_with_parts_in_note(self):
        note = json.dumps({'kaynak': 'test', 'analiz': 'x'})
        [row] = self.write([rec(HYPHEN, 'C-pep-', review_status='verified', taslak_notu=note)], pages=PAGES)
        self.assertEqual((row['kanit_sayfa'], row['kanit_sayfa_bitis'], row['birlestirme']), (2, 3, 'sayfa'))
        self.assertEqual(row['kanit_cumle'], HYPHEN[0]['metin'])  # ilk parça; birleştirilmiş alıntı yok
        stored = json.loads(row['taslak_notu'])
        self.assertEqual(stored, {'kaynak': 'test', 'analiz': 'x', 'kanit_parcalari': HYPHEN})
        [same_page] = self.write([rec(FIGURE, 'AUC=0.692', review_status='verified')], pages=PAGES)
        self.assertEqual((same_page['kanit_sayfa'], same_page['kanit_sayfa_bitis'], same_page['birlestirme']),
                         (5, 5, 'ara_blok'))

    def test_pages_from_pdf_pages_doc(self):
        doc = dict(self.DOC, pages=[{'page_number': n, 'text': t} for n, t in PAGES.items()])
        conn = FakeConnection()
        import_records(doc, [rec(ECHO, 'two experienced', review_status='verified')], connection=conn)
        self.assertEqual(conn.inserts[0]['kanit_sayfa_bitis'], 2)

    def test_hash_includes_parts_single_page_hash_unchanged(self):
        a, b = (self.write([rec(ECHO, 'two experienced', review_status='verified', **kw)], pages=PAGES)[0]
                for kw in ({}, {'klinik_bilgi_en': 'two experienced'}))
        self.assertEqual(a['satir_hash'], b['satir_hash'])
        single = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='Intro', kanit_cumle='Intro text.',
                      sayfa=1, review_status='verified')
        identity = [self.DOC['document_id'], 'tani', 'study_finding', None, 'Intro', 1, 'Intro text.']
        expected = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for record in (single, dict(single, sayfa_bitis=1)):
            [row] = self.write([record])  # sayfa gerekmez: tek sayfalı yol değişmedi
            self.assertEqual(row['satir_hash'], expected)
            self.assertNotIn('kanit_sayfa_bitis', row)
        multi = self.write([rec(FIGURE, 'AUC=0.692', review_status='verified')], pages=PAGES)[0]
        other = self.write([rec(FIGURE, 'AUC=0.692', review_status='verified',
                                kanit_parcalari=[FIGURE[0], {'sayfa': 5, 'metin': 'specificity (AUC=0.692)'}])],
                           pages=PAGES)[0]
        self.assertNotEqual(multi['satir_hash'], other['satir_hash'])  # parçalar kimlikte

    def test_inconsistent_or_unverifiable_blocked_before_db(self):
        fake = ModuleType('src.database')
        fake.get_connection = lambda: self.fail('Veritabanına bağlanılmamalı')
        good = rec(ECHO, 'two experienced', review_status='verified')
        cases = [
            (dict(good, kanit_cumle=ECHO[0]['metin'] + ' ' + ECHO[1]['metin']), PAGES, 'ilk kanıt parçası'),
            (dict(good, birlestirme='ara_blok'), PAGES, "'sayfa' olmalı"),
            (dict(good, sayfa_bitis=1), PAGES, 'sayfa_bitis'),
            (dict(good, kanit_parcalari=[ECHO[0], {'sayfa': 2, 'metin': 'metinde yok'}]), PAGES, 's.2 metninde'),
            (good, None, 'PDF sayfaları gerekli'),
            (dict(good, review_status=None), PAGES, 'manuel onaylı'),
            (dict(good, taslak_notu='bozuk {json'), PAGES, 'taslak_notu JSON değil'),
        ]
        with patch.dict('sys.modules', {'src.database': fake}):
            for record, pages, needle in cases:
                with self.assertRaisesRegex(ValueError, needle):
                    import_records(self.DOC, [record], pages=pages)


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



V32 = Path('data/review/pilot/pilot_findings_v3_2.json')
STATS = Path('data/review/pilot/pilot_findings_v3_1_statistics.json')


@unittest.skipUnless(PDF.exists() and V31.exists() and V32.exists(), 'pilot PDF / v3.1 / v3.2 yok')
class PilotV32Regression(unittest.TestCase):
    """v3.2 = v3.1 + yalnız #6 kanıtında PDF yükleyici v4'ün geri getirdiği tire."""

    @classmethod
    def setUpClass(cls):
        from src.pilot_clinical import pdf_pages
        cls.doc, cls.pages = pdf_pages(PDF)
        cls.v31 = json.loads(V31.read_text(encoding='utf-8'))
        cls.v32 = json.loads(V32.read_text(encoding='utf-8'))

    def test_all_190_valid_with_loader_v4(self):
        self.assertTrue(self.doc['processing_version'].startswith('markdown-rotated-v4'))
        good, errors = validate_records(self.v32, self.pages)
        self.assertEqual((len(good), errors), (190, []))
        self.assertFalse(any('review_status' in r for r in self.v32))

    def test_only_record_6_hyphen_changed(self):
        changed = [i for i, (a, b) in enumerate(zip(self.v31, self.v32), 1) if a != b]
        self.assertEqual((len(self.v32), changed), (190, [6]))
        old, new = self.v31[5], self.v32[5]
        for field in ('kanit_cumle', 'klinik_bilgi_en'):
            self.assertEqual(new[field], old[field].replace('0.6820.889', '0.682-0.889'))
            self.assertIn('p<001', new[field])  # kaynak yazım hatası korunur
        self.assertEqual({k: v for k, v in new.items() if k not in ('kanit_cumle', 'klinik_bilgi_en')},
                         {k: v for k, v in old.items() if k not in ('kanit_cumle', 'klinik_bilgi_en')})
        self.assertEqual(self.pages[1].count(new['kanit_cumle']), 1)

    @unittest.skipUnless(STATS.exists(), 'statistics yok')
    def test_v32_with_statistics_records(self):
        stats = json.loads(STATS.read_text(encoding='utf-8'))
        good, errors = validate_records(self.v32 + stats, self.pages)
        self.assertEqual((len(good), errors), (196, []))


if __name__ == '__main__':
    unittest.main()
