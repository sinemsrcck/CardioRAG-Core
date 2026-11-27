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
        # kanit_cumle: doğrulanmış birleşik kanıt (yalnız sol parça değil); ham parçalar kanit_parcalari'nda aynen
        self.assertEqual((r['sayfa'], r['sayfa_bitis'], r['kanit_cumle'], r['kanit_turu'], r['birlestirme']),
                         (1, 2, PART1 + ' ' + PART2, 'metin', 'sayfa'))
        self.assertEqual(r['kanit_parcalari'], [{'sayfa': 1, 'metin': PART1}, {'sayfa': 2, 'metin': PART2}])
        # pencereler boundary_excerpt ile ayrı alanlar
        self.assertTrue(B_LEFT.endswith(tasks[0]['left_excerpt']) and B_RIGHT.startswith(tasks[0]['right_excerpt']))
        row = route(valid, 'x.pdf')['diseases'][0]
        self.assertEqual((row['sayfa'], row['sayfa_bitis'], row['birlestirme'], row['kanit']),
                         (1, 2, 'sayfa', PART1 + ' ' + PART2))

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
        values = {(r['klinik_bilgi_en'], r['deger'], r.get('ci95')): r.get('sonuc_olcutu_tr') for r in valid}
        # tablo görevinin atladığı RR hücresi (Stroke 2.42) ve 11.1 korunur; sütunu artık sayfadaki başlık satırından
        # (tekil, tam hücre) doldurulur; karşılananlar tablo görevinin sütun bilgisiyle gelir. RR hücresinin %95 GA'sı
        # tablo modeliyle (pilot_tablo.row_analyses) ci95 alanına ayrılır.
        self.assertEqual(values, {('Stroke', '2.42', '2.17–2.71'): 'Relative risk (95% CI)',
                                  ('Stroke', '3.6', None): 'Incidence per 1000 participant-y',
                                  ('Heart failure', '4.99', '3.04–8.22'): 'Relative risk (95% CI)',
                                  ('Heart failure', '11.1', None): 'Incidence per 1000 participant-y'})
        rr = next(r for r in valid if r['deger'] == '2.42')
        self.assertEqual((rr['etki_turu'], rr['tablo_modeli']['ham_deger']), ('RR', '2.42 (2.17–2.71)'))
        # karşılanan iki eski kayıt sütunu dolunca tablo görevi kaydıyla birebir aynı olur: validate_records tekrar kuralı
        self.assertEqual(sorted(r['neden'][:6] for r in rejected), ['tekrar', 'tekrar'])

    def test_failed_table_task_keeps_existing_table_records(self):
        old = [cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='2.42 (2.17–2.71)')]
        (valid, rejected, calls), _ = table_run({1: T1_PAGE}, {1: old}, {1: ValueError('Eksik API yanıtı')})
        self.assertEqual([(r['klinik_bilgi_en'], r['deger'], r.get('ci95')) for r in valid],
                         [('Stroke', '2.42', '2.17–2.71')])                             # GA tablo modeliyle ayrılır
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


class MarkupNormalizationTests(unittest.TestCase):
    """AF s.25: tablo görevi doğru hücreleri '**Overweight or<br>obesity**' biçiminde döndürdü, hepsi reddedildi."""

    def test_bold_markup_record_accepted_with_exact_source_text(self):
        new = [cell('**Overweight or<br>obesity**', T2_ROW, 1, deger='**Increased**', sonuc_olcutu_tr='AF recurrence')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: new})
        self.assertEqual(len(valid), 1, rejected)
        r = valid[0]
        self.assertEqual((r['klinik_bilgi_en'], r['deger']), ('Overweight or<br>obesity', 'Increased'))
        self.assertEqual(r['kanit_cumle'], T2_ROW)                                    # ham kanıt aynen
        self.assertIn('klinik_bilgi_en:bicim', r['otomatik_duzeltme'])
        self.assertIn('deger:bicim', r['otomatik_duzeltme'])

    def test_br_and_whitespace_difference_mapped_to_source_span(self):
        from src.rag_klinik_cikarim import source_span
        self.assertEqual(source_span('Overweight or obesity', T2_ROW), 'Overweight or<br>obesity')
        self.assertEqual(source_span('Overweight  or\nobesity', T2_ROW), 'Overweight or<br>obesity')

    def test_meaningful_superscript_and_values_not_lost(self):
        from src.rag_klinik_cikarim import _cmp, source_span
        row = '|WBC|7.2 × 10<sup>9</sup>/L|1.61 (1.38–1.87)<sup>d</sup>|'
        self.assertIsNone(source_span('7.2 × 109/L', row))                            # sayısal üst simge korunur
        self.assertIsNone(source_span('7.2 × 10 9/L', row))
        self.assertNotEqual(_cmp('10<sup>9</sup>/L'), _cmp('109/L'))
        self.assertEqual(_cmp('1.61 (1.38–1.87)<sup>d</sup>'), _cmp('1.61 (1.38–1.87)'))  # dipnot harfi yok sayılır
        self.assertNotEqual(_cmp('1.61 (1.38–1.87)'), _cmp('1.61'))                     # güven aralığı kaybolmaz
        self.assertEqual(_cmp('**AF**<br>**burden**<sup>**_d_**</sup>'), _cmp('AF burden'))

    def test_narrowest_repair_keeps_written_br_and_footnote(self):
        from src.rag_klinik_cikarim import source_span
        row = ('|Physical fitness<sup>g</sup>|B-R<sup>4,53</sup>|Exercise|\n'
               '|Comprehensive<br>care|A<sup>4</sup>|Comprehensive care<br>addressing lifestyle|')
        self.assertEqual(source_span('**Physical fitness<sup>g</sup>**', row), 'Physical fitness<sup>g</sup>')
        self.assertEqual(source_span('**Comprehensive<br>care**', row), 'Comprehensive<br>care')   # <br> ile tekil
        self.assertEqual(source_span('**Comprehensive care**', row), 'Comprehensive care')  # dar kademede tekil
        self.assertIsNone(source_span('**Comprehensive  care**', row))                # <br>/boşluk kademesinde 2 yer

    def test_paraphrase_and_ambiguous_markup_still_rejected(self):
        from src.rag_klinik_cikarim import source_span
        self.assertIsNone(source_span('**Obesity**', T2_ROW))                          # içerik farkı: onarım yok
        self.assertIsNone(source_span('**Decreased**', T2_ROW))                        # satırda 2 kez: belirsiz
        new = [cell('**Obese patients**', T2_ROW, 1, deger='Decreased', sonuc_olcutu_tr='AF symptoms')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: new})
        self.assertEqual((len(valid), rejected[0]['asama']), (0, 'dogrulama'))


MV_TEXT = ('In this meta-analysis, DOACs were associated with lower relative risk of stroke by 32% (2.0% vs 3.0%), '
           'but with 39% increased risk of major bleeding (4.8% vs 3.2%) in patients who received apixaban or warfarin.')


class MultiValueFieldTests(unittest.TestCase):
    """AF s.5/s.11: '32%; 2.0% vs 3.0%' gibi çok değerli alanlar kaydın tamamını reddettiriyordu."""

    def test_each_component_verified_in_evidence(self):
        self.assertIsNone(source_problem({'kanit_cumle': MV_TEXT, 'deger': '32%; 2.0% vs 3.0%'}, MV_TEXT))
        self.assertIn('deger', source_problem({'kanit_cumle': MV_TEXT, 'deger': '32%; 2.5% vs 3.0%'}, MV_TEXT))
        self.assertIn('deger', source_problem({'kanit_cumle': MV_TEXT, 'deger': '32%;; 3.0%'}, MV_TEXT))
        self.assertIn('deger', source_problem({'kanit_cumle': MV_TEXT, 'deger': '32% ... 3.0%'}, MV_TEXT))
        self.assertIsNone(source_problem({'kanit_cumle': MV_TEXT, 'hasta_grubu': 'apixaban; warfarin'}, MV_TEXT))
        self.assertIn('hasta_grubu', source_problem({'kanit_cumle': MV_TEXT, 'hasta_grubu': 'apixaban; dabigatran'},
                                                    MV_TEXT))

    def test_group_value_relation_must_be_unambiguous(self):
        ok = {'kanit_cumle': MV_TEXT, 'deger': '2.0%; 3.0%', 'hasta_grubu': 'apixaban; warfarin'}
        self.assertIsNone(source_problem(ok, MV_TEXT))
        bad = {**ok, 'deger': '32%; 2.0%; 3.0%'}
        self.assertIn('bileşen sayısı', source_problem(bad, MV_TEXT))

    def test_record_kept_with_field_unchanged_and_unverified_field_rejects_record(self):
        good = dict(bilgi_turu='olcum', baglam='literature_background', sayfa=1, kanit_cumle=MV_TEXT,
                    klinik_bilgi_en='lower relative risk of stroke by 32%', deger='32%; 2.0% vs 3.0%')
        bad = {**good, 'klinik_bilgi_en': 'major bleeding', 'deger': '39%; 5.0% vs 3.2%'}
        valid, rejected, _ = extract({1: MV_TEXT}, build_tasks({1: MV_TEXT}, {1: []}), FakeClient({1: [good, bad]}), 'm')
        self.assertEqual([(r['klinik_bilgi_en'], r['deger']) for r in valid],
                         [('lower relative risk of stroke by 32%', '32%; 2.0% vs 3.0%')])
        self.assertEqual(len(rejected), 1)                                             # alan silinip kabul edilmez
        self.assertIn('deger kanıt cümlesinde birebir yok', rejected[0]['neden'])


W_LEFT = ('AF stage 4 is permanent AF. Treatment decisions vary by patient.\n\n### Prognosis\n\n'
          'In a meta-analysis, AF was associated with absolute risk increase of approximately 3.6 for')
W_RIGHT = 'stroke, 11.1 for HF, and 3.8 for mortality (Table 1).<sup>38</sup> Dementia risk also rose. More text here.'
W_PAGES = {1: W_LEFT, 2: W_RIGHT}
W_SENT_L = 'In a meta-analysis, AF was associated with absolute risk increase of approximately 3.6 for'
W_SENT_R = 'stroke, 11.1 for HF, and 3.8 for mortality (Table 1).<sup>38</sup>'


def window_record(**kw):
    rec = dict(bilgi_turu='olcum', baglam='literature_background', klinik_bilgi_en='3.6 for stroke',
               kanit_parcalari=[{'sayfa': 1, 'metin': W_LEFT}, {'sayfa': 2, 'metin': W_RIGHT}])   # pencerenin tamamı
    rec.update(kw)
    return rec


def boundary_and_page_run(boundary_records, page2_records):
    from src.rag_klinik_cikarim import build_boundary_tasks
    doc = {'pages': [{'page_number': n, 'text': t} for n, t in W_PAGES.items()]}
    tasks = build_tasks(W_PAGES, {2: []}) + build_boundary_tasks(doc, W_PAGES, set(W_PAGES))
    return extract(W_PAGES, tasks, FakeClient({2: page2_records, ('sinir', 1): boundary_records}), 'm')


class BoundaryEvidenceTests(unittest.TestCase):
    """AF s.5-6 / s.6-7: model 2200 karakterlik pencereyi parça diye döndürdü; kanit_cumle yalnız sol parçaydı."""

    def test_whole_window_trimmed_to_crossing_sentence_and_joined(self):
        (valid, rejected, _), _ = run_boundary([window_record()], W_PAGES)
        self.assertEqual(len(valid), 1, rejected)
        r = valid[0]
        self.assertEqual(r['kanit_parcalari'], [{'sayfa': 1, 'metin': W_SENT_L}, {'sayfa': 2, 'metin': W_SENT_R}])
        self.assertEqual(r['kanit_cumle'], W_SENT_L + ' ' + W_SENT_R)
        self.assertIn('kanit_parcalari', r['otomatik_duzeltme'])

    def test_fact_from_other_sentence_of_window_rejected(self):
        (valid, rejected, _), _ = run_boundary([window_record(klinik_bilgi_en='Dementia risk also rose')], W_PAGES)
        self.assertEqual((len(valid), rejected[0]['asama']), (0, 'dogrulama'))

    def test_trim_keeps_abbreviations_and_decimals_inside_sentence(self):
        from src.rag_klinik_cikarim import trim_boundary_parts
        left, right = trim_boundary_parts('Old text. Smith et al. reported HR 0.48 vs.', 'placebo in 2020. Next one.')
        self.assertEqual((left, right), ('Smith et al. reported HR 0.48 vs.', 'placebo in 2020.'))
        self.assertEqual(trim_boundary_parts('Ends here.', 'Next.'), ('', 'Next.'))

    def test_true_duplicate_single_page_record_dropped(self):
        single = dict(bilgi_turu='olcum', baglam='literature_background', sayfa=2, klinik_bilgi_en='11.1 for HF',
                      kanit_cumle=W_SENT_R)
        valid, rejected, _ = boundary_and_page_run([window_record(klinik_bilgi_en='11.1 for HF')], [single])
        self.assertEqual([(r['klinik_bilgi_en'], r.get('sayfa_bitis')) for r in valid], [('11.1 for HF', 2)])
        self.assertEqual([r['asama'] for r in rejected], ['sinir_tekrari'])

    def test_different_type_value_or_fact_not_merged(self):
        base = dict(baglam='literature_background', sayfa=2, kanit_cumle=W_SENT_R)
        singles = [dict(base, bilgi_turu='klinik_sonuc', klinik_bilgi_en='11.1 for HF'),      # tür farklı
                   dict(base, bilgi_turu='olcum', klinik_bilgi_en='11.1 for HF', deger='11.1'),  # değer farklı
                   dict(base, bilgi_turu='olcum', klinik_bilgi_en='3.8 for mortality')]       # bilgi farklı
        valid, rejected, _ = boundary_and_page_run([window_record(klinik_bilgi_en='11.1 for HF')], singles)
        self.assertEqual(len(valid), 4, rejected)
        self.assertFalse(any(r['asama'] == 'sinir_tekrari' for r in rejected))


T1_ROW_MI = '|Myocardial<br>infarction|1.61 (1.38–1.87)<sup>d</sup>|1.4<sup>d</sup>|'
T1_PAGE_MI = T1_PAGE + T1_ROW_MI + '\n'


class TableDuplicateTests(unittest.TestCase):
    """AF s.24: '1.61 (1.38–1.87)' ve '…<sup>d</sup>' aynı hücre olduğu halde iki kayıt kaldı."""

    def test_page_record_superseded_despite_footnote_superscript(self):
        old = [cell('Myocardial<br>infarction', T1_ROW_MI, 1, bilgi_turu='olcum', deger='1.61 (1.38–1.87)')]
        new = [cell('Myocardial<br>infarction', T1_ROW_MI, 1, bilgi_turu='olcum', deger='1.61 (1.38–1.87)<sup>d</sup>',
                    sonuc_olcutu_tr='Relative risk (95% CI)')]
        (valid, rejected, _), _ = table_run({1: T1_PAGE_MI}, {1: old}, {1: new})
        self.assertEqual([r['deger'] for r in valid], ['1.61 (1.38–1.87)<sup>d</sup>'])
        self.assertEqual([r['asama'] for r in rejected], ['tablo_degisimi'])

    def test_format_only_duplicate_cells_collapsed_but_other_columns_kept(self):
        new = [cell('Myocardial<br>infarction', T1_ROW_MI, 1, bilgi_turu='olcum', deger='1.4<sup>d</sup>',
                    sonuc_olcutu_tr='Incidence per 1000<br>participant-y'),
               cell('Myocardial<br>infarction', T1_ROW_MI, 1, bilgi_turu='olcum', deger='1.4',
                    sonuc_olcutu_tr='Incidence per 1000 participant-y'),
               cell('Myocardial<br>infarction', T1_ROW_MI, 1, bilgi_turu='olcum', deger='1.61 (1.38–1.87)',
                    sonuc_olcutu_tr='Relative risk (95% CI)')]
        (valid, rejected, _), _ = table_run({1: T1_PAGE_MI}, {1: []}, {1: new})
        self.assertEqual([r['deger'] for r in valid], ['1.4<sup>d</sup>', '1.61 (1.38–1.87)'])
        self.assertEqual([r['asama'] for r in rejected], ['tablo_tekrari'])

    def test_treatment_text_record_not_superseded_by_cell_without_column(self):
        # AF s.25: öneri sütunu hücresi (sonuç ölçütü/grup yok) sayfa görevinin tedavi kaydını silmemeli
        old = [cell('Weight loss', T2_ROW, 1, bilgi_turu='tedavi')]
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='tedavi', deger='Weight loss')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: old}, {1: new})
        self.assertIn(('Weight loss', None), [(r['klinik_bilgi_en'], r.get('deger')) for r in valid])
        self.assertFalse(any(r['asama'] == 'tablo_degisimi' for r in rejected))

    def test_same_value_in_other_outcome_column_not_superseded_or_dropped(self):
        old = [cell('Overweight or<br>obesity', T2_ROW, 1, deger='Decreased', sonuc_olcutu_tr='AF symptoms')]
        new = [cell('**Overweight or<br>obesity**', T2_ROW, 1, deger='Decreased', sonuc_olcutu_tr='AF burden')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: old}, {1: new})
        self.assertEqual(sorted(r['sonuc_olcutu_tr'] for r in valid), ['AF burden', 'AF symptoms'])
        self.assertEqual(rejected, [])


T3_ROW = ('|TRIAL-X,<sup>9</sup>2008|Ablation: 41 Control:<br>40|AAD|Composite score<br>Ablation: LVEF 35 ± 9% '
          '6MWT 340 ± 49 m Control: 28 ± 6% 6MWT 297 ± 36 m (P < .001)|6|')
T3_PAGE = ('**Table 3.** Trials\n|Source|Participants|Control|Primary outcome|Follow-up|\n|---|---|---|---|---|\n'
           + T3_ROW + '\n')
DRUG_ROW = '|Aspirin|100 mg|daily|'
DRUG_PAGE = '##### Table 4: Drugs.\n|Drug|Dose|Schedule|\n|---|---|---|\n' + DRUG_ROW + '\n'


class TableCellRepresentationTests(unittest.TestCase):
    """AF s.25/s.28: satır etiketi = değer hücreleri ve 'tedavi: <satır etiketi>' kayıtları (SQL treatments adı)."""

    def test_row_label_as_value_cell_dropped(self):
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='diger', deger='Overweight or<br>obesity')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: new})
        self.assertEqual(valid, [])
        self.assertEqual((rejected[0]['asama'], rejected[0]['neden']),
                         ('tablo_hucresi', 'Tablo hücresi satır etiketinin kendisi; yeni klinik bilgi yok'))

    def test_intervention_cell_text_moved_to_fact(self):
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='tedavi', deger='Weight loss')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: new})
        self.assertEqual(len(valid), 1, rejected)
        r = valid[0]
        self.assertEqual((r['klinik_bilgi_en'], r.get('deger'), r['kanit_cumle']), ('Weight loss', None, T2_ROW))
        self.assertIn('tablo_hucresi:deger->klinik_bilgi_en', r['otomatik_duzeltme'])
        self.assertEqual(categories_of(r, grouped_rows(valid)), ['treatments'])   # clinical_outcomes'a düşmez

    def test_medication_numeric_value_and_unit_preserved(self):
        dose = cell('Aspirin', DRUG_ROW, 1, bilgi_turu='ilac', deger='100 mg')
        unit = cell('Aspirin', DRUG_ROW, 1, bilgi_turu='doz', deger='100', unit='mg')
        text = cell('Aspirin', DRUG_ROW, 1, bilgi_turu='doz', deger='daily')        # rakamsız ilaç/doz hücresi
        (valid, rejected, _), _ = table_run({1: DRUG_PAGE}, {1: []}, {1: [dose, unit, text]})
        got = sorted((r['bilgi_turu'], r['klinik_bilgi_en'], r.get('deger'), r.get('unit')) for r in valid)
        self.assertEqual(got, [('doz', 'Aspirin', '100', 'mg'), ('doz', 'daily', None, None),
                               ('ilac', 'Aspirin', '100 mg', None)], rejected)

    def test_not_whole_cell_or_with_column_unchanged(self):
        part = cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='tedavi', deger='Weight')    # hücrenin parçası
        col = cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='tedavi', deger='Decreased',
                   sonuc_olcutu_tr='AF symptoms')                                              # sonuç sütunu var
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: [part, col]})
        self.assertEqual([(r['klinik_bilgi_en'], r['deger']) for r in valid], [('Overweight or<br>obesity', 'Decreased')])
        # hücrenin parçası olan sütunsuz değer: sütunu tekil belirlenemez -> aktarım dışı, ham kaydıyla korunur
        self.assertEqual([(r['asama'], r['kayit']['deger']) for r in rejected], [('tablo_baglami_belirsiz', 'Weight')])

    def test_transformed_record_still_source_checked_and_deduplicated(self):
        old = [cell('Weight loss', T2_ROW, 1, bilgi_turu='tedavi')]                          # sayfa görevi
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, bilgi_turu='tedavi', deger='Weight loss')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: old}, {1: new})
        self.assertEqual([r['klinik_bilgi_en'] for r in valid], ['Weight loss'])             # gerçek tekrar tek kayıt
        self.assertEqual(len(rejected), 1)


def gcell(fact, row, group, value, **kw):
    return dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en=fact, kanit_cumle=row, sayfa=1,
                kanit_turu='tablo', hasta_grubu=group, deger=value, **kw)


SEX_ROW = '|Male<br>Female|152<br>37|79<br>17|73<br>20|0.584|'


class GroupComparisonTests(unittest.TestCase):
    """AF s.27 PABA-CHF: tek 'Primary outcome' hücresinden ayrıştırılan Ablation/Control alt sonuçları satırı grup
    karşılaştırması yapıyor, LVEF/6MWT/MLHFQ tek measurements satırında birleşiyordu."""

    def test_in_cell_sub_results_are_not_a_group_comparison(self):
        recs = [gcell('LVEF 35 ± 9%', T3_ROW, 'Ablation', '35 ± 9%'), gcell('6MWT 340 ± 49 m', T3_ROW, 'Ablation', '340 ± 49 m'),
                gcell('Control: 28 ± 6%', T3_ROW, 'Control', '28 ± 6%'),
                cell('TRIAL-X,<sup>9</sup>2008', T3_ROW, 1, bilgi_turu='diger', deger='6')]
        self.assertEqual(grouped_rows(recs), set())
        out = route(recs, 'x.pdf')
        self.assertEqual(out['measurements'], [])
        self.assertEqual(len(out['measurements_by_group']), 3)                        # grup başına değer korunur
        self.assertEqual(len(out['clinical_outcomes']), 4)                            # satırın diğer hücreleri

    def test_real_group_table_kept_and_parameter_from_row(self):
        recs = [dict(GOOD[2][0], klinik_bilgi_en='Age'),                              # kayıt bilgisi satırdan farklı
                GOOD[2][1], GOOD[2][2]]
        self.assertEqual(len(grouped_rows(recs)), 1)
        rows = route(recs, 'x.pdf')['measurements']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['klinik_bilgi'], 'Age, years')                       # doğrulanmış satır değişkeni
        self.assertEqual(rows[0]['gruplar'], [{'grup': 'ACS Present (n=94)', 'deger': '62 (55-70)'},
                                              {'grup': 'ACS Absent (n=20)', 'deger': '58 (50-66)'}])
        self.assertEqual((rows[0]['total_group'], rows[0]['total_value'], rows[0]['p']),
                         ('Total (n=114)', '61 (54-69)', '0.031'))

    def test_stacked_variables_in_one_row_are_separate_comparisons(self):
        recs = [gcell(f, SEX_ROW, g, v) for g, vals in (('Total', ('152', '37')), ('Study group', ('79', '17')),
                                                         ('Control group', ('73', '20')))
                for f, v in zip(('Male', 'Female'), vals)]
        rows = {r['klinik_bilgi']: r for r in route(recs, 'x.pdf')['measurements']}
        self.assertEqual(set(rows), {'Male', 'Female'})
        self.assertEqual(rows['Female']['gruplar'], [{'grup': 'Study group', 'deger': '17'},
                                                     {'grup': 'Control group', 'deger': '20'}])
        self.assertEqual(rows['Male']['total_value'], '152')

    def test_single_group_or_total_only_is_not_comparison(self):
        self.assertEqual(grouped_rows([GOOD[2][2]]), set())                           # yalnız Total
        self.assertEqual(grouped_rows([GOOD[2][0]]), set())                           # tek grup


FR_TEXT = ("Les personnes ayant été victimes d'un arrêt cardiaque étaient principalement de sexe masculin et "
           "présentaient davantage de comorbidités que dans les autres régions.")


class NonEnglishTests(unittest.TestCase):
    """OHCA s.8-9: Fransızca özetten 14 kayıt kayitlar.json'a giriyordu (değerlendirme yalnız bellekte eliyordu)."""

    def test_non_english_record_rejected_with_reason(self):
        pages = {1: TEXT + '\n' + FR_TEXT}
        fr = dict(bilgi_turu='klinik_sonuc', baglam='literature_background', sayfa=1, kanit_cumle=FR_TEXT,
                  klinik_bilgi_en='principalement de sexe masculin')
        en = GOOD[1][1]
        valid, rejected, _ = extract(pages, build_tasks(pages, {1: []}), FakeClient({1: [fr, en]}), 'm')
        self.assertEqual([r['klinik_bilgi_en'] for r in valid], ['Chest pain'])
        self.assertEqual((rejected[0]['asama'], rejected[0]['neden']),
                         ('dil', 'Kanıt İngilizce değil (rag_son_filtre.is_non_english)'))

    def test_english_boundary_record_kept_with_all_parts(self):
        (valid, rejected, _), _ = run_boundary([boundary_record()])
        self.assertEqual((len(valid), len(valid[0]['kanit_parcalari'])), (1, 2))


class CellBoundaryTests(unittest.TestCase):
    def test_real_cell_boundary_violation_detected(self):
        from src.rag_klinik_cikarim import crosses_cell_boundary
        self.assertTrue(crosses_cell_boundary('Stroke|2.42', T1_ROW_S))
        self.assertTrue(crosses_cell_boundary(T1_ROW_S, T1_ROW_S))                    # bütün satır bilgi olamaz
        self.assertFalse(crosses_cell_boundary('Stroke', T1_ROW_S))
        self.assertFalse(crosses_cell_boundary('A \\| B', '|A \\| B|3.6|'))            # kaçışlı | ayırıcı değil
        self.assertFalse(crosses_cell_boundary('3.6', '|3.6|x 3.6|y|'))                 # tek hücrede geçen konum var
        self.assertFalse(crosses_cell_boundary('a|b', 'a|b in text'))                  # tablo satırı değil

    def test_cross_cell_fact_rejected_in_extraction(self):
        bad = cell('Stroke|2.42', T1_ROW_S, 1, bilgi_turu='olcum', deger='2.42 (2.17–2.71)',
                   sonuc_olcutu_tr='Relative risk (95% CI)')
        good = cell('Stroke', T1_ROW_S, 1, bilgi_turu='olcum', deger='3.6', sonuc_olcutu_tr='Relative risk (95% CI)')
        (valid, rejected, _), _ = table_run({1: T1_PAGE}, {1: []}, {1: [bad, good]})
        self.assertEqual([r['klinik_bilgi_en'] for r in valid], ['Stroke'])
        self.assertEqual(rejected[0]['asama'], 'tablo_hucresi')
        self.assertIn('hücre sınırını aşıyor', rejected[0]['neden'])


G_ROW = '|Age (years)|62|58|0.031|'
G_PAGE = '##### Table 1: Baseline.\n|Variable|Event (n=372)|No event (n=853)|p|\n|---|---|---|---|\n' + G_ROW + '\n'
OR_ROW = '|ELR|2.735|0.002|'
OR_PAGE = '##### Table 2: Regression.\n|Variable|OR|p|\n|---|---|---|\n' + OR_ROW + '\n'


def grp(fact, row, group, value, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'olcum'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=row, sayfa=1, kanit_turu='tablo', hasta_grubu=group, deger=value, **kw)


class PDuplicateTests(unittest.TestCase):
    """TK36/TK37: p sütunu, p'si zaten grup kayıtlarında olan satırda ayrı kayıt olarak çıkarılıyordu."""

    def test_p_column_record_dropped_when_groups_carry_p(self):
        recs = [grp('Age (years)', G_ROW, 'Event (n=372)', '62', p='0.031'),
                grp('Age (years)', G_ROW, 'No event (n=853)', '58', p='0.031'),
                cell('Age (years)', G_ROW, 1, bilgi_turu='olcum', deger='0.031', p='0.031')]
        (valid, rejected, _), _ = table_run({1: G_PAGE}, {1: []}, {1: recs})
        self.assertEqual(sorted(r['deger'] for r in valid), ['58', '62'])
        self.assertEqual([r['asama'] for r in rejected], ['p_tekrari'])

    def test_independent_statistic_kept(self):
        # Regresyon satırı: tablo modeli p sütununu OR sütununa bağlıyor ve OR kaydı aynı p'yi taşıyor -> p hücresinin
        # ayrı kaydı tekrar (TK36 'bağımsız p kayıtları'); değer kaybolmaz, OR kaydının p alanında.
        recs = [cell('ELR', OR_ROW, 1, bilgi_turu='klinik_sonuc', deger='2.735', sonuc_olcutu_tr='OR', p='0.002'),
                cell('ELR', OR_ROW, 1, bilgi_turu='klinik_sonuc', deger='0.002', sonuc_olcutu_tr='p')]
        (valid, rejected, _), _ = table_run({1: OR_PAGE}, {1: []}, {1: recs})
        self.assertEqual([(r['deger'], r.get('p')) for r in valid], [('2.735', '0.002')], rejected)
        self.assertEqual([r['asama'] for r in rejected], ['etki_p_tekrari'])
        # Sahip etki kaydı yoksa p hücresi bağımsız bulgu olarak kalır (silinmez).
        (valid, rejected, _), _ = table_run({1: OR_PAGE}, {1: []}, {1: recs[1:]})
        self.assertEqual([r['deger'] for r in valid], ['0.002'], rejected)
        self.assertEqual(valid[0]['tablo_modeli']['durum'], 'p_hucresi')
        # grup kayıtlarının p'si farklı -> p hücresi kayıt kalır
        row = G_ROW.replace('0.031|', '0.031|0.5|')
        page = ('##### Table 1\n|Variable|Event (n=372)|No event (n=853)|p|p (adjusted)|\n|---|---|---|---|---|\n'
                + row + '\n')
        recs = [grp('Age (years)', row, 'Event (n=372)', '62', p='0.5'),
                cell('Age (years)', row, 1, bilgi_turu='olcum', deger='0.031')]
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: recs})
        # p hücresi kaydı silinmez. Grup kaydının p'si (0.5) tablo modelinin p sütunuyla (0.031) çelişiyor: olası
        # ilişki tutarsızlığı otomatik kabul edilmez, kayıt gerekçesiyle incelemeye ayrılır (silinmez).
        self.assertEqual([r['deger'] for r in valid], ['0.031'], rejected)
        self.assertEqual([(r['asama'], r['kayit']['deger']) for r in rejected], [('tablo_hizalama_inceleme', '62')])
        self.assertIn('tutarsızlığı', rejected[0]['neden'])


J_ROW = '|Age||64 (53-74)|63 (52-72)|0.100|'
J_PAGE = ('##### Table I\n|Variable||**Total (n=114)**<br>**Median (Q1-Q3)**|**ACS Absent (n=94)**<br>**Median (Q1-Q3)**|**P**|'
          '\n|---|---|---|---|---|\n' + J_ROW + '\n')


class CellDuplicateTests(unittest.TestCase):
    """JCPSP/TK36: sayfa ve tablo görevi aynı hücreyi farklı (biçimli / alt başlıklı) etiketle veriyordu."""

    def test_same_cell_with_header_subline_label_collapsed(self):
        page = [grp('Age', J_ROW, 'Total (n=114)', '64 (53-74)', p='0.100')]
        table = [grp('Age', J_ROW, '**Total (n=114)**<br>**Median (Q1-Q3)**', '64 (53-74)')]
        (valid, rejected, _), _ = table_run({1: J_PAGE}, {1: page}, {1: table})
        self.assertEqual([(r['hasta_grubu'], r.get('p')) for r in valid], [('Total (n=114)', '0.100')])
        self.assertEqual([r['asama'] for r in rejected], ['hucre_tekrari'])

    def test_equal_values_in_different_cells_are_not_duplicates(self):
        row = '|Score|5|5|0.9|'
        page = '##### T\n|Variable|Group A|Group B|p|\n|---|---|---|---|\n' + row + '\n'
        recs = [grp('Score', row, 'Group A', '5'), grp('Score', row, 'Group B', '5')]
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: recs})
        self.assertEqual(sorted(r['hasta_grubu'] for r in valid), ['Group A', 'Group B'], rejected)

    def test_stacked_variables_and_specific_type_preserved(self):
        recs = [gcell('Male', SEX_ROW, 'Study group', '79'), gcell('Female', SEX_ROW, 'Study group', '17')]
        page = '##### T\n|Sex|Total|Study group|Control group|p|\n|---|---|---|---|---|\n' + SEX_ROW + '\n'
        (valid, _, _), _ = table_run({1: page}, {1: []}, {1: recs})
        self.assertEqual(sorted(r['klinik_bilgi_en'] for r in valid), ['Female', 'Male'])  # 'male' 'female' içinde değil
        tani = grp('Age', G_ROW, 'Event (n=372)', '62', bilgi_turu='tani')
        olcum = grp('Age (years)', G_ROW, 'Event (n=372)', '62')
        (valid, rejected, _), _ = table_run({1: G_PAGE}, {1: [olcum]}, {1: [tani]})
        self.assertEqual([r['bilgi_turu'] for r in valid], ['tani'])                 # özgül tür kalır
        self.assertEqual([r['asama'] for r in rejected], ['hucre_tekrari'])


S_ROW = '|PET STRESS|85%|86%|'
S_PAGE = '##### Table 2: Tests.\n|Test|SEN|SPE|\n|---|---|---|\n' + S_ROW + '\n'
PAIR_ROW = '|Pairwise comparation for AUC|P (A-B)=0.291|P (A-C)=0.323|'
PAIR_PAGE = '##### Table 3\n|Comparison|AB|AC|\n|---|---|---|\n' + PAIR_ROW + '\n'


class StatisticLabelTests(unittest.TestCase):
    """CAD: 'SEN/SPE' hasta_grubu olarak geldi, tanısal performans satırı grup karşılaştırması sayıldı (45 kayıt)."""

    def test_measure_label_moved_from_group_and_verified(self):
        recs = [grp('PET STRESS', S_ROW, 'SEN', '85%'), grp('PET STRESS', S_ROW, 'SPE', '86%')]
        (valid, rejected, _), _ = table_run({1: S_PAGE}, {1: []}, {1: recs})
        self.assertEqual(sorted((r.get('hasta_grubu'), r['sonuc_olcutu_tr'], r['deger']) for r in valid),
                         [(None, 'SEN', '85%'), (None, 'SPE', '86%')], rejected)
        self.assertEqual(grouped_rows(valid), set())                                   # grup karşılaştırması değil
        self.assertIn('hasta_grubu->sonuc_olcutu_tr:istatistik_etiketi', valid[0]['otomatik_duzeltme'])

    def test_comparison_label_inside_cell(self):
        recs = [grp('Pairwise comparation for AUC', PAIR_ROW, 'P (A-B)', '0.291')]
        (valid, rejected, _), _ = table_run({1: PAIR_PAGE}, {1: []}, {1: recs})
        self.assertEqual([(r.get('hasta_grubu'), r['sonuc_olcutu_tr']) for r in valid], [(None, 'P (A-B)')], rejected)

    def test_ambiguous_or_unverifiable_rejected_not_guessed(self):
        other = grp('PET STRESS', S_ROW, 'SEN', '85%', sonuc_olcutu_tr='SPE')           # başka ölçüt: belirsiz
        loose = grp('PET STRESS', S_ROW, 'SEN', '85')                                   # tam hücre değil
        (valid, rejected, _), _ = table_run({1: S_PAGE}, {1: []}, {1: [other, loose]})
        self.assertEqual(valid, [])
        self.assertTrue(all(r['asama'] == 'tablo_hucresi' and 'İstatistik etiketi' in r['neden'] for r in rejected))

    def test_real_groups_and_single_letters_untouched(self):
        from src.rag_klinik_cikarim import is_statistic_label
        for label in ('ACS Present (n=94)', 'Study group', 'B', 'PLR', 'Total', 'Median (Q1-Q3)'):
            self.assertFalse(is_statistic_label(label), label)
        for label in ('SEN', '**SPE**', 'AUC', 'OR', 'aHR', 'p value', 'P (A-B)', 'P (A vs B)', '95% CI'):
            self.assertTrue(is_statistic_label(label), label)


MH_ROW = '|Al Hasan, 2019|Kuwait|314|54.7 (GP); 61|70|'
MH_ROW2 = '|Batt, 2017|UAE|14|43|14|'
MH_PAGE = ('**Table 1** Characteristics of studies\n'
           '|**Study**||**Characteristi**|**cs**|**Outcome**|\n|---|---|---|---|---|\n'
           '||**Country**|**No. in**<br>**sample**|**Mean age,**<br>**years**|**ROSC, %**|\n'
           '|Adult cases|||||\n' + MH_ROW + '\n|||||(EMS)|\n' + MH_ROW2 + '\n')
RD_ROW = '|Risc of ACS||48 (42.1)|7.8 (2.4-25.1)<br>or|'
RD_PAGE = ('|||**Total (n=114)**<br>**Median (Q1-Q3)**|**P**|\n|---|---|---|---|\n|Age||64 (53-74)|0.100|\n'
           '|||Total (n=114)<br>n (%)|P|\n' + RD_ROW + '\n')


def bare(fact, row, value, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'olcum'), baglam='literature_background', klinik_bilgi_en=fact,
                kanit_cumle=row, sayfa=1, kanit_turu='tablo', deger=value, **kw)


# Literatür / özet tablosu fikstürlerinin (satır adları yazar, yıl; ilk sütun 'Study') yapısı aynı, kimliği nötr
# kopyası: hücre bağlamı mekanizmalarını literatür kuralından bağımsız sınamak için. Literatür hali ayrıca sınanır
# (src.rag_tablo_hizalama: incelemeye ayrılır).
NEUTRAL_NAMES = (('Al Hasan, 2019', 'Cohort A'), ('Batt, 2017', 'Cohort B'), ('**Study**', '**Cohort**'),
                 ('Characteristics of studies', 'Characteristics of cohorts'), ('**Table 1** Studies', '**Table 1** Cohorts'))


def neutral(text):
    for old, new in NEUTRAL_NAMES:
        text = text.replace(old, new)
    return text


class ColumnContextTests(unittest.TestCase):
    """OHCA Tablo 1 (124), AF Tablo 3: hücre değeri sütun anlamı olmadan geliyordu."""

    def test_header_from_bold_sublayer_not_fragmented_super_header(self):
        from src.rag_klinik_cikarim import column_header, clean_label
        header, problem = column_header({'kanit_cumle': MH_ROW}, MH_PAGE)
        self.assertIsNone(problem)
        self.assertEqual([clean_label(c) for c in header], ['', 'Country', 'No. in sample', 'Mean age, years', 'ROSC, %'])
        # '(EMS)' devam satırı başlık sayılmaz; alttaki satır da aynı başlığı bulur
        self.assertEqual(column_header({'kanit_cumle': MH_ROW2}, MH_PAGE)[0], header)

    def test_mid_table_header_redefinition_used(self):
        from src.rag_klinik_cikarim import column_header, clean_label
        header, _ = column_header({'kanit_cumle': RD_ROW}, RD_PAGE)
        self.assertEqual(clean_label(header[2]), 'Total (n=114) n (%)')

    def test_contextless_values_filled_only_when_unique(self):
        row, row2, page = neutral(MH_ROW), neutral(MH_ROW2), neutral(MH_PAGE)
        recs = [bare('Cohort A', row, '314'), bare('Cohort A', row, '54.7 (GP); 61'),
                bare('Cohort B', row2, '14')]                                             # 14 iki hücrede
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: recs})
        self.assertEqual(sorted((r['deger'], r['sonuc_olcutu_tr']) for r in valid),
                         [('314', 'No. in sample'), ('54.7 (GP); 61', 'Mean age, years')])
        self.assertTrue(all('tablo_hucresi:sonuc_olcutu_tr<-sutun_basligi' in r['otomatik_duzeltme'] for r in valid))
        self.assertEqual([(r['asama'], r['kayit']['deger'], r['kayit']['kanit_cumle']) for r in rejected],
                         [('tablo_baglami_belirsiz', '14', row2)])                       # ham kanıtıyla korunur
        self.assertIn('tekil', rejected[0]['neden'])

    def test_literature_table_records_resolved_with_study_identity_not_deleted(self):
        # Satır adları yazar / yıl, ilk sütun 'Study': başka çalışmaların verisi; yazar adı ölçüm adı olmaz.
        # Deterministik çalışma özeti modeli (src.rag_tablo_ozel): çalışma kimliği notta, ölçüm = sütun başlığı.
        recs = [bare('Al Hasan, 2019', MH_ROW, '314'), bare('Batt, 2017', MH_ROW2, '43')]
        (valid, rejected, _), _ = table_run({1: MH_PAGE}, {1: []}, {1: recs})
        self.assertEqual(sorted((r['tablo_modeli']['kaynak_calisma']['ad'], r['sonuc_olcutu_tr'], r['deger'])
                                for r in valid),
                         [('Al Hasan', 'No. in sample', '314'), ('Batt', 'Mean age, years', '43'),
                          ('Batt', 'No. in sample', '14'), ('Batt', 'ROSC, %', '14')])
        self.assertTrue(all(r['baglam'] == 'literature_background' and 'hasta_grubu' not in r for r in valid))
        self.assertFalse(any(r['sonuc_olcutu_tr'] in ('Al Hasan, 2019', 'Batt, 2017') for r in valid))
        # Özgün LLM kayıtları silinmez: önceki aşamasıyla (literatür incelemesi) ret raporunda kalır.
        replaced = [r for r in rejected if r['asama'] == 'tablo_ozel_degisimi']
        self.assertEqual(sorted(r['kayit']['deger'] for r in replaced), ['314', '43'])
        self.assertTrue(all(r['onceki_asama'] == 'tablo_hizalama_inceleme' and 'literatür' in r['neden']
                            for r in replaced))
        self.assertTrue(all(r['kayit']['kanit_cumle'] in (MH_ROW, MH_ROW2) for r in replaced))
        # Niteleyicisi ('(EMS)') başka sütunun altında kalan hücreler tahminle eşleştirilmez: gerekçeli inceleme.
        held = sorted(r['neden'] for r in rejected if r['asama'] == 'tablo_ozel_inceleme')
        self.assertEqual(held, ["hücre biçimi tekil ayrıştırılamadı (sütun 'Mean age, years')",
                                "hücre biçimi tekil ayrıştırılamadı (sütun 'ROSC, %')"])

    def test_ambiguous_cases_not_guessed(self):
        recs = [bare('Risc of ACS', RD_ROW, '7.8 (2.4-25.1)'),                           # çok satırlı hücrenin satırı
                bare('Al Hasan, 2019', MH_ROW, 'Al Hasan, 2019', p='314')]                # satır etiketi sütunu
        pages = {1: MH_PAGE + '\n' + RD_PAGE}
        (valid, rejected, _), _ = table_run(pages, {1: []}, {1: recs})
        # Kabul edilenler yalnız literatür tablosunun deterministik kayıtları; iki belirsiz LLM kaydı tahminle
        # eşleştirilmez ve değiştirilmez.
        self.assertTrue(valid and all(r['tablo_modeli']['durum'] == 'ozel' for r in valid))
        llm = [r for r in rejected if r.get('kayit') in recs]
        self.assertEqual(sorted(r['asama'] for r in llm), ['tablo_baglami_belirsiz'] * 2)
        mismatch = MH_PAGE.replace('|**ROSC, %**|', '|**ROSC, %**|**Extra**|')                 # hücre sayısı uyuşmaz
        (valid, rejected, _), _ = table_run({1: mismatch}, {1: []}, {1: [bare('Al Hasan, 2019', MH_ROW, '314')]})
        self.assertEqual((valid, rejected[0]['asama']), ([], 'tablo_baglami_belirsiz'))
        self.assertTrue(all('hücre sayısı başlıkla uyuşmuyor' in r['neden'] for r in rejected
                            if r['asama'] == 'tablo_ozel_inceleme'))                    # model de değer dağıtmaz

    def test_group_header_fills_patient_group(self):
        row = '|Age|62|'
        page = '##### T\n|Variable|Event (n=372)|\n|---|---|\n' + row + '\n'
        (valid, _, _), _ = table_run({1: page}, {1: []}, {1: [bare('Age', row, '62')]})
        self.assertEqual((valid[0].get('hasta_grubu'), valid[0].get('sonuc_olcutu_tr')), ('Event (n=372)', None))


ST_ROW = '|ELR|7.912<br>2,735.621<br>0.002|'
ST_PAGE = '##### Table 3: Regression.\n|Variable|**B**<br>**Exp (B) (OR)**<br>**p value**|\n|---|---|\n' + ST_ROW + '\n'


class StackedCellTests(unittest.TestCase):
    """TK37 Tablo 3: 'B / Exp(B)(OR) / p value' hücresi tek değer olarak geliyordu."""

    def test_stacked_cell_split_with_p_attached(self):
        rec = bare('ELR', ST_ROW, '7.912<br>2,735.621<br>0.002', bilgi_turu='klinik_sonuc')
        (valid, rejected, _), _ = table_run({1: ST_PAGE}, {1: []}, {1: [rec]})
        self.assertEqual(sorted((r['sonuc_olcutu_tr'], r['deger'], r.get('p')) for r in valid),
                         [('B', '7.912', '0.002'), ('Exp (B) (OR)', '2,735.621', '0.002')], rejected)
        self.assertTrue(all(r['kanit_cumle'] == ST_ROW for r in valid))

    def test_ci_line_attached_and_misaligned_or_conflicting_not_split(self):
        from src.rag_klinik_cikarim import split_stacked_cell
        row = '|ELR|2.73<br>1.2-6.1<br>0.002|'
        page = '##### T\n|Variable|**OR**<br>**95% CI**<br>**p**|\n|---|---|\n' + row + '\n'
        parts = split_stacked_cell(bare('ELR', row, '2.73<br>1.2-6.1<br>0.002'), page)
        self.assertEqual([(p['sonuc_olcutu_tr'], p['deger'], p['ci95'], p['p']) for p in parts],
                         [('OR', '2.73', '1.2-6.1', '0.002')])
        two = '##### T\n|Variable|**OR**<br>**p**|\n|---|---|\n' + row + '\n'
        self.assertIsNone(split_stacked_cell(bare('ELR', row, '2.73<br>1.2-6.1<br>0.002'), two))     # hizasız
        self.assertIsNone(split_stacked_cell(bare('ELR', row, '2.73<br>1.2-6.1<br>0.002', p='0.5'), page))  # çelişki


class LabelCleaningTests(unittest.TestCase):
    def test_markup_removed_meaning_kept(self):
        from src.rag_klinik_cikarim import clean_label
        self.assertEqual(clean_label('**AF**<br>**burden**<sup>**_d_**</sup>'), 'AF burden')
        self.assertEqual(clean_label('**Total (n=114)**<br>**Median (Q1-Q3)**'), 'Total (n=114) Median (Q1-Q3)')
        self.assertEqual(clean_label('BMI kg/m<sup>2</sup>'), 'BMI kg/m<sup>2</sup>')          # sayısal üst simge
        self.assertEqual(clean_label('Lifetime risk, % (95% CI)'), 'Lifetime risk, % (95% CI)')
        self.assertEqual(clean_label('NT_proBNP_level'), 'NT_proBNP_level')                   # kelime içi '_'

    def test_labels_cleaned_in_records_and_still_source_checked(self):
        new = [cell('Overweight or<br>obesity', T2_ROW, 1, deger='Increased', sonuc_olcutu_tr='**AF**<br>**recurrence**')]
        (valid, rejected, _), _ = table_run({1: T2_PAGE}, {1: []}, {1: new})
        self.assertEqual(valid[0]['sonuc_olcutu_tr'], 'AF recurrence', rejected)
        self.assertEqual(valid[0]['kanit_cumle'], T2_ROW)
        self.assertIn('etiket_bicim:sonuc_olcutu_tr', valid[0]['otomatik_duzeltme'])


AB_TEXT = ('**Figure 1.** Appropriate test selection based on patient risk. Abbreviations: CCTA, coronary computed '
           'tomography angiography; CACS, coronary artery calcium score; ICA, invasive coronary angiography.')
FIG_TEXT = 'AF and HF are predisposing conditions for each other and often coexist (Figure 1).'


class AbbreviationListTests(unittest.TestCase):
    """CAD s.5: şekil altı kısaltma listesinden 9 'yöntem/tanı' kaydı çıkarılıyordu."""

    def test_glossary_only_facts_rejected_caption_and_figure_sentences_kept(self):
        pages = {1: AB_TEXT + '\n' + FIG_TEXT}
        base = dict(baglam='literature_background', sayfa=1)
        recs = [dict(base, bilgi_turu='teshis_yontemi', klinik_bilgi_en='coronary artery calcium score', kanit_cumle=AB_TEXT),
                dict(base, bilgi_turu='teshis_yontemi', klinik_bilgi_en='invasive coronary angiography', kanit_cumle=AB_TEXT),
                dict(base, bilgi_turu='diger', klinik_bilgi_en='Appropriate test selection based on patient risk',
                     kanit_cumle=AB_TEXT),
                dict(base, bilgi_turu='klinik_sonuc', klinik_bilgi_en='often coexist', kanit_cumle=FIG_TEXT)]
        valid, rejected, _ = extract(pages, build_tasks(pages, {1: []}), FakeClient({1: recs}), 'm')
        self.assertEqual(sorted(r['klinik_bilgi_en'] for r in valid),
                         ['Appropriate test selection based on patient risk', 'often coexist'])
        self.assertEqual([r['asama'] for r in rejected], ['kisaltma_listesi'] * 2)

    def test_prose_after_marker_is_not_a_glossary(self):
        from src.rag_klinik_cikarim import glossary_span
        self.assertIsNone(glossary_span('Abbreviations: see the methods section for definitions of all terms used.'))
        self.assertIsNotNone(glossary_span('Abbreviation: DAPT, dual antiplatelet therapy.'))


FD_ROW = '|delta 1h-0h %||12.3 (1.7-91.4)|9.9 (1.4-43.0)|<0.001|'
FD_PAGE = ('|||**Total (n=114)**<br>**Median (Q1-Q3)**|**ACS Absent (n=94)**<br>**Median (Q1-Q3)**|**P**|\n'
           '|---|---|---|---|---|\n|Age||64 (53-74)|63 (52-72)|0.100|\n'
           '|||Total (n=114)<br>n (%)|ACS Absent (n=94)<br>n (%)|P|\n|Non risc||66 (57.9)|62 (66.0)|<0.001|\n'
           '|||Median (Q1.-Q3.)|Median (Q1.-Q3.)||\n' + FD_ROW + '\n')
SG_ROW = '|WBC|7.9±2.2|8.0±2.4|0.589|'
SG_PAGE = ('**TABLE 1:** Parameters\n|**Variable**|**Study group**<br>**(X±SD) (minimum-maximum)**|'
           '**Control group**<br>**(X±SD) (minimum-maximum)**|**p value**|\n|---|---|---|---|\n' + SG_ROW + '\n')


class FormatDescriptorTests(unittest.TestCase):
    """JCPSP: 'Median (Q1.-Q3.)' 25 kayıtta grup sanıldı; TK37: grup adlarına '(X±SD) (minimum-maximum)' eklendi."""

    def test_format_label_detection(self):
        from src.rag_klinik_cikarim import is_format_label, split_format_descriptor
        for label in ('Median (Q1.-Q3.)', 'n (%)', '(X±SD) (minimum-maximum)', 'Mean ± SD', 'IQR', '**Median (Q1-Q3)**'):
            self.assertTrue(is_format_label(label), label)
        for label in ('Total (n=114)', 'Study group', 'ACS Present (n=20)', 'Patients with SD', 'Control'):
            self.assertFalse(is_format_label(label), label)
        self.assertEqual(split_format_descriptor('ACS Absent (n=94) n (%)'), ('ACS Absent (n=94)', 'n (%)'))
        self.assertEqual(split_format_descriptor('Patients with SD'), ('Patients with SD', ''))     # çıplak kelime kalır

    def test_descriptor_only_group_resolved_from_column_position(self):
        recs = [grp('delta 1h-0h %', FD_ROW, 'Median (Q1.-Q3.)', '12.3 (1.7-91.4)'),
                grp('delta 1h-0h %', FD_ROW, 'Median (Q1.-Q3.)', '9.9 (1.4-43.0)')]
        (valid, rejected, _), _ = table_run({1: FD_PAGE}, {1: []}, {1: recs})
        self.assertEqual(sorted((r['hasta_grubu'], r['deger']) for r in valid),
                         [('ACS Absent (n=94)', '9.9 (1.4-43.0)'), ('Total (n=114)', '12.3 (1.7-91.4)')], rejected)
        self.assertTrue(all(any(n.startswith('hasta_grubu:deger_bicimi=') and 'Median' in n for n in r['otomatik_duzeltme'])
                            for r in valid))                                           # biçim bilgisi notta korunur
        self.assertEqual(len(grouped_rows(valid)), 1)                                  # gerçek grup karşılaştırması

    def test_appended_descriptor_separated_and_group_without_n_kept(self):
        recs = [grp('WBC', SG_ROW, '**Study group**<br>**(X±SD) (minimum-maximum)**', '7.9±2.2', p='0.589'),
                grp('WBC', SG_ROW, '**Control group**<br>**(X±SD) (minimum-maximum)**', '8.0±2.4', p='0.589')]
        (valid, rejected, _), _ = table_run({1: SG_PAGE}, {1: []}, {1: recs})
        self.assertEqual(sorted(r['hasta_grubu'] for r in valid), ['Control group', 'Study group'], rejected)
        self.assertEqual(valid[0]['kanit_cumle'], SG_ROW)

    def test_unresolvable_descriptor_group_held_for_review(self):
        row = '|Score|5|5|'
        page = '##### T\n|Variable|**Median**|**Median**|\n|---|---|---|\n' + row + '\n'
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: [grp('Score', row, 'Median', '5')]})
        self.assertEqual(valid, [])
        self.assertEqual(rejected[0]['asama'], 'inceleme')
        self.assertEqual(rejected[0]['kayit']['hasta_grubu'], 'Median')                # ham kayıt korunur


NN_ROW = '||Delta TropI (0-1)h|75.4%|AMI Absent|94|0|'
NN_ROW2 = '|1|Delta TropI (0-3)h|72.1%|AMI Present|20|0|'
NN_PAGE = ('##### Table III: Models\n||**Variable Importa**|**nce**|**Sample**|**Predicted A**|**MI**|\n'
           '|---|---|---|---|---|---|\n|**Model**|**Independent Variable**|**Normalized**<br>**Importance**|'
           '**Training**|**Absent**|**Present**|\n' + NN_ROW + '\n' + NN_ROW2 + '\n')
CQ_ROW = '|Al Hasan, 2019|Kuwait|314|70 (GP); 67|'
CQ_PAGE = ('**Table 1** Studies\n||**Country**|**No. in sample**|**Males, %**|\n|---|---|---|---|\n' + CQ_ROW + '\n'
           '|||||\n'.replace('|||||', '||||(EMS)|') + '|Batt, 2017|UAE|14|43|\n')


class ReviewQueueTests(unittest.TestCase):
    """İlişkisi tablo yapısıyla kurulamayan değerler incelemeye ayrılır (silinmez)."""

    def test_numeric_row_label_and_fragmented_header_group_held(self):
        recs = [cell('1', NN_ROW2, 1, bilgi_turu='olcum', deger='72.1%', sonuc_olcutu_tr='Normalized Importance'),
                grp('AMI Absent', NN_ROW, 'Absent', '94'),
                cell('Delta TropI (0-1)h', NN_ROW, 1, bilgi_turu='olcum', deger='75.4%',
                     sonuc_olcutu_tr='Normalized Importance')]
        (valid, rejected, _), _ = table_run({1: NN_PAGE}, {1: []}, {1: recs})
        # Sayısal satır adı (model numarası) ve kalın ikinci başlık satırı: yapısal olarak karmaşık tablo. Sütunu
        # belli görünen hücre de (yanlış değişkene bağlanma riski) otomatik kabul edilmez; üç kayıt da silinmeden
        # gerekçesiyle incelemeye ayrılır.
        self.assertEqual(valid, [])
        held = [r for r in rejected if r['asama'] == 'tablo_hizalama_inceleme']
        self.assertEqual(sorted(r['kayit']['klinik_bilgi_en'] for r in held), ['1', 'AMI Absent', 'Delta TropI (0-1)h'])
        self.assertTrue(all('karmaşık tablo' in r['neden'] and 'sayısal satır adı' in r['neden'] for r in held))

    def test_in_cell_sub_result_and_missing_context_held(self):
        recs = [grp('LVEF 35 ± 9%', T3_ROW, 'Ablation', '35 ± 9%')]
        (valid, rejected, _), _ = table_run({1: T3_PAGE}, {1: []}, {1: recs})
        self.assertEqual((valid, rejected[0]['asama']), ([], 'inceleme'))
        self.assertIn('alt sonuç', rejected[0]['neden'])

    def test_valid_groups_and_comparisons_kept(self):
        # '(n=)' taşımayan grup tek katmanlı başlıkta: kalır; ikili karşılaştırma (istatistik etiketi) kalır
        row = '|Male<br>Female|152<br>37|79<br>17|0.584|'
        page = '##### T\n|Gender|Total|Study group|p value|\n|---|---|---|---|\n' + row + '\n'
        recs = [grp('Male', row, 'Study group', '79', p='0.584'), grp('Male', row, 'Total', '152', p='0.584')]
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: recs})
        self.assertEqual(len(valid), 2, rejected)
        recs = [grp('Pairwise comparation for AUC', PAIR_ROW, 'P (A-B)', '0.291')]
        (valid, rejected, _), _ = table_run({1: PAIR_PAGE}, {1: []}, {1: recs})
        self.assertEqual(len(valid), 1, rejected)

    def test_continuation_row_qualifier_held_with_raw_row(self):
        row, page = neutral(CQ_ROW), neutral(CQ_PAGE)
        recs = [bare('Cohort A', row, '70 (GP); 67'), bare('Cohort A', row, '314')]
        (valid, rejected, _), _ = table_run({1: page}, {1: []}, {1: recs})
        self.assertEqual([(r['deger'], r['sonuc_olcutu_tr']) for r in valid], [('314', 'No. in sample')])
        held = [r for r in rejected if r['asama'] == 'inceleme']
        self.assertEqual(len(held), 1)
        self.assertEqual((held[0]['kayit']['deger'], held[0]['kayit']['kanit_cumle'], held[0]['devam_satiri']),
                         ('70 (GP); 67', row, '||||(EMS)|'))                             # birleşik metin üretilmez
        self.assertIn('(EMS)', held[0]['neden'])
        # Yazar / yıl satırlı (literatür) hali: çalışma özeti modeli devam satırındaki niteleyiciyi AYNI sütunda
        # doğrular ve iki değeri etiketleriyle ayırır; özgün LLM kayıtları silinmez (ret raporunda, önceki aşamasıyla).
        recs = [bare('Al Hasan, 2019', CQ_ROW, '70 (GP); 67'), bare('Al Hasan, 2019', CQ_ROW, '314')]
        (valid, rejected, _), _ = table_run({1: CQ_PAGE}, {1: []}, {1: recs})
        al = sorted((r['sonuc_olcutu_tr'], r['deger'], (r['tablo_modeli'].get('alt_grup') or {}).get('etiket'))
                    for r in valid if r['tablo_modeli']['kaynak_calisma']['ad'] == 'Al Hasan')
        self.assertEqual(al, [('Males, %', '67', 'EMS'), ('Males, %', '70', 'GP'), ('No. in sample', '314', None)])
        split = [r for r in valid if r['deger'] == '67'][0]
        self.assertEqual(split['kanit_cumle'], CQ_ROW + '\n||||(EMS)|')                # iki fiziksel satır da kanıtta
        self.assertTrue(all('hasta_grubu' not in r for r in valid))                     # alt grup hasta grubu sayılmaz
        replaced = [r for r in rejected if r['asama'] == 'tablo_ozel_degisimi']
        self.assertEqual(sorted(r['kayit']['deger'] for r in replaced), ['314', '70 (GP); 67'])
        self.assertTrue(all(r['onceki_asama'] == 'tablo_hizalama_inceleme' for r in replaced))


class NoDatabaseTests(unittest.TestCase):
    def test_modules_do_not_import_database(self):
        code = ('import sys, src.rag_klinik_cikarim, src.rag_klinik_karsilastir; '
                'sys.exit(1 if "src.database" in sys.modules or "mysql.connector" in sys.modules else 0)')
        self.assertEqual(subprocess.run([sys.executable, '-c', code]).returncode, 0)


if __name__ == '__main__':
    unittest.main()
