import json
import unittest
from types import SimpleNamespace
from src.pilot_auto_extract import extract_pages, split_page, locate_exact, is_non_english

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
    def test_normalized_quote_replaced_by_exact_source(self):
        page = 'Survival was low – only 6.3%\nsurvived to dis-\ncharge.'
        rec = dict(bilgi_turu='klinik_sonuc', baglam='study_finding', klinik_bilgi_en='only 6.3% survived to discharge',
                   kanit_cumle='Survival was low - only 6.3% survived to discharge.', sayfa=1, kanit_turu='metin')
        valid, rejected, _ = extract_pages({1: page}, FakeClient([rec]), 'test')
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0]['kanit_cumle'], page)
        self.assertEqual(valid[0]['klinik_bilgi_en'], 'only 6.3%\nsurvived to dis-\ncharge')
    def test_locate_exact_quotes_and_none(self):
        self.assertEqual(locate_exact('"ROSC" rate', 'the “ROSC”  rate'), '“ROSC”  rate')
        self.assertIsNone(locate_exact('heart failure', 'atrial fibrillation'))
    def test_reject_invalid_kanit_turu_and_non_english(self):
        arabic = 'السكتة القلبية'
        french = "Les taux de survie dans les pays du CCG sont faibles par rapport aux autres pays."
        page = arabic + '\n' + french + '\nAF was common.'
        recs = [dict(bilgi_turu='tani', klinik_bilgi_en='AF', kanit_cumle='AF was common.', sayfa=1, kanit_turu='metن'),
                dict(bilgi_turu='tani', klinik_bilgi_en=arabic[:6], kanit_cumle=arabic, sayfa=1, kanit_turu='metin'),
                dict(bilgi_turu='klinik_sonuc', klinik_bilgi_en='Les taux de survie', kanit_cumle=french, sayfa=1, kanit_turu='metin')]
        valid, rejected, _ = extract_pages({1: page}, FakeClient(recs), 'test')
        self.assertEqual(valid, [])
        self.assertEqual([r['neden'] for r in rejected], ['Geçersiz kanit_turu (yalnızca metin veya tablo)',
                                                          'İngilizce olmayan metin', 'İngilizce olmayan metin'])
        self.assertFalse(is_non_english('Use of emergency medical services was low across the GCC countries.'))
    def test_split_preserves_text(self):
        text = 'one\n' * 200
        chunks = split_page(text, limit=100)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(c in text for c in chunks))

if __name__ == '__main__':
    unittest.main()
