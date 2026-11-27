"""RAG klinik çıkarımının önbellek tabanlı, salt okunur yeniden oynatması ve değerlendirmesi.

Sıfır API / LLM / embedding çağrısı; veritabanı yok. Mevcut çalıştırma klasörleri, önbellek, taslaklar ve referans
dosyaları yalnız okunur; sonuçlar YENİ bir klasöre yazılır (var olan klasörün üzerine yazılmaz).

Yöntem:
  - Her makalenin son çalıştırmasının calistirma.json planı (seçilen sayfalar, kapsama, sınır / tablo bayrakları)
    ile görevler yeniden kurulur; retrieval / embedding gerekmez, önbellek anahtarları birebir aynıdır.
  - Önbellek yazmaya kapalı sarmalayıcıyla açılır, extract(max_calls=0): tek anahtar eksikse hiç çağrı yapılmadan durur.
  - Referans karşılaştırması bellek içinde; referans kayıtları yalnız değerlendirmede kullanılır, çıkarımın girdisi
    değildir. Ölçütler AYRI adlarla raporlanır:
      * degerlendir_auto_toleransli  (TEMEL ÖLÇÜT): evaluation/degerlendir_auto.py kuralları değiştirilmeden
        (katı SequenceMatcher >= 0.9 + aynı sayfada >= 4 kelimelik içerme; --ingilizce-olmayani-ele bellek içinde).
        Özgün temel: 2023 kayıt / 1163 eşleşme.
      * degerlendir_auto_kati: yalnız katı eşleşme.
      * degerlendir_auto_toleransli_deger_esit (yan): toleranslı çiftlerden iki tarafta da değer dolu ve farklı olanlar
        sayılmaz.
      * rag_klinik_karsilastir_kayit ve rag_klinik_karsilastir_kayit_deger_esit (yan): fact_level kuralı
        (karsilastirma.json YAZILMAZ).
  - Adaptör taslağı SQL okumadan kurulur (build_draft, mevcut kayıt listesi boş: --sql-okuma-yok ile aynı).
  - B / GA / SE / analiz bilgisinin kaynak tablo modelinden kabul kayıtlarına ve taslak notlarına taşındığı hücre bazında
    denetlenir.
  - --temel <önceki sonuç klasörü>: temel kayıtları aynı ölçütlerle yeniden değerlendirilir; temel kayıt başına sınıf
    (değişmedi / yalnız işaretlendi / düzeltildi / mükerrer / değersiz / inceleme / yeniden çıkarım), precision
    artışının ayırmadan ve gerçek düzeltmeden gelen kısmı ayrı, kaybedilen ve kazanılan referans eşleşmeleri
    gerekçeleriyle.

    python scripts/rag_onbellek_degerlendirme.py --etiket temel
    python scripts/rag_onbellek_degerlendirme.py --etiket yeni --temel evaluation/rag_onbellek_degerlendirme/<temel>
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.pilot_tablo as PT  # noqa: E402
from evaluation import degerlendir_auto as DA  # noqa: E402
from src import rag_aktarim as A  # noqa: E402
from src import rag_klinik_cikarim as K  # noqa: E402
from src.pilot_clinical import pdf_pages  # noqa: E402
from src.rag_klinik_karsilastir import PRESETS, compare_category, load_records  # noqa: E402

CIKARIM_KOKU = ROOT / 'data' / 'rag_cikarim'
SONUC_KOKU = ROOT / 'evaluation' / 'rag_onbellek_degerlendirme'
TEMEL_OLCUT = 'degerlendir_auto_toleransli'
# Ayrılan (silinmeyen) kayıtların ret aşamaları; her grup ayrı raporlanır.
MUKERRER_ASAMALARI = {'hucre_tekrari', 'tablo_tekrari', 'p_tekrari', 'sinir_tekrari', 'tablo_degisimi',
                      'bolunmemis_tekrar', 'etki_p_tekrari', 'satir_etiketi', 'katsayi_etkiye_baglandi',
                      'toplu_madde_tekrari', 'kanit_duzeyi_oneriye_baglandi'}
DEGERSIZ_ASAMALARI = {'deger_bildirilmemis'}
INCELEME_ASAMALARI = {'inceleme', 'tablo_baglami_belirsiz', 'tablo_hizalama_inceleme', 'anlam_inceleme',
                      'tablo_ozel_inceleme'}
YENIDEN_CIKARIM_ASAMALARI = {'yeniden_cikarim_gerekli'}
YAPISAL_ASAMALARI = {'tablo_ozel_degisimi'}
ASAMA_GRUBU = {**{a: 'mukerrer_ayrildi' for a in MUKERRER_ASAMALARI},
               **{a: 'degersiz_ayrildi' for a in DEGERSIZ_ASAMALARI},
               **{a: 'incelemeye_gonderildi' for a in INCELEME_ASAMALARI},
               **{a: 'yeniden_cikarim_gerekli' for a in YENIDEN_CIKARIM_ASAMALARI},
               **{a: 'deterministik_kayitla_degisti' for a in YAPISAL_ASAMALARI}}
META_ALANLARI = ('anlam_denetimi', 'tablo_iliskisi')


class OnbellekYazmaYasak(RuntimeError):
    pass


class SaltOkunurOnbellek(K.Cache):
    def put(self, key, findings):  # noqa: D401 - yazma yasak
        raise OnbellekYazmaYasak('önbellek salt okunur; eksik anahtar için API çağrısı yapılmaz')


class YasakIstemci:
    def __getattr__(self, name):
        raise RuntimeError('LLM istemcisi kullanılamaz (yalnız önbellek)')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def son_calistirma(pdf_name):
    base = CIKARIM_KOKU / Path(pdf_name).stem
    runs = sorted(p for p in base.iterdir() if p.is_dir() and (p / 'calistirma.json').exists())
    if not runs:
        raise SystemExit(f'çalıştırma yok: {base}')
    return runs[-1]


def gorevleri_kur(meta, doc, pages):
    """run() ile aynı görev sırası; sayfa planı calistirma.json'dan (embedding yok)."""
    targets = {int(p): cats for p, cats in meta['secilen_sayfalar'].items()}
    added = [int(p) for p in meta['kapsama_tamamlama']]
    task_pages = set(targets) | set(added)
    tasks = K.build_tasks(pages, targets) + K.build_tasks(pages, {p: [] for p in added})
    if meta.get('sinir'):
        tasks += K.build_boundary_tasks(doc, pages, task_pages)
    if meta.get('tablo'):
        tasks += K.build_table_tasks(pages, task_pages)
    if len(tasks) != meta['gorev_sayisi']:
        raise SystemExit(f"görev sayısı {len(tasks)} != calistirma.json {meta['gorev_sayisi']}")
    return tasks


def _flat(records):
    return [{'sayfa': r.get('sayfa'), 'kanit': r.get('kanit_cumle'),
             'klinik_bilgi': r.get('klinik_bilgi_en') or r.get('klinik_bilgi'), 'deger': r.get('deger')} for r in records]


def eslesmeler(extracted, verified, deger=False):
    """fact_level ile aynı açgözlü eşleşme; (çıkarım indeksi, referans indeksi) çiftleri. deger=True: yan ölçüt,
    ikisinde de değer doluysa normalize değer eşitliği de aranır (rag_klinik_karsilastir measurements_by_group kuralı)."""
    flat_x, flat_y = _flat(extracted), _flat(verified)
    from src.rag_klinik_karsilastir import _norm, matches
    used, pairs = set(), []
    for i, x in enumerate(flat_x):
        for j, y in enumerate(flat_y):
            if deger and x.get('deger') and y.get('deger') and _norm(x['deger']) != _norm(y['deger']):
                continue
            if j not in used and matches(x, y, 'kayit'):
                used.add(j)
                pairs.append((i, j))
                break
    return pairs


def _ingilizce_indeksler(records):
    """degerlendir_auto --ingilizce-olmayani-ele ile aynı süzgeç (bellek içi; kayıtlar değişmez)."""
    from src.rag_son_filtre import is_non_english
    return [i for i, k in enumerate(records)
            if not is_non_english(str(k.get('kanit_cumle') or k.get('klinik_bilgi_en') or ''))]


def temel_ciftler(extracted, verified):
    """TEMEL ÖLÇÜT (degerlendir_auto, kurallar değiştirilmeden): ([(çıkarım indeksi, referans indeksi, 'kati'|'ek')],
    değerlendirilen çıkarım sayısı)."""
    keep = _ingilizce_indeksler(extracted)
    sub = [extracted[i] for i in keep]
    ciftler = DA.eslestir(sub, verified)
    kati = [c for c in ciftler if c[2] >= DA.ESLESME_ESIGI]
    ek = DA.toleransli_eslestir(sub, verified, {i for i, _, _ in kati}, {j for _, j, _ in kati})
    return [(keep[i], j, 'kati') for i, j, _ in kati] + [(keep[i], j, 'ek') for i, j, _ in ek], len(sub)


def _deger_farkli(x, y):
    from src.rag_klinik_karsilastir import _norm
    return bool(x.get('deger')) and bool(y.get('deger')) and _norm(x['deger']) != _norm(y['deger'])


def _olcu(eslesen, cikarilan, dogrulanmis):
    return {'eslesen': eslesen, 'cikarilan': cikarilan, 'dogrulanmis': dogrulanmis,
            'precision': _oran(eslesen, cikarilan), 'recall': _oran(eslesen, dogrulanmis)}


def olcutler(extracted, verified):
    """(temel ölçüt çiftleri [(i, j)], {ölçüt adı: ölçü}). Her yöntem ayrı adla."""
    pairs, n = temel_ciftler(extracted, verified)
    n_v = len(verified)
    rk = eslesmeler(extracted, verified)
    rk_d = eslesmeler(extracted, verified, deger=True)
    out = {
        TEMEL_OLCUT: _olcu(len(pairs), n, n_v),
        'degerlendir_auto_kati': _olcu(sum(1 for p in pairs if p[2] == 'kati'), n, n_v),
        'degerlendir_auto_toleransli_deger_esit': _olcu(
            sum(1 for i, j, _ in pairs if not _deger_farkli(extracted[i], verified[j])), n, n_v),
        'rag_klinik_karsilastir_kayit': _olcu(len(rk), len(extracted), n_v),
        'rag_klinik_karsilastir_kayit_deger_esit': _olcu(len(rk_d), len(extracted), n_v),
    }
    return [(i, j) for i, j, _ in pairs], out


def bilgi_koruma(records, rejected, drafts, pages):
    """Kaynak tablo modelindeki (pilot_tablo) B / GA / SE / analiz bilgisi hücre bazında: kabul kaydında / taslak
    notunda var mı, yalnız ret raporunda mı, hiç çıkarılmamış mı."""
    from src.rag_tablo_hizalama import _c, _dash
    tables = PT.parse_tables(pages)
    by_cell = {}
    for r in records:
        m = r.get('tablo_modeli') or {}
        if m.get('durum') in A.MODEL_STATES:
            by_cell.setdefault((m['tablo_no'], m['satir_no'], m.get('sutun')), []).append(r)
    rej = [x['kayit'] for x in rejected if isinstance(x.get('kayit'), dict)]

    def on_page(recs, page, texts):
        return [r for r in recs if r.get('sayfa') == page
                and texts & {_c(r.get('deger')), _c((r.get('tablo_modeli') or {}).get('ham_deger'))}]
    sonuc, eksik, model_yok = Counter(), [], 0
    for t in tables:
        if t.get('elle_inceleme_satirlari'):
            model_yok += 1
            continue
        for row in t['satirlar']:
            for prev in PT.sql_preview({'kanit_cumle': '', 'bilgi_turu': None, 'baglam': None}, t, row):
                note = json.loads(prev['taslak_notu'])
                col = note.get('sutun')
                checks = [(ad, alan, val) for ad, alan, val in (
                    ('GA', 'ci95', prev.get('ci95')), ('SE', 'se', note.get('se')),
                    ('analiz', 'analiz', note.get('analiz')), ('B', 'regresyon_katsayisi', note.get('regresyon_katsayisi')))
                    if val]
                if not checks:
                    continue
                owners = by_cell.get((t['tablo_no'], row['satir_no'], col), [])
                texts = {x for x in (_c(row['sutun_degerleri'].get(col, '') if col else ''), _c(prev.get('deger'))) if x}
                for ad, alan, val in checks:
                    def has(r):
                        v = r.get('ci95') if alan == 'ci95' else (r.get('tablo_modeli') or {}).get(alan)
                        return _c(_dash(v)) == _c(_dash(val))
                    if any(has(r) for r in owners):
                        durum = 'kabul_kaydinda'
                    elif ad == 'B' and on_page(records, t['sayfa'], {_c(val)}):
                        durum = 'kabul_ayri_B_kaydi'
                    elif on_page(records, t['sayfa'], texts):
                        durum = 'kabul_kaydi_var_alan_eksik'
                    elif on_page(rej, t['sayfa'], texts):
                        durum = 'yalniz_ret_raporunda'
                    else:
                        durum = 'hucre_cikarilmamis'
                    sonuc[f'{ad}:{durum}'] += 1
                    if durum != 'kabul_kaydinda':
                        eksik.append({'bilgi': ad, 'durum': durum, 'tablo_no': t['tablo_no'], 'satir': row['degisken'],
                                      'sutun': col, 'beklenen': val})

    def note_of(c):
        n = c.get('taslak_notu')
        return json.loads(n) if isinstance(n, str) else (n or {})
    taslak = {}
    for alan in ('analiz', 'se', 'regresyon_katsayisi'):
        taslak[alan] = {'kabul': sum(1 for r in records if (r.get('tablo_modeli') or {}).get(alan)),
                        'taslak': sum(1 for c in drafts if note_of(c).get(alan))}
    taslak['ci95'] = {'kabul': sum(1 for r in records if r.get('ci95')), 'taslak': sum(1 for c in drafts if c.get('ci95'))}
    for alan in ('kaynak_calisma', 'alt_grup', 'kol', 'oneri_sinifi', 'kanit_duzeyi'):
        taslak[alan] = {'kabul': sum(1 for r in records if (r.get('tablo_modeli') or {}).get(alan)),
                        'taslak': sum(1 for c in drafts if note_of(c).get(alan))}
    taslak['tablo_iliskisi.kanit_duzeyi'] = {
        'kabul': sum(1 for r in records if (r.get('tablo_iliskisi') or {}).get('kanit_duzeyi')),
        'taslak': sum(1 for c in drafts if (note_of(c).get('tablo_iliskisi') or {}).get('kanit_duzeyi'))}
    return {'hucre': dict(sonuc), 'eksik': eksik, 'modelsiz_tablo': model_yok, 'taslaga_tasinan': taslak}


def ozel_tablo_raporu(records, rejected, pages):
    """Özel tablo aşamasının ayrı raporu: tablo bazında kaynak hücre kapsamı, kurtarılan / yeni / yapısal düzeltilen /
    mükerrer / hâlâ incelemede kayıtlar ve bağımsız kaynak ilişkisi denetimi."""
    from src import rag_tablo_ozel as O
    from src.rag_anlam_denetimi import missing_marker
    dets = [r for r in records if (r.get('tablo_modeli') or {}).get('durum') == O.STATE]
    replaced = [x for x in rejected if x.get('asama') == O.REPLACED_STAGE]
    audit = Counter()
    failures = []
    for r in dets:
        problems = O.audit_record(r, pages)
        audit['gecti' if not problems else 'kaldi'] += 1
        if problems:
            failures.append({'kayit': {k: r.get(k) for k in ('sayfa', 'klinik_bilgi_en', 'deger', 'sonuc_olcutu_tr')},
                             'neden': problems})
    tables = []
    for table, name, built, holds in O.build(pages):
        rows = O._rows_of(table)
        cells = covered = missing = features = 0
        table_dets = [r for r in dets if r['tablo_modeli'].get('tablo_no') == table['tablo_no']
                      and r['sayfa'] in {s['sayfa'] for s in table['segmentler']}]
        note_texts = set()
        for r in table_dets:
            m = r['tablo_modeli']
            for key in ('oneri_sinifi', 'kanit_duzeyi', 'oneri_on_kosulu', 'alt_bolum', 'tahmin_modeli', 'gercek_sinif'):
                if m.get(key):
                    note_texts.add(O._c(m[key]))
            for v in ((m.get('kaynak_calisma') or {}).get('ozellikler') or {}).values():
                for x in (v if isinstance(v, list) else [v]):
                    note_texts.add(O._c(x))
        for seg in table['segmentler']:
            _, _, label, first = O._header(seg)
            for i in range(first, seg['end']):
                row = PT.split_cells(seg['lines'][i])
                if PT.is_footnote_row(row):
                    continue                                   # dipnot satırı veri değil (tablo notunda)
                for j, cell in enumerate(row[1:], 1):
                    if not PT.clean(cell):
                        continue
                    if O.is_missing(cell, table['sozluk']) or missing_marker(cell):
                        missing += 1
                        continue
                    if re.fullmatch(r'[\W_]*', PT.clean(cell)):
                        continue
                    cells += 1
                    on_row = [r for r in dets if r['sayfa'] == seg['sayfa']
                              and i in r['tablo_modeli'].get('satir_indeksleri', [])]
                    hit = any(r['tablo_modeli'].get('sutun_no') == j or (r.get('deger') and r['deger'] in cell)
                              or r['klinik_bilgi_en'] in cell for r in on_row)
                    # Kayıt değil, kaydın tablo notunda tutulan hücre: çalışma özelliği (ülke, dönem, kontrol), öneri
                    # sınıfı / kanıt düzeyi / ön koşul / alt başlık, model no, gerçek sınıf etiketi.
                    feature = not hit and O._c(cell) in note_texts
                    covered += hit
                    features += feature
        acc_rows = [r for r in dets if (r['sayfa'], O._c(r['kanit_cumle'].split('\n')[0])) in rows]
        held = [x for x in rejected if x.get('asama') == O.HOLD_STAGE and x.get('tablo_no') == table['tablo_no']
                and x.get('sayfa') in {s['sayfa'] for s in table['segmentler']}]
        tables.append({'tablo_no': table['tablo_no'], 'sayfalar': [s['sayfa'] for s in table['segmentler']],
                       'yapi': name, 'literatur': table['literatur'], 'deger_hucresi': cells,
                       'kayitla_kapsanan_hucre': covered, 'calisma_notunda_kapsanan_hucre': features,
                       'kapsanmayan_hucre': cells - covered - features, 'bildirilmemis_hucre': missing,
                       'kabul_edilen_deterministik_kayit': len(acc_rows), 'inceleme': len(held),
                       'inceleme_nedenleri': dict(Counter(x['neden'].split(' (')[0][:80] for x in held))})
    prev = Counter(x.get('onceki_asama') for x in replaced)
    return {
        'deterministik_kabul': len(dets),
        'onceki_llm_kaydi_olmayan_yeni': sum(1 for r in dets if not r['tablo_modeli'].get('onceki_llm_kaydi')),
        'llm_kaydinin_yerine_gecen': sum(1 for r in dets if r['tablo_modeli'].get('onceki_llm_kaydi')),
        'degistirilen_llm_kayitlarinin_onceki_asamasi': dict(prev),
        'incelemeden_kurtarilan_llm_kaydi': sum(v for k, v in prev.items() if k in INCELEME_ASAMALARI),
        'kabulden_yapisal_duzeltilen_llm_kaydi': prev.get('kabul', 0),
        'toplu_madde_tekrari': sum(1 for x in rejected if x.get('asama') == O.BLOB_STAGE),
        'kanit_duzeyi_oneriye_baglandi': sum(1 for x in rejected if x.get('asama') == O.LOE_STAGE),
        'tablo_iliskisi_eklenen_kayit': sum(1 for r in records if r.get('tablo_iliskisi')),
        'kaynak_iliskisi_denetimi': dict(audit), 'denetimden_kalanlar': failures[:50],
        'tablolar': tables,
    }


def kimlik(rec):
    """Kayıt farkı için kimlik: ham kanıt + ham bilgi + sayfa (yapılandırılmış alanlar hariç)."""
    parts = tuple((p.get('sayfa'), p.get('metin')) for p in rec.get('kanit_parcalari') or ())
    return (rec.get('sayfa'), rec.get('kanit_cumle'), parts, rec.get('klinik_bilgi_en'), rec.get('bilgi_turu'),
            rec.get('baglam'))


def makale_degerlendir(pdf_name, haric=None):
    run_dir = son_calistirma(pdf_name)
    meta = json.loads((run_dir / 'calistirma.json').read_text(encoding='utf-8'))
    doc, pages = pdf_pages(str(ROOT / 'data' / 'pdfs' / meta['pdf']))
    if doc['document_id'] != meta['document_id'] or doc['processing_version'] != meta['processing_version']:
        raise SystemExit(f'{pdf_name}: PDF / işleme sürümü çalıştırmadakiyle aynı değil')
    tasks = gorevleri_kur(meta, doc, pages)
    cache_path = CIKARIM_KOKU / Path(meta['pdf']).stem / 'llm_onbellek.jsonl'
    cache = SaltOkunurOnbellek(cache_path)
    valid, rejected, calls = K.extract(pages, tasks, YasakIstemci(), meta['model'], cache=cache, max_calls=0)
    if calls:
        raise SystemExit(f'{pdf_name}: API çağrısı sayısı {calls} (0 olmalı)')
    grouped = K.grouped_rows(valid)
    records = [{**r, 'kategoriler': K.categories_of(r, grouped)} for r in valid]
    stored = json.loads((run_dir / 'kayitlar.json').read_text(encoding='utf-8'))
    verified = [r for p in PRESETS[pdf_name] for r in load_records(ROOT / p) if r.get('review_status') == 'verified']
    pairs, metrics = olcutler(valid, verified)
    held = [r['kayit'] for r in rejected if r.get('asama') in INCELEME_ASAMALARI and isinstance(r.get('kayit'), dict)]
    reextract = [r['kayit'] for r in rejected
                 if r.get('asama') in YENIDEN_CIKARIM_ASAMALARI and isinstance(r.get('kayit'), dict)]
    draft = A.build_draft(records, pages, meta, [], haric=haric)
    drafts = [A.serializable(r) for r in draft['adaylar']]
    from src import rag_dogrulama as V
    verification = V.verify(records, draft, pages, doc['document_id'])
    routed_x, routed_y = K.route(valid, pdf_name), K.route(verified, pdf_name)
    per_cat = {c: compare_category(routed_x[c], routed_y[c], c) for c in K.CATEGORIES}
    return {
        'pdf': pdf_name, 'calistirma': str(run_dir.relative_to(ROOT)), 'gorev': len(tasks), 'api_cagrisi': calls,
        'kayitlar_json_ile_birebir': records == stored,
        'temel_olcut': TEMEL_OLCUT, 'cikarilan': len(valid), 'dogrulanmis': len(verified), 'eslesen': len(pairs),
        'olcutler': metrics, 'eslesen_ciftler': pairs, 'inceleme_kayitlari': held,
        'yeniden_cikarim_kayitlari': reextract,
        'ret_asamalari': dict(Counter(r.get('asama') for r in rejected)),
        'kategori': {c: {k: per_cat[c][k] for k in ('cikarilan', 'dogrulanmis', 'eslesen')} for c in K.CATEGORIES},
        'bilgi_koruma': bilgi_koruma(records, rejected, drafts, pages),
        'ozel_tablo': ozel_tablo_raporu(records, rejected, pages),
        'olcutler_ozel_haric': olcutler([r for r in valid if (r.get('tablo_modeli') or {}).get('durum') != 'ozel'],
                                        verified)[1],
        'olcutler_yalniz_ozel': olcutler([r for r in valid if (r.get('tablo_modeli') or {}).get('durum') == 'ozel'],
                                         verified)[1],
        'anlam_denetimi': {
            'isaretli_kayit': sum(1 for r in records if r.get('anlam_denetimi')),
            'alanlar': dict(Counter(k for r in records for k in (r.get('anlam_denetimi') or {}))),
            'uyarilar': dict(Counter(w.split(' (')[0] for r in records
                                     for w in (r.get('anlam_denetimi') or {}).get('uyarilar', []))),
            'bilesenler': dict(Counter(
                f"{b.get('bicim')} | {b.get('aralik_turu') or b.get('yayilim_turu') or b.get('parantez_turu') or ''} | "
                f"{b.get('etiket_kaynagi') or 'etiketsiz'}"
                for r in records for b in [(r.get('anlam_denetimi') or {}).get('deger_bilesenleri')] if b))},
        'dogrulama_ozeti': verification['ozet'], '_dogrulama': verification,
        '_taslak': {'pdf': meta['pdf'], 'document_id': doc['document_id'], 'calistirma': str(run_dir.relative_to(ROOT)),
                    'sayilar': draft['sayilar'], 'adaylar': drafts, 'inceleme': draft['inceleme'],
                    'kesin_tekrar': draft['kesin_tekrar']},
        'taslak_sayilari': draft['sayilar'], 'kayitlar': records, 'reddedilenler': rejected,
        'taslak_adaylari': drafts, 'taslak_inceleme': draft['inceleme'],
        '_verified': verified,
    }


def _oran(a, b):
    return round(a / b, 4) if b else None


def _strip(r):
    return {k: v for k, v in r.items() if k != 'kategoriler'}


def _strip_meta(r):
    return {k: v for k, v in _strip(r).items() if k not in META_ALANLARI and k != 'otomatik_duzeltme'}


def kayit_siniflari(t_records, n_records, n_rejected):
    """Temel kabul kayıtlarının her biri için yeni durum; dönen liste temel kayıt sırasındadır: (sınıf, ayrıntı).

    Eşleme sıraya bağlı DEĞİLDİR; bütün kayıtlar için ayrı geçişler, her geçişte eşleşen kayıt tüketilir:
      1. yeni kabul listesinde tam eşitlik            -> degismedi
      2. yalnız meta veri farkı (anlam_denetimi, ...)  -> yalniz_isaretlendi
      3. ret raporunda tam eşitlik                      -> aşamasının grubu (mükerrer / inceleme / ...)
      4. kimlik düzeyi (sayfa + kanıt + bilgi + tür + bağlam) yalnız TEKİL ise: önce yeni kabul (duzeltildi), sonra ret
         raporu. Aynı kimlikte birden çok aday kalırsa zorla atanmaz -> belirsiz_eslesme (ör. aynı satırın Avantajlar /
         Sınırlılıklar kayıtları çaprazlanmaz).
    Hiçbirine uymayan -> yok."""
    out = [None] * len(t_records)
    n_left = list(range(len(n_records)))
    rej = [k for k, x in enumerate(n_rejected) if isinstance(x.get('kayit'), dict) and x.get('asama') in ASAMA_GRUBU]
    rej_left = list(rej)

    def consume(pool, test):
        for i, r in enumerate(t_records):
            if out[i] is not None:
                continue
            hit = next((k for k in pool if test(r, k)), None)
            if hit is not None:
                pool.remove(hit)
                out[i] = hit
                yield i, hit
    for i, k in list(consume(n_left, lambda r, k: _strip(n_records[k]) == _strip(r))):
        out[i] = ('degismedi', None)
    for i, k in list(consume(n_left, lambda r, k: _strip_meta(n_records[k]) == _strip_meta(r))):
        out[i] = ('yalniz_isaretlendi', sorted({**(n_records[k].get('anlam_denetimi') or {}),
                                               **({'tablo_iliskisi': 1} if n_records[k].get('tablo_iliskisi') else {})}))
    for i, k in list(consume(rej_left, lambda r, k: _strip(n_rejected[k]['kayit']) == _strip(r))):
        x = n_rejected[k]
        out[i] = (ASAMA_GRUBU[x['asama']], {'asama': x['asama'], 'neden': x.get('neden')})
    # 4. Kimlik düzeyi: yalnız tekil eşleşme.
    remaining = [i for i, v in enumerate(out) if v is None]
    by_key_t = Counter(kimlik(t_records[i]) for i in remaining)
    for i in remaining:
        key = kimlik(t_records[i])
        cands = [k for k in n_left if kimlik(n_records[k]) == key]
        rcands = [k for k in rej_left if kimlik(n_rejected[k]['kayit']) == key]
        if by_key_t[key] == 1 and len(cands) == 1 and not rcands:
            k = cands[0]
            n_left.remove(k)
            changed = sorted(f for f in set(_strip(n_records[k])) | set(_strip(t_records[i])) if f not in META_ALANLARI
                             and f != 'otomatik_duzeltme' and _strip(n_records[k]).get(f) != _strip(t_records[i]).get(f))
            out[i] = ('duzeltildi', changed)
        elif by_key_t[key] == 1 and len(rcands) == 1 and not cands:
            k = rcands[0]
            rej_left.remove(k)
            x = n_rejected[k]
            out[i] = (ASAMA_GRUBU[x['asama']], {'asama': x['asama'], 'neden': x.get('neden')})
        elif cands or rcands:
            out[i] = ('belirsiz_eslesme', {'yeni_aday': len(cands), 'ret_aday': len(rcands),
                                           'ayni_kimlikli_temel_kayit': by_key_t[key]})
        else:
            out[i] = ('yok', None)
    return out


def fark(temel, yeni):
    """Temel -> yeni: temel kayıtları AYNI ölçütlerle bellekte yeniden değerlendirilir (saklanan sayılara güvenilmez)."""
    out = {}
    for pdf, n in yeni.items():
        t = temel[pdf]
        verified = n['_verified']
        t_pairs, t_metrics = olcutler(t['kayitlar'], verified)
        classes = kayit_siniflari(t['kayitlar'], n['kayitlar'], n['reddedilenler'])
        # Precision ayrıştırması: yalnız AYIRMA uygulansaydı (temelden yeni hattın ayırdığı kayıtlar çıkarılır, kalanlar
        # temel haliyle) -> P_ayirma. Ayırma katkısı = P_ayirma - P_temel; düzeltme katkısı = P_yeni - P_ayirma.
        separated = {i for i, (c, _) in enumerate(classes) if c in ('mukerrer_ayrildi', 'degersiz_ayrildi',
                                                                     'incelemeye_gonderildi', 'yeniden_cikarim_gerekli',
                                                                     'deterministik_kayitla_degisti')}
        only_sep = [r for i, r in enumerate(t['kayitlar']) if i not in separated]
        _, sep_metrics = olcutler(only_sep, verified)
        _, t_wo_special = olcutler([r for r in t['kayitlar'] if (r.get('tablo_modeli') or {}).get('durum') != 'ozel'],
                                   verified)
        t_matched = {j: i for i, j in t_pairs}
        n_matched = {j: i for i, j in n['eslesen_ciftler']}
        kayip, kazanc = [], []
        for j, i in sorted(t_matched.items()):
            if j in n_matched:
                continue
            cls, detail = classes[i]
            kayip.append({'referans': {k: verified[j].get(k) for k in ('sayfa', 'klinik_bilgi_en', 'deger', 'hasta_grubu')},
                          'temel_kayit': {k: t['kayitlar'][i].get(k) for k in ('sayfa', 'klinik_bilgi_en', 'deger',
                                                                               'bilgi_turu', 'baglam')},
                          'temel_kaydin_yeni_durumu': cls, 'ayrinti': detail})
        for j, i in sorted(n_matched.items()):
            if j not in t_matched:
                kazanc.append({'referans': {k: verified[j].get(k) for k in ('sayfa', 'klinik_bilgi_en', 'deger')},
                               'yeni_kayit': {k: n['kayitlar'][i].get(k) for k in ('sayfa', 'klinik_bilgi_en', 'deger')}})
        out[pdf] = {
            'temel_kayit': len(t['kayitlar']), 'yeni_kayit': len(n['kayitlar']),
            'temel_olcutler': t_metrics, 'yalniz_ayirma_olcutler': sep_metrics, 'yeni_olcutler': n['olcutler'],
            'temel_ozel_haric_olcutler': t_wo_special, 'yeni_ozel_haric_olcutler': n['olcutler_ozel_haric'],
            'temel_kayit_siniflari': dict(Counter(c for c, _ in classes)),
            'duzeltilen_alanlar': dict(Counter(f for c, d in classes if c == 'duzeltildi' for f in d)),
            'isaret_alanlari': dict(Counter(f for c, d in classes if c == 'yalniz_isaretlendi' for f in d)),
            'ayrilan_asamalar': dict(Counter(d['asama'] for c, d in classes if isinstance(d, dict) and 'asama' in d)),
            'temelde_olmayan_yeni_kayit': len(n['kayitlar']) - sum(
                1 for c, _ in classes if c in ('degismedi', 'yalniz_isaretlendi', 'duzeltildi')),
            'kaybedilen_eslesme': kayip, 'kazanilan_eslesme': kazanc,
            'kayitlar_temelle_birebir': [_strip(r) for r in t['kayitlar']] == [_strip(r) for r in n['kayitlar']],
        }
    return out


def _toplam_olcu(per_article, name):
    e = sum(m[name]['eslesen'] for m in per_article)
    c = sum(m[name]['cikarilan'] for m in per_article)
    d = sum(m[name]['dogrulanmis'] for m in per_article)
    return _olcu(e, c, d)


def ozet(results):
    names = list(next(iter(results.values()))['olcutler'])
    tot = {'cikarilan': sum(r['cikarilan'] for r in results.values()),
           'dogrulanmis': sum(r['dogrulanmis'] for r in results.values()),
           'temel_olcut': TEMEL_OLCUT,
           'olcutler': {name: _toplam_olcu([r['olcutler'] for r in results.values()], name) for name in names}}
    base = tot['olcutler'][TEMEL_OLCUT]
    koruma = Counter()
    for r in results.values():
        koruma.update(r['bilgi_koruma']['hucre'])
    tot.update({'eslesen': base['eslesen'], 'duyarlilik': base['recall'], 'kesinlik': base['precision'],
                'api_cagrisi': sum(r['api_cagrisi'] for r in results.values()),
                'kayitlar_json_ile_birebir': all(r['kayitlar_json_ile_birebir'] for r in results.values()),
                'inceleme_kaydi': sum(len(r['inceleme_kayitlari']) for r in results.values()),
                'yeniden_cikarim_kaydi': sum(len(r['yeniden_cikarim_kayitlari']) for r in results.values()),
                'bilgi_koruma': dict(koruma)})
    oz = Counter()
    for r in results.values():
        o = r['ozel_tablo']
        oz.update({k: o[k] for k in ('deterministik_kabul', 'onceki_llm_kaydi_olmayan_yeni', 'llm_kaydinin_yerine_gecen',
                                     'incelemeden_kurtarilan_llm_kaydi', 'kabulden_yapisal_duzeltilen_llm_kaydi',
                                     'toplu_madde_tekrari', 'kanit_duzeyi_oneriye_baglandi',
                                     'tablo_iliskisi_eklenen_kayit')})
        oz.update({f"denetim_{k}": v for k, v in o['kaynak_iliskisi_denetimi'].items()})
    tot['ozel_tablo'] = dict(oz)
    dg = {'genel_sonuc': Counter(), 'semantik_dogrulama_gerekli': 0, 'semantik_gerekce': Counter(), 'kontrol_bazinda': {}}
    for r in results.values():
        o = r['dogrulama_ozeti']
        dg['genel_sonuc'].update(o['genel_sonuc'])
        dg['semantik_dogrulama_gerekli'] += o['semantik_dogrulama_gerekli']
        dg['semantik_gerekce'].update(o['semantik_gerekce'])
        for code, counts in o['kontrol_bazinda'].items():
            dg['kontrol_bazinda'].setdefault(code, Counter()).update(counts)
    tot['dogrulama'] = {'genel_sonuc': dict(dg['genel_sonuc']), 'semantik_dogrulama_gerekli': dg['semantik_dogrulama_gerekli'],
                        'semantik_gerekce': dict(dg['semantik_gerekce']),
                        'kontrol_bazinda': {k: dict(v) for k, v in sorted(dg['kontrol_bazinda'].items())}}
    tot['taslak_koruma'] = {}
    for r in results.values():
        for alan, v in r['bilgi_koruma']['taslaga_tasinan'].items():
            acc = tot['taslak_koruma'].setdefault(alan, {'kabul': 0, 'taslak': 0})
            acc['kabul'] += v['kabul']
            acc['taslak'] += v['taslak']
    tot['olcutler_ozel_haric'] = {n: _toplam_olcu([r['olcutler_ozel_haric'] for r in results.values()], n)
                                  for n in tot['olcutler']}
    tot['olcutler_yalniz_ozel'] = {n: _toplam_olcu([r['olcutler_yalniz_ozel'] for r in results.values()], n)
                                   for n in tot['olcutler']}
    return tot


def korunan_dosyalar():
    paths = sorted(CIKARIM_KOKU.rglob('*')) + sorted((ROOT / 'data' / 'review').rglob('*.json'))
    return {str(p.relative_to(ROOT)): sha256_file(p) for p in paths if p.is_file()}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--etiket', required=True)
    parser.add_argument('--temel', help='karşılaştırılacak önceki sonuç klasörü')
    parser.add_argument('--cikti', help='varsayılan: evaluation/rag_onbellek_degerlendirme/<zaman>_<etiket>')
    parser.add_argument('--aktarim-disi', help='aktarım dışı listesi (rag_semantik_pilot aktarim_disi çıktısı)')
    args = parser.parse_args(argv)
    out = Path(args.cikti) if args.cikti else SONUC_KOKU / f"{datetime.now():%Y%m%d-%H%M%S}_{args.etiket}"
    out.mkdir(parents=True, exist_ok=False)
    before = korunan_dosyalar()
    haric = A.load_haric(args.aktarim_disi)
    results = {pdf: makale_degerlendir(pdf, haric) for pdf in PRESETS}
    after = korunan_dosyalar()
    if before != after:
        raise SystemExit('korunan dosyalardan biri değişti!')
    summary = {'etiket': args.etiket, 'zaman': datetime.now().astimezone().isoformat(timespec='seconds'),
               'aktarim_disi_listesi': args.aktarim_disi, 'aktarim_disi_kayit': len(haric or {}),
               'toplam': ozet(results), 'korunan_dosya_sayisi': len(before), 'korunan_dosyalar_degismedi': True,
               'makaleler': {pdf: {k: v for k, v in r.items() if k not in ('kayitlar', 'reddedilenler', 'eslesen_ciftler',
                                                                          'inceleme_kayitlari', 'taslak_adaylari',
                                                                          'yeniden_cikarim_kayitlari', '_dogrulama',
                                                                          '_taslak',
                                                                          'taslak_inceleme', '_verified')}
                             for pdf, r in results.items()}}
    if args.temel:
        temel = {}
        tdir = Path(args.temel)
        for pdf in PRESETS:
            temel[pdf] = json.loads((tdir / f'{Path(pdf).stem}.json').read_text(encoding='utf-8'))
        diff = fark(temel, results)
        names = list(summary['toplam']['olcutler'])
        tot = {key: {name: _toplam_olcu([d[key] for d in diff.values()], name) for name in names}
               for key in ('temel_olcutler', 'yalniz_ayirma_olcutler', 'yeni_olcutler', 'temel_ozel_haric_olcutler',
                           'yeni_ozel_haric_olcutler')}
        ayristirma = {}
        for name in (TEMEL_OLCUT, 'degerlendir_auto_toleransli_deger_esit'):
            p_t, p_s, p_n, p_tw, p_nw = (tot[k][name]['precision'] for k in (
                'temel_olcutler', 'yalniz_ayirma_olcutler', 'yeni_olcutler', 'temel_ozel_haric_olcutler',
                'yeni_ozel_haric_olcutler'))
            special = round((p_n - p_nw) - (p_t - p_tw), 4)      # özel tablo kayıtlarının etkisindeki DEĞİŞİM
            separation = round(p_s - p_t, 4)
            ayristirma[name] = {'temel': p_t, 'yalniz_ayirma': p_s, 'yeni': p_n, 'temel_ozel_tablo_haric': p_tw,
                                'yeni_ozel_tablo_haric': p_nw, 'ayirma_katkisi': separation,
                                'ozel_tablo_katkisi': special,
                                'duzeltme_katkisi': round(p_n - p_t - separation - special, 4)}
        summary['temel_ile_fark'] = {
            'temel_klasoru': str(tdir), 'toplam': tot, 'precision_ayristirmasi': ayristirma,
            'not': 'yalniz_ayirma: temel kayıtlarından yeni hattın ayırdığı (mükerrer / değersiz / inceleme / yeniden '
                   'çıkarım / deterministik kayıtla değişen) kayıtlar çıkarılmış, kalanlar TEMEL haliyle '
                   'değerlendirilmiştir. ayirma_katkisi = yalniz_ayirma - temel; ozel_tablo_katkisi = (yeni - yeni '
                   'özel hariç) - (temel - temel özel hariç); duzeltme_katkisi = yeni - temel - ayırma - özel tablo.',
            'temel_kayit_siniflari': dict(sum((Counter(d['temel_kayit_siniflari']) for d in diff.values()), Counter())),
            'kaybedilen_eslesme': sum(len(d['kaybedilen_eslesme']) for d in diff.values()),
            'kazanilan_eslesme': sum(len(d['kazanilan_eslesme']) for d in diff.values()),
            'kayitlar_temelle_birebir': all(d['kayitlar_temelle_birebir'] for d in diff.values()),
            'makaleler': diff}
    for pdf, r in results.items():
        (out / f'{Path(pdf).stem}.json').write_text(json.dumps({k: v for k, v in r.items()
                                                                if k not in ('_verified', '_dogrulama', '_taslak')},
                                                               ensure_ascii=False, indent=1), encoding='utf-8')
        (out / f'{Path(pdf).stem}.dogrulama.json').write_text(json.dumps(r['_dogrulama'], ensure_ascii=False, indent=1),
                                                              encoding='utf-8')
        # İzole SQL entegrasyon testi için adaptör taslağı biçiminde (rag_aktarim taslak çıktısıyla aynı alanlar).
        (out / f'{Path(pdf).stem}.taslak.json').write_text(json.dumps(r['_taslak'], ensure_ascii=False, indent=1,
                                                                      default=str), encoding='utf-8')
    (out / 'ozet.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    t = summary['toplam']
    print(f"{args.etiket}: çıkarım {t['cikarilan']} | API {t['api_cagrisi']} | inceleme {t['inceleme_kaydi']} | "
          f"yeniden çıkarım {t['yeniden_cikarim_kaydi']}")
    for name, m in t['olcutler'].items():
        mark = ' (TEMEL)' if name == TEMEL_OLCUT else ''
        print(f"  {name}{mark}: eşleşme {m['eslesen']} / {m['dogrulanmis']} | recall {m['recall']} | "
              f"precision {m['precision']}")
    for pdf, r in results.items():
        print(f"  {pdf[:45]:45} çıkarım {r['cikarilan']:4} eşleşme {r['eslesen']:4}/{r['dogrulanmis']:4} "
              f"taslak {r['taslak_sayilari']['pending_aday']}")
    print(f"  bilgi koruma: {t['bilgi_koruma']}")
    print(f"  özel tablo: {t['ozel_tablo']}")
    print(f"  taslak koruma: {t['taslak_koruma']}")
    print(f"  doğrulama genel: {t['dogrulama']['genel_sonuc']} | semantik gerekli: {t['dogrulama']['semantik_dogrulama_gerekli']} "
          f"{t['dogrulama']['semantik_gerekce']}")
    if 'temel_ile_fark' in summary:
        f = summary['temel_ile_fark']
        print(f"  temel kayıt sınıfları: {f['temel_kayit_siniflari']} | kayıtlar temelle birebir: "
              f"{f['kayitlar_temelle_birebir']}")
        print(f"  precision ayrıştırması: {f['precision_ayristirmasi']}")
        print(f"  kaybedilen eşleşme: {f['kaybedilen_eslesme']} | kazanılan eşleşme: {f['kazanilan_eslesme']}")
    print(f'Sonuç: {out}')
    return out


if __name__ == '__main__':
    main()
