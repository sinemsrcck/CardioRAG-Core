"""
src/pilot_tablo.py testi: pilot tablo kayıtlarının tablo/sütun başlıklarıyla deterministik eşlenmesi.
Veritabanı gerektirmez ve kullanmaz; data/pdfs/TK-cardiovascular36-2-1.pdf gerekli.

Çalıştırma:
    python -m evaluation.test_pilot_tablo

Kontroller:
- Gerçek PDF: Table 1 (s.4) ve Table 2 (s.5) bulunuyor; çok satırlı Table 2 başlıkları birleşiyor,
  iki "p value" sütunu ayrı anahtarlarla tutuluyor; alt bölümler doğru.
- Satırlar: HbA1c, Female gender (<br>), Warfarin (<br>'li satır), Table 2 HCR (univariate/multivariate OR, CI, p).
- Başlık bozulması ("Dhi f" / "emograpcs eatures") raporlanıyor, ad uydurulmuyor; sentetik bozuk başlık da.
- Ham kanıt sayfa metninde birebir; birim ayırma; eşleşmeyen ve belirsiz kayıt.
- Zenginleştirme: pilot_findings_v2.json varsa tüm tablo kayıtları eşleşiyor, bilgi_turu/baglam değişmiyor.
- SQL önizleme: grup/analiz başına satır; küme alanları src.clinical_validation kümelerinde;
  validate_row'un sayı ve tablo-kanıt kontrolleri (saf fonksiyonlar) geçiyor.
- mudahale_turu yalnız ilaç satırlarında ("arka plan tedavisi"); diğerleri boş ve raporda listeli.
- Veritabanı: gerçek modüller yüklenir (mysql-connector-python gerekli), ama bağlantı fonksiyonları
  çağrılınca hata veren sarmalayıcılarla değiştirilir; hiç çağrılmamalı.
"""

import json
import sys
from pathlib import Path

import mysql.connector

import src.clinical_candidates as clinical_candidates
import src.database as database
from src.clinical_validation import (
    ETKI_TURU, KANIT_TURU, MUDAHALE_TURU, NUMBER_CHECKED, OUTCOME_TYPE, YON, evidence_in_text, numbers_in)
import src.pilot_tablo as pt
from src.pilot_clinical import pdf_pages

# Gerçek modüller yüklü; yalnızca bağlantı açan fonksiyonlar kilitlenir. Bir çağrı olursa test düşer.
DB_CALLS = []


def _no_db(*args, **kwargs):
    DB_CALLS.append(args)
    raise AssertionError('Veritabanı çağrılmamalı')


database.get_connection = _no_db
clinical_candidates.get_connection = _no_db  # 'from src.database import get_connection' ile bağlanmış ad
mysql.connector.connect = _no_db

PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')
results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def row_by(table, name):
    return next(r for r in table['satirlar'] if r['degisken'] == name)


def note(row):
    return json.loads(row['taslak_notu'])


def main():
    if not PDF.exists():
        print(f'PDF bulunamadı: {PDF}')
        sys.exit(1)
    doc, pages = pdf_pages(PDF)
    tables = pt.parse_tables(pages)
    t1 = next(t for t in tables if t['tablo_no'] == 1)
    t2 = next(t for t in tables if t['tablo_no'] == 2)

    # --- Tablolar ve başlıklar ---
    check('tablo sayısı / sayfalar', [(1, 4), (2, 5)], [(t['tablo_no'], t['sayfa']) for t in tables])
    check('T1 başlık', True, t1['tablo_basligi'].startswith('TABLE 1: Comparison of patients'))
    check('T1 sütunlar', ['Variable', 'Ischemic event (n=372)', 'No ischemic event (n=853)', 'p value'],
          [c['anahtar'] for c in t1['sutunlar']])
    check('T1 grup n', [372, 853], [c['n'] for c in t1['sutunlar'] if c['rol'] == 'grup'])
    check('T1 alt bölümler', ['Laboratory findings', 'Echocardiographic parameters', 'Medications'], t1['alt_bolumler'])
    check('T1 veri satırı', 51, len(t1['satirlar']))
    check('T2 çok satırlı başlık', ['Variable', 'Univariate analysis / Odds ratio (95% CI)', 'p value [Univariate analysis]',
                                    'Multivariate analysis / Odds ratio (95% CI)', 'p value [Multivariate analysis]'],
          [c['anahtar'] for c in t2['sutunlar']])
    check('T2 veri satırı', 7, len(t2['satirlar']))
    check('T2 başlıkta harf kopması raporlandı', True,
          any(s['tur'] == 'baslikta_olasi_harf_kopmasi' and s['parca'] == 'u nivariate' for s in t2['sorunlar']))

    # --- Satırlar ---
    hba1c = row_by(t1, 'HbA1c')
    check('HbA1c hücreleri', {'Ischemic event (n=372)': '7.83±1.2', 'No ischemic event (n=853)': '6.23±1.2', 'p value': '<0.001'},
          hba1c['sutun_degerleri'])
    check('HbA1c alt bölüm', 'Laboratory findings', hba1c['alt_bolum'])
    female = row_by(t1, 'Female gender')
    check('Female ham <br> korunuyor', True, '<br>' in female['ham'])
    check('Female temiz <br>siz', True, '<br>' not in female['temiz'] and '189 (50.8)' in female['temiz'])
    check('Female birim / değer', ('n (%)', '189 (50.8)', '0.289'),
          (female['unit'], female['sutun_degerleri']['Ischemic event (n=372)'], female['sutun_degerleri']['p value']))
    warfarin = row_by(t1, 'Warfarin')
    check('Warfarin ham', '|<br>Warfarin|<br>21 (5.64)|<br>42 (4.92)|0.781|', warfarin['ham'])
    check('Warfarin hücreleri', {'Ischemic event (n=372)': '21 (5.64)', 'No ischemic event (n=853)': '42 (4.92)', 'p value': '0.781'},
          warfarin['sutun_degerleri'])
    check('Warfarin alt bölüm', 'Medications', warfarin['alt_bolum'])
    hcr2 = pt.row_analyses(t2, row_by(t2, 'HCR'))
    check('T2 HCR analizleri',
          [('univariate', 'OR', '3.124', '2.213-4.310', '<0.001'), ('multivariate', 'OR', '3.216', '2.334-4.365', '<0.001')],
          [(a['analiz'], a['etki_turu'], a['deger'], a['ci95'], a['p']) for a in hcr2])
    age2 = pt.row_analyses(t2, row_by(t2, 'Age'))
    check('T2 Age iki p ayrı', ['0.002', '0.679'], [a['p'] for a in age2])
    check('birim: Glucose / E/e\' / WBC / BMI', [('Glucose', 'mg/dL'), ("E/e'", None), ('WBC count', 'x10^3/µl'), ('BMI', 'kg/m^2')],
          [(r['degisken'], r['unit']) for r in (row_by(t1, 'Glucose'), row_by(t1, "E/e'"), row_by(t1, 'WBC count'), row_by(t1, 'BMI'))])
    lines = {p: set(line.strip() for line in text.splitlines()) for p, text in pages.items()}
    check('ham kanıt sayfada birebir', True, all(r['ham'] in lines[t['sayfa']] for t in tables for r in t['satirlar']))

    # --- Başlık bozulması (gerçek) ---
    age = t1['satirlar'][0]
    kinds = {s['tur'] for s in t1['sorunlar']}
    check('T1 bozuk başlık raporlandı', {'sutun_basliginda_artik_parca', 'satira_yapismis_parca', 'ilk_bolum_basligi_okunamadi'},
          kinds & {'sutun_basliginda_artik_parca', 'satira_yapismis_parca', 'ilk_bolum_basligi_okunamadi'})
    check('Age: ad/bölüm uydurulmadı', ('Age', 'years', None), (age['degisken'], age['unit'], age['alt_bolum']))
    dumped = json.dumps(tables, ensure_ascii=False)
    check('"Demographic" uydurulmadı', False, 'Demographic' in dumped)
    check('parçalar raporda', True, 'Dhi f' in dumped and 'emograpcs eatures' in dumped)

    # --- Sentetik: bozuk başlık, başlıksız tablo ---
    synthetic = {9: 'metin\n|**Variable**<br>Xy z|**Group A (n=10)**|**Group B (n=12)**|**p value**|\n|---|---|---|---|\n'
                    '|ab cd<br>Weight (kg)|5±1|6±1|0.1|\n|Drugs||||\n|Aspirin|3 (30)|4 (33.3)|0.9|\n'}
    st = pt.parse_tables(synthetic)[0]
    st_kinds = {s['tur'] for s in st['sorunlar']}
    check('sentetik: sorunlar', {'tablo_basligi_yok', 'sutun_basliginda_artik_parca', 'satira_yapismis_parca', 'ilk_bolum_basligi_okunamadi'},
          st_kinds)
    check('sentetik: satırlar', [('Weight', 'kg', None), ('Aspirin', None, 'Drugs')],
          [(r['degisken'], r['unit'], r['alt_bolum']) for r in st['satirlar']])

    # --- Eşleme: eşleşmeyen / belirsiz ---
    fake = [{'v2_sira': 'x1', 'kanit_turu': 'tablo', 'sayfa': 4, 'kanit_cumle': '|Troponin (ng/L)|12±3|10±2|0.04|'},
            {'v2_sira': 'x2', 'kanit_turu': 'tablo', 'sayfa': 5, 'kanit_cumle': '|HbA1c|7.83±1.2|6.23±1.2|<0.001|'},
            {'v2_sira': 'x3', 'kanit_turu': 'tablo', 'sayfa': 4, 'kanit_cumle': 'n (%)'},
            {'v2_sira': 'x4', 'kanit_turu': 'tablo', 'sayfa': 4, 'kanit_cumle': '|HbA1c|7.83±1.2|6.23±1.2|<0.001|'}]
    fr = pt.enrich(fake, pages, doc['document_id'])
    check('eşleşmeyen / yanlış sayfa / belirsiz / eşleşen', ['eslesmedi', 'eslesmedi', 'belirsiz', 'eslesti'],
          [r['eslesme'] for r in fr['kayitlar']])
    check('eşleşmeyen için SQL önizleme yok', 2, len(fr['sql_onizleme']))

    # --- Zenginleştirme: gerçek pilot girdisi (varsa) ---
    real = pt.DEFAULT_JSON.exists()
    if real:
        records, _ = pt.load_records(pt.DEFAULT_JSON)
    else:
        records = [{'v2_sira': i, 'kanit_turu': 'tablo', 'sayfa': t['sayfa'], 'kanit_cumle': r['ham'], 'bilgi_turu': 'olcum',
                    'baglam': 'study_finding'} for i, (t, r) in enumerate(((t, r) for t in tables for r in t['satirlar']), 1)]
    before = {r['v2_sira']: (r.get('bilgi_turu'), r.get('baglam')) for r in records}
    result = pt.enrich(records, pages, doc['document_id'])
    summary = pt.summarize(result, 'test')
    check(f'girdi ({"pilot_findings_v2.json" if real else "sentetik"}): tablo kaydı', 58, summary['tablo_kaydi'])
    check('eşleşen / eşleşmeyen / belirsiz', (58, 0, 0), (summary['eslesen'], summary['eslesmeyen'], summary['belirsiz']))
    check('pilot kaydında yok', 0, summary['pilot_kaydinda_yok'])
    check('bilgi_turu/baglam değişmedi', True,
          all((r.get('bilgi_turu'), r.get('baglam')) == before[r['v2_sira']] for r in result['kayitlar']))
    if real:
        tani = [r for r in result['kayitlar'] if r.get('bilgi_turu') == 'tani']
        check('tani kayıtları zenginleşti', ['CAD', 'Diabetes mellitus', 'Hypertension', 'Hyperlipidemia', 'HF', 'Mild mitral stenosis'],
              [r['tablo']['degisken'] for r in tani])
    enriched_hba1c = next(r for r in result['kayitlar'] if r['eslesme'] == 'eslesti' and r['tablo']['degisken'] == 'HbA1c'
                          and r['tablo']['tablo_no'] == 1)
    check('zengin kayıt alanları', (1, 'Laboratory findings', 'HbA1c', 'ortalama±SS'),
          (enriched_hba1c['tablo']['tablo_no'], enriched_hba1c['tablo']['alt_bolum'], enriched_hba1c['tablo']['degisken'],
           enriched_hba1c['tablo']['deger_bicimi']))

    # --- SQL önizleme ---
    preview = result['sql_onizleme']
    check('SQL önizleme satırı (51x2 + 7x2)', 116, len(preview))
    hb = [r for r in preview if r['sonuc_olcutu_tr'] == 'HbA1c' and note(r)['tablo_no'] == 1]
    check('HbA1c -> 2 grup satırı',
          [('Ischemic event', 'No ischemic event', '7.83±1.2', '<0.001', 372, 'diger'),
           ('No ischemic event', 'Ischemic event', '6.23±1.2', '<0.001', 853, 'diger')],
          [(r['hasta_grubu'], r['comparator'], r['deger'], r['p'], note(r)['grup_n'], r['etki_turu']) for r in hb])
    check('HbA1c deger_bicimi taslak_notu', 'ortalama±SS', note(hb[0])['deger_bicimi'])
    fg = next(r for r in preview if r['sonuc_olcutu_tr'] == 'Female gender')
    check('Female gender etki_turu / unit', ('oran / yuzde', 'n (%)', 'n (%)'), (fg['etki_turu'], fg['unit'], note(fg)['deger_bicimi']))
    gl = next(r for r in preview if r['sonuc_olcutu_tr'] == 'Glucose')
    check('Glucose unit / mudahale_turu / outcome_type', ('mg/dL', None, 'çalışma popülasyonu'),
          (gl['unit'], gl['mudahale_turu'], gl['outcome_type']))
    lvef = next(r for r in preview if r['sonuc_olcutu_tr'] == 'LvEF')
    check('LvEF (eko) mudahale_turu boş + neden', (None, True),
          (lvef['mudahale_turu'], 'ekip kararı bekliyor' in note(lvef)['mudahale_turu_bos_nedeni']))
    medication = {(n['tablo_no'], n['satir_no']) for n in map(note, preview) if n['alt_bolum'] == 'Medications'}
    check('mudahale_turu yalnız ilaç satırlarında', True,
          all(bool(r['mudahale_turu']) == ((note(r)['tablo_no'], note(r)['satir_no']) in medication) for r in preview))
    check('"tanı testi" hiç atanmadı', 0, sum(r['mudahale_turu'] == 'tanı testi' for r in preview))
    check('mudahale_turu dolu / boş (13x2 ilaç)', (26, 90), (sum(bool(r['mudahale_turu']) for r in preview), summary['sql_mudahale_turu_bos']))
    check('raporda bekleyen satır listesi (38 T1 + 7 T2)', (45, 90),
          (len(summary['sql_mudahale_turu_bos_satirlar']), sum(p['sql_satiri'] for p in summary['sql_mudahale_turu_bos_satirlar'])))
    check('Table 2 outcome_type boş', True, all(r['outcome_type'] is None for r in preview if note(r)['tablo_no'] == 2))
    wf = [r for r in preview if r['mudahale_en'] == 'Warfarin']
    check('ilaç satırı', [('arka plan tedavisi', 'uygulama oranı', None, 'kullanım oranı', 'oran / yuzde')] * 2,
          [(r['mudahale_turu'], r['outcome_type'], r['doz'], r['sonuc_olcutu_tr'], r['etki_turu']) for r in wf])
    hc = [r for r in preview if r['sonuc_olcutu_tr'] == 'HCR' and note(r)['tablo_no'] == 2]
    check('T2 HCR -> univariate + multivariate',
          [('OR', '3.124', '2.213-4.310', '<0.001', 'univariate', None), ('OR', '3.216', '2.334-4.365', '<0.001', 'multivariate', None)],
          [(r['etki_turu'], r['deger'], r['ci95'], r['p'], note(r)['analiz'], r['hasta_grubu']) for r in hc])
    check('hasta_sayisi hep NULL (n başlıkta)', True, all(r['hasta_sayisi'] is None for r in preview))

    # --- Küme kontrolü (src.clinical_validation) ---
    constants = {pt.ETKI_N_PCT, pt.ETKI_OTHER} | {code for _, code in pt.EFFECT_TYPES}
    check('modül sabitleri ETKI_TURU içinde', True, constants <= ETKI_TURU)
    check('modül sabitleri MUDAHALE/OUTCOME içinde', True,
          {pt.MUDAHALE_ILAC} <= MUDAHALE_TURU and {pt.OUTCOME_KULLANIM, pt.OUTCOME_POPULASYON} <= OUTCOME_TYPE)
    bad = [(i, name, row.get(name)) for i, row in enumerate(preview)
           for name, allowed in (('etki_turu', ETKI_TURU), ('mudahale_turu', MUDAHALE_TURU), ('outcome_type', OUTCOME_TYPE),
                                 ('yon', YON), ('kanit_turu', KANIT_TURU))
           if row.get(name) and row.get(name) not in allowed]
    check('önizleme küme alanları izinli', [], bad)
    empty_mudahale = sum(not r['mudahale_turu'] for r in preview)
    check('mudahale_turu boş = rapordaki sayı', summary['sql_mudahale_turu_bos'], empty_mudahale)
    extra = [(i, name, sorted(numbers_in(row.get(name)) - numbers_in(row['kanit_cumle']))) for i, row in enumerate(preview)
             for name in NUMBER_CHECKED if numbers_in(row.get(name)) - numbers_in(row['kanit_cumle'])]
    check('sayı kontrolü (kanitta_olmayan_sayi yok)', [], extra[:3])
    check('tablo kanıtı evidence_in_text', True,
          all(evidence_in_text(r['kanit_cumle'], 'tablo', '', pages[r['kanit_sayfa']]) for r in preview))

    # --- Veritabanı ---
    check('gerçek src.database yüklendi (yer tutucu yok)', True, hasattr(database, '__file__') and 'mysql.connector' in sys.modules)
    check('veritabanı çağrılmadı', 0, len(DB_CALLS))

    width = max(len(r[0]) for r in results)
    print(f"{'Kontrol':<{width}}  {'Beklenen':<30}  {'Gerçek':<30}  Sonuç")
    print('-' * (width + 74))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:30]:<30}  {act[:30]:<30}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        for name, exp, act, _ in failed:
            print(f'HATA {name}\n  beklenen: {exp}\n  gerçek:   {act}')
        print(f'BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.')
        sys.exit(1)
    print(f'BAŞARILI: {len(results)} kontrolün hepsi geçti.')


if __name__ == '__main__':
    main()
