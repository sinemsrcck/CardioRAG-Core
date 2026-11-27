"""SQL aktarım hazırlığı regresyon testleri: aktarım dışı listesi, otomatik doğrulama sonuçlarının kalite_isaretleri'ne
yalnız ekleme olarak parti hâlinde yazımı ve geri alınması (sahte veritabanı), üretim izni / yedek / hedef denetimi,
aday dosyası bağı, taslak --dogrulama-raporu, src.aktarim --yeniden RAG koruması, toplu semantik bütçe seçimi,
data_quality eski sürüm temizliğinin 'rag-' satırlarını koruması, sql/rag_dogrulama_core.sql statik denetimi.
Veritabanı / LLM / embedding yok."""
import hashlib
import importlib.util
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import rag_aktarim as A
from src import rag_dogrulama as V
from src import data_quality as DQ

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = 'd' * 64
META = {'cikti_dizini': 'x', 'model': 'm', 'pdf': 'x.pdf', 'document_id': DOC_ID}
TEXT = 'Atrial fibrillation (AF) was diagnosed in 40 patients. Chest pain was reported in most cases.'
PAGES = {1: TEXT}


def text(fact, **kw):
    return dict(bilgi_turu=kw.pop('bilgi_turu', 'tani'), baglam='study_finding', klinik_bilgi_en=fact,
                kanit_cumle=TEXT, sayfa=1, kanit_turu='metin', kategoriler=['diseases'], **kw)


RECORDS = [text('Atrial fibrillation (AF)'), text('Chest pain', bilgi_turu='semptom')]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        self.conn.sql.append((sql, params))

    def executemany(self, sql, rows):
        self.conn.sql.append((sql, list(rows)))

    def fetchone(self):
        return (self.conn.db,)

    def close(self):
        pass


class FakeConn:
    def __init__(self, db='kardiyo_test_rag'):
        self.db, self.sql, self.commits, self.rollbacks = db, [], 0, 0
        self.in_transaction = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def start_transaction(self):
        pass


def load_pilot():
    spec = importlib.util.spec_from_file_location('rag_semantik_pilot', ROOT / 'scripts' / 'rag_semantik_pilot.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ExclusionTests(unittest.TestCase):
    def test_excluded_candidate_routed_to_review_with_reason_others_unchanged(self):
        base = A.build_draft(RECORDS, PAGES, META, [])
        self.assertEqual(len(base['adaylar']), 2)
        key = A.satir_hash(DOC_ID, base['adaylar'][1])
        result = A.build_draft(RECORDS, PAGES, META, [], haric={key: 'tür sorunu'})
        self.assertEqual(len(result['adaylar']), 1)
        self.assertEqual(A.satir_hash(DOC_ID, result['adaylar'][0]), A.satir_hash(DOC_ID, base['adaylar'][0]))
        held = [r for r in result['inceleme'] if r['asama'] == 'aktarim_disi']
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]['satir_hash'], key)
        self.assertIn('tür sorunu', held[0]['neden'])
        self.assertTrue(all(r['review_status'] == 'pending' for r in result['adaylar']))

    def test_transfer_refuses_verified_record_on_exclusion_list(self):
        recs = [A.serializable(r) for r in A.build_draft(RECORDS, PAGES, META, [])['adaylar']]
        recs[0]['review_status'] = 'verified'                                      # insan onayı (test)
        doc = {'document_id': DOC_ID, 'filename': 'x.pdf', 'pages': []}
        with self.assertRaises(A.AdaptorHatasi) as ctx:
            A.aktar(object(), doc, PAGES, recs, haric={A.satir_hash(DOC_ID, recs[0]): 'x'})
        self.assertIn('aktarım dışı', str(ctx.exception))

    def test_load_haric_accepts_both_shapes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'a.json'
            p.write_text(json.dumps({'haric': {'h': 'r'}, 'not': 'x'}), encoding='utf-8')
            self.assertEqual(A.load_haric(p), {'h': 'r'})
            p.write_text(json.dumps({'h': 'r'}), encoding='utf-8')
            self.assertEqual(A.load_haric(p), {'h': 'r'})
        self.assertIsNone(A.load_haric(None))

    def test_artefact_only_decisions_not_excluded(self):
        P = load_pilot()
        rows = {k: {'makale': 'm', 'grup': 'A', 'aday': {'klinik_bilgi_en': k}} for k in ('a', 'b', 'c', 'd')}
        results = [
            {'kimlik': 'a', 'durum': 'tamamlandi', 'karar': 'kismen_destekleniyor',
             'hatali_alanlar': [{'alan': 'tablo_ve_baglam_notlari.model', 'sorun': 'artefakt'}]},
            {'kimlik': 'b', 'durum': 'tamamlandi', 'karar': 'kaynakla_celisiyor',
             'hatali_alanlar': [{'alan': 'tablo_ve_baglam_notlari.model'}, {'alan': 'bilgi_turu', 'sorun': 'tür'}]},
            {'kimlik': 'c', 'durum': 'tamamlandi', 'karar': 'destekleniyor', 'hatali_alanlar': []},
            {'kimlik': 'd', 'durum': 'hata', 'karar': None, 'hatali_alanlar': [{'alan': 'bilgi_turu'}]},
        ]
        haric, artefakt = P.exclusion_list(results, rows)
        self.assertEqual(list(haric), ['b'])
        self.assertIn('bilgi_turu', haric['b'])
        self.assertNotIn('tablo_ve_baglam_notlari.model', haric['b'])
        self.assertEqual([x['kimlik'] for x in artefakt], ['a'])


def det_report():
    draft = A.build_draft(RECORDS, PAGES, META, [])
    return V.verify(RECORDS, draft, PAGES, DOC_ID)


SEM = [{'kimlik': 'h1', 'durum': 'tamamlandi', 'karar': 'kaynakla_celisiyor', 'hatali_alanlar': [], 'model': 'llm',
        'belge': DOC_ID},
       {'kimlik': 'h2', 'durum': 'kontrol_edilmedi', 'karar': None, 'belge': DOC_ID},
       {'kimlik': 'h3', 'durum': 'calistirilamadi', 'karar': None, 'belge': DOC_ID}]
PARTI = '20261127120000-0123456789abcdef'


class FakeDB:
    """kalite_isaretleri / klinik_bulgular için küçük bellek içi veritabanı (transaction: anlık görüntü + geri dönüş)."""

    def __init__(self, db='kardiyo_test_rag', kalite=None):
        self.db, self.sql, self.commits, self.rollbacks = db, [], 0, 0
        self.klinik = {1: ('verified', 'a'), 2: ('verified', 'b')}
        self.kalite = dict(kalite or {})
        self.next_id = max(self.kalite, default=0) + 1
        self.snap, self.mutate_klinik = None, False

    def cursor(self):
        return FakeDBCursor(self)

    def start_transaction(self):
        self.snap = (dict(self.klinik), {k: dict(v) for k, v in self.kalite.items()}, self.next_id)

    def commit(self):
        self.commits += 1
        self.snap = None

    def rollback(self):
        self.rollbacks += 1
        if self.snap:
            self.klinik, self.kalite, self.next_id = self.snap
            self.snap = None


KEYS = ('klinik_bulgu_id', 'satir_hash', 'kontrol_kodu', 'kategori', 'seviye', 'grup_id', 'aciklama', 'ayrinti',
        'kontrol_surumu')


class FakeDBCursor:
    def __init__(self, conn):
        self.c, self.result, self.description, self.rowcount = conn, [], None, 0

    def execute(self, sql, params=()):
        c, params = self.c, list(params or [])
        c.sql.append((sql, params))
        if sql == 'SELECT DATABASE()':
            self.result = [(c.db,)]
        elif sql.startswith(V.KALITE_IZ_SQL):
            rows = sorted(c.kalite.items())
            if 'WHERE grup_id' in sql:
                rows = [(i, r) for i, r in rows if r['grup_id'] == params[0]]
            self.result = [(i, hashlib.sha256(json.dumps([i, r], sort_keys=True).encode()).hexdigest()) for i, r in rows]
        elif sql.startswith('SELECT COUNT(*) FROM kalite_isaretleri WHERE grup_id = %s AND'):
            self.result = [(sum(1 for r in c.kalite.values() if r['grup_id'] == params[0] and (
                r['klinik_bulgu_id'] is not None or not r['kontrol_surumu'].startswith('rag-'))),)]
        elif sql.startswith('SELECT COUNT(*) FROM kalite_isaretleri WHERE grup_id = %s'):
            self.result = [(sum(1 for r in c.kalite.values() if r['grup_id'] == params[0]),)]
        elif sql.startswith('SELECT DISTINCT grup_id FROM kalite_isaretleri WHERE grup_id LIKE'):
            self.result = sorted({(r['grup_id'],) for r in c.kalite.values() if r['grup_id'].endswith(params[0][1:])})
        elif sql.startswith('SELECT DISTINCT grup_id FROM kalite_isaretleri WHERE kontrol_surumu LIKE'):
            shas = set(params[1:])
            hits = set()
            for r in c.kalite.values():
                src = (json.loads(r['ayrinti']) if isinstance(r['ayrinti'], str) else r['ayrinti']).get('kaynak') or {}
                if r['kontrol_surumu'].startswith('rag-') and {src.get('rapor_sha256'), src.get('sonuc_sha256')} & shas:
                    hits.add((r['grup_id'],))
            self.result = sorted(hits)
        elif sql.startswith('SELECT * FROM klinik_bulgular'):
            self.description = [('id',), ('review_status',), ('v',)]
            self.result = [(i, *v) for i, v in sorted(c.klinik.items())]
        elif sql.startswith('DELETE FROM kalite_isaretleri'):
            parti, pattern, *ids = params
            assert pattern == 'rag-%'
            self.rowcount = 0
            for i in ids:
                r = c.kalite.get(i)
                if r and r['grup_id'] == parti and r['klinik_bulgu_id'] is None and r['kontrol_surumu'].startswith('rag-'):
                    del c.kalite[i]
                    self.rowcount += 1
        else:
            raise AssertionError(f'beklenmeyen SQL: {sql}')

    def executemany(self, sql, rows):
        c = self.c
        rows = list(rows)
        c.sql.append((sql, rows))
        assert sql == V.KALITE_INSERT_SQL
        for row in rows:
            c.kalite[c.next_id] = dict(zip(KEYS, row))
            c.next_id += 1
        if c.mutate_klinik:
            c.klinik[1] = ('pending', 'degisti')

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return list(self.result)

    def close(self):
        pass


def groups_for(parti=PARTI, sem=None):
    out = V.kalite_satirlari(det_report(), sem, parti=parti, kaynak={'aday_dosyasi': 'x.taslak.json'})
    return {v: r for v, r in out.items() if r}


DQ_ROW = {'klinik_bulgu_id': 1, 'satir_hash': 'q', 'kontrol_kodu': 'dq', 'kategori': 'sayisal', 'seviye': 'uyari',
          'grup_id': '', 'aciklama': 'x', 'ayrinti': '{}', 'kontrol_surumu': 'r5'}


class QualityRowsTests(unittest.TestCase):
    def test_rows_are_namespaced_traceable_and_never_human_approval(self):
        report = det_report()
        groups = V.kalite_satirlari(report, SEM, parti=PARTI, kaynak={'aday_dosyasi': 'x', 'aday_dosyasi_sha256': 's'},
                                    semantik_kaynak={'sonuc_dosyasi': 'y'})
        self.assertEqual(set(groups), {V.SURUM, 'rag-semantik-v1'})
        det = groups[V.SURUM]
        summaries = [r for r in det if r['kontrol_kodu'] == 'rag_det_ozet']
        self.assertEqual(len(summaries), len(report['kayitlar']))
        self.assertTrue(all(r['klinik_bulgu_id'] is None and r['grup_id'] == PARTI for g in groups.values() for r in g))
        self.assertTrue(all(r['ayrinti']['dogrulama_turu'] == 'otomatik_deterministik' for r in summaries))
        self.assertTrue(all(r['kontrol_kodu'].startswith('rag_det:') for r in det if r not in summaries))
        self.assertTrue(all(r['ayrinti']['belge_kimligi'] == DOC_ID and r['ayrinti']['kaynak']['aday_dosyasi'] == 'x'
                            for r in det))
        sem = {r['satir_hash']: r for r in groups['rag-semantik-v1']}
        self.assertEqual(list(sem), ['h1'])                          # tamamlanmayan sonuçlar yazılmaz (kontrol yapılmadı)
        self.assertEqual(sem['h1']['seviye'], 'hata')
        self.assertEqual((sem['h1']['ayrinti']['belge_kimligi'], sem['h1']['ayrinti']['kaynak']), (DOC_ID, {'sonuc_dosyasi': 'y'}))
        self.assertTrue(sem['h1']['ayrinti']['insan_dogrulamasi_degildir'])
        self.assertNotIn('verified', json.dumps(groups, ensure_ascii=False, default=str))

    def test_insert_only_dry_run_rolls_back_and_write_records_before_commit(self):
        conn = FakeDB(kalite={7: dict(DQ_ROW)})
        groups = groups_for()
        n = sum(len(r) for r in groups.values())
        out = V.kalite_yaz(conn, groups, PARTI)
        self.assertEqual((out['kalici'], conn.commits, len(out['eklenen']), list(conn.kalite)), (False, 0, n, [7]))
        sqls = [s for s, _ in conn.sql]
        self.assertFalse(any(s.startswith(('DELETE', 'UPDATE')) for s in sqls))   # yalnız ekleme
        self.assertFalse(any('kalite_calistirmalari' in s for s in sqls))
        self.assertFalse(any('klinik_bulgular' in s and not s.startswith('SELECT') for s in sqls))
        seen = []
        conn = FakeDB(kalite={7: dict(DQ_ROW)})
        out = V.kalite_yaz(conn, groups, PARTI, yaz=True, kayit_yaz=lambda k: seen.append((conn.commits, dict(k))))
        self.assertEqual((out['kalici'], conn.commits, len(conn.kalite)), (True, 1, n + 1))
        self.assertEqual(seen[0][0], 0)                                          # kayıt commit'ten ÖNCE
        self.assertEqual(set(seen[0][1]['eklenen']), {str(i) for i in conn.kalite if i != 7})
        self.assertEqual(conn.kalite[7], DQ_ROW)                                 # mevcut satır aynı
        with self.assertRaises(RuntimeError):                                     # aynı parti ikinci kez yazılmaz
            V.kalite_yaz(conn, groups, PARTI, yaz=True)
        self.assertEqual(conn.commits, 1)

    def test_production_needs_explicit_permission_and_only_source_database(self):
        groups = groups_for()
        for db in ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data'):
            conn = FakeDB(db)
            with self.assertRaises(RuntimeError):
                V.kalite_yaz(conn, groups, PARTI, yaz=True)
            self.assertFalse(conn.kalite or conn.commits or any(s.startswith('INSERT') for s, _ in conn.sql))
        for db in ('kardiyo_sunum', 'kardiyo_clinical_data', 'kardiyo_test_rag'):
            conn = FakeDB(db)
            with self.assertRaises(RuntimeError):
                V.kalite_yaz(conn, groups, PARTI, yaz=True, uretim=True)
            self.assertFalse(conn.kalite or conn.commits)
        conn = FakeDB('kardiyo_tam_metin')
        self.assertTrue(V.kalite_yaz(conn, groups, PARTI, yaz=True, uretim=True)['kalici'])
        with self.assertRaises(ValueError):
            V.kalite_yaz(FakeDB(), {'r5': groups[V.SURUM]}, PARTI, yaz=True)
        with self.assertRaises(ValueError):
            V.kalite_yaz(FakeDB(), groups, 'parti', yaz=True)
        with self.assertRaises(ValueError):                                       # satırlar başka partiye ait
            V.kalite_yaz(FakeDB(), groups_for('20261127120000-ffffffffffffffff'), PARTI, yaz=True)

    def test_precommit_equality_failure_rolls_back(self):
        conn = FakeDB(kalite={7: dict(DQ_ROW)})
        conn.mutate_klinik = True
        seen = []
        with self.assertRaises(RuntimeError) as ctx:
            V.kalite_yaz(conn, groups_for(), PARTI, yaz=True, kayit_yaz=seen.append)
        self.assertIn('klinik_bulgular', str(ctx.exception))
        self.assertEqual((conn.commits, seen, list(conn.kalite), conn.klinik[1]), (0, [], [7], ('verified', 'a')))

    def test_rollback_removes_only_that_batch(self):
        conn = FakeDB('kardiyo_tam_metin', kalite={7: dict(DQ_ROW)})
        first = V.kalite_yaz(conn, groups_for(), PARTI, yaz=True, uretim=True)
        other = '20261127130000-fedcba9876543210'
        V.kalite_yaz(conn, groups_for(other), other, yaz=True, uretim=True)
        before = dict(conn.kalite)
        with self.assertRaises(RuntimeError):                                     # açık izin olmadan üretimde olmaz
            V.kalite_geri_al(conn, first, yaz=True)
        tampered = dict(first, eklenen={**first['eklenen'], next(iter(first['eklenen'])): '0' * 64})
        with self.assertRaises(RuntimeError):                                     # kayıt birebir değilse silinmez
            V.kalite_geri_al(conn, tampered, yaz=True, uretim=True)
        self.assertEqual(conn.kalite, before)
        dry = V.kalite_geri_al(conn, first, uretim=True)
        self.assertEqual((dry['kalici'], conn.kalite), (False, before))
        res = V.kalite_geri_al(conn, first, yaz=True, uretim=True)
        self.assertEqual(res['silinen'], len(first['eklenen']))
        self.assertEqual(set(conn.kalite), set(before) - {int(i) for i in first['eklenen']})
        self.assertEqual(conn.kalite[7], DQ_ROW)
        self.assertTrue(all(r['grup_id'] == other for i, r in conn.kalite.items() if i != 7))

    def test_data_quality_old_version_cleanup_keeps_rag_rows(self):
        conn = FakeConn()
        DQ.write_flags(conn, [], [], 'r5-test', 'kapsam', own_transaction=False, delete_old_versions=True)
        cleanup = [(s, p) for s, p in conn.sql if '<>' in s]
        self.assertEqual(len(cleanup), 1)
        self.assertIn('NOT LIKE', cleanup[0][0])
        self.assertEqual(cleanup[0][1], ('r5-test', 'rag-%'))


def write_pair(tmp, name='m', drop_last=False, sha=None):
    """Geçici klasörde rapor + aday dosyası (<ad>.dogrulama.json / <ad>.taslak.json)."""
    draft = A.build_draft(RECORDS, PAGES, META, [])
    report = V.verify(RECORDS, draft, PAGES, DOC_ID)
    cands = [A.serializable(r) for r in draft['adaylar']]
    taslak = Path(tmp) / f'{name}.taslak.json'
    taslak.write_text(json.dumps({'pdf': 'x.pdf', 'document_id': DOC_ID,
                                  'adaylar': cands[:-1] if drop_last else cands}), encoding='utf-8')
    if sha:
        report['aday_dosyasi'] = {'yol': taslak.as_posix(), 'sha256': sha}
    path = Path(tmp) / f'{name}.dogrulama.json'
    path.write_text(V.dumps(report), encoding='utf-8')
    return path, report, taslak


class CandidateFileTests(unittest.TestCase):
    def test_binding_and_traceability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, report, taslak = write_pair(tmp)
            info = V.aday_dosyasi(path, report)
            self.assertEqual(info['aday_dosyasi_sha256'], hashlib.sha256(taslak.read_bytes()).hexdigest())
            self.assertEqual((info['dogrulama_surumu'], info['pdf']), (V.SURUM, 'x.pdf'))
            sem = Path(tmp) / 'sonuclar.json'
            sem.write_text(json.dumps({'sonuclar': SEM}), encoding='utf-8')
            groups, parti, src = V.kalite_gruplari([tmp], sem, simdi=datetime(2026, 11, 27, 12, 0, 0))
            self.assertRegex(parti, r'^20261127120000-[0-9a-f]{16}$')
            self.assertEqual(src['semantik_yazilmayan_tamamlanmamis'], 2)
            self.assertEqual(src['semantik_guncel_aday_disi'], 1)                 # h1 güncel aday değil
            det = groups[V.SURUM]
            self.assertTrue(all(r['ayrinti']['kaynak']['aday_dosyasi_sha256'] == info['aday_dosyasi_sha256'] for r in det))
            self.assertEqual(parti, V.kalite_gruplari([tmp], sem, simdi=datetime(2026, 11, 27, 12, 0, 0))[1])

    def test_mismatch_or_changed_candidate_file_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, report, _ = write_pair(tmp, drop_last=True)
            with self.assertRaises(ValueError):                                   # kimlikler aday dosyasıyla eşleşmiyor
                V.aday_dosyasi(path, report)
            path, report, _ = write_pair(tmp, name='n', sha='0' * 64)
            with self.assertRaises(ValueError):                                   # aday dosyası rapordan sonra değişti
                V.aday_dosyasi(path, report)
            with self.assertRaises(ValueError):
                V.aday_dosyasi(path, dict(report, surum='rag-dogrulama-v0'))


class BackupTests(unittest.TestCase):
    def make(self, tmp, readonly=True, bad_sha=False):
        yedek = Path(tmp) / 'yedek'
        geri = Path(tmp) / 'geri_yukleme'
        yedek.mkdir()
        geri.mkdir()
        f = yedek / 'kardiyo_tam_metin.sql'
        f.write_text('-- dump\n', encoding='utf-8')
        sha = hashlib.sha256(f.read_bytes()).hexdigest().upper()
        (yedek / 'manifest.json').write_text(json.dumps({'kardiyo_tam_metin': {
            'dosya': f.name, 'boyut': f.stat().st_size, 'sha256': '0' * 64 if bad_sha else sha}}), encoding='utf-8')
        (geri / 'temizleme_raporu.json').write_text(json.dumps({'kardiyo_tam_metin': {'kaynak_sha256': sha.lower()}}),
                                                    encoding='utf-8')
        if readonly:
            os.chmod(f, stat.S_IREAD)
        return f

    def tearDown(self):
        for f in getattr(self, 'files', []):
            if f.exists():
                os.chmod(f, stat.S_IWRITE | stat.S_IREAD)

    def test_verified_current_backup_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.files = [self.make(tmp)]
            self.assertEqual(V.yedek_dogrula(self.files[0])['veritabani'], 'kardiyo_tam_metin')
            os.chmod(self.files[0], stat.S_IWRITE | stat.S_IREAD)
        for kw in ({'readonly': False}, {'bad_sha': True}):
            with tempfile.TemporaryDirectory() as tmp:
                self.files = [self.make(tmp, **kw)]
                with self.assertRaises(RuntimeError):
                    V.yedek_dogrula(self.files[0])
                os.chmod(self.files[0], stat.S_IWRITE | stat.S_IREAD)
        with tempfile.TemporaryDirectory() as tmp:                               # manifest yok
            f = Path(tmp) / 'kardiyo_tam_metin.sql'
            f.write_text('x', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                V.yedek_dogrula(f)
            with self.assertRaises(Exception):                                    # yedek yok
                V.yedek_dogrula(Path(tmp) / 'yok.sql')


class DraftReportTests(unittest.TestCase):
    def test_taslak_writes_bound_verification_report(self):
        doc = {'document_id': DOC_ID, 'filename': 'x.pdf'}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'x_rag_taslak_1.json'
            with mock.patch.object(A, 'load_run', return_value=(META, RECORDS)), \
                    mock.patch.object(A, 'pdf_pages', return_value=(doc, PAGES)):
                A.main(['taslak', '--calistirma', 'x', '--pdf', 'x.pdf', '--sql-okuma-yok', '--cikti', str(out),
                        '--dogrulama-raporu'])
            rpath = Path(tmp) / 'x_rag_taslak_1.dogrulama.json'
            report = json.loads(rpath.read_text(encoding='utf-8'))
            self.assertEqual(report['aday_dosyasi']['sha256'], hashlib.sha256(out.read_bytes()).hexdigest())
            info = V.aday_dosyasi(rpath, report)
            self.assertEqual(info['aday_dosyasi'], out.as_posix())
            self.assertTrue(all(r['review_status'] == 'pending' for r in json.loads(out.read_text(encoding='utf-8'))['adaylar']))
            with mock.patch.object(A, 'load_run', return_value=(META, RECORDS)), \
                    mock.patch.object(A, 'pdf_pages', return_value=(doc, PAGES)), self.assertRaises(FileExistsError):
                A.main(['taslak', '--calistirma', 'x', '--pdf', 'x.pdf', '--sql-okuma-yok', '--cikti', str(out),
                        '--dogrulama-raporu'])


class ReimportGuardTests(unittest.TestCase):
    def test_yeniden_stops_when_document_has_rag_rows(self):
        from src import aktarim as AK

        class Cur:
            def __init__(self, log):
                self.log, self.r = log, []

            def execute(self, sql, params=()):
                self.log.append(sql)
                if "cikarma_yontemi = 'rag'" in sql:
                    self.r = [(2,)]
                elif sql.startswith('SELECT filename FROM pdf_belgeleri'):
                    self.r = [('x.pdf',)]
                elif sql.startswith('SELECT id, satir_hash FROM klinik_bulgular'):
                    self.r = [(1, 'h')]
                else:
                    self.r = [(0, 0)]

            def fetchone(self):
                return self.r[0]

            def fetchall(self):
                return list(self.r)

            def close(self):
                pass

        log = []
        conn = mock.Mock(autocommit=False, in_transaction=True)
        conn.cursor.side_effect = lambda: Cur(log)
        makale = {'doc': {'document_id': DOC_ID, 'filename': 'x.pdf'}, 'files': [], 'beklenen': 1, 'kontrol': {},
                  'pages': {}}
        with mock.patch.object(AK, 'makaleyi_kontrol_et'), self.assertRaises(AK.AktarimHatasi) as ctx:
            AK.aktar(conn, [makale], yeniden=True, out=lambda *_: None)
        self.assertIn('RAG', str(ctx.exception))
        self.assertFalse(any(s.lstrip().upper().startswith('DELETE') for s in log))


class BatchSemanticTests(unittest.TestCase):
    def test_partial_budget_keeps_cached_and_marks_rest_unchecked(self):
        P = load_pilot()
        rows = [{'onbellek_anahtari': k} for k in ('c1', 'n1', 'c2', 'n2', 'n3')]
        cache = mock.Mock()
        cache.get.side_effect = lambda k: {'x': 1} if k.startswith('c') else None
        run, rest = P.butce_secimi(rows, cache, 1)
        self.assertEqual([r['onbellek_anahtari'] for r in run], ['c1', 'n1', 'c2'])
        self.assertEqual([r['onbellek_anahtari'] for r in rest], ['n2', 'n3'])
        run, rest = P.butce_secimi(rows, cache, 0)
        self.assertEqual(([r['onbellek_anahtari'] for r in run], len(rest)), (['c1', 'c2'], 3))

    def test_toplu_hazirla_uses_bound_candidates_without_api(self):
        P = load_pilot()
        doc = {'document_id': DOC_ID, 'filename': 'x.pdf'}
        with tempfile.TemporaryDirectory() as tmp:
            path, report, _ = write_pair(tmp)
            args = mock.Mock(rapor=[str(path)], tum=True, model='m', cikti=str(Path(tmp) / 'toplu'))
            with mock.patch('src.pilot_clinical.pdf_pages', return_value=(doc, PAGES)):
                out = P.toplu_hazirla(args)
            plan = json.loads((out / 'plan.json').read_text(encoding='utf-8'))
            rows = json.loads((out / 'ornekler.json').read_text(encoding='utf-8'))
            self.assertEqual((plan['tur'], plan['ornek_sayisi'], plan['pdfler']), ('toplu', len(report['kayitlar']), {DOC_ID: 'x.pdf'}))
            self.assertEqual({r['kimlik'] for r in rows}, {e['kayit_kimligi'] for e in report['kayitlar']})
            self.assertFalse((out / 'llm_onbellek.jsonl').exists())
            args = mock.Mock(hazirlik=str(out), max_calls=1, onay=False, deneme=1)
            with self.assertRaises(SystemExit):                                   # --onay olmadan API yok
                P.calistir(args)


class HashMapTests(unittest.TestCase):
    def setUp(self):
        self.P = load_pilot()
        base = A.serializable(A.build_draft([text('Atrial fibrillation (AF)')], PAGES, META, [])['adaylar'][0])
        note = json.loads(base['taslak_notu'])
        self.old = dict(base, taslak_notu=json.dumps({**note, 'model': '1'}, ensure_ascii=False))
        self.new = dict(base, taslak_notu=json.dumps({**note, 'model': 'llm-x', 'tahmin_modeli': '1'},
                                                     ensure_ascii=False))

    def test_metadata_only_change_detected_and_other_changes_rejected(self):
        self.assertTrue(self.P.metadata_only_change(self.old, self.new))
        self.assertFalse(self.P.metadata_only_change(self.old, dict(self.new, deger='9')))
        other = json.loads(self.new['taslak_notu'])
        self.assertFalse(self.P.metadata_only_change(
            self.old, dict(self.new, taslak_notu=json.dumps({**other, 'tahmin_modeli': '2'}))))
        self.assertFalse(self.P.metadata_only_change(
            self.old, dict(self.new, taslak_notu=json.dumps({**other, 'satir_no': 999}))))

    def test_map_links_old_semantic_result_without_changing_it(self):
        old_id = A.satir_hash(DOC_ID, self.old)
        new_id = A.satir_hash(DOC_ID, self.new)
        self.assertNotEqual(old_id, new_id)
        rows = {old_id: {'grup': 'A', 'belge': DOC_ID, 'makale': 'x.pdf', 'aday': self.old}}
        results = [{'kimlik': old_id, 'durum': 'tamamlandi', 'karar': 'destekleniyor', 'hatali_alanlar': []}]
        frozen = json.dumps(results, sort_keys=True)
        found, missing = self.P.hash_map(results, rows, {DOC_ID: ([self.new], set())})
        self.assertEqual((list(found), missing), ([old_id], []))
        self.assertEqual(found[old_id]['yeni_satir_hash'], new_id)
        sem = V.kalite_satirlari({'kayitlar': []}, results, hash_map=found)['rag-semantik-v1'][0]
        self.assertEqual(sem['satir_hash'], new_id)
        self.assertEqual(sem['ayrinti']['eski_satir_hash'], old_id)
        self.assertEqual(sem['ayrinti']['karar'], 'destekleniyor')
        self.assertEqual(json.dumps(results, sort_keys=True), frozen)               # eski sonuç değişmedi
        _, missing = self.P.hash_map(results, rows, {DOC_ID: ([dict(self.new, deger='9')], set())})
        self.assertEqual(len(missing), 1)                                          # metaveri dışı fark: eşleme yok


SQL = (ROOT / 'sql' / 'rag_dogrulama_core.sql').read_text(encoding='utf-8')


def statements(sql):
    body = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    return [s.strip() for s in body.split(';') if s.strip()]


class SqlViewTests(unittest.TestCase):
    def test_only_new_views_created(self):
        stmts = statements(SQL)
        self.assertEqual(len(stmts), 2)
        names = [re.match(r'CREATE OR REPLACE VIEW (\w+) AS', s).group(1) for s in stmts]
        self.assertEqual(names, ['v_rag_dogrulama_durumu', 'v_rag_ek_bilgi'])
        existing = set()
        for f in (ROOT / 'sql').rglob('*.sql'):
            if f.name != 'rag_dogrulama_core.sql':
                existing |= set(re.findall(r'VIEW\s+`?(\w+)`?', f.read_text(encoding='utf-8'), re.I))
        self.assertFalse(set(names) & existing)                                    # mevcut view'lar değişmez

    def test_statements_free_of_write_or_ddl(self):
        for s in statements(SQL):
            self.assertIsNone(re.search(r'\b(ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE|CREATE\s+TABLE)\b', s, re.I), s)

    def test_status_columns_kept_separate_and_human_status_from_review_status(self):
        durum = statements(SQL)[0]
        self.assertIn("'rag_det_ozet'", durum)
        self.assertIn("'rag_sem_ozet'", durum)
        self.assertIn('$.genel_sonuc', durum)
        self.assertIn('$.karar', durum)
        self.assertRegex(durum, r'b\.review_status\s+AS insan_durumu')
        self.assertRegex(durum, r"\(b\.review_status = 'verified'\)\s+AS insan_onayli")
        self.assertIn("kontrol_surumu LIKE 'rag-dogrulama-%'", durum)
        self.assertIn("kontrol_surumu LIKE 'rag-semantik-%'", durum)
        self.assertEqual(durum.count('klinik_bulgu_id IS NULL'), 4)

    def test_only_current_candidate_batch_is_linked(self):
        durum = statements(SQL)[0]
        self.assertIn('MAX(x.grup_id) AS parti', durum)                          # belge başına en son deterministik parti
        self.assertIn("GROUP BY JSON_UNQUOTE(JSON_EXTRACT(x.ayrinti, '$.belge_kimligi'))", durum)
        self.assertIn('MAX(y.grup_id) AS parti', durum)                          # semantik: aynı satir_hash, en son parti
        self.assertIn('ON s.satir_hash = d.satir_hash', durum)
        row = groups_for()[V.SURUM][0]['ayrinti']
        for path in ('belge_kimligi', 'kaynak.aday_dosyasi'):
            self.assertIn(f"'$.{path}'", durum)
            node = row
            for part in path.split('.'):
                node = node[part]

    def test_json_paths_match_keys_produced_by_code(self):
        rows = V.kalite_satirlari(det_report(), SEM)
        self.assertIn('genel_sonuc', rows[V.SURUM][0]['ayrinti'])
        self.assertIn('karar', rows['rag-semantik-v1'][0]['ayrinti'])
        code = ''.join((ROOT / 'src' / f).read_text(encoding='utf-8')
                       for f in ('rag_aktarim.py', 'rag_tablo_ozel.py', 'rag_tablo_hizalama.py'))
        ek = statements(SQL)[1]
        for path in set(re.findall(r"\$\.([\w.]+)'", ek)):
            for part in path.split('.'):
                self.assertRegex(code, rf"['\"]{part}['\"]", path)
        self.assertIn('AS cikarim_modeli', ek)
        self.assertIn("'$.tahmin_modeli'", ek)
        self.assertIn('JSON_VALID', ek)


def load_toplu():
    spec = importlib.util.spec_from_file_location('rag_toplu_isleme', ROOT / 'scripts' / 'rag_toplu_isleme.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def a4_ortak():
    """uretim_asama4_rag.ps1 PY_A4_ORTAK bloğu (check_view) - veritabanısız."""
    text = (ROOT / 'scripts' / 'uretim_asama4_rag.ps1').read_text(encoding='utf-8-sig')
    code = re.search(r"(?s)\$PY_A4_ORTAK = @'\r?\n(.*?)\r?\n'@", text).group(1)
    ns = {'E': {'Y4_WORK': '.', 'Y4_KAYNAK': '.'}, 'GERI': {'kardiyo_tam_metin': 'p'}, 'os': os, 're': re,
          'json': json, 'txt': lambda v: v}
    exec(code, ns)
    return ns


class NextBatchTests(unittest.TestCase):
    def test_same_input_file_never_written_twice_previous_batches_kept(self):
        conn = FakeDB('kardiyo_tam_metin')
        rows = V.kalite_satirlari(det_report(), parti=PARTI, kaynak={'rapor_sha256': 'r1', 'aday_dosyasi_sha256': 'a1'})
        V.kalite_yaz(conn, rows, PARTI, yaz=True, uretim=True)
        before = dict(conn.kalite)
        other = '20261128120000-1111111111111111'
        again = V.kalite_satirlari(det_report(), parti=other, kaynak={'rapor_sha256': 'r1', 'aday_dosyasi_sha256': 'a1'})
        with self.assertRaises(RuntimeError) as ctx:                               # aynı rapor dosyası, farklı parti
            V.kalite_yaz(conn, again, other, yaz=True, uretim=True)
        self.assertIn(PARTI, str(ctx.exception))
        same_digest = '20261128130000-0123456789abcdef'                           # aynı girdi özeti, farklı zaman
        with self.assertRaises(RuntimeError):
            V.kalite_yaz(conn, V.kalite_satirlari(det_report(), parti=same_digest), same_digest, yaz=True, uretim=True)
        self.assertEqual(conn.kalite, before)
        new = V.kalite_satirlari(det_report(), parti=other, kaynak={'rapor_sha256': 'r2'})
        V.kalite_yaz(conn, new, other, yaz=True, uretim=True)
        self.assertTrue(all(conn.kalite[i] == r for i, r in before.items()))   # önceki parti korunur

    def test_semantic_only_continuation_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sem = Path(tmp) / 'sonuclar_1.json'
            sem.write_text(json.dumps({'sonuclar': SEM}), encoding='utf-8')
            groups, parti, src = V.kalite_gruplari([], sem)
            self.assertEqual(set(groups), {'rag-semantik-v1'})                     # deterministik parti değişmez
            self.assertEqual([r['satir_hash'] for r in groups['rag-semantik-v1']], ['h1'])
            self.assertEqual(groups['rag-semantik-v1'][0]['ayrinti']['kaynak']['sonuc_sha256'], src['semantik']['sonuc_sha256'])
            self.assertRegex(parti, r'^\d{14}-[0-9a-f]{16}$')
            with self.assertRaises(ValueError):
                V.kalite_gruplari([], None)
            with self.assertRaises(ValueError):
                V.kalite_gruplari([str(Path(tmp) / 'bos')], sem)

    def test_view_expectation_keeps_other_documents_and_previous_semantic_decisions(self):
        ns = a4_ortak()
        new_parti = '20261128120000-2222222222222222'
        groups = groups_for(new_parti, sem=[{'kimlik': 'x', 'durum': 'tamamlandi', 'karar': 'destekleniyor'}])
        det = [r for r in groups[V.SURUM] if r['kontrol_kodu'] == 'rag_det_ozet']
        h_new = det[0]['satir_hash']
        before = {'o1': ['DIGER', 'gecti', 'kismen_destekleniyor', PARTI, PARTI],      # başka belge, önceki karar
                  'eski': [DOC_ID, 'belirsiz', None, PARTI, None]}                     # aynı belgenin eski adayı
        after = {'o1': list(before['o1'])}
        for r in det:
            after[r['satir_hash']] = [DOC_ID, r['ayrinti']['genel_sonuc'], None, new_parti, None]
        self.assertEqual(ns['check_view'](after, before, groups, new_parti), [])
        lost = dict(after, o1=['DIGER', 'gecti', None, PARTI, None])
        self.assertTrue(ns['check_view'](lost, before, groups, new_parti))          # önceki karar kaybolursa hata
        stale = dict(after, eski=before['eski'])
        self.assertTrue(ns['check_view'](stale, before, groups, new_parti))         # eski aday güncel kalırsa hata
        self.assertNotIn(h_new, before)


class BatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.T = load_toplu()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.T.PDF_KOKU = base / 'pdfs'
        self.T.TOPLU_KOKU = base / 'toplu'
        self.T.ROOT = base
        self.T.PDF_KOKU.mkdir()
        for name, body in (('a.pdf', b'A'), ('b.pdf', b'B'), ('kopya.pdf', b'A')):
            (self.T.PDF_KOKU / name).write_bytes(body)

    def tearDown(self):
        for f in Path(self.tmp.name).rglob('*'):
            if f.is_file():
                os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
        self.tmp.cleanup()

    def plan(self, ad, **kw):
        def fake_plan(pdf, top_k):
            return {'document_id': hashlib.sha256(Path(pdf).read_bytes()).hexdigest(), 'gorev_sayisi': 4,
                    'bekleyen_cagri': 4, 'sayfa_sayisi': 2, 'chunk_sayisi': 3, 'sinir_gorev_sayisi': 0,
                    'tablo_gorev_sayisi': 0}
        args = SimpleNamespace(ad=ad, pdf=[str(self.T.PDF_KOKU)], sql_kontrol=False, yeniden_isle=False, top_k=6, **kw)
        with mock.patch.object(self.T, 'cikarim_plani', side_effect=fake_plan):
            return self.T.plan(args)

    def test_plan_without_api_records_duplicates_and_prior_batches(self):
        state = self.plan('p1')
        self.assertEqual(len(state['makaleler']), 2)                                # aynı içerik bir kez
        self.assertEqual(state['mukerrer'][0]['pdf'].split('/')[-1], 'kopya.pdf')
        self.assertTrue(all(m['plan']['bekleyen_cagri'] == 4 and not m['atla'] for m in state['makaleler'].values()))
        doc = next(iter(state['makaleler']))
        state['makaleler'][doc]['adimlar']['taslak'] = {'durum': 'tamam'}
        self.T.durum_yaz('p1', state)
        state2 = self.plan('p2')
        self.assertIn('p1', state2['makaleler'][doc]['atla'])                       # önceki partide işlendi
        with self.assertRaises(SystemExit):
            self.T.pdf_listesi([__file__])                                          # data/pdfs dışı

    def test_extraction_needs_api_approval_and_respects_budget(self):
        self.plan('p1')
        state = self.T.durum_oku('p1')
        base = SimpleNamespace(ad='p1', top_k=6)
        with self.assertRaises(SystemExit):
            self.T.calistir_cikarim(SimpleNamespace(**vars(base), api_onay=False, cikarim_butce=10), state)
        with mock.patch.object(self.T, 'cikarim_plani', return_value={'bekleyen_cagri': 4}), \
                mock.patch('src.rag_klinik_cikarim.run', side_effect=AssertionError('API çağrısı yapılmamalı')):
            self.T.calistir_cikarim(SimpleNamespace(**vars(base), api_onay=True, cikarim_butce=3), state)
        self.assertTrue(all(m['adimlar']['cikarim']['durum'] == 'butce_yetersiz' for m in state['makaleler'].values()))

    def test_semantic_copies_keep_previous_decisions(self):
        self.plan('p1')
        state = self.T.durum_oku('p1')
        sdir = self.T.klasor('p1') / 'semantik'
        sdir.mkdir()
        state['semantik'] = {'hazirlik': 'toplu/p1/semantik', 'kopyalar': []}
        runs = [[{'kimlik': 'h1', 'durum': 'tamamlandi', 'karar': 'destekleniyor'},
                 {'kimlik': 'h2', 'durum': 'kontrol_edilmedi', 'karar': None}],
                [{'kimlik': 'h1', 'durum': 'tamamlandi', 'karar': 'destekleniyor'},
                 {'kimlik': 'h2', 'durum': 'tamamlandi', 'karar': 'kaynakla_celisiyor'}],
                [{'kimlik': 'h1', 'durum': 'calistirilamadi', 'karar': None},
                 {'kimlik': 'h2', 'durum': 'tamamlandi', 'karar': 'kaynakla_celisiyor'}]]
        pilot = mock.Mock()
        args = SimpleNamespace(ad='p1', api_onay=True, semantik_butce=1, deneme=1)
        with mock.patch.object(self.T, 'load_pilot', return_value=pilot):
            for i, results in enumerate(runs):
                pilot.calistir.side_effect = lambda a, r=results: (sdir / 'sonuclar.json').write_text(
                    json.dumps({'sonuclar': r}), encoding='utf-8')
                if i < 2:
                    with mock.patch.object(self.T, 'datetime') as dt:
                        dt.now.return_value = datetime(2026, 11, 28, 12, 0, i)
                        self.T.calistir_semantik(args, state)
                else:
                    with self.assertRaises(RuntimeError):                           # h1 kararı kaybolurdu
                        self.T.calistir_semantik(args, state)
        self.assertEqual([c['tamamlanan'] for c in state['semantik']['kopyalar']], [1, 2])
        self.assertEqual(len(list(sdir.glob('sonuclar_*.json'))), 2)
        with self.assertRaises(SystemExit):
            self.T.calistir_semantik(SimpleNamespace(ad='p1', api_onay=False, semantik_butce=1, deneme=1), state)

    def test_ingestion_commit_guard_rolls_back_on_change(self):
        conn = mock.Mock()
        guard = self.T.DenetimliBaglanti(conn, {'klinik_bulgular': {1: 'a'}, 'kalite_isaretleri': {}, 'sunum': {}})
        with mock.patch.object(self.T, '_koruma_izi', return_value={'klinik_bulgular': {1: 'b'}, 'kalite_isaretleri': {},
                                                                     'sunum': {}}):
            with self.assertRaises(RuntimeError):
                guard.commit()
        conn.commit.assert_not_called()
        with mock.patch.object(self.T, '_koruma_izi', return_value={'klinik_bulgular': {1: 'a'}, 'kalite_isaretleri': {},
                                                                     'sunum': {}}):
            guard.commit()
        conn.commit.assert_called_once()
        with self.assertRaises(SystemExit):
            self.T.ingestion(SimpleNamespace(ad='p1', uretim_onay=False, yedek='x', model=None))


class ExtractionPlanTests(unittest.TestCase):
    def test_plan_counts_uncached_calls_without_api(self):
        from src import rag_klinik_cikarim as K
        doc = {'document_id': DOC_ID, 'filename': 'x.pdf', 'processing_version': 'v'}
        pages = {1: TEXT, 2: TEXT}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(K, 'pdf_pages', return_value=(doc, pages)), \
                mock.patch.object(K, 'build_chunks', return_value=[]), \
                mock.patch.object(K, 'plan_pages', return_value=({1: ['diseases']}, {})), \
                mock.patch.object(K, 'fill_coverage', return_value=([2], [])), \
                mock.patch.object(K, 'llm_client', side_effect=AssertionError('API yok')):
            meta, run_dir = K.run('x.pdf', plan_only=True, model='m', out_root=tmp)
            self.assertIsNone(run_dir)
            self.assertEqual(meta['bekleyen_cagri'], meta['gorev_sayisi'])
            tasks = K.build_tasks(pages, {1: ['diseases']})
            system, payload = K.task_request(tasks[0])
            K.Cache(Path(tmp) / 'x' / 'llm_onbellek.jsonl').put(K.Cache.key('m', system, payload), {'kayitlar': []})
            meta, _ = K.run('x.pdf', plan_only=True, model='m', out_root=tmp)
            self.assertEqual(meta['bekleyen_cagri'], meta['gorev_sayisi'] - 1)


if __name__ == '__main__':
    unittest.main()
