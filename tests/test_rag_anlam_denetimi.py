"""src.rag_anlam_denetimi genel regresyon testleri. Sentetik metin ve tablolar; makaleye özel ad veya değer yok.
LLM / API / embedding / veritabanı kullanılmaz."""
import json
import unittest

from src import rag_anlam_denetimi as D


def rec(fact, quote, **kw):
    base = dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en=fact, kanit_cumle=quote, sayfa=1,
                kanit_turu='metin')
    base.update(kw)
    return base


def run(*records, pages=None):
    rejected = []
    pages = pages or {1: ' '.join(r['kanit_cumle'] for r in records)}
    kept = D.check_meaning(list(records), pages, rejected)
    return kept, rejected


def meta(item):
    return item.get(D.NOTE_KEY) or {}


class MissingVersusZeroTests(unittest.TestCase):
    def test_missing_markers_are_not_values(self):
        for marker in ('NA', 'N/A', 'n.a.', 'NR', 'not reported', 'Not available', '–', '—', '-', '−', '...',
                       '**NA**', 'NA<br>NA'):
            with self.subTest(marker=marker):
                self.assertIsNotNone(D.missing_marker(marker))

    def test_real_zero_and_codes_are_kept(self):
        for value in ('0', '0.0', '0%', '0 (0)', 'B-NR', 'NRS-2002', '12<br>NA', '-1.5', '−0.3', '1–3', 'none'):
            with self.subTest(value=value):
                self.assertIsNone(D.missing_marker(value))

    def test_missing_value_record_separated_not_deleted_and_zero_kept(self):
        quote = '|Marker|Group A|Group B|\n|Troponin|NR|0|'
        na = rec('Troponin', quote, kanit_turu='tablo', deger='NR', hasta_grubu='Group A')
        zero = rec('Troponin', quote, kanit_turu='tablo', deger='0', hasta_grubu='Group B')
        kept, rejected = run(na, zero)
        self.assertEqual([k['deger'] for k in kept], ['0'])
        self.assertEqual([(r['asama'], r['kayit']['deger']) for r in rejected], [(D.STAGE_MISSING, 'NR')])
        self.assertIn('gerçek sıfır değil', rejected[0]['neden'])


class DashAndNumberTests(unittest.TestCase):
    def test_negative_numbers_and_ranges(self):
        self.assertEqual(D.to_number('−1.5'), -1.5)
        self.assertEqual(D.to_number('-1.5'), -1.5)
        self.assertEqual(D.to_number('–2.3'), -2.3)
        self.assertEqual(D.to_number('2,735.621'), 2735.621)
        self.assertEqual(D.to_number('<0.001'), 0.001)
        self.assertEqual(D.split_range('0.89-1.37'), (0.89, 1.37))
        self.assertEqual(D.split_range('0.89–1.37'), (0.89, 1.37))
        self.assertEqual(D.split_range('−2.1 – −0.5'), (-2.1, -0.5))
        self.assertEqual(D.split_range('-3-5'), (-3.0, 5.0))
        self.assertEqual(D.split_range('1 to 3'), (1.0, 3.0))
        self.assertIsNone(D.split_range('12'))

    def test_negative_value_components_keep_sign_and_source_text(self):
        comp = D.value_components('-3.2±1.1', 'Mean ± SD')
        self.assertEqual((comp['merkez'], comp['yayilim'], comp['yayilim_turu']), ('-3.2', '1.1', 'sd'))
        comp = D.value_components('-0.5 (-1.2–0.3)', 'Mean difference (95% CI)')
        self.assertEqual((comp['merkez'], comp['aralik_alt'], comp['aralik_ust'], comp['aralik_turu']),
                         ('-0.5', '-1.2', '0.3', '%95 GA'))

    def test_source_value_not_rewritten(self):
        quote = 'The mean change was −2.1 (−3.0 – −1.2) points.'
        item = rec('mean change', quote, deger='−2.1 (−3.0 – −1.2)')
        kept, _ = run(item)
        self.assertEqual(kept[0]['deger'], '−2.1 (−3.0 – −1.2)')
        self.assertNotIn('otomatik_duzeltme', kept[0])


class StatisticComponentTests(unittest.TestCase):
    def test_labelled_components_named(self):
        cases = [
            ('71.4±7.6 (56-90)', '(X±SD) (minimum-maximum)',
             {'merkez_turu': 'ortalama', 'yayilim_turu': 'sd', 'aralik_turu': 'min–maks'}),
            ('12.3 (1.7-91.4)', 'Median (Q1-Q3)', {'merkez_turu': 'medyan', 'aralik_turu': 'IQR (Q1–Q3)'}),
            ('64 (53-74)', 'Median (IQR)', {'merkez_turu': 'medyan', 'aralik_turu': 'IQR (Q1–Q3)'}),
            ('64 (40-90)', 'Median (range)', {'merkez_turu': 'medyan', 'aralik_turu': 'min–maks'}),
            ('1.25 (0.89-1.37)', 'Odds ratio (95% CI)', {'merkez_turu': 'nokta tahmini', 'aralik_turu': '%95 GA'}),
            ('120 (32.3)', 'n (%)', {'bicim': 'n (%)', 'n': '120', 'yuzde': '32.3'}),
            ('5.1±0.4', 'Mean ± SEM', {'yayilim_turu': 'se'}),
        ]
        for value, label, expected in cases:
            with self.subTest(value=value, label=label):
                comp = D.value_components(value, label)
                for key, val in expected.items():
                    self.assertEqual(comp[key], val)

    def test_unlabelled_components_never_guessed(self):
        self.assertEqual(D.value_components('12.3 (1.7-91.4)', None)['aralik_turu'], 'belirtilmemiş')
        self.assertEqual(D.value_components('5.1±0.4', None)['yayilim_turu'], 'belirtilmemiş (SD / SE)')
        self.assertEqual(D.value_components('120 (32.3)', None)['parantez_turu'], 'belirtilmemiş')
        self.assertIsNone(D.value_components('increased', 'n (%)'))

    def test_document_declaration_only_when_unambiguous(self):
        sd = {1: 'Continuous variables were expressed as mean±standard deviation and categorical variables were '
                 'expressed as numbers and percentages.'}
        both = {1: 'Data are presented as mean ± SD. Biomarkers are presented as mean ± SEM.'}
        self.assertEqual(D.document_formats(sd), {'±': 'sd', 'n (%)': True})
        self.assertEqual(D.document_formats(both)['±'], None)
        self.assertEqual(D.document_formats({1: 'No declaration here.'}), {'±': None, 'n (%)': False})
        quote = '|Variable|Group A (n=50)|\n|Age|62±10.3|'
        item = rec('Age', quote, kanit_turu='tablo', deger='62±10.3', hasta_grubu='Group A (n=50)')
        kept, _ = run(item, pages={1: quote + '\n' + sd[1]})
        comp = meta(kept[0])['deger_bilesenleri']
        self.assertEqual((comp['yayilim_turu'], comp['etiket_kaynagi']), ('sd', 'belge_bildirimi'))
        kept, _ = run(item, pages={1: quote + '\n' + both[1]})
        self.assertEqual(meta(kept[0])['deger_bilesenleri']['yayilim_turu'], 'belirtilmemiş (SD / SE)')

    def test_ci95_filled_with_iqr_column_held(self):
        quote = '|Marker|Median (IQR)|\n|CRP|4.1 (2.0-7.9)|'
        item = rec('CRP', quote, kanit_turu='tablo', deger='4.1', ci95='2.0-7.9',
                   tablo_modeli={'durum': 'eslesti', 'sutun': 'Median (IQR)'})
        kept, rejected = run(item)
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0]['asama'], D.STAGE_REVIEW)
        self.assertIn('GA değil', rejected[0]['neden'])


class CitationTests(unittest.TestCase):
    def test_contiguous_citation_removed_from_value_with_raw_kept(self):
        quote = 'Survival to discharge was 5.9% _(30)_ in one registry.'
        kept, _ = run(rec('Survival to discharge was 5.9%', quote, deger='5.9% _(30)_'))
        item = kept[0]
        self.assertEqual(item['deger'], '5.9%')
        self.assertEqual(meta(item)['ham_deger'], '5.9% _(30)_')
        self.assertEqual(meta(item)['kaynakca_numaralari'], ['30'])
        self.assertIn('anlam_denetimi:deger', item['otomatik_duzeltme'])
        self.assertEqual(item['kanit_cumle'], quote)                       # özgün kanıt değişmez
        self.assertEqual(item['baglam'], 'study_finding')                  # literature_background'a TAŞINMAZ
        self.assertIn('atıflı değer', ' '.join(meta(item)['uyarilar']))

    def test_interleaved_citations_keep_original_value(self):
        quote = 'Rates ranged from 1.4% _(23)_ to 31% _(20)_ across studies.'
        kept, _ = run(rec('Rates ranged', quote, deger='1.4% _(23)_ to 31% _(20)_', baglam='literature_background'))
        item = kept[0]
        self.assertEqual(item['deger'], '1.4% _(23)_ to 31% _(20)_')
        self.assertEqual(meta(item)['deger_kaynakcasiz'], '1.4% to 31%')
        self.assertEqual(meta(item)['kaynakca_numaralari'], ['23', '20'])
        self.assertNotIn('uyarilar', meta(item))                          # literature_background: uyarı yok

    def test_superscript_lists_are_citations_exponents_are_not(self):
        self.assertEqual(D.split_citations('B-R<sup>4,52</sup>'), ('B-R', ['4', '52']))
        self.assertEqual(D.split_citations('B-R<sup>3–5</sup>'), ('B-R', ['3', '5']))
        self.assertEqual(D.split_citations('kg/m<sup>2</sup>'), ('kg/m<sup>2</sup>', []))
        self.assertEqual(D.split_citations('x10<sup>3</sup>/µl'), ('x10<sup>3</sup>/µl', []))
        self.assertEqual(D.split_citations('B-R<sup>4</sup>'), ('B-R<sup>4</sup>', []))   # tek numara: belirsiz
        self.assertEqual(D.split_citations('12 (30)'), ('12 (30)', []))                     # düz parantez: n (%)


class ConsistencyTests(unittest.TestCase):
    def check(self, **kw):
        quote = kw.pop('quote', 'Sentence with values.')
        kept, rejected = run(rec('value', quote, **kw))
        self.assertEqual(rejected, [])
        return kept[0], meta(kept[0]).get('uyarilar', [])

    # GA–p, p aralığı, GA sırası ve değerin GA içinde olması src.data_quality kontrolleridir (bağımsız doğrulama
    # aşamasında çalışır). Aynı girdiler: anlam denetimi bunları TEKRARLAMAZ, data_quality aynı sonuçları verir.
    @staticmethod
    def dq(**kw):
        from src import data_quality as Q
        row = {'satir_hash': 'h', **kw}
        return [f['kontrol_kodu'] for f in Q.check_numeric(row) + Q.check_ci_p(row)]

    def test_ci_and_p_disagreement_flagged_by_data_quality_values_unchanged(self):
        item, warnings = self.check(deger='1.25', ci95='0.89-1.37', p='0.002', etki_turu='OR')
        self.assertEqual(warnings, [])                                          # anlam denetiminde tekrar yok
        self.assertEqual((item['deger'], item['ci95'], item['p']), ('1.25', '0.89-1.37', '0.002'))
        self.assertIn('ci_p_celiskisi', self.dq(deger='1.25', ci95='0.89-1.37', p='0.002', etki_turu='OR'))
        self.assertIn('ci_p_celiskisi', self.dq(deger='0.70', ci95='0.54-0.91', p='0.68', etki_turu='HR'))
        self.assertIn('ci_p_celiskisi', self.dq(deger='0.62', ci95='0.55-0.70', p='0.40',
                                                etki_turu='AUC / C-istatistigi'))  # AUC boş değeri 0.5
        self.assertNotIn('ci_p_celiskisi', self.dq(deger='0.62', ci95='0.48-0.70', p='0.40',
                                                   etki_turu='AUC / C-istatistigi'))  # 0.5'i içeriyor: tutarlı

    def test_consistent_and_borderline_not_flagged(self):
        for kw in (dict(deger='1.8', ci95='1.2-2.6', p='0.004', etki_turu='OR'),
                   dict(deger='1.2', ci95='0.9-1.6', p='0.21', etki_turu='RR'),
                   dict(deger='1.3', ci95='1.0-1.7', p='0.05', etki_turu='OR'),       # sınırda yuvarlama
                   dict(deger='1.3', ci95='1.01-1.7', p='0.049', etki_turu='HR'),
                   dict(deger='0.12', ci95='0.05-0.20', p='0.01', etki_turu='ortalama fark')):
            with self.subTest(**kw):
                self.assertEqual(self.check(**kw)[1], [])
                self.assertNotIn('ci_p_celiskisi', self.dq(**kw))

    def test_point_outside_ci_reversed_bounds_and_p_range_by_data_quality(self):
        self.assertEqual(self.check(deger='2.5', ci95='1.1-1.9')[1], [])
        self.assertIn('deger_ci_disinda', self.dq(deger='2.5', ci95='1.1-1.9'))
        self.assertIn('ci_ters', self.dq(deger='1.5', ci95='1.9-1.1'))
        self.assertIn('p_aralik_disi', self.dq(deger='3', p='1.7'))

    def test_group_n_percentage_and_order(self):
        quote = '|Variable|Group A (n=200)|\n|Smoking|50 (25.0)|'
        base = dict(quote=quote, kanit_turu='tablo', hasta_grubu='Group A (n=200)',
                    tablo_modeli={'durum': 'eslesti', 'bicim_etiketi': 'n (%)'})
        self.assertEqual(self.check(deger='50 (25.0)', **base)[1], [])
        base['quote'] = quote.replace('50 (25.0)', '50 (30.0)')
        self.assertTrue(any("grup n'i ile uyumsuz" in w for w in self.check(deger='50 (30.0)', **base)[1]))
        base['quote'] = '|Variable|Group A|\n|Age|71±7 (95-60)|'
        base['tablo_modeli'] = {'durum': 'eslesti', 'bicim_etiketi': '(X±SD) (min-max)'}
        base['hasta_grubu'] = 'Group A'
        self.assertTrue(any('aralık alt sınırı' in w for w in self.check(deger='71±7 (95-60)', **base)[1]))


class TypeAndContextTests(unittest.TestCase):
    def assert_review(self, item, phrase):
        kept, rejected = run(item)
        self.assertEqual(kept, [], phrase)
        self.assertEqual(rejected[0]['asama'], D.STAGE_REVIEW)
        self.assertIn(phrase, rejected[0]['neden'])

    def assert_kept(self, item):
        kept, rejected = run(item)
        self.assertEqual(rejected, [], [r['neden'] for r in rejected])
        return kept[0]

    def test_dose_type_checks(self):
        self.assert_review(rec('120 patients', 'The cohort consisted of 120 patients.', bilgi_turu='doz',
                               deger='120', baglam='study_method'), 'hasta sayısı')
        self.assert_review(rec('extended (>12 months)', 'Therapy can be extended (>12 months).', bilgi_turu='doz',
                               baglam='recommendation'), 'tedavi süresi')
        self.assert_review(rec('below 110 beats per minute', 'Aim for a rate below 110 beats per minute.',
                               bilgi_turu='doz', baglam='recommendation'), 'doz birimi yok')
        self.assert_kept(rec('75 mg daily', 'Aspirin 75 mg daily was given.', bilgi_turu='doz'))
        self.assert_kept(rec('twice daily', 'The drug was given twice daily.', bilgi_turu='doz'))   # sıklık: rejim
        self.assert_kept(rec('1 mg/kg twice daily', 'Enoxaparin 1 mg/kg twice daily for 5 days.', bilgi_turu='doz'))

    def test_device_versus_drug(self):
        self.assert_review(rec('drug-eluting stents', 'PCI used drug-eluting stents.', bilgi_turu='ilac',
                               baglam='literature_background'), 'cihaz')
        self.assert_kept(rec('beta-blockers', 'Patients received beta-blockers.', bilgi_turu='ilac'))
        self.assert_kept(rec('heparin-coated catheter', 'A heparin-coated catheter was used.', bilgi_turu='tedavi'))

    def test_inclusion_versus_diagnostic_criterion(self):
        self.assert_review(rec('stenosis ≥70%', 'Patients with stenosis ≥70% were enrolled.', bilgi_turu='tani_kriteri',
                               baglam='study_method'), 'dahil edilme')
        self.assert_kept(rec('troponin above the 99th percentile', 'MI was defined as troponin above the 99th '
                             'percentile.', bilgi_turu='tani_kriteri', baglam='study_method'))

    def test_aim_and_limitation_sentences(self):
        self.assert_review(rec('to compare marker levels', 'The aim of this study was to compare marker levels.'),
                           'araştırma amacı')
        self.assert_review(rec('the small sample size', 'One limitation of this study is the small sample size.'),
                           'sınırlılık')
        self.assert_kept(rec('our review is the first to collate the data', 'Despite these limitations, our review is '
                             'the first to collate the data.'))
        self.assert_kept(rec('the time to admission was short', 'Although the sample is the main limitation, the time '
                             'to admission was short.'))


class ScopeTests(unittest.TestCase):
    def stage_of(self, fact, quote, **kw):
        kept, rejected = run(rec(fact, quote, bilgi_turu=kw.pop('bilgi_turu', 'tani'), **kw))
        if rejected:
            return rejected[0]['asama'], rejected[0]['neden']
        return None, meta(kept[0]).get('kapsam_uyarisi')

    def test_negation_governing_fact_goes_to_reextraction(self):
        cases = [
            ('Aspirin', 'Aspirin is not recommended for stroke prevention.', 'recommendation'),
            ('mortality between the groups', 'There was no significant difference in mortality between the groups.',
             'study_finding'),
            ('a rise of >20% in troponin', 'It is noted that a rise of >20% in troponin will not be sufficient.',
             'recommendation'),
            ('heart failure', 'Outpatient care is appropriate in the absence of heart failure.', 'recommendation'),
            ('ischemia', 'Outpatient care is appropriate in the absence of heart failure, ischemia, or shock.',
             'recommendation'),
            ('anticoagulants', 'Anticoagulants are contraindicated in active bleeding.', 'recommendation'),
        ]
        for fact, quote, context in cases:
            with self.subTest(fact=fact):
                stage, reason = self.stage_of(fact, quote, baglam=context)
                self.assertEqual(stage, D.STAGE_REEXTRACT, reason)

    def test_condition_phrase_flagged_but_kept(self):
        stage, warning = self.stage_of('a loop recorder is reasonable', 'In patients with stroke without known AF, a '
                                       'loop recorder is reasonable.', baglam='recommendation', bilgi_turu='teshis_yontemi')
        self.assertIsNone(stage)
        self.assertIn('In patients with stroke without known AF', warning)
        stage, warning = self.stage_of('drug X', 'Differently, in patients who do not undergo surgery, drug X is '
                                       'preferred.', baglam='recommendation', bilgi_turu='ilac')
        self.assertIsNone(stage)
        self.assertIn('do not undergo surgery', warning)

    def test_population_and_time_scope_flagged_for_semantic_check(self):
        cases = [
            ('drug X reduced events', 'In patients with diabetes, drug X reduced events.', 'study_finding',
             'In patients with diabetes'),
            ('mortality was lower', 'At 12 months, mortality was lower in the treated arm.', 'study_finding',
             'At 12 months'),
            ('the drug is reasonable', 'The drug is reasonable in elderly patients with renal failure.',
             'recommendation', 'in elderly patients'),
            ('readmission decreased', 'Readmission decreased at 6 months after discharge.', 'study_finding',
             'at 6 months'),
            ('events were reduced', 'Events were reduced during follow-up in both groups.', 'study_finding',
             'during follow-up'),
        ]
        for fact, quote, context, phrase in cases:
            with self.subTest(fact=fact):
                kept, rejected = run(rec(fact, quote, baglam=context))
                self.assertEqual(rejected, [])                                   # silinmez, ayrılmaz
                m = meta(kept[0])
                self.assertIn(phrase, m.get('kapsam_uyarisi', ''))
                self.assertTrue(m.get('semantik_dogrulama_gerekli'))
                self.assertEqual(kept[0]['klinik_bilgi_en'], fact)              # bağlam tahminle eklenmez

    def test_population_time_not_flagged_when_inside_fact_or_context_excluded(self):
        for fact, quote, context in (
                ('In patients with diabetes, drug X reduced events', 'In patients with diabetes, drug X reduced events.',
                 'study_finding'),
                ('189 patients were included', 'In this study, 189 patients were included at 12 centres.', 'study_method'),
                ('sepsis', 'Patients with sepsis were excluded at baseline.', 'exclusion_criterion'),
                ('drug X reduced events', 'Although few patients were treated, drug X reduced events.', 'study_finding')):
            with self.subTest(fact=fact):
                kept, _ = run(rec(fact, quote, baglam=context))
                self.assertNotIn('kapsam_uyarisi', meta(kept[0]))

    def test_negation_not_governing_fact_ignored(self):
        cases = [
            ('12-lead ECG', 'AF shows irregular activity without discrete P waves on 12-lead ECG.'),
            ('cardiomyopathy', 'Such patients may or may not have underlying cardiomyopathy.'),
            ('diabetes', 'Control for confounding related to the presence of diabetes could not be achieved.'),
            ('thrombosis in diabetes', 'Although the mechanisms underlying thrombosis in diabetes have not been '
                                       'elucidated, it is known that markers rise.'),
            ('no reflow', 'The no reflow phenomenon was observed.'),
            ('not significantly different', 'Values were not significantly different.'),
            ('sepsis', 'Patients with sepsis were not included.'),
        ]
        for fact, quote in cases:
            with self.subTest(fact=fact):
                context = 'exclusion_criterion' if fact == 'sepsis' else 'literature_background'
                stage, warning = self.stage_of(fact, quote, baglam=context)
                self.assertIsNone(stage, warning)
                self.assertIsNone(warning)

    def test_negation_scope_across_multi_part_evidence(self):
        # Bilgi ikinci parçada; olumsuz yüklem yalnız birleşik kanıtta görülür (kanit_cumle bu aşamada ilk parça).
        parts = [{'sayfa': 1, 'metin': 'In this setting, routine'}, {'sayfa': 2, 'metin': 'imaging is not advised.'}]
        item = rec('imaging', 'In this setting, routine', sayfa=1, sayfa_bitis=2, kanit_parcalari=parts,
                   baglam='recommendation', bilgi_turu='teshis_yontemi')
        _, rejected = run(item, pages={1: 'In this setting, routine', 2: 'imaging is not advised.'})
        self.assertEqual(rejected[0]['asama'], D.STAGE_REEXTRACT)
        # Edat tümleci özne değildir: 'screening with [imaging] is not advised' bilgiyi olumsuzlamaz.
        self.assertIsNone(D.scope_problem(rec('imaging', 'Routine screening with imaging is not advised.')))


class SingleTermAndPipelineTests(unittest.TestCase):
    def test_single_term_records_flagged_not_removed(self):
        quote = 'Symptoms include palpitations, dyspnea and chest pain.'
        records = [rec(t, quote, bilgi_turu='semptom', baglam='literature_background')
                   for t in ('palpitations', 'dyspnea', 'chest pain')]
        kept, rejected = run(*records)
        self.assertEqual(rejected, [])
        self.assertEqual(len(kept), 3)
        self.assertTrue(all(meta(k).get('tek_terim') for k in kept))

    def test_nothing_deleted_and_inputs_unchanged(self):
        records = [rec('value', 'Troponin was NR.', deger='NR'),
                   rec('Aspirin', 'Aspirin is not recommended.', baglam='recommendation', bilgi_turu='ilac'),
                   rec('120 patients', 'There were 120 patients.', bilgi_turu='doz', deger='120'),
                   rec('Rate was 5.9%', 'Rate was 5.9% _(30)_ overall.', deger='5.9% _(30)_'),
                   rec('heart rate', 'Heart rate was recorded.', bilgi_turu='olcum')]
        snapshot = json.dumps(records, sort_keys=True)
        kept, rejected = run(*records)
        self.assertEqual(json.dumps(records, sort_keys=True), snapshot)            # girdi kayıtları değişmez
        self.assertEqual(len(kept) + len(rejected), len(records))
        self.assertEqual(sorted(r['asama'] for r in rejected), sorted([D.STAGE_MISSING, D.STAGE_REEXTRACT, D.STAGE_REVIEW]))
        self.assertTrue(all(r['neden'] and r['kayit'] in records for r in rejected))
        self.assertFalse(any(k.get('review_status') == 'verified' for k in kept))

    def test_cleaned_value_not_in_page_is_not_applied(self):
        quote = 'Rate was 5.9% _(30)_ overall.'
        item = rec('Rate was 5.9%', quote, deger='5.9% _(30)_', hasta_grubu='Registry Z')
        kept, _ = run(item, pages={1: 'unrelated page text'})                     # grup kaynakta yok -> doğrulanamaz
        self.assertEqual(kept[0]['deger'], '5.9% _(30)_')
        self.assertEqual(meta(kept[0])['deger_kaynakcasiz'], '5.9%')
        self.assertNotIn('ham_deger', meta(kept[0]))
        self.assertNotIn('otomatik_duzeltme', kept[0])


class AdapterTests(unittest.TestCase):
    def test_meta_carried_to_note_and_hash_changes(self):
        from src import rag_aktarim as A
        pages = {1: 'Survival was 5.9% _(30)_ in one registry.'}
        meta_ = {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf'}
        item = rec('Survival was 5.9%', pages[1], deger='5.9% _(30)_', kategoriler=['measurements'])
        kept, _ = run(item, pages=pages)
        plain = A.build_draft([item], pages, meta_, [])['adaylar'][0]
        checked = A.build_draft(kept, pages, meta_, [])['adaylar'][0]
        note = checked['taslak_notu'] if isinstance(checked['taslak_notu'], dict) else json.loads(checked['taslak_notu'])
        self.assertEqual(note['anlam_denetimi']['kaynakca_numaralari'], ['30'])
        self.assertEqual(checked['deger'], '5.9%')
        self.assertEqual(checked['review_status'], 'pending')
        self.assertNotIn('anlam_denetimi', checked)                                # SQL alanı değil, notta
        self.assertNotEqual(A.satir_hash('d' * 64, plain), A.satir_hash('d' * 64, checked))

    def test_table_cell_position_uses_raw_value(self):
        from src import rag_aktarim as A
        row = '|Risk factor|Recommendation|LOE|\n|---|---|---|\n|Obesity|Weight loss|B-R<sup>4,52</sup>|'
        pages = {1: '**Table 1.** Recommendations\n' + row + '\n'}
        data_row = '|Obesity|Weight loss|B-R<sup>4,52</sup>|'
        item = dict(bilgi_turu='olcum', baglam='recommendation', klinik_bilgi_en='Obesity', kanit_cumle=data_row,
                    sayfa=1, kanit_turu='tablo', deger='B-R<sup>4,52</sup>', sonuc_olcutu_tr='LOE', kategoriler=['x'])
        kept, _ = run(item, pages=pages)
        self.assertEqual(kept[0]['deger'], 'B-R')
        result = A.build_draft(kept, pages, {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf'}, [])
        self.assertEqual(len(result['adaylar']), 1, result['inceleme'])
        note = result['adaylar'][0]['taslak_notu']
        note = note if isinstance(note, dict) else json.loads(note)
        self.assertEqual(note['sutun'], 'LOE')


if __name__ == '__main__':
    unittest.main()
