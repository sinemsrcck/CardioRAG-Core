"""RAG klinik çıkarım çıktısından (data/rag_cikarim/<pdf>/<zaman>/kayitlar.json) SQL aktarımına güvenli adaptör.

İki adım; insan onayı zorunlu:
  1. taslak : kayitlar.json -> eşleme + kaynak doğrulama + mevcut SQL kayıtlarıyla tekrar kontrolü (yalnız SELECT) ->
              data/review/rag/<belge>_rag_taslak_<zaman>.json. Bütün aday kayıtlar review_status='pending'.
              Adaptör hiçbir kaydı kendiliğinden 'verified' yapmaz. Kesin tekrarlar taslağa girmez; olası tekrarlar ve
              ilişkisi kurulamayan kayıtlar ayrı 'inceleme' listesinde gerekçesiyle saklanır (silinmez).
  2. aktar  : yalnız insan tarafından review_status='verified' yapılmış taslak kayıtları, src.pilot_clinical.import_records
              ile (cikarma_yontemi='rag') ek olarak yazar. Varsayılan PROVA: tek transaction, sonda ROLLBACK. --yaz için
              --yedek (son 24 saat) zorunlu. --yeniden yok; DELETE / UPDATE / DDL bağlantı korumasıyla engellenir.
              Mevcut bütün klinik_bulgular satırlarının TÜM alanları önce / sonra parmak iziyle karşılaştırılır.

Kullanım:
    python -m src.rag_aktarim taslak --calistirma data/rag_cikarim/<pdf>/<zaman> [--pdf <pdf>] [--sql-okuma-yok]
        [--dogrulama-raporu]   # aynı taslaktan <taslak>.dogrulama.json (src.rag_dogrulama.verify; API / yazma yok)
    python -m src.rag_aktarim aktar --pdf <pdf> --taslak <taslak.json> [--yaz --yedek backups/<dosya>.sql]

Eşleme kuralları (ayrıntı fonksiyon belgelerinde):
  - kategoriler atılır; otomatik_duzeltme taslak_notu'na taşınır; klinik_bilgi_en ve ham kanıt değişmez.
  - Çok parçalı kanıt: kanit_cumle = ilk parça (importer sözleşmesi); bütün parçalar kanit_parcalari ile korunur ve
    birleşimleri RAG çıktısındaki kanit_cumle ile birebir aynı olmalıdır.
  - Tablo kaydı: taslak_notu'na tablo_no / tablo_basligi (sayfadaki 'Table N' başlığı ya da aynı başlık satırıyla
    önceki sayfadan devam), satir_no ((satır, satır değişkeni) başına; Python measurement_key ile aynı birim),
    degisken, sutun, alt_bolum (+ bolum_goster), sutun_rolu, deger_bicimi. Kurulamayan ilişki -> inceleme.
  - sonuc_olcutu_tr: grup kaydında ve grupsuz sayısal hücrede satır değişkeni; grupsuz nitel etki hücresinde
    ('Decreased') sonuç sütunu + etki_turu='sayisal deger yok' + satırın TEK müdahale hücresi (mudahale_en);
    tanısal performans sütununda (SEN/SPE/AUC ...) ölçü adı + outcome_type='tanısal performans' + test (mudahale_en).
"""
import argparse
import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from src.pilot_clinical import (CONTEXTS, INPUT_FIELDS, STRUCTURED_FIELDS, TYPES, evidence_end_page, import_records,
                                is_multi_evidence, pdf_pages, validate_records, _note_with_parts)
from src.rag_klinik_cikarim import (CATEGORIES, CITATION, TOTAL_GROUP, _SEPARATOR_ROW, _cell_lines, _cmp, _norm,
                                    categories_of, cell_positions, clean_label, grouped_rows, is_fragmented_header,
                                    locate_header, row_cells, row_parameter, source_problem)

from src.rag_tablo_hizalama import NOTE_KEYS as _MODEL_NOTE_KEYS  # noqa: E402

KAYNAK = 'rag-klinik-cikarim-v1'
# Tablo modeline hizalanmış kayıt durumları (src.rag_tablo_hizalama) ve taslak_notu'na taşınan model alanları.
MODEL_STATES = ('eslesti', 'p_hucresi', 'katsayi')
MODEL_NOTE_KEYS = _MODEL_NOTE_KEYS + ('istatistik', 'etki_sutunu')
# Olası tekrar: bu alanlardan biri iki kayıtta da dolu ve farklıysa kayıtlar farklı bilgidir (değer / grup / istatistik).
CONFLICT_FIELDS = ('deger', 'hasta_grubu', 'p', 'ci95', 'unit', 'etki_turu', 'sonuc_olcutu_tr')
TASLAK_DIZINI = Path('data/review/rag')
SATIR_NO_OFSET = 1000          # pilot satırlarının satir_no değerleriyle çakışmaz; SQL'de mevcutlarla ayrıca denetlenir
ALLOWED = frozenset(INPUT_FIELDS) | frozenset(STRUCTURED_FIELDS)
CAPTION = re.compile(r'^[#*\s_]*(?:table|tablo)\s*([IVXLC]+|\d+)\b\s*\**\s*[.:]?\s*\**\s*(.*)$', re.I)
DIAGNOSTIC = re.compile(r'^(?:sen|spe|sensitivity|specificity|ppv|npv|accuracy|auc|auroc)$', re.I)
QUALITATIVE_MAX_WORDS = 3
INTERVENTION_TYPES = ('tedavi', 'ilac', 'doz')
COMPARE_FIELDS = ('deger', 'hasta_grubu', 'unit', 'p', 'sonuc_olcutu_tr')
SQL_COLUMNS = ('id', 'document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_sayfa',
               'kanit_sayfa_bitis', 'kanit_cumle', 'kanit_turu', 'birlestirme', 'hasta_grubu', 'deger', 'unit', 'p',
               'ci95', 'etki_turu', 'comparator', 'sonuc_olcutu_tr', 'mudahale_en', 'mudahale_turu', 'outcome_type',
               'taslak_notu', 'review_status', 'cikarma_yontemi', 'satir_hash')


class AdaptorHatasi(Exception):
    pass


# ---------------------------------------------------------------------------
# Görünen alan temizliği (yalnız türetilen alanlar; klinik_bilgi_en / kanıt değişmez)
# ---------------------------------------------------------------------------

def display_label(text):
    """Parametre / sütun / başlık gösterimi: clean_label + metin içi atıf işareti ('_(14)_') temizliği.
    Sayısal üst simge ve birim korunur."""
    return clean_label(CITATION.sub('', str(text or ''))).strip(' ,;')


def _roman_to_int(text):
    text = text.upper()
    if text.isdigit():
        return int(text)
    values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
    total = 0
    for i, ch in enumerate(text):
        value = values[ch]
        total += -value if i + 1 < len(text) and values[text[i + 1]] > value else value
    return total


# ---------------------------------------------------------------------------
# Tablo yapısı: tablo kimliği, satır konumu, bölüm
# ---------------------------------------------------------------------------

def _row_index(lines, row):
    at = [i for i, line in enumerate(lines) if line.strip() == row.strip()]
    return at[0] if len(at) == 1 else None


def _caption_above(lines, start):
    """Tablo bloğunun (start = ilk tablo satırı) üstündeki başlık: (no, başlık) ya da None. Başka bir tablo satırına
    ulaşınca ya da 4 dolu metin satırından sonra durur."""
    seen, k = 0, start - 1
    while k >= 0 and seen < 4:
        line = lines[k].strip()
        if line.startswith('|'):
            return None
        if line:
            match = CAPTION.match(line)
            if match:
                return _roman_to_int(match.group(1)), display_label(match.group(2)) or None
            seen += 1
        k -= 1
    return None


def _caption_in_header(lines, start, at):
    """Başlık tablonun ilk satırına (ayırıcının üstü) gömülü ve PDF dönüşümünde hücrelere bölünmüş olabilir
    ('|**TABL**|**E 3:** Multivariate ...|'): hücreler ayraçsız birleştirildiğinde 'Table N' ile başlıyorsa."""
    first = lines[start].strip() if start < at else ''
    if not first or _SEPARATOR_ROW.match(first) or start + 1 > at or not _SEPARATOR_ROW.match(lines[start + 1].strip()):
        return None
    joined = ''.join(clean_label(c) for c in row_cells(first))
    match = CAPTION.match(joined)
    return (_roman_to_int(match.group(1)), display_label(match.group(2)) or None) if match else None


def _table_block(lines, at):
    start = at
    while start - 1 >= 0 and lines[start - 1].strip().startswith('|'):
        start -= 1
    return start


def _header_line(lines, start, at):
    for j in range(start, at):
        if _SEPARATOR_ROW.match(lines[j].strip()):
            return lines[j - 1].strip() if j > start else None
    return None


def table_identity(item, pages):
    """(tablo_no, tablo_basligi, satır konumu, belirsizlik). Başlık sayfada tablonun üstünde aranır; yoksa tablo
    sayfanın ilk tablosuysa ve başlık satırı önceki sayfanın son tablosunun başlık satırıyla birebir aynıysa o tablonun
    devamıdır. Başka durumda tahmin yapılmaz."""
    page = item.get('sayfa')
    lines = str(pages.get(page, '')).splitlines()
    at = _row_index(lines, str(item.get('kanit_cumle') or ''))
    if at is None:
        return None, None, None, 'kanıt satırı sayfada tekil değil'
    start = _table_block(lines, at)
    found = _caption_above(lines, start) or _caption_in_header(lines, start, at)
    if found:
        return found[0], found[1], (page, at), None
    header = _header_line(lines, start, at)
    first_table = not any(line.strip().startswith('|') for line in lines[:start])
    prev = str(pages.get(page - 1, '')).splitlines() if isinstance(page, int) else []
    if header and first_table and prev:
        seps = [j for j, line in enumerate(prev) if _SEPARATOR_ROW.match(line.strip())]
        if seps and seps[-1] > 0 and prev[seps[-1] - 1].strip() == header:
            found = _caption_above(prev, _table_block(prev, seps[-1]))
            if found:
                return found[0], found[1], (page, at), None
    return None, None, None, 'tablo numarası (Table N başlığı) belirlenemedi'


def section_of(item, pages):
    """(bölüm, belirsizlik). Veri satırının üstünde, aynı tablonun ayırıcısından sonra, tek dolu metin hücresi olan en
    yakın satır (ör. 'Adult cases', 'delta hs Trop I (0-1)h ±20%'). Parantezli devam niteleyicileri ('(EMS)') bölüm
    değildir. Tablo önceki sayfadan devam ediyor ve o sayfada bölüm satırları varsa, bu sayfada bölüm satırı yoksa
    belirsizdir."""
    page = item.get('sayfa')
    lines = str(pages.get(page, '')).splitlines()
    at = _row_index(lines, str(item.get('kanit_cumle') or ''))
    if at is None:
        return None, 'kanıt satırı sayfada tekil değil'
    start = _table_block(lines, at)
    sep = next((j for j in range(start, at) if _SEPARATOR_ROW.match(lines[j].strip())), None)
    for j in range(at - 1, (sep if sep is not None else start - 1), -1):
        cells = row_cells(lines[j])
        filled = [c for c in cells if c.strip()]
        if len(filled) == 1 and re.search(r'[^\W\d_]', filled[0]) and not re.fullmatch(r'\(.*\)', clean_label(filled[0])):
            if cells and cells[0].strip():
                return display_label(filled[0]), None
    if sep is None:
        prev = str(pages.get(page - 1, '')).splitlines() if isinstance(page, int) else []
        if any(len([c for c in row_cells(l) if c.strip()]) == 1 and row_cells(l)[0].strip() for l in prev
               if l.strip().startswith('|') and not _SEPARATOR_ROW.match(l.strip())):
            return None, 'tablo önceki sayfadan devam ediyor; bölüm bağlamı bu sayfada doğrulanamadı'
    return None, None


def _value_index(item, header):
    """Değerin (yoksa klinik bilginin) satırdaki tekil hücre konumu (hücre, satır) ya da None. Değer satırda birden
    çok hücrede geçiyorsa, kaydın sütun etiketiyle (hasta_grubu / sonuc_olcutu_tr) eşleşen başlık hücresi TEK ise o."""
    value = item.get('deger') if item.get('deger') not in (None, '') else item.get('klinik_bilgi_en')
    positions = cell_positions(value, item.get('kanit_cumle'))
    if not positions and item.get('unit'):
        positions = cell_positions(f"{item.get('deger')} {item['unit']}", item.get('kanit_cumle'))
    if len(positions) > 1:
        label = _cmp(display_label(item.get('hasta_grubu') or item.get('sonuc_olcutu_tr')))
        if label:
            def matches(i):
                head = _cmp(display_label(header[i])) if i < len(header) else ''
                return bool(head) and (head == label or head.startswith(label + ' '))
            positions = [p for p in positions if matches(p[0])]
    if not positions and item.get('deger') not in (None, ''):
        # İkili karşılaştırma hücresi ('P (A-B)=0.291'): etiket ve değer aynı hücrede birlikte geçiyorsa o hücre.
        label = _norm(item.get('sonuc_olcutu_tr') or item.get('hasta_grubu'))
        cells = row_cells(item.get('kanit_cumle'))
        hits = [i for i, c in enumerate(cells) if label and label in _norm(c) and _cmp(item['deger']) in _cmp(c)]
        positions = [(hits[0], None)] if len(hits) == 1 else []
    return positions[0] if len(positions) == 1 else None


def _column_label(item, header, index):
    """Değerin sütun başlığı. Başlık hücresi alt alta yazılmış birden çok etiket taşıyor ve değer hücrenin bir satırıysa
    ('B<br>Exp (B) (OR)<br>p value' / '7.912<br>2,735.621<br>0.002'), aynı sıradaki başlık satırı alınır: birleşik başlık
    metni etki türüne (OR) dönüşmez. Satır sayıları eşit değilse birleşik başlık kalır."""
    if index[0] >= len(header):
        return ''
    cell = header[index[0]]
    lines = _cell_lines(cell)
    cells = row_cells(item.get('kanit_cumle'))
    if index[1] is not None and index[0] < len(cells) and len(lines) > 1 \
            and len(lines) == len(_cell_lines(cells[index[0]])) and index[1] < len(lines):
        return display_label(lines[index[1]])
    return display_label(cell)


def _verified_label(item, pages, *candidates):
    """Adaylardan sayfada (_norm) doğrulanabilen ilki: gösterim biçimi kaynakta yoksa ham biçime düşülür."""
    page = pages.get(item.get('sayfa'), '')
    for text in candidates:
        if text and _norm(text) and _norm(text) in _norm(page):
            return text
    return None


def _parameter_order(item):
    cells = row_cells(item.get('kanit_cumle'))
    lines = _cell_lines(cells[0]) if cells else []
    target = _cmp(row_parameter(item))
    return next((i for i, line in enumerate(lines) if _cmp(line) == target), 0)


# ---------------------------------------------------------------------------
# Eşleme
# ---------------------------------------------------------------------------

def _effect_type(label):
    text = label.lower()
    if re.search(r'relative risk|\brr\b', text):
        return 'RR'
    if re.search(r'odds ratio|\bor\b', text):
        return 'OR'
    if re.search(r'hazard ratio|\bhr\b', text):
        return 'HR'
    if re.search(r'\bauc\b|c[- ]?statistic', text):
        return 'AUC / C-istatistigi'
    if '%' in text:
        return 'oran / yuzde'
    return 'diger'


def _is_qualitative(value):
    return (isinstance(value, str) and not re.search(r'\d', value)
            and 0 < len(clean_label(value).split()) <= QUALITATIVE_MAX_WORDS)


def _base_note(item, run_meta):
    note = {'kaynak': KAYNAK, 'calistirma': run_meta.get('cikti_dizini'), 'model': run_meta.get('model')}
    if item.get('otomatik_duzeltme'):
        note['otomatik_duzeltme'] = list(item['otomatik_duzeltme'])
    fmt = [n.split('=', 1)[1] for n in item.get('otomatik_duzeltme', []) if n.startswith('hasta_grubu:deger_bicimi=')]
    if fmt and fmt[0]:
        note['deger_bicimi'] = fmt[0]
    if item.get('tablo_iliskisi'):
        # Modeli olmayan öneri tablosunda kanıt düzeyi (LOE) ve öneri metni ilişkisi (src.rag_tablo_ozel).
        note['tablo_iliskisi'] = item['tablo_iliskisi']
    if item.get('anlam_denetimi'):
        # Değer anlamı / bağlam denetimi (src.rag_anlam_denetimi): istatistik bileşenleri, kaynakça numaraları,
        # tutarlılık ve kapsam uyarıları. Değerler tahminle değişmez; uyarılar inceleyene gösterilir.
        note['anlam_denetimi'] = item['anlam_denetimi']
    return note


SPECIAL_STATE = 'ozel'
SPECIAL_SKIP = ('durum', 'satir_indeksleri', 'tablo_no', 'tablo_basligi', 'onceki_llm_kaydi')


def _map_special(item, rec, note):
    """Deterministik özel tablo modeli kaydı (src.rag_tablo_ozel): tablo kimliği, sütun / satır ilişkisi, çalışma
    kimliği, alt grup / kol, öneri sınıfı / kanıt düzeyi modelden gelir; yapılandırılmış alanlar kayıtta hazırdır.
    Kaynak doğrulaması (source_check) build_draft'ta diğer kayıtlarla aynı kurallarla yapılır."""
    model = item['tablo_modeli']
    note.update({'tablo_no': model['tablo_no'], 'tablo_basligi': model.get('tablo_basligi'),
                 '_konum': [item.get('sayfa'), model['satir_indeksleri'][0], _parameter_order(item)]})
    note.update({k: v for k, v in model.items() if k not in SPECIAL_SKIP and v not in (None, '', [], {})})
    if model.get('alt_bolum'):
        note['bolum_goster'] = True
    rec['taslak_notu'] = note
    return rec, None


def map_record(item, pages, run_meta, row_interventions):
    """(eşlenmiş kayıt, inceleme nedeni). Kayıt satir_no'suz döner; satir_no assign_row_numbers ile verilir."""
    rec = {k: v for k, v in item.items() if k in ALLOWED and k != 'review_status'}
    rec['review_status'] = 'pending'
    note = _base_note(item, run_meta)
    parts = item.get('kanit_parcalari')
    if parts:
        joined = ' '.join(str(p.get('metin', '')).strip() for p in parts)
        if joined != str(item.get('kanit_cumle') or ''):
            return None, 'çok parçalı kanıt: kanit_cumle parçaların birleşimiyle aynı değil'
        rec['kanit_cumle'] = parts[0]['metin']            # importer sözleşmesi; parçalar kanit_parcalari'nda tam
    if item.get('kanit_turu') != 'tablo':
        rec['taslak_notu'] = note
        return rec, None
    if (item.get('tablo_modeli') or {}).get('durum') == SPECIAL_STATE:
        return _map_special(item, rec, note)

    tablo_no, title, position, problem = table_identity(item, pages)
    if problem:
        return None, problem
    header, main, problem = locate_header(item, pages.get(item.get('sayfa'), ''))
    if problem:
        return None, f'sütun başlığı: {problem}'
    model = item.get('tablo_modeli') or {}
    aligned = model.get('durum') in MODEL_STATES
    # Tablo modeli değeri ayrıştırdıysa (GA / SE / işaret) hücre konumu özgün hücre değeriyle bulunur.
    # Anlam denetimi kaynakça numarasını ayırdıysa da hücre konumu özgün hücre değeriyle bulunur.
    raw = model.get('ham_deger') if aligned else None
    raw = raw or (item.get('anlam_denetimi') or {}).get('ham_deger')
    index = _value_index({**item, 'deger': raw} if raw else item, header)
    if index is None:
        return None, 'değerin satırdaki hücre konumu tekil değil'
    if aligned:
        section = model.get('alt_bolum')                 # bölüm kapsamı tablo modelinden (pilot_tablo kuralları)
    else:
        section, problem = section_of(item, pages)
        if problem:
            return None, problem
    column = _column_label(item, header, index)
    parameter = display_label(row_parameter(item))
    note.update({'tablo_no': tablo_no, 'tablo_basligi': title, 'degisken': parameter,
                 'sutun': (model.get('sutun') if aligned and model.get('sutun') else column) or None,
                 '_konum': [position[0], position[1], _parameter_order(item)]})
    if aligned:
        note.update({k: model[k] for k in MODEL_NOTE_KEYS if model.get(k) not in (None, '', [])})
    if header is not main and main and not is_fragmented_header(main) and index[0] < len(main):
        upper = display_label(main[index[0]])
        if upper and upper != column:
            note['ust_baslik'] = upper
    if section:
        note['alt_bolum'] = section
        note['bolum_goster'] = True
    group, value = item.get('hasta_grubu'), item.get('deger')
    if group and TOTAL_GROUP.match(_norm(group)):
        note['sutun_rolu'] = 'toplam'
    if model.get('durum') == 'katsayi':
        # Regresyon katsayısı (B): etki ölçüsü değil; özgün değer ve anlamı notta korunur, OR / HR olarak yorumlanmaz.
        rec['etki_turu'] = 'diger'
        note.update({'istatistik': model.get('istatistik'), 'regresyon_katsayisi': value,
                     'katsayi_sutunu': model.get('sutun')})
    if not parameter:
        return None, 'satır değişkeni (parametre) boş'
    # sonuc_olcutu_tr kaynakta doğrulanmalı: gösterim biçimi (atıfsız) sayfada yoksa temizlenmiş ham biçim.
    row_variable = _verified_label(item, pages, parameter, clean_label(row_parameter(item)))
    options = row_interventions.get((item.get('sayfa'), item.get('kanit_cumle')), set())

    if value in (None, ''):
        if item.get('bilgi_turu') in INTERVENTION_TYPES:
            rec['mudahale_en'] = display_label(item.get('klinik_bilgi_en'))
    elif DIAGNOSTIC.match(column or '') and not group:
        if not row_variable:
            return None, 'tanısal performans: test adı (satır değişkeni) kaynakta doğrulanamadı'
        rec['sonuc_olcutu_tr'] = column
        rec['mudahale_en'] = row_variable
        rec['outcome_type'] = 'tanısal performans'
        rec.setdefault('etki_turu', 'AUC / C-istatistigi' if re.match(r'^au', column, re.I) else 'oran / yuzde')
    elif not group and _is_qualitative(value) and options:
        # Nitel etki hücresi ('Decreased'): satırın TEK müdahale hücresi ve sonuç sütunu birlikte kurulmalı.
        if not column or len(options) != 1:
            return None, ('nitel etki hücresi: satırda birden çok müdahale hücresi' if column
                          else 'nitel etki hücresi: sonuç sütunu başlığı yok')
        rec['sonuc_olcutu_tr'] = column
        rec['mudahale_en'] = next(iter(options))
        rec['etki_turu'] = 'sayisal deger yok'
    else:
        # Grup hücresi, grupsuz sayısal hücre ya da betimleyici metin hücresi: ölçülen değişken = satır değişkeni.
        if not row_variable:
            return None, 'satır değişkeni kaynakta doğrulanamadı'
        rec['sonuc_olcutu_tr'] = row_variable
        if not group:
            rec.setdefault('etki_turu', _effect_type(column or ''))
    rec['taslak_notu'] = note
    return rec, None


def row_interventions(records):
    """(sayfa, satır) -> o satırın müdahale hücresi metinleri (değersiz tedavi / ilaç / doz kaydı, tam hücre)."""
    found = defaultdict(set)
    for item in records:
        if (item.get('kanit_turu') == 'tablo' and item.get('bilgi_turu') in INTERVENTION_TYPES
                and item.get('deger') in (None, '') and cell_positions(item.get('klinik_bilgi_en'), item.get('kanit_cumle'))):
            found[(item.get('sayfa'), item.get('kanit_cumle'))].add(display_label(item['klinik_bilgi_en']))
    return found


def assign_row_numbers(records):
    """satir_no = SATIR_NO_OFSET + (tablo içindeki (sayfa, satır, satır değişkeni) sırası). Böylece SQL row_key
    (tablo_no, satir_no) Python measurement_key ile aynı birime iner; alt alta değişkenler ayrı satırdır."""
    keys = defaultdict(set)
    for rec in records:
        note = rec.get('taslak_notu') or {}
        if '_konum' in note:
            keys[note['tablo_no']].add(tuple(note['_konum']))
    order = {t: {k: i for i, k in enumerate(sorted(v), 1)} for t, v in keys.items()}
    for rec in records:
        note = rec.get('taslak_notu') or {}
        if '_konum' in note:
            note['satir_no'] = SATIR_NO_OFSET + order[note['tablo_no']][tuple(note.pop('_konum'))]
    return records


def source_check(rec, pages):
    """Eşlenmiş kaydın kaynak doğrulaması: validate_records (kanıt / bilgi / parçalar) + source_problem (değer, p,
    birim, grup, sonuç ölçütü) + mudahale_en sayfada. Hata metni ya da None."""
    probe = {k: v for k, v in rec.items() if k != 'taslak_notu'}
    _, errors = validate_records([probe], pages)
    if errors:
        return errors[0]['hata']
    problem = source_problem(probe, pages.get(rec.get('sayfa'), ''))
    if problem:
        return problem
    page = pages.get(rec.get('sayfa'), '') + '\n' + pages.get(evidence_end_page(rec), '')
    if rec.get('mudahale_en') and _norm(rec['mudahale_en']) not in _norm(page):
        return f"mudahale_en sayfada yok: {rec['mudahale_en']!r}"
    unknown = sorted(set(rec) - ALLOWED)
    if unknown:
        return 'izin verilmeyen alan: ' + ', '.join(unknown)
    return None


# ---------------------------------------------------------------------------
# Mevcut SQL kayıtlarıyla tekrar kontrolü
# ---------------------------------------------------------------------------

def _parts_of(row):
    note = row.get('taslak_notu')
    if isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            note = {}
    return (note or {}).get('kanit_parcalari') if isinstance(note, dict) else None


def exact_key(rec):
    """Kesin tekrar anahtarı: aynı kanıt (sayfa, bitiş, ham kanıt, parçalar), aynı bilgi (ham), aynı tür ve bağlam,
    aynı değer / grup / birim / p / sonuç ölçütü (biçim farkı _cmp ile yok sayılır)."""
    parts = rec.get('kanit_parcalari') if 'kanit_parcalari' in rec else _parts_of(rec)
    parts = tuple((p.get('sayfa'), str(p.get('metin', '')).strip()) for p in parts) if parts else ()
    page = rec.get('sayfa', rec.get('kanit_sayfa'))
    end = rec.get('sayfa_bitis', rec.get('kanit_sayfa_bitis')) or page
    return ((page, end, str(rec.get('kanit_cumle') or '').strip(), parts, str(rec.get('klinik_bilgi_en') or '').strip(),
             rec.get('bilgi_turu'), rec.get('baglam')) + tuple(_cmp(rec.get(f)) for f in COMPARE_FIELDS))


def _similar(a, b):
    x, y = _cmp(a), _cmp(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short.split()) >= 4 and re.search(r'(?<!\w)' + re.escape(short) + r'(?!\w)', long_):
        return True
    return difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() >= 0.9


def classify_against_existing(rec, existing):
    """('kesin', id) | ('olasi', [id...]) | ('yeni', None). Olası tekrar: aynı sayfa aralığında benzer bilgi
    (toleranslı eşleşme) ya da aynı tablo satırında aynı değer; klinik olarak farklı olabilir, silinmez -> inceleme."""
    key = exact_key(rec)
    for row in existing:
        if exact_key(row) == key:
            return 'kesin', row.get('id')
    pages = set(range(rec['sayfa'], (rec.get('sayfa_bitis') or rec['sayfa']) + 1))
    close = []
    for row in existing:
        start = row.get('kanit_sayfa')
        span = set(range(start, (row.get('kanit_sayfa_bitis') or start) + 1)) if isinstance(start, int) else set()
        if not pages & span:
            continue
        same_row_value = (rec.get('kanit_turu') == 'tablo' and _cmp(rec.get('kanit_cumle')) == _cmp(row.get('kanit_cumle'))
                          and rec.get('deger') and _cmp(rec.get('deger')) == _cmp(row.get('deger')))
        if conflicting(rec, row):
            continue                                     # farklı değer / grup / istatistik: aynı bilgi değil
        if same_row_value or (rec.get('kanit_turu') != 'tablo' and _similar(rec.get('klinik_bilgi_en'), row.get('klinik_bilgi_en'))):
            close.append(row.get('id'))
    return ('olasi', close) if close else ('yeni', None)


def conflicting(rec, row):
    """İki kayıtta da dolu ve normalize biçimde farklı bir değer / grup / istatistik alanı var mı."""
    return any(rec.get(f) not in (None, '') and row.get(f) not in (None, '') and _cmp(rec.get(f)) != _cmp(row.get(f))
               for f in CONFLICT_FIELDS)


# ---------------------------------------------------------------------------
# Python / SQL grup kuralı uyumu
# ---------------------------------------------------------------------------

def sql_row_key(rec):
    note = rec.get('taslak_notu') or {}
    if rec.get('kanit_turu') == 'tablo' and note.get('tablo_no') is not None:
        return ('t', note['tablo_no'], note.get('satir_no'))
    return None


def group_harmony(records):
    """SQL satir_grup_karsilastirmasi (row_key'de herhangi bir hasta_grubu) ile Python grouped_rows aynı mı?
    Uyumsuz row_key'lerin kayıt indekslerini döndürür (ör. satırın tek grubu kaldıysa)."""
    python = grouped_rows(records)
    by_key = defaultdict(list)
    for i, rec in enumerate(records):
        key = sql_row_key(rec)
        if key:
            by_key[key].append(i)
    from src.rag_klinik_cikarim import measurement_key
    bad = []
    for key, idx in by_key.items():
        sql_flag = any(records[i].get('hasta_grubu') for i in idx)
        py_flag = any(measurement_key(records[i]) in python for i in idx)
        if sql_flag != py_flag:
            bad.extend(idx)
    return bad


# ---------------------------------------------------------------------------
# Taslak üretimi
# ---------------------------------------------------------------------------

def build_draft(records, pages, run_meta, existing=(), haric=None):
    """Bellekte taslak: {'adaylar': [...pending], 'inceleme': [...], 'kesin_tekrar': [...], 'sayilar': {...}}.
    haric: {satir_hash: gerekçe} — aktarım dışında tutulacak adaylar (ör. semantik pilotta gerçek tür / bağlam /
    kapsam sorunu bulunanlar); aday olmaz, gerekçesiyle 'aktarim_disi' incelemesine gider. LLM kararı insan onayı
    yerine geçmez; bu yalnız bir güvenlik önlemidir."""
    interventions = row_interventions(records)
    mapped, review, exact = [], [], []
    for index, item in enumerate(records):
        rec, reason = map_record(item, pages, run_meta, interventions)
        if reason:
            review.append({'sira': index + 1, 'neden': reason, 'asama': 'esleme', 'kayit': item})
            continue
        mapped.append((index, item, rec))
    assign_row_numbers([rec for _, _, rec in mapped])
    checked = []
    for index, item, rec in mapped:
        if haric:
            key = satir_hash(run_meta.get('document_id'), rec)
            if key in haric:
                review.append({'sira': index + 1, 'asama': 'aktarim_disi', 'kayit': item, 'satir_hash': key,
                               'neden': f'aktarım dışı: {haric[key]}'})
                continue
        problem = source_check(rec, pages)
        if problem:
            review.append({'sira': index + 1, 'neden': f'kaynak doğrulaması: {problem}', 'asama': 'dogrulama', 'kayit': item})
            continue
        checked.append((index, item, rec))
    candidates = []
    for index, item, rec in checked:
        kind, ref = classify_against_existing(rec, existing)
        if kind == 'kesin':
            exact.append({'sira': index + 1, 'mevcut_id': ref, 'kayit': rec})
        elif kind == 'olasi':
            review.append({'sira': index + 1, 'neden': 'olası tekrar (benzer mevcut kayıt)', 'asama': 'olasi_tekrar',
                           'mevcut_idler': ref, 'kayit': rec})
        else:
            candidates.append((index, item, rec))
    bad = set(group_harmony([rec for _, _, rec in candidates]))
    final, sources = [], []
    for pos, (index, item, rec) in enumerate(candidates):
        if pos in bad:
            review.append({'sira': index + 1, 'asama': 'grup_uyumu', 'kayit': rec,
                           'neden': 'grup karşılaştırması eksik (satırın gruplarından biri kaldı); SQL ve Python kuralı uyuşmuyor'})
            continue
        final.append(rec)
        sources.append(index)
    # taslak_notu korunur: validate_records tekrar anahtarı (_structured_key) tablo_no / satir_no / analiz'i notta
    # okur; not atılırsa aynı sayfadaki farklı tablo satırlarının (ör. iki testin aynı maddesi) kimliği kaybolur.
    # Not bilgisi olmayan kayıtların (metin) anahtarı değişmez; aynı satırdaki gerçek tekrar yine yakalanır.
    _, errors = validate_records(final, pages)
    if errors:
        raise AdaptorHatasi(f'taslakta kendi içinde tekrar / doğrulama hatası: {errors[:3]}')
    multi_in = sum(bool(r.get('kanit_parcalari')) for r in records)
    multi_out = [r for r in final if r.get('kanit_parcalari')]
    counts = {'girdi': len(records), 'eslenen': len(mapped), 'kaynak_dogrulanan': len(checked),
              'kesin_tekrar': len(exact), 'inceleme': len(review),
              'inceleme_nedenleri': dict(Counter(r['asama'] for r in review)), 'pending_aday': len(final),
              'cok_parcali_girdi': multi_in, 'cok_parcali_aday': len(multi_out),
              'cok_parcali_parca_eksiksiz': all(len(r['kanit_parcalari']) >= 2 for r in multi_out)}
    # aday_kaynaklari: her adayın girdi kayıt listesindeki sırası (bağımsız doğrulama özgün kaydı okuyabilsin).
    return {'adaylar': final, 'aday_kaynaklari': sources, 'inceleme': review, 'kesin_tekrar': exact, 'sayilar': counts}


def expected_routing(candidates):
    """Sekiz kategori için beklenen yönlendirme: kayıt bazında (categories_of) ve SQL görünüm satırı bazında
    (treatments / measurements row_key başına tek satır)."""
    grouped = grouped_rows(candidates)
    per_record = Counter(c for r in candidates for c in categories_of(r, grouped))
    rows = defaultdict(set)
    for i, r in enumerate(candidates):
        key = sql_row_key(r) or ('k', i)
        for c in categories_of(r, grouped):
            rows[c].add(key if c in ('treatments', 'measurements') else ('k', i))
    return {c: per_record[c] for c in CATEGORIES}, {c: len(rows[c]) for c in CATEGORIES}


def serializable(rec):
    out = dict(rec)
    if isinstance(out.get('taslak_notu'), dict):
        out['taslak_notu'] = json.dumps(out['taslak_notu'], ensure_ascii=False)
    return out


# ---------------------------------------------------------------------------
# SQL: salt okunur erişim, koruma, parmak izi
# ---------------------------------------------------------------------------

_READ = re.compile(r'^\s*(?:SELECT|SHOW)\b', re.I)
_INSERT = re.compile(r'^\s*INSERT\s+IGNORE\s+INTO\s+klinik_bulgular\b', re.I)


class KorumaliBaglanti:
    """Bağlantı sarmalayıcısı. Salt okuma modunda yalnız SELECT / SHOW; aktarım modunda ek olarak
    'INSERT IGNORE INTO klinik_bulgular'. UPDATE / DELETE / REPLACE / DDL ve diğer her ifade engellenir.
    commit() yalnız izin=True ile."""

    def __init__(self, conn, yazma=False, commit_izni=False):
        self._conn, self._yazma, self._commit_izni = conn, yazma, commit_izni

    def cursor(self, *args, **kwargs):
        outer = self
        inner = self._conn.cursor(*args, **kwargs)

        class Imlec:
            def execute(self, sql, params=None):
                text = str(sql)
                if not (_READ.match(text) or (outer._yazma and _INSERT.match(text))):
                    raise AdaptorHatasi(f'korumalı bağlantıda izin verilmeyen ifade: {text[:60]!r}')
                return inner.execute(sql, params) if params is not None else inner.execute(sql)

            def __getattr__(self, name):
                return getattr(inner, name)

        return Imlec()

    def commit(self):
        if not self._commit_izni:
            raise AdaptorHatasi('commit yalnız --yaz ve geçerli yedekle yapılabilir')
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def existing_rows(conn, document_id):
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT {', '.join(SQL_COLUMNS)} FROM klinik_bulgular WHERE document_id = %s "
                       f"AND bilgi_turu IS NOT NULL ORDER BY id", (document_id,))
        return [dict(zip(SQL_COLUMNS, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def table_fingerprint(conn):
    """klinik_bulgular'ın bütün satırlarının bütün alanları: {id: sha256(satır)}. Mevcut doğrulanmış kayıtların hiçbir
    alanının değişmediğini aktarım öncesi / sonrası karşılaştırmak için."""
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM klinik_bulgular ORDER BY id')
        names = [d[0] for d in cursor.description]
        out = {}
        for row in cursor.fetchall():
            data = dict(zip(names, row))
            out[data['id']] = hashlib.sha256(json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
                                             .encode('utf-8')).hexdigest()
        return out
    finally:
        cursor.close()


def satir_hash(document_id, rec):
    """import_records'un satir_hash formülünün aynısı (yazmadan önce çakışma kontrolü için)."""
    payload = {'document_id': document_id, 'bilgi_turu': rec['bilgi_turu'], 'baglam': rec['baglam'],
               'klinik_bilgi': rec.get('klinik_bilgi'), 'klinik_bilgi_en': rec.get('klinik_bilgi_en'),
               'kanit_sayfa': rec['sayfa'], 'kanit_cumle': rec['kanit_cumle']}
    for key in STRUCTURED_FIELDS:
        value = rec.get(key)
        if value is not None:
            payload[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    identity = [payload[k] for k in ('document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en',
                                     'kanit_sayfa', 'kanit_cumle')]
    multi = None
    if is_multi_evidence(rec):
        if rec.get('kanit_parcalari') is not None:
            payload['taslak_notu'] = _note_with_parts(rec)
        multi = {'kanit_sayfa_bitis': evidence_end_page(rec), 'birlestirme': rec.get('birlestirme'),
                 'kanit_parcalari': rec.get('kanit_parcalari')}
    structured = {k: payload[k] for k in STRUCTURED_FIELDS if k in payload}
    if structured:
        identity.append(structured)
    if multi is not None:
        identity.append(multi)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def aktar(conn, doc, pages, records, *, yaz=False, haric=None):
    """Yalnız verified taslak kayıtlarını ek olarak yazar. conn: KorumaliBaglanti (yazma=True). Varsayılan prova:
    sonda ROLLBACK. Kontroller: verified dışı kayıt yok sayılır (sayılır), kaynak doğrulaması, grup uyumu,
    satir_hash çakışması yok, satir_no mevcut tablo satırlarıyla çakışmıyor, yazılan = beklenen, mevcut satırların
    tamamı (bütün alanlar) değişmedi, yeni satırların hepsi cikarma_yontemi='rag'."""
    verified = [r for r in records if r.get('review_status') == 'verified']
    skipped = len(records) - len(verified)
    if not verified:
        raise AdaptorHatasi('aktarılacak verified kayıt yok (adaptör kendiliğinden verified atamaz)')
    if haric:
        blocked = [satir_hash(doc['document_id'], r) for r in verified if satir_hash(doc['document_id'], r) in haric]
        if blocked:
            raise AdaptorHatasi(f'{len(blocked)} verified kayıt aktarım dışı listesinde (ör. {blocked[0][:12]}); '
                                f'aktarım durdu, hiçbir şey yazılmadı')
    for i, rec in enumerate(verified, 1):
        note = rec.get('taslak_notu')
        rec_note = json.loads(note) if isinstance(note, str) else note
        if not isinstance(rec_note, dict) or rec_note.get('kaynak') != KAYNAK:
            raise AdaptorHatasi(f'kayıt {i}: taslak_notu.kaynak {KAYNAK!r} değil; yalnız adaptör taslakları aktarılır')
        problem = source_check({**rec, 'taslak_notu': rec_note}, pages)
        if problem:
            raise AdaptorHatasi(f'kayıt {i}: kaynak doğrulaması: {problem}')
    parsed = [{**r, 'taslak_notu': json.loads(r['taslak_notu']) if isinstance(r.get('taslak_notu'), str) else r.get('taslak_notu')}
              for r in verified]
    if group_harmony(parsed):
        raise AdaptorHatasi('onaylanan kayıtlarda grup karşılaştırması eksik (aynı satırın bütün grupları onaylanmalı)')
    before = table_fingerprint(conn)
    cursor = conn.cursor()
    try:
        hashes = [satir_hash(doc['document_id'], r) for r in verified]
        cursor.execute(f"SELECT satir_hash FROM klinik_bulgular WHERE satir_hash IN ({', '.join(['%s'] * len(hashes))})",
                       tuple(hashes))
        clash = cursor.fetchall()
        if clash:
            raise AdaptorHatasi(f'{len(clash)} kaydın satir_hash değeri veritabanında zaten var; aktarım durdu')
        existing = existing_rows(conn, doc['document_id'])
        used = {(n.get('tablo_no'), n.get('satir_no')) for n in (_parse_note(r.get('taslak_notu')) for r in existing)}
        mine = {(r['taslak_notu'].get('tablo_no'), r['taslak_notu'].get('satir_no')) for r in parsed if r['kanit_turu'] == 'tablo'}
        if used & mine:
            raise AdaptorHatasi('satir_no mevcut tablo satırlarıyla çakışıyor; aktarım durdu')
    finally:
        cursor.close()
    written = import_records(doc, verified, connection=conn, pages=pages, cikarma_yontemi='rag')
    after = table_fingerprint(conn)
    changed = [i for i, h in before.items() if after.get(i) != h]
    new_ids = sorted(set(after) - set(before))
    if changed:
        conn.rollback()
        raise AdaptorHatasi(f'mevcut {len(changed)} satırın alanları değişti; ROLLBACK')
    if written != len(verified) or len(new_ids) != len(verified):
        conn.rollback()
        raise AdaptorHatasi(f'yazılan {written}, yeni satır {len(new_ids)}, beklenen {len(verified)}; ROLLBACK')
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM klinik_bulgular WHERE id IN ({', '.join(['%s'] * len(new_ids))}) "
                       f"AND cikarma_yontemi = 'rag' AND review_status = 'verified'", tuple(new_ids))
        if cursor.fetchone()[0] != len(new_ids):
            conn.rollback()
            raise AdaptorHatasi('yeni satırların hepsi cikarma_yontemi=rag / verified değil; ROLLBACK')
    finally:
        cursor.close()
    if yaz:
        conn.commit()
    else:
        conn.rollback()
    return {'yazilan': written, 'atlanan_onaysiz': skipped, 'yeni_idler': new_ids, 'mevcut_degisen': 0,
            'kalici': bool(yaz)}


def _parse_note(note):
    if isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            return {}
    return note if isinstance(note, dict) else {}


# ---------------------------------------------------------------------------
# Komut
# ---------------------------------------------------------------------------

def load_run(run_dir):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / 'calistirma.json').read_text(encoding='utf-8'))
    records = json.loads((run_dir / 'kayitlar.json').read_text(encoding='utf-8'))
    return meta, records


def _readonly_connection():
    from src.database import get_connection
    conn = get_connection()
    try:
        conn.start_transaction(readonly=True)
    except Exception:  # noqa: BLE001 - sürücü desteklemiyorsa koruma yine yalnız SELECT'e izin verir
        pass
    return conn


def load_haric(path):
    """Aktarım dışı listesi: {'haric': {satir_hash: gerekçe}} (scripts/rag_semantik_pilot.py aktarim_disi) ya da
    doğrudan {satir_hash: gerekçe}. Yol verilmezse None."""
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return dict(data.get('haric', data) if isinstance(data, dict) else {})


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m src.rag_aktarim', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='komut', required=True)
    t = sub.add_parser('taslak', help='kayitlar.json -> pending taslak (veritabanına yazmaz)')
    t.add_argument('--calistirma', required=True)
    t.add_argument('--pdf', help='varsayılan: data/pdfs/<calistirma.json pdf>')
    t.add_argument('--sql-okuma-yok', action='store_true', help='mevcut SQL kayıtlarıyla tekrar kontrolü yapma')
    t.add_argument('--cikti', help=f'varsayılan: {TASLAK_DIZINI}/<belge>_rag_taslak_<zaman>.json')
    t.add_argument('--aktarim-disi', help='aktarım dışı listesi (JSON: {"haric": {satir_hash: gerekçe}})')
    t.add_argument('--dogrulama-raporu', action='store_true',
                   help='aynı taslağın adaylarından deterministik doğrulama raporu (<taslak>.dogrulama.json); aday '
                        'dosyasının yolu ve SHA-256 değeri rapora yazılır (kalite-yaz bağ denetimi)')
    a = sub.add_parser('aktar', help='yalnız verified taslak kayıtlarını ek olarak yaz (varsayılan prova)')
    a.add_argument('--pdf', required=True)
    a.add_argument('--taslak', required=True)
    a.add_argument('--yaz', action='store_true')
    a.add_argument('--yedek')
    a.add_argument('--aktarim-disi', help='aktarım dışı listesi; listedeki verified kayıt varsa aktarım durur')
    args = parser.parse_args(argv)

    if args.komut == 'taslak':
        meta, records = load_run(args.calistirma)
        doc, pages = pdf_pages(args.pdf or f"data/pdfs/{meta['pdf']}")
        existing = []
        if not args.sql_okuma_yok:
            conn = _readonly_connection()
            try:
                existing = existing_rows(KorumaliBaglanti(conn), doc['document_id'])
            finally:
                conn.rollback()
                conn.close()
        draft = build_draft(records, pages, meta, existing, haric=load_haric(args.aktarim_disi))
        out = Path(args.cikti) if args.cikti else TASLAK_DIZINI / (
            f"{Path(meta['pdf']).stem}_rag_taslak_{datetime.now():%Y%m%d-%H%M%S}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'x', encoding='utf-8') as handle:
            json.dump({'pdf': meta['pdf'], 'document_id': doc['document_id'], 'calistirma': str(args.calistirma),
                       'sayilar': draft['sayilar'], 'adaylar': [serializable(r) for r in draft['adaylar']],
                       'inceleme': draft['inceleme'], 'kesin_tekrar': draft['kesin_tekrar']},
                      handle, ensure_ascii=False, indent=2, default=str)
        print(json.dumps(draft['sayilar'], ensure_ascii=False))
        print(f'Taslak: {out} (bütün adaylar pending; insan onayı gerekli)')
        if args.dogrulama_raporu:
            from src import rag_dogrulama as V
            report = V.verify(records, draft, pages, doc['document_id'])
            report['aday_dosyasi'] = {'yol': out.as_posix(), 'sha256': hashlib.sha256(out.read_bytes()).hexdigest()}
            report_path = out.with_name(out.stem + '.dogrulama.json')
            V.aday_dosyasi(report_path, report, rapor_okunur=False)   # bağ denetimi: kimlikler = taslak adayları
            with open(report_path, 'x', encoding='utf-8') as handle:
                handle.write(V.dumps(report))
            print(json.dumps(report['ozet'], ensure_ascii=False))
            print(f'Doğrulama raporu: {report_path} (otomatik deterministik; insan onayı değildir)')
        return draft

    from src.aktarim import yedek_kontrolu
    if args.yaz:
        yedek_kontrolu(args.yedek)
    data = json.loads(Path(args.taslak).read_text(encoding='utf-8'))
    doc, pages = pdf_pages(args.pdf)
    if data.get('document_id') != doc['document_id']:
        raise AdaptorHatasi('taslak bu PDF\'e ait değil (document_id farklı)')
    from src.database import get_connection
    raw = get_connection()
    try:
        conn = KorumaliBaglanti(raw, yazma=True, commit_izni=args.yaz)
        result = aktar(conn, doc, pages, data['adaylar'], yaz=args.yaz, haric=load_haric(args.aktarim_disi))
    finally:
        if not args.yaz:
            raw.rollback()
        raw.close()
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
