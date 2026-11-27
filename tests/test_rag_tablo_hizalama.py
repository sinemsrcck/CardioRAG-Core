"""Tablo hizalama (src.rag_tablo_hizalama), hizalanmış kayıt tekrar ayıklaması (rag_klinik_cikarim.
drop_aligned_duplicates) ve SQL aktarım adaptörü (src.rag_aktarim) için GENEL regresyon testleri.

Sentetik tablolar; makale adı, yazar adı ya da gerçek makale değeri yok. LLM yerine sahte istemci (tests.
test_rag_klinik_cikarim.table_run), veritabanı yok. Çalıştırma: python -m unittest tests.test_rag_tablo_hizalama
"""
import json
import unittest

from src import rag_aktarim as A
from src.rag_tablo_hizalama import HOLD_STAGE, KATSAYI_STAGE
from tests.test_rag_klinik_cikarim import table_run

META = {'cikti_dizini': 'data/rag_cikarim/x/20260101-000000', 'model': 'm', 'pdf': 'x.pdf'}


def rec(fact, row, value=None, **kw):
    out = dict(bilgi_turu=kw.pop('bilgi_turu', 'klinik_sonuc'), baglam='study_finding', klinik_bilgi_en=fact,
               kanit_cumle=row, sayfa=1, kanit_turu='tablo', **kw)
    if value is not None:
        out['deger'] = value
    return out


def run(page, records):
    (valid, rejected, calls), _ = table_run({1: page}, {1: []}, {1: records})
    assert calls >= 0
    return valid, rejected


def stages(rejected):
    return sorted(r['asama'] for r in rejected)


def draft(valid, page):
    grouped = __import__('src.rag_klinik_cikarim', fromlist=['grouped_rows']).grouped_rows(valid)
    from src.rag_klinik_cikarim import categories_of
    return A.build_draft([{**r, 'kategoriler': categories_of(r, grouped)} for r in valid], {1: page}, META, [])


def note_of(candidate):
    note = candidate['taslak_notu']
    return json.loads(note) if isinstance(note, str) else note


# Regresyon tablosu: alt alta yazılmış birleşik sütun (katsayı / etki / p), başlıkta tek analiz etiketi.
REG_ROWS = ('|X1|0.512<br>1.669<br>0.004|', '|X2|0.020<br>1.020<br>0.310|')
REG_PAGE = ('**Table 4:** Multivariate logistic regression analysis for outcome\n'
            '|**Variable**|**B**<br>**Exp (B) (OR)**<br>**p value**|\n|---|---|\n' + '\n'.join(REG_ROWS) + '\n')
# Tek / çok değişkenli analiz: iki etki sütunu, iki aynı adlı p sütunu, GA uzun tireyle.
UM_ROW = '|Marker A|1.40 (1.10–1.80)|0.010|1.25 (0.95–1.60)|0.090|'
UM_PAGE = ('**Table 5:** Univariate and multivariate analysis\n|**Variable**|**Univariate analysis**<br>**Odds ratio (95% CI)**'
           '|**p value**|**Multivariate analysis**<br>**Odds ratio (95% CI)**|**p value**|\n|---|---|---|---|---|\n'
           + UM_ROW + '\n|Marker B|2.10 (1.30–3.40)|0.002|1.90 (1.10–3.20)|0.020|\n')
# Grup tablosu: alt alta yazılmış alt satırlar (ad ve değer hücreleri aynı sayıda parça).
SX_ROW = '|Level 1<br>Level 2|40<br>60|25<br>35|15<br>25|0.410|'
SX_PAGE = ('**Table 6:** Distribution\n|**Category**|**Total**|**Group A**|**Group B**|**p value**|\n|---|---|---|---|---|\n'
           + SX_ROW + '\n|Score (mm)|12.1±2.0|11.8±1.9|12.5±2.2|0.200|\n')
SCORE_ROW = '|Score (mm)|12.1±2.0|11.8±1.9|12.5±2.2|0.200|'
# Tanısal performans tablosu: testler sütunlarda, ölçüler satırlarda, ±SE ve ikili karşılaştırma.
DG_PAGE = ('**Table 7:** Diagnostic performance\n||**Test interval 1**|**Test interval 2**|\n|---|---|---|\n'
           '||**( A )**|**( B )**|\n|Sensitivity|75.0%|80.0%|\n|Specificity|79.8%|85.1%|\n'
           '|AUC±SE.|0.755 ± 0.072|0.809 ± 0.074|\n|Pairwise comparison for AUC|P (A-B)=0.291||\n'
           '|_ROC analysis. AUC: area under the ROC curve; SE: standard error._|||\n')
# p sütununda etki ölçüsü ('or' işareti) ve test işareti ('u').
PE_ROWS = ('|Feature X|30 (25.0)|10 (10.0)|8.4 (2.1-33.0)<br>or|', '|Feature Y|50 (41.7)|45 (45.0)|0.620<br>u|')
PE_PAGE = ('**Table 8:** Comparison\n|**Variable**|**Cases (n=120)**|**Controls (n=100)**|**P**|\n|---|---|---|---|\n'
           + '\n'.join(PE_ROWS) + '\n|_u Mann Whitney u-test, or Odds Ratio (95% Confidence Interval)_||||\n')
# Literatür / özet tablosu (satır adları çalışma kimliği) ve yapısal olarak karmaşık tablo (sayısal satır adı).
LIT_ROWS = ('|Smith, 2015|Country A|210|61.0|', '|Lee, 2018|Country B|95|58.5|', '|Kim, 2020|Country C|130|63.2|')
LIT_PAGE = ('**Table 9:** Characteristics of selected studies\n|**Study**|**Country**|**No. in sample**|**Mean age, years**|'
            '\n|---|---|---|---|\n' + '\n'.join(LIT_ROWS) + '\n')
CX_ROWS = ('|1|Predictor P|72.1%|Training|', '|2|Predictor Q|65.0%|Training|')
CX_PAGE = ('**Table 10:** Models\n|**Model**|**Independent Variable**|**Normalized Importance**|**Sample**|\n|---|---|---|---|\n'
           + '\n'.join(CX_ROWS) + '\n')
# Nitel etki tablosu (yalnız metin değerli sütun): pilot modeli satır üretmez, kayıt değişmeden kalır.
QL_ROW = '|Risk factor R|Intervention I|Decreased|'
QL_PAGE = '**Table 11:** Effects\n|**Risk factor**|**Recommendation**|**Symptoms**|\n|---|---|---|\n' + QL_ROW + '\n'


class CoefficientTests(unittest.TestCase):
    """B katsayısı OR olarak yorumlanmaz; özgün değeri ve anlamı korunur."""

    def records(self):
        return [rec('X1', REG_ROWS[0], '0.512<br>1.669<br>0.004')]       # split_stacked_cell -> B ve Exp(B) kayıtları

    def test_coefficient_attached_to_effect_record_not_or(self):
        valid, rejected = run(REG_PAGE, self.records())
        self.assertEqual([(r['deger'], r['etki_turu'], r['p']) for r in valid], [('1.669', 'OR', '0.004')], rejected)
        model = valid[0]['tablo_modeli']
        self.assertEqual((model['analiz'], model['regresyon_katsayisi'], model['katsayi_sutunu'], model['sutun']),
                         ('multivariate', '0.512', 'B', 'Exp (B) (OR)'))
        coef = [r for r in rejected if r['asama'] == KATSAYI_STAGE]
        self.assertEqual([r['kayit']['deger'] for r in coef], ['0.512'])                # silinmez, rapora yazılır
        note = note_of(draft(valid, REG_PAGE)['adaylar'][0])
        self.assertEqual((note['analiz'], note['regresyon_katsayisi'], note['sutun']), ('multivariate', '0.512', 'Exp (B) (OR)'))

    def test_coefficient_without_effect_record_kept_as_coefficient(self):
        b_only = [rec('X1', REG_ROWS[0], '0.512', sonuc_olcutu_tr='B')]
        valid, rejected = run(REG_PAGE, b_only)
        self.assertEqual(len(valid), 1, rejected)
        self.assertEqual(valid[0]['tablo_modeli']['durum'], 'katsayi')
        out = draft(valid, REG_PAGE)['adaylar'][0]
        note = note_of(out)
        self.assertEqual(out['etki_turu'], 'diger')                                     # OR değil
        self.assertEqual((note['istatistik'], note['regresyon_katsayisi'], note['katsayi_sutunu']),
                         ('regresyon katsayısı (B)', '0.512', 'B'))

    def test_adapter_uses_line_of_stacked_header_without_model(self):
        # Tek satırlı tablo: model birleşik sütunu bölmez; adaptör yine aynı sıradaki başlık satırını kullanır.
        page = '**Table 4:** Regression\n|**Variable**|**B**<br>**Exp (B) (OR)**<br>**p value**|\n|---|---|\n' + REG_ROWS[0] + '\n'
        valid, rejected = run(page, self.records())
        out = {r['deger']: r for r in draft(valid, page)['adaylar']}
        self.assertEqual((out['0.512']['etki_turu'], note_of(out['0.512'])['sutun']), ('diger', 'B'))
        self.assertEqual((out['1.669']['etki_turu'], note_of(out['1.669'])['sutun']), ('OR', 'Exp (B) (OR)'))


class AnalysisAndPTests(unittest.TestCase):
    """Tek / çok değişkenli ayrımı, p sütununun sahibi, GA ayrımı ve bağımsız p kayıtları."""

    def test_analysis_label_p_owner_and_ci_kept_in_source_characters(self):
        recs = [rec('Marker A', UM_ROW, '1.40 (1.10–1.80)', sonuc_olcutu_tr='Univariate analysis Odds ratio (95% CI)'),
                rec('Marker A', UM_ROW, '1.25 (0.95–1.60)', sonuc_olcutu_tr='Multivariate analysis Odds ratio (95% CI)')]
        valid, rejected = run(UM_PAGE, recs)
        got = sorted((r['tablo_modeli']['analiz'], r['deger'], r['ci95'], r['p'], r['etki_turu'],
                      r['tablo_modeli']['p_sutun']) for r in valid)
        self.assertEqual(got, [('multivariate', '1.25', '0.95–1.60', '0.090', 'OR', 'p value [Multivariate analysis]'),
                               ('univariate', '1.40', '1.10–1.80', '0.010', 'OR', 'p value [Univariate analysis]')],
                         rejected)
        self.assertTrue(all(r['kanit_cumle'] == UM_ROW for r in valid))                # ham kanıt değişmez
        notes = sorted(note_of(r)['analiz'] for r in draft(valid, UM_PAGE)['adaylar'])
        self.assertEqual(notes, ['multivariate', 'univariate'])

    def test_p_record_dropped_only_when_owner_carries_same_p(self):
        recs = [rec('Marker A', UM_ROW, '1.40 (1.10–1.80)', sonuc_olcutu_tr='Univariate analysis Odds ratio (95% CI)'),
                rec('Marker A', UM_ROW, '0.010', sonuc_olcutu_tr='p value'),            # tek değişkenli p: sahibi var
                rec('Marker A', UM_ROW, '0.090', sonuc_olcutu_tr='p value')]            # çok değişkenli p: sahibi yok
        valid, rejected = run(UM_PAGE, recs)
        self.assertEqual(sorted(r['deger'] for r in valid), ['0.090', '1.40'], rejected)
        self.assertEqual([(r['asama'], r['kayit']['deger']) for r in rejected], [('etki_p_tekrari', '0.010')])
        kept_p = next(r for r in valid if r['deger'] == '0.090')
        self.assertEqual(kept_p['tablo_modeli']['p_sutun'], 'p value [Multivariate analysis]')

    def test_conflicting_p_held_not_corrected(self):
        bad = [rec('Marker A', UM_ROW, '1.40 (1.10–1.80)', p='0.090')]               # p başka analizin sütunundan
        valid, rejected = run(UM_PAGE, bad)
        self.assertEqual(valid, [])
        self.assertEqual(stages(rejected), [HOLD_STAGE])
        self.assertIn('tutarsızlığı', rejected[0]['neden'])
        self.assertEqual(rejected[0]['kayit']['p'], '0.090')                            # kayıt değiştirilmeden saklanır


class StackedRowTests(unittest.TestCase):
    """Birleşik alt satır hücreleri: eşdeğerlik doğrulanmadan tekrar sayılmaz."""

    def split(self, group, values):
        return [rec(name, SX_ROW, value, hasta_grubu=group, p='0.410', bilgi_turu='olcum')
                for name, value in zip(('Level 1', 'Level 2'), values)]

    def unsplit(self, group, value, **kw):
        return rec('Level 1<br>Level 2', SX_ROW, value, hasta_grubu=group, bilgi_turu='olcum', **kw)

    def test_unsplit_dropped_only_when_every_line_has_equivalent_split_record(self):
        recs = self.split('Group A', ('25', '35')) + [self.unsplit('Group A', '25<br>35', p='0.410')]
        valid, rejected = run(SX_PAGE, recs)
        self.assertEqual(sorted(r['klinik_bilgi_en'] for r in valid), ['Level 1', 'Level 2'], rejected)
        self.assertEqual(stages(rejected), ['bolunmemis_tekrar'])

    def test_unsplit_held_when_equivalence_missing_or_group_differs(self):
        recs = self.split('Group A', ('25', '35'))[:1] + [self.unsplit('Group A', '25<br>35')]   # Level 2 eksik
        valid, rejected = run(SX_PAGE, recs)
        self.assertEqual([r['klinik_bilgi_en'] for r in valid], ['Level 1'])
        self.assertEqual(stages(rejected), [HOLD_STAGE])
        self.assertIn('eşdeğer bölünmüş kayıt yok', rejected[0]['neden'])
        recs = self.split('Group A', ('25', '35')) + [self.unsplit('Group B', '15<br>25')]       # başka grup
        valid, rejected = run(SX_PAGE, recs)
        self.assertEqual(len(valid), 2)
        self.assertEqual(stages(rejected), [HOLD_STAGE])

    def test_split_records_carry_model_row_and_column(self):
        valid, _ = run(SX_PAGE, self.split('Group A', ('25', '35')))
        self.assertEqual(sorted((r['tablo_modeli']['satir_no'], r['tablo_modeli']['sutun']) for r in valid),
                         [(1, 'Group A'), (2, 'Group A')])


class RowLabelAndUnitTests(unittest.TestCase):
    def test_value_less_row_label_dropped_only_when_row_values_present(self):
        label = rec('Score (mm)', SCORE_ROW, bilgi_turu='olcum')
        value = rec('Score (mm)', SCORE_ROW, '11.8±1.9', hasta_grubu='Group A', bilgi_turu='olcum')
        valid, rejected = run(SX_PAGE, [label, value])
        self.assertEqual([r.get('deger') for r in valid], ['11.8±1.9'], rejected)
        self.assertEqual(stages(rejected), ['satir_etiketi'])
        valid, rejected = run(SX_PAGE, [label])                                          # değer kaydı yok: kalır
        self.assertEqual(len(valid), 1, rejected)
        treat = rec('Score (mm)', SCORE_ROW, bilgi_turu='tedavi')
        valid, rejected = run(SX_PAGE, [treat, value])                                   # müdahale türü kalır
        self.assertEqual(len(valid), 2, rejected)

    def test_unit_from_row_label_filled_when_stated_in_source(self):
        value = rec('Score (mm)', SCORE_ROW, '11.8±1.9', hasta_grubu='Group A', bilgi_turu='olcum')
        valid, rejected = run(SX_PAGE, [value])
        self.assertEqual(valid[0]['unit'], 'mm', rejected)
        self.assertIn('tablo_modeli:unit', valid[0]['otomatik_duzeltme'])


class DiagnosticAndPColumnEffectTests(unittest.TestCase):
    def test_diagnostic_table_test_measure_se_and_pairwise(self):
        recs = [rec('Sensitivity', '|Sensitivity|75.0%|80.0%|', '75.0%', bilgi_turu='olcum'),
                rec('AUC±SE.', '|AUC±SE.|0.755 ± 0.072|0.809 ± 0.074|', '0.755 ± 0.072', bilgi_turu='olcum'),
                rec('Pairwise comparison for AUC', '|Pairwise comparison for AUC|P (A-B)=0.291||', 'P (A-B)=0.291',
                    bilgi_turu='olcum')]
        valid, rejected = run(DG_PAGE, recs)
        by = {r['klinik_bilgi_en']: r for r in valid}
        self.assertEqual(set(by), {'Sensitivity', 'AUC±SE.', 'Pairwise comparison for AUC'}, rejected)
        sens = by['Sensitivity']
        self.assertEqual((sens['mudahale_en'], sens['outcome_type'], sens['etki_turu']),
                         ('Test interval 1', 'tanısal performans', 'oran / yuzde'))
        auc = by['AUC±SE.']
        self.assertEqual((auc['deger'], auc['etki_turu'], auc['tablo_modeli']['se']), ('0.755', 'AUC / C-istatistigi', '0.072'))
        pair = by['Pairwise comparison for AUC']
        self.assertEqual((pair.get('deger'), pair['p'], pair['mudahale_en'], pair['comparator']),
                         (None, '0.291', 'Test interval 1', 'Test interval 2'))

    def test_effect_in_p_column_and_test_mark(self):
        recs = [rec('Feature X', PE_ROWS[0], '8.4 (2.1-33.0)<br>or', bilgi_turu='olcum'),
                rec('Feature Y', PE_ROWS[1], '50 (41.7)', hasta_grubu='Cases (n=120)', p='0.620<br>u', bilgi_turu='olcum')]
        valid, rejected = run(PE_PAGE, recs)
        by = {r['klinik_bilgi_en']: r for r in valid}
        x = by['Feature X']
        self.assertEqual((x['deger'], x['ci95'], x['etki_turu'], x['tablo_modeli']['etki_kaynagi']),
                         ('8.4', '2.1-33.0', 'OR', 'p_sutunu'), rejected)
        self.assertEqual(by['Feature Y']['tablo_modeli']['p_test_isareti'], 'u')


class UnreliableTableTests(unittest.TestCase):
    def test_literature_table_held_by_alignment_then_resolved_records_kept(self):
        # Hizalama literatür tablosunu modellemez (gerekçeli inceleme); ardından çalışma özeti modeli
        # (src.rag_tablo_ozel) hücreleri çalışma kimliğiyle çözer. LLM kayıtları silinmez, önceki aşamaları korunur.
        recs = [rec(row.split('|')[1], row, row.split('|')[3], bilgi_turu='olcum') for row in LIT_ROWS]
        valid, rejected = run(LIT_PAGE, recs)
        self.assertEqual(stages(rejected), ['tablo_ozel_degisimi'] * 3)
        self.assertTrue(all(r['onceki_asama'] == HOLD_STAGE and 'literatür' in r['neden'] for r in rejected))
        self.assertEqual(sorted(r['kayit']['deger'] for r in rejected), ['130', '210', '95'])
        got = sorted((r['tablo_modeli']['kaynak_calisma']['ad'], r['sonuc_olcutu_tr'], r['deger']) for r in valid)
        self.assertIn(('Smith', 'No. in sample', '210'), got)
        self.assertIn(('Kim', 'Mean age, years', '63.2'), got)
        self.assertTrue(all(r['tablo_modeli']['kaynak_calisma']['ozellikler'].get('Country') for r in valid))
        self.assertTrue(all(r['baglam'] == 'literature_background' and 'hasta_grubu' not in r for r in valid))

    def test_structurally_complex_table_held(self):
        recs = [rec('Predictor P', CX_ROWS[0], '72.1%', sonuc_olcutu_tr='Normalized Importance', bilgi_turu='olcum')]
        valid, rejected = run(CX_PAGE, recs)
        self.assertEqual((valid, stages(rejected)), ([], [HOLD_STAGE]))
        self.assertIn('sayısal satır adı', rejected[0]['neden'])

    def test_text_valued_qualitative_table_left_unchanged(self):
        recs = [rec('Risk factor R', QL_ROW, 'Decreased', sonuc_olcutu_tr='Symptoms')]
        valid, rejected = run(QL_PAGE, recs)
        self.assertEqual(len(valid), 1, rejected)
        self.assertNotIn('tablo_modeli', valid[0])


class AdapterDuplicateAndHashTests(unittest.TestCase):
    def test_possible_duplicate_requires_no_conflicting_value_group_or_statistic(self):
        base = {'kanit_turu': 'metin', 'sayfa': 1, 'kanit_cumle': 'Event rate was 12% in group A and 20% in group B.',
                'klinik_bilgi_en': 'Event rate was 12% in group A', 'bilgi_turu': 'olcum', 'baglam': 'study_finding'}
        existing = [{'id': 7, 'kanit_sayfa': 1, 'kanit_cumle': base['kanit_cumle'], 'klinik_bilgi_en':
                     'Event rate was 12% in group A', 'deger': '12%', 'hasta_grubu': 'group A', 'bilgi_turu': 'olcum',
                     'baglam': 'study_finding'}]
        self.assertEqual(A.classify_against_existing({**base, 'deger': '12%', 'hasta_grubu': 'group A',
                                                      'p': '0.01'}, existing), ('olasi', [7]))
        self.assertEqual(A.classify_against_existing({**base, 'deger': '20%', 'hasta_grubu': 'group B'}, existing),
                         ('yeni', None))                                                # farklı değer / grup
        self.assertEqual(A.classify_against_existing({**base, 'deger': '12%', 'etki_turu': 'OR'},
                                                     [dict(existing[0], etki_turu='HR')]), ('yeni', None))

    def test_changed_records_get_new_hash_and_old_decisions_do_not_carry(self):
        raw = rec('Marker A', UM_ROW, '1.40 (1.10–1.80)', sonuc_olcutu_tr='Univariate analysis Odds ratio (95% CI)')
        valid, _ = run(UM_PAGE, [raw])
        new = A.serializable(draft(valid, UM_PAGE)['adaylar'][0])
        old_like = dict(new, deger='1.40 (1.10–1.80)', ci95=None)
        self.assertNotEqual(A.satir_hash('d' * 64, new), A.satir_hash('d' * 64, old_like))
        self.assertEqual((new['deger'], new['ci95'], new['review_status']), ('1.40', '1.10–1.80', 'pending'))
        self.assertEqual(new['kanit_cumle'], UM_ROW)


if __name__ == '__main__':
    unittest.main()
