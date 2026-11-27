"""scripts/rag_inceleme.py birim testleri. Veritabanı, API, LLM ya da gerçek PDF kullanılmaz: sahte PDF baytları,
bellek içi sayfa metinleri (tests.test_rag_aktarim) ve geçici dizin. Çalıştırma:
    python -m unittest tests.test_rag_inceleme
"""
import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import rag_aktarim as A
from tests import test_rag_aktarim as T
from tests.test_rag_aktarim import G_AGE, PAGES, R_STROKE, boundary, cell, text

_SPEC = importlib.util.spec_from_file_location(
    'rag_inceleme', Path(__file__).resolve().parents[1] / 'scripts' / 'rag_inceleme.py')
R = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(R)

PDF_BYTES = b'%PDF-1.4 sahte inceleme test belgesi\n'
DOC_ID = hashlib.sha256(PDF_BYTES).hexdigest()


def reader(path):
    return {'document_id': R.sha256_file(path)}, PAGES


def source_records():
    return [text('Atrial fibrillation (AF)'),
            text('Chest pain', bilgi_turu='semptom', otomatik_duzeltme=['kanit_cumle']),
            cell('Age, years', G_AGE, 2, '62', hasta_grubu='Event (n=10)', p='0.031'),
            cell('Age, years', G_AGE, 2, '58', hasta_grubu='No event (n=20)', p='0.031'),
            cell('Age, years', G_AGE, 2, '60', hasta_grubu='Total (n=30)', p='0.031'),
            cell('Stroke', R_STROKE, 3, '2.42 (2.17–2.71)', sonuc_olcutu_tr='Relative risk (95% CI)'),
            cell('Stroke', R_STROKE, 3, '7.1', sonuc_olcutu_tr='5-y risk, %'),
            boundary()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pdf = self.dir / 'x.pdf'
        self.pdf.write_bytes(PDF_BYTES)
        built = A.build_draft(source_records(), PAGES, T.META, [])
        self.assertEqual(len(built['adaylar']), 8, built['inceleme'])
        review = built['inceleme'] + [
            {'sira': 90, 'neden': 'olası tekrar (benzer mevcut kayıt)', 'asama': 'olasi_tekrar', 'mevcut_idler': [1],
             'kayit': A.serializable(built['adaylar'][0])},
            {'sira': 91, 'neden': 'eşleme', 'asama': 'esleme', 'kayit': {'bilgi_turu': 'tani'}}]
        self.taslak = {'pdf': 'x.pdf', 'document_id': DOC_ID, 'calistirma': T.META['cikti_dizini'],
                       'sayilar': built['sayilar'], 'adaylar': [A.serializable(r) for r in built['adaylar']],
                       'inceleme': review, 'kesin_tekrar': [{'sira': 99, 'mevcut_id': 1, 'kayit': {}}]}
        self.draft_path = self.dir / 'x_rag_taslak_20260101-000000.json'
        with open(self.draft_path, 'x', encoding='utf-8') as handle:
            json.dump(self.taslak, handle, ensure_ascii=False, indent=2, default=str)
        self.draft_sha = R.sha256_file(self.draft_path)
        self.pdf_sha = R.sha256_file(self.pdf)
        self.karar_path = self.dir / 'inceleme' / 'x.kararlar.json'
        self.patch = mock.patch.object(R, 'pdf_pages', reader)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def args(self, **kw):
        base = dict(taslak=str(self.draft_path), pdf=str(self.pdf), kararlar=str(self.karar_path))
        base.update(kw)
        return argparse.Namespace(**base)

    def makale(self):
        return R.makale_yukle(self.draft_path, self.pdf)

    def hazir(self):
        R.hazirla(self.args())
        m = self.makale()
        doc, _ = R.karar_dosyasi_oku(self.karar_path, m)
        return m, doc

    def unit(self, m, kind, grup=None):
        return next(u for u in m['birimler'] if u['tur'] == kind and (grup is None or u['grup'] == grup))

    def hashes(self, m, unit):
        return [m['hashes'][i] for i in unit['kayitlar']]

    def originals_unchanged(self):
        self.assertEqual(R.sha256_file(self.draft_path), self.draft_sha)
        self.assertEqual(R.sha256_file(self.pdf), self.pdf_sha)


class UnitAndViewTests(Base):
    def test_units_group_rows_and_text_records(self):
        m = self.makale()
        kinds = sorted((u['tur'], u['grup'], len(u['kayitlar'])) for u in m['birimler'])
        self.assertEqual(kinds, [('metin', False, 1)] * 3 + [('tablo', False, 2), ('tablo', True, 3)])
        self.assertEqual(m['kapsam_disi'], {'olasi_tekrar': 1, 'diger_inceleme': 1, 'kesin_tekrar': 1})
        self.assertEqual([u['sayfa'] for u in m['birimler']], sorted(u['sayfa'] for u in m['birimler']))

    def test_table_view_full_block_header_row_and_cells(self):
        m = self.makale()
        unit = self.unit(m, 'tablo', grup=True)
        view = unit['tablo']
        self.assertEqual(view['sayfadaki_baslik'], [1, 'Baseline characteristics'])
        self.assertEqual([r['kanit'] for r in view['satirlar']].count(True), 1)
        self.assertEqual(len(view['satirlar']), 5)                     # başlık, ayırıcı, bölüm, iki veri satırı
        self.assertEqual(view['satirlar'][view['ana_baslik']]['hucreler'][0], 'Variable')
        cells = [g['hucre']['hucre'] for g in unit['gorunum']]
        self.assertEqual(sorted(cells), [1, 2, 3])
        self.assertEqual({g['hucre']['baslik'] for g in unit['gorunum']},
                         {'Event (n=10)', 'No event (n=20)', 'Total (n=30)'})
        note = unit['gorunum'][0]['not']
        self.assertEqual((note['tablo_no'], note['alt_bolum']), (1, 'Laboratory findings'))
        for label in ('Klinik bulgu (EN)', 'Bilgi türü', 'Bağlam', 'Sayısal değer', 'Birim', 'Hasta grubu'):
            self.assertIn(label, [a for a, _ in unit['gorunum'][0]['temel']])

    def test_multi_part_evidence_all_parts_located_and_auto_flags_visible(self):
        m = self.makale()
        multi = next(g for u in m['birimler'] for g in u['gorunum'] if len(g['kanit']) > 1)
        self.assertEqual([p['sayfa'] for p in multi['kanit']], [9, 10])
        self.assertTrue(all(p['araliklar'] for p in multi['kanit']))
        flagged = [g for u in m['birimler'] for g in u['gorunum'] if g['otomatik']]
        self.assertEqual(len(flagged), 1)
        self.assertIn('kanit_cumle', flagged[0]['otomatik'])

    def test_continuation_table_shows_previous_page_header(self):
        view = R.tablo_gorunumu({'sayfa': 4, 'kanit_cumle': T.R_HF}, PAGES)
        self.assertIsNone(view['sayfadaki_baslik'])
        self.assertEqual(view['onceki_sayfa']['sayfa'], 3)
        self.assertEqual(view['onceki_sayfa']['baslik'], [2, 'Risks'])


class PrepareAndFingerprintTests(Base):
    def test_prepare_creates_empty_decisions_never_overwrites_and_keeps_originals(self):
        m, doc = self.hazir()
        self.assertEqual(doc['kararlar'], {})
        self.assertEqual(R.ilerleme(doc, m)['kararsiz'], 8)
        before = self.karar_path.read_bytes()
        R.hazirla(self.args())
        self.assertEqual(self.karar_path.read_bytes(), before)
        self.originals_unchanged()

    def test_changed_draft_or_pdf_stops(self):
        self.hazir()
        self.draft_path.write_text(self.draft_path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        with self.assertRaises(R.IncelemeHatasi):
            R.karar_dosyasi_oku(self.karar_path, self.makale())
        self.pdf.write_bytes(PDF_BYTES + b'x')
        with self.assertRaises(R.IncelemeHatasi):
            self.makale()

    def test_fingerprint_check_detects_change_during_session(self):
        m = self.makale()
        R.parmak_izi_kontrol(m)
        self.pdf.write_bytes(PDF_BYTES + b'x')
        with self.assertRaises(R.IncelemeHatasi):
            R.parmak_izi_kontrol(m)

    def test_original_draft_must_be_all_pending(self):
        data = copy.deepcopy(self.taslak)
        data['adaylar'][0]['review_status'] = 'verified'
        with self.assertRaises(R.IncelemeHatasi):
            R.makale_olustur(data, 's', DOC_ID, PAGES)

    def test_pdf_name_cannot_escape_pdf_directory(self):
        for bad in ('../x.pdf', 'a/b.pdf', 'x.txt', None):
            with self.assertRaises(R.IncelemeHatasi):
                R.varsayilan_pdf({'pdf': bad})


class DecisionValidationTests(Base):
    def entry(self, karar='onay', gerekce=''):
        return {'karar': karar, 'gerekce': gerekce, 'inceleyen': 'test', 'zaman': '2026-01-01T10:00:00+03:00'}

    def test_invalid_unknown_and_malformed_decisions_rejected(self):
        m, doc = self.hazir()
        h = m['hashes'][0]
        cases = {'bilinmeyen': {'f' * 64: self.entry()}, 'deger': {h: self.entry('verified')},
                 'gerekcesiz_ret': {h: self.entry('ret', '  ')}, 'fazla_alan': {h: dict(self.entry(), x=1)},
                 'eksik_alan': {h: {'karar': 'onay'}}, 'saat_dilimsiz': {h: dict(self.entry(), zaman='2026-01-01T10:00')},
                 'inceleyensiz': {h: dict(self.entry(), inceleyen='')}}
        for name, decisions in cases.items():
            self.assertTrue(R.kararlari_dogrula(dict(doc, kararlar=decisions), m), name)
        self.assertTrue(R.kararlari_dogrula(dict(doc, fazla=1), m))
        self.assertEqual(R.kararlari_dogrula(dict(doc, kararlar={h: self.entry()}), m), [])

    def test_duplicate_key_in_decision_file_rejected(self):
        m, _ = self.hazir()
        h = m['hashes'][0]
        raw = self.karar_path.read_text(encoding='utf-8').replace(
            '"kararlar": {}', '"kararlar": {"%s": %s, "%s": %s}' % (h, json.dumps(self.entry()), h,
                                                                     json.dumps(self.entry('ret', 'x'))))
        self.karar_path.write_text(raw, encoding='utf-8')
        with self.assertRaises(R.IncelemeHatasi) as ctx:
            R.karar_dosyasi_oku(self.karar_path, m)
        self.assertIn('mükerrer', str(ctx.exception))

    def test_group_row_cannot_be_split(self):
        m, doc = self.hazir()
        unit = self.unit(m, 'tablo', grup=True)
        hs = self.hashes(m, unit)
        with self.assertRaises(R.IncelemeHatasi):
            R.karar_uygula(doc, m, unit['id'], {hs[0]: {'karar': 'onay'}}, 'test')
        new = R.karar_uygula(doc, m, unit['id'], {h: {'karar': 'onay'} for h in hs}, 'test')
        with self.assertRaises(R.IncelemeHatasi):
            R.karar_uygula(new, m, unit['id'], {hs[1]: {'karar': 'ret', 'gerekce': 'yanlış'}}, 'test')
        split = dict(doc, kararlar={hs[0]: self.entry(), hs[1]: self.entry('ret', 'x')})
        self.assertTrue(any('gruplu satır' in e for e in R.kararlari_dogrula(split, m)))

    def test_decision_only_for_records_of_the_unit(self):
        m, doc = self.hazir()
        text_unit = self.unit(m, 'metin')
        other = next(h for h in m['hashes'] if h not in self.hashes(m, text_unit))
        with self.assertRaises(R.IncelemeHatasi):
            R.karar_uygula(doc, m, text_unit['id'], {other: {'karar': 'onay'}}, 'test')
        with self.assertRaises(R.IncelemeHatasi):
            R.karar_uygula(doc, m, 'yok', {other: {'karar': 'onay'}}, 'test')


class AtomicWriteTests(Base):
    def test_atomic_write_leaves_no_partial_file(self):
        target = self.dir / 'a.json'
        R.atomic_write_json(target, {'a': 1})
        with mock.patch.object(R.os, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                R.atomic_write_json(target, {'a': 2})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'a': 1})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith('a.json')), ['a.json'])


class ExportTests(Base):
    def decide(self, m, doc):
        text_units = [u for u in m['birimler'] if u['tur'] == 'metin']
        grp = self.unit(m, 'tablo', grup=True)
        stroke = self.unit(m, 'tablo', grup=False)
        doc = R.karar_uygula(doc, m, text_units[0]['id'], {self.hashes(m, text_units[0])[0]: {'karar': 'onay'}}, 'test')
        doc = R.karar_uygula(doc, m, text_units[1]['id'],
                             {self.hashes(m, text_units[1])[0]: {'karar': 'ret', 'gerekce': 'kaynakla uyuşmuyor'}}, 'test')
        doc = R.karar_uygula(doc, m, grp['id'], {h: {'karar': 'onay'} for h in self.hashes(m, grp)}, 'test')
        doc = R.karar_uygula(doc, m, stroke['id'], {self.hashes(m, stroke)[0]: {'karar': 'sonra'}}, 'test')
        R.atomic_write_json(self.karar_path, doc)
        return doc

    def test_no_decision_no_copy(self):
        m, doc = self.hazir()
        with self.assertRaises(R.IncelemeHatasi):
            R.inceleme_kopyasi(m, doc)

    def test_only_explicit_approvals_verified_everything_else_identical(self):
        m, doc = self.hazir()
        doc = self.decide(m, doc)
        out = R.inceleme_kopyasi(m, doc)
        approved = {h for h, e in doc['kararlar'].items() if e['karar'] == 'onay'}
        self.assertEqual(len(approved), 4)
        for i, (a, b) in enumerate(zip(self.taslak['adaylar'], out['adaylar'])):
            expected = 'verified' if m['hashes'][i] in approved else 'pending'
            self.assertEqual(b['review_status'], expected)
            self.assertEqual({k: v for k, v in b.items() if k != 'review_status'},
                             {k: v for k, v in a.items() if k != 'review_status'})
            self.assertEqual(A.satir_hash(DOC_ID, b), m['hashes'][i])
        for key in ('inceleme', 'kesin_tekrar', 'sayilar', 'pdf', 'document_id', 'calistirma'):
            self.assertEqual(out[key], self.taslak[key])
        self.assertEqual((out['rag_inceleme']['verified'], out['rag_inceleme']['ret']), (4, 1))

    def test_export_command_writes_new_file_and_adapter_accepts_it(self):
        m, doc = self.hazir()
        self.decide(m, doc)
        target = R.disari_aktar(self.args(cikti_dizini=str(self.dir / 'cikti')))
        self.originals_unchanged()
        data = json.loads(Path(target).read_text(encoding='utf-8'))
        self.assertEqual(sum(r['review_status'] == 'verified' for r in data['adaylar']), 4)
        with mock.patch.object(T, 'DOC_ID', DOC_ID):
            conn = T.FakeConn()
            result = A.aktar(A.KorumaliBaglanti(conn, yazma=True), {'document_id': DOC_ID, 'filename': 'x.pdf'},
                             PAGES, data['adaylar'])
        self.assertEqual((result['yazilan'], result['atlanan_onaysiz'], result['kalici']), (4, 4, False))
        self.assertEqual(conn.commits, 0)

    def test_tampered_decision_file_blocks_export(self):
        m, doc = self.hazir()
        doc = self.decide(m, doc)
        bad = copy.deepcopy(doc)
        bad['kararlar']['0' * 64] = bad['kararlar'][next(iter(bad['kararlar']))]
        R.atomic_write_json(self.karar_path, bad)
        with self.assertRaises(R.IncelemeHatasi):
            R.disari_aktar(self.args(cikti_dizini=str(self.dir / 'cikti')))
        self.assertFalse((self.dir / 'cikti').exists())


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.hazir()
        self.server = R.IncelemeSunucusu(0, self.makale(), self.karar_path, 'test')
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': host or f'127.0.0.1:{self.port}'}
        h.update(headers or {})
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def post(self, body, **headers):
        base = {'Content-Type': 'application/json', 'X-Inceleme-Anahtari': self.server.anahtar}
        base.update(headers)
        return self.call('POST', '/api/karar', body, {k: v for k, v in base.items() if v is not None})

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_page_has_csp_and_no_data_and_no_html_injection_sinks(self):
        resp, body = self.call('GET', '/')
        self.assertEqual(resp.status, 200)
        csp = resp.getheader('Content-Security-Policy')
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        html = body.decode('utf-8')
        self.assertNotIn('Atrial fibrillation', html)
        for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval('):
            self.assertNotIn(sink, R.SAYFA)

    def test_data_needs_token_and_host(self):
        self.assertEqual(self.call('GET', '/api/veri')[0].status, 403)
        resp, body = self.call('GET', '/api/veri', headers={'X-Inceleme-Anahtari': self.server.anahtar})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(json.loads(body)['birimler']), 5)
        self.assertEqual(self.call('GET', '/', host='evil.example')[0].status, 421)

    def test_only_this_articles_pdf_is_served(self):
        resp, body = self.call('GET', '/pdf?s=2')
        self.assertEqual((resp.status, body), (200, PDF_BYTES))
        for path in ('/x.pdf', '/pdf/../x.json', '/data/pdfs/x.pdf', '/' + self.draft_path.name, '/../x.pdf'):
            self.assertEqual(self.call('GET', path)[0].status, 404, path)

    def test_post_requires_token_json_and_same_origin(self):
        m = self.server.makale
        unit = self.unit(m, 'metin')
        body = {'birim': unit['id'], 'kararlar': {self.hashes(m, unit)[0]: {'karar': 'onay', 'gerekce': ''}}}
        self.assertEqual(self.post(body, **{'X-Inceleme-Anahtari': None})[0].status, 403)
        self.assertEqual(self.post(body, Origin='http://evil.example')[0].status, 403)
        self.assertEqual(self.post(body, **{'Content-Type': 'text/plain'})[0].status, 415)
        self.assertEqual(json.loads(self.karar_path.read_text(encoding='utf-8'))['kararlar'], {})
        resp, payload = self.post(body, Origin=f'http://127.0.0.1:{self.port}')
        self.assertEqual(resp.status, 200, payload)
        saved = json.loads(self.karar_path.read_text(encoding='utf-8'))['kararlar']
        self.assertEqual(list(saved.values())[0]['karar'], 'onay')
        self.assertEqual(list(saved.values())[0]['inceleyen'], 'test')
        self.originals_unchanged()

    def test_post_group_split_rejected_and_resume_reads_saved(self):
        m = self.server.makale
        unit = self.unit(m, 'tablo', grup=True)
        hs = self.hashes(m, unit)
        resp, _ = self.post({'birim': unit['id'], 'kararlar': {hs[0]: {'karar': 'onay', 'gerekce': ''}}})
        self.assertEqual(resp.status, 400)
        resp, _ = self.post({'birim': unit['id'], 'kararlar': {h: {'karar': 'ret', 'gerekce': 'eksik'} for h in hs}})
        self.assertEqual(resp.status, 200)
        again = R.IncelemeSunucusu(0, self.makale(), self.karar_path, 'test')        # sürdürme
        try:
            self.assertEqual({again.doc['kararlar'][h]['karar'] for h in hs}, {'ret'})
        finally:
            again.server_close()

    def test_external_change_of_decision_file_stops_server(self):
        m = self.server.makale
        unit = self.unit(m, 'metin')
        body = {'birim': unit['id'], 'kararlar': {self.hashes(m, unit)[0]: {'karar': 'sonra', 'gerekce': ''}}}
        doc = json.loads(self.karar_path.read_text(encoding='utf-8'))
        doc['olusturma'] = '2000-01-01T00:00:00+00:00'
        R.atomic_write_json(self.karar_path, doc)
        self.assertEqual(self.post(body)[0].status, 409)
        self.assertEqual(self.call('GET', '/api/veri', headers={'X-Inceleme-Anahtari': self.server.anahtar})[0].status,
                         409)


if __name__ == '__main__':
    unittest.main()
