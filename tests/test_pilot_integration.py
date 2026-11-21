"""Offline SQL contract and clinical extraction tests; no database/API writes."""
import unittest
from unittest.mock import patch
from types import ModuleType
from src.pilot_clinical import import_records
from src.pilot_auto_extract import extract_pages


class Cursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1
        self.last = ''
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last = sql
    def fetchone(self):
        return ('pilot.pdf',)
    def fetchall(self):
        return [(c,) for c in ('bilgi_turu', 'klinik_bilgi', 'klinik_bilgi_en', 'document_id', 'kanit_sayfa', 'kanit_cumle', 'kanit_turu', 'satir_hash', 'baglam', 'review_status')]
    def close(self): pass


class Connection:
    def __init__(self):
        self.cur = Cursor()
        self.commits = 0
        self.rollbacks = 0
    def cursor(self): return self.cur
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): pass


class Choice:
    finish_reason = 'stop'
    class message:
        content = '{"findings": [{"bilgi_turu":"tani","klinik_bilgi_en":"atrial fibrillation","kanit_cumle":"Patients with atrial fibrillation were enrolled.","sayfa":1,"kanit_turu":"metin"},{"bilgi_turu":"semptom","klinik_bilgi_en":"palpitations","kanit_cumle":"Symptoms included palpitations and dizziness.","sayfa":1,"kanit_turu":"metin"},{"bilgi_turu":"semptom","klinik_bilgi_en":"dizziness","kanit_cumle":"Symptoms included palpitations and dizziness.","sayfa":1,"kanit_turu":"metin"},{"bilgi_turu":"teshis_yontemi","klinik_bilgi_en":"ECG","kanit_cumle":"Diagnosis was confirmed using ECG.","sayfa":1,"kanit_turu":"metin"}]}'


class FakeClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                class Result:
                    choices = [Choice()]
                return Result()


class Tests(unittest.TestCase):
    def test_sql_payload_verified(self):
        db = Connection()
        doc = {'document_id': 'a' * 64, 'filename': 'pilot.pdf'}
        records = [{'bilgi_turu': 'semptom', 'baglam': 'study_finding',
                    'review_status': 'verified', 'klinik_bilgi': 'Çarpıntı',
                    'klinik_bilgi_en': 'palpitations', 'sayfa': 2,
                    'kanit_cumle': 'Patients reported palpitations.'}]
        fake_database = ModuleType('src.database')
        fake_database.get_connection = lambda: db
        with patch.dict('sys.modules', {'src.database': fake_database}):
            self.assertEqual(import_records(doc, records), 1)
        self.assertEqual(db.commits, 1)
        insert_sql, values = db.cur.calls[-1]
        self.assertIn('INSERT IGNORE INTO klinik_bulgular', insert_sql)
        self.assertIn('`bilgi_turu`', insert_sql)
        self.assertIn('`baglam`', insert_sql)
        self.assertIn('`klinik_bilgi_en`', insert_sql)
        self.assertIn('verified', values)
        self.assertIn('study_finding', values)
        self.assertIn('palpitations', values)

    def test_sql_rejects_pending_without_connection(self):
        doc = {'document_id': 'a' * 64, 'filename': 'pilot.pdf'}
        record = {'bilgi_turu': 'semptom', 'baglam': 'study_finding',
                  'review_status': 'pending', 'klinik_bilgi_en': 'palpitations',
                  'sayfa': 2, 'kanit_cumle': 'Patients reported palpitations.'}
        fake_database = ModuleType('src.database')
        fake_database.get_connection = lambda: self.fail('Veritabanına bağlanılmamalı')
        with patch.dict('sys.modules', {'src.database': fake_database}):
            with self.assertRaisesRegex(ValueError, 'manuel onaylı'):
                import_records(doc, [record])

    def test_sql_rejects_missing_context_without_connection(self):
        doc = {'document_id': 'a' * 64, 'filename': 'pilot.pdf'}
        record = {'bilgi_turu': 'semptom', 'review_status': 'verified',
                  'klinik_bilgi_en': 'palpitations', 'sayfa': 2,
                  'kanit_cumle': 'Patients reported palpitations.'}
        fake_database = ModuleType('src.database')
        fake_database.get_connection = lambda: self.fail('Veritabanına bağlanılmamalı')
        with patch.dict('sys.modules', {'src.database': fake_database}):
            with self.assertRaisesRegex(ValueError, 'bağlamı geçerli'):
                import_records(doc, [record])

    def test_distinct_facts_from_same_page(self):
        pages = {1: 'Patients with atrial fibrillation were enrolled.\nSymptoms included palpitations and dizziness.\nDiagnosis was confirmed using ECG.'}
        records, rejected, calls = extract_pages(pages, FakeClient(), 'mock-model')
        self.assertEqual(len(records), 4)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(calls, 1)
        self.assertEqual({r['bilgi_turu'] for r in records}, {'tani', 'semptom', 'teshis_yontemi'})


if __name__ == '__main__':
    unittest.main()
