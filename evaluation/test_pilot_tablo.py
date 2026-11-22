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
- Zenginleştirme: pilot_tablo.DEFAULT_JSON (v3.2, yoksa v2) varsa tüm tablo kayıtları eşleşiyor, bilgi_turu/baglam değişmiyor.
- SQL önizleme: grup/analiz başına satır; küme alanları src.clinical_validation kümelerinde;
  validate_row'un sayı ve tablo-kanıt kontrolleri (saf fonksiyonlar) geçiyor.
- mudahale_turu yalnız ilaç satırlarında ("arka plan tedavisi"); diğerleri boş (aktarım yolu zorunlu tutmuyor).
- Aktarım (import_records girdisi): 116 kayıt, izinli alanlar, review_status yok; validate_records
  grup/analiz satırlarını tekrar saymıyor, gerçek tekrarı "tekrar" hatasıyla yakalıyor.
- Pilot çıktısı main'deki kodla birebir (sabit parmak izleri): parse_tables satırları ve sütunları,
  sql_onizleme, aktarım, table_info ve satir_hash listesi (import_records kimliğiyle).
- Genel tablo kuralları (sentetik tablo, PDF'siz): Roma rakamlı başlık, dipnot satırı, ara başlık / biçim
  satırı (bölüm kapsamını bitirir), başlığı boş etiket sütunu ve birleşik hücre adı, <br> alt satırları,
  test / post-hoc işaretleri, "(A)" etiketi, "±SE", karmaşık tablo -> elle inceleme, boş değerli kayıt yok.
- Troponin PDF'i (who-J-Coll-…pdf, varsa): 3 tablo; Table III elle inceleme, kayıt yok; boş / çöp kayıt yok.
  Ekip kararları: Total sütunu (sutun_rolu=toplam, comparator / p yok, n başlıktan), p sütunundaki OR'ler etki
  kaydı, tekrar eden ad ("Non risc") bölümüyle, Table II tanısal performans (test x ölçü, P -> AUC, ikili AUC
  karşılaştırması), "(SE.)" birim değil. Bilinen: validate_records'un tekrar anahtarında mudahale_en yok (3 kayıt).
  Grup içi test satırları (P value for intra groups, Pairwise A-B / A-C / B-C; dipnot Friedman / Dunn): comparator /
  p yok, karsilastirma_turu = 'grup_ici'; comparator'lar insan doğrulamasıyla (zehra_troponin_tablo_dogrulanmis.json,
  varsa, yalnız okunur) aynı.
- Grup içi kuralı (sentetik): ad ve dipnot işareti yolları; Mann-Whitney işareti, p değeri olmayan "Pairwise" ve
  p sütunu dolu "Post hoc" satırı grup içi sayılmaz; gruplar arası satırlar değişmez.
- Girdi: findings zorunlu; başka belgenin findings'i (içerik ya da document_id) reddedilir; başka belgenin
  çıktısının üzerine --overwrite ile bile yazılmaz.
- Temiz satır adı (row_name): istatistik etiketi (n (%), mean ± SD, median (IQR) …) atılır, klinik birim
  ve <sup> ham kalır, <br> öncesi parça ve kenar ** atılır; ad kanıtta birebir. Aktarımda tablo
  kaydının klinik_bilgi_en'i temiz ad; zehra_dogrulanmis_v3_3.json'daki 116 tablo kaydıyla birebir
  aynı, diğer alanlar (taslak_notu.inceleme_notu hariç) da aynı.
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
from src.pilot_clinical import INPUT_FIELDS, STRUCTURED_FIELDS, pdf_pages, validate_records

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


V33 = Path('data/review/pilot/zehra_dogrulanmis_v3_3.json')
TROPONIN_PDF = Path('data/pdfs/who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf')
# İnsan doğrulaması (yalnız okunur): grup içi satırlarda comparator boşaltılmış hali.
VERIFIED_TROPONIN = Path('data/review/troponin/zehra_troponin_tablo_dogrulanmis.json')
# main'deki (genel tablo kurallarından önceki) kodun pilot çıktısının parmak izleri. Pilot çıktısı değişirse
# bilerek güncellenir; bu değişiklik pilot kayıtlarını (ve satir_hash'leri) değiştirmemeli.
PILOT_FINGERPRINTS = {
    'tablolar_satirlar': '4d9adca7f57d73b0d419d9d53667b4edd55f0719222e429c3da1dd41ca621a11',
    'tablolar_sutunlar': 'fc211d87280dae2ec8178b0fd074b526c46d538d900e40227b7da8869771603f',
    'sql_onizleme': '863dec4099fe5f50805de6064a72e15e9a805bba56ff84188f0bfedf3bb802fd',
    'aktarim': '5e17ce2d040132866a41a3ec5b25c3faa105a166d1e7fa716134a1a4603b1d7d',
    'table_info': '21a3bd4e0fd4ff42945b2481e95bae12b3a841f9c372808a850abc35a522baa2',
    'satir_hash': '4012000e26ef16a33513cf1de06e9815448bbba387b529a72675a40a1826282f',
}
ROW_KEYS = ('satir_no', 'sayfa', 'ham', 'temiz', 'degisken_ham', 'ad', 'degisken', 'unit', 'alt_bolum', 'sutun_degerleri')
SYNTHETIC_PAGE = """**Table II: Synthetic general rules.**

|||**Group A (n=10)**|**Group B (n=12)**|**P**|
|---|---|---|---|---|
|||n (%)|n (%)||
|Age||50 (40-60)|55 (45-65)|0.100<br>u|
|Sex<br>Female<br>Male||4 (40.0)<br>6 (60.0)|5 (41.7)<br>7 (58.3)|0.900<br>p|
|Drug section|||||
|Aspirin||3 (30.0)|4 (33.3)<br>II|0.700|
|||Median (Q1-Q3)|Median (Q1-Q3)||
|Score (A)||3 (1-5)|4 (2-6)|<0.001<br>u|
|AUC±SE.||0.70 (0.6-0.8)|0.75 (0.65-0.85)|0.040|
|Test value<br>f||<0.001|0.020||
||X-Y|0.010|0.030||
|Pairwise check|X-Z|0.020|0.040||
||Y-Z|0.030||7.8 (2.4-25.1)<br>or|
|_a footnote,_<br>_u Mann Whitney_|_more_|_x_|_y_|_z_|

**Table 3: Complex synthetic.**

||**Variable**|**Imp**|**Class**|
|---|---|---|---|
|**Model**|**Name**|**Importance**|**Predicted**|
|1|Alpha|50%|Absent|
||Beta|40%|Present|
"""


def fingerprint(obj):
    import hashlib
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def satir_hash(item, document_id):
    """src.pilot_clinical.import_records'un satir_hash kimliğiyle aynı (yazmadan, yalnız karşılaştırma için)."""
    import hashlib
    payload = {'document_id': document_id, 'bilgi_turu': item['bilgi_turu'], 'baglam': item['baglam'],
               'klinik_bilgi': item.get('klinik_bilgi'), 'klinik_bilgi_en': item.get('klinik_bilgi_en'),
               'kanit_sayfa': item['sayfa'], 'kanit_cumle': item['kanit_cumle']}
    identity = [payload[k] for k in ('document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en',
                                     'kanit_sayfa', 'kanit_cumle')]
    structured = {k: (item[k] if isinstance(item[k], str) else json.dumps(item[k], ensure_ascii=False))
                  for k in STRUCTURED_FIELDS if item.get(k) is not None}
    if structured:
        identity.append(structured)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def check_pilot_unchanged(result, document_id):
    """Genel tablo kuralları pilot çıktısını değiştirmez: main'deki kodun parmak izleriyle birebir."""
    import hashlib
    rows = [[t['tablo_no']] + [r[k] for k in ROW_KEYS] for t in result['tablolar'] for r in t['satirlar']]
    cols = [[t['tablo_no'], t['tablo_basligi'], t['sutunlar'], t['alt_bolumler'], [s['tur'] for s in t['sorunlar']]]
            for t in result['tablolar']]
    hashes = sorted(satir_hash(r, document_id) for r in result['aktarim'])
    got = {'tablolar_satirlar': fingerprint(rows), 'tablolar_sutunlar': fingerprint(cols),
           'sql_onizleme': fingerprint(result['sql_onizleme']), 'aktarim': fingerprint(result['aktarim']),
           'table_info': fingerprint([r.get('tablo') for r in result['kayitlar']]),
           'satir_hash': hashlib.sha256(''.join(hashes).encode()).hexdigest()}
    check('pilot: main ile birebir (satırlar, sütunlar, sql_onizleme, aktarım, table_info, satir_hash)',
          PILOT_FINGERPRINTS, got)
    check('pilot: 116 ayrı satir_hash', 116, len(set(hashes)))
    check('pilot: atlanan satır / üretilmeyen kayıt / elle inceleme yok', ([], [], 0),
          ([s for t in result['tablolar'] for s in t['atlanan_satirlar']], result['atlanan_kayitlar'],
           sum(r['eslesme'] == 'elle_inceleme' for r in result['kayitlar'])))
    check('pilot: yeni taslak_notu alanı yok', set(),
          {k for r in result['aktarim'] for k in json.loads(r['taslak_notu'])}
          & {'posthoc_isareti', 'p_test_isareti', 'test_isareti', 'bicim_etiketi', 'etiket', 'sutun_etiketi'})


def check_general_rules():
    """Genel tablo kuralları, sentetik sayfa (PDF / makale gerekmez)."""
    check('roma rakamı', [1, 2, 3, 4, 9, 14, None], [pt.roman_to_int(x) for x in ('I', 'II', 'III', 'IV', 'IX', 'XIV', 'IIZ')])
    check('split_unit: (A) etiket, ±SE etiket', [('hs Trop I 0h (A)', 'ng/l'), ('AUC', None), ('Glucose', 'mg/dL')],
          [pt.split_unit('hs Trop I  0h  ng/l  (A)'), pt.split_unit('AUC±SE.'), pt.split_unit('Glucose (mg/dL)')])
    check('row_name: ±SE / ± SD / (SE.) atılır', ['AUC', 'Weight', 'BMI kg/m<sup>2</sup>', 'Odds Ratio', 'Disease'],
          [pt.row_name('AUC±SE.'), pt.row_name('Weight ± SD'), pt.row_name('BMI kg/m<sup>2</sup>'),
           pt.row_name('Odds Ratio (SE.)'), pt.row_name('Disease')])
    check('split_unit: "(SE.)" birim değil', [('Odds Ratio', None), ('Glucose', 'mg/dL')],
          [pt.split_unit('Odds Ratio (SE.)'), pt.split_unit('Glucose (mg/dL)')])
    check('group_title: (n=…) her yerden, biçim ifadesi ayrı; eski başlıklar aynı',
          [('Total', 114, 'Median (Q1-Q3)'), ('Ischemic event', 372, None), ('All patients', 50, 'n (%)'),
           ('Group A', 10, None)],
          [pt.group_title('Total (n=114) / Median (Q1-Q3)'), pt.group_title('Ischemic event (n=372)'),
           pt.group_title('All patients (n=50) / n (%)'), pt.group_title('Group A (n = 10)')])
    check('Total adı genel kural (Total / Overall / All patients / Whole cohort; grup adı değil)',
          [True, True, True, True, True, False, False],
          [bool(pt.TOTAL_GROUP.match(x)) for x in ('Total', 'OVERALL', 'All patients', 'Whole cohort', 'Toplam',
                                                    'Total cholesterol', 'ACS Absent')])
    tables = pt.parse_tables({7: SYNTHETIC_PAGE})
    check('sentetik: Roma rakamlı başlık sayıya çevrildi', [(2, 'Table II: Synthetic general rules.'), (3, None)],
          [(t['tablo_no'], t['tablo_basligi'] if t['tablo_no'] == 2 else None) for t in tables])
    t2 = tables[0]
    roles = [(c['rol'], c['baslik']) for c in t2['sutunlar']]
    check('sentetik: başlığı boş etiket sütunu grup değil', ('etiket', ''), roles[1])
    rows = {r['degisken']: r for r in t2['satirlar']}
    check('sentetik: satır adları (alt satırlar, birleşik hücre adı, işaretsiz ad)',
          ['Age', 'Female', 'Male', 'Aspirin', 'Score (A)', 'AUC', 'Test value', 'Pairwise check X-Y',
           'Pairwise check X-Z', 'Pairwise check Y-Z'], [r['degisken'] for r in t2['satirlar']])
    check('sentetik: Sex alt satırları bölüm "Sex", değerler ayrı',
          [('Sex', {'Group A (n=10)': '4 (40.0)', 'Group B (n=12)': '5 (41.7)', 'P': '0.900'}),
           ('Sex', {'Group A (n=10)': '6 (60.0)', 'Group B (n=12)': '7 (58.3)', 'P': '0.900'})],
          [(rows[n]['alt_bolum'], {k: v for k, v in rows[n]['sutun_degerleri'].items() if k != ' [sütun 2]'})
           for n in ('Female', 'Male')])
    check('sentetik: biçim satırı bölüm kapsamını bitirir', ('Drug section', None),
          (rows['Aspirin']['alt_bolum'], rows['Score (A)']['alt_bolum']))
    check('sentetik: işaretler değerden ayrıldı (p test, post-hoc, ad)',
          ('0.100', {'P': 'u'}, '4 (33.3)', {'Group B (n=12)': 'II'}, 'f'),
          (rows['Age']['sutun_degerleri']['P'], rows['Age'].get('isaretler'), rows['Aspirin']['sutun_degerleri']['Group B (n=12)'],
           rows['Aspirin'].get('isaretler'), rows['Test value'].get('ad_isareti')))
    check('sentetik: p hücresinde OR (or) -> satırın etki ölçüsü (ekip kararı 8), p değil',
          ('7.8 (2.4-25.1)', {'P': {'sutun': 'P', 'etki_turu': 'OR', 'isaret': 'or', 'deger': '7.8', 'ci95': '2.4-25.1'}}),
          (rows['Pairwise check Y-Z']['sutun_degerleri']['P'], rows['Pairwise check Y-Z'].get('p_etki')))
    check('sentetik: atlanan satırlar', ['bicim_satiri', 'bicim_satiri', 'dipnot'],
          sorted(s['tur'] for s in t2['atlanan_satirlar']))
    check('sentetik: dipnot satırı veri değil, dipnot metni saklandı', (True, False),
          (bool(t2.get('dipnot')) and 'Mann Whitney' in t2['dipnot'][0], any('footnote' in r['ham'] for r in t2['satirlar'])))
    records = [{'v2_sira': i, 'kanit_turu': 'tablo', 'sayfa': 7, 'kanit_cumle': ham, 'bilgi_turu': 'olcum',
                'baglam': 'study_finding', 'klinik_bilgi_en': ham}
               for i, ham in enumerate(dict.fromkeys(r['ham'] for r in t2['satirlar']), 1)]
    records.append({'v2_sira': 99, 'kanit_turu': 'tablo', 'sayfa': 7, 'kanit_cumle': '|1|Alpha|50%|Absent|',
                    'bilgi_turu': 'olcum', 'baglam': 'study_finding', 'klinik_bilgi_en': 'Alpha'})
    result = pt.enrich(records, {7: SYNTHETIC_PAGE})
    transfer = result['aktarim']
    check('sentetik: alt satırlı PDF satırı tek kayıttan iki satır (Female, Male)', ['eslesti'] * (len(records) - 1),
          [r['eslesme'] for r in result['kayitlar'][:-1]])
    check('sentetik: karmaşık tablonun kaydı -> elle_inceleme, kayıt yok', ('elle_inceleme', 0),
          (result['kayitlar'][-1]['eslesme'], sum(json.loads(r['taslak_notu'])['tablo_no'] == 3 for r in transfer)))
    check('sentetik: boş değerli / boş adlı / boş gruplu kayıt yok', [],
          [r for r in transfer if not r.get('deger') or not r.get('klinik_bilgi_en') or r.get('hasta_grubu') == ''
           or r.get('p') == ''])
    check('sentetik: boş değerli grup kaydı üretilmedi, raporlandı', [('Pairwise check Y-Z', 'Group B (n=12)')],
          [(a['degisken'], a['sutun']) for a in result['atlanan_kayitlar']])
    check('sentetik: kayıt sayısı (10 satır x 2 grup - 1 boş + 1 p sütunu etkisi)', 20, len(transfer))
    effect = [r for r in transfer if r.get('etki_turu') == 'OR']
    check('sentetik: p sütunu etkisi ayrı kayıt (OR, deger, ci95 ayrı; p yok; grup kaydında p boş)',
          ([('Y-Z', 'OR', '7.8', '2.4-25.1', None, None, 'p_sutunu')], [None]),
          ([(r['klinik_bilgi_en'], r['etki_turu'], r['deger'], r['ci95'], r.get('p'), r.get('hasta_grubu'),
             json.loads(r['taslak_notu']).get('etki_kaynagi')) for r in effect],
           [r.get('p') for r in transfer if r['klinik_bilgi_en'] == 'Y-Z' and r.get('hasta_grubu')]))
    notes = {(r['klinik_bilgi_en'], r.get('hasta_grubu')): json.loads(r['taslak_notu']) for r in transfer}
    check('sentetik: taslak_notu işaretleri / biçim (değerle tutarlıysa) / etiket',
          [{'p_test_isareti': 'u'}, {'bicim_etiketi': 'n (%)', 'p_test_isareti': 'p'},
           {'posthoc_isareti': 'II', 'bicim_etiketi': 'n (%)'}, {'bicim_etiketi': 'Median (Q1-Q3)', 'p_test_isareti': 'u'},
           {'test_isareti': 'f'}, {'etiket': 'X-Z'}],
          [{k: notes[key][k] for k in ('posthoc_isareti', 'p_test_isareti', 'test_isareti', 'bicim_etiketi', 'etiket')
            if k in notes[key]} for key in (('Age', 'Group A'), ('Female', 'Group A'), ('Aspirin', 'Group B'),
                                            ('Score (A)', 'Group A'), ('Test value', 'Group A'), ('X-Z', 'Group A'))])
    t3 = tables[1]
    issue = next((s for s in t3['sorunlar'] if s['tur'] == 'elle_inceleme_gerekli'), None)
    check('sentetik karmaşık tablo: elle_inceleme_gerekli, satır yok, ham satırlar saklandı', (True, 0, 3),
          (issue is not None, len(t3['satirlar']), len(t3.get('elle_inceleme_satirlari', []))))
    check('sentetik karmaşık tablo: nedenler (genel kurallar)', True, issue['parca'] if issue else None,
          bool(issue) and any(p.startswith('çok seviyeli') for p in issue['parca'])
          and any(p.startswith('metin değerli') for p in issue['parca']) and any(p.startswith('sayısal') for p in issue['parca']))
    accepted, errors = validate_records([dict(r, review_status='verified') for r in transfer], {7: SYNTHETIC_PAGE})
    check('sentetik: validate_records', (20, 0), (len(accepted), len(errors)))
    check_synthetic_decisions()


SYNTHETIC_DECISIONS = """**Table 4: Three groups and overall.**

|**Variable**|**Overall (n=30)**|**Low (n=10)**|**Mid (n=10)**|**High (n=10)**|**p**|
|---|---|---|---|---|---|
|Age|50±5|48±4|50±5|52±6|0.200|

**Table 5: Diagnostic values.**

||**Test X**|**Test Y**|
|---|---|---|
|Sensitivity|70%|80%|
|Specificity|60%|90%|
|NPV|75%|85%|
|P|0.010|0.020|
"""


def check_synthetic_decisions():
    """Ekip kararlarının genel kuralları (makaleye özel değil): 'Overall' Total sayılır; 3+ karşılaştırma grubunda
    comparator yok; AUC'siz tanısal tabloda P bağlanamaz (ayrı kayıt + sorun)."""
    pages = {9: SYNTHETIC_DECISIONS}
    tables = pt.parse_tables(pages)
    records = [{'v2_sira': i, 'kanit_turu': 'tablo', 'sayfa': 9, 'kanit_cumle': r['ham'], 'bilgi_turu': 'olcum',
                'baglam': 'study_finding', 'klinik_bilgi_en': r['ham']}
               for i, r in enumerate((r for t in tables for r in t['satirlar']), 1)]
    result = pt.enrich(records, pages)
    t4 = [r for r in result['aktarim'] if json.loads(r['taslak_notu'])['tablo_no'] == 4]
    check('sentetik kararlar: "Overall" Total; 3 karşılaştırma grubunda comparator yok; Total p yok',
          [('Overall', None, None, 'toplam'), ('Low', None, '0.200', None), ('Mid', None, '0.200', None),
           ('High', None, '0.200', None)],
          [(r['hasta_grubu'], r.get('comparator'), r.get('p'), json.loads(r['taslak_notu']).get('sutun_rolu')) for r in t4])
    t5 = next(t for t in result['tablolar'] if t['tablo_no'] == 5)
    rows5 = [r for r in result['aktarim'] if json.loads(r['taslak_notu'])['tablo_no'] == 5]
    p_rows = [json.loads(r['taslak_notu']) for r in rows5 if r['klinik_bilgi_en'] == 'P']
    check('sentetik kararlar: AUC\'siz tanısal tablo -> P bağlanamadı (ayrı kayıt, sorun raporlandı)',
          ('tanisal_performans', 8, 2, True, True),
          (t5.get('tablo_turu'), len(rows5), len(p_rows), all('p_baglanamadi' in n for n in p_rows),
           any(s['tur'] == 'p_satiri_baglanamadi' for s in t5['sorunlar'])))
    check_synthetic_within_group()


SYNTHETIC_WITHIN = """**Table 6: Within-group changes.**

|**Variable**|**Total (n=40)**|**Group A (n=20)**|**Group B (n=20)**|**p**|
|---|---|---|---|---|
|Weight|70 (60-80)|68 (58-78)|72 (62-82)|0.300<br>u|
|Within-group p|<0.001|0.010|0.200||
|Change 0-6 mo<br>w|0.004|0.020|0.150||
|Change 0-12 mo<br>u|0.004|0.020|0.150||
|Pairwise score|12 (40.0)|5 (25.0)|7 (35.0)|0.410|
|Post hoc p|0.010|0.020|0.030|0.040|
|_w Wilcoxon signed-rank test, u Mann-Whitney U test_|||||
"""


def check_synthetic_within_group():
    """Grup içi test satırı genel kuralı (makaleye özel değil). Yapı şartı: grup hücreleri (Total dahil) p değeri ve
    satırın p sütunu boş; ayrıca satır adı grup içi diyor ya da test işaretinin dipnot anlamı grup içi test."""
    pages = {10: SYNTHETIC_WITHIN}
    table = pt.parse_tables(pages)[0]
    reasons = {r['degisken']: pt.within_group_reason(table, r) for r in table['satirlar']}
    check('sentetik grup içi: ad ("Within-group") ve dipnot işareti (w Wilcoxon signed-rank) tanınıyor; Mann-Whitney '
          'işareti, p değeri olmayan "Pairwise" ve p sütunu dolu "Post hoc" satırı tanınmıyor',
          {'Weight': False, 'Within-group p': True, 'Change 0-6 mo': True, 'Change 0-12 mo': False,
           'Pairwise score': False, 'Post hoc p': False},
          {name: reason is not None for name, reason in reasons.items()})
    check('sentetik grup içi: gerekçe ad / işaret yolunu söylüyor', (True, True),
          ("'Within-group'" in (reasons['Within-group p'] or ''), "işareti 'w'" in (reasons['Change 0-6 mo'] or '')))
    records = [{'v2_sira': i, 'kanit_turu': 'tablo', 'sayfa': 10, 'kanit_cumle': r['ham'], 'bilgi_turu': 'olcum',
                'baglam': 'study_finding', 'klinik_bilgi_en': r['ham']} for i, r in enumerate(table['satirlar'], 1)]
    rows = pt.enrich(records, pages)['aktarim']

    def summary(name):
        return [(r['hasta_grubu'], r.get('comparator'), r.get('p'), json.loads(r['taslak_notu']).get('karsilastirma_turu'),
                 json.loads(r['taslak_notu']).get('p_sutun'))
                for r in rows if json.loads(r['taslak_notu'])['degisken'] == name]

    check('sentetik grup içi: "Within-group p" kayıtlarında comparator / p / p_sutun yok, karsilastirma_turu grup_ici',
          [('Total', None, None, 'grup_ici', None), ('Group A', None, None, 'grup_ici', None),
           ('Group B', None, None, 'grup_ici', None)], summary('Within-group p'))
    check('sentetik grup içi: gruplar arası satırlar değişmedi (comparator karşılıklı, p sütunu, karsilastirma_turu yok)',
          [('Total', None, None, None, None), ('Group A', 'Group B', '0.040', None, 'p'),
           ('Group B', 'Group A', '0.040', None, 'p')], summary('Post hoc p'))


def check_troponin():
    """İkinci makale (Troponin, 3 tablo): sentetik kayıtlarla (her ham satır bir kayıt), bellekte."""
    if not TROPONIN_PDF.exists():
        check('troponin PDF var (yoksa bu bölüm sınanmaz)', True, False)
        return
    doc, pages = pdf_pages(TROPONIN_PDF)
    tables = pt.parse_tables(pages)
    check('troponin: 3 tablo, Roma rakamlı başlıklar', [(1, 'Table I'), (2, 'Table II'), (3, 'Table III')],
          [(t['tablo_no'], (t['tablo_basligi'] or '').lstrip('# ').split(':')[0]) for t in tables])
    hams = list(dict.fromkeys([(t['sayfa'], r['ham']) for t in tables for r in t['satirlar']]
                              + [(t['sayfa'], h) for t in tables for h in t.get('elle_inceleme_satirlari', [])]))
    records = [{'v2_sira': i, 'kanit_turu': 'tablo', 'sayfa': s, 'kanit_cumle': h, 'bilgi_turu': 'olcum',
                'baglam': 'study_finding', 'klinik_bilgi_en': h} for i, (s, h) in enumerate(hams, 1)]
    result = pt.enrich(records, pages, doc['document_id'])
    tables = result['tablolar']
    per = {t['tablo_no']: [r for r in result['aktarim'] if json.loads(r['taslak_notu'])['tablo_no'] == t['tablo_no']]
           for t in tables}
    check('troponin: satır / kayıt (Table I: 19x3 grup + 3 OR; Table II: 8 ölçü x 3 test; Table III yok)',
          [(19, 60), (8, 24), (0, 0)], [(len(t['satirlar']), len(per[t['tablo_no']])) for t in tables])
    check('troponin: Table III elle inceleme (kayıt yok; 8 ham satırın hepsi)', (True, 8),
          (any(s['tur'] == 'elle_inceleme_gerekli' for s in tables[2]['sorunlar']),
           sum(r['eslesme'] == 'elle_inceleme' for r in result['kayitlar'])))

    def is_result_in_p(r):  # tanısal P / ikili karşılaştırma kaydı: sonucu p'de, deger boş (doğru)
        return json.loads(r['taslak_notu']).get('tani_olcusu') in ('p', 'karsilastirma') and bool(r.get('p'))

    check('troponin: boş / çöp kayıt yok (boş değer, boş grup, ham satır adı, dipnot)', [],
          [(r.get('klinik_bilgi_en'), r.get('hasta_grubu'), r.get('deger')) for r in result['aktarim']
           if (not is_result_in_p(r) and (not r.get('deger') or not any(ch.isdigit() for ch in r['deger'])))
           or r.get('hasta_grubu') == '' or '|' in r['klinik_bilgi_en'] or r['klinik_bilgi_en'].startswith('_')][:3])
    names = [(r['degisken'], r['alt_bolum']) for r in tables[0]['satirlar']]
    check('troponin Table I: Sex alt satırları, işaretsiz ad, birleşik hücre adı',
          (('Female', 'Sex'), ('Male', 'Sex'), ('P value for intra groups', None), ('Pairwise comparation A-B', None)),
          (names[1], names[2], names[15], names[16]))
    check('troponin Table I: biçim satırı bölüm kapsamını bitirdi', None, tables[0]['satirlar'][9]['alt_bolum'])

    # Madde 6: Total sütunu veri olarak kalır, karşılaştırma grubu sayılmaz.
    t1, t2 = per[1], per[2]
    groups1 = [(c['grup'], c['n'], c.get('bicim'), bool(c.get('toplam'))) for c in tables[0]['sutunlar'] if c['rol'] == 'grup']
    check('troponin Table I: (n=…) başlığın her yerinden, " / Median (Q1-Q3)" addan ayrıldı, Total işaretli',
          [('Total', 114, 'Median (Q1-Q3)', True), ('ACS Absent', 94, 'Median (Q1-Q3)', False),
           ('ACS Present', 20, 'Median (Q1-Q3)', False)], groups1)
    notes1 = [json.loads(r['taslak_notu']) for r in t1]
    total = [r for r, n in zip(t1, notes1) if n.get('sutun_rolu') == 'toplam']
    check('troponin Table I: Total kaydı 19 (sutun_rolu=toplam, hasta_grubu Total, comparator / p yok, grup_n 114)',
          (19, {('Total', None, None, 114)}),
          (len(total), {(r['hasta_grubu'], r.get('comparator'), r.get('p'), json.loads(r['taslak_notu'])['grup_n'])
                        for r in total}))
    check('troponin Table I: comparator yalnız gerçek gruplar arasında (ACS Absent <-> ACS Present; grup içi hariç)',
          {('ACS Absent', 'ACS Present'), ('ACS Present', 'ACS Absent')},
          {(r['hasta_grubu'], r.get('comparator')) for r, n in zip(t1, notes1)
           if r.get('hasta_grubu') and n.get('sutun_rolu') != 'toplam' and n.get('karsilastirma_turu') != 'grup_ici'})
    # Grup içi test satırları (dipnot: f Friedman; Post Hoc Dunn): her sütun kendi grubunun zaman noktalarını
    # karşılaştırır; gruplar arası değil -> comparator / p sütunu yok, karsilastirma_turu = 'grup_ici'.
    within = [(r['klinik_bilgi_en'], r['hasta_grubu'], r.get('comparator'), r.get('p'), n.get('comparator_sutun'),
               n.get('comparator_n'), n.get('p_sutun')) for r, n in zip(t1, notes1) if n.get('karsilastirma_turu') == 'grup_ici']
    check('troponin Table I: grup içi satırlar (P value for intra groups, Pairwise A-B / A-C / B-C) -> 12 kayıt, '
          'comparator / p / p_sutun yok',
          (12, ['P value for intra groups', 'A-B', 'A-C', 'B-C'], {(None, None, None, None, None)}),
          (len(within), list(dict.fromkeys(w[0] for w in within)), {w[2:] for w in within}))
    check('troponin Table I: grup içi kural yalnız bu 4 satırda; gerekçe satır adından',
          [16, 17, 18, 19], [r['satir_no'] for r in tables[0]['satirlar'] if pt.within_group_reason(tables[0], r)])
    check('troponin Table I: "P value for intra groups" işareti f -> dipnotta Friedman (işaret yolu da grup içi)',
          True, bool(pt.WITHIN_GROUP_TEST.search(pt.footnote_mark_meaning(tables[0], 'f') or '')))
    check('troponin Table I: Mann-Whitney işareti (u) grup içi test sayılmıyor', False,
          bool(pt.WITHIN_GROUP_TEST.search(pt.footnote_mark_meaning(tables[0], 'u') or '')))
    if VERIFIED_TROPONIN.exists():
        verified = json.loads(VERIFIED_TROPONIN.read_text(encoding='utf-8'))
        def unit(r):
            n = json.loads(r['taslak_notu'])
            return n['tablo_no'], n['satir_no'], n.get('sutun'), r.get('etki_turu'), r['kanit_cumle']

        human = {unit(r): r.get('comparator') for r in verified}
        check('troponin: comparator\'lar insan doğrulamasıyla aynı (zehra_troponin_tablo_dogrulanmis.json, 84 kayıt)',
              (84, []), (len(human), [(unit(r)[:3], r.get('comparator'), human.get(unit(r), 'yok'))
                                      for r in result['aktarim'] if human.get(unit(r), 'yok') != r.get('comparator')][:5]))
    check('troponin Table I: Total Age biçimi başlıktan (Median (Q1-Q3); ara satırdaki "n (%)" değerle uymuyor)',
          'Median (Q1-Q3)', json.loads(total[0]['taslak_notu']).get('bicim_etiketi'))
    # Madde 8: p sütunundaki "7.8 (2.4-25.1)<br>or" -> OR etki kaydı; p sütununun asıl p'leri değişmez.
    effects = [(r['klinik_bilgi_en'], n['alt_bolum'], r['etki_turu'], r['deger'], r['ci95'], r.get('p'), r.get('hasta_grubu'))
               for r, n in zip(t1, notes1) if n.get('etki_kaynagi') == 'p_sutunu']
    check('troponin Table I: p sütunundaki OR\'ler etki kaydı (OR, deger, ci95 ayrı; p yok)',
          [('Risc of ACS', 'delta hs Trop I (0-1)h ±20%', 'OR', '7.8', '2.4-25.1', None, None),
           ('Risc of ACS', 'delta hs Trop I (0-3)h ±20%', 'OR', '9.6', '2.6-34.9', None, None),
           ('Risc of ACS', 'delta hs Trop I (1-3)h ±20%', 'OR', '13.4', '3.6-49.2', None, None)], effects)
    check('troponin Table I: OR satırlarının grup kayıtlarında p yok, Non risc p\'si <0.001 (asıl p değişmedi)',
          ({None}, {'<0.001'}),
          ({r.get('p') for r in t1 if r['klinik_bilgi_en'] == 'Risc of ACS' and r.get('hasta_grubu')},
           {r.get('p') for r, n in zip(t1, notes1) if r['klinik_bilgi_en'] == 'Non risc' and r.get('hasta_grubu')
            and n.get('sutun_rolu') != 'toplam'}))
    # Madde 11: tekrar eden ad bölümüyle kayıtta; klinik_bilgi_en değişmez.
    check('troponin Table I: "Non risc" 3 bölümde, klinik_bilgi_en değişmedi, bölüm taslak_notu.alt_bolum',
          ({'Non risc'}, ['delta hs Trop I (0-1)h ±20%', 'delta hs Trop I (0-3)h ±20%', 'delta hs Trop I (1-3)h ±20%']),
          ({r['klinik_bilgi_en'] for r in t1 if r['sonuc_olcutu_tr'] == 'Non risc'},
           sorted({n['alt_bolum'] for r, n in zip(t1, notes1) if r['sonuc_olcutu_tr'] == 'Non risc'})))

    # Madde 12: tanısal performans tablosu, kayıt = test x ölçü.
    tests = [(c['grup'], c.get('etiket_harf')) for c in tables[1]['sutunlar'] if c['rol'] == 'test']
    check('troponin Table II: tanısal performans tablosu; sütunlar test (etiket harfiyle)',
          ('tanisal_performans', [('Delta HS Trop I 1h-0h %', 'A'), ('Delta HS Trop I 3h-0h %', 'B'),
                                  ('Delta HS Trop I 3h-1h %', 'C')]), (tables[1].get('tablo_turu'), tests))
    notes2 = [json.loads(r['taslak_notu']) for r in t2]
    check('troponin Table II: hepsi tanı testi / tanısal performans, hasta_grubu yok, test mudahale_en\'de',
          ({'tanı testi'}, {'tanısal performans'}, {None}, {t for t, _ in tests}),
          ({r['mudahale_turu'] for r in t2}, {r['outcome_type'] for r in t2}, {r.get('hasta_grubu') for r in t2},
           {r['mudahale_en'] for r in t2}))
    first = [(r['sonuc_olcutu_tr'], r['etki_turu'], r.get('deger'), r.get('p'), r.get('unit'), n['tani_olcusu'], n.get('se'))
             for r, n in zip(t2, notes2) if r['mudahale_en'] == 'Delta HS Trop I 1h-0h %']
    check('troponin Table II: 1h-0h testinin ölçüleri (P AUC\'ye bağlı; Odds Ratio birimsiz, SE ayrı)',
          [('Cut off', 'diger', '>66.7', None, None, 'cutoff', None),
           ('Sensitivity', 'oran / yuzde', '75.0%', None, None, 'sensitivity', None),
           ('Specificity', 'oran / yuzde', '79.8%', None, None, 'specificity', None),
           ('Accuracyrate', 'oran / yuzde', '78.95%', None, None, 'accuracy', None),
           ('AUC', 'AUC / C-istatistigi', '0.755', None, None, 'auc', '0.072'),
           ('AUC', 'AUC / C-istatistigi', None, '<0.001', None, 'p', None),
           ('Pairwise comparation for AUC', 'AUC / C-istatistigi', None, '0.291', None, 'karsilastirma', None),
           ('Pairwise comparation for AUC', 'AUC / C-istatistigi', None, '0.323', None, 'karsilastirma', None),
           ('Odds Ratio', 'OR', '11.84', None, None, 'or', '0.70')], first)
    link = next(n for n in notes2 if n['tani_olcusu'] == 'p')
    check('troponin Table II: P satırı AUC satırına bağlandı (bitişik + dipnot ROC)', (5, 'AUC', True),
          (link.get('p_baglandigi_satir'), link.get('p_baglandigi_olcu'), 'dipnot' in link.get('p_baglama_nedeni', '')))
    check('troponin Table II: ikili AUC karşılaştırmaları test çiftine çözüldü',
          [('A-B', 'Delta HS Trop I 1h-0h %', 'Delta HS Trop I 3h-0h %', '0.291'),
           ('A-C', 'Delta HS Trop I 1h-0h %', 'Delta HS Trop I 3h-1h %', '0.323'),
           ('B-C', 'Delta HS Trop I 3h-0h %', 'Delta HS Trop I 3h-1h %', '0.613')],
          [(n['karsilastirma'], r['mudahale_en'], r['comparator'], r['p']) for r, n in zip(t2, notes2)
           if n['tani_olcusu'] == 'karsilastirma'])
    check('troponin Table II: "Accuracyrate" kanıt metninde de boşluksuz (pymupdf4llm layout yolu; ad kanıtta birebir)',
          (True, False), ('|Accuracyrate|' in pages[3], '|Accuracy rate|' in pages[3]))
    check('troponin: hiçbir kayıtta unit "SE." yok', [], [r['klinik_bilgi_en'] for r in result['aktarim']
                                                       if (r.get('unit') or '').upper().startswith('SE')])
    check('troponin: kayıt alanları küme değerlerinde', [],
          [(r['klinik_bilgi_en'], name, r.get(name)) for r in result['aktarim']
           for name, allowed in (('etki_turu', ETKI_TURU), ('mudahale_turu', MUDAHALE_TURU), ('outcome_type', OUTCOME_TYPE))
           if r.get(name) and r[name] not in allowed])
    # Sayı kontrolü: test adı (mudahale_en) ve hasta_grubu / comparator sütun başlığından gelir; satır kanıtında
    # olmayan sayıları ("1h-0h") yalnız bu başlık alanlarında beklenir.
    header_fields = {'mudahale_en', 'hasta_grubu', 'comparator'}
    check('troponin: sayı kontrolü (başlık alanları hariç: mudahale_en / hasta_grubu / comparator)', [],
          [(r['klinik_bilgi_en'], name) for r in result['aktarim'] for name in NUMBER_CHECKED
           if name not in header_fields and numbers_in(r.get(name)) - numbers_in(r['kanit_cumle'])][:3])
    accepted, errors = validate_records([dict(r, review_status='verified') for r in result['aktarim']], pages)
    # Bilinen: src.pilot_clinical._structured_key (tekrar anahtarı) mudahale_en içermiyor; aynı değeri taşıyan iki
    # test (Sensitivity 3h-0h / 3h-1h "80.0%", P "<0.001") "tekrar" sayılıyor. Anahtara mudahale_en eklenince tekrar yok.
    dup = [result['aktarim'][e['satir'] - 1] for e in errors]
    check('troponin: validate_records (bilinen: 3 tanısal kayıt "tekrar"; anahtarda mudahale_en yok)',
          (81, [('Sensitivity', 'Delta HS Trop I 3h-1h %'), ('P', 'Delta HS Trop I 3h-0h %'), ('P', 'Delta HS Trop I 3h-1h %')]),
          (len(accepted), [(r['klinik_bilgi_en'], r['mudahale_en']) for r in dup]),
          len(accepted) == 81 and all(e['hata'].startswith('tekrar') for e in errors)
          and [(r['klinik_bilgi_en'], r['mudahale_en']) for r in dup]
          == [('Sensitivity', 'Delta HS Trop I 3h-1h %'), ('P', 'Delta HS Trop I 3h-0h %'), ('P', 'Delta HS Trop I 3h-1h %')])
    import src.pilot_clinical as pc
    keys = [(r['sayfa'], r['klinik_bilgi_en'].casefold(), r.get('mudahale_en')) + pc._structured_key(r)
            for r in result['aktarim']]
    check('troponin: tekrar anahtarına mudahale_en eklenince 84 kaydın hepsi ayrı', 84, len(set(keys)))


def check_inputs(doc, pages):
    """Girdi: findings zorunlu; başka belgenin findings'i ve çıktısı reddedilir."""
    import tempfile
    raised = _raises
    check('findings yok -> FindingsError (pilot JSON\'una dönülmez)', True,
          raised(lambda: pt.run(None, None, doc, pages), pt.FindingsError))
    check('load_records(None) -> FindingsError', True, raised(lambda: pt.load_records(None), pt.FindingsError))
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / 'findings.json'
        records = json.loads(pt.DEFAULT_JSON.read_text(encoding='utf-8')) if pt.DEFAULT_JSON.exists() else []
        wrapper.write_text(json.dumps({'document_id': 'baska-belge', 'kayitlar': records}), encoding='utf-8')
        check('findings başka document_id beyan ediyor -> FindingsError', True,
              raised(lambda: pt.run(None, wrapper, doc, pages), pt.FindingsError))
        wrapper.write_text(json.dumps({'document_id': doc['document_id'], 'kayitlar': records}), encoding='utf-8')
        if records:
            check('findings aynı document_id -> kabul', 'document_id', pt.run(None, wrapper, doc, pages)['rapor']['belge_kontrolu'])
    if TROPONIN_PDF.exists() and pt.DEFAULT_JSON.exists():
        other, other_pages = pdf_pages(TROPONIN_PDF)
        check('pilot findings + Troponin PDF -> FindingsError (içerik kontrolü)', True,
              raised(lambda: pt.run(None, pt.DEFAULT_JSON, other, other_pages), pt.FindingsError))
        check('Troponin varsayılan çıktı adı pilot çıktısından ayrı', True,
              pt.default_output(other) != pt.DEFAULT_OUTPUT and 'who_J_Coll' in pt.default_output(other).name)
        if pt.DEFAULT_OUTPUT.exists():
            check('pilot çıktısının üzerine başka belge --overwrite ile bile yazamaz', True,
                  raised(lambda: pt.check_outputs(pt.output_paths(pt.DEFAULT_OUTPUT), other, True), FileExistsError))
            check('pilot belgesi kendi çıktısının üzerine --overwrite ile yazabilir', False,
                  raised(lambda: pt.check_outputs(pt.output_paths(pt.DEFAULT_OUTPUT), doc, True), FileExistsError))


def _raises(func, exc):
    try:
        func()
    except exc:
        return True
    return False


def transfer_key(record):
    n = json.loads(record['taslak_notu'])
    return n['tablo_no'], n['satir_no'], n.get('sutun'), n.get('analiz')


def check_row_name_rules():
    """row_name kuralları (sentetik hücreler; PDF gerekmez)."""
    cases = [
        ('CAD n (%)', 'CAD'),                                  # istatistik etiketi atılır
        ('Female gender, n (%)', 'Female gender'),             # önündeki virgülle
        ('Smoking n(%)', 'Smoking'),
        ('Age (years)', 'Age (years)'),                        # klinik birim kalır
        ('Glucose (mg/dL)', 'Glucose (mg/dL)'),
        ('LvEF (%)', 'LvEF (%)'),                              # "(%)" tek başına birimdir
        ('BMI kg/m<sup>2</sup>', 'BMI kg/m<sup>2</sup>'),      # <sup> ham kalır (kanıtta birebir)
        ('Age, mean ± SD', 'Age'),
        ('Age (mean ± SD)', 'Age'),
        ('Weight (kg), mean±SD', 'Weight (kg)'),
        ('CRP, median (IQR)', 'CRP'),
        ('CRP (mg/L) median [IQR]', 'CRP (mg/L)'),
        ('Duration, median (range)', 'Duration'),
        ('**Hypertension** n (%)', 'Hypertension'),            # kenar ** atılır
        ('<br>Warfarin', 'Warfarin'),                          # <br> öncesi boş parça
        ('emograpcs eatures<br>Age (years)', 'Age (years)'),   # <br> öncesi bölüm kalıntısı
        ('BuN (mg/dL )', 'BuN (mg/dL )'),                      # iç boşluk değişmez (kanıtta birebir)
        ("E/e'", "E/e'"),
    ]
    got = [(cell, pt.row_name(cell)) for cell, _ in cases]
    check('row_name: istatistik etiketi atılır, birim / <sup> kalır', cases, got)
    check('row_name: ad ham hücrenin birebir alt dizesi (<br>/kenar ** hariç)', True,
          all(want in cell for cell, want in cases))


def check_clean_names(transfer, tables, real):
    """Aktarımda klinik_bilgi_en = temiz satır adı; kanıtta birebir; v3_3 ile birebir eşleşme."""
    rows = {(t['tablo_no'], r['satir_no']): r for t in tables for r in t['satirlar']}
    names = [(r['klinik_bilgi_en'], rows[transfer_key(r)[:2]]['ad']) for r in transfer]
    check('aktarım: klinik_bilgi_en = tablo satırının temiz adı', len(transfer), sum(a == b for a, b in names))
    check('aktarım: klinik_bilgi_en ham tablo satırı değil (| yok, " n (%)" yok)', [],
          [a for a, _ in names if '|' in a or pt.STAT_LABEL.search(a)][:3])
    check('aktarım: temiz ad kanit_cumle içinde birebir', len(transfer),
          sum(r['klinik_bilgi_en'] in r['kanit_cumle'] for r in transfer))
    check('aktarım: temiz_ad_kanitta_yok sorunu yok', [],
          [s for t in tables for s in t['sorunlar'] if s['tur'] == 'temiz_ad_kanitta_yok'])
    examples = {name: next((r['klinik_bilgi_en'] for r in transfer if rows[transfer_key(r)[:2]]['degisken'] == name), None)
                for name in ('CAD', 'Female gender', 'Age', 'BMI', 'WBC count', 'Warfarin', 'HCR')}
    check('aktarım örnekleri', {'CAD': 'CAD', 'Female gender': 'Female gender', 'Age': 'Age (years)',
                                'BMI': 'BMI kg/m<sup>2</sup>', 'WBC count': 'WBC count (x10<sup>3</sup>/µl)',
                                'Warfarin': 'Warfarin', 'HCR': 'HCR'}, examples)
    if not (real and V33.exists()):
        check('v3_3: zehra_dogrulanmis_v3_3.json var (gerçek girdi)', True, False)
        return
    v33 = {transfer_key(r): r for r in json.loads(V33.read_text(encoding='utf-8')) if r.get('kanit_turu') == 'tablo'}
    mine = {transfer_key(r): r for r in transfer}
    check('v3_3: 116 tablo kaydı, aynı anahtarlar (tablo, satır, sütun, analiz)', (116, True),
          (len(v33), set(v33) == set(mine)))
    differ = [(k, mine[k]['klinik_bilgi_en'], v33[k]['klinik_bilgi_en']) for k in v33
              if k in mine and mine[k]['klinik_bilgi_en'] != v33[k]['klinik_bilgi_en']]
    check('v3_3: klinik_bilgi_en 116/116 birebir', [], differ[:5])

    def note_without_review(record):
        note = json.loads(record['taslak_notu'])
        note.pop('inceleme_notu', None)  # v3_3'te elle eklenmiş inceleme notu; kod üretmez
        return note

    other = sorted({f for k in v33 if k in mine for f in set(v33[k]) | set(mine[k])
                    if f not in ('review_status', 'klinik_bilgi_en', 'taslak_notu') and v33[k].get(f) != mine[k].get(f)}
                   | {'taslak_notu' for k in v33 if k in mine and note_without_review(v33[k]) != note_without_review(mine[k])})
    check('v3_3: diğer alanlar aynı (review_status ve taslak_notu.inceleme_notu hariç)', [], other)


def main():
    if not PDF.exists():
        print(f'PDF bulunamadı: {PDF}')
        sys.exit(1)
    doc, pages = pdf_pages(PDF)
    tables = pt.parse_tables(pages)
    t1 = next(t for t in tables if t['tablo_no'] == 1)
    t2 = next(t for t in tables if t['tablo_no'] == 2)

    check_row_name_rules()

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
    check(f'girdi ({pt.DEFAULT_JSON.name if real else "sentetik"}): tablo kaydı', 58, summary['tablo_kaydi'])
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
          (lvef['mudahale_turu'], 'zorunlu tutmuyor' in note(lvef)['mudahale_turu_bos_nedeni']))
    medication = {(n['tablo_no'], n['satir_no']) for n in map(note, preview) if n['alt_bolum'] == 'Medications'}
    check('mudahale_turu yalnız ilaç satırlarında', True,
          all(bool(r['mudahale_turu']) == ((note(r)['tablo_no'], note(r)['satir_no']) in medication) for r in preview))
    check('"tanı testi" hiç atanmadı', 0, sum(r['mudahale_turu'] == 'tanı testi' for r in preview))
    check('mudahale_turu dolu / boş (13x2 ilaç)', (26, 90), (sum(bool(r['mudahale_turu']) for r in preview), summary['sql_mudahale_turu_bos']))
    check('raporda mudahale_turu boş satır listesi (38 T1 + 7 T2)', (45, 90),
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

    # --- import_records aktarım kayıtları ---
    transfer = result['aktarim']
    allowed = INPUT_FIELDS | set(STRUCTURED_FIELDS)
    check('aktarım kaydı = önizleme satırı', (116, 116), (len(transfer), summary['aktarim_kaydi']))
    check('aktarım alanları import_records\'un kabul ettikleri', [],
          sorted({k for r in transfer for k in r} - allowed))
    check('aktarımda review_status / hasta_sayisi yok', (0, 0),
          (sum('review_status' in r for r in transfer), sum('hasta_sayisi' in r for r in transfer)))
    # klinik_bilgi_en bilerek farklı: tablo kaydında ham satır yerine temiz satır adı (aşağıda ayrıca sınanır).
    check('aktarımda Sinem alanları pilot kaydıyla aynı (klinik_bilgi_en hariç)', True, all(
        all(r[k] == records[json.loads(r['taslak_notu'])['pilot_v2_sira'] - 1][k]
            for k in ('bilgi_turu', 'baglam', 'kanit_cumle', 'sayfa', 'kanit_turu'))
        for r in transfer) if real else True)
    check_clean_names(transfer, result['tablolar'], real)
    if real:
        check_pilot_unchanged(result, doc['document_id'])
    check_general_rules()
    check_troponin()
    check_inputs(doc, pages)
    hb_t = [r for r in transfer if r.get('sonuc_olcutu_tr') == 'HbA1c' and json.loads(r['taslak_notu'])['tablo_no'] == 1]
    check('aktarım HbA1c: aynı kanıt, iki grup', (2, 1, ['Ischemic event', 'No ischemic event']),
          (len(hb_t), len({r['kanit_cumle'] for r in hb_t}), [r['hasta_grubu'] for r in hb_t]))
    accepted, errors = validate_records(transfer, pages)
    check('validate_records(aktarım): grup/analiz satırları tekrar sayılmıyor', (116, 0), (len(accepted), len(errors)))
    accepted, errors = validate_records(transfer + [dict(transfer[0])], pages)
    check('validate_records: gerçek tekrar "tekrar" hatası', (116, 1, True),
          (len(accepted), len(errors), bool(errors) and errors[0]['hata'].startswith('tekrar')))

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
