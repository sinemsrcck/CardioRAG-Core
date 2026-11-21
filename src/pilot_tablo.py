"""Pilot tablo kayıtlarını tablo/sütun başlıklarıyla eşleyen deterministik modül (LLM yok, SQL yazımı yok).

Sorun: pilot çıkarım (src/pilot_auto_extract.py) tablo satırını başlıksız saklıyor;
"|HbA1c|7.83±1.2|6.23±1.2|<0.001|" hangi değerin hangi gruba ait olduğunu söylemiyor.
Bu modül pdf_pages metnindeki markdown tabloları ayrıştırır, kanit_turu=tablo kayıtlarını
ilgili satıra bağlar ve klinik_bulgular sütunlarına (yeni tablo/sütun açmadan) bir SQL
ÖNİZLEMESİ üretir. Ayrıca src.pilot_clinical.import_records'a doğrudan verilebilecek aktarım
kayıtları (grup/analiz başına bir kayıt, review_status YOK) yazar. Veritabanına bağlanmaz,
bilgi_turu/baglam değerlerini değiştirmez.

Kurallar:
- Ham kanıt (tablo satırı) birebir korunur; ayrıca <br>, <sup>, ** temizlenmiş metin üretilir.
- Bozuk başlık parçaları (ör. "Dhi f", "emograpcs eatures") düzeltilmez, rapora yazılır.
- Hiyerarşi/alt kırılım (ör. CCB -> Dihydropyridine) tablo yapısında yazmadığı için çıkarılmaz.
- Tablo kaydının klinik_bilgi_en'i ham tablo satırı değil, temiz satır adıdır (row_name):
  "CAD n (%)|119 (31.98)|…" -> "CAD"; "Age (years)" -> "Age (years)" (klinik birim kalır).
  İstatistik etiketi (n (%), mean ± SD, median (IQR) …) atılır. Ad kanıt satırının birebir alt
  dizesidir: <sup>2</sup> gibi etiketler adda ham kalır, çünkü kanit_cumle ham satırdır ve
  validate_records / data_quality --pilot (pilot_bilgi_kanitta_yok) birebir alt dize arar;
  "^2" gösterimi sunum katmanında (sql/sunum_hoca v_sunum_taban evidence) üretilir.

Çalıştırma:
    python -m src.pilot_tablo --pdf data/pdfs/TK-cardiovascular36-2-1.pdf
"""
import argparse
import csv
import json
import re
from pathlib import Path

from src.pilot_clinical import STRUCTURED_FIELDS

PILOT_DIR = Path('data/review/pilot')
# Güncel pilot çıktısı (PDF yükleyici v4 ile 190/190 geçerli); yoksa eski v2. --findings ile başka girdi verilir.
# Not: taslak_notu.pilot_v2_sira adı geriye uyum için korunur; değeri girdi dosyasındaki 1 tabanlı sıradır.
DEFAULT_JSON = next((p for p in (PILOT_DIR / 'pilot_findings_v3_2.json', PILOT_DIR / 'pilot_findings_v2.json')
                     if p.exists()), PILOT_DIR / 'pilot_findings_v3_2.json')
DEFAULT_CSV = PILOT_DIR / '02_zehra_klinik_incelemesi.csv'
DEFAULT_OUTPUT = PILOT_DIR / 'pilot_tablo_zengin.json'
VERSION = 'pilot-tablo-v1'

# SQL önizlemesinde kullanılan küme değerleri. src.clinical_validation'daki izinli
# kümelerin elemanları olmalı (evaluation/test_pilot_tablo.py doğrular). Burada içe
# aktarılmıyor: o modül src.database'i (mysql) yüklüyor, bu modül DB'den bağımsız kalmalı.
ETKI_N_PCT = 'oran / yuzde'
ETKI_OTHER = 'diger'
MUDAHALE_ILAC = 'arka plan tedavisi'
MUDAHALE_BOS_NEDENI = ('İlaç satırı değil (grup ölçümü / ilişki satırı); aktarım yolu '
                       '(pilot_clinical.import_records) mudahale_turu\'yu zorunlu tutmuyor, boş kalması sorun değil')
# import_records'a gidecek temel (Sinem'in) alanlar; pilot kaydından aynen alınır. Tablo kaydında
# klinik_bilgi_en istisnadır: ham satır yerine temiz satır adı (row_name) yazılır.
TRANSFER_BASE_FIELDS = ('bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_cumle', 'sayfa', 'kanit_turu')
OUTCOME_KULLANIM = 'uygulama oranı'
OUTCOME_POPULASYON = 'çalışma popülasyonu'
EFFECT_TYPES = (('odds ratio', 'OR'), ('hazard ratio', 'HR'), ('relative risk', 'RR'),
                ('risk ratio', 'RR'), ('incidence rate ratio', 'IRR'))
ANALYSIS_LABELS = ('univariate', 'multivariate', 'univariable', 'multivariable', 'unadjusted', 'adjusted')

SEPARATOR = re.compile(r'^\|(?:\s*:?-{3,}:?\s*\|)+\s*$')
CAPTION = re.compile(r'^[\s#>*_]*table\s*(\d+)\s*[:.]', re.IGNORECASE)
GROUP_N = re.compile(r'^(.*?)\s*\(\s*n\s*=\s*(\d+)\s*\)\s*$', re.IGNORECASE)
MEAN_SD = re.compile(r'^-?\d+(?:\.\d+)?\s*±\s*\d+(?:\.\d+)?$')
N_PCT = re.compile(r'^\d+\s*\(\s*\d+(?:\.\d+)?\s*\)$')
VAL_RANGE = re.compile(r'^(-?\d+(?:\.\d+)?)\s*\(\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*\)$')
N_PCT_SUFFIX = re.compile(r'\s+n\s*\(%\)\s*$', re.IGNORECASE)
UNIT_PAREN = re.compile(r'^(.*?)\s*\(([^()]+)\)\s*$')
UNIT_TOKEN = re.compile(r'^[A-Za-zµ%]+(?:/[A-Za-zµ0-9^]+)+$')
# Satır adının sonundaki istatistik etiketi (klinik birim değil): "n (%)", "n, %", "mean ± SD",
# "mean (SD)", "median (IQR)", "median [IQR]", "median (range)", "median (Q1-Q3)"; isteğe bağlı parantez
# ve önündeki virgül/boşlukla. "(%)" tek başına birimdir (ör. "LvEF (%)") ve kalır.
STAT_LABEL = re.compile(
    r'[\s,]*\(?\s*(?:n\s*\(\s*%\s*\)|n\s*,\s*%|mean\s*(?:±|\+/-|\+-)\s*s\.?d\.?|mean\s*\(\s*s\.?d\.?\s*\)'
    r'|median\s*[(\[]\s*(?:iqr|range|q1\s*[-–,]\s*q3)\s*[)\]]|median\s*,\s*iqr)\s*\)?\s*$', re.IGNORECASE)
BOLD_EDGE = re.compile(r'^(?:\*\*|__)+|(?:\*\*|__)+$')
# Başlıkta tek harfin kelimeden kopması ("u nivariate"); a/I gerçek kelime olabilir.
SPLIT_LETTER = re.compile(r'(?<![A-Za-z])[b-hj-zB-HJ-Z] [a-z]{3,}')


# ---------------------------------------------------------------------------
# Metin temizleme
# ---------------------------------------------------------------------------

def clean(text):
    """<br>, <sup>/<sub>, ** ve __ işaretlerini kaldırır; boşlukları sadeleştirir."""
    text = re.sub(r'<br\s*/?>', ' ', text or '', flags=re.IGNORECASE)
    # Üst simge anlamı korunur: x10<sup>3</sup> -> x10^3 (x103 değil).
    text = re.sub(r'<sup>\s*(.*?)\s*</sup>', r'^\1', text, flags=re.IGNORECASE)
    text = re.sub(r'</?sub>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?[A-Za-z][^>]*>', ' ', text)
    text = text.replace('**', '').replace('__', '')
    return re.sub(r'\s+', ' ', text).strip()


def row_name(first_cell):
    """Tablo satırının temiz adı (klinik_bilgi_en için), ham ilk hücreden.

    - <br> ile bölünmüşse son parça (öndeki parçalar bölüm başlığı kalıntısıdır; bkz. satira_yapismis_parca).
    - Sondaki istatistik etiketi atılır (STAT_LABEL); klinik birim kalır ("Glucose (mg/dL)").
    - Kenardaki ** / __ atılır; başka hiçbir şey değişmez (boşluk, <sup> ham kalır), böylece ad ham
      kanıt satırının birebir alt dizesidir.
    """
    parts = br_parts(first_cell)
    name = (parts[-1] if parts else '').strip()
    previous = None
    while name != previous:
        previous = name
        name = BOLD_EDGE.sub('', STAT_LABEL.sub('', name)).strip()
    return name


def split_cells(line):
    inner = line.strip()
    if inner.startswith('|'):
        inner = inner[1:]
    if inner.endswith('|'):
        inner = inner[:-1]
    return inner.split('|')


def br_parts(cell):
    return [p for p in re.split(r'<br\s*/?>', cell, flags=re.IGNORECASE) if clean(p)]


def header_cell(cell):
    """(başlık, başlık dışı parçalar). Çok satırlı başlıkta kalın parçalar ' / ' ile birleşir;
    kalın olmayan artık parça başlığa katılmaz (ör. '**Variable**<br>Dhi f' -> 'Variable', ['Dhi f'])."""
    parts = br_parts(cell)
    bold = [p for p in parts if p.strip().startswith('**') or p.strip().startswith('__')]
    if not bold:
        return clean(cell), []
    stray = [clean(p) for p in parts if p not in bold]
    return ' / '.join(clean(p) for p in bold), stray


def split_unit(name):
    """'Glucose (mg/dL)' -> ('Glucose', 'mg/dL'); 'Female gender n (%)' -> ('Female gender', 'n (%)');
    'BMI kg/m2' -> ('BMI', 'kg/m2'); tek kelimelik adlar (E/e') bölünmez."""
    name = name.strip()
    match = N_PCT_SUFFIX.search(name)
    if match:
        return name[:match.start()].strip().rstrip(','), 'n (%)'
    match = UNIT_PAREN.match(name)
    if match and match.group(1).strip():
        return match.group(1).strip().rstrip(',').strip(), match.group(2).strip()
    tokens = name.split()
    if len(tokens) > 1 and UNIT_TOKEN.match(tokens[-1]):
        return ' '.join(tokens[:-1]), tokens[-1]
    return name, None


def value_format(value):
    if MEAN_SD.match(value):
        return 'ortalama±SS'
    if N_PCT.match(value):
        return 'n (%)'
    if VAL_RANGE.match(value):
        return 'değer (aralık)'
    return 'diger'


# ---------------------------------------------------------------------------
# Tablo ayrıştırma
# ---------------------------------------------------------------------------

def _caption(lines, start):
    for k in range(start - 1, max(-1, start - 6), -1):
        match = CAPTION.match(lines[k])
        if match:
            return int(match.group(1)), lines[k].strip()
    return None, None


def _columns(headers):
    """Sütun rolleri: degisken / grup / p / etki (OR, HR...). p sütunu önceki etki sütununa bağlanır.
    Her sütunun 'anahtar'ı benzersizdir: aynı başlık tekrar ederse (Table 2'de iki 'p value')
    sahibi etki sütununun ilk başlık parçası eklenir: 'p value [Univariate analysis]'."""
    roles = _column_roles(headers)
    counts = {}
    for col in roles:
        counts[col['baslik']] = counts.get(col['baslik'], 0) + 1
    for index, col in enumerate(roles):
        key = col['baslik']
        if counts[key] > 1:
            owner = col.get('etki_sutunu')
            suffix = roles[owner]['baslik'].split(' / ')[0] if owner is not None else f'sütun {index + 1}'
            key = f'{key} [{suffix}]'
        col['anahtar'] = key
    return roles


def _column_roles(headers):
    roles = []
    for index, title in enumerate(headers):
        low = title.casefold()
        effect = next((code for key, code in EFFECT_TYPES if key in low), None)
        if index == 0:
            roles.append({'rol': 'degisken', 'baslik': title})
        elif effect:
            label = title.split(' / ')[0].strip()
            first = label.split()[0].casefold() if label else ''
            roles.append({'rol': 'etki', 'baslik': title, 'etki_turu': effect,
                          'analiz': first if first in ANALYSIS_LABELS else label})
        elif re.fullmatch(r'p(?:\s*-?\s*value)?', low.strip()):
            owner = next((i for i in range(index - 1, 0, -1) if roles[i]['rol'] == 'etki'), None)
            roles.append({'rol': 'p', 'baslik': title, 'etki_sutunu': owner})
        else:
            match = GROUP_N.match(title)
            roles.append({'rol': 'grup', 'baslik': title,
                          'grup': match.group(1).strip() if match else title,
                          'n': int(match.group(2)) if match else None})
    return roles


def parse_tables(pages):
    """pages: {sayfa: metin}. Her markdown tabloyu başlık, sütunlar, bölümler ve satırlarla döndürür."""
    tables = []
    for page in sorted(pages):
        lines = pages[page].splitlines()
        i = 0
        while i < len(lines):
            if not (lines[i].strip().startswith('|') and i + 1 < len(lines) and SEPARATOR.match(lines[i + 1].strip())):
                i += 1
                continue
            start = i
            end = i + 2
            while end < len(lines) and lines[end].strip().startswith('|'):
                end += 1
            tables.append(_parse_table(page, lines, start, end, len(tables) + 1))
            i = end
    return tables


def _parse_table(page, lines, start, end, ordinal):
    no, caption_raw = _caption(lines, start)
    issues = []
    if no is None:
        issues.append({'tur': 'tablo_basligi_yok', 'aciklama': f's.{page}: tablonun üstünde "TABLE n:" başlığı bulunamadı; sıra no kullanıldı'})
    caption = clean(caption_raw) if caption_raw else None
    if caption and SPLIT_LETTER.search(caption):
        issues.append({'tur': 'baslikta_olasi_harf_kopmasi', 'parca': SPLIT_LETTER.search(caption).group(0),
                       'aciklama': 'Tablo başlığında kelimeden kopmuş tek harf; düzeltilmedi'})
    header_raw = lines[start]
    headers, stray_header = [], []
    for cell in split_cells(header_raw):
        title, stray = header_cell(cell)
        headers.append(title)
        stray_header.extend(stray)
    if stray_header:
        issues.append({'tur': 'sutun_basliginda_artik_parca', 'parca': stray_header, 'ham': header_raw.strip(),
                       'aciklama': 'Başlık hücresinde kalın olmayan parça; başlığa katılmadı, düzeltme yapılmadı'})
    columns = _columns(headers)
    rows, sections, section, first_data = [], [], None, True
    for line_no in range(start + 2, end):
        raw = lines[line_no]
        cells = split_cells(raw)
        cleaned = [clean(c) for c in cells]
        if cleaned and cleaned[0] and not any(cleaned[1:]):
            section = cleaned[0]
            sections.append(section)
            continue
        if not any(cleaned):
            continue
        parts = br_parts(cells[0])
        name_raw = clean(parts[-1]) if parts else ''
        fused = [clean(p) for p in parts[:-1]]
        if fused:
            issues.append({'tur': 'satira_yapismis_parca', 'parca': fused, 'degisken': name_raw, 'ham': raw.strip(),
                           'aciklama': 'Değişken hücresinde <br> ile ayrılmış ek parça (olası bölüm başlığı kalıntısı); '
                                       'değişken adı son parçadan alındı, parça bölüm başlığı olarak kullanılmadı'})
        if section is None and first_data and (fused or stray_header):
            issues.append({'tur': 'ilk_bolum_basligi_okunamadi',
                           'parca': {'sutun_basligi': stray_header, 'ilk_satir': fused},
                           'aciklama': 'İlk alt bölüm başlığı başlık hücresine ve ilk satıra bölünmüş görünüyor; '
                                       'alt_bolum=None bırakıldı, ad uydurulmadı'})
        first_data = False
        variable, unit = split_unit(name_raw)
        cells_map = {}
        for col, value in zip(columns[1:], cleaned[1:]):
            cells_map[col['anahtar']] = value
        rows.append({'satir_no': len(rows) + 1, 'sayfa': page, 'ham': raw.strip(),
                     'temiz': ' | '.join(c for c in cleaned if c), 'degisken_ham': name_raw,
                     'ad': row_name(cells[0]),
                     'degisken': variable, 'unit': unit, 'alt_bolum': section, 'sutun_degerleri': cells_map})
    return {'tablo_no': no if no is not None else ordinal, 'sayfa': page, 'tablo_basligi': caption,
            'tablo_basligi_ham': caption_raw, 'sutun_basliklari_ham': header_raw.strip(),
            'sutunlar': columns, 'alt_bolumler': sections, 'satirlar': rows, 'sorunlar': issues}


def row_analyses(table, row):
    """Etki tablosu satırı için analiz başına {analiz, etki_turu, deger, ci95, p}."""
    out = []
    for index, col in enumerate(table['sutunlar']):
        if col['rol'] != 'etki':
            continue
        value = row['sutun_degerleri'].get(col['anahtar'], '')
        match = VAL_RANGE.match(value)
        p_col = next((c for c in table['sutunlar'] if c['rol'] == 'p' and c.get('etki_sutunu') == index), None)
        out.append({'analiz': col['analiz'], 'etki_turu': col['etki_turu'], 'sutun': col['anahtar'],
                    'deger': match.group(1) if match else value,
                    'ci95': f'{match.group(2)}-{match.group(3)}' if match else None,
                    'p': row['sutun_degerleri'].get(p_col['anahtar']) if p_col else None,
                    'p_sutun': p_col['anahtar'] if p_col else None})
    return out


def row_groups(table, row):
    """Grup tablosu satırı için grup başına {grup, n, deger, deger_bicimi, comparator, comparator_n, p}."""
    groups = [c for c in table['sutunlar'] if c['rol'] == 'grup']
    p_cols = [c for c in table['sutunlar'] if c['rol'] == 'p' and c.get('etki_sutunu') is None]
    p_value = row['sutun_degerleri'].get(p_cols[0]['anahtar']) if len(p_cols) == 1 else None
    out = []
    for col in groups:
        others = [c for c in groups if c is not col]
        value = row['sutun_degerleri'].get(col['anahtar'], '')
        out.append({'sutun': col['anahtar'], 'grup': col['grup'], 'n': col['n'], 'deger': value,
                    'deger_bicimi': value_format(value),
                    'comparator': others[0]['grup'] if len(others) == 1 else None,
                    'comparator_n': others[0]['n'] if len(others) == 1 else None,
                    'comparator_sutun': others[0]['anahtar'] if len(others) == 1 else None,
                    'p': p_value, 'p_sutun': p_cols[0]['anahtar'] if len(p_cols) == 1 else None})
    return out


# ---------------------------------------------------------------------------
# Kayıt eşleme ve zenginleştirme
# ---------------------------------------------------------------------------

def load_records(path=None):
    """(kayıtlar, kaynak). JSON ana girdi; yoksa CSV yedeği. Her kayda v2_sira eklenir (JSON'da indeks+1)."""
    if path is None:
        path = DEFAULT_JSON if DEFAULT_JSON.exists() else DEFAULT_CSV
    path = Path(path)
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, list):
            raise ValueError('JSON kökünde kayıt listesi olmalı')
        records = [dict(item, v2_sira=item.get('v2_sira', index)) for index, item in enumerate(data, 1)]
    else:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            records = [dict(row, v2_sira=int(row['v2_sira']), sayfa=int(row['sayfa'])) for row in csv.DictReader(handle)]
    return records, str(path)


def _needle(quote):
    text = (quote or '').strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|'):
        text = text[:-1]
    return text.strip()


def match_record(record, tables):
    """('eslesti', tablo, satır) | ('eslesmedi', None, None) | ('belirsiz', None, adaylar)."""
    needle = _needle(record.get('kanit_cumle'))
    if not needle:
        return 'eslesmedi', None, None
    hits = [(t, r) for t in tables if t['sayfa'] == record.get('sayfa') for r in t['satirlar'] if needle in r['ham']]
    if len(hits) == 1:
        return 'eslesti', hits[0][0], hits[0][1]
    if not hits:
        return 'eslesmedi', None, None
    return 'belirsiz', None, [(t['tablo_no'], r['satir_no']) for t, r in hits]


def table_info(table, row):
    info = {'tablo_no': table['tablo_no'], 'tablo_basligi': table['tablo_basligi'], 'sayfa': table['sayfa'],
            'satir_no': row['satir_no'], 'alt_bolum': row['alt_bolum'], 'degisken': row['degisken'],
            'degisken_ham': row['degisken_ham'], 'ad': row['ad'], 'unit': row['unit'], 'sutun_degerleri': row['sutun_degerleri'],
            'temiz_satir': row['temiz']}
    analyses = row_analyses(table, row)
    if analyses:
        info['analizler'] = analyses
    else:
        info['gruplar'] = row_groups(table, row)
        formats = {g['deger_bicimi'] for g in info['gruplar']}
        info['deger_bicimi'] = formats.pop() if len(formats) == 1 else sorted(formats)
        if is_medication(row):
            info['not'] = 'Kullanım oranı n (%); doz değil (makalede doz bilgisi yok).'
    return info


def is_medication(row):
    return bool(row['alt_bolum']) and row['alt_bolum'].casefold().startswith('medication')


# ---------------------------------------------------------------------------
# SQL önizleme (yazma yok)
# ---------------------------------------------------------------------------

def sql_preview(record, table, row, document_id=None):
    """klinik_bulgular sütunlarıyla önizleme satırları. Mevcut sütunlar; yeni sütun yok.

    - Küme alanları (etki_turu, mudahale_turu, outcome_type) src.clinical_validation kümelerinden.
    - Grup n'leri hasta_sayisi'na yazılmaz (ekip kararı); taslak_notu'da (grup_n, comparator_n) kalır.
    - mudahale_turu yalnız ilaç satırlarında ("arka plan tedavisi"); diğerleri boş. Aktarım yolu
      (pilot_clinical.import_records) mudahale_turu'yu zorunlu tutmuyor.
    """
    base = {'document_id': document_id, 'kanit_sayfa': table['sayfa'], 'kanit_cumle': record['kanit_cumle'],
            'kanit_turu': 'tablo', 'review_status': 'pending', 'cikarma_yontemi': None, 'doz': None, 'yon': None}
    note_base = {'kaynak': VERSION, 'pilot_v2_sira': record.get('v2_sira'), 'bilgi_turu': record.get('bilgi_turu'),
                 'baglam': record.get('baglam'), 'tablo_no': table['tablo_no'], 'tablo_basligi': table['tablo_basligi'],
                 'alt_bolum': row['alt_bolum'], 'degisken': row['degisken'], 'satir_no': row['satir_no']}
    out = []
    analyses = row_analyses(table, row)
    if analyses:
        for item in analyses:
            note = dict(note_base, analiz=item['analiz'], sutun=item['sutun'], p_sutun=item['p_sutun'],
                        outcome_type_bos_nedeni='Retrospektif ilişki/prediktör analizi; "prognostik değer" '
                                                'boylamsal izlem gerektirir, emin olunamadı',
                        mudahale_turu_bos_nedeni=MUDAHALE_BOS_NEDENI)
            out.append(dict(base, sonuc_olcutu_tr=row['degisken'], unit=row['unit'], hasta_grubu=None,
                            hasta_sayisi=None, comparator=None, mudahale_en=None, mudahale_turu=None,
                            etki_turu=item['etki_turu'], deger=item['deger'], ci95=item['ci95'], p=item['p'],
                            outcome_type=None, taslak_notu=json.dumps(note, ensure_ascii=False)))
        return out
    medication = is_medication(row)
    for group in row_groups(table, row):
        note = dict(note_base, sutun=group['sutun'], grup_n=group['n'], comparator_sutun=group['comparator_sutun'],
                    comparator_n=group['comparator_n'], p_sutun=group['p_sutun'], deger_bicimi=group['deger_bicimi'],
                    hasta_sayisi_bos_nedeni='Ekip kararı: grup n\'leri taslak_notu\'da (grup_n, comparator_n) kalır')
        if medication:
            note['not'] = 'Kullanım oranı n (%); doz değil.'
            mudahale_turu, mudahale_en, outcome, measure = MUDAHALE_ILAC, row['degisken'], OUTCOME_KULLANIM, 'kullanım oranı'
        else:
            note['mudahale_turu_bos_nedeni'] = MUDAHALE_BOS_NEDENI
            mudahale_turu, mudahale_en, outcome, measure = None, None, OUTCOME_POPULASYON, row['degisken']
        out.append(dict(base, sonuc_olcutu_tr=measure, unit=row['unit'], hasta_grubu=group['grup'], hasta_sayisi=None,
                        comparator=group['comparator'], mudahale_en=mudahale_en, mudahale_turu=mudahale_turu,
                        etki_turu=ETKI_N_PCT if group['deger_bicimi'] == 'n (%)' else ETKI_OTHER,
                        deger=group['deger'], ci95=None, p=group['p'], outcome_type=outcome,
                        taslak_notu=json.dumps(note, ensure_ascii=False)))
    return out


def transfer_record(record, preview_row, row=None):
    """import_records'a verilecek kayıt: pilot kaydının temel alanları + önizleme satırının dolu
    yapılandırılmış alanları. review_status konmaz (onay sonra insan tarafından verilir).

    row (eşleşen tablo satırı) verilirse klinik_bilgi_en temiz satır adıdır (row['ad']); ad kanıtta
    birebir geçmiyorsa kayıttaki değer kalır (enrich bunu 'temiz_ad_kanitta_yok' sorunu olarak yazar)."""
    item = {key: record[key] for key in TRANSFER_BASE_FIELDS if record.get(key) is not None}
    if row is not None and row.get('ad') and row['ad'] in (record.get('kanit_cumle') or ''):
        item['klinik_bilgi_en'] = row['ad']
    item.update({key: preview_row[key] for key in STRUCTURED_FIELDS if preview_row.get(key) is not None})
    return item


# ---------------------------------------------------------------------------
# Akış ve rapor
# ---------------------------------------------------------------------------

def enrich(records, pages, document_id=None):
    tables = parse_tables(pages)
    enriched, preview, transfer, by_row = [], [], [], {}
    for record in records:
        if record.get('kanit_turu') != 'tablo':
            continue
        status, table, row = match_record(record, tables)
        item = dict(record, eslesme=status)
        if status == 'eslesti':
            item['tablo'] = table_info(table, row)
            by_row.setdefault((table['tablo_no'], row['satir_no']), []).append(record.get('v2_sira'))
            rows = sql_preview(record, table, row, document_id)
            preview.extend(rows)
            transfer.extend(transfer_record(record, p, row) for p in rows)
            if not row['ad'] or row['ad'] not in (record.get('kanit_cumle') or ''):
                table['sorunlar'].append({'tur': 'temiz_ad_kanitta_yok', 'satir_no': row['satir_no'], 'ad': row['ad'],
                                          'aciklama': 'Temiz satır adı kanit_cumle içinde birebir yok; '
                                                      'klinik_bilgi_en kayıttaki değerle bırakıldı'})
        elif status == 'belirsiz':
            item['adaylar'] = row
        enriched.append(item)
    missing = [{'tablo_no': t['tablo_no'], 'sayfa': t['sayfa'], 'satir_no': r['satir_no'], 'alt_bolum': r['alt_bolum'],
                'degisken': r['degisken'], 'ham': r['ham']}
               for t in tables for r in t['satirlar'] if (t['tablo_no'], r['satir_no']) not in by_row]
    duplicates = {f'Table {k[0]} satır {k[1]}': v for k, v in by_row.items() if len(v) > 1}
    return {'tablolar': tables, 'kayitlar': enriched, 'sql_onizleme': preview, 'aktarim': transfer,
            'pilot_kaydinda_yok': missing, 'ayni_satira_birden_fazla_kayit': duplicates}


def mudahale_pending(preview):
    """mudahale_turu boş önizleme satırları, tablo satırı başına gruplanmış (bilgi amaçlı; aktarım için sorun değil)."""
    groups = {}
    for row in preview:
        if row['mudahale_turu']:
            continue
        info = json.loads(row['taslak_notu'])
        key = (info['tablo_no'], info['satir_no'])
        item = groups.setdefault(key, {'tablo_no': info['tablo_no'], 'satir_no': info['satir_no'],
                                       'alt_bolum': info['alt_bolum'], 'degisken': info['degisken'],
                                       'bilgi_turu': info['bilgi_turu'], 'pilot_v2_sira': info['pilot_v2_sira'],
                                       'sql_satiri': 0})
        item['sql_satiri'] += 1
    return [groups[k] for k in sorted(groups)]


def summarize(result, source):
    records = result['kayitlar']
    preview = result['sql_onizleme']
    count = lambda status: sum(r['eslesme'] == status for r in records)
    return {
        'girdi': source,
        'tablo_sayisi': len(result['tablolar']),
        'tablo_veri_satiri': sum(len(t['satirlar']) for t in result['tablolar']),
        'tablo_kaydi': len(records),
        'eslesen': count('eslesti'), 'eslesmeyen': count('eslesmedi'), 'belirsiz': count('belirsiz'),
        'eslesmeyen_kayitlar': [r.get('v2_sira') for r in records if r['eslesme'] != 'eslesti'],
        'pilot_kaydinda_yok': len(result['pilot_kaydinda_yok']),
        'ayni_satira_birden_fazla_kayit': len(result['ayni_satira_birden_fazla_kayit']),
        'sql_onizleme_satiri': len(preview),
        'aktarim_kaydi': len(result['aktarim']),
        'sql_mudahale_turu_bos': sum(not r['mudahale_turu'] for r in preview),
        'sql_mudahale_turu_bos_satirlar': mudahale_pending(preview),
        'sql_outcome_type_bos': sum(not r['outcome_type'] for r in preview),
        'sorunlu_basliklar': [dict(s, tablo_no=t['tablo_no'], sayfa=t['sayfa'])
                              for t in result['tablolar'] for s in t['sorunlar']],
    }


def report_markdown(summary, result):
    s = summary
    lines = [
        '# Pilot tablo eşleme raporu', '',
        f'Sürüm `{VERSION}` · deterministik (LLM yok) · veritabanına yazılmadı · girdi `{s["girdi"]}`', '',
        '| Ölçü | Sayı |', '|---|---|',
        f'| Tablo | {s["tablo_sayisi"]} |',
        f'| Tablo veri satırı | {s["tablo_veri_satiri"]} |',
        f'| kanit_turu=tablo kaydı | {s["tablo_kaydi"]} |',
        f'| Eşleşen | {s["eslesen"]} |',
        f'| Eşleşmeyen | {s["eslesmeyen"]} |',
        f'| Belirsiz | {s["belirsiz"]} |',
        f'| Pilot kaydında olmayan tablo satırı | {s["pilot_kaydinda_yok"]} |',
        f'| Aynı satıra birden fazla kayıt | {s["ayni_satira_birden_fazla_kayit"]} |',
        f'| SQL önizleme satırı | {s["sql_onizleme_satiri"]} |',
        f'| import_records aktarım kaydı (`pilot_tablo_aktarim.json`, review_status yok) | {s["aktarim_kaydi"]} |',
        f'| …mudahale_turu dolu (yalnız ilaç: "arka plan tedavisi") | {s["sql_onizleme_satiri"] - s["sql_mudahale_turu_bos"]} |',
        f'| …mudahale_turu boş (aktarım yolu zorunlu tutmuyor; sorun değil) | {s["sql_mudahale_turu_bos"]} |',
        f'| …outcome_type boş | {s["sql_outcome_type_bos"]} |', '',
    ]
    for t in result['tablolar']:
        lines.append(f'- Table {t["tablo_no"]} (s.{t["sayfa"]}): {len(t["satirlar"])} satır; sütunlar: '
                     + ' · '.join(f'`{c["anahtar"]}`' for c in t['sutunlar'])
                     + (f'; alt bölümler: {", ".join(t["alt_bolumler"])}' if t['alt_bolumler'] else ''))
    lines += ['', '## Eşleşmeyen / belirsiz kayıtlar', '']
    lines += [f'- v2_sira {r.get("v2_sira")} ({r["eslesme"]}): `{r.get("kanit_cumle")}`'
              for r in result['kayitlar'] if r['eslesme'] != 'eslesti'] or ['- Yok']
    lines += ['', '## Pilot kaydında olmayan tablo satırları', '']
    lines += [f'- Table {m["tablo_no"]} satır {m["satir_no"]} ({m["alt_bolum"] or "bölüm yok"}): {m["degisken"]} — `{m["ham"]}`'
              for m in result['pilot_kaydinda_yok']] or ['- Yok']
    if result['ayni_satira_birden_fazla_kayit']:
        lines += ['', '## Aynı satıra birden fazla kayıt', '']
        lines += [f'- {k}: v2_sira {v}' for k, v in result['ayni_satira_birden_fazla_kayit'].items()]
    pending = s['sql_mudahale_turu_bos_satirlar']
    lines += ['', '## mudahale_turu boş satırlar (bilgi amaçlı)', '',
              f'Aktarım yolu `src.pilot_clinical.import_records` `mudahale_turu`\'yu zorunlu tutmuyor; eski '
              f'`validate_row` yolu bu pilot için kullanılmıyor. Yalnız ilaç satırları "arka plan tedavisi"; aşağıdaki '
              f'{len(pending)} tablo satırı ({s["sql_mudahale_turu_bos"]} SQL satırı) boş kalır ve bu sorun değildir. '
              f'Grup ortalaması / oran ölçümleri tanı testi değerlendirmesi olmadığı için "tanı testi" atanmadı.', '',
              '| Tablo | Satır | Alt bölüm | Değişken | bilgi_turu (pilot) | v2_sira | SQL satırı |',
              '|---|---|---|---|---|---|---|']
    lines += [f'| {p["tablo_no"]} | {p["satir_no"]} | {p["alt_bolum"] or "—"} | {p["degisken"]} | {p["bilgi_turu"]} | '
              f'{p["pilot_v2_sira"]} | {p["sql_satiri"]} |' for p in pending]
    lines += ['', '## Sorunlu başlıklar (düzeltme yapılmadı)', '']
    for issue in s['sorunlu_basliklar']:
        part = issue.get('parca')
        lines.append(f'- Table {issue["tablo_no"]} (s.{issue["sayfa"]}) `{issue["tur"]}`'
                     + (f' — parça: `{json.dumps(part, ensure_ascii=False)}`' if part else '') + f'. {issue["aciklama"]}')
    lines += ['', '## SQL önizleme eşlemesi (klinik_bulgular, mevcut sütunlar)', '',
              '- Grup tablosu (Table 1): grup hücresi başına 1 satır. `hasta_grubu`=grup, `comparator`=diğer grup, `deger`=hücre, '
              '`p`=grup karşılaştırma p\'si, `unit` değişken adından. `etki_turu`: n (%) -> "oran / yuzde", diğerleri -> "diger"; '
              'biçim `taslak_notu.deger_bicimi`. `outcome_type`="çalışma popülasyonu".',
              '- İlaç satırları (Medications): `mudahale_en`=ilaç, `mudahale_turu`="arka plan tedavisi", '
              '`outcome_type`="uygulama oranı", `sonuc_olcutu_tr`="kullanım oranı", `doz`=NULL (kullanım oranı, doz değil).',
              '- İlaç dışı tüm satırlar (demografik, tanı, laboratuvar, eko, Table 2): `mudahale_turu` boş; '
              'aktarım yolu zorunlu tutmuyor.',
              '- Etki tablosu (Table 2): analiz başına 1 satır; `etki_turu`="OR", `deger`=OR, `ci95`, `p` ayrı; '
              'analiz türü `taslak_notu.analiz`; `hasta_grubu` NULL. `outcome_type` boş: retrospektif prediktör analizi '
              'için "prognostik değer" uygunluğundan emin olunamadı.',
              '- `hasta_sayisi` NULL ve `comparator` n\'siz (ekip kararı): n değerleri `taslak_notu.grup_n` / `comparator_n`.',
              '- Aktarım (`pilot_tablo_aktarim.json`): önizleme satırı başına bir kayıt; pilot kaydının `bilgi_turu`, `baglam`, '
              '`kanit_cumle`, `sayfa`, `kanit_turu` alanları aynen, `klinik_bilgi_en` temiz satır adı (ör. "CAD", '
              '"Age (years)"; ham tablo satırı değil) + dolu yapılandırılmış alanlar '
              '(`src.pilot_clinical.STRUCTURED_FIELDS`). `review_status` yok: manuel doğrulamadan sonra insan verir; '
              '`import_records` yalnız `verified` kayıt yazar.']
    return '\n'.join(lines) + '\n'


def run(pdf, findings=None):
    from src.pilot_clinical import pdf_pages
    doc, pages = pdf_pages(pdf)
    records, source = load_records(findings)
    result = enrich(records, pages, doc.get('document_id'))
    summary = summarize(result, source)
    return dict({'surum': VERSION, 'pdf': doc.get('filename'), 'document_id': doc.get('document_id'),
                 'rapor': summary}, **result)


def main():
    parser = argparse.ArgumentParser(description='Pilot tablo kayıtlarını başlıklarla eşle (deterministik, SQL yazmaz)')
    parser.add_argument('--pdf', required=True)
    parser.add_argument('--findings', help='Varsayılan: pilot_findings_v3_2.json, yoksa pilot_findings_v2.json, yoksa '
                             '02_zehra_klinik_incelemesi.csv')
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    parser.add_argument('--overwrite', action='store_true', help='Mevcut çıktıların üzerine yaz')
    args = parser.parse_args()
    output = Path(args.output)
    report = output.with_name(output.stem.replace('_zengin', '') + '_raporu.md')
    transfer = output.with_name(output.stem.replace('_zengin', '') + '_aktarim.json')
    if not args.overwrite and (output.exists() or report.exists() or transfer.exists()):
        raise FileExistsError(f'Mevcut çıktı var (--overwrite ile yazılır): {output} / {report} / {transfer}')
    result = run(args.pdf, args.findings)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report.write_text(report_markdown(result['rapor'], result), encoding='utf-8')
    transfer.write_text(json.dumps(result['aktarim'], ensure_ascii=False, indent=2), encoding='utf-8')
    s = result['rapor']
    print(f"Girdi: {s['girdi']} | Tablo: {s['tablo_sayisi']} ({s['tablo_veri_satiri']} satır) | "
          f"Tablo kaydı: {s['tablo_kaydi']} | Eşleşen: {s['eslesen']} | Eşleşmeyen: {s['eslesmeyen']} | "
          f"Belirsiz: {s['belirsiz']} | Pilot kaydında yok: {s['pilot_kaydinda_yok']} | "
          f"SQL önizleme: {s['sql_onizleme_satiri']} satır | Aktarım kaydı: {s['aktarim_kaydi']}")
    print(f'Çıktı: {output} | Rapor: {report} | Aktarım: {transfer} | SQL değişmedi')


if __name__ == '__main__':
    main()
