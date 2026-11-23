import collections
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.rag_klinik_cikarim import (Cache, build_tasks, categories_of, extract, grouped_rows, plan_pages, route,
                                    source_problem)
from src.rag_klinik_karsilastir import compare_category

TEXT = 'Atrial fibrillation (AF) was diagnosed in 40 patients. Chest pain was reported. Aspirin 100 mg was given.'
TABLE = ('##### Table 1: Baseline.\n|Variable|ACS Present (n=94)|ACS Absent (n=20)|Total (n=114)|p|\n|---|---|---|---|---|\n'
         '|Age, years|62 (55-70)|58 (50-66)|61 (54-69)|0.031|\n')
PAGES = {1: TEXT, 2: TABLE}


class FakeClient:
    def __init__(self, by_page):
        self.by_page, self.calls = by_page, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs['messages'][1]['content'])
        if 'start_page' in payload:
            page = ('sinir', payload['start_page'])
        elif payload.get('gorev') == 'tablo':
            page = ('tablo', payload['sayfa'])
        else:
            page = payload['sayfa']
        records = self.by_page.get(page, [])
        if isinstance(records, Exception):
            raise records
        content = json.dumps({'findings': records})
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=content))])


ROW = '|Age, years|62 (55-70)|58 (50-66)|61 (54-69)|0.031|'
GOOD = {
    1: [dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='Atrial fibrillation (AF)', sayfa=1,
             kanit_cumle='Atrial fibrillation (AF) was diagnosed in 40 patients.', kanit_turu='metin'),
        dict(bilgi_turu='semptom', baglam='study_finding', klinik_bilgi_en='Chest pain', sayfa=1,
             kanit_cumle='Chest pain was reported.'),
        dict(bilgi_turu='ilac', baglam='study_method', klinik_bilgi_en='Aspirin', sayfa=1, deger='100', unit='mg',
             kanit_cumle='Aspirin 100 mg was given.'),
        # uydurma değer: kanıtta 200 yok -> ret
        dict(bilgi_turu='ilac', baglam='study_method', klinik_bilgi_en='Aspirin', sayfa=1, deger='200',
             kanit_cumle='Aspirin 100 mg was given.'),
        # uydurma bilgi: kanıtta yok -> ret (validate_records)
        dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='heart failure', sayfa=1,
             kanit_cumle='Chest pain was reported.')],
    2: [dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en='Age, years', sayfa=2, kanit_turu='tablo',
             kanit_cumle=ROW, hasta_grubu=g, deger=v, p='0.031')
        for g, v in (('ACS Present (n=94)', '62 (55-70)'), ('ACS Absent (n=20)', '58 (50-66)'),
                     ('Total (n=114)', '61 (54-69)'))],
}


class SourceControlTests(unittest.TestCase):
    def test_structured_fields_need_source(self):
        quote = 'Aspirin 100 mg was given.'
        self.assertIsNone(source_problem({'kanit_cumle': quote, 'deger': '100', 'unit': 'mg'}, quote))
        self.assertIn('deger', source_problem({'kanit_cumle': quote, 'deger': '200'}, quote))
        self.assertIn('p', source_problem({'kanit_cumle': quote, 'p': '0.01'}, quote))
        self.assertIn('hasta_grubu', source_problem({'kanit_cumle': quote, 'hasta_grubu': 'Controls'}, quote))
        # tablo hücresi: grup başlığı satırda değil sayfada (başlıkta)
        self.assertIsNone(source_problem({'kanit_cumle': ROW, 'hasta_grubu': 'ACS Absent (n=20)', 'deger': '58 (50-66)'},
                                         TABLE))


class RepairTests(unittest.TestCase):
    def test_label_homoglyphs_fixed_only_to_valid_labels(self):
        from src.rag_klinik_cikarim import fix_label, TYPES
        self.assertEqual(fix_label('digер', TYPES), 'diger')      # Kiril е, р
        self.assertEqual(fix_label('digər', TYPES), 'diger')      # Azerice ə
        self.assertEqual(fix_label('disease', TYPES), 'disease')  # geçersiz kalır -> ret

    def test_quote_repair_stays_exact_substring(self):
        from src.rag_klinik_cikarim import repair_quote
        self.assertEqual(repair_quote('|' + ROW, TABLE), ROW)
        self.assertEqual(repair_quote('  Chest pain was reported. ', TEXT), 'Chest pain was reported.')
        self.assertIsNone(repair_quote('Chest pain was severe.', TEXT))

    def test_citation_marker_restored_only_when_unique_and_unchanged(self):
        from src.rag_klinik_cikarim import repair_quote
        page = 'Gulf countries report low survival rates _(1)_ . Other regions report better outcomes _(2,3)_ .'
        self.assertEqual(repair_quote('Gulf countries report low survival rates.', page),
                         'Gulf countries report low survival rates _(1)_ .')
        # klinik içerik farklı ("high") -> eşleşme yok
        self.assertIsNone(repair_quote('Gulf countries report high survival rates.', page))
        # aynı atıfsız metin iki yerde -> belirsiz, ret
        twice = 'Survival was low in the cohort _(1)_ . Survival was low in the cohort _(2)_ .'
        self.assertIsNone(repair_quote('Survival was low in the cohort.', twice))
        # model atıfı "(1)" biçiminde yazdıysa: numara sayfadakiyle aynıysa kabul, farklıysa ret
        self.assertEqual(repair_quote('Gulf countries report low survival rates (1).', page),
                         'Gulf countries report low survival rates _(1)_ .')
        self.assertIsNone(repair_quote('Gulf countries report low survival rates (7).', page))
        # <sup> işaretine dokunulmaz (değerin parçası olabilir)
        self.assertIsNone(repair_quote('platelets were 150 x 10/L in all patients',
                                       'platelets were 150 x 10<sup>9</sup>/L in all patients'))

    def test_repaired_record_accepted_with_note(self):
        bad = dict(bilgi_turu='digер', baglam='study_finding', klinik_bilgi_en='Chest pain', sayfa=1,
                   kanit_cumle='Chest pain was reported.   ')
        valid, rejected, _ = extract(PAGES, build_tasks({1: TEXT}, {1: []}), FakeClient({1: [bad]}), 'm')
        self.assertEqual((len(valid), len(rejected)), (1, 0))
        self.assertEqual(valid[0]['bilgi_turu'], 'diger')
        self.assertEqual(sorted(valid[0]['otomatik_duzeltme']), ['bilgi_turu', 'kanit_cumle'])


class ExtractTests(unittest.TestCase):
    def test_extract_validates_and_caches(self):
        tasks = build_tasks(PAGES, {1: ['diseases'], 2: ['measurements']})
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / 'c.jsonl')
            client = FakeClient(GOOD)
            valid, rejected, calls = extract(PAGES, tasks, client, 'm', cache=cache)
            self.assertEqual((len(valid), len(rejected), calls), (6, 2, 2))
            reasons = ' '.join(r['neden'] for r in rejected)
            self.assertIn('deger', reasons)
            self.assertIn('Klinik bilgi', reasons)
            # aynı istekler önbellekten: yeni API çağrısı yok
            again, _, calls2 = extract(PAGES, tasks, client, 'm', cache=Cache(Path(tmp) / 'c.jsonl'))
            self.assertEqual((len(again), calls2, client.calls), (6, 0, 2))

    def test_budget_guard_makes_no_call(self):
        client = FakeClient(GOOD)
        with self.assertRaises(RuntimeError):
            extract(PAGES, build_tasks(PAGES, {1: [], 2: []}), client, 'm', max_calls=1)
        self.assertEqual(client.calls, 0)


class RoutingTests(unittest.TestCase):
    def test_routes_to_presentation_categories(self):
        valid, _, _ = extract(PAGES, build_tasks(PAGES, {1: [], 2: []}), FakeClient(GOOD), 'm')
        out = route(valid, 'x.pdf')
        self.assertEqual([r['klinik_bilgi'] for r in out['diseases']], ['Atrial fibrillation (AF)'])
        self.assertEqual([r['klinik_bilgi'] for r in out['symptoms']], ['Chest pain'])
        self.assertEqual([(r['deger'], r['birim']) for r in out['treatments']], [('100', 'mg')])
        self.assertEqual(len(out['measurements_by_group']), 3)
        self.assertEqual(len(out['measurements']), 1)
        row = out['measurements'][0]
        self.assertEqual([g['grup'] for g in row['gruplar']], ['ACS Present (n=94)', 'ACS Absent (n=20)'])
        self.assertEqual((row['total_group'], row['total_value'], row['p']), ('Total (n=114)', '61 (54-69)', '0.031'))
        self.assertTrue(all(r['kanit'] and r['pdf'] == 'x.pdf' and r['sayfa'] for c in out.values() for r in c))

    def test_text_outcome_and_general(self):
        records = [dict(bilgi_turu='klinik_sonuc', kanit_turu='metin', sayfa=1, kanit_cumle='a'),
                   dict(bilgi_turu='hasta_grubu', kanit_turu='metin', sayfa=1, kanit_cumle='b'),
                   dict(bilgi_turu='olcum', kanit_turu='tablo', sayfa=2, kanit_cumle='c', deger='1')]
        grouped = grouped_rows(records)
        self.assertEqual([categories_of(r, grouped) for r in records],
                         [['clinical_outcomes'], ['general_clinical_findings'], ['clinical_outcomes']])


class PlanTests(unittest.TestCase):
    def test_table_page_always_targets_measurements(self):
        chunks = [{'page_number': 1, 'chunk_index': 0, 'text': TEXT}, {'page_number': 2, 'chunk_index': 0, 'text': TABLE}]
        targets, hits = plan_pages(chunks, search=lambda q, items, k: [items[0]])
        self.assertIn('diseases', targets[1])
        self.assertEqual(targets[2], ['measurements', 'measurements_by_group'])


INTRO = ('Introduction. Coronary artery disease remains a leading cause of death worldwide. ' * 12).strip()
SHORT_CLINICAL = 'Statins reduced major adverse cardiac events in patients with stable coronary artery disease.'
COVERAGE_PAGES = {
    1: INTRO,                                    # retrieval seçti
    2: SHORT_CLINICAL,                           # seçilmedi, kısa (<400 karakter) klinik sayfa -> eklenmeli
    3: '**Figure 2**',                           # seçilmedi, içerik yok -> eklenmemeli
    4: SHORT_CLINICAL.replace('Statins', 'Aspirin') + '\n\n## References\n\n1. Smith A. Heart J. 2020;1:1-2.\n',
    5: '2. Jones B. Circulation. 2019;2:3-4.\n3. Lee C. Lancet. 2018;3:5-6.\n',  # yalnız kaynakça -> eklenmemeli
}


def coverage_doc(pages=COVERAGE_PAGES):
    return {'document_id': 'd', 'filename': 'x.pdf', 'processing_version': 'test',
            'pages': [{'page_number': n, 'text': t} for n, t in pages.items()]}


class CoverageTests(unittest.TestCase):
    def test_references_excluded_clinical_pages_added(self):
        from src.rag_klinik_cikarim import fill_coverage
        added, excluded = fill_coverage(coverage_doc(), {1: ['diseases']})
        # 2: kısa klinik sayfa (400 karakter eşiği bunu dışarıda bırakırdı); 4: kaynakçası çıkarılınca klinik kısım kalır
        self.assertEqual(added, [2, 4])
        self.assertEqual(excluded[5], 'kaynakca')
        self.assertTrue(excluded[3].startswith('icerik_yok'))
        self.assertLess(len(COVERAGE_PAGES[2]), 400)

    def test_existing_tasks_and_cache_keys_unchanged(self):
        from src.rag_klinik_cikarim import fill_coverage, SYSTEM
        targets = {1: ['diseases', 'treatments']}
        snapshot = copy.deepcopy(targets)
        key = lambda t: Cache.key('m', SYSTEM, {k: t[k] for k in ('sayfa', 'hedef_kategoriler', 'page_excerpt')})
        before = [key(t) for t in build_tasks(COVERAGE_PAGES, targets)]
        added, _ = fill_coverage(coverage_doc(), targets)
        self.assertEqual(targets, snapshot)                       # plan_pages sonucu değişmedi
        self.assertEqual([key(t) for t in build_tasks(COVERAGE_PAGES, targets)], before)
        extra = build_tasks(COVERAGE_PAGES, {p: [] for p in added})
        self.assertTrue(all(t['hedef_kategoriler'] == [] for t in extra))
        # kanıt doğrulaması için LLM'e ham sayfa metni gider (kaynakça kırpılmış metin değil)
        self.assertEqual([t['page_excerpt'] for t in extra], [COVERAGE_PAGES[2], COVERAGE_PAGES[4]])

    def test_plan_meta_separates_retrieval_and_coverage_tasks(self):
        from unittest.mock import patch
        from src.rag_klinik_cikarim import run
        doc = coverage_doc()
        first_chunk = lambda q, items, k: [c for c in items if c['page_number'] == 1][:1]
        with patch('src.rag_klinik_cikarim.pdf_pages', return_value=(doc, COVERAGE_PAGES)):
            meta, run_dir = run('x.pdf', plan_only=True, embedder=lambda texts: [[1.0]] * len(texts), search=first_chunk)
        self.assertIsNone(run_dir)
        self.assertEqual(list(meta['secilen_sayfalar']), [1])
        self.assertEqual(meta['kapsama_tamamlama'], [2, 4])
        self.assertEqual((meta['retrieval_gorev_sayisi'], meta['kapsama_gorev_sayisi'], meta['gorev_sayisi']), (1, 2, 3))
        self.assertEqual(meta['atlanan_sayfalar'], [3, 5])


B_LEFT = ('## Discussion\n\nCoronary disease is common worldwide and well described in many cohorts. '
          'The data suggest that COVID-19 may play a role in ACS, especially')
B_RIGHT = 'STEMI, due to increased thrombogenicity, in 42% of cases. Other findings followed.'
B_PAGES = {1: B_LEFT, 2: B_RIGHT}
PART1 = 'The data suggest that COVID-19 may play a role in ACS, especially'
PART2 = 'STEMI, due to increased thrombogenicity, in 42% of cases.'


def boundary_record(**kw):
    rec = dict(bilgi_turu='tani', baglam='literature_background', klinik_bilgi_en='STEMI',
               kanit_parcalari=[{'sayfa': 1, 'metin': PART1}, {'sayfa': 2, 'metin': PART2}],
               sayfa=9, kanit_cumle='uydurma', kanit_turu='tablo')   # LLM'in türetilmiş alanları yok sayılır
    rec.update(kw)
    return rec


def run_boundary(records, pages=B_PAGES):
    from src.rag_klinik_cikarim import build_boundary_tasks
    doc = {'pages': [{'page_number': n, 'text': t} for n, t in pages.items()]}
    tasks = build_boundary_tasks(doc, pages, set(pages))
    return extract(pages, tasks, FakeClient({('sinir', 1): records}), 'm'), tasks


class BoundaryTests(unittest.TestCase):
    def test_boundary_record_accepted_with_derived_fields(self):
        (valid, rejected, calls), tasks = run_boundary([boundary_record()])
        self.assertEqual((len(tasks), len(valid), len(rejected), calls), (1, 1, 0, 1))
        r = valid[0]
        self.assertEqual((r['sayfa'], r['sayfa_bitis'], r['kanit_cumle'], r['kanit_turu'], r['birlestirme']),
                         (1, 2, PART1, 'metin', 'sayfa'))
        # pencereler boundary_excerpt ile ayrı alanlar
        self.assertTrue(B_LEFT.endswith(tasks[0]['left_excerpt']) and B_RIGHT.startswith(tasks[0]['right_excerpt']))
        row = route(valid, 'x.pdf')['diseases'][0]
        self.assertEqual((row['sayfa'], row['sayfa_bitis'], row['birlestirme'], row['kanit']), (1, 2, 'sayfa', PART1))

    def test_part_not_at_page_end_rejected(self):
        bad = boundary_record(kanit_parcalari=[{'sayfa': 1, 'metin': 'COVID-19 may play a role'},
                                               {'sayfa': 2, 'metin': PART2}])
        (valid, rejected, _), _ = run_boundary([bad])
        self.assertEqual((len(valid), rejected[0]['neden']), (0, 'Kanıt parçaları sayfa sonu / sonraki sayfa başı değil'))

    def test_part_from_wrong_page_rejected(self):
        bad = boundary_record(kanit_parcalari=[{'sayfa': 1, 'metin': PART1}, {'sayfa': 2, 'metin': 'ACS, especially'}])
        (valid, rejected, _), _ = run_boundary([bad])
        self.assertEqual(len(valid), 0)
        self.assertIn('penceresinde birebir', rejected[0]['neden'])

    def test_paraphrase_rejected_by_validate_records(self):
        (valid, rejected, _), _ = run_boundary([boundary_record(klinik_bilgi_en='ST-elevation myocardial infarction')])
        self.assertEqual((len(valid), rejected[0]['asama']), (0, 'dogrulama'))

    def test_value_in_second_part_accepted(self):
        (valid, rejected, _), _ = run_boundary([boundary_record(bilgi_turu='olcum', deger='42%')])
        self.assertEqual((len(valid), len(rejected)), (1, 0))
        (valid, rejected, _), _ = run_boundary([boundary_record(bilgi_turu='olcum', deger='55%')])
        self.assertIn('deger', rejected[0]['neden'])

    def test_fact_across_boundary_and_hyphen_not_joined(self):
        (valid, _, _), _ = run_boundary([boundary_record(klinik_bilgi_en='especially STEMI')])
        self.assertEqual(len(valid), 1)                       # tek boşlukla sınırı geçen bilgi kabul
        pages = {1: 'Discussion of markers in this cohort. The HbA1c/C-pep-',
                 2: 'tide ratio was associated with events. Next.'}
        rec = boundary_record(bilgi_turu='olcum', klinik_bilgi_en='C-peptide ratio',
                              kanit_parcalari=[{'sayfa': 1, 'metin': 'The HbA1c/C-pep-'},
                                               {'sayfa': 2, 'metin': 'tide ratio was associated with events.'}])
        (valid, rejected, _), _ = run_boundary([rec], pages)
        self.assertEqual(len(valid), 0)                       # tireli bölünme birleştirilmez
        self.assertEqual(rejected[0]['asama'], 'dogrulama')

    def test_filter_skips_sentence_end_and_reference_pages(self):
        from src.rag_klinik_cikarim import build_boundary_tasks
        pages = {1: INTRO + ' The cohort', 2: 'was followed. Results were reported in full detail here.',
                 3: 'Discussion text ends here.', 4: 'Next page starts.',
                 5: SHORT_CLINICAL + '\n\n## References\n\n1. Smith A. Heart J. 2020;1:1-2. See also',
                 6: '2. Jones B. Circulation. 2019;2:3-4.\n'}
        doc = {'pages': [{'page_number': n, 'text': t} for n, t in pages.items()]}
        tasks = build_boundary_tasks(doc, pages, set(pages))
        # (1,2) cümle ortası -> var; (3,4) nokta ile bitiyor -> yok; (5,6) sayfa 6 yalnız kaynakça -> yok
        self.assertEqual([(t['sayfa'], t['sayfa_bitis']) for t in tasks], [(1, 2)])
        self.assertEqual(build_boundary_tasks(doc, pages, {1, 3}), [])   # çiftin iki sayfası da görev sayfası olmalı


class BoundaryCompatibilityTests(unittest.TestCase):
    def test_page_task_keys_identical_to_previous_formula(self):
        from src.rag_klinik_cikarim import task_request, SYSTEM, BOUNDARY_SYSTEM
        for t in build_tasks(PAGES, {1: ['diseases'], 2: ['measurements']}):
            old = Cache.key('m', SYSTEM, {k: t[k] for k in ('sayfa', 'hedef_kategoriler', 'page_excerpt')})
            self.assertEqual(Cache.key('m', *task_request(t)), old)
        self.assertTrue(BOUNDARY_SYSTEM.startswith(SYSTEM) and BOUNDARY_SYSTEM != SYSTEM)

    def test_sinir_flag_appends_only(self):
        from unittest.mock import patch
        from src.rag_klinik_cikarim import run
        pages = {**COVERAGE_PAGES, 1: INTRO + ' The cohort', 2: 'was followed. ' + SHORT_CLINICAL}
        doc = coverage_doc(pages)
        first = lambda q, items, k: [c for c in items if c['page_number'] == 1][:1]
        emb = lambda texts: [[1.0]] * len(texts)
        with patch('src.rag_klinik_cikarim.pdf_pages', return_value=(doc, pages)):
            off, _ = run('x.pdf', plan_only=True, embedder=emb, search=first)
            on, _ = run('x.pdf', plan_only=True, embedder=emb, search=first, boundary=True)
        self.assertEqual((off['sinir'], off['sinir_gorev_sayisi'], off['sinir_sayfa_ciftleri']), (False, 0, []))
        same = ('secilen_sayfalar', 'kapsama_tamamlama', 'retrieval_gorev_sayisi', 'kapsama_gorev_sayisi')
        self.assertEqual({k: on[k] for k in same}, {k: off[k] for k in same})
        self.assertEqual(on['sinir_sayfa_ciftleri'], [[1, 2]])
        self.assertEqual(on['gorev_sayisi'], off['gorev_sayisi'] + 1)

    def test_default_extract_unchanged_without_boundary(self):
        valid, rejected, calls = extract(PAGES, build_tasks(PAGES, {1: [], 2: []}), FakeClient(GOOD), 'm')
        self.assertEqual((len(valid), len(rejected), calls), (6, 2, 2))
        self.assertTrue(all('sayfa_bitis' not in flat_row for c in route(valid, 'x.pdf').values() for flat_row in c
                            if flat_row.get('kategori') != 'measurements'))


class FieldNameTests(unittest.TestCase):
    BASE = dict(bilgi_turu='semptom', klinik_bilgi_en='Chest pain', kanit_cumle='Chest pain was reported.', sayfa=1)

    def run_page(self, record):
        return extract(PAGES, build_tasks({1: TEXT}, {1: []}), FakeClient({1: [record]}), 'm')

    def test_defined_aliases_mapped_without_changing_values(self):
        from src.rag_klinik_cikarim import normalize_fields
        cases = [('bagcontext', 'baglam', 'study_finding'), ('bagлан', 'baglam', 'study_finding'),
                 ('bag context', 'baglam', 'study_finding'), ('hasta_grupu', 'hasta_grubu', 'ACS Present (n=94)'),
                 ('kanit_cумle', 'kanit_cumle', 'x'), ('kanit_cumple', 'kanit_cumle', 'x')]
        for wrong, right, value in cases:
            out, notes, problem = normalize_fields({'bilgi_turu': 'tani', wrong: value})
            self.assertIsNone(problem)
            self.assertEqual((out.get(right), wrong in out), (value, False), wrong)
            self.assertEqual(notes, [f'alan_adi:{wrong}->{right}'])

    def test_alias_record_accepted_on_page_task(self):
        valid, rejected, _ = self.run_page({**self.BASE, 'bagcontext': 'study_finding'})
        self.assertEqual((len(valid), len(rejected)), (1, 0))
        self.assertEqual(valid[0]['baglam'], 'study_finding')
        self.assertNotIn('bagcontext', valid[0])
        self.assertIn('alan_adi:bagcontext->baglam', valid[0]['otomatik_duzeltme'])
        # Kiril alan adlı kanıt artık doğru alanda; kanıt metni aynen
        valid, _, _ = self.run_page({k: v for k, v in self.BASE.items() if k != 'kanit_cumle'}
                                    | {'kanit_cумle': 'Chest pain was reported.'})
        self.assertEqual(valid[0]['kanit_cumle'], 'Chest pain was reported.')

    def test_unknown_field_rejected_not_silently_kept(self):
        valid, rejected, _ = self.run_page({**self.BASE, 'baglam': 'study_finding', 'klinik_bilgi_tr': 'Göğüs ağrısı'})
        self.assertEqual(len(valid), 0)
        self.assertEqual(rejected[0]['neden'], "Tanınmayan alan adı: 'klinik_bilgi_tr'")

    def test_correct_field_wins_and_conflict_is_recorded(self):
        from src.rag_klinik_cikarim import normalize_fields
        out, notes, problem = normalize_fields({'baglam': 'study_finding', 'bagcontext': 'study_finding'})
        self.assertEqual((out, problem), ({'baglam': 'study_finding'}, None))
        self.assertEqual(notes, ['alan_adi:bagcontext atildi (=baglam)'])
        out, notes, _ = normalize_fields({'hasta_grubu': 'Controls', 'hasta_grupu': 'Cases'})
        self.assertEqual(out, {'hasta_grubu': 'Controls'})            # birleştirme yok, doğru alan korunur
        self.assertIn("atilan deger='Cases'", notes[0])               # atılan değer sessizce kaybolmaz

    def test_boundary_records_use_same_field_validation(self):
        rec = boundary_record(bilgi_turu='olcum', deger='42%')
        rec['bagcontext'] = rec.pop('baglam')
        (valid, rejected, _), _ = run_boundary([rec])
        self.assertEqual((len(valid), valid[0]['baglam']), (1, 'literature_background'))
        (valid, rejected, _), _ = run_boundary([boundary_record(yorum='uydurma alan')])
        self.assertEqual((len(valid), rejected[0]['neden']), (0, "Tanınmayan alan adı: 'yorum'"))

    def test_fields_seen_in_existing_llm_cache_all_accepted_or_aliased(self):
        from src.rag_klinik_cikarim import ALLOWED_FIELDS, FIELD_ALIASES
        seen = {'bilgi_turu', 'klinik_bilgi_en', 'sayfa', 'kanit_turu', 'kanit_cumle', 'baglam', 'deger',
                'hasta_grubu', 'p', 'unit', 'hasta_grupu', 'bagcontext', 'bag context', 'kanit_cумle', 'bagлан',
                'kanit_cumple'}
        self.assertEqual(seen - ALLOWED_FIELDS - set(FIELD_ALIASES), set())


T2_ROW = '|Overweight or<br>obesity|Weight loss|Decreased|Decreased|Increased|'
T2_PAGE = ('##### Table 2: Benefits of lifestyle modification.\n'
           '|Risk factor|Intervention|AF symptoms|AF burden|AF recurrence|\n|---|---|---|---|---|\n'
           + T2_ROW + '\n|Smoking|Quit smoking|Decreased|||\n')
T1_ROW_S = '|Stroke|2.42 (2.17–2.71)|3.6|'
T1_ROW_H = '|Heart failure|4.99 (3.04–8.22)|11.1|'
T1_PAGE = ('##### Table 1: Relative risks.\n|Outcome|Relative risk (95% CI)|Incidence per 1000<br>participant-y|\n'
           '|---|---|---|\n' + T1_ROW_S + '\n' + T1_ROW_H + '\n')


def cell(fact, row, page, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'klinik_sonuc'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=row, sayfa=page, kanit_turu='tablo', **kw)


def table_run(pages, page_records, table_records):
    from src.rag_klinik_cikarim import build_table_tasks
    tasks = build_tasks(pages, {p: ['measurements'] for p in pages}) + build_table_tasks(pages, set(pages))
    client = FakeClient({**page_records, **{('tablo', p): r for p, r in table_records.items()}})
    return extract(pages, tasks, client, 'm'), tasks


class TableTaskTests(unittest.TestCase):
    def test_table2_same_value_in_different_outcome_columns_kept_separately(self):
        old = [cell('Weight loss', T2_ROW, 1, bilgi_turu='tedavi'),                  # tedavi sütunu: korunmalı
               cell('Decreased', T2_ROW, 1), cell('Decreased', T2_ROW, 1), cell('Increased', T2_ROW, 1)]
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, deger=v, sonuc_olcutu_tr=h)
               for h, v in (('AF symptoms', 'Decreased'), ('AF burden', 'Decreased'), ('AF recurrence', 'Increased'))]
        new.append(cell('Overweight or<br>obesity', T2_ROW, 1, deger='Decreased', sonuc_olcutu_tr='AF mortality'))
        (valid, rejected, calls), tasks = table_run({1: T2_PAGE}, {1: old}, {1: new})
        self.assertEqual([t.get('tur', 'sayfa') for t in tasks], ['sayfa', 'tablo'])
        cells = sorted((r['sonuc_olcutu_tr'], r['deger']) for r in valid if r.get('sonuc_olcutu_tr'))
        self.assertEqual(cells, [('AF burden', 'Decreased'), ('AF recurrence', 'Increased'), ('AF symptoms', 'Decreased')])
        self.assertIn('Weight loss', [r['klinik_bilgi_en'] for r in valid])           # tedavi kaydı kaybolmadı
        self.assertNotIn('Decreased', [r['klinik_bilgi_en'] for r in valid])          # hücre değeri-bilgi kaydı gitti
        reasons = collections.Counter(r['asama'] for r in rejected)
        self.assertEqual(reasons['tablo_degisimi'], 2)                                 # 'Decreased' + 'Increased'
        self.assertTrue(any("sonuc_olcutu_tr sayfada yok: 'AF mortality'" == r['neden'] for r in rejected))
        self.assertTrue(all(r['kanit_turu'] == 'tablo' for r in valid))

    def test_table1_rr_records_not_lost_when_table_task_is_partial(self):
        old = [cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='2.42 (2.17–2.71)'),
               cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='3.6'),
               cell('Heart failure', T1_ROW_H, 1, bilgi_turu='olcum', deger='4.99 (3.04–8.22)'),
               cell('Heart failure', T1_ROW_H, 1, bilgi_turu='olcum', deger='11.1')]
        new = [cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='3.6',
                    sonuc_olcutu_tr='Incidence per 1000 participant-y'),
               cell('Heart failure', T1_ROW_H, 1, bilgi_turu='olcum', deger='4.99 (3.04–8.22)',
                    sonuc_olcutu_tr='Relative risk (95% CI)')]
        (valid, rejected, _), _ = table_run({1: T1_PAGE}, {1: old}, {1: new})
        values = {(r['klinik_bilgi_en'], r['deger']): r.get('sonuc_olcutu_tr') for r in valid}
        # tablo görevinin atladığı RR hücresi (Stroke 2.42) ve 11.1 korunur; karşılananlar sütun bilgisiyle gelir
        self.assertEqual(values, {('Stroke', '2.42 (2.17–2.71)'): None, ('Stroke', '3.6'): 'Incidence per 1000 participant-y',
                                  ('Heart failure', '4.99 (3.04–8.22)'): 'Relative risk (95% CI)', ('Heart failure', '11.1'): None})
        self.assertEqual(sum(r['asama'] == 'tablo_degisimi' for r in rejected), 2)

    def test_failed_table_task_keeps_existing_table_records(self):
        old = [cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='2.42 (2.17–2.71)')]
        (valid, rejected, calls), _ = table_run({1: T1_PAGE}, {1: old}, {1: ValueError('Eksik API yanıtı')})
        self.assertEqual([(r['klinik_bilgi_en'], r['deger']) for r in valid], [('Stroke', '2.42 (2.17–2.71)')])
        self.assertEqual([r['asama'] for r in rejected], ['tablo_gorevi'])
        self.assertEqual(calls, 2)

    def test_table_task_record_must_be_table_row(self):
        page = T2_PAGE + '\nLifestyle changes reduced AF burden in obese patients.'
        text = dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en='reduced AF burden', sayfa=1,
                    kanit_cumle='Lifestyle changes reduced AF burden in obese patients.')
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: [text]})
        self.assertEqual((len(valid), rejected[0]['neden']), (0, 'Tablo görevi kaydı tablo satırına dayanmıyor'))

    def test_table_tasks_only_on_table_pages_and_keys_separate(self):
        from src.rag_klinik_cikarim import build_table_tasks, task_request, SYSTEM, TABLE_SYSTEM, BOUNDARY_SYSTEM
        pages = {1: TEXT, 2: T2_PAGE, 3: 'Only one row |a|b| here.'}
        tasks = build_table_tasks(pages, set(pages))
        self.assertEqual([t['sayfa'] for t in tasks], [2])
        page_task = build_tasks(pages, {2: ['measurements']})[0]
        old_key = Cache.key('m', SYSTEM, {k: page_task[k] for k in ('sayfa', 'hedef_kategoriler', 'page_excerpt')})
        self.assertEqual(Cache.key('m', *task_request(page_task)), old_key)        # sayfa görevi anahtarı aynı
        self.assertNotEqual(Cache.key('m', *task_request(tasks[0])), old_key)
        self.assertTrue(TABLE_SYSTEM.startswith(SYSTEM) and BOUNDARY_SYSTEM.startswith(SYSTEM)
                        and TABLE_SYSTEM != BOUNDARY_SYSTEM)

    def test_tablo_flag_appends_only(self):
        from unittest.mock import patch
        from src.rag_klinik_cikarim import run
        pages = {**COVERAGE_PAGES, 6: T2_PAGE}
        doc = coverage_doc(pages)
        first = lambda q, items, k: [c for c in items if c['page_number'] == 1][:1]
        emb = lambda texts: [[1.0]] * len(texts)
        with patch('src.rag_klinik_cikarim.pdf_pages', return_value=(doc, pages)):
            off, _ = run('x.pdf', plan_only=True, embedder=emb, search=first)
            on, _ = run('x.pdf', plan_only=True, embedder=emb, search=first, table=True, boundary=True)
        same = ('secilen_sayfalar', 'kapsama_tamamlama', 'retrieval_gorev_sayisi', 'kapsama_gorev_sayisi')
        self.assertEqual({k: on[k] for k in same}, {k: off[k] for k in same})
        self.assertEqual((off['tablo'], off['tablo_gorev_sayisi']), (False, 0))
        self.assertEqual((on['tablo_sayfalari'], on['tablo_gorev_sayisi']), ([6], 1))
        self.assertEqual(on['gorev_sayisi'], off['gorev_sayisi'] + on['sinir_gorev_sayisi'] + 1)

    def test_unit_normalized_like_group(self):
        self.assertIsNone(source_problem({'kanit_cumle': T1_ROW_S, 'unit': 'per 1000 participant-y'}, T1_PAGE))
        self.assertIn('unit', source_problem({'kanit_cumle': T1_ROW_S, 'unit': 'per 100 person-years'}, T1_PAGE))


class CompareTests(unittest.TestCase):
    def test_match_by_page_evidence_and_fact(self):
        x = [{'sayfa': 1, 'kanit': 'Chest pain was reported.', 'klinik_bilgi': 'Chest pain'},
             {'sayfa': 1, 'kanit': 'Something else entirely here.', 'klinik_bilgi': 'other'}]
        y = [{'sayfa': 1, 'kanit': 'Chest  pain was reported.', 'klinik_bilgi': 'chest pain'},
             {'sayfa': 2, 'kanit': 'Chest pain was reported.', 'klinik_bilgi': 'Chest pain'}]
        r = compare_category(x, y, 'symptoms')
        self.assertEqual((r['eslesen'], r['kesinlik'], r['duyarlilik']), (1, 0.5, 0.5))


class NoDatabaseTests(unittest.TestCase):
    def test_modules_do_not_import_database(self):
        code = ('import sys, src.rag_klinik_cikarim, src.rag_klinik_karsilastir; '
                'sys.exit(1 if "src.database" in sys.modules or "mysql.connector" in sys.modules else 0)')
        self.assertEqual(subprocess.run([sys.executable, '-c', code]).returncode, 0)


if __name__ == '__main__':
    unittest.main()
