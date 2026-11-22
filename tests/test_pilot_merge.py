"""Çok sayfalı kanıt, tablo grup tekrarları, SQL alan koruma ve checkpoint/timeout testleri.

Gerçek API çağrısı veya veritabanı yazımı yapılmaz (mock istemci / mock bağlantı)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from src import pilot_auto_extract as auto
from src.pilot_clinical import STRUCTURED_FIELDS, import_records, validate_records

LEFT = 'Methods were standard. Patients with atrial fibril'
RIGHT = 'lation were enrolled. Follow-up lasted one year.'
PAGES = {1: LEFT, 2: RIGHT}
CROSS = 'Patients with atrial fibrillation were enrolled.'
DOC = {'document_id': 'a' * 64, 'filename': 'pilot.pdf'}


def row(**kw):
    base = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='atrial fibrillation',
                kanit_cumle=CROSS, sayfa=1, sayfa_bitis=2, kanit_turu='metin')
    base.update(kw)
    return base


class MultiPageEvidence(unittest.TestCase):
    def test_quote_crossing_boundary_accepted(self):
        good, errors = validate_records([row()], PAGES)
        self.assertEqual((len(good), errors), (1, []))

    def test_without_sayfa_bitis_cross_quote_rejected(self):
        good, errors = validate_records([row(sayfa_bitis=None)], PAGES)
        self.assertEqual(len(good), 0)
        self.assertIn('birebir', errors[0]['hata'])

    def test_quote_wholly_on_one_page_must_not_claim_two_pages(self):
        good, errors = validate_records([row(kanit_cumle='Methods were standard.',
                                             klinik_bilgi_en='Methods')], PAGES)
        self.assertEqual(len(good), 0)

    def test_non_adjacent_or_invalid_end_page_rejected(self):
        pages = {1: LEFT, 2: RIGHT, 3: 'x'}
        for end in (3, 0, '2', True):
            good, errors = validate_records([row(sayfa_bitis=end)], pages)
            self.assertEqual(len(good), 0, end)
            self.assertIn('bitiş sayfası', errors[0]['hata'])

    def test_quote_must_be_exact(self):
        # Boşluk eklenmiş / tirelenme onarılmış metin kabul edilmez.
        good, _ = validate_records([row(kanit_cumle='Patients with atrial  fibrillation were enrolled.')], PAGES)
        self.assertEqual(len(good), 0)

    def test_explicit_single_page_end_same_as_missing(self):
        pages = {1: 'heart failure'}
        base = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='heart failure',
                    kanit_cumle='heart failure', sayfa=1)
        good, errors = validate_records([base, dict(base, sayfa_bitis=1)], pages)
        self.assertEqual(len(good), 1)
        self.assertTrue(errors[0]['hata'].startswith('tekrar'))


TABLE_QUOTE = 'HbA1c 7.83±1.2 6.23±1.2 <0.001'


def table_row(**kw):
    base = dict(bilgi_turu='olcum', baglam='study_finding', klinik_bilgi_en='HbA1c', kanit_cumle=TABLE_QUOTE,
                sayfa=1, kanit_turu='tablo', sonuc_olcutu_tr='HbA1c', p='<0.001', etki_turu='diger',
                taslak_notu=json.dumps({'tablo_no': 1, 'satir_no': 4, 'analiz': None}))
    base.update(kw)
    return base


class TableGroups(unittest.TestCase):
    pages = {1: 'Table 1\n' + TABLE_QUOTE}

    def test_same_evidence_different_groups_kept(self):
        rows = [table_row(hasta_grubu='Ischemic event', comparator='No ischemic event', deger='7.83±1.2'),
                table_row(hasta_grubu='No ischemic event', comparator='Ischemic event', deger='6.23±1.2')]
        good, errors = validate_records(rows, self.pages)
        self.assertEqual((len(good), errors), (2, []))

    def test_same_evidence_different_analysis_kept(self):
        note = lambda a: json.dumps({'tablo_no': 2, 'satir_no': 1, 'analiz': a})
        rows = [table_row(etki_turu='OR', deger='3.124', taslak_notu=note('univariate')),
                table_row(etki_turu='OR', deger='3.124', taslak_notu=note('multivariate'))]
        good, errors = validate_records(rows, self.pages)
        self.assertEqual((len(good), errors), (2, []))

    def test_identical_table_row_is_duplicate(self):
        r = table_row(hasta_grubu='Ischemic event', deger='7.83±1.2')
        good, errors = validate_records([r, dict(r)], self.pages)
        self.assertEqual(len(good), 1)
        self.assertTrue(errors[0]['hata'].startswith('tekrar'))


class Cursor:
    def __init__(self, columns):
        self.columns, self.calls, self.rowcount = columns, [], 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return ('pilot.pdf',)

    def fetchall(self):
        return [(c,) for c in self.columns]

    def close(self):
        pass


class Connection:
    def __init__(self, columns):
        self.cur = Cursor(columns)

    def cursor(self):
        return self.cur

    def commit(self):
        raise AssertionError('connection verildiğinde commit edilmemeli')

    def rollback(self):
        pass

    def close(self):
        pass


BASE_COLUMNS = ('bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'document_id', 'kanit_sayfa',
                'kanit_cumle', 'kanit_turu', 'satir_hash', 'review_status', 'cikarma_yontemi')
ALL_COLUMNS = BASE_COLUMNS + STRUCTURED_FIELDS + ('kanit_sayfa_bitis',)


class SqlImport(unittest.TestCase):
    def full_row(self, group, value, comparator):
        values = {k: f'{k}-x' for k in STRUCTURED_FIELDS}
        values.update(hasta_grubu=group, deger=value, comparator=comparator,
                      taslak_notu={'tablo_no': 1, 'satir_no': 4, 'grup_n': 372})
        return dict(table_row(), review_status='verified', **values)

    def inserted(self, conn):
        out = []
        for sql, params in conn.cur.calls:
            if sql.startswith('INSERT'):
                cols = [c.strip('`') for c in sql[sql.index('(') + 1:sql.index(')')].split(', ')]
                out.append(dict(zip(cols, params)))
        return out

    def test_all_structured_fields_written_and_groups_distinct(self):
        conn = Connection(ALL_COLUMNS)
        rows = [self.full_row('Ischemic event', '7.83±1.2', 'No ischemic event'),
                self.full_row('No ischemic event', '6.23±1.2', 'Ischemic event')]
        self.assertEqual(import_records(DOC, rows, connection=conn), 2)
        written = self.inserted(conn)
        self.assertEqual(len(written), 2)
        for src, dst in zip(rows, written):
            for key in STRUCTURED_FIELDS:
                expected = src[key] if isinstance(src[key], str) else json.dumps(src[key], ensure_ascii=False)
                self.assertEqual(dst[key], expected, key)
            self.assertEqual((dst['review_status'], dst['kanit_sayfa']), ('verified', 1))
        self.assertEqual(written[0]['kanit_cumle'], written[1]['kanit_cumle'])
        self.assertNotEqual(written[0]['satir_hash'], written[1]['satir_hash'])

    def test_single_page_explicit_end_allowed(self):
        conn = Connection(ALL_COLUMNS)
        record = dict(bilgi_turu='semptom', baglam='study_finding', review_status='verified',
                      klinik_bilgi_en='dyspnea', sayfa=2, sayfa_bitis=2, kanit_cumle='dyspnea')
        self.assertEqual(import_records(DOC, [record], connection=conn), 1)
        self.assertNotIn('kanit_sayfa_bitis', self.inserted(conn)[0])

    def test_multi_page_blocked_before_db(self):
        fake = ModuleType('src.database')
        fake.get_connection = lambda: self.fail('Veritabanına bağlanılmamalı')
        with patch.dict('sys.modules', {'src.database': fake}):
            with self.assertRaisesRegex(ValueError, 'Çok sayfalı kanıt'):
                import_records(DOC, [row(review_status='verified')])

    def test_verified_still_required(self):
        with self.assertRaisesRegex(ValueError, 'manuel onaylı'):
            import_records(DOC, [row(sayfa_bitis=1, review_status='pending')], connection=Connection(ALL_COLUMNS))

    def test_missing_structured_column_raises(self):
        conn = Connection(BASE_COLUMNS)
        with self.assertRaisesRegex(ValueError, 'sütunları eksik'):
            import_records(DOC, [self.full_row('A', '1', 'B')], connection=conn)
        self.assertFalse(self.inserted(conn))


def response(findings):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({'findings': findings})))])


class ScriptedClient:
    """Her çağrıda sıradaki davranışı uygular: Exception örneği yükseltir, aksi halde payload'a göre yanıt."""

    def __init__(self, script=None):
        self.script, self.payloads = list(script or []), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
        self.payloads.append(payload)
        if 'end_page' in payload:
            return response([row()])
        page = payload['sayfa']
        if page == 1:
            return response([dict(bilgi_turu='diger', baglam='study_method', klinik_bilgi_en='Methods',
                                  kanit_cumle='Methods were standard.', sayfa=1, kanit_turu='metin')])
        return response([dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en='Follow-up',
                              kanit_cumle='Follow-up lasted one year.', sayfa=2, kanit_turu='metin')])


class AutoExtract(unittest.TestCase):
    def run_extract(self, client, **kw):
        kw.setdefault('sleep', lambda s: None)
        kw.setdefault('retry_exceptions', (TimeoutError,))
        return auto.extract_pages(PAGES, client, 'mock-model', **kw)

    def test_cross_page_finding_extracted(self):
        client = ScriptedClient()
        valid, rejected, calls = self.run_extract(client)
        self.assertEqual(calls, 3)  # 2 sayfa + 1 sınır
        self.assertEqual(rejected, [])
        cross = [r for r in valid if r.get('sayfa_bitis') == 2]
        self.assertEqual([r['kanit_cumle'] for r in cross], [CROSS])

    def test_page_mode_cannot_claim_cross_page_quote(self):
        class Client(ScriptedClient):
            def create(self, **kwargs):
                return response([row(sayfa_bitis=None)])
        valid, rejected, _ = self.run_extract(Client())
        self.assertFalse([r for r in valid if r.get('sayfa_bitis') is None])

    def test_timeout_retried_with_limit(self):
        sleeps = []
        client = ScriptedClient([TimeoutError(), TimeoutError()])
        valid, _, calls = self.run_extract(client, sleep=sleeps.append)
        self.assertEqual((calls, sleeps, len(valid)), (3, [1, 2], 3))

    def test_timeout_exhausted_raises_and_failed_call_not_checkpointed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / 'out.checkpoint.json'
            # 1. görev başarılı, 2. görev 3 kez timeout.
            client = ScriptedClient([None, TimeoutError(), TimeoutError(), TimeoutError()])
            with self.assertRaises(TimeoutError):
                self.run_extract(client, checkpoint_path=cp)
            state = json.loads(cp.read_text(encoding='utf-8'))
            self.assertEqual(len(state['completed']), 1)

            # Kaldığı yerden devam: yalnızca kalan 2 görev çağrılır, mükerrer kayıt yok.
            resumed = ScriptedClient()
            valid, rejected, calls = self.run_extract(resumed, checkpoint_path=cp)
            self.assertEqual(calls, 2)
            self.assertEqual([p.get('sayfa', p.get('start_page')) for p in resumed.payloads], [2, 1])
            fresh, _, _ = self.run_extract(ScriptedClient())
            self.assertEqual(valid, fresh)
            self.assertEqual(rejected, [])

            # Tamamlanmış checkpoint ile tekrar: API çağrısı yok, aynı sonuç.
            again = ScriptedClient()
            valid2, _, calls2 = self.run_extract(again, checkpoint_path=cp)
            self.assertEqual((calls2, again.payloads, valid2), (0, [], fresh))

    def test_invalid_response_not_retried_nor_checkpointed(self):
        class Bad(ScriptedClient):
            def create(self, **kwargs):
                self.payloads.append(1)
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=None)])
        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / 'cp.json'
            client = Bad()
            with self.assertRaises(ValueError):
                self.run_extract(client, checkpoint_path=cp)
            self.assertEqual(len(client.payloads), 1)
            self.assertFalse(cp.exists())

    def test_checkpoint_mismatch_rejected_without_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / 'cp.json'
            self.run_extract(ScriptedClient(), checkpoint_path=cp, source_id='doc-a')
            client = ScriptedClient()
            for kwargs in ({'source_id': 'doc-b'}, {'source_id': 'doc-a', 'model': 'other-model'}):
                model = kwargs.pop('model', 'mock-model')
                with self.assertRaisesRegex(ValueError, 'Checkpoint'):
                    auto.extract_pages(PAGES, client, model, checkpoint_path=cp, **kwargs)
            with self.assertRaisesRegex(ValueError, 'Checkpoint'):
                auto.extract_pages({1: LEFT + 'X', 2: RIGHT}, client, 'mock-model', checkpoint_path=cp,
                                   source_id='doc-a')
            with patch.object(auto, 'BOUNDARY_WINDOW', 10):
                with self.assertRaisesRegex(ValueError, 'Checkpoint'):
                    auto.extract_pages(PAGES, client, 'mock-model', checkpoint_path=cp, source_id='doc-a')
            self.assertEqual(client.payloads, [])

    def test_max_calls_checked_before_any_call(self):
        client = ScriptedClient()
        with self.assertRaisesRegex(RuntimeError, 'çağrı sınırı'):
            self.run_extract(client, max_calls=2)
        self.assertEqual(client.payloads, [])

    def test_write_new_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.json'
            path.write_text('orijinal', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                auto.write_new(path, [])
            self.assertEqual(path.read_text(encoding='utf-8'), 'orijinal')

    def test_main_refuses_existing_output_before_pdf_or_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.json'
            out.write_text('[]', encoding='utf-8')
            with patch.object(auto, 'pdf_pages', side_effect=AssertionError('PDF okunmamalı')), \
                    patch('sys.argv', ['x', '--pdf', 'a.pdf', '--output', str(out)]):
                with self.assertRaises(FileExistsError):
                    auto.main()
            self.assertEqual(out.read_text(encoding='utf-8'), '[]')


if __name__ == '__main__':
    unittest.main()
