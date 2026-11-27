"""src.rag_tablo_ozel genel regresyon testleri. Sentetik tablolar (makale / yazar adı ya da gerçek değer yok);
LLM / API / embedding / veritabanı kullanılmaz."""
import json
import unittest

from src import rag_tablo_ozel as O

# --- 1. Satır yönelimli çalışma özeti (alt başlık, iki satırlık grup hücresi, '–', sözlükte 'NA' = grup adı) -----
ROW_STUDY_PAGE = '\n'.join([
    'Table 1 Characteristics of the sample reported in the selected studies',
    '',
    '|**Study**||**Characteri**|**stics**||',
    '|---|---|---|---|---|',
    '||**Country**|**No. in**<br>**sample**|**Mean age,**<br>**years**|**Survival, %**|',
    '|Adult cases|||||',
    '|Smith, 2015_(4)_|Northland|120|50 (XA); 60|10|',
    '|||||(YB)||',
    '|Jones, 2016_(5)_<sup>_a_</sup>|Southland|80|–|10|',
    '|Brown, 2017_(6)_|Northland|45|41 (XA); 39|7 (NA); 9|',
    '||||(NA)|(XA)|',
    '|Paediatric cases|||||',
    '|Smith, 2018_(7)_|Eastland|30|6|0|',
    '',
    '_XA: group alpha; YB: group beta; NA: northern ancestry. N-dash (–): not reported. aPre-print data._',
])
# Satır 7'deki devam satırı 5 hücreli tabloda 6 hücre içeriyor -> düzelt: aynı hücre sayısı.
ROW_STUDY_PAGE = ROW_STUDY_PAGE.replace('|||||(YB)||', '||||(YB)||')

# --- 2. Sütun yönelimli çalışma özeti -------------------------------------------------------------------------
COL_STUDY_PAGE = '\n'.join([
    '**Table 2.** Risks of outcomes in published studies',
    '',
    '||**Doe et al**<sup>**3**</sup>||**Roe et al**<sup>**8**</sup>|',
    '|---|---|---|---|',
    '|Study design|Meta-analysis||Cohort|',
    '|**Outcome**|**Relative risk (95% CI)**|**Rate per 1000**<sup>**_a_**</sup>|**5-y risk, %**|',
    '|Stroke|2.10 (1.80–2.40)|3.3|6.6|',
    '|Heart failure|3.30 (2.10–4.40)<sup>b</sup>|9.9||',
    '',
    'aRate data are unavailable for Roe et al. bAdjusted estimate.',
])

# --- 3. Öneri tablosu (sınıf / kanıt düzeyi / metin; alt başlık, virgüllü ve cümle ortası ön koşul) --------------
REC_PAGE = '\n'.join([
    '**Table 3.** Indications for imaging [9].',
    '',
    '|**COR**|**LOE**|**Indications**|',
    '|---|---|---|',
    '|||Acute Symptoms|',
    '|1|A|For high-risk patients, imaging X is useful for diagnosis.|',
    '|||For patients with known disease categorized as low risk,|',
    '|2a|B-R|test Y is reasonable as a first-line test.|',
    '|||For patients who have symptoms with previous|',
    '|2a|B-NR|surgery, imaging X is reasonable to evaluate graft patency.|',
    '',
    'Abbreviations: COR, class of recommendation; LOE, level of evidence.',
])

# --- 4. Test × tanısal ölçü × madde listesi (madde işareti ayrı sütuna kaymış) ---------------------------------
TEST_PAGE = '\n'.join([
    '**Table 4.** Testing options.',
    '',
    '|||||**Testing**||',
    '|---|---|---|---|---|---|',
    '||**SEN**|**SPE**||**Advantages**|**Limitations**|',
    '|TEST A|90%|80%|_•_<br>_•_|Fast<br>Widely available|_•_<br>Radiation exposure<br>_•_<br>Higher cost|',
    '|TEST B|70%|75%|_•_<br>_•_|Cheap<br>No contrast|_•_<br>Lower accuracy|',
    '',
    'Abbreviations: SEN, sensitivity; SPE, specificity.',
])

# --- 5. Yan yana bağımsız bölümler (değişken önemi + sınıflandırma matrisi) ------------------------------------
def side_page(a_present='0', overall='82.5%'):
    return '\n'.join([
        '##### **Table 5: Models for prediction of event.**',
        '',
        '||**Variable Importa**|**nce**|**Sample (Holdout)**||**Predicted E**|**vent**|',
        '|---|---|---|---|---|---|---|',
        '|**Model**|**Independent Variable**|**Normalized**<br>**Importance**|**Training (%100)**|**Absent**|**Present**|**Percent Correct**|',
        f'||Marker change A|75.4%|Event Absent|94|{a_present}|100.0%|',
        '|1|Marker change B|72.1%|Event Present|20|0|0.0%|',
        f'||Marker change C|100.0%|Overall Percent|100.0%|0.0%|{overall}|',
        '||Cut off A|39.0%|Event Absent|87|7|92.6%|',
        '|2|Cut off B|38.6%|Event Present|5|15|75.0%|',
        '||Cut off C|100.0%|Overall Percent|80.7%|19.3%|89.5%|',
        '|_Network_<br>_note_|_hidden layer_|_activation_|_tanh_|_output_|_layer_|_identity_|',
    ])


# --- 6. Satır yönelimli RKÇ tablosu (kollar, birincil sonuç hücresi) -------------------------------------------
RCT_PAGE = '\n'.join([
    '**Table 6.** Randomized Clinical Trials of Procedure Z for Disease Q',
    '',
    '|**Source**|**Participants, No.**|**Control**|**Primary outcome**|**Length of**<br>**follow-up,**<br>**mo**|',
    '|---|---|---|---|---|',
    '|ALPHA-1,<sup>11</sup>2005|Procedure: 30 Control:<br>35|DRG|Recurrence Procedure: 4 (13%) DRG: 20<br>(57%) (P < .001)|12|',
    '|BETA,<sup>12</sup>2019|Procedure: 100<br>Control: 98|DRG|Death or stroke Procedure: 9 (9%) Control: 12 (12.2%) (HR,<br>0.74 [95% CI, 0.31–1.15]; P = .30)|Median,<br>40.5|',
    '|GAMMA,<sup>13</sup>2010|Procedure + DRG: 50<br>Control: 50|DRG|Freedom Procedure: 40 (80%)<br>DRG: 30 (60%) (P = .05)|12|',
    '',
    'Abbreviations: DRG, standard drug.',
])


def page_of(text):
    return {1: text}


def built(pages):
    return {name: (table, recs, holds) for table, name, recs, holds in O.build(pages)}


class StudyIdentityTests(unittest.TestCase):
    def test_parse_study_variants(self):
        cases = {'Smith, 2015_(4)_': ('Smith', '2015', ['4'], None),
                 'Jones, 2016_(5)_<sup>_a_</sup>': ('Jones', '2016', ['5'], 'a'),
                 'Lee, 2017<sup>b</sup>': ('Lee', '2017', [], 'b'),
                 'Kim, 2018_(28)_b': ('Kim', '2018', ['28'], 'b'),
                 'ALPHA-1,<sup>11</sup>2005': ('ALPHA-1', '2005', ['11'], None),
                 'TRIAL X,<sup>12</sup><br>2010': ('TRIAL X', '2010', ['12'], None),
                 'Doe et al<sup>3</sup>': ('Doe et al', None, ['3'], None)}
        for cell, (name, year, refs, mark) in cases.items():
            with self.subTest(cell=cell):
                s = O.parse_study(cell)
                self.assertEqual((s['ad'], s['yil'], s['kaynakca_no'], s['dipnot_isareti']), (name, year, refs, mark))
        for cell in ('Age', 'EXERCISE ECG', 'Overweight or<br>obesity', '1', 'Event Absent'):
            self.assertIsNone(O.parse_study(cell))

    def test_glossary_meaning_overrides_generic_missing_markers(self):
        glossary = {'NA': 'northern ancestry', 'N-dash (–)': 'not reported'}
        glossary, _, _ = O.notes_below(['_XA: group alpha; NA: northern ancestry. N-dash (–): not reported._'], 0)
        self.assertFalse(O.is_missing('NA', glossary))                       # tabloda grup adı
        self.assertTrue(O.is_missing('–', glossary))
        self.assertTrue(O.is_missing('NA', {}))                              # sözlük yoksa genel kural
        self.assertTrue(O.is_missing('NA', {'NA': 'not available'}))
        self.assertFalse(O.is_missing('0', glossary))


class RowStudyTableTests(unittest.TestCase):
    def setUp(self):
        self.pages = page_of(ROW_STUDY_PAGE)
        self.table, self.recs, self.holds = built(self.pages)['calisma_satir']

    def by(self, study, column, year=None):
        years = {'Smith': '2015', 'Jones': '2016', 'Brown': '2017'}
        return [r for r in self.recs if r['tablo_modeli']['kaynak_calisma']['ad'] == study
                and r['tablo_modeli']['kaynak_calisma']['yil'] == (year or years[study])
                and r['sonuc_olcutu_tr'] == column]

    def test_study_identity_in_note_not_patient_group(self):
        self.assertTrue(self.recs)
        for r in self.recs:
            self.assertNotIn('hasta_grubu', r)
            self.assertEqual(r['baglam'], 'literature_background')
            study = r['tablo_modeli']['kaynak_calisma']
            self.assertTrue(study['ad'] and study['yil'])
            self.assertIn(r['klinik_bilgi_en'], r['kanit_cumle'])            # kanıtta birebir
            self.assertIn(r['deger'], r['kanit_cumle'])
        smith = self.by('Smith', 'No. in sample')
        self.assertEqual([(r['deger'], r['tablo_modeli']['kaynak_calisma']['kaynakca_no']) for r in smith],
                         [('120', ['4'])])
        self.assertEqual(smith[0]['tablo_modeli']['kaynak_calisma']['ozellikler'], {'Country': 'Northland'})

    def test_population_section_and_measure_from_column(self):
        adult = self.by('Smith', 'No. in sample')[0]
        child = [r for r in self.recs if r['tablo_modeli']['kaynak_calisma'].get('yil') == '2018']
        self.assertEqual(adult['tablo_modeli']['alt_bolum'], 'Adult cases')
        self.assertTrue(child and all(r['tablo_modeli']['alt_bolum'] == 'Paediatric cases' for r in child))
        survival = self.by('Smith', 'Survival, %')[0]
        self.assertEqual((survival['deger'], survival['unit']), ('10', '%'))
        age = self.by('Smith', 'Mean age, years')
        self.assertTrue(all(r['tablo_modeli']['istatistik'] == 'ortalama' and r['unit'] == 'years' for r in age))
        zero = [r for r in child if r['sonuc_olcutu_tr'] == 'Survival, %']
        self.assertEqual(zero[0]['deger'], '0')                              # gerçek sıfır kayıt olarak kalır

    def test_two_line_group_cells_split_with_labels_and_expansions(self):
        age = sorted((r['deger'], r['tablo_modeli']['alt_grup']['etiket'], r['tablo_modeli']['alt_grup']['acilim'])
                     for r in self.by('Smith', 'Mean age, years'))
        self.assertEqual(age, [('50', 'XA', 'group alpha'), ('60', 'YB', 'group beta')])
        rec = self.by('Smith', 'Mean age, years')[0]
        self.assertEqual(rec['kanit_cumle'].count('\n'), 1)                   # iki fiziksel satırın ikisi de kanıtta
        brown = sorted((r['deger'], r['tablo_modeli']['alt_grup']['etiket'])
                       for r in self.by('Brown', 'Survival, %'))
        self.assertEqual(brown, [('7', 'NA'), ('9', 'XA')])                  # 'NA' grup adı, eksik değer değil

    def test_missing_cells_produce_no_record_and_same_values_not_merged(self):
        self.assertEqual(self.by('Jones', 'Mean age, years'), [])            # '–' = bildirilmemiş
        same = [r for r in self.recs if r['sonuc_olcutu_tr'] == 'Survival, %' and r['deger'] == '10']
        self.assertEqual(sorted(r['tablo_modeli']['kaynak_calisma']['ad'] for r in same), ['Jones', 'Smith'])
        jones = self.by('Jones', 'No. in sample')[0]
        self.assertEqual(jones['tablo_modeli']['kaynak_calisma']['dipnot'], 'Pre-print data.')

    def test_not_literature_table_held_not_attributed(self):
        table = dict(self.table, literatur=False)
        recs, holds = O.study_rows(table, self.pages)
        self.assertEqual(recs, [])
        self.assertTrue(holds and all('aktarılan çalışma' in h['neden'] for h in holds))


class ColumnStudyTableTests(unittest.TestCase):
    def test_study_from_spanning_header_and_ci_split(self):
        _, recs, holds = built(page_of(COL_STUDY_PAGE))['calisma_sutun']
        self.assertEqual(holds, [])
        stroke = {r['tablo_modeli']['olcu']: r for r in recs if r['klinik_bilgi_en'] == 'Stroke'}
        rr = stroke['Relative risk (95% CI)']
        self.assertEqual((rr['deger'], rr['ci95'], rr['etki_turu'], rr['sonuc_olcutu_tr']), ('2.10', '1.80–2.40', 'RR', 'Stroke'))
        self.assertEqual(rr['tablo_modeli']['kaynak_calisma']['ad'], 'Doe et al')
        self.assertEqual(stroke['Rate per 1000']['tablo_modeli']['kaynak_calisma']['ad'], 'Doe et al')   # yayılan başlık
        self.assertEqual(stroke['5-y risk, %']['tablo_modeli']['kaynak_calisma']['ad'], 'Roe et al')
        self.assertEqual(stroke['5-y risk, %']['unit'], '%')
        self.assertEqual(rr['tablo_modeli']['kaynak_calisma']['ozellikler'], {'Study design': ['Meta-analysis']})
        hf = [r for r in recs if r['klinik_bilgi_en'] == 'Heart failure' and r.get('ci95')][0]
        self.assertEqual((hf['deger'], hf['tablo_modeli']['dipnot']), ('3.30', {'b': 'Adjusted estimate.'}))
        self.assertTrue(all(r['baglam'] == 'literature_background' and 'hasta_grubu' not in r for r in recs))


class RecommendationTableTests(unittest.TestCase):
    def test_class_level_text_linked_with_prefixes(self):
        _, recs, holds = built(page_of(REC_PAGE))['oneri']
        self.assertEqual(holds, [])
        got = [(r['tablo_modeli']['oneri_sinifi'], r['tablo_modeli']['kanit_duzeyi'], r['tablo_modeli'].get('alt_bolum'),
                r['tablo_modeli'].get('oneri_on_kosulu')) for r in recs]
        self.assertEqual(got, [('1', 'A', 'Acute Symptoms', None),
                               ('2a', 'B-R', 'Acute Symptoms', 'For patients with known disease categorized as low risk,'),
                               ('2a', 'B-NR', 'Acute Symptoms', 'For patients who have symptoms with previous')])
        for r in recs:
            self.assertEqual((r['baglam'], r['outcome_type']), ('recommendation', 'öneri'))
            self.assertNotIn('deger', r)                                     # kanıt düzeyi klinik ölçüm değil
            self.assertIn(r['klinik_bilgi_en'], r['kanit_cumle'])
        self.assertEqual(recs[1]['kanit_cumle'].count('\n'), 1)               # ön koşul satırı kanıtta


class TestListTableTests(unittest.TestCase):
    def test_diagnostic_measures_and_shifted_bullet_items(self):
        _, recs, holds = built(page_of(TEST_PAGE))['test_listesi']
        self.assertEqual(holds, [])
        diag = sorted((r['mudahale_en'], r['sonuc_olcutu_tr'], r['deger'], r['tablo_modeli']['tani_olcusu'])
                      for r in recs if r.get('deger'))
        self.assertIn(('TEST A', 'SEN', '90%', 'sensitivity'), diag)
        items = sorted((r['tablo_modeli']['test'], r['tablo_modeli']['liste_sutunu'], r['klinik_bilgi_en'])
                       for r in recs if not r.get('deger'))
        self.assertIn(('TEST A', 'Advantages', 'Widely available'), items)
        self.assertIn(('TEST A', 'Limitations', 'Higher cost'), items)
        self.assertIn(('TEST B', 'Limitations', 'Lower accuracy'), items)

    def test_bullet_text_count_mismatch_held(self):
        page = TEST_PAGE.replace('|_•_<br>_•_|Cheap<br>No contrast|', '|_•_<br>_•_|Cheap|')
        _, recs, holds = built(page_of(page))['test_listesi']
        self.assertTrue(any('sayısı uyuşmuyor' in h['neden'] for h in holds))
        self.assertFalse(any(r['klinik_bilgi_en'] == 'Cheap' for r in recs))


class SideBySideTests(unittest.TestCase):
    def test_matrix_and_importance_extracted_when_verified(self):
        _, recs, holds = built(page_of(side_page()))['yan_yana']
        self.assertEqual(holds, [])
        matrix = [r for r in recs if r['tablo_modeli'].get('gercek_sinif')]
        importance = [r for r in recs if not r['tablo_modeli'].get('gercek_sinif')]
        self.assertEqual((len(matrix), len(importance)), (18, 6))
        cell = [r for r in matrix if r['tablo_modeli']['tahmin_modeli'] == '2' and r['klinik_bilgi_en'] == 'Event Present'
                and r['sonuc_olcutu_tr'] == 'Present'][0]
        self.assertEqual((cell['deger'], cell['tablo_modeli']['tahmin_sinifi']), ('15', 'Present'))
        imp = [r for r in importance if r['klinik_bilgi_en'] == 'Cut off A'][0]
        self.assertEqual((imp['deger'], imp['tablo_modeli']['tahmin_modeli']), ('39.0%', '2'))
        # Yan yana bölümler birbirine bağlanmaz: değişken adı matris kaydında yok.
        self.assertFalse(any(r['klinik_bilgi_en'].startswith(('Marker', 'Cut off')) for r in matrix))

    def test_inconsistent_matrix_held_unique_block_not_sufficient(self):
        _, recs, holds = built(page_of(side_page(a_present='3')))['yan_yana']
        self.assertTrue(any('matrisi doğrulanamadı' in h['neden'] and h.get('tahmin_modeli') == '1' for h in holds))
        self.assertFalse(any(r['tablo_modeli'].get('tahmin_modeli') == '1' and r['tablo_modeli'].get('gercek_sinif')
                             for r in recs))
        self.assertTrue(any(r['tablo_modeli'].get('tahmin_modeli') == '2' and r['tablo_modeli'].get('gercek_sinif')
                            for r in recs))
        _, recs, holds = built(page_of(side_page(overall='70.0%')))['yan_yana']
        self.assertTrue(any('genel doğru' in h['neden'] for h in holds))


class TrialTableTests(unittest.TestCase):
    def setUp(self):
        _, self.recs, self.holds = built(page_of(RCT_PAGE))['calisma_satir']

    def test_arms_effect_and_p(self):
        alpha = [r for r in self.recs if r['tablo_modeli']['kaynak_calisma']['ad'] == 'ALPHA-1']
        arms = sorted((r['tablo_modeli']['kol']['etiket'], r['tablo_modeli']['kol']['rol'], r['deger'])
                      for r in alpha if r['sonuc_olcutu_tr'] == 'Participants, No.')
        self.assertEqual(arms, [('Control', 'kontrol', '35'), ('Procedure', 'tedavi', '30')])
        outcome = sorted((r['tablo_modeli']['kol']['etiket'], r['deger'], r.get('p'))
                         for r in alpha if r['sonuc_olcutu_tr'] == 'Recurrence')
        self.assertEqual(outcome, [('DRG', '20<br>(57%)', '.001'), ('Procedure', '4 (13%)', '.001')])
        beta = [r for r in self.recs if r['tablo_modeli']['kaynak_calisma']['ad'] == 'BETA' and r.get('etki_turu') == 'HR']
        self.assertEqual([(r['deger'], r['ci95'], r['p'], r['mudahale_en'], r['comparator']) for r in beta],
                         [('0.74', '0.31–1.15', '.30', 'Procedure', 'Control')])
        follow = [r for r in self.recs if r['tablo_modeli']['kaynak_calisma']['ad'] == 'BETA'
                  and r['sonuc_olcutu_tr'].startswith('Length')]
        self.assertEqual((follow[0]['deger'], follow[0]['unit'], follow[0]['tablo_modeli']['istatistik']),
                         ('40.5', 'mo', 'medyan'))

    def test_arm_label_mismatch_held_not_guessed(self):
        self.assertTrue(any('Primary outcome' in h['neden'] and 'GAMMA' in h['kanit_cumle'] for h in self.holds))
        self.assertFalse(any(r['tablo_modeli']['kaynak_calisma']['ad'] == 'GAMMA'
                             and r['sonuc_olcutu_tr'] == 'Freedom' for r in self.recs))


class PipelineTests(unittest.TestCase):
    def test_equivalent_llm_record_replaced_with_extras_kept_and_wrong_link_stays_held(self):
        pages = page_of(side_page())
        row = side_page().splitlines()[5]
        good = dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en='Event Absent', kanit_cumle=row,
                    sayfa=1, kanit_turu='tablo', deger='94', sonuc_olcutu_tr='Absent')
        wrong = dict(good, klinik_bilgi_en='Marker change A', sonuc_olcutu_tr='Event Absent')   # yan bölüme bağlanmış
        rejected = [{'sayfa': 1, 'asama': 'tablo_hizalama_inceleme', 'neden': 'karmaşık tablo', 'kayit': wrong}]
        out = O.apply_special_tables([good], pages, rejected)
        det = [r for r in out if r['klinik_bilgi_en'] == 'Event Absent' and r['deger'] == '94'
               and r['tablo_modeli']['tahmin_modeli'] == '1']
        self.assertEqual(len(det), 1)
        self.assertNotIn(good, out)
        self.assertEqual(det[0]['tablo_modeli']['llm_kaydi'], [{'bilgi_turu': 'klinik_sonuc'}])
        stages = {x['asama'] for x in rejected}
        self.assertIn(O.REPLACED_STAGE, stages)
        self.assertEqual([x['asama'] for x in rejected if x.get('kayit') is wrong], ['tablo_hizalama_inceleme'])

    def _two_tables(self, second_rows=('TEST C', 'TEST D')):
        second = TEST_PAGE.replace('**Table 4.** Testing options.', '**Table 8.** Other testing options.') \
            .replace('TEST A', second_rows[0]).replace('TEST B', second_rows[1])
        pages = page_of(TEST_PAGE + '\n\n' + second)
        firsts = ('|TEST A|', f'|{second_rows[0]}|')
        rows = [l for l in pages[1].splitlines() if l.startswith(firsts)]
        llm = [dict(bilgi_turu='olcum', baglam='literature_background', klinik_bilgi_en=row.split('|')[1],
                    kanit_cumle=row, sayfa=1, kanit_turu='tablo', deger=row.split('|')[2], sonuc_olcutu_tr='SEN')
               for row in rows]                                                   # bağlam LLM kaydından gelir
        return pages, llm

    def test_same_item_for_different_tests_kept_as_distinct_relations(self):
        # Tablo / satır kimliği kaynak tablodan doğrulama anahtarına ulaşır (_structured_key): iki testin aynı
        # maddesi iki ayrı klinik ilişkidir, tekrar sayılmaz; adaptörün iç tekrar kontrolünden de geçer.
        from src import rag_aktarim as A
        pages, llm = self._two_tables()
        rejected = []
        out = O.apply_special_tables(llm, pages, rejected)
        cost = sorted(r['tablo_modeli']['test'] for r in out if r['klinik_bilgi_en'] == 'Higher cost')
        self.assertEqual(cost, ['TEST A', 'TEST C'])
        self.assertFalse(any(x['asama'] == O.HOLD_STAGE for x in rejected))
        draft = A.build_draft([dict(r, kategoriler=['x']) for r in out], pages,
                              {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf'}, [])
        notes = [c['taslak_notu'] for c in draft['adaylar'] if c['klinik_bilgi_en'] == 'Higher cost']
        self.assertEqual(sorted(n['test'] for n in notes), ['TEST A', 'TEST C'])
        self.assertEqual(len({(n['tablo_no'], n['satir_no']) for n in notes}), 2)
        self.assertEqual(len(draft['aday_kaynaklari']), len(draft['adaylar']))

    def test_true_duplicate_in_same_row_still_caught(self):
        page = TEST_PAGE.replace('|Fast<br>Widely available|', '|Fast<br>Fast|')   # aynı satırda aynı madde
        pages = page_of(page)
        row_a = [l for l in page.splitlines() if l.startswith('|TEST A|')][0]
        llm = [dict(bilgi_turu='olcum', baglam='literature_background', klinik_bilgi_en='TEST A', kanit_cumle=row_a,
                    sayfa=1, kanit_turu='tablo', deger='90%', sonuc_olcutu_tr='SEN')]
        rejected = []
        out = O.apply_special_tables(llm, pages, rejected)
        self.assertEqual(len([r for r in out if r['klinik_bilgi_en'] == 'Fast']), 1)
        held = [x for x in rejected if x['asama'] == O.HOLD_STAGE and x.get('kayit', {}).get('klinik_bilgi_en') == 'Fast']
        self.assertEqual(len(held), 1)
        self.assertIn('ayırt edemiyor', held[0]['neden'])

    def test_no_identity_invented_when_row_text_is_ambiguous(self):
        # İki tabloda birebir aynı satır metni: kaynak satır tekil değil -> kimlik verilmez; ikinci kayıt incelemede.
        pages, llm = self._two_tables(second_rows=('TEST A', 'TEST D'))
        line_ids = {}
        for table, name, _, _ in O.build(pages):
            for seg in table['segmentler']:
                for i in range(seg['start'], seg['end']):
                    key = (seg['sayfa'], O._c(seg['lines'][i]))
                    line_ids[key] = None if key in line_ids else (table['tablo_no'], i)
        row_a = [l for l in pages[1].splitlines() if l.startswith('|TEST A|')][0]
        self.assertIsNone(O.row_identity({'kanit_turu': 'tablo', 'sayfa': 1, 'kanit_cumle': row_a}, line_ids))
        rejected = []
        out = O.apply_special_tables(llm[:1], pages, rejected)
        self.assertEqual(len([r for r in out if r['klinik_bilgi_en'] == 'Higher cost']), 1)
        self.assertTrue(any(x['asama'] == O.HOLD_STAGE and x.get('kayit', {}).get('klinik_bilgi_en') == 'Higher cost'
                            for x in rejected))
        # Model kimliği kaynakla çelişirse kimlik verilmez.
        conflict = {'kanit_turu': 'tablo', 'sayfa': 1, 'kanit_cumle': pages[1].splitlines()[5],
                    'tablo_modeli': {'durum': O.STATE, 'tablo_no': 99, 'satir_indeksleri': [5]}}
        self.assertIsNone(O.row_identity(conflict, line_ids))

    def test_held_deterministic_record_never_removes_its_llm_equivalent(self):
        from unittest import mock
        import src.pilot_clinical as PC
        pages = page_of(TEST_PAGE)
        row_b = [l for l in TEST_PAGE.splitlines() if l.startswith('|TEST B|')][0]
        llm_b = dict(bilgi_turu='diger', baglam='literature_background', klinik_bilgi_en='Cheap', kanit_cumle=row_b,
                     sayfa=1, kanit_turu='tablo')
        real = PC.validate_records

        def fake(records, pg):                     # birlikte doğrulamada 'Cheap' deterministik kaydını düşür
            accepted, errors = real(records, pg)
            for k, r in enumerate(records):
                if len(records) > 1 and r.get('klinik_bilgi_en') == 'Cheap' and r is not records[0] \
                        and r.get('taslak_notu') is not None:
                    errors.append({'satir': k + 1, 'hata': 'tekrar: test'})
            return accepted, errors
        rejected = []
        with mock.patch.object(PC, 'validate_records', fake):
            out = O.apply_special_tables([llm_b], pages, rejected)
        self.assertIn(llm_b, out)                                                # kabul edilmiş LLM kaydı yerinde
        self.assertFalse(any(x.get('kayit') is llm_b for x in rejected))
        self.assertTrue(any(x['asama'] == O.HOLD_STAGE and x.get('kayit', {}).get('klinik_bilgi_en') == 'Cheap'
                            for x in rejected))

    def test_prediction_model_number_does_not_overwrite_extraction_model(self):
        # taslak_notu.model = çıkarım modeli (provenance); sinir ağı / tahmin modeli numarası tahmin_modeli'de.
        from src import rag_aktarim as A
        from src import rag_semantik_dogrulama as S
        pages = page_of(side_page())
        recs = O.apply_special_tables([], pages, [])
        self.assertTrue(recs and all('model' not in r['tablo_modeli'] for r in recs))
        draft = A.build_draft([dict(r, kategoriler=['x']) for r in recs], pages,
                              {'cikti_dizini': 'x', 'model': 'llm-x', 'pdf': 'x.pdf'}, [])
        notes = [c['taslak_notu'] for c in draft['adaylar']]
        self.assertEqual(len(notes), 24)
        self.assertTrue(all(n['model'] == 'llm-x' for n in notes))
        self.assertEqual({n['tahmin_modeli'] for n in notes}, {'1', '2'})
        payload = S.build_payload(draft['adaylar'][0], pages)
        extra = payload['kayit']['tablo_ve_baglam_notlari']
        self.assertIn('tahmin_modeli', extra)
        self.assertNotIn('llm-x', json.dumps(payload))                            # çıkarım modeli adı yükte yok

    def test_evidence_level_linked_to_recommendation_row(self):
        page = '\n'.join([
            '**Table 7.** Benefits of adhering to recommendations',
            '',
            '||||**Associated b**|**enefits**|',
            '|---|---|---|---|---|',
            '|**Risk factor**|**Level of**<br>**evidence**<sup>**_a_**</sup>|**Recommendation**|**Symptoms**|**Burden**|',
            '|Factor P|B-R<sup>4,52</sup>|Do activity Q|Decreased|Decreased|',
            '|Factor S|A<sup>4</sup>|Do activity T|||',
        ])
        pages = page_of(page)
        lines = page.splitlines()
        loe = dict(bilgi_turu='olcum', baglam='recommendation', klinik_bilgi_en='Factor P', kanit_cumle=lines[5],
                   sayfa=1, kanit_turu='tablo', deger='B-R<sup>4,52</sup>')
        eff = dict(loe, bilgi_turu='klinik_sonuc', deger='Decreased', sonuc_olcutu_tr='Symptoms')
        lone = dict(loe, klinik_bilgi_en='Factor S', kanit_cumle=lines[6], deger='A<sup>4</sup>')
        rejected = []
        out = O.apply_special_tables([loe, eff, lone], pages, rejected)
        kept = {r['klinik_bilgi_en'] + ':' + r['deger']: r for r in out}
        self.assertNotIn('Factor P:B-R<sup>4,52</sup>', kept)
        self.assertEqual(kept['Factor P:Decreased'][O.LINK_KEY]['kanit_duzeyi'], 'B-R')
        self.assertEqual(kept['Factor P:Decreased'][O.LINK_KEY]['kaynakca_numaralari'], ['4', '52'])
        self.assertEqual(kept['Factor P:Decreased'][O.LINK_KEY]['oneri_metni'], 'Do activity Q')
        self.assertIn('Factor S:A<sup>4</sup>', kept)                       # satırda başka kayıt yok: kayıt kalır
        self.assertEqual([x['asama'] for x in rejected], [O.LOE_STAGE])

    def test_adapter_maps_special_record_with_study_in_note(self):
        from src import rag_aktarim as A
        pages = page_of(ROW_STUDY_PAGE)
        recs = O.apply_special_tables([], pages, [])
        result = A.build_draft([dict(r, kategoriler=['measurements']) for r in recs], pages,
                               {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf'}, [])
        self.assertEqual(result['inceleme'], [])
        self.assertEqual(len(result['adaylar']), len(recs))
        cand = [c for c in result['adaylar'] if c['deger'] == '120'][0]
        note = cand['taslak_notu'] if isinstance(cand['taslak_notu'], dict) else json.loads(cand['taslak_notu'])
        self.assertEqual(note['kaynak_calisma']['ad'], 'Smith')
        self.assertEqual(note['degisken'], 'No. in sample')
        self.assertEqual(cand['sonuc_olcutu_tr'], 'No. in sample')
        self.assertNotIn('hasta_grubu', cand)
        self.assertEqual(cand['review_status'], 'pending')
        self.assertTrue(note['satir_no'])


if __name__ == '__main__':
    unittest.main()
