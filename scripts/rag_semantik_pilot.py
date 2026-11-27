"""LLM semantik doğrulama pilotu: örneklem hazırlığı (API yok), onaylı çalıştırma, değerlendirme.

    python scripts/rag_semantik_pilot.py hazirla --kaynak evaluation/rag_onbellek_degerlendirme/<çalıştırma>
    python scripts/rag_semantik_pilot.py calistir --hazirlik <klasör> --max-calls 60 --onay      # GERÇEK API
        (HTTP isteği üst sınırı = --max-calls × --deneme; --deneme varsayılanı 1)
    python scripts/rag_semantik_pilot.py degerlendir --hazirlik <klasör> [--fiyat-girdi X --fiyat-cikti Y]
    python scripts/rag_semantik_pilot.py toplu-hazirla --rapor <klasör|*.dogrulama.json ...> [--tum]   # API yok
    python scripts/rag_semantik_pilot.py calistir --hazirlik <toplu klasör> --max-calls N --onay      # GERÇEK API

toplu-hazirla: üretim adayları için ÖRNEKLEMSİZ hazırlık (aynı doğrulayıcı, aynı yük, aynı önbellek anahtarı). Kaynak:
deterministik doğrulama raporları ve bağlı aday dosyaları (src.rag_dogrulama.aday_dosyasi bağ denetimi). Varsayılan:
semantik_dogrulama_gerekli adaylar; --tum: bütün adaylar. Referans dosyası yoktur. calistir toplu hazırlıkta bütçeyi
KISMİ uygular: önbellekteki sonuçlar + önbellekte olmayan ilk --max-calls aday; kalanlar 'kontrol_edilmedi' olarak
ayrı tutulur. Aynı komut yeniden çalıştırıldığında önbellektekiler API'siz gelir, sıradaki adaylar işlenir (kaldığı
yerden devam). Başarısız çağrılar önbelleğe girmez, sonraki çalıştırmada yeniden denenir.

hazirla: değerlendirme çalıştırmasının kayıtlarından ve deterministik doğrulama raporundan 60 kayıtlık, gruplar arası
tekrarsız, makalelere dengeli, sabit tohumlu (yeniden üretilebilir) örneklem seçer; her kayıt için doğrulayıcı yükünü
kurar ve token / çağrı tahmini çıkarır. Referans eşleşmeleri AYRI dosyaya yazılır ve doğrulayıcıya verilmez.
Hiçbir mevcut dosya değişmez; çıktı yeni klasöre yazılır.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import rag_semantik_dogrulama as S  # noqa: E402

PILOT_KOKU = ROOT / 'evaluation' / 'rag_semantik_pilot'
TOHUM = 'rag-semantik-pilot-v1'
GRUPLAR = (('D_yeniden_cikarim', 5), ('C_klinik_iliski_basarisiz', 10), ('E_karmasik_tablo_kapsam', 5),
           ('B_semantik_belirsiz', 20), ('A_deterministik_gecti', 20))
OZEL_TURLER = ('calisma_satir', 'calisma_sutun', 'yan_yana', 'oneri', 'test_listesi')
TAHMINI_CIKTI_TOKEN = (250, 500)


def _hash(text):
    return hashlib.sha256(f'{TOHUM}|{text}'.encode('utf-8')).hexdigest()


def _record_id(rec):
    return hashlib.sha256(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def collect(run_dir):
    """Değerlendirme çalıştırmasından aday havuzu: [{kimlik, makale, belge, aday, kanit_turu, bilgi_turu,
    deterministik_sonuc, semantik_gerekce, klinik_iliski, kapsam, ozel_tur, kaynak_sira, havuzlar}]."""
    out = []
    for path in sorted(Path(run_dir).glob('*.json')):
        if path.name == 'ozet.json' or path.name.endswith('.dogrulama.json'):
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        report = json.loads(path.with_name(path.stem + '.dogrulama.json').read_text(encoding='utf-8'))
        records, candidates = data['kayitlar'], data['taslak_adaylari']
        doc = report['belge_kimligi']
        for cand, entry in zip(candidates, report['kayitlar']):
            item = records[entry['kaynak_sira']] if entry.get('kaynak_sira') is not None else {}
            checks = {c['kod']: c['sonuc'] for c in entry['kontroller']}
            out.append({'kimlik': entry['kayit_kimligi'], 'makale': data['pdf'], 'belge': doc, 'aday': cand,
                        'kanit_turu': cand.get('kanit_turu'), 'bilgi_turu': cand.get('bilgi_turu'),
                        'deterministik_sonuc': entry['genel_sonuc'], 'semantik_gerekce': entry['semantik_gerekce'],
                        'klinik_iliski': checks.get('klinik_iliski'), 'kapsam': checks.get('anlam_kapsam') == 'belirsiz',
                        'tablo_hucre': checks.get('tablo_hucre'),
                        'ozel_tur': (item.get('tablo_modeli') or {}).get('tur'), 'kaynak_sira': entry.get('kaynak_sira')})
        for x in data['reddedilenler']:
            if x.get('asama') == 'yeniden_cikarim_gerekli' and isinstance(x.get('kayit'), dict):
                rec = x['kayit']
                out.append({'kimlik': _record_id(rec), 'makale': data['pdf'], 'belge': doc, 'aday': rec,
                            'kanit_turu': rec.get('kanit_turu'), 'bilgi_turu': rec.get('bilgi_turu'),
                            'deterministik_sonuc': 'yeniden_cikarim_gerekli', 'semantik_gerekce': [x.get('neden')],
                            'klinik_iliski': None, 'kapsam': False, 'ozel_tur': None, 'kaynak_sira': None,
                            'yeniden_cikarim': True})
    return out


def _pool(group, e):
    if group == 'D_yeniden_cikarim':
        return e.get('yeniden_cikarim', False)
    if e.get('yeniden_cikarim'):
        return False
    if group == 'C_klinik_iliski_basarisiz':
        return e['klinik_iliski'] == 'basarisiz'
    if group == 'E_karmasik_tablo_kapsam':
        return e['ozel_tur'] in OZEL_TURLER or e['kapsam']
    if group == 'B_semantik_belirsiz':
        # Semantik gerekçeli metin kayıtları + hücre ilişkisi deterministik olarak belirsiz kalan tablo kayıtları.
        return (bool(e['semantik_gerekce']) or e.get('tablo_hucre') == 'belirsiz') and e['deterministik_sonuc'] != 'basarisiz'
    if group == 'A_deterministik_gecti':
        return e['deterministik_sonuc'] == 'gecti' and not e['semantik_gerekce']
    return False


def _mechanism(e):
    if e.get('yeniden_cikarim'):
        return 'yeniden_cikarim'
    if e['ozel_tur']:
        return 'ozel:' + e['ozel_tur']
    if e['kapsam']:
        return 'kapsam'
    if e.get('tablo_hucre') == 'belirsiz':
        return 'tablo_hucre'
    return ','.join(e['semantik_gerekce']) or 'yok'


def select_sample(entries, groups=GRUPLAR):
    """Gruplar sırayla doldurulur (D, C, E, B, A); bir kayıt yalnız bir gruba girer. Her grupta makaleler arasında
    sırayla (round-robin) seçilir; makale içinde en az görülen (kanıt türü, bilgi türü, mekanizma) bileşimi, eşitlikte
    sabit tohumlu sha256 sırası. Sonuç girdi sırasından bağımsız ve yeniden üretilebilir."""
    used, sample = set(), []
    for group, size in groups:
        by_article = defaultdict(list)
        for e in entries:
            if e['kimlik'] not in used and _pool(group, e):
                by_article[e['makale']].append(e)
        for lst in by_article.values():
            lst.sort(key=lambda e: _hash(e['kimlik']))
        seen = Counter()
        chosen = []
        while len(chosen) < size and any(by_article.values()):
            for article in sorted(by_article):
                lst = [e for e in by_article[article] if e['kimlik'] not in used]
                by_article[article] = lst
                if not lst:
                    continue
                best = min(lst, key=lambda e: (seen[(e['kanit_turu'], e['bilgi_turu'], _mechanism(e))],
                                               _hash(e['kimlik'])))
                used.add(best['kimlik'])
                seen[(best['kanit_turu'], best['bilgi_turu'], _mechanism(best))] += 1
                chosen.append({**best, 'grup': group, 'mekanizma': _mechanism(best)})
                if len(chosen) == size:
                    break
        sample += chosen
    return sample


def _pages(run_dir):
    from src.pilot_clinical import pdf_pages
    out = {}
    for path in sorted(Path(run_dir).glob('*.json')):
        if path.name == 'ozet.json' or path.name.endswith('.dogrulama.json'):
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        doc, pages = pdf_pages(str(ROOT / 'data' / 'pdfs' / data['pdf']))
        out[doc['document_id']] = pages
    return out


def _references(run_dir, sample):
    """YALNIZ pilot sonrası karşılaştırma: kayıt temel ölçütte (degerlendir_auto) referansla eşleşiyor mu."""
    matched = {}
    for path in sorted(Path(run_dir).glob('*.json')):
        if path.name == 'ozet.json' or path.name.endswith('.dogrulama.json'):
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        matched[data['pdf']] = {i for i, _ in data['eslesen_ciftler']}
    return {s['kimlik']: (s['kaynak_sira'] in matched[s['makale']]) if s['kaynak_sira'] is not None else None
            for s in sample}


def hazirla(args):
    from src.rag_klinik_cikarim import Cache
    run_dir = Path(args.kaynak)
    meta_model = args.model
    entries = collect(run_dir)
    sample = select_sample(entries)
    pages = _pages(run_dir)
    out = Path(args.cikti) if args.cikti else PILOT_KOKU / f'{datetime.now():%Y%m%d-%H%M%S}_hazirlik'
    out.mkdir(parents=True, exist_ok=False)
    rows, chars = [], []
    for s in sample:
        payload = S.build_payload(s['aday'], pages[s['belge']])
        size = len(S.SYSTEM) + len(json.dumps(payload, ensure_ascii=False))
        chars.append(size)
        rows.append({k: s[k] for k in ('kimlik', 'grup', 'mekanizma', 'makale', 'belge', 'kanit_turu', 'bilgi_turu',
                                       'deterministik_sonuc', 'semantik_gerekce')}
                    | {'aday': s['aday'], 'yuk_karakter': size,
                       'onbellek_anahtari': Cache.key(meta_model, S.SYSTEM, payload)})
    total = sum(chars)
    estimate = {'cagri': len(rows), 'girdi_karakter': total,
                'girdi_token_tahmini': [round(total / 4.5), round(total / 3.0)],
                'cikti_token_tahmini': [len(rows) * TAHMINI_CIKTI_TOKEN[0], len(rows) * TAHMINI_CIKTI_TOKEN[1]],
                'yontem': "karakter / 3.0–4.5 (modelin tokenizer'ı bilinmiyor); çıktı 250–500 token / çağrı varsayımı"}
    plan = {'surum': S.SURUM, 'tohum': TOHUM, 'kaynak_calistirma': str(run_dir), 'model': meta_model,
            'ornek_sayisi': len(rows), 'grup': dict(Counter(r['grup'] for r in rows)),
            'makale': dict(Counter(r['makale'] for r in rows)),
            'grup_makale': {g: dict(Counter(r['makale'] for r in rows if r['grup'] == g)) for g, _ in GRUPLAR},
            'kanit_turu': dict(Counter(r['kanit_turu'] for r in rows)),
            'bilgi_turu': dict(Counter(r['bilgi_turu'] for r in rows)),
            'mekanizma': dict(Counter(r['mekanizma'] for r in rows)),
            'havuz_buyuklugu': {g: sum(1 for e in entries if _pool(g, e)) for g, _ in GRUPLAR},
            'tahmin': estimate, 'onerilen_butce': len(rows),
            'not': 'API çağrısı yapılmadı. Referans eşleşmeleri ayrı dosyada; doğrulayıcı yüküne girmez.'}
    (out / 'ornekler.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'referans_eslesmeleri.json').write_text(json.dumps(_references(run_dir, sample), indent=1), encoding='utf-8')
    (out / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: plan[k] for k in ('ornek_sayisi', 'grup', 'makale', 'kanit_turu', 'havuz_buyuklugu',
                                           'tahmin')}, ensure_ascii=False, indent=1))
    print(f'Hazırlık: {out}')
    return out


def toplu_hazirla(args):
    """Üretim adayları (örneklemsiz). Rapor + aday dosyası bağı src.rag_dogrulama.aday_dosyasi ile denetlenir."""
    from src.pilot_clinical import pdf_pages
    from src.rag_aktarim import satir_hash
    from src.rag_dogrulama import aday_dosyasi
    from src.rag_klinik_cikarim import Cache
    files = []
    for p in args.rapor:
        path = Path(p)
        files += sorted(path.glob('*.dogrulama.json')) if path.is_dir() else [path]
    if not files:
        raise SystemExit('doğrulama raporu bulunamadı')
    out = Path(args.cikti) if args.cikti else PILOT_KOKU / f'{datetime.now():%Y%m%d-%H%M%S}_toplu'
    out.mkdir(parents=True, exist_ok=False)
    rows, sources, pdfs, skipped = [], [], {}, Counter()
    for f in files:
        report = json.loads(f.read_text(encoding='utf-8'))
        src = aday_dosyasi(f, report)
        cands = json.loads(Path(src['aday_dosyasi']).read_text(encoding='utf-8'))['adaylar']
        doc_id = report['belge_kimligi']
        doc, pages = pdf_pages(str(ROOT / 'data' / 'pdfs' / src['pdf']))
        if doc['document_id'] != doc_id:
            raise SystemExit(f'{f.name}: PDF belge kimliği rapordakiyle aynı değil')
        pdfs[doc_id] = src['pdf']
        sources.append(src)
        by_id = {satir_hash(doc_id, c): c for c in cands}
        for e in report['kayitlar']:
            if not (args.tum or e['semantik_dogrulama_gerekli']):
                skipped['semantik_gerekli_degil'] += 1
                continue
            cand = by_id[e['kayit_kimligi']]
            payload = S.build_payload(cand, pages)
            rows.append({'kimlik': e['kayit_kimligi'], 'grup': 'toplu',
                         'mekanizma': ','.join(e['semantik_gerekce']) or 'yok', 'makale': src['pdf'], 'belge': doc_id,
                         'kanit_turu': cand.get('kanit_turu'), 'bilgi_turu': cand.get('bilgi_turu'),
                         'deterministik_sonuc': e['genel_sonuc'], 'semantik_gerekce': e['semantik_gerekce'],
                         'aday': cand, 'yuk_karakter': len(S.SYSTEM) + len(json.dumps(payload, ensure_ascii=False)),
                         'onbellek_anahtari': Cache.key(args.model, S.SYSTEM, payload)})
    total = sum(r['yuk_karakter'] for r in rows)
    plan = {'tur': 'toplu', 'surum': S.SURUM, 'model': args.model, 'pdfler': pdfs, 'kaynaklar': sources,
            'secim': 'tum' if args.tum else 'semantik_dogrulama_gerekli', 'ornek_sayisi': len(rows),
            'secilmeyen': dict(skipped), 'deterministik_sonuc': dict(Counter(r['deterministik_sonuc'] for r in rows)),
            'makale': dict(Counter(r['makale'] for r in rows)),
            'tahmin': {'cagri': len(rows), 'girdi_karakter': total,
                       'girdi_token_tahmini': [round(total / 4.5), round(total / 3.0)],
                       'cikti_token_tahmini': [len(rows) * TAHMINI_CIKTI_TOKEN[0],
                                               len(rows) * TAHMINI_CIKTI_TOKEN[1]]},
            'not': 'API çağrısı yapılmadı. calistir bütçeyi kısmi uygular; kalanlar kontrol_edilmedi olarak ayrı '
                   'tutulur. LLM kararı insan onayı değildir.'}
    (out / 'ornekler.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: plan[k] for k in ('secim', 'ornek_sayisi', 'secilmeyen', 'deterministik_sonuc', 'makale',
                                           'tahmin')}, ensure_ascii=False, indent=1))
    print(f'Toplu hazırlık: {out}')
    return out


def butce_secimi(rows, cache, max_calls):
    """Toplu hazırlıkta kısmi bütçe: (çalıştırılacaklar, kontrol edilmeyenler). Önbellektekiler her zaman dahil;
    önbellekte olmayanlardan hazırlık sırasıyla ilk max_calls tanesi. Sıra sabit: tekrar çalıştırma kaldığı yerden sürer."""
    run, rest, budget = [], [], max_calls
    for r in rows:
        if cache.get(r['onbellek_anahtari']) is not None:
            run.append(r)
        elif budget > 0:
            run.append(r)
            budget -= 1
        else:
            rest.append(r)
    return run, rest


def calistir(args):
    if not args.onay:
        raise SystemExit('Gerçek API çağrısı için --onay gerekir (ayrı onay olmadan çalıştırılmaz).')
    from src.rag_klinik_cikarim import Cache, llm_client
    out = Path(args.hazirlik)
    plan = json.loads((out / 'plan.json').read_text(encoding='utf-8'))
    rows = json.loads((out / 'ornekler.json').read_text(encoding='utf-8'))
    cache = Cache(out / 'llm_onbellek.jsonl')
    rest = []
    if plan.get('tur') == 'toplu':
        from src.pilot_clinical import pdf_pages
        pages = {doc: pdf_pages(str(ROOT / 'data' / 'pdfs' / pdf))[1] for doc, pdf in plan['pdfler'].items()}
        rows, rest = butce_secimi(rows, cache, args.max_calls)
    else:
        pages = _pages(plan['kaynak_calistirma'])
    items = [{'kimlik': r['kimlik'], 'belge': r['belge'], 'grup': r['grup'], 'aday': r['aday']} for r in rows]
    # Kesin üst sınır: gerçek HTTP isteği sayısı <= max_calls × deneme (varsayılan deneme = 1: yeniden deneme yok).
    results, counter = S.run(items, pages, llm_client(), plan['model'], cache, max_calls=args.max_calls,
                             attempts=args.deneme)
    results += [{'kimlik': r['kimlik'], 'belge': r['belge'], 'grup': r['grup'], 'durum': 'kontrol_edilmedi',
                 'karar': None, 'neden': 'cagri_butcesi', 'insan_dogrulamasi_degildir': True} for r in rest]
    (out / 'sonuclar.json').write_text(json.dumps({'sonuclar': results, 'kullanim': S.usage_report(counter),
                                                   'kontrol_edilmeyen': len(rest)},
                                                  ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({**S.usage_report(counter), 'kontrol_edilmeyen': len(rest)}, ensure_ascii=False))


def degerlendir(args):
    out = Path(args.hazirlik)
    rows = {r['kimlik']: r for r in json.loads((out / 'ornekler.json').read_text(encoding='utf-8'))}
    data = json.loads((out / 'sonuclar.json').read_text(encoding='utf-8'))
    refs = json.loads((out / 'referans_eslesmeleri.json').read_text(encoding='utf-8'))
    price = {'girdi': args.fiyat_girdi, 'cikti': args.fiyat_cikti} if args.fiyat_girdi is not None else None
    report = S.metrics(data['sonuclar'], rows, refs)
    usage = dict(data.get('kullanim') or {})
    if price and 'girdi_token' in usage:
        usage['maliyet_usd'] = round(usage['girdi_token'] / 1e6 * price['girdi']
                                     + usage['cikti_token'] / 1e6 * price['cikti'], 4)
    report['kullanim'] = usage
    (out / 'degerlendirme.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=1))


# Pilotta doğrulayıcı yük artefaktından (taslak_notu.model = çıkarım modeli adının tablo bilgisi olarak gönderilmesi)
# kaynaklanan alan; gerçek klinik hata sayılmaz.
ARTEFAKT_ALANLARI = ('tablo_ve_baglam_notlari.model',)
SORUNLU_KARARLAR = ('kismen_destekleniyor', 'kaynakla_celisiyor')


def exclusion_list(results, rows):
    """(haric {kimlik: gerekçe}, artefakt nedeniyle dışlanmayanlar). Yalnız gerçek alan sorunu (artefakt dışı en az bir
    hatalı alan) taşıyan sorunlu kararlar dışlanır. LLM kararı insan onayı ya da reddi değildir."""
    haric, artefakt = {}, []
    for r in results:
        if r.get('durum') != 'tamamlandi' or r.get('karar') not in SORUNLU_KARARLAR:
            continue
        real = [f for f in r.get('hatali_alanlar', []) if f.get('alan') not in ARTEFAKT_ALANLARI]
        row = rows[r['kimlik']]
        if not real:
            artefakt.append({'kimlik': r['kimlik'], 'makale': row['makale'], 'karar': r['karar'],
                             'klinik_bilgi_en': row['aday'].get('klinik_bilgi_en')})
            continue
        fields = '; '.join(f"{f['alan']}: {f.get('sorun', '')}" for f in real)
        note = ' (zaten aday değil: yeniden çıkarım gerekli)' if row['grup'] == 'D_yeniden_cikarim' else ''
        haric[r['kimlik']] = (f"semantik pilot {S.SURUM} [{r['karar']}]{note} — {fields}"[:600])
    return haric, artefakt


def aktarim_disi(args):
    out = Path(args.hazirlik)
    rows = {r['kimlik']: r for r in json.loads((out / 'ornekler.json').read_text(encoding='utf-8'))}
    data = json.loads((out / 'sonuclar.json').read_text(encoding='utf-8'))
    haric, artefakt = exclusion_list(data['sonuclar'], rows)
    path = out / 'aktarim_disi.json'
    if path.exists():
        raise SystemExit(f'{path} zaten var; üzerine yazılmaz')
    path.write_text(json.dumps({'haric': haric, 'artefakt_nedeniyle_haric_tutulmayan': artefakt,
                                'kaynak': str(out), 'surum': S.SURUM,
                                'not': 'LLM semantik kararı insan onayı değildir; aktarım dışı tutma bir güvenlik '
                                       'önlemidir. Kayıtlar gerekçeleriyle incelemede kalır.'},
                               ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'haric': len(haric), 'artefakt': len(artefakt)}, ensure_ascii=False))
    print(f'Aktarım dışı listesi: {path}')


# Yalnız taslak_notu.model -> tahmin_modeli metaveri düzeltmesi (sinir ağı model numarası çıkarım modelinin adını
# eziyordu). Eski semantik sonuçlar DEĞİŞMEZ; eski -> yeni satir_hash eşlemesi ayrı, izlenebilir dosyadadır.
METAVERI_DUZELTMESI = 'taslak_notu.model -> taslak_notu.tahmin_modeli'


def _note(rec):
    note = rec.get('taslak_notu')
    return json.loads(note) if isinstance(note, str) else dict(note or {})


def metadata_only_change(old, new):
    """old (eski aday) ile new (yeni aday) yalnız model numarasının model -> tahmin_modeli taşınmasıyla mı farklı?"""
    if {k: v for k, v in old.items() if k != 'taslak_notu'} != {k: v for k, v in new.items() if k != 'taslak_notu'}:
        return False
    a, b = _note(old), _note(new)
    if 'tahmin_modeli' in a or 'tahmin_modeli' not in b or a.get('model') != b['tahmin_modeli']:
        return False
    if b.get('model') == b['tahmin_modeli']:
        return False
    rest = lambda n: {k: v for k, v in n.items() if k not in ('model', 'tahmin_modeli')}  # noqa: E731
    return rest(a) == rest(b)


def hash_map(results, rows, drafts):
    """drafts: {belge: (adaylar, aktarim_disi_hashleri)}. Yeni taslakta bulunmayan (D grubu dışı) pilot kayıtları için
    tekil metaveri eşleşmesi. Dönüş: (eslesme {eski: {...}}, eslesmeyen [...])."""
    from src.rag_aktarim import satir_hash
    found, missing = {}, []
    for r in results:
        old_id = r['kimlik']
        row = rows[old_id]
        if row['grup'] == 'D_yeniden_cikarim':
            continue
        cands, held = drafts[row['belge']]
        new_ids = {satir_hash(row['belge'], c): c for c in cands}
        if old_id in new_ids or old_id in held:
            continue
        hits = [h for h, c in new_ids.items() if metadata_only_change(row['aday'], c)]
        old_ok = satir_hash(row['belge'], row['aday']) == old_id
        if len(hits) != 1 or not old_ok:
            missing.append({'eski_satir_hash': old_id, 'makale': row['makale'], 'aday_sayisi': len(hits),
                            'eski_hash_yeniden_hesaplandi': old_ok})
            continue
        a, b = _note(row['aday']), _note(new_ids[hits[0]])
        found[old_id] = {'yeni_satir_hash': hits[0], 'makale': row['makale'], 'belge': row['belge'],
                         'grup': row['grup'], 'semantik_karar': r.get('karar'),
                         'degisiklik': METAVERI_DUZELTMESI,
                         'alan_farki': {'taslak_notu.model': [a.get('model'), b.get('model')],
                                        'taslak_notu.tahmin_modeli': [None, b.get('tahmin_modeli')]},
                         'diger_butun_alanlar_ayni': True, 'eski_hash_mevcut_formulle_yeniden_hesaplandi': True}
    return found, missing


def hash_esleme(args):
    out = Path(args.hazirlik)
    rows = {r['kimlik']: r for r in json.loads((out / 'ornekler.json').read_text(encoding='utf-8'))}
    results = json.loads((out / 'sonuclar.json').read_text(encoding='utf-8'))['sonuclar']
    drafts = {}
    for f in sorted(Path(args.degerlendirme).glob('*.taslak.json')):
        d = json.loads(f.read_text(encoding='utf-8'))
        drafts[d['document_id']] = (d['adaylar'], {x.get('satir_hash') for x in d['inceleme']
                                                    if x.get('asama') == 'aktarim_disi'})
    found, missing = hash_map(results, rows, drafts)
    path = out / 'hash_esleme.json'
    if path.exists():
        raise SystemExit(f'{path} zaten var; üzerine yazılmaz')
    path.write_text(json.dumps({'eslesme': found, 'eslesmeyen': missing, 'kaynak_sonuclar': str(out / 'sonuclar.json'),
                                'yeni_taslaklar': str(args.degerlendirme), 'degisiklik': METAVERI_DUZELTMESI,
                                'not': 'Eski semantik sonuçlar değiştirilmedi. Eşleme yalnız taslak_notu.model -> '
                                       'tahmin_modeli metaveri düzeltmesi için ve diğer bütün alanlar aynıyken '
                                       'tekil eşleşmeyle kurulur. LLM kararı insan onayı değildir.'},
                               ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'eslesme': len(found), 'eslesmeyen': len(missing)}, ensure_ascii=False))
    print(f'Hash eşlemesi: {path}')
    if missing:
        raise SystemExit(1)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='komut', required=True)
    h = sub.add_parser('hazirla')
    h.add_argument('--kaynak', required=True)
    h.add_argument('--model', default='gpt-6-luna')
    h.add_argument('--cikti')
    c = sub.add_parser('calistir')
    c.add_argument('--hazirlik', required=True)
    c.add_argument('--max-calls', type=int, required=True)
    c.add_argument('--onay', action='store_true')
    c.add_argument('--deneme', type=int, default=1, help='kayıt başına en fazla istek (zaman aşımı / kota yeniden denemesi)')
    x = sub.add_parser('aktarim_disi')
    x.add_argument('--hazirlik', required=True)
    e = sub.add_parser('hash-esleme', help='metaveri düzeltmesiyle hash değeri değişen pilot kayıtları için eski -> yeni eşleme')
    e.add_argument('--hazirlik', required=True)
    e.add_argument('--degerlendirme', required=True, help='yeni *.taslak.json klasörü')
    t = sub.add_parser('toplu-hazirla', help='üretim adayları için örneklemsiz hazırlık (API yok)')
    t.add_argument('--rapor', required=True, nargs='+', help='*.dogrulama.json ya da klasör')
    t.add_argument('--tum', action='store_true', help='yalnız semantik_dogrulama_gerekli değil, bütün adaylar')
    t.add_argument('--model', default='gpt-6-luna')
    t.add_argument('--cikti')
    d = sub.add_parser('degerlendir')
    d.add_argument('--hazirlik', required=True)
    d.add_argument('--fiyat-girdi', type=float)
    d.add_argument('--fiyat-cikti', type=float)
    args = p.parse_args(argv)
    return {'hazirla': hazirla, 'calistir': calistir, 'degerlendir': degerlendir,
            'aktarim_disi': aktarim_disi, 'hash-esleme': hash_esleme,
            'toplu-hazirla': toplu_hazirla}[args.komut](args)


if __name__ == '__main__':
    main()
