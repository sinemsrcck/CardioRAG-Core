"""Modeli çıkarılamayan (pilot_tablo: elle_inceleme_gerekli / literatur_tablosu) tablolar için deterministik modeller.

LLM / API / embedding yok. Makaleye, yazara ya da sayısal değere özel kural yok; her yapı kendi tanıma koşulları KESİN
sağlanırsa kayıt üretir, çözülemeyen hücre gerekçesiyle incelemeye ayrılır (tahminle eşleştirme yok).

Desteklenen genel yapılar:
  - calisma_satir:   satırları çalışma kimliği ("Yazar, yıl" / "Yazar et al, yıl" / "KISALTMA,<sup>n</sup>yıl"), sütunları
                     ölçüm olan çalışma özet tablosu (alt başlık satırı = popülasyon, iki satıra bölünmüş grup etiketli
                     hücre, "Kol: n" listeleri, "Kol: n (%) ... (HR, x [95% CI, a–b]; P = y)" birincil sonuç hücresi).
  - calisma_sutun:   çalışma kimliği sütun başlığında (yayılan başlık soldan doldurulur), satırları sonuç olan tablo.
  - oneri:           öneri sınıfı (COR / Class) + kanıt düzeyi (LOE / Level of evidence) + öneri metni; sınıfsız ön koşul
                     satırı bir sonraki öneriye bağlanır, alt başlık satırı kapsamdır.
  - test_listesi:    satırları test, sütunları tanısal ölçü (SEN / SPE ...) ve madde listesi (avantaj / sınırlılık) olan
                     tablo; madde işareti ayrı sütuna kaymışsa madde sayısı eşitliğiyle doğrulanır.
  - yan_yana:        aynı fiziksel satırlarda bağımsız bölümler (bölünmüş üst başlık grupları); dikey birleşik anahtar
                     (model no) blok döngüsüyle, sınıflandırma matrisi sınıf adı ve aritmetik tutarlılıkla doğrulanır.

Kayıtlar: kanıt = kaynaktaki fiziksel satır(lar)ın BİREBİR metni ('\\n' ile ardışık satırlar), klinik bilgi = satır
etiketi (kanıtta birebir), değer = hücre metninin birebir parçası. Çalışma kimliği, yazar, yıl, kaynakça no, ülke ve
çalışma özellikleri 'tablo_modeli.kaynak_calisma' içinde; hasta_grubu'na YAZILMAZ. Alt grup ve tedavi kolları anlamlarıyla
('alt_grup' / 'kol') notta; otomatik hasta grubu sayılmaz. 'literature_background' yalnız tablo aktarılan çalışma
tablosu olarak tanınmış VE satır / sütun çalışma kimliği doğrulanmışsa atanır.
"""
import re

import src.pilot_tablo as PT
from src.rag_anlam_denetimi import missing_marker

STATE = 'ozel'
HOLD_STAGE = 'tablo_ozel_inceleme'
REPLACED_STAGE = 'tablo_ozel_degisimi'
BLOB_STAGE = 'toplu_madde_tekrari'
LOE_STAGE = 'kanit_duzeyi_oneriye_baglandi'
LINK_KEY = 'tablo_iliskisi'

_BR = re.compile(r'<br\s*/?>', re.I)


def _c(text):
    """Karşılaştırma biçimi (pilot_tablo.clean + boşluk / tire sadeleştirme)."""
    return re.sub(r'\s+', ' ', PT.clean(str(text or '')).replace('–', '-').replace('—', '-')).strip().casefold()


def _clean(text):
    return re.sub(r'\s+', ' ', PT.clean(str(text or ''))).strip()


def _label(cell):
    """Başlık hücresi metni; dipnot işareti üst simgesi (<sup>a</sup>) atılır (rag_klinik_cikarim.FOOTNOTE_SUP)."""
    from src.rag_klinik_cikarim import FOOTNOTE_SUP
    return _clean(FOOTNOTE_SUP.sub('', str(cell or '')))


# ---------------------------------------------------------------------------
# Çalışma kimliği
# ---------------------------------------------------------------------------

_YEAR = r'(?:19|20)\d{2}'
STUDY_ID = re.compile(
    r'^(?P<ad>[^\W\d][^,]{0,60}?)\s*,\s*'                                     # ad / yazar / çalışma kısaltması
    r'(?:<sup>\s*(?P<ref1>\d+(?:\s*[,–-]\s*\d+)*)\s*</sup>\s*)?(?:<br\s*/?>\s*)?'
    r'(?P<yil>' + _YEAR + r')\s*'
    r'(?:_\(\s*(?P<ref2>\d+(?:\s*[,–-]\s*\d+)*)\s*\)_)?\s*'
    r'(?:<sup>\s*_?(?P<mark1>[a-z])_?\s*</sup>|(?P<mark2>[a-z]))?\s*$', re.I)
AUTHOR_HEADER = re.compile(r'^(?P<ad>[^\W\d][\w .\'-]{1,60}?\bet\s+al\.?)\s*(?:<sup>\s*\**\s*(?P<ref>\d+)\s*\**\s*</sup>)?\s*$',
                           re.I)


def parse_study(cell):
    """Hücreden çalışma kimliği {'etiket', 'ad', 'yil', 'kaynakca_no', 'dipnot_isareti'} ya da None."""
    raw = re.sub(r'\*\*', '', str(cell or '')).strip()
    m = STUDY_ID.match(raw)
    if m:
        ref = m.group('ref1') or m.group('ref2')
        return {'etiket': _clean(raw), 'ad': _clean(m.group('ad')), 'yil': m.group('yil'),
                'kaynakca_no': re.findall(r'\d+', ref) if ref else [],
                'dipnot_isareti': m.group('mark1') or m.group('mark2')}
    m = AUTHOR_HEADER.match(raw)
    if m:
        return {'etiket': _clean(raw), 'ad': _clean(m.group('ad')), 'yil': None,
                'kaynakca_no': [m.group('ref')] if m.group('ref') else [], 'dipnot_isareti': None}
    return None


# ---------------------------------------------------------------------------
# Tablo bloğu, başlık, dipnot ve kısaltma sözlüğü
# ---------------------------------------------------------------------------

def _block(lines, rows):
    """(başlangıç, ayraç, bitiş) satır indeksleri. rows: tablonun ham satır dizisi; dizi sayfada TEKİL bir konumda
    ardışık geçmelidir (aynı başlık satırını paylaşan iki tablo karışmaz); tekil değilse None."""
    rows = [r.strip() for r in rows]
    hits = [i for i in range(len(lines) - len(rows) + 1)
            if all(lines[i + k].strip() == rows[k] for k in range(len(rows)))]
    if len(hits) != 1:
        return None
    at = hits[0]
    start = at
    while start > 0 and lines[start - 1].strip().startswith('|'):
        start -= 1
    end = at
    while end < len(lines) and lines[end].strip().startswith('|'):
        end += 1
    sep = next((i for i in range(start, end) if PT.SEPARATOR.match(lines[i].strip())), None)
    return (start, sep, end) if sep is not None else None


def _bold_row(cells):
    nonempty = [c.strip() for c in cells if PT.clean(c)]
    return bool(nonempty) and all(c.startswith(('**', '__')) for c in nonempty)


GLOSSARY_ENTRY = re.compile(r'(?:^|[;.]\s*)(?P<k>[A-Za-z0-9][\w\-–()/ ]{0,24}?)\s*[:,]\s*(?P<v>[^;:]{2,120}?)(?=;|\.\s|\.$|$)')
FOOTNOTE_LINE = re.compile(r'^\s*_?(?P<m>[a-z])\s?(?P<t>[A-Z][^\n]{5,})$')
MISSING_MEANING = re.compile(r'^(?:not\s+(?:reported|available|applicable|done)|unknown|unavailable|missing)\b', re.I)


def notes_below(lines, end, limit=8):
    """Tablonun altındaki dipnot / kısaltma satırları: ({kısaltma: açılım}, {işaret: metin}, ham satırlar)."""
    glossary, marks, raw = {}, {}, []
    for line in lines[end:end + limit]:
        text = _clean(line).strip('_ ')
        if not text:
            continue
        if line.strip().startswith('|') or PT.CAPTION.match(line) or PT.CAPTION_BOLD.match(line):
            break
        raw.append(line)
        body = re.sub(r'^\s*(?:Abbreviations?|Abbr\.)\s*:\s*', '', text, flags=re.I)
        for part in re.split(r'(?<=\.)\s+(?=[a-z][A-Z])', body):
            fm = FOOTNOTE_LINE.match(part)
            if fm and not re.match(r'^[a-z][A-Z]{1,}[,:]', part):
                marks[fm.group('m')] = fm.group('t').strip()
                continue
            for entry in re.split(r';\s*|\.\s+(?=[A-Z0-9])', part):   # girdiler ';' ya da '. ' ile ayrılır
                m = re.match(r'^\s*(?P<k>[^:;,]{1,30}?)\s*(?:\((?P<sym>[^)]{1,4})\))?\s*[:,]\s*(?P<v>.{2,160}?)\.?\s*$', entry)
                if m:
                    key = (m.group('sym') or m.group('k')).strip()
                    glossary[key] = m.group('v').strip()
    return glossary, marks, raw


def table_missing_markers(glossary):
    """Tabloda 'bildirilmemiş' anlamında tanımlanan işaretler; tabloya göre eksik değer OLMAYAN işaretler."""
    missing = {k for k, v in glossary.items() if MISSING_MEANING.match(v)}
    not_missing = {k for k, v in glossary.items() if not MISSING_MEANING.match(v)}
    return missing, not_missing


def is_missing(cell, glossary):
    """Hücre bildirilmemiş mi: tablo sözlüğü önceliklidir (ör. 'NA: North African' -> eksik değil)."""
    text = _clean(cell)
    if not text:
        return False
    missing, not_missing = table_missing_markers(glossary)
    if text in not_missing:
        return False
    return text in missing or bool(missing_marker(cell))


# ---------------------------------------------------------------------------
# Mantıksal tablolar (devam sayfaları birleşik)
# ---------------------------------------------------------------------------

def logical_tables(pages):
    """pilot_tablo'nun modelleyemediği tablolar; devam sayfası (tablo_devami) önceki parçaya bağlanır."""
    out = []
    for t in PT.parse_tables(pages):
        if not t.get('elle_inceleme_satirlari'):
            continue
        lines = pages[t['sayfa']].splitlines()
        block = _block(lines, t['elle_inceleme_satirlari'])
        if block is None:
            continue
        start, sep, end = block
        kinds = {s['tur'] for s in t['sorunlar']}
        glossary, marks, raw_notes = notes_below(lines, end)
        segment = {'sayfa': t['sayfa'], 'lines': lines, 'start': start, 'sep': sep, 'end': end}
        if 'tablo_devami' in kinds and out and out[-1]['tablo_no'] == t['tablo_no'] \
                and out[-1]['segmentler'][-1]['sayfa'] in (t['sayfa'] - 1, t['sayfa']):
            head = out[-1]
            head['segmentler'].append(segment)
            head['sozluk'].update(glossary)
            head['dipnotlar'].update(marks)
            head['literatur'] = head['literatur'] or 'literatur_tablosu' in kinds
            continue
        out.append({'tablo_no': t['tablo_no'], 'baslik': t.get('tablo_basligi'), 'segmentler': [segment],
                    'literatur': 'literatur_tablosu' in kinds, 'sozluk': glossary, 'dipnotlar': marks,
                    'sorunlar': sorted(kinds)})
    return out


def _header(segment):
    """(grup başlık satırları, özellik satırları, etiket satırı (idx, hücreler), ilk veri satırı indeksi)."""
    lines, start, sep, end = segment['lines'], segment['start'], segment['sep'], segment['end']
    groups = [(i, PT.split_cells(lines[i])) for i in range(start, sep)]
    after = list(range(sep + 1, min(end, sep + 4)))
    bold = [i for i in after if _bold_row(PT.split_cells(lines[i]))]
    if bold:
        label = bold[-1]
        props = [(i, PT.split_cells(lines[i])) for i in range(sep + 1, label)]
        return groups, props, (label, PT.split_cells(lines[label])), label + 1
    if groups:
        label = groups[-1]
        return groups[:-1], [], label, sep + 1
    return [], [], None, sep + 1


def _group_spans(cells, page_text):
    """Bölünmüş üst başlık hücrelerinden (sütun indeksi -> grup adı). Kelime ortasından bölünen parçalar birleşir;
    boşluklu / boşluksuz birleşimden sayfada geçen tercih edilir. Boş hücre soldaki grubu sürdürür."""
    names, spans, current = [], {}, None
    low_page = _c(page_text)
    for index, cell in enumerate(cells):
        text = _clean(cell)
        if text:
            if current is not None and names and re.search(r'[A-Za-z]$', names[current]) and re.match(r'^[a-z]', text):
                joined, spaced = names[current] + text, names[current] + ' ' + text
                names[current] = spaced if _c(spaced) in low_page and _c(joined) not in low_page else joined
            else:
                names.append(text)
                current = len(names) - 1
        if current is not None:
            spans[index] = current
    return {i: names[g] for i, g in spans.items()}, {i: g for i, g in spans.items()}


def _data_rows(segment, first):
    """[(satır indeksleri, hücre listeleri)] — ilk hücresi boş ve dolu hücreleri yalnız parantezli niteleyici ya da
    'değer (niteleyici)' olan satır, önceki satırın DEVAMIDIR (aynı mantıksal satır)."""
    lines = segment['lines']
    rows = []
    for i in range(first, segment['end']):
        cells = PT.split_cells(lines[i])
        if not any(PT.clean(c) for c in cells):
            continue
        filled = [PT.clean(c) for c in cells[1:] if PT.clean(c)]
        continuation = (rows and not PT.clean(cells[0]) and filled
                        and all(re.fullmatch(r'(?:[-\d.,]+\s*)?\(\s*[A-Za-z][\w .-]{0,24}\)\s*;?', f) for f in filled))
        if continuation:
            rows[-1][0].append(i)
            rows[-1][1].append(cells)
        else:
            rows.append(([i], [cells]))
    return rows


def _merged(cells_list, index):
    """Mantıksal satırda bir sütunun ham parçaları (üst + devam satırları)."""
    return [c[index] for c in cells_list if index < len(c) and PT.clean(c[index])]


# ---------------------------------------------------------------------------
# Hücre değer biçimleri (değer metni kaynak hücrenin BİREBİR parçası)
# ---------------------------------------------------------------------------

_SP = r'(?:\s|<br\s*/?>)*'
_V = r'-?\d+(?:[.,]\d+)?\s*%?'
GROUPED = re.compile(r'^\s*(?P<v1>' + _V + r')\s*\(\s*(?P<l1>[A-Za-z][\w .-]{0,24}?)\s*\)\s*;' + _SP +
                     r'(?P<v2>' + _V + r')' + _SP + r'\(\s*(?P<l2>[A-Za-z][\w .-]{0,24}?)\s*\)\s*$')
PLAIN = re.compile(r'^\s*(?P<v>' + _V + r')\s*$')
STAT_PREFIX = re.compile(r'^\s*(?P<s>Median|Mean)\s*,' + _SP + r'(?P<v>' + _V + r')\s*$', re.I)
ARM = re.compile(r'(?P<l>[A-Z][A-Za-z0-9 /+\-]{0,30}?)\s*:' + _SP + r'(?P<v>\d[\d,]*)(?=' + _SP + r'(?:[A-Z]|$))')
EFFECT_TAIL = re.compile(r'\(\s*(?P<e>HR|OR|RR)\s*,?\s*(?P<v>\d*\.?\d+)\s*\[\s*95%\s*CI\s*,\s*(?P<lo>\d*\.?\d+)\s*[–-]\s*'
                         r'(?P<hi>\d*\.?\d+)\s*\]\s*;\s*P\s*(?P<op>[=<>])\s*(?P<p>\d*\.?\d+)\s*\)\s*$', re.I)
P_TAIL = re.compile(r'\(\s*P\s*(?P<op>[=<>])\s*(?P<p>\d*\.?\d+)\s*\)\s*$', re.I)
ARM_RESULT = r'\d[\d,]*' + _SP + r'\(\s*\d+(?:\.\d+)?%\s*\)|\d+(?:\.\d+)?%'


def _raw_find(raw, cleaned_value):
    """Temiz değerin ham hücredeki birebir karşılığı (araya <br> girebilir); yoksa None."""
    if not cleaned_value:
        return None
    if cleaned_value in raw:
        return cleaned_value
    pattern = _SP.join(re.escape(tok) for tok in re.findall(r'\S', cleaned_value.replace(' ', '')))
    m = re.search(pattern, raw)
    return m.group(0) if m else None


def parse_value_cell(raw, glossary):
    """[(değer (ham), {'alt_grup': {...}} / {} / {'istatistik': ..})] ya da None (biçim tekil değil). Eksik -> []."""
    text = re.sub(r'\s+', ' ', _BR.sub(' ', PT.clean(raw))).strip()
    if is_missing(raw, glossary):
        return []
    m = PLAIN.match(text)
    if m:
        return [(_raw_find(raw, m.group('v').strip()) or m.group('v').strip(), {})]
    m = STAT_PREFIX.match(text)
    if m:
        return [(_raw_find(raw, m.group('v').strip()), {'istatistik': 'medyan' if m.group('s').lower() == 'median'
                                                         else 'ortalama'})]
    m = GROUPED.match(text)
    if m:
        out = []
        for v, label in ((m.group('v1'), m.group('l1')), (m.group('v2'), m.group('l2'))):
            found = _raw_find(raw, v.strip())
            if not found:
                return None
            out.append((found, {'alt_grup': {'etiket': label.strip(), 'acilim': glossary.get(label.strip())}}))
        return out
    return None


def parse_arms(raw):
    """'Ablation: 33 Control: 37' -> [(kol, değer)] (en az iki kol, metnin tamamı kol listesi) ya da None."""
    text = re.sub(r'\s+', ' ', _BR.sub(' ', PT.clean(raw))).strip()
    arms = [(m.group('l').strip(), m.group('v')) for m in ARM.finditer(text)]
    rebuilt = ' '.join(f'{l}: {v}' for l, v in arms)
    if len(arms) < 2 or re.sub(r'\s+', ' ', rebuilt) != text:
        return None
    return arms


def parse_outcome_cell(raw, arm_labels, control_labels):
    """Birincil sonuç hücresi: 'Sonuç adı Kol1: n (%) Kol2: n (%) (HR, x [95% CI, a–b]; P = y)'. Kol adları satırın
    katılımcı sütunundaki kollarla (ya da kontrol sütunundaki adla) aynı olmalı. Dönen: {'sonuc', 'kollar': [(kol,
    değer)], 'etki': {...} | None, 'p': ...} ya da None."""
    text = re.sub(r'\s+', ' ', _BR.sub(' ', PT.clean(raw))).strip()
    effect = EFFECT_TAIL.search(text)
    tail = effect or P_TAIL.search(text)
    body = text[:tail.start()].strip() if tail else text
    labels = sorted({*arm_labels, *control_labels}, key=len, reverse=True)
    if not labels:
        return None
    label_re = '|'.join(re.escape(l) for l in labels)
    pieces = list(re.finditer(r'(?<![\w/])(?P<l>' + label_re + r')\s*:\s*', body))
    if len(pieces) < 2:
        return None
    outcome = body[:pieces[0].start()].strip()
    arms = []
    for k, m in enumerate(pieces):
        stop = pieces[k + 1].start() if k + 1 < len(pieces) else len(body)
        value = body[m.end():stop].strip()
        if not re.fullmatch(r'\d[\d,]*\s*\(\s*\d+(?:\.\d+)?%\s*\)|\d+(?:\.\d+)?%', value):
            return None                                        # birden çok ölçü / serbest metin: tekil değil
        arms.append((m.group('l'), value))
    if not outcome or len({a for a, _ in arms}) != len(arms):
        return None
    result = {'sonuc': outcome, 'kollar': arms, 'etki': None, 'p': None, 'p_op': None}
    if tail:
        result['p'], result['p_op'] = tail.group('p'), tail.group('op')
    if effect:
        result['etki'] = {'tur': effect.group('e').upper(), 'deger': effect.group('v'),
                          'ci95': f"{effect.group('lo')}–{effect.group('hi')}"}
    return result


# ---------------------------------------------------------------------------
# Kayıt kurucu
# ---------------------------------------------------------------------------

def _kanit(segment, idxs):
    return '\n'.join(segment['lines'][i] for i in idxs)


def _base(table, segment, idxs, fact, kind, context, model, **fields):
    rec = {'bilgi_turu': kind, 'baglam': context, 'klinik_bilgi_en': fact, 'kanit_cumle': _kanit(segment, idxs),
           'sayfa': segment['sayfa'], 'kanit_turu': 'tablo'}
    rec.update({k: v for k, v in fields.items() if v not in (None, '')})
    rec['tablo_modeli'] = {'durum': STATE, 'tablo_no': table['tablo_no'], 'tablo_basligi': _clean(table['baslik']),
                           'satir_indeksleri': idxs, **{k: v for k, v in model.items() if v not in (None, '', [], {})}}
    return rec


def _hold(segment, idxs, reason, table, **extra):
    return {'sayfa': segment['sayfa'], 'asama': HOLD_STAGE, 'neden': reason, 'tablo_no': table['tablo_no'],
            'kanit_cumle': _kanit(segment, idxs), **extra}


def _unit_of(label):
    """Başlıktaki birim: 'Males, %' -> '%', 'Median age, years' -> 'years', 'Length of follow-up, mo' -> 'mo'."""
    m = re.search(r',\s*(%|years?|y|mo|months?|days?|weeks?)\s*$', label, re.I)
    if m:
        return m.group(1)
    return '%' if re.search(r'%\s*$', label) else None


def _stat_of(label):
    low = label.casefold()
    if re.search(r'\bmedian\b', low):
        return 'medyan'
    if re.search(r'\bmean\b', low):
        return 'ortalama'
    if re.match(r'^no\.|\bnumber\b|\bn\b', low):
        return 'n'
    return None


def _effect_of(label):
    low = label.casefold()
    for pat, code in ((r'relative risk|\brr\b', 'RR'), (r'hazard ratio|\bhr\b', 'HR'), (r'odds ratio|\bor\b', 'OR')):
        if re.search(pat, low):
            return code
    if '%' in label:
        return 'oran / yuzde'
    return 'diger'


# ---------------------------------------------------------------------------
# 1. Satır yönelimli çalışma özeti
# ---------------------------------------------------------------------------

DESCRIPTOR_HEADER = re.compile(r'country|setting|region|design|enrol+ment|period|inclusion|exclusion|control|'
                               r'population|ülke|tasarım', re.I)
OUTCOME_HEADER = re.compile(r'outcome|end\s*point|result', re.I)
ARM_HEADER = re.compile(r'participants|patients|subjects|\bn\b|no\.', re.I)


def study_rows(table, pages):
    seg0 = table['segmentler'][0]
    _, _, label, first = _header(seg0)
    if label is None:
        return None
    labels = [_label(c) for c in label[1]]
    rows = []
    for k, seg in enumerate(table['segmentler']):
        if k == 0:
            rows += [(seg, r) for r in _data_rows(seg, first)]
        else:
            _, _, lab, f = _header(seg)
            if lab is None or [_label(c) for c in lab[1]] != labels:
                return None                                    # devam sayfasında başlık kapsamı farklı: güvenli değil
            rows += [(seg, r) for r in _data_rows(seg, f)]
    studies = [parse_study(cells[0][0]) for _, (_, cells) in rows if len([c for c in cells[0] if PT.clean(c)]) > 1]
    if not studies or sum(s is not None for s in studies) < max(2, 0.6 * len(studies)):
        return None
    glossary, marks = table['sozluk'], table['dipnotlar']
    records, holds, section = [], [], None
    for seg, (idxs, cells) in rows:
        top = cells[0]
        if PT.clean(top[0]) and not any(PT.clean(c) for c in top[1:]):
            section = _clean(top[0])
            continue
        study = parse_study(top[0])
        if study is None:
            holds.append(_hold(seg, idxs, 'çalışma kimliği ayrıştırılamadı', table))
            continue
        if any(len(c) != len(labels) for c in cells):
            # Hücre sayısı başlıkla uyuşmuyorsa sütun ilişkisi kayabilir: değer sırayla dağıtılmaz.
            holds.append(_hold(seg, idxs, 'hücre sayısı başlıkla uyuşmuyor; sütun ilişkisi doğrulanamıyor', table))
            continue
        literature = table['literatur'] and bool(study.get('yil'))
        if study.get('dipnot_isareti') and study['dipnot_isareti'] in marks:
            study['dipnot'] = marks[study['dipnot_isareti']]
        descriptors, value_cols = {}, []
        for j in range(1, len(labels)):
            parts = _merged(cells, j)
            if not parts:
                continue
            if DESCRIPTOR_HEADER.search(labels[j]) and not OUTCOME_HEADER.search(labels[j]):
                descriptors[labels[j]] = _clean(' '.join(parts))
            else:
                value_cols.append(j)
        study['ozellikler'] = descriptors
        arm_labels, control_labels = [], []
        control = next((v for h, v in descriptors.items() if re.search(r'control', h, re.I)), None)
        if control:
            control_labels.append(control)
        for j in value_cols:
            if ARM_HEADER.search(labels[j]):
                arms = parse_arms(' '.join(_merged(cells, j)))
                if arms:
                    arm_labels += [a for a, _ in arms]
        context = 'literature_background' if literature else None
        if context is None:
            holds.append(_hold(seg, idxs, 'aktarılan çalışma niteliği doğrulanamadı (tablo literatür tablosu olarak '
                                          'tanınmadı ya da çalışma yılı yok)', table))
            continue
        for j in value_cols:
            raw = ' <br> '.join(_merged(cells, j)) if len(_merged(cells, j)) > 1 else _merged(cells, j)[0]
            header = labels[j]
            model = {'tur': 'calisma_satir', 'sutun': header, 'sutun_no': j, 'kaynak_calisma': study,
                     'alt_bolum': section, 'degisken': header}
            produced = _study_cell(table, seg, idxs, study, header, raw, model, glossary, arm_labels, control_labels,
                                   control)
            if produced is None:
                holds.append(_hold(seg, idxs, f"hücre biçimi tekil ayrıştırılamadı (sütun '{header}')", table,
                                   hucre=_clean(raw)))
                continue
            records += produced
    return records, holds


def _study_cell(table, seg, idxs, study, header, raw, model, glossary, arm_labels, control_labels, control):
    fact = PT.split_cells(seg['lines'][idxs[0]])[0].strip()
    unit, stat = _unit_of(header), _stat_of(header)
    if OUTCOME_HEADER.search(header):
        parsed = parse_outcome_cell(raw, arm_labels, control_labels)
        if parsed is None:
            return None
        out = []
        outcome_raw = _raw_find(raw, parsed['sonuc'])
        if not outcome_raw:
            return None
        arm_p = _raw_find(raw, parsed['p']) if parsed['p'] and not parsed['etki'] else None
        for arm, value in parsed['kollar']:
            v = _raw_find(raw, value)
            if not v:
                return None
            role = 'kontrol' if arm in control_labels or arm.lower().startswith('control') else 'tedavi'
            # Etki büyüklüğü yoksa kollar arası karşılaştırmanın p'si kol kayıtlarında (grup satırı kuralı).
            out.append(_base(table, seg, idxs, fact, 'klinik_sonuc', 'literature_background',
                             {**model, 'kol': {'etiket': arm, 'rol': role, 'acilim': glossary.get(arm)},
                              'olcu': 'kol sonucu', 'p_isareti': parsed['p_op'] if arm_p else None},
                             deger=v, sonuc_olcutu_tr=parsed['sonuc'], p=arm_p,
                             etki_turu='oran / yuzde' if '%' in value else 'diger'))
        if parsed['etki']:
            treat = [a for a, _ in parsed['kollar'] if a not in control_labels and not a.lower().startswith('control')]
            comp = [a for a, _ in parsed['kollar'] if a in control_labels or a.lower().startswith('control')]
            v = _raw_find(raw, parsed['etki']['deger'])
            p = _raw_find(raw, parsed['p']) if parsed['p'] else None
            if not v or len(treat) != 1 or len(comp) != 1:
                return None
            out.append(_base(table, seg, idxs, fact, 'klinik_sonuc', 'literature_background',
                             {**model, 'olcu': 'etki büyüklüğü', 'p_isareti': parsed['p_op']}, deger=v,
                             ci95=parsed['etki']['ci95'], p=p, etki_turu=parsed['etki']['tur'],
                             sonuc_olcutu_tr=parsed['sonuc'], mudahale_en=treat[0], comparator=comp[0]))
        return out
    arms = parse_arms(raw) if ARM_HEADER.search(header) else None
    if arms:
        out = []
        for arm, value in arms:
            v = _raw_find(raw, value)
            if not v:
                return None
            role = 'kontrol' if arm in control_labels or arm.lower().startswith('control') else 'tedavi'
            out.append(_base(table, seg, idxs, fact, 'olcum', 'literature_background',
                             {**model, 'kol': {'etiket': arm, 'rol': role, 'acilim': glossary.get(arm)},
                              'istatistik': 'n'}, deger=v, sonuc_olcutu_tr=_clean(header)))
        return out
    values = parse_value_cell(raw, glossary)
    if values is None:
        return None
    out = []
    for v, extra in values:
        out.append(_base(table, seg, idxs, fact, 'olcum', 'literature_background',
                         {**model, **extra, 'istatistik': extra.get('istatistik') or stat}, deger=v,
                         unit=unit, sonuc_olcutu_tr=_clean(header), etki_turu='oran / yuzde' if unit == '%' else None))
    return out


# ---------------------------------------------------------------------------
# 2. Sütun yönelimli çalışma özeti
# ---------------------------------------------------------------------------

def study_columns(table, pages):
    if len(table['segmentler']) != 1:
        return None
    seg = table['segmentler'][0]
    groups, props, label, first = _header(seg)
    if label is None or not groups:
        return None
    page_text = pages[seg['sayfa']]
    group_cells = groups[0][1]
    names, _ = _group_spans(group_cells, page_text)
    study_cells = {i: parse_study(c) for i, c in enumerate(group_cells) if PT.clean(c)}
    if len([s for s in study_cells.values() if s]) < 2 or any(s is None for s in study_cells.values()):
        return None
    col_study = {}
    current = None
    for i in range(len(label[1])):
        if i in study_cells:
            current = study_cells[i]
        if i > 0:
            col_study[i] = current
    if any(v is None for v in col_study.values()):
        return None
    labels = [_label(c) for c in label[1]]
    for i, cells in props:                                     # 'Study design' gibi özellik satırları
        prop = _clean(cells[0])
        for j in range(1, len(cells)):
            if PT.clean(cells[j]) and j in col_study:
                col_study[j].setdefault('ozellikler', {}).setdefault(prop, [])
                if _clean(cells[j]) not in col_study[j]['ozellikler'][prop]:
                    col_study[j]['ozellikler'][prop].append(_clean(cells[j]))
    marks, glossary = table['dipnotlar'], table['sozluk']
    row_dim = labels[0]
    kind = 'klinik_sonuc' if re.search(r'outcome|end\s*point|sonuç', row_dim, re.I) else 'olcum'
    records, holds = [], []
    if not table['literatur']:
        return [], [_hold(seg, [first], 'aktarılan çalışma niteliği doğrulanamadı (literatür tablosu değil)', table)]
    for idxs, cells_list in _data_rows(seg, first):
        cells = cells_list[0]
        fact = cells[0].strip()
        outcome = _clean(cells[0])
        for j in range(1, len(labels)):
            if j >= len(cells) or not PT.clean(cells[j]) or is_missing(cells[j], glossary):
                continue
            raw = cells[j]
            mark = re.findall(r'<sup>\s*_?\**([a-z])\**_?\s*</sup>', raw)
            body = re.sub(r'<sup>\s*_?\**[a-z]\**_?\s*</sup>', '', raw).strip()
            header = labels[j]
            study = dict(col_study[j])
            model = {'tur': 'calisma_sutun', 'sutun': header, 'sutun_no': j, 'kaynak_calisma': study,
                     'degisken': outcome, 'olcu': header}
            if mark:
                model['dipnot'] = {m: marks.get(m) for m in mark}
            vr = PT.VAL_RANGE.match(_clean(body))
            ci = None
            if vr and re.search(r'95\s*%\s*ci', header, re.I):
                value, ci = vr.group(1), f'{vr.group(2)}–{vr.group(3)}'
                ci_raw = re.search(r'\(\s*(.+?)\s*\)', body)
                ci = ci_raw.group(1) if ci_raw else ci
            elif PT.PLAIN_NUMBER.match(_clean(body)) or vr:
                value = _clean(body)
            else:
                holds.append(_hold(seg, idxs, f"hücre biçimi tekil ayrıştırılamadı (sütun '{header}')", table))
                continue
            found = _raw_find(raw, value)
            if not found:
                holds.append(_hold(seg, idxs, 'değer hücrede birebir bulunamadı', table))
                continue
            records.append(_base(table, seg, idxs, fact, kind, 'literature_background', model, deger=found, ci95=ci,
                                 sonuc_olcutu_tr=outcome, etki_turu=_effect_of(header),
                                 unit=_unit_of(header)))
    return records, holds


# ---------------------------------------------------------------------------
# 3. Öneri tablosu (sınıf / kanıt düzeyi / öneri metni)
# ---------------------------------------------------------------------------

CLASS_HEADER = re.compile(r'^(?:cor|class(?:\s+of\s+recommendation)?|recommendation\s+class)$', re.I)
LEVEL_HEADER = re.compile(r'^(?:loe|level\s+of\s+evidence)$', re.I)
SENTENCE_END = re.compile(r'[.;:]\s*\)?\s*$')


def recommendation_table(table, pages):
    if len(table['segmentler']) != 1:
        return None
    seg = table['segmentler'][0]
    _, _, label, first = _header(seg)
    if label is None:
        return None
    labels = [_label(c) for c in label[1]]
    ci = [i for i, l in enumerate(labels) if CLASS_HEADER.match(l)]
    li = [i for i, l in enumerate(labels) if LEVEL_HEADER.match(l)]
    if len(ci) != 1 or len(li) != 1:
        return None
    ci, li = ci[0], li[0]
    text_cols = [i for i in range(len(labels)) if i not in (ci, li)]
    if len(text_cols) != 1:
        return None
    ti = text_cols[0]
    records, holds, section, prefix = [], [], None, None
    lines = seg['lines']
    for i in range(first, seg['end']):
        cells = PT.split_cells(lines[i])
        if len(cells) != len(labels):
            holds.append(_hold(seg, [i], 'hücre sayısı başlıkla uyuşmuyor', table))
            prefix = None
            continue
        cls, lvl, text = _clean(cells[ci]), _clean(cells[li]), _clean(cells[ti])
        if not cls and not lvl:
            if not text:
                continue
            nxt = PT.split_cells(lines[i + 1]) if i + 1 < seg['end'] else []
            next_has_class = len(nxt) == len(labels) and _clean(nxt[ci]) and _clean(nxt[li])
            next_continues = next_has_class and re.match(r'^(?:\s*<br\s*/?>\s*)*[a-z(]', nxt[ti].strip())
            if next_has_class and (text.endswith(',') or next_continues) and not SENTENCE_END.search(text):
                prefix = (i, text)                             # sınıfsız ön koşul: sonraki öneriye bağlanır
                continue
            if len(text.split()) <= 6 and not text.endswith(',') and not SENTENCE_END.search(text):
                section, prefix = text, None                   # alt başlık (kapsam)
                continue
            holds.append(_hold(seg, [i], 'sınıfsız öneri satırı ne alt başlık ne sonraki öneriye bağlı ön koşul', table))
            prefix = None
            continue
        if not cls or not lvl or not text:
            holds.append(_hold(seg, [i], 'öneri sınıfı / kanıt düzeyi / metin eksik', table))
            prefix = None
            continue
        idxs = [prefix[0], i] if prefix and prefix[0] == i - 1 else [i]
        fact = re.sub(r'^(?:\s*<br\s*/?>\s*)+', '', cells[ti].strip())
        model = {'tur': 'oneri', 'oneri_sinifi': cls, 'kanit_duzeyi': lvl, 'sinif_basligi': labels[ci],
                 'duzey_basligi': labels[li], 'metin_basligi': labels[ti], 'alt_bolum': section,
                 'oneri_on_kosulu': prefix[1] if len(idxs) == 2 else None, 'degisken': labels[ti]}
        records.append(_base(table, seg, idxs, fact, 'diger', 'recommendation', model, outcome_type='öneri'))
        prefix = None
    return records, holds


# ---------------------------------------------------------------------------
# 4. Test × tanısal ölçü × madde listesi
# ---------------------------------------------------------------------------

DIAG_HEADER = re.compile(r'^(?:sen|spe|sensitivity|specificity|ppv|npv|accuracy|auc)$', re.I)
BULLET = re.compile(r'^\s*_?[•·▪●]_?\s*$')


def _items(cell):
    """Madde listesi hücresi -> madde metinleri (ham); madde işareti yoksa <br> satırları. (maddeler, işaret sayısı)."""
    tokens = re.split(r'(<br\s*/?>)', cell, flags=re.I)        # ayraçlar korunur: madde metni hücrenin birebir dilimi
    parts = [(tokens[k], tokens[k + 1] if k + 1 < len(tokens) else '') for k in range(0, len(tokens), 2)]
    bullets = sum(1 for p, _ in parts if BULLET.match(p))
    items, current = [], []

    def flush():
        if current:
            text = ''.join(p + sep for p, sep in current[:-1]) + current[-1][0]
            items.append(text.strip())
    if bullets:
        for p, sep in parts:
            if BULLET.match(p):
                flush()
                current = []
            elif PT.clean(p):
                current.append((p, sep))
        flush()
        return items, bullets
    return [p.strip() for p, _ in parts if PT.clean(p)], 0


def test_list_table(table, pages):
    seg0 = table['segmentler'][0]
    _, _, label, first = _header(seg0)
    if label is None:
        return None
    labels = [_label(c) for c in label[1]]
    diag = [i for i, l in enumerate(labels) if DIAG_HEADER.match(l)]
    if not diag:
        return None
    records, holds = [], []
    for k, seg in enumerate(table['segmentler']):
        f = first
        if k:
            _, _, lab, f = _header(seg)
            if lab is None or [_label(c) for c in lab[1]] != labels:
                return None
        for idxs, cells_list in _data_rows(seg, f):
            cells = cells_list[0]
            if len(cells) != len(labels):
                holds.append(_hold(seg, idxs, 'hücre sayısı başlıkla uyuşmuyor', table))
                continue
            test = cells[0].strip()
            if not PT.clean(test):
                continue
            for j in diag:
                value = _clean(cells[j])
                if not value or is_missing(cells[j], table['sozluk']):
                    continue
                if not PT.PLAIN_NUMBER.match(value.rstrip('%')):
                    holds.append(_hold(seg, idxs, f"tanısal ölçü hücresi sayı değil ('{labels[j]}')", table))
                    continue
                model = {'tur': 'test_listesi', 'sutun': labels[j], 'sutun_no': j, 'degisken': _clean(test),
                         'tani_olcusu': table['sozluk'].get(labels[j], labels[j])}
                records.append(_base(table, seg, idxs, test, 'olcum', None, model, deger=_raw_find(cells[j], value),
                                     sonuc_olcutu_tr=labels[j], mudahale_en=_clean(test),
                                     outcome_type='tanısal performans', etki_turu='oran / yuzde'))
            j = 1
            while j < len(labels):
                if j in diag or not PT.clean(cells[j]):
                    j += 1
                    continue
                items, bullets = _items(cells[j])
                header = labels[j]
                if all(BULLET.match(p) for p in _BR.split(cells[j]) if PT.clean(p) or p.strip()):
                    # Madde işaretleri ayrı sütuna kaymış: metinler bir sonraki sütunda, sayı eşit olmalı.
                    nxt = j + 1
                    if nxt >= len(labels) or bullets == 0:
                        j += 1
                        continue
                    texts, _ = _items(cells[nxt])
                    header = labels[nxt] or header
                    if len(texts) != bullets:
                        holds.append(_hold(seg, idxs, f"madde işareti ({bullets}) ile madde metni ({len(texts)}) sayısı "
                                                      f"uyuşmuyor ('{header}')", table))
                        j += 2
                        continue
                    items, j = texts, j + 2
                else:
                    j += 1
                if not header:
                    holds.append(_hold(seg, idxs, 'madde listesi sütununun başlığı boş', table))
                    continue
                for n, item in enumerate(items, 1):
                    model = {'tur': 'test_listesi', 'liste_sutunu': header, 'madde_no': n, 'degisken': _clean(test),
                             'test': _clean(test)}
                    records.append(_base(table, seg, idxs, item, 'diger', None, model))
    return records, holds


# ---------------------------------------------------------------------------
# 5. Aynı fiziksel satırlarda yan yana bağımsız bölümler
# ---------------------------------------------------------------------------

PCT_HEADER = re.compile(r'percent|%|correct|accuracy', re.I)
OVERALL = re.compile(r'^(?:overall|total)\b', re.I)


def _num(text):
    t = _clean(text).rstrip('%').strip()
    try:
        return float(t)
    except ValueError:
        return None


def side_by_side(table, pages):
    if len(table['segmentler']) != 1:
        return None
    seg = table['segmentler'][0]
    groups, _, label, first = _header(seg)
    if label is None or not groups:
        return None
    # Bölünmüş üst başlıkta en az iki grup: aynı fiziksel satırda bağımsız bölüm işareti. Grup sınırları PDF
    # çıkarımında kayabildiği için bölümler VERİ türünden kurulur: metin sütunu + onu izleyen sayısal sütunlar.
    _, gid = _group_spans(groups[0][1], pages[seg['sayfa']])
    if len(set(gid.values())) < 2:
        return None
    labels = [_label(c) for c in label[1]]
    rows, footnotes = [], []
    for i in range(first, seg['end']):
        cells = PT.split_cells(seg['lines'][i])
        if PT.is_footnote_row(cells):
            footnotes.append(' '.join(_clean(c) for c in cells if PT.clean(c)))
            continue
        if len(cells) != len(labels):
            return [], [_hold(seg, [i], 'hücre sayısı başlıkla uyuşmuyor', table)]
        rows.append((i, cells))
    all_idx = [r[0] for r in rows]
    filled = [sum(1 for _, c in rows if PT.clean(c[j])) for j in range(len(labels))]
    if not rows or not 0 < filled[0] <= len(rows) / 2:
        return [], [_hold(seg, all_idx, 'bölümlerin ortak (dikey birleşik) anahtar sütunu yok', table)]
    kc = 0
    kinds = {}
    for j in range(1, len(labels)):
        vals = [_clean(c[j]) for _, c in rows if PT.clean(c[j])]
        nums = [_num(v) is not None for v in vals]
        kinds[j] = 'sayi' if vals and all(nums) else 'metin' if vals and not any(nums) else 'karisik'
    if 'karisik' in kinds.values() or kinds.get(1) != 'metin':
        return [], [_hold(seg, all_idx, 'sütun türleri (metin / sayı) tekil değil; bölümler ayrılamadı', table)]
    parts = []
    for j in range(1, len(labels)):
        if kinds[j] == 'metin':
            parts.append([j])
        else:
            parts[-1].append(j)
    pairs = [s for s in parts if len(s) == 2]
    matrices = [s for s in parts if len(s) >= 3]
    if len(parts) < 2 or len(matrices) != 1 or len(pairs) + len(matrices) != len(parts):
        return [], [_hold(seg, all_idx, 'bağımsız bölümler tekil biçimde ayrılamadı', table)]
    lc, matrix_sec = matrices[0][0], matrices[0][1:]
    pair_sec = pairs[0] if len(pairs) == 1 else None
    names = {j: labels[s[0]] for s in parts for j in s}
    # Bloklar: etiket bölümündeki satır etiketleri döngüsü 'Overall/Total' ile biter; her blokta TEK anahtar.
    blocks, current = [], []
    for i, cells in rows:
        current.append((i, cells))
        if OVERALL.match(_clean(cells[lc])):
            blocks.append(current)
            current = []
    reasons = []
    if current or not blocks:
        reasons.append('blok döngüsü Overall/Total satırıyla kapanmıyor')
    cycles = {tuple(_clean(c[lc]) for _, c in b) for b in blocks}
    if len(cycles) != 1:
        reasons.append('bloklardaki gerçek sınıf etiketleri aynı döngüde değil')
    keys = []
    for b in blocks:
        ks = [_clean(c[kc]) for _, c in b if PT.clean(c[kc])]
        if len(ks) != 1:
            reasons.append('blokta model anahtarı tekil değil')
        keys.append(ks[0] if len(ks) == 1 else None)
    if len(set(keys)) != len(keys):
        reasons.append('model anahtarları tekil değil')
    if reasons:
        return [], [_hold(seg, [r[0] for r in rows], '; '.join(sorted(set(reasons))), table)]
    pred_cols = [j for j in matrix_sec if not PCT_HEADER.search(labels[j])]
    pct_cols = [j for j in matrix_sec if PCT_HEADER.search(labels[j])]
    records, holds = [], []
    for key, block in zip(keys, blocks):
        ok, why = _verify_matrix(block, lc, pred_cols, pct_cols, labels)
        idx_all = [i for i, _ in block]
        if not ok:
            holds.append(_hold(seg, idx_all, f'sınıflandırma matrisi doğrulanamadı: {why}', table, tahmin_modeli=key))
        else:
            for i, cells in block:
                actual = cells[lc].strip()
                overall = bool(OVERALL.match(_clean(actual)))
                for j in matrix_sec:
                    value = _clean(cells[j])
                    if not value:
                        continue
                    if j in pct_cols:
                        measure = 'genel doğru sınıflandırma yüzdesi' if overall else 'doğru sınıflandırma yüzdesi'
                    else:
                        measure = 'tahmin edilen sınıf yüzdesi' if overall else 'sınıflandırılan hasta sayısı'
                    model = {'tur': 'yan_yana', 'bolum': names.get(j), 'tahmin_modeli': key, 'gercek_sinif': _clean(actual),
                             'tahmin_sinifi': None if j in pct_cols else labels[j], 'olcut': measure,
                             'orneklem': labels[lc], 'sutun': labels[j], 'degisken': _clean(actual),
                             'dipnot': footnotes or None}
                    records.append(_base(table, seg, [i], actual, 'olcum', 'study_finding', model,
                                         deger=_raw_find(cells[j], value), sonuc_olcutu_tr=labels[j]))
        if pair_sec:
            vc, nc = pair_sec[0], pair_sec[1]
            imp = [(i, c) for i, c in block if PT.clean(c[vc])]
            vals = [_num(c[nc]) for _, c in imp]
            normalized = re.search(r'normali[sz]ed', labels[nc], re.I)
            if any(v is None for v in vals) or (normalized and max(vals) != 100.0):
                holds.append(_hold(seg, [i for i, _ in imp], 'değişken önemi bölümü doğrulanamadı (normalleştirilmiş '
                                                           'önemde en büyük değer 100 değil ya da sayı değil)', table))
                continue
            for i, cells in imp:
                model = {'tur': 'yan_yana', 'bolum': names.get(vc), 'tahmin_modeli': key, 'olcut': labels[nc],
                         'sutun': labels[nc], 'degisken': _clean(cells[vc]), 'dipnot': footnotes or None}
                records.append(_base(table, seg, [i], cells[vc].strip(), 'olcum', 'study_finding', model,
                                     deger=_raw_find(cells[nc], _clean(cells[nc])), sonuc_olcutu_tr=labels[nc]))
    return records, holds


def _verify_matrix(block, lc, pred_cols, pct_cols, labels):
    """Gerçek sınıf adı tahmin sütunlarından TEK birinin adıyla bitmeli; satır yüzdesi = doğru / satır toplamı;
    genel satır = sütun toplamı / genel toplam ve genel doğru yüzdesi. Tolerans 0.1 puan."""
    if len(pct_cols) != 1 or len(pred_cols) < 2:
        return False, 'tahmin sütunları / yüzde sütunu tekil değil'
    pc = pct_cols[0]
    class_rows, overall = [], None
    for _, cells in block:
        name = _clean(cells[lc])
        if OVERALL.match(name):
            overall = cells
            continue
        own = [j for j in pred_cols if name.casefold().endswith(labels[j].casefold())]
        if len(own) != 1:
            return False, f"gerçek sınıf '{name}' tahmin sınıflarından birine tekil bağlanamadı"
        counts = [_num(cells[j]) for j in pred_cols]
        pct = _num(cells[pc])
        if any(v is None for v in counts) or pct is None or sum(counts) == 0:
            return False, 'sayılar okunamadı'
        if abs(_num(cells[own[0]]) / sum(counts) * 100 - pct) > 0.1:
            return False, f"'{name}' doğru sınıflandırma yüzdesi sayılarla tutarsız"
        class_rows.append((own[0], counts))
    if overall is None or not class_rows:
        return False, 'genel satır yok'
    total = sum(sum(c) for _, c in class_rows)
    for k, j in enumerate(pred_cols):
        col = sum(c[k] for _, c in class_rows)
        if _num(overall[j]) is None or abs(col / total * 100 - _num(overall[j])) > 0.1:
            return False, f"genel satır '{labels[j]}' yüzdesi sütun toplamıyla tutarsız"
    correct = sum(c[pred_cols.index(j)] for j, c in class_rows)
    if _num(overall[pc]) is None or abs(correct / total * 100 - _num(overall[pc])) > 0.1:
        return False, 'genel doğru sınıflandırma yüzdesi tutarsız'
    return True, None


# ---------------------------------------------------------------------------
# Öneri tablosunda kanıt düzeyinin öneriye bağlanması (modeli olmayan, kayıtları kabul edilmiş tablolar)
# ---------------------------------------------------------------------------

def link_evidence_levels(table, records, rejected):
    """'Level of evidence' sütunu olan tabloda kanıt düzeyini taşıyan kayıt, aynı satırın öneri / sonuç kayıtlarına
    'tablo_iliskisi' olarak bağlanır (kanıt düzeyi klinik ölçüm değildir). Satırda başka kayıt yoksa kayıt kalır."""
    seg = table['segmentler'][0]
    _, _, label, first = _header(seg)
    if label is None:
        return records
    labels = [_label(c) for c in label[1]]
    li = [i for i, l in enumerate(labels) if LEVEL_HEADER.match(l)]
    ri = [i for i, l in enumerate(labels) if re.match(r'^recommendation', l, re.I)]
    if len(li) != 1:
        return records
    li = li[0]
    from src.rag_anlam_denetimi import split_citations
    drop = set()
    for idxs, cells_list in _data_rows(seg, first):
        line = seg['lines'][idxs[0]]
        cells = cells_list[0]
        if li >= len(cells):
            continue
        level, refs = split_citations(re.sub(r'\*\*', '', cells[li]).strip())   # üst simge kaynakça ham metinde
        level = _clean(level)
        level_raw = cells[li].strip()
        on_row = [k for k, r in enumerate(records) if r.get('sayfa') == seg['sayfa']
                  and str(r.get('kanit_cumle') or '').strip() == line.strip()]
        if not on_row or not level:
            continue
        link = {'tur': 'oneri', 'kanit_duzeyi': level, 'kanit_duzeyi_ham': level_raw, 'duzey_basligi': labels[li],
                'kaynakca_numaralari': refs or None, 'tablo_no': table['tablo_no'],
                'oneri_metni': _clean(cells[ri[0]]) if ri and ri[0] < len(cells) else None}
        holders = [k for k in on_row if _c(records[k].get('deger')) in (_c(level), _c(level_raw))]
        others = [k for k in on_row if k not in holders]
        for k in on_row:
            records[k] = {**records[k], LINK_KEY: {kk: vv for kk, vv in link.items() if vv not in (None, '', [])}}
        if others:
            for k in holders:
                drop.add(k)
                rejected.append({'sayfa': records[k].get('sayfa'), 'asama': LOE_STAGE, 'kayit': records[k],
                                 'neden': f"kanıt düzeyi ({level}) aynı satırın öneri / sonuç kayıtlarına "
                                          f"tablo_iliskisi.kanit_duzeyi olarak bağlandı (klinik ölçüm değil)"})
    return [r for k, r in enumerate(records) if k not in drop]


# ---------------------------------------------------------------------------
# Hat aşaması
# ---------------------------------------------------------------------------

HANDLERS = (('calisma_satir', study_rows), ('calisma_sutun', study_columns), ('oneri', recommendation_table),
            ('test_listesi', test_list_table), ('yan_yana', side_by_side))
TYPE_FROM_LLM = ('oneri', 'test_listesi')
COMPARE_FIELDS = ('bilgi_turu', 'baglam', 'klinik_bilgi_en', 'deger', 'unit', 'hasta_grubu', 'sonuc_olcutu_tr',
                  'etki_turu', 'ci95', 'p', 'comparator', 'mudahale_en', 'outcome_type')


def build(pages):
    """[(mantıksal tablo, tür, kayıtlar, inceleme)] — hiçbir yapı uymayan tablo listede tür None ile."""
    out = []
    for table in logical_tables(pages):
        done = None
        for name, handler in HANDLERS:
            result = handler(table, pages)
            if result is not None:
                done = (table, name, result[0], result[1])
                break
        out.append(done or (table, None, [], []))
    return out


def _page(value):
    return int(value) if isinstance(value, str) and value.strip().isdigit() else value


def _rows_of(table):
    return {(seg['sayfa'], _c(seg['lines'][i])) for seg in table['segmentler'] for i in range(seg['start'], seg['end'])}


def _cell_texts(det):
    model = det['tablo_modeli']
    lines = det['kanit_cumle'].split('\n')
    texts = {_c(det.get('deger'))} if det.get('deger') else set()
    j = model.get('sutun_no')
    if j is not None:
        parts = [PT.split_cells(l)[j] for l in lines if j < len(PT.split_cells(l))]
        texts |= {_c(p) for p in parts if PT.clean(p)}
        texts.add(_c(' '.join(parts)))
    return {t for t in texts if t}


def equivalent(llm, det):
    """LLM kaydı deterministik kaydın klinik anlamını ve kaynak ilişkisini taşıyor mu: aynı sayfa, LLM kanıt satırı
    deterministik kanıtın bir satırı, aynı hücre (değer ya da bütün hücre metni) ve — LLM'de varsa — uyumlu sütun /
    sonuç etiketi. Değersiz kayıtta bilgi metni (ya da LLM'in değer alanına yazdığı metin) aynı olmalı."""
    if str(llm.get('sayfa')) != str(det.get('sayfa')):           # ön elemedeki ham LLM kaydında sayfa metin olabilir
        return False
    rows = {_c(l) for l in det['kanit_cumle'].split('\n')}
    if _c(llm.get('kanit_cumle')) not in rows:
        return False
    model = det['tablo_modeli']
    if det.get('deger'):
        if _c(llm.get('deger')) not in _cell_texts(det):
            return False
        label = _c(llm.get('sonuc_olcutu_tr') or llm.get('hasta_grubu'))
        allowed = {_c(det.get('sonuc_olcutu_tr')), _c(model.get('sutun')), _c(model.get('olcu')),
                   _c((model.get('alt_grup') or {}).get('etiket')), _c((model.get('kol') or {}).get('etiket'))}
        return not label or label in allowed
    fact = _c(det.get('klinik_bilgi_en'))
    return fact in (_c(llm.get('klinik_bilgi_en')), _c(llm.get('deger')))


def row_identity(rec, line_ids):
    """(tablo no, satır indeksi) ya da None. Kimlik kaynak tablodan; belirsiz ya da çelişkili ise None."""
    if rec.get('kanit_turu') != 'tablo':
        return None
    first = str(rec.get('kanit_cumle') or '').split('\n')[0]
    ident = line_ids.get((_page(rec.get('sayfa')), _c(first)))
    model = rec.get('tablo_modeli') or {}
    if ident and model.get('durum') == STATE and (model.get('tablo_no'), (model.get('satir_indeksleri') or [None])[0]) != ident:
        return None                                    # model kimliği kaynakla çelişiyor
    return ident


def audit_record(rec, pages):
    """Deterministik özel tablo kaydının kaynak ilişkisini üretim yolundan BAĞIMSIZ yeniden denetler (kaynak sayfa
    metni yeniden okunur; kaydın meta verisi doğru kabul edilmez): kanıt satırları kayıtlı satır indekslerinde sayfada
    birebir; değer kayıtlı sütundaki hücrede (ya da devam satırında) birebir; sütun başlığı kayıtlı başlıkla aynı;
    bilgi metni satır etiketi hücresi; çalışma tablosunda çalışma kimliği satır etiketiyle aynı. Hata nedeni listesi
    (boş = geçti). İnsan klinik doğrulaması DEĞİLDİR."""
    m = rec.get('tablo_modeli') or {}
    lines = str(pages.get(rec.get('sayfa'), '')).splitlines()
    idxs = m.get('satir_indeksleri') or []
    if not idxs or any(i >= len(lines) for i in idxs) or '\n'.join(lines[i] for i in idxs) != rec.get('kanit_cumle'):
        return ['kanıt satırları kayıtlı satır indekslerinde değil']
    cells = [PT.split_cells(lines[i]) for i in idxs]
    problems = []
    kind = m.get('tur')
    if kind in ('calisma_satir', 'calisma_sutun', 'test_listesi') and m.get('liste_sutunu') is None:
        if str(rec.get('klinik_bilgi_en') or '').strip() != cells[0][0].strip():
            problems.append('bilgi metni satır etiketi hücresi değil')
    if m.get('liste_sutunu') is not None and not any(str(rec.get('klinik_bilgi_en')) in c for row in cells for c in row):
        problems.append('madde metni satırın hücrelerinde birebir yok')
    if rec.get('deger'):
        j = m.get('sutun_no')
        pool = [row[j] for row in cells if j < len(row)] if j is not None else [c for row in cells for c in row]
        if not any(rec['deger'] in c for c in pool) and rec['deger'] not in ' <br> '.join(pool):
            problems.append('değer kayıtlı sütunun hücresinde birebir yok')
    if m.get('sutun_no') is not None:
        j = m['sutun_no']
        seps = [i for i in range(0, idxs[0]) if PT.SEPARATOR.match(lines[i].strip())]
        start = seps[-1] - 1 if seps else 0
        headers = [lines[i] for i in range(max(start, 0), idxs[0]) if lines[i].strip().startswith('|')
                   and not PT.SEPARATOR.match(lines[i].strip())]
        if not any(j < len(PT.split_cells(h)) and _c(_label(PT.split_cells(h)[j])) == _c(m.get('sutun')) for h in headers):
            problems.append('sütun başlığı kayıtlı başlıkla aynı değil')
    if kind == 'calisma_satir':
        study = (m.get('kaynak_calisma') or {}).get('etiket')
        if not study or _c(study) != _c(cells[0][0]):
            problems.append('çalışma kimliği satır etiketiyle aynı değil')
    if rec.get('baglam') == 'literature_background' and str(kind).startswith('calisma') \
            and not (m.get('kaynak_calisma') or {}).get('etiket'):
        problems.append('literature_background ama çalışma kimliği yok')
    return problems


def apply_special_tables(records, pages, rejected):
    """Deterministik kayıtları hatta ekler (iki aşamalı; kayıt listesi doğrulama bitmeden değiştirilmez).

    1. Her tablo için deterministik kayıtlar ve eşdeğer LLM kayıtları (kabul ya da ret raporunda) belirlenir; kayıt
       tek başına kaynak doğrulamasından geçmezse ya da bağlamı belirlenemezse 'tablo_ozel_inceleme'ye ayrılır.
    2. Kalan bütün kayıtlarla BİRLİKTE doğrulanır (tablo içi ve tablolar arası tekrar anahtarı); geçemeyen deterministik
       kayıt incelemeye ayrılır ve onun eşdeğer LLM kayıtları yerinde kalır (bilgi kaybı yok).
    3. Yalnız doğrulamadan geçen deterministik kayıtların eşdeğerleri değiştirilir: silinmez, ret raporuna
       'tablo_ozel_degisimi' (önceki aşamasıyla) yazılır; farklı alanları deterministik kayda 'llm_kaydi' olarak taşınır."""
    from src.pilot_clinical import validate_records
    from src.rag_klinik_cikarim import source_problem
    built = build(pages)
    plans, holds_all, blob_pools = [], [], []
    for table, name, dets, holds in built:
        if name is None:
            continue
        holds_all += holds
        rows = _rows_of(table)
        acc = [r for r in records if (_page(r.get('sayfa')), _c(r.get('kanit_cumle'))) in rows
               and (r.get('tablo_modeli') or {}).get('durum') != STATE]          # deterministik kayıt değiştirilmez
        rej_idx = [k for k, x in enumerate(rejected) if isinstance(x.get('kayit'), dict)
                   and (_page(x['kayit'].get('sayfa')), _c(x['kayit'].get('kanit_cumle'))) in rows
                   and x.get('asama') not in (REPLACED_STAGE, HOLD_STAGE)]
        if name == 'test_listesi':
            blob_pools.append(acc)
        for det in dets:
            matches = [('a', r) for r in acc if equivalent(r, det)]
            matches += [('r', k) for k in rej_idx if equivalent(rejected[k]['kayit'], det)]
            if matches:
                llm = [r if s == 'a' else rejected[r]['kayit'] for s, r in matches]
                extra = []
                for l in llm:
                    diff = {f: l.get(f) for f in COMPARE_FIELDS if l.get(f) not in (None, '') and l.get(f) != det.get(f)}
                    if diff:
                        extra.append(diff)
                if name in TYPE_FROM_LLM:
                    kinds = {l.get('bilgi_turu') for l in llm if l.get('bilgi_turu')}
                    contexts = {l.get('baglam') for l in llm if l.get('baglam')}
                    if len(kinds) == 1:
                        det['bilgi_turu'] = kinds.pop()
                    if det.get('baglam') is None and len(contexts) == 1:
                        det['baglam'] = contexts.pop()
                units = {l.get('unit') for l in llm if l.get('unit')}
                if not det.get('unit') and len(units) == 1:
                    det['unit'] = units.pop()                  # LLM'in birimi korunur (kaynakta aşağıda doğrulanır)
                    det['tablo_modeli']['llm_alanlari'] = ['unit']
                if extra:
                    det['tablo_modeli']['llm_kaydi'] = extra
                det['tablo_modeli']['onceki_llm_kaydi'] = len(llm)
            if det.get('baglam') is None:
                # Bağlam yalnız kaynak tablodan ya da eşdeğer / aynı tablodaki LLM kayıtlarından; tahmin yok.
                peers = {r.get('baglam') for r in acc if r.get('baglam')}
                if len(peers) == 1:
                    det['baglam'] = peers.pop()
                else:
                    holds_all.append({'sayfa': det['sayfa'], 'asama': HOLD_STAGE, 'tablo_no': table['tablo_no'],
                                      'neden': 'bağlam kaynak tablodan belirlenemedi', 'kayit': det})
                    continue
            probe = {k: v for k, v in det.items() if k != 'tablo_modeli'}
            _, errors = validate_records([probe], pages)
            problem = errors[0]['hata'] if errors else source_problem(probe, pages.get(det['sayfa'], ''))
            if problem:
                holds_all.append({'sayfa': det['sayfa'], 'asama': HOLD_STAGE, 'tablo_no': table['tablo_no'],
                                  'neden': f'kaynak doğrulaması: {problem}', 'kayit': det})
                continue
            plans.append((table, det, matches))

    # 2. Birlikte doğrulama: tekrar anahtarı (sayfa + tür + bağlam + bilgi + yapılandırılmış alanlar) gevşetilmez.
    # Anahtar tablo / satır kimliğini (_structured_key: taslak_notu.tablo_no / satir_no) KAYNAK tablodan alır: kaydın
    # kanıt satırı özel tablonun fiziksel satırıysa (tablo no, satır indeksi). Satır metni sayfada birden çok yerde
    # geçiyorsa ya da kayıttaki model kimliğiyle çelişiyorsa kimlik verilmez (sahte kimlik yok). Aynı satırdaki gerçek
    # tekrar (aynı kimlik) yine yakalanır.
    line_ids = {}
    for table, name, _, _ in built:
        for seg in table['segmentler']:
            for i in range(seg['start'], seg['end']):
                key = (seg['sayfa'], _c(seg['lines'][i]))
                line_ids[key] = None if key in line_ids else (table['tablo_no'], i)

    def strip(r):
        probe = {k: v for k, v in r.items() if k != 'tablo_modeli'}
        ident = row_identity(r, line_ids)
        if ident:
            probe['taslak_notu'] = {'tablo_no': ident[0], 'satir_no': ident[1]}
        return probe
    alive = list(plans)
    while True:
        replaced = {id(r) for _, _, m in alive for s, r in m if s == 'a'}
        base = [r for r in records if id(r) not in replaced]
        _, errors = validate_records([strip(r) for r in base] + [strip(p[1]) for p in alive], pages)
        bad = {e['satir'] - 1 - len(base): e['hata'] for e in errors if e['satir'] - 1 >= len(base)}
        if not bad:
            break
        for k, err in sorted(bad.items()):
            table, det, _ = alive[k]
            reason = ('aynı sayfada aynı bilgi başka bir kayıtta; doğrulama anahtarı (sayfa / tür / bağlam / bilgi / '
                      'yapılandırılmış alanlar) ayırt edemiyor' if err.startswith('tekrar') else f'kaynak doğrulaması: {err}')
            holds_all.append({'sayfa': det['sayfa'], 'asama': HOLD_STAGE, 'tablo_no': table['tablo_no'],
                              'neden': reason, 'kayit': det})
        alive = [p for k, p in enumerate(alive) if k not in bad]

    # 3. Yalnız kabul edilen deterministik kayıtların eşdeğerleri değiştirilir.
    replaced_acc, replaced_rej = {}, set()
    for _, det, matches in alive:
        for s, r in matches:
            if s == 'a':
                replaced_acc[id(r)] = r
            else:
                replaced_rej.add(r)
    for k in sorted(replaced_rej):
        old = rejected[k]
        rejected[k] = {**old, 'asama': REPLACED_STAGE, 'onceki_asama': old.get('asama'),
                       'neden': f"deterministik tablo modeli kaydıyla değiştirildi (önceki: {old.get('neden')})"}
    for r in replaced_acc.values():
        rejected.append({'sayfa': r.get('sayfa'), 'asama': REPLACED_STAGE, 'onceki_asama': 'kabul', 'kayit': r,
                         'neden': 'deterministik tablo modeli kaydıyla değiştirildi'})
    # Toplu madde listesi kaydı: hücrenin BÜTÜN maddeleri kabul edilen ayrı kayıtlarda varsa bilgi kaybı yoktur.
    blobs = {}
    items = {(d['sayfa'], _c(d['kanit_cumle']), _c(d['klinik_bilgi_en'])) for _, d, _ in alive if not d.get('deger')}
    for acc in blob_pools:
        for r in acc:
            if id(r) in replaced_acc or id(r) in blobs:
                continue
            parts, bullets = _items(str(r.get('deger') or ''))
            if bullets and len(parts) > 1 and all((r['sayfa'], _c(r['kanit_cumle']), _c(p)) in items for p in parts):
                blobs[id(r)] = r
                rejected.append({'sayfa': r['sayfa'], 'asama': BLOB_STAGE, 'kayit': r,
                                 'neden': 'toplu madde listesi; bütün maddeler ayrı kayıtlarda (sütun ilişkisiyle)'})
    records = [r for r in records if id(r) not in replaced_acc and id(r) not in blobs] + [d for _, d, _ in alive]
    rejected.extend(holds_all)
    for table, name, _, _ in built:
        if name is None:
            records = link_evidence_levels(table, records, rejected)
    return records
