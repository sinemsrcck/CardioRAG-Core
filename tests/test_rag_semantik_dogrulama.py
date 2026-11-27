"""src.rag_semantik_dogrulama ve scripts/rag_semantik_pilot örneklem seçimi. GERÇEK API ÇAĞRISI YOK: sahte istemci
OpenAI chat.completions biçiminde yanıt döndürür. Veritabanı yok."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src import rag_semantik_dogrulama as S
from src.rag_klinik_cikarim import Cache

ROOT = Path(__file__).resolve().parents[1]
TEXT = ('Background paragraph about the cohort.\n\n'
        'Aspirin is not recommended for stroke prevention in patients with AF. Other drugs were considered.\n\n'
        'Next paragraph with more context.')
TABLE = ('**Table 1:** Baseline\n|Variable|Event (n=50)|No event (n=80)|\n|---|---|---|\n|Age|62±10.4|58.2±11.2|\n'
         '\nAbbreviations: SD, standard deviation.')
PAGES = {1: TEXT, 2: TABLE}
CAND = {'bilgi_turu': 'ilac', 'baglam': 'recommendation', 'klinik_bilgi_en': 'Aspirin', 'sayfa': 1,
        'kanit_cumle': 'Aspirin is not recommended for stroke prevention in patients with AF.', 'kanit_turu': 'metin',
        'review_status': 'pending', 'taslak_notu': {'anlam_denetimi': {'uyarilar': ['x']}, 'kaynak': 'rag',
                                                    'referans': 'GİZLİ'}}


def response(content, finish='stop', prompt=900, completion=120):
    msg = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls += 1
                out = outer.outputs.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out
        self.chat = SimpleNamespace(completions=_C())


def verdict(karar='kaynakla_celisiyor', quote='Aspirin is not recommended for stroke prevention', page=1, **kw):
    body = {'karar': karar, 'hatali_alanlar': [{'alan': 'klinik_bilgi_en', 'sorun': 'olumsuzluk kaybı'}],
            'gerekce': 'Kaynak aspirini önermiyor.', 'kanit_alintilari': [{'metin': quote, 'sayfa': page}],
            'konum': {'sayfa': page, 'tablo': None, 'satir': None, 'sutun': None}, 'yeniden_cikarim_gerekli': True}
    body.update(kw)
    return response(json.dumps({'findings': [body]}))


def item(cand=CAND, kimlik='k1'):
    return {'kimlik': kimlik, 'belge': 'doc', 'grup': 'B', 'aday': cand}


def run(outputs, items=None, max_calls=5, cache=None):
    client = FakeClient(outputs)
    cache = cache if cache is not None else Cache(None)
    results, counter = S.run(items or [item()], {'doc': PAGES}, client, 'model-x', cache, max_calls=max_calls,
                             sleep=lambda s: None)
    return results, counter, client, cache


class PayloadTests(unittest.TestCase):
    def test_payload_has_source_context_but_no_reference_or_deterministic_results(self):
        payload = S.build_payload(CAND, PAGES)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('GİZLİ', text)
        self.assertNotIn('anlam_denetimi', text)
        self.assertNotIn('review_status', text)
        ctx = payload['kaynak'][0]['baglam']
        self.assertTrue(ctx['bulundu'])
        self.assertIn('Aspirin is not recommended', ctx['paragraf'])
        self.assertIn('Background paragraph', ctx['onceki'])
        self.assertIn('Next paragraph', ctx['sonraki'])
        self.assertEqual(payload['dogrulayici_surumu'], S.SURUM)

    def test_table_payload_includes_caption_header_row_and_notes(self):
        cand = {'bilgi_turu': 'olcum', 'baglam': 'study_finding', 'klinik_bilgi_en': 'Age', 'sayfa': 2,
                'kanit_cumle': '|Age|62±10.4|58.2±11.2|', 'kanit_turu': 'tablo', 'deger': '62±10.4',
                'hasta_grubu': 'Event (n=50)', 'taslak_notu': {'tablo_no': 1, 'sutun': 'Event (n=50)'}}
        ctx = S.build_payload(cand, PAGES)['kaynak'][0]['baglam']
        self.assertEqual(ctx['tablo_basligi'], '**Table 1:** Baseline')
        self.assertIn('|Variable|Event (n=50)|No event (n=80)|', ctx['baslik_satirlari'])
        self.assertIn('Abbreviations: SD, standard deviation.', ctx['alt_notlar'])
        self.assertEqual(S.build_payload(cand, PAGES)['kayit']['tablo_ve_baglam_notlari'],
                         {'tablo_no': 1, 'sutun': 'Event (n=50)'})

    def test_cache_key_covers_source_record_model_and_version(self):
        key = Cache.key('m', S.SYSTEM, S.build_payload(CAND, PAGES))
        self.assertNotEqual(key, Cache.key('m2', S.SYSTEM, S.build_payload(CAND, PAGES)))
        self.assertNotEqual(key, Cache.key('m', S.SYSTEM, S.build_payload(dict(CAND, deger='5'), PAGES)))
        changed = {1: TEXT.replace('Other drugs', 'Other agents'), 2: TABLE}
        self.assertNotEqual(key, Cache.key('m', S.SYSTEM, S.build_payload(CAND, changed)))
        self.assertIn(S.SURUM, S.SYSTEM)


class RunTests(unittest.TestCase):
    def test_valid_verdict_with_verified_quote(self):
        results, counter, client, cache = run([verdict()])
        r = results[0]
        self.assertEqual((r['durum'], r['karar'], r['model_karari']), ('tamamlandi', 'kaynakla_celisiyor',
                                                                        'kaynakla_celisiyor'))
        self.assertTrue(r['alinti_dogrulama'])
        self.assertTrue(r['yeniden_cikarim_gerekli'])
        self.assertEqual((counter.cagri, counter.girdi, counter.cikti), (1, 900, 120))
        self.assertIsNotNone(cache.get(r['onbellek_anahtari']))
        self.assertTrue(r['insan_dogrulamasi_degildir'])

    def test_unverifiable_or_missing_quote_downgrades_to_uncertain(self):
        results, *_ = run([verdict(karar='destekleniyor', quote='Aspirin is recommended for everyone')])
        self.assertEqual((results[0]['model_karari'], results[0]['karar'], results[0]['alinti_dogrulama']),
                         ('destekleniyor', 'belirsiz', False))
        results, *_ = run([verdict(karar='destekleniyor', kanit_alintilari=[])])
        self.assertEqual(results[0]['karar'], 'belirsiz')
        results, *_ = run([verdict(karar='destekleniyor', page=2)])            # başka sayfanın alıntısı
        self.assertEqual(results[0]['karar'], 'belirsiz')

    def test_invalid_output_timeout_and_quota_never_supported_nor_cached(self):
        timeout = TimeoutError('timed out')          # yeniden denenen hata türü (openai yoksa TimeoutError)
        outputs = [response('not json'), response(json.dumps({'findings': [{'karar': 'harika'}]})),
                   response('{"findings": []}', finish='length'), timeout, timeout, timeout,
                   RuntimeError('quota exceeded')]
        items = [item(kimlik=f'k{i}', cand=dict(CAND, deger=str(i))) for i in range(5)]
        results, counter, client, cache = run(outputs, items=items, max_calls=5)
        self.assertEqual([r['durum'] for r in results], ['gecersiz_cikti', 'gecersiz_cikti', 'gecersiz_cikti',
                                                         'calistirilamadi', 'calistirilamadi'])
        self.assertTrue(all(r['karar'] is None for r in results))
        self.assertEqual(cache.data, {})                                         # hiçbiri önbelleğe girmedi

    def test_budget_exceeded_makes_no_call(self):
        items = [item(kimlik=f'k{i}', cand=dict(CAND, deger=str(i))) for i in range(3)]
        client = FakeClient([verdict()] * 3)
        with self.assertRaises(S.ButceAsildi):
            S.run(items, {'doc': PAGES}, client, 'model-x', Cache(None), max_calls=2)
        self.assertEqual(client.calls, 0)

    def test_resume_from_cache_without_new_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.jsonl'
            first, _, client1, _ = run([verdict()], cache=Cache(path))
            second, counter, client2, _ = run([], cache=Cache(path), max_calls=0)      # bütçe 0: yalnız önbellek
            self.assertEqual(client2.calls, 0)
            self.assertEqual((second[0]['kaynak'], second[0]['karar']), ('onbellek', first[0]['karar']))

    def test_records_not_modified(self):
        cand = json.loads(json.dumps(CAND))
        run([verdict()], items=[item(cand=cand)])
        self.assertEqual(cand, CAND)

    def test_metrics_and_usage(self):
        items = [item(kimlik='a', cand=dict(CAND, deger='1')), item(kimlik='b', cand=dict(CAND, deger='2'))]
        results, counter, *_ = run([verdict(karar='kismen_destekleniyor'), verdict(karar='destekleniyor')],
                                   items=items)
        sample = {'a': {'grup': 'A', 'makale': 'm1', 'kanit_turu': 'metin', 'deterministik_sonuc': 'gecti'},
                  'b': {'grup': 'C', 'makale': 'm2', 'kanit_turu': 'metin', 'deterministik_sonuc': 'basarisiz'}}
        m = S.metrics(results, sample, references={'a': True, 'b': False})
        self.assertEqual(m['deterministik_gecti_semantik_sorunlu'], ['a'])
        self.assertEqual(m['deterministik_isaretli_semantik_destekli'], ['b'])
        self.assertEqual(m['referansla_eslesen_semantik_sorunlu'], ['a'])
        self.assertIn('klinik doğruluk oranı değildir', m['not'])
        self.assertEqual(S.usage_report(counter, {'girdi': 1.0, 'cikti': 2.0})['maliyet_usd'],
                         round(1800 / 1e6 + 240 * 2 / 1e6, 4))


def load_pilot():
    spec = importlib.util.spec_from_file_location('pilot', ROOT / 'scripts' / 'rag_semantik_pilot.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SampleSelectionTests(unittest.TestCase):
    def entries(self):
        out = []
        for a in range(6):
            for k in range(30):
                e = {'kimlik': f'{a}-{k}', 'makale': f'makale{a}', 'kanit_turu': 'tablo' if k % 3 else 'metin',
                     'bilgi_turu': ('olcum', 'tani', 'ilac')[k % 3], 'deterministik_sonuc': 'gecti',
                     'semantik_gerekce': [], 'klinik_iliski': 'uygulanamaz', 'kapsam': False, 'ozel_tur': None,
                     'kaynak_sira': k}
                if k < 4:
                    e.update(klinik_iliski='basarisiz', deterministik_sonuc='basarisiz', semantik_gerekce=['klinik_iliski'])
                elif k < 10:
                    e.update(deterministik_sonuc='belirsiz', semantik_gerekce=['klinik_iliski'], klinik_iliski='belirsiz')
                elif k < 12:
                    e.update(ozel_tur='calisma_satir')
                out.append(e)
            out.append({'kimlik': f'{a}-r', 'makale': f'makale{a}', 'kanit_turu': 'metin', 'bilgi_turu': 'tani',
                        'deterministik_sonuc': 'yeniden_cikarim_gerekli', 'semantik_gerekce': ['x'],
                        'klinik_iliski': None, 'kapsam': False, 'ozel_tur': None, 'kaynak_sira': None,
                        'yeniden_cikarim': True})
        return out

    def test_disjoint_balanced_and_reproducible(self):
        P = load_pilot()
        entries = self.entries()
        first = P.select_sample(entries)
        second = P.select_sample(list(reversed(entries)))
        self.assertEqual([e['kimlik'] for e in first], [e['kimlik'] for e in second])      # girdi sırasından bağımsız
        self.assertEqual(len({e['kimlik'] for e in first}), len(first))                      # gruplar arası tekrar yok
        counts = {g: sum(e['grup'] == g for e in first) for g, _ in P.GRUPLAR}
        self.assertEqual(counts, dict(P.GRUPLAR))
        a_group = [e['makale'] for e in first if e['grup'] == 'A_deterministik_gecti']
        self.assertTrue(max(a_group.count(m) for m in set(a_group)) - min(a_group.count(m) for m in set(a_group)) <= 1)
        self.assertEqual(len(set(a_group)), 6)
        self.assertTrue(all(e['yeniden_cikarim'] for e in first if e['grup'] == 'D_yeniden_cikarim'))


if __name__ == '__main__':
    unittest.main()
