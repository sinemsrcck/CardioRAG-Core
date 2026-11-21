"""pilot_auto_extract ret raporu: reddedilen kaydın tam orijinal JSON'u, sayfası, sırası ve gerekçesi.

API çağrısı yok (sahte istemci); geçerli çıktının davranışı değişmez."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import pilot_auto_extract as auto

PAGES = {1: 'Atrial fibrillation was common. Dyspnea was reported.', 2: 'Stroke occurred in some patients.'}
VALID = dict(bilgi_turu='tani', baglam='study_finding', klinik_bilgi_en='Atrial fibrillation',
             kanit_cumle='Atrial fibrillation was common.', sayfa=1)
SYMPTOM = dict(bilgi_turu='semptom', baglam='study_finding', klinik_bilgi_en='Dyspnea',
               kanit_cumle='Dyspnea was reported.', sayfa=1)
PAGE1 = [
    VALID,                                                        # 1 kabul
    'serbest metin',                                              # 2 nesne değil
    dict(VALID, sayfa=2),                                         # 3 sayfa geçersiz
    dict(VALID, kanit_cumle='Atrial fibrillation was rare.'),     # 4 kanıt parçada yok
    dict(VALID),                                                  # 5 tekrar -> doğrulama
    dict(SYMPTOM, klinik_bilgi_en='Shortness of breath'),         # 6 bilgi kanıtta yok -> doğrulama
    SYMPTOM,                                                      # 7 kabul
]
BOUNDARY = [dict(VALID, sayfa_bitis=3, kanit_cumle='reported.Stroke')]  # sınır kaydı sayfa geçersiz


def response(findings):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({'findings': findings})))])


class Client:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs['messages'][1]['content'])
        if 'end_page' in payload:
            return response(BOUNDARY)
        return response(PAGE1 if payload['sayfa'] == 1 else [])


def run(**kw):
    return auto.extract_pages(PAGES, kw.pop('client', None) or Client(), 'mock-model',
                              retry_exceptions=(TimeoutError,), sleep=lambda s: None, **kw)


class RejectionReport(unittest.TestCase):
    def test_valid_output_unchanged(self):
        valid, rejected, calls = run()
        self.assertEqual(valid, [VALID, SYMPTOM])
        self.assertEqual(calls, 3)  # sayfa 1, sayfa 2, 1→2 sınırı
        self.assertEqual(len(rejected), 6)

    def test_every_rejection_has_full_original_record(self):
        _, rejected, _ = run()
        by_order = {r['aday_sirasi']: r for r in rejected}
        expected = {2: ('Kayıt nesne değil', 'on_eleme'), 3: ('Sayfa veya tür geçersiz', 'on_eleme'),
                    4: ('Kanıt parçada bulunamadı', 'on_eleme'), 8: ('Sınır kaydı sayfa veya tür geçersiz', 'on_eleme')}
        for order, (reason, stage) in expected.items():
            self.assertEqual((by_order[order]['neden'], by_order[order]['asama']), (reason, stage), order)
        for order in (2, 3, 4):
            self.assertEqual(by_order[order]['kayit'], PAGE1[order - 1])
            self.assertEqual((by_order[order]['sayfa'], by_order[order]['gorev_turu'], by_order[order]['gorev_ici_sira'],
                              by_order[order]['sayfa_bitis']), (1, 'page', order, None))
        boundary = by_order[8]
        self.assertEqual(boundary['kayit'], BOUNDARY[0])
        self.assertEqual((boundary['sayfa'], boundary['sayfa_bitis'], boundary['gorev_turu'],
                          boundary['gorev_ici_sira'], boundary['gorev_no']), (1, 2, 'boundary', 1, 3))

    def test_validation_rejections_keep_satir_and_gain_origin(self):
        _, rejected, _ = run()
        stage2 = [r for r in rejected if r['asama'] == 'dogrulama']
        self.assertEqual([r['aday_sirasi'] for r in stage2], [5, 6])
        duplicate, fact = stage2
        self.assertTrue(duplicate['neden'].startswith('tekrar'))
        self.assertIn('Klinik bilgi', fact['neden'])
        self.assertEqual((duplicate['satir'], fact['satir']), (2, 3))  # ön elemeden geçenler içindeki sıra
        self.assertEqual((duplicate['kayit'], fact['kayit']), (PAGE1[4], PAGE1[5]))
        self.assertEqual((duplicate['sayfa'], duplicate['gorev_no'], duplicate['gorev_ici_sira']), (1, 1, 5))

    def test_old_fields_kept_and_report_serializable(self):
        _, rejected, _ = run()
        for entry in rejected:
            self.assertTrue({'sayfa', 'neden', 'asama', 'aday_sirasi', 'gorev_no', 'gorev_turu', 'gorev_ici_sira',
                             'sayfa_bitis', 'kayit'} <= set(entry))
        self.assertEqual(json.loads(json.dumps(rejected, ensure_ascii=False)), rejected)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.rejected.json'
            auto.write_new(path, rejected)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), rejected)

    def test_report_is_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / 'cp.json'
            _, rejected, _ = run(checkpoint_path=cp)
            before = cp.read_text(encoding='utf-8')
            rejected[1]['kayit']['sayfa'] = 99
            self.assertEqual(cp.read_text(encoding='utf-8'), before)
            # Kaldığı yerden (tamamlanmış checkpoint) çalıştırma API çağırmadan aynı raporu üretir.
            client = Client()
            valid, again, calls = run(checkpoint_path=cp, client=client)
            self.assertEqual((calls, client.calls), (0, 0))
            original = copy.deepcopy(again)
            self.assertEqual(again[1]['kayit']['sayfa'], 2)
            self.assertEqual(valid, [VALID, SYMPTOM])
            self.assertEqual(original, again)


if __name__ == '__main__':
    unittest.main()
