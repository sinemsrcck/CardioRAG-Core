"""data_quality istatistiksel tutarlılık kontrolleri. API/veritabanı yok; kaynak değerler değişmez.

Gerçek örnekler: pilot PDF'in Table 1/Table 2 satırları (pilot_tablo aktarım satırları) ve
Statistics bölümü kayıtları. Dosyalar yoksa bu testler atlanır."""
import copy
import hashlib
import json
import unittest
from pathlib import Path

from src.data_quality import (CHECKS, check_ci_p, check_model_terminology, check_pilot_row,
                              check_statistical_groups, config_payload, parse_p, run_checks)

PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')
V31 = Path('data/review/pilot/pilot_findings_v3_1.json')
STATS = Path('data/review/pilot/pilot_findings_v3_1_statistics.json')
NEW_CODES = ('ci_p_celiskisi', 'kategorik_p_yeniden_uretilemedi', 'surekli_p_supheli', 'model_etki_olcusu_celiskisi')


def hashed(rows, prefix='r'):
    out = []
    for i, row in enumerate(rows):
        row = dict(row)
        row.setdefault('document_id', 'D1')
        row['satir_hash'] = hashlib.sha256(f'{prefix}{i}{json.dumps(row, sort_keys=True, default=str)}'.encode()).hexdigest()
        out.append(row)
    return out


def ratio(deger, ci, p, etki='OR'):
    return hashed([dict(etki_turu=etki, deger=deger, ci95=ci, p=p)])[0]


def pair(a, b, p, n=(372, 853), satir=1):
    note = lambda n_own, n_other: json.dumps({'tablo_no': 1, 'satir_no': satir, 'grup_n': n_own, 'comparator_n': n_other})
    return hashed([dict(hasta_grubu='A', comparator='B', deger=a, p=p, taslak_notu=note(n[0], n[1])),
                   dict(hasta_grubu='B', comparator='A', deger=b, p=p, taslak_notu=note(n[1], n[0]))],
                  prefix=f's{satir}')


def levels(flags, code):
    return sorted({f['seviye'] for f in flags if f['kontrol_kodu'] == code})


class Units(unittest.TestCase):
    def test_codes_registered_and_versioned(self):
        for code in NEW_CODES:
            self.assertIn(code, CHECKS)
        self.assertEqual(config_payload()['kurallar'], 'kalite-v1-r5')   # r5: yüzde türü (percent_kind)

    def test_parse_p_keeps_ambiguous_unparsed(self):
        self.assertEqual(parse_p('<0.001'), ('<', 0.001))
        self.assertEqual(parse_p('p=0.03'), ('=', 0.03))
        self.assertEqual(parse_p('**0.007**'), ('=', 0.007))
        for text in ('<001', '', None, 'NS', '1.5'):
            self.assertIsNone(parse_p(text), text)

    def test_ci_p(self):
        # Kaynak tutarsızlığı: belirgin marjda 'uyari', sınırda 'bilgi'; hiçbir zaman 'hata' değil.
        self.assertEqual(levels(check_ci_p(ratio('0.852', '0.542-0.915', '0.679')), 'ci_p_celiskisi'), ['uyari'])
        self.assertEqual(levels(check_ci_p(ratio('1.273', '0.963-1.521', '0.032')), 'ci_p_celiskisi'), ['bilgi'])
        self.assertEqual(check_ci_p(ratio('3.124', '2.213-4.310', '<0.001')), [])
        self.assertEqual(check_ci_p(ratio('1.2', '0.9-1.5', '0.40', etki='HR')), [])
        auc = 'AUC / C-istatistigi'
        self.assertEqual(check_ci_p(ratio('0.55', '0.45-0.62', '0.30', etki=auc)), [])
        self.assertEqual(levels(check_ci_p(ratio('0.70', '0.60-0.80', '0.30', etki=auc)), 'ci_p_celiskisi'), ['uyari'])
        self.assertEqual(levels(check_ci_p(ratio('0.70', '0.52-0.80', '0.30', etki=auc)), 'ci_p_celiskisi'), ['bilgi'])
        self.assertEqual(check_ci_p(ratio('0.692', '0.512-0.753', '<001', etki='AUC / C-istatistigi')), [])
        self.assertEqual(check_ci_p(ratio('5', '1-3', '0.2', etki='diger')), [])

    def test_categorical(self):
        flags = check_statistical_groups(pair('167 (44.89)', '341 (39.97)', '0.007'))
        self.assertEqual(levels(flags, 'kategorik_p_yeniden_uretilemedi'), ['uyari'])
        self.assertEqual(len({f['grup_id'] for f in flags}), 1)
        self.assertEqual(levels(check_statistical_groups(pair('189 (50.8)', '435 (50.99)', '0.289')),
                                'kategorik_p_yeniden_uretilemedi'), ['bilgi'])
        self.assertEqual(check_statistical_groups(pair('85 (22.84)', '196 (22.97)', '0.943')), [])
        bad_pct = check_statistical_groups(pair('85 (30.00)', '196 (22.97)', '0.943'))
        self.assertIn('grup n', bad_pct[0]['aciklama'])
        self.assertTrue(all(f['seviye'] != 'hata' for f in bad_pct))

    def test_continuous_never_error(self):
        flags = check_statistical_groups(pair('28.44±1.45', '29.38±1.51', '0.349'))
        self.assertEqual(levels(flags, 'surekli_p_supheli'), ['uyari'])
        self.assertIn('varsayımlar doğrulanmadı', flags[0]['aciklama'])
        self.assertEqual(check_statistical_groups(pair('127.23±11.43', '125.18±12.21', '0.437')), [])
        self.assertEqual(check_statistical_groups(pair('28.44±1.45', '29.38±1.51', '0.349', n=(10, 12))), [])

    def test_model_terminology(self):
        cox = dict(kanit_cumle='Variables significant in the univariate Cox proportional regression were included.')
        logistic = dict(kanit_cumle='In multivariate logistic regression analysis, HCR was a predictor.')
        or_row = dict(etki_turu='OR', deger='3.216', ci95='2.334-4.365', p='<0.001')
        flags = check_model_terminology(hashed([cox, logistic, or_row]))
        self.assertEqual(levels(flags, 'model_etki_olcusu_celiskisi'), ['uyari'])
        self.assertEqual(len(flags), 3)
        hr_row = dict(etki_turu='HR', deger='1.5', ci95='1.1-2.0', p='0.01')
        self.assertEqual(check_model_terminology(hashed([cox, logistic, or_row, hr_row])), [])
        self.assertEqual(check_model_terminology(hashed([logistic, or_row])), [])
        other_doc = dict(cox, document_id='D2')
        self.assertEqual(check_model_terminology(hashed([other_doc, logistic, or_row])), [])


@unittest.skipUnless(PDF.exists() and V31.exists() and STATS.exists(), 'pilot PDF / v3.1 / statistics yok')
class PilotDeduplication(unittest.TestCase):
    """--pilot akışında aynı kaynak tutarsızlığı iki kodla işaretlenmez; kapsam korunur."""

    def codes_by_row(self, flags):
        out = {}
        for f in flags:
            out.setdefault(f['satir_hash'], set()).add(f['kontrol_kodu'])
        return out

    def test_ci_p_single_code_per_row(self):
        row = dict(ratio('0.852', '0.542-0.915', '0.679'), bilgi_turu='olcum', kanit_cumle='x', klinik_bilgi_en='x')
        plain = run_checks([row])
        pilot = run_checks([row], pilot=True) + check_pilot_row(row, {'pages': {}})
        self.assertIn('ci_p_celiskisi', self.codes_by_row(plain)[row['satir_hash']])
        codes = self.codes_by_row(pilot)[row['satir_hash']]
        self.assertIn('kaynak_ci_p_celiski', codes)
        self.assertNotIn('ci_p_celiskisi', codes)
        self.assertEqual(levels(pilot, 'kaynak_ci_p_celiski'), ['bilgi'])

    def test_pilot_ci_p_keeps_auc_and_skips_ambiguous_p(self):
        auc = 'AUC / C-istatistigi'
        hit = dict(ratio('0.70', '0.60-0.80', '0.30', etki=auc), kanit_cumle='x', klinik_bilgi_en='x')
        ambiguous = dict(ratio('0.692', '0.512-0.753', '<001', etki=auc), kanit_cumle='x', klinik_bilgi_en='x')
        self.assertEqual(levels(check_pilot_row(hit, {'pages': {}}), 'kaynak_ci_p_celiski'), ['bilgi'])
        self.assertEqual(levels(check_pilot_row(ambiguous, {'pages': {}}), 'kaynak_ci_p_celiski'), [])
        # Zehra'nın beklentileri: GA 1'i kapsıyor + p=0.002 -> bilgi; kapsamıyor + p<0.001 -> yok.
        self.assertEqual(levels(check_pilot_row(dict(ratio('1.254', '0.892-1.372', '0.002'), kanit_cumle='x',
                                                     klinik_bilgi_en='x'), {'pages': {}}), 'kaynak_ci_p_celiski'),
                         ['bilgi'])
        self.assertEqual(levels(check_pilot_row(dict(ratio('3.124', '2.213-4.310', '<0.001'), kanit_cumle='x',
                                                     klinik_bilgi_en='x'), {'pages': {}}), 'kaynak_ci_p_celiski'), [])

    def test_percent_arithmetic_single_code(self):
        rows = [dict(r, bilgi_turu='olcum', kanit_cumle='x', klinik_bilgi_en='x')
                for r in pair('85 (30.00)', '196 (22.97)', '0.943')]
        plain = check_statistical_groups(rows)
        self.assertEqual(levels(plain, 'kategorik_p_yeniden_uretilemedi'), ['uyari'])
        pilot_group = check_statistical_groups(rows, pilot=True)
        self.assertEqual(pilot_group, [])
        pilot_row = [f for r in rows for f in check_pilot_row(r, {'pages': {}})]
        self.assertEqual(levels(pilot_row, 'pilot_yuzde_uyumsuz'), ['uyari'])
        # Ki-kare yeniden hesabı pilot akışında da korunur (yüzdeler tutarlıysa).
        dm = [dict(r, bilgi_turu='tani') for r in pair('167 (44.89)', '341 (39.97)', '0.007')]
        self.assertEqual(levels(check_statistical_groups(dm, pilot=True), 'kategorik_p_yeniden_uretilemedi'), ['uyari'])

    def test_no_source_inconsistency_is_error(self):
        rows = pair('167 (44.89)', '341 (39.97)', '0.007') + pair('28.44±1.45', '29.38±1.51', '0.349', satir=2)
        rows += [ratio('0.852', '0.542-0.915', '0.679'), ratio('1.185', '0.136-1.788', '0.003')]
        stat_codes = set(NEW_CODES) | {'kaynak_ci_p_celiski'}
        self.assertFalse([f for f in run_checks(rows) if f['kontrol_kodu'] in stat_codes and f['seviye'] == 'hata'])


@unittest.skipUnless(PDF.exists() and V31.exists() and STATS.exists(), 'pilot PDF / v3.1 / statistics yok')
class RealPilotTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import src.pilot_tablo as pt
        from src.pilot_clinical import pdf_pages
        doc, pages = pdf_pages(PDF)
        records, _ = pt.load_records(V31)
        transfer = pt.enrich(records, pages, doc['document_id'])['aktarim']
        cls.rows = hashed([dict(r, document_id=doc['document_id']) for r in transfer], prefix='t')
        cls.v31 = json.loads(V31.read_text(encoding='utf-8'))
        cls.stats = json.loads(STATS.read_text(encoding='utf-8'))
        cls.doc_id = doc['document_id']

    def note(self, row):
        return json.loads(row['taslak_notu'])

    def test_table2_ci_p(self):
        found = {}
        for row in self.rows:
            if self.note(row)['tablo_no'] == 2:
                for f in check_ci_p(row):
                    found[(row['sonuc_olcutu_tr'], self.note(row)['analiz'])] = f['seviye']
        self.assertEqual(found, {('Age', 'univariate'): 'uyari', ('Age', 'multivariate'): 'uyari',
                                 ('Heart rate', 'univariate'): 'uyari', ('CHA2DS2-vASc score', 'univariate'): 'uyari',
                                 ('LA size', 'multivariate'): 'uyari', ('LA size', 'univariate'): 'bilgi'})

    def test_table1_groups(self):
        flags = check_statistical_groups([r for r in self.rows if self.note(r)['tablo_no'] == 1])
        by_var = {}
        for f in flags:
            row = next(r for r in self.rows if r['satir_hash'] == f['satir_hash'])
            by_var.setdefault(f['kontrol_kodu'], {})[self.note(row)['degisken']] = f['seviye']
        categorical = by_var['kategorik_p_yeniden_uretilemedi']
        self.assertEqual([v for v, level in categorical.items() if level == 'uyari'], ['Diabetes mellitus'])
        self.assertEqual(categorical['Female gender'], 'bilgi')
        self.assertNotIn('Hyperlipidemia', categorical)
        continuous = by_var['surekli_p_supheli']
        self.assertEqual(set(continuous), {'ALT', 'AST', 'Haemoglobin', 'WBC count', 'platelet', 'LvEF',
                                           'LvDD', 'LvDS'})
        self.assertEqual(set(continuous.values()), {'uyari'})
        self.assertNotIn('hata', {f['seviye'] for f in flags})

    def test_model_conflict_with_statistics_records(self):
        text_rows = hashed([dict(r, document_id=self.doc_id) for r in self.stats + self.v31
                            if r.get('kanit_turu') != 'tablo'], prefix='x')
        table2 = [r for r in self.rows if self.note(r)['tablo_no'] == 2]
        flags = check_model_terminology(text_rows + table2)
        self.assertEqual(levels(flags, 'model_etki_olcusu_celiskisi'), ['uyari'])
        involved = {f['satir_hash'] for f in flags}
        cox = [r for r in text_rows if 'Cox' in r['kanit_cumle']]
        logistic = [r for r in text_rows if 'logistic regression' in r['kanit_cumle']]
        self.assertEqual(len(cox), 2)
        self.assertTrue({r['satir_hash'] for r in cox + logistic} <= involved)
        self.assertEqual(flags[0]['ayrinti']['raporlanan_etki_olcusu'], 'OR')
        # Statistics kayıtları olmadan çelişki görünmez (yalnız lojistik/OR).
        self.assertEqual(check_model_terminology([r for r in text_rows if r not in cox] + table2), [])

    def test_pilot_flow_flags_each_ci_p_conflict_once(self):
        import src.pilot_tablo as pt
        from src.data_quality import run_pilot_checks
        from src.pilot_clinical import pdf_pages
        _, pages = pdf_pages(PDF)
        sources = {self.doc_id: {'dosya': PDF.name, 'pages': pages, 'tables': pt.parse_tables(pages)}}
        pilot_flags, _ = run_pilot_checks(self.rows, sources)
        flags = run_checks(self.rows, pilot=True) + pilot_flags
        ci_codes = {}
        for f in flags:
            if f['kontrol_kodu'] in ('ci_p_celiskisi', 'kaynak_ci_p_celiski'):
                ci_codes.setdefault(f['satir_hash'], []).append(f['kontrol_kodu'])
        self.assertEqual(len(ci_codes), 6)
        self.assertTrue(all(codes == ['kaynak_ci_p_celiski'] for codes in ci_codes.values()))
        self.assertFalse([f for f in flags if f['kontrol_kodu'] == 'kategorik_p_yeniden_uretilemedi'
                          and 'grup n' in f['aciklama']])

    def test_run_checks_leaves_source_values_unchanged(self):
        rows = copy.deepcopy(self.rows)
        flags = run_checks(rows)
        self.assertEqual(rows, self.rows)
        codes = {f['kontrol_kodu'] for f in flags}
        self.assertTrue({'ci_p_celiskisi', 'kategorik_p_yeniden_uretilemedi', 'surekli_p_supheli'} <= codes)

    def test_statistics_records(self):
        from src.pilot_clinical import pdf_pages, validate_records
        _, pages = pdf_pages(PDF)
        good, errors = validate_records(self.stats, pages)
        self.assertEqual((len(good), errors), (6, []))
        self.assertEqual({(r['bilgi_turu'], r['baglam'], r['sayfa']) for r in self.stats},
                         {('diger', 'study_method', 3)})
        self.assertFalse(any('review_status' in r for r in self.stats))
        v31_quotes = {r['kanit_cumle'] for r in self.v31}
        self.assertFalse(any(r['kanit_cumle'] in v31_quotes for r in self.stats))
        combined, errors = validate_records(self.v31 + self.stats, pages)
        self.assertEqual([e['satir'] for e in errors], [6])  # yalnız bilinen v3 yükleyici kalıntısı


if __name__ == '__main__':
    unittest.main()
