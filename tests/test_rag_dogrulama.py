"""src.rag_dogrulama (bağımsız deterministik doğrulama raporu) ve değerlendirme betiğinin kayıt sınıflandırması için
genel regresyon testleri. Sentetik metin / tablolar; LLM / API / embedding yok; veritabanı bağlantısı açılmaz."""
import copy
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from src import rag_aktarim as A
from src import rag_dogrulama as V

ROOT = Path(__file__).resolve().parents[1]
META = {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf'}
DOC_ID = 'd' * 64
TEXT = ('The rate was 12% overall. Drug Z was associated with mortality (OR 1.25, 95% CI 0.89-1.37, p=0.002). '
        'Age was 62±10.3 years in the event group.')
TABLE = ('**Table 1:** Baseline characteristics\n|Variable|Event (n=50)|No event (n=80)|p|\n|---|---|---|---|\n'
         '|Age|62±10.4|58.2±11.2|0.002|\n|Weight|80±12|75±10|0.04|\n')
PAGES = {1: TEXT, 2: TABLE}


def text(fact, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'olcum'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=kw.pop('kanit_cumle', TEXT), sayfa=1, kanit_turu='metin', kategoriler=['x'], **kw)


def report(records, pages=PAGES):
    draft = A.build_draft(records, pages, META, [])
    return V.verify(records, draft, pages, DOC_ID), draft


def checks(entry, code):
    return [c for c in entry['kontroller'] if c['kod'] == code]


class ReportContractTests(unittest.TestCase):
    def test_report_does_not_change_records_or_review_status(self):
        recs = [text('The rate was 12% overall', deger='12%', unit='%')]
        snapshot = copy.deepcopy(recs)
        rep, draft = report(recs)
        self.assertEqual(recs, snapshot)
        entry = rep['kayitlar'][0]
        self.assertEqual(entry['review_status'], 'pending')
        self.assertFalse(entry['klinik_veya_semantik_dogrulama'])
        self.assertEqual(entry['dogrulama_turu'], 'otomatik_deterministik')
        self.assertEqual(entry['kayit_kimligi'], A.satir_hash(DOC_ID, draft['adaylar'][0]))
        self.assertEqual(entry['belge_kimligi'], DOC_ID)
        for c in entry['kontroller']:
            self.assertIn(c['sonuc'], V.SONUCLAR)
            self.assertEqual(c['surum'], V.SURUM)
            self.assertTrue(c['gerekce'])
        results = {c['kod']: c['sonuc'] for c in entry['kontroller']}
        self.assertEqual(results['kaynak_kanit'], 'gecti')
        self.assertEqual(results['dq_kaynak_chunk'], 'calistirilmadi')       # veritabanı bağlamı gerekir
        self.assertEqual(results['dq_satir_tutarliligi'], 'uygulanamaz')
        self.assertEqual(results['tablo_hucre'], 'uygulanamaz')               # metin kaydı

    def test_source_is_rechecked_from_pages_not_trusted(self):
        recs = [text('The rate was 12% overall', deger='12%', unit='%')]
        draft = A.build_draft(recs, PAGES, META, [])
        tampered = {1: TEXT.replace('12%', '13%'), 2: TABLE}                  # kaynak metin farklı
        rep = V.verify(recs, draft, tampered, DOC_ID)
        self.assertEqual(checks(rep['kayitlar'][0], 'kaynak_kanit')[0]['sonuc'], 'basarisiz')
        self.assertEqual(rep['kayitlar'][0]['genel_sonuc'], 'basarisiz')

    def test_ci_p_inconsistency_is_uncertain_not_failure_and_values_unchanged(self):
        recs = [text('Drug Z was associated with mortality', deger='1.25', ci95='0.89-1.37', p='0.002', etki_turu='OR')]
        rep, draft = report(recs)
        ci = checks(rep['kayitlar'][0], 'dq_ci_p')
        self.assertEqual([(c['sonuc'], c['seviye']) for c in ci], [('belirsiz', 'uyari')])
        self.assertIn('KAYNAK TUTARSIZLIĞI', ci[0]['gerekce'])
        self.assertEqual((draft['adaylar'][0]['deger'], draft['adaylar'][0]['ci95']), ('1.25', '0.89-1.37'))

    def test_markdown_in_p_does_not_hide_ci_p_check_and_hash_unchanged(self):
        quote = 'Drug Z was associated with mortality (OR 1.25, 95% CI 0.89-1.37, p=**<0.001**).'
        recs = [text('Drug Z was associated with mortality', kanit_cumle=quote, deger='1.25', ci95='0.89-1.37',
                     p='**<0.001**', etki_turu='OR')]
        pages = {1: quote, 2: TABLE}
        rep, draft = report(recs, pages)
        entry = rep['kayitlar'][0]
        self.assertEqual([c['sonuc'] for c in checks(entry, 'dq_ci_p')], ['belirsiz'])
        self.assertEqual(entry['kayit_kimligi'], A.satir_hash(DOC_ID, draft['adaylar'][0]))   # özgün p ile
        self.assertEqual(draft['adaylar'][0]['p'], '**<0.001**')

    def test_multi_component_fields_not_reported_as_contradiction(self):
        quote = 'Event rates were 2.0% vs 3.0% with drug Z, a 32% lower risk.'
        recs = [text('Event rates were 2.0% vs 3.0% with drug Z', kanit_cumle=quote, deger='32%; 2.0% vs 3.0%')]
        rep, _ = report(recs, {1: quote, 2: TABLE})
        rel = checks(rep['kayitlar'][0], 'klinik_iliski')[0]
        self.assertEqual(rel['sonuc'], 'belirsiz')
        self.assertIn('çok bileşenli', rel['gerekce'])
        self.assertTrue(rep['kayitlar'][0]['semantik_dogrulama_gerekli'])

    def test_verification_module_not_imported_by_extraction_pipeline(self):
        code = ('import sys, src.rag_klinik_cikarim, src.rag_anlam_denetimi, src.rag_tablo_ozel; '
                'sys.exit(1 if "src.rag_dogrulama" in sys.modules or "src.data_quality" in sys.modules '
                'or "src.database" in sys.modules else 0)')
        self.assertEqual(subprocess.run([sys.executable, '-c', code], cwd=str(ROOT)).returncode, 0)


class TableCellAuditTests(unittest.TestCase):
    def test_aligned_record_audited_against_source_table(self):
        from src import rag_tablo_hizalama as H
        import src.pilot_tablo as PT
        row = '|Age|62±10.4|58.2±11.2|0.002|'
        item = dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en='Age', kanit_cumle=row, sayfa=2,
                    kanit_turu='tablo', deger='62±10.4', hasta_grubu='Event (n=50)')
        tables = PT.parse_tables(PAGES)
        out = H.align_tables([item], PAGES, [])
        aligned = out[0]
        self.assertEqual(aligned['tablo_modeli']['durum'], 'eslesti')
        self.assertEqual(H.audit_record(aligned, tables), [])
        wrong = copy.deepcopy(aligned)
        wrong['deger'] = '58.2±11.2'                                           # başka sütunun değeri
        wrong['tablo_modeli'].pop('ham_deger', None)
        self.assertEqual(H.audit_record(wrong, tables), ['değer kayıtlı sütunun hücresinde yok'])

    def test_table_records_relationship_not_judged_by_text_rule(self):
        row = '|Age|62±10.4|58.2±11.2|0.002|'
        base = dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en='Age', kanit_cumle=row, sayfa=2,
                    kanit_turu='tablo', kategoriler=['x'], p='0.002')
        items = [dict(base, deger='62±10.4', hasta_grubu='Event (n=50)'),         # grup uyumu: iki grup birlikte
                 dict(base, deger='58.2±11.2', hasta_grubu='No event (n=80)')]
        rep, _ = report(items)
        self.assertEqual(len(rep['kayitlar']), 2)
        for entry in rep['kayitlar']:
            self.assertEqual(checks(entry, 'klinik_iliski')[0]['sonuc'], 'uygulanamaz')
            self.assertEqual(checks(entry, 'kaynak_kanit')[0]['sonuc'], 'gecti')


class TextTableTests(unittest.TestCase):
    def run_check(self, value, evidence, group=None):
        import src.pilot_tablo as PT
        item = dict(kanit_turu='metin', deger=value, kanit_cumle=evidence, sayfa=1, hasta_grubu=group)
        return V.check_text_table(item, PT.parse_tables(PAGES))

    def test_same_rounding_and_inconsistent_values(self):
        same = self.run_check('58.2±11.2', 'Age was 58.2±11.2 years in the no event group.')
        self.assertEqual(same['sonuc'], 'gecti')
        rounding = self.run_check('62±10', 'Age was 62±10 years in the event group.')
        self.assertEqual((rounding['sonuc'], rounding['gerekce']), ('gecti', 'yalnız yuvarlama farkı'))
        bad = self.run_check('62±10.3', 'Age was 62±10.3 years in the event group.')
        self.assertEqual((bad['sonuc'], bad['seviye']), ('belirsiz', 'uyari'))
        self.assertEqual(bad['kanit']['tablo']['hucre'], '62±10.4')          # iki kaynak da raporda
        self.assertIn('hiçbiri doğru kabul edilmedi', bad['gerekce'])

    def test_ambiguous_or_different_measure_not_forced(self):
        self.assertEqual(self.run_check('62±10.3', 'Age was 62±10.3 years.')['sonuc'], 'uygulanamaz')      # grup yok
        self.assertEqual(self.run_check('62±10.3', 'Height was 62±10.3 cm in the event group.')['sonuc'],
                         'uygulanamaz')                                        # tabloda olmayan ölçüm
        self.assertEqual(self.run_check('1,525', 'Age data from 1,525 patients.')['sonuc'], 'uygulanamaz')  # düz sayı

    def test_pair_values_compared_without_group_mapping_claim(self):
        ok = self.run_check('62±10.4 vs. 58.2±11.2', 'Age was 62±10.4 vs. 58.2±11.2 years.')
        self.assertEqual(ok['sonuc'], 'gecti')
        self.assertTrue(ok['kanit']['grup_eslemesi_dogrulanmadi'])
        bad = self.run_check('62±10.4 vs. 55.0±11.2', 'Age was 62±10.4 vs. 55.0±11.2 years.')
        self.assertEqual(bad['sonuc'], 'belirsiz')


def load_eval():
    spec = importlib.util.spec_from_file_location('rag_eval', ROOT / 'scripts' / 'rag_onbellek_degerlendirme.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordClassificationTests(unittest.TestCase):
    def setUp(self):
        self.E = load_eval()
        row = '|TEST A|90%|80%|x|y|'
        self.adv = dict(sayfa=1, kanit_cumle=row, klinik_bilgi_en='TEST A', bilgi_turu='diger', baglam='literature_background',
                        kanit_turu='tablo', deger='_•_<br>Fast<br>_•_<br>Cheap', sonuc_olcutu_tr='Advantages')
        self.lim = dict(self.adv, deger='_•_<br>Radiation<br>_•_<br>Cost', sonuc_olcutu_tr='Limitations')

    def test_same_identity_records_not_cross_matched(self):
        # Avantajlar kaydı ayrıldı (toplu madde), Sınırlılıklar kaydı kabulde kaldı: çapraz eşleşme olmamalı.
        rejected = [{'asama': 'toplu_madde_tekrari', 'neden': 'toplu', 'kayit': dict(self.adv)}]
        classes = self.E.kayit_siniflari([self.adv, self.lim], [dict(self.lim)], rejected)
        self.assertEqual(classes[0][0], 'mukerrer_ayrildi')
        self.assertEqual(classes[1], ('degismedi', None))

    def test_ambiguous_identity_not_force_assigned(self):
        changed_a = dict(self.adv, deger='other A')
        changed_b = dict(self.lim, deger='other B')
        classes = self.E.kayit_siniflari([self.adv, self.lim], [changed_a, changed_b], [])
        self.assertEqual([c for c, _ in classes], ['belirsiz_eslesme', 'belirsiz_eslesme'])
        single = self.E.kayit_siniflari([self.adv], [changed_a], [])
        self.assertEqual(single[0], ('duzeltildi', ['deger']))


if __name__ == '__main__':
    unittest.main()
