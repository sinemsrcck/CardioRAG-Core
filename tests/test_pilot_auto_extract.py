import json
import unittest
from types import SimpleNamespace
from src.pilot_auto_extract import extract_pages, split_page

class FakeClient:
    def __init__(self, records):
        self.records = records
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'findings': self.records})))])

class Tests(unittest.TestCase):
    def test_valid_distinct_facts(self):
        page = 'Atrial fibrillation and palpitations were reported.'
        records = [dict(bilgi_turu=k, klinik_bilgi_en=v, kanit_cumle=page, sayfa=1, kanit_turu='metin') for k,v in [('tani','Atrial fibrillation'), ('semptom','palpitations')]]
        valid, rejected, calls = extract_pages({1:page}, FakeClient(records), 'test')
        self.assertEqual((len(valid),len(rejected),calls),(2,0,1))
    def test_reject_invented_fact(self):
        record = dict(bilgi_turu='tani', klinik_bilgi_en='heart failure', kanit_cumle='heart failure', sayfa=1)
        valid, rejected, _ = extract_pages({1:'Atrial fibrillation'}, FakeClient([record]), 'test')
        self.assertEqual(len(valid),0)
        self.assertEqual(len(rejected),1)
    def test_split_preserves_text(self):
        text = 'one\n' * 200
        chunks = split_page(text, limit=100)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(c in text for c in chunks))

if __name__ == '__main__':
    unittest.main()
