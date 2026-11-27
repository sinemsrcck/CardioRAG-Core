"""src/rag_sunum_tablolari.py: aday satırları (bağ denetimi, pending, tekrar durumu), mevcut sunum SQL'inin dönüşümü,
durum birleştirme (kaynak aday hash'leri ve durumları korunur), içerik anahtarı (değişken alanlar hariç), hedef denetimi.
Veritabanı / API yok."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import rag_aktarim as A
from src import rag_sunum_tablolari as R
from src.pilot_clinical import klinik_satiri

DOC = 'd' * 64
META = {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf', 'document_id': DOC}
TEXT = 'Atrial fibrillation (AF) was diagnosed in 40 patients. Chest pain was reported in most cases.'
PAGES = {1: TEXT}


def rec(fact, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'tani'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=TEXT, sayfa=1, kanit_turu='metin', kategoriler=['diseases'], **kw)


def candidates():
    draft = A.build_draft([rec('Atrial fibrillation (AF)'), rec('Chest pain', bilgi_turu='semptom')], PAGES, META, [])
    return [A.serializable(r) for r in draft['adaylar']]


def source(h, det='gecti', sem='kontrol_edilmedi', insan='yok', tekrar='yeni_aday'):
    return {'satir_hash': h, 'document_id': DOC, 'deterministik_dogrulama': det, 'semantik_dogrulama': sem,
            'insan_onayi': insan, 'uretim_tekrar_durumu': tekrar, 'aday_dosyasi_sha256': 's', 'deterministik_parti': 'p'}


class CandidateRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cands = candidates()
        self.path = Path(self.tmp.name) / 'x.taslak.json'
        self.path.write_text(json.dumps({'document_id': DOC, 'adaylar': self.cands}), encoding='utf-8')
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.view = {A.satir_hash(DOC, c): {'satir_hash': A.satir_hash(DOC, c), 'belge': DOC, 'aday_dosyasi': str(self.path),
                                            'aday_dosyasi_sha256': self.sha, 'parti': 'p1', 'deterministik': 'gecti',
                                            'semantik': None, 'insan': None} for c in self.cands}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_are_pending_bound_and_hash_matches_import_formula(self):
        rows = R.aday_satirlari(self.view, lambda b: [], 't', 'y')
        self.assertEqual({r['satir_hash'] for r in rows}, set(self.view))
        for c, r in zip(self.cands, rows):
            self.assertEqual(klinik_satiri({'document_id': DOC}, c)['satir_hash'], A.satir_hash(DOC, c))
            self.assertEqual((r['review_status'], r['cikarma_yontemi'], r['semantik_dogrulama'], r['insan_onayi']),
                             ('pending', 'rag', 'kontrol_edilmedi', 'yok'))
        self.assertNotIn('verified', json.dumps(rows, default=str))

    def test_duplicate_status_from_existing_records(self):
        existing = [dict(id=7, kanit_sayfa=1, kanit_sayfa_bitis=None, taslak_notu=None,
                         **{k: v for k, v in self.cands[0].items() if k not in ('sayfa', 'taslak_notu')})]
        rows = R.aday_satirlari(self.view, lambda b: existing, 't', 'y')
        self.assertEqual([r['uretim_tekrar_durumu'] for r in rows], ['kesin_tekrar', 'yeni_aday'])

    def test_binding_failures_refused(self):
        bad_sha = {h: dict(v, aday_dosyasi_sha256='0' * 64) for h, v in self.view.items()}
        with self.assertRaises(R.SunumTabloHatasi):
            R.aday_satirlari(bad_sha, lambda b: [], 't', 'y')
        missing = dict(list(self.view.items())[:1])
        with self.assertRaises(R.SunumTabloHatasi):
            R.aday_satirlari(missing, lambda b: [], 't', 'y')


class StatusTests(unittest.TestCase):
    def test_multi_candidate_rows_keep_hashes_and_statuses(self):
        s = R.durum_ozeti([source('a'), source('b', det='belirsiz', sem='destekleniyor'), source('c', det='basarisiz')])
        self.assertEqual((s['deterministik_dogrulama'], s['deterministik_dagilim']), ('basarisiz', 'basarisiz:1, belirsiz:1, gecti:1'))
        self.assertEqual(s['semantik_dogrulama'], 'karisik')
        self.assertEqual(json.loads(s['kaynak_aday_hashleri']), ['a', 'b', 'c'])
        self.assertEqual([x['semantik_dogrulama'] for x in json.loads(s['kaynak_adaylar'])],
                         ['kontrol_edilmedi', 'destekleniyor', 'kontrol_edilmedi'])
        self.assertEqual(R.durum_ozeti([source('a')])['semantik_dogrulama'], 'kontrol_edilmedi')
        with self.assertRaises(R.SunumTabloHatasi):
            R.durum_ozeti([source('a', det=None)])

    def test_content_key_ignores_variable_fields(self):
        a = {'finding': 'x', 'sira': 1, 'yukleme_kimligi': 'y1', 'yukleme_zamani': 't1', 'id': 1}
        b = dict(a, sira=9, yukleme_kimligi='y2', yukleme_zamani='t2', id=5)
        self.assertEqual(R.icerik_anahtari(a), R.icerik_anahtari(b))
        self.assertNotEqual(R.icerik_anahtari(a), R.icerik_anahtari(dict(a, finding='z')))

    def test_output_rows_need_known_sources(self):
        rows = R.cikti_satirlari(['finding', '_kaynak_idler'], [('f', '1,2')], {1: source('a'), 2: source('b')}, 't', 'y')
        self.assertEqual((rows[0]['sira'], rows[0]['kaynak_aday_sayisi']), (1, 2))
        self.assertNotIn('_kaynak_idler', rows[0])
        with self.assertRaises(R.SunumTabloHatasi):
            R.cikti_satirlari(['finding', '_kaynak_idler'], [('f', '3')], {1: source('a')}, 't', 'y')


class SqlTransformTests(unittest.TestCase):
    def test_existing_presentation_sql_reused_without_manual_links(self):
        pilot = R.pilot_sql('kardiyo_rag_sunum')
        self.assertIn('`kardiyo_rag_sunum`.`aday_bulgular` AS x', pilot)
        self.assertIn('AS k ON 1 = 0', pilot)                                   # manuel kalite işaretleri bağlanmaz
        taban = R.taban_sql('kardiyo_rag_sunum')
        self.assertIn("WHERE b.review_status = 'pending'", taban)
        self.assertNotIn("'verified'", taban)
        for name in R.TABLOLAR:
            sql = R.tablo_sql(name, 'kardiyo_rag_sunum', 3)
            self.assertIn('_kaynak_idler', sql)
            for bad in ('{{', 'v_sunum_taban', 'v_pilot_bulgular', "'verified'"):
                self.assertNotIn(bad, sql)
        self.assertIn('group_3_value', R.tablo_sql('measurements', 'kardiyo_rag_sunum', 3))
        self.assertEqual(R.temel_sutunlar('clinical_outcomes', 2),
                         ['finding', 'statistical_result', 'clinical_context', 'evidence', 'source', 'page'])

    def test_remaining_five_tables_reuse_manual_sql_with_minimal_technical_fields(self):
        from src.sunum_hoca import EXPECTED_COLUMNS
        self.assertEqual(set(R.TABLOLAR_YALIN), {'diseases', 'diagnostics', 'symptoms', 'treatments', 'general_clinical_findings'})
        for name in R.TABLOLAR_YALIN:
            sql = R.tablo_sql(name, 'kardiyo_rag_sunum', 2)
            self.assertIn('AS _kaynak_idler', sql)
            self.assertIn('`kardiyo_rag_sunum`.`aday_bulgular`', sql)
            for bad in ('{{', 'v_sunum_taban', "'verified'"):
                self.assertNotIn(bad, sql)
            self.assertEqual(R.temel_sutunlar(name, 2), EXPECTED_COLUMNS[name])
        self.assertEqual([c for c, _ in R.YALIN_EK], ['sira', 'kaynak_aday_hashleri', 'icerik_anahtari', 'yukleme_kimligi'])
        rows = R.cikti_satirlari(['disease', 'page', '_kaynak_idler'], [('AF', '1', '1,2')],
                                 {1: source('a'), 2: source('b')}, 't', 'y', tam=False)
        self.assertEqual(set(rows[0]), {'disease', 'page', 'kaynak_aday_hashleri', 'icerik_anahtari', 'sira', 'yukleme_kimligi'})
        self.assertEqual(json.loads(rows[0]['kaynak_aday_hashleri']), ['a', 'b'])

    def test_unknown_table_refused_before_any_query(self):
        conn = mock.Mock()
        with mock.patch.dict(os.environ, {'MYSQL_RAG_SUNUM_DATABASE': 'kardiyo_rag_sunum'}):
            with self.assertRaises(R.SunumTabloHatasi):
                R.yukle(conn, yaz=True, tablolar=['diseases', 'klinik_bulgular'])
        conn.cursor.assert_not_called()

    def test_changed_source_sql_is_detected(self):
        with mock.patch.object(R, '_govde', return_value='SELECT 1 FROM klinik_bulgular AS y'):
            with self.assertRaises(R.SunumTabloHatasi):
                R.pilot_sql('kardiyo_rag_sunum')

    def test_target_never_production(self):
        for name in ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data', 'bad-name'):
            with mock.patch.dict(os.environ, {'MYSQL_RAG_SUNUM_DATABASE': name}):
                with self.assertRaises(R.SunumTabloHatasi):
                    R.hedef_adi()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MYSQL_RAG_SUNUM_DATABASE', None)
            self.assertEqual(R.hedef_adi(), 'kardiyo_rag_sunum')

    def test_load_refuses_wrong_connection_database(self):
        conn = mock.Mock()
        cur = conn.cursor.return_value
        cur.fetchone.return_value = ('kardiyo_tam_metin',)
        with mock.patch.dict(os.environ, {'MYSQL_RAG_SUNUM_DATABASE': 'kardiyo_rag_sunum'}):
            with self.assertRaises(R.SunumTabloHatasi):
                R.yukle(conn, yaz=True)
        conn.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
