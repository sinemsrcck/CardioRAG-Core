"""RAG taslak adaylarının yerel klinik incelemesi. Karar insanındır; araç hiçbir kaydı kendiliğinden onaylamaz.

Komutlar (repo kökünden):
    python scripts/rag_inceleme.py hazirla      --taslak <taslak.json> [--pdf <pdf>] [--kararlar <dosya>]
    python scripts/rag_inceleme.py incele       --taslak <taslak.json> --inceleyen <ad> [--port 8765] [--tarayici-acma]
    python scripts/rag_inceleme.py disari_aktar --taslak <taslak.json> [--cikti-dizini <dizin>]

Kurallar:
  - Özgün taslak ve PDF yalnız okunur. Taslağın ve PDF'in SHA-256 değerleri karar dosyasına yazılır; her komutta ve
    her karar yazımında yeniden hesaplanır. Fark varsa işlem durur. PDF'in SHA-256 değeri taslaktaki document_id ile
    aynı olmalıdır.
  - Yalnız 'adaylar' incelenir. 'inceleme' (olası tekrar ve diğer gerekçeli kayıtlar) ve 'kesin_tekrar' kapsam dışıdır;
    dışarı aktarılan kopyada olduğu gibi kalır.
  - Kararlar ayrı dosyada (varsayılan data/review/rag_inceleme/<pdf>.kararlar.json), satir_hash anahtarıyla tutulur
    ve atomik olarak (geçici dosya + os.replace) yazılır. İnceleme kaldığı yerden sürer.
  - Karar değerleri: onay, ret (gerekçe zorunlu), sonra. Karar yoksa kayıt kararsızdır.
  - Gruplu tablo satırı (satırın herhangi bir kaydında hasta_grubu): satırın bütün kayıtlarına aynı karar verilir
    (rag_aktarim.group_harmony ile aynı satır anahtarı).
  - disari_aktar: özgün taslağın yeni kopyası; YALNIZ açıkça onaylanan adayların review_status alanı 'verified' olur.
    Başka hiçbir alan değişmez (taslak_notu ve satir_hash aynı kalır). Onaylananlar rag_aktarim.source_check ve
    group_harmony kontrollerinden geçmelidir. Sonra: python -m src.rag_aktarim aktar --pdf <pdf> --taslak <kopya>
  - Yerel sunucu yalnız 127.0.0.1'e bağlanır; yalnız bu makalenin PDF'ini ve inceleme verisini sunar. Yazma isteği
    oturum anahtarı, Host / Origin denetimi ve JSON içerik türü ister. Sayfa içeriği yalnız textContent ile yazılır.
Veritabanına bağlanmaz, API / LLM çağırmaz.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pilot_clinical import STRUCTURED_FIELDS, pdf_pages  # noqa: E402
from src.rag_aktarim import (_caption_above, _caption_in_header, _row_index, _table_block, _value_index,  # noqa: E402
                             group_harmony, satir_hash, source_check, sql_row_key)
from src.rag_klinik_cikarim import _SEPARATOR_ROW, locate_header, row_cells  # noqa: E402

SURUM = 'rag-inceleme-v1'
KARAR_DIZINI = ROOT / 'data' / 'review' / 'rag_inceleme'
PDF_DIZINI = ROOT / 'data' / 'pdfs'
KARARLAR = ('onay', 'ret', 'sonra')
KARAR_ALANLARI = frozenset({'karar', 'gerekce', 'inceleyen', 'zaman'})
UST_ALANLAR = frozenset({'surum', 'taslak', 'taslak_sha256', 'pdf', 'pdf_sha256', 'document_id', 'aday_sayisi',
                         'olusturma', 'kararlar'})
GEREKCE_MAX = 2000
INCELEYEN_MAX = 100
HOST = '127.0.0.1'
GOVDE_MAX = 64 * 1024
# Her zaman birlikte gösterilen temel alanlar (boşsa '—'); ardından diğer bütün alanlar.
TEMEL_ALANLAR = (('klinik_bilgi_en', 'Klinik bulgu (EN)'), ('bilgi_turu', 'Bilgi türü'), ('baglam', 'Bağlam'),
                 ('deger', 'Sayısal değer'), ('unit', 'Birim'), ('hasta_grubu', 'Hasta grubu'))
DIGER_ALANLAR = (('klinik_bilgi', 'Klinik bulgu (TR)'), ('p', 'p'), ('ci95', 'GA %95'), ('etki_turu', 'Etki türü'),
                 ('comparator', 'Karşılaştırma'), ('sonuc_olcutu_tr', 'Sonuç ölçütü'), ('mudahale_en', 'Müdahale'),
                 ('mudahale_turu', 'Müdahale türü'), ('outcome_type', 'Sonuç türü'), ('kanit_turu', 'Kanıt türü'),
                 ('sayfa', 'Sayfa'), ('sayfa_bitis', 'Bitiş sayfası'), ('birlestirme', 'Birleştirme'))
GOSTERILMEYEN = frozenset({'review_status', 'taslak_notu', 'kanit_cumle', 'kanit_parcalari'})


class IncelemeHatasi(Exception):
    pass


# ---------------------------------------------------------------------------
# Dosya yardımcıları
# ---------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _strict_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise IncelemeHatasi(f'JSON içinde mükerrer anahtar: {key!r}')
        out[key] = value
    return out


def strict_loads(text):
    """Mükerrer anahtar içeren JSON'u reddeder (sessizce sonuncuyu almaz)."""
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs)
    except ValueError as exc:
        raise IncelemeHatasi(f'geçersiz JSON: {exc}') from None


def atomic_write_json(path, data):
    """Aynı dizinde geçici dosyaya yazar, fsync, os.replace. Yarım dosya kalmaz."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha256_bytes(text.encode('utf-8'))


def _note(rec):
    note = rec.get('taslak_notu')
    if isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            raise IncelemeHatasi('taslak_notu JSON değil') from None
    if not isinstance(note, dict):
        raise IncelemeHatasi('taslak_notu JSON nesnesi değil')
    return note


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Makale: taslak + PDF + inceleme birimleri
# ---------------------------------------------------------------------------

def varsayilan_pdf(taslak):
    name = taslak.get('pdf')
    if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith('.pdf'):
        raise IncelemeHatasi(f'taslaktaki pdf adı geçersiz: {name!r}')
    return PDF_DIZINI / name


def varsayilan_kararlar(taslak):
    return KARAR_DIZINI / f"{Path(varsayilan_pdf(taslak).name).stem}.kararlar.json"


def metin_araliklari(page_text, quote):
    """Kanıtın sayfa metnindeki konumları (boşluk farkı toleranslı, içerik birebir)."""
    tokens = str(quote or '').split()
    if not tokens:
        return []
    pattern = r'\s+'.join(re.escape(t) for t in tokens)
    return [[m.start(), m.end()] for m in re.finditer(pattern, page_text or '')]


def _evidence_parts(rec):
    parts = rec.get('kanit_parcalari')
    if parts:
        return [{'sayfa': p.get('sayfa'), 'metin': str(p.get('metin', ''))} for p in parts]
    return [{'sayfa': rec.get('sayfa'), 'metin': str(rec.get('kanit_cumle') or '')}]


def tablo_gorunumu(rec, pages):
    """Kanıt satırının bulunduğu tablo bloğunun tamamı: başlık üstü satırlar, bütün satırlar (hücreleriyle), ana ve
    seçilen başlık satırı, kanıt satırı; tablo sayfanın başındaysa önceki sayfadaki tablonun başlık satırları."""
    page = rec.get('sayfa')
    text = str(pages.get(page, ''))
    lines = text.splitlines()
    at = _row_index(lines, str(rec.get('kanit_cumle') or ''))
    if at is None:
        return {'sayfa': page, 'hata': 'kanıt satırı sayfada tekil değil; PDF üzerinde kontrol edin'}
    start = _table_block(lines, at)
    end = at
    while end + 1 < len(lines) and lines[end + 1].strip().startswith('|'):
        end += 1
    header, main, problem = locate_header(rec, text)
    rows, main_at, header_at = [], None, None
    for j in range(start, end + 1):
        s = lines[j].strip()
        sep = bool(_SEPARATOR_ROW.match(s))
        rows.append({'hucreler': row_cells(s), 'ayirici': sep, 'kanit': j == at})
        if sep and main_at is None and j > start:
            main_at = j - 1 - start
    if header is not None:
        for j in range(at - 1, start - 1, -1):
            if row_cells(lines[j].strip()) == header:
                header_at = j - start
                break
    above, k = [], start - 1
    while k >= 0 and len(above) < 4 and not lines[k].strip().startswith('|'):
        if lines[k].strip():
            above.insert(0, lines[k].strip())
        k -= 1
    caption = _caption_above(lines, start) or _caption_in_header(lines, start, at)
    onceki = None
    prev = str(pages.get(page - 1, '')).splitlines() if isinstance(page, int) else []
    first_table = not any(line.strip().startswith('|') for line in lines[:start])
    if caption is None and first_table and prev:
        seps = [j for j, line in enumerate(prev) if _SEPARATOR_ROW.match(line.strip())]
        if seps and seps[-1] > 0:
            pstart = _table_block(prev, seps[-1])
            onceki = {'sayfa': page - 1, 'baslik': list(_caption_above(prev, pstart) or ()) or None,
                      'satirlar': [row_cells(prev[j].strip()) for j in range(pstart, seps[-1])]}
    return {'sayfa': page, 'satirlar': rows, 'ana_baslik': main_at, 'secilen_baslik': header_at,
            'baslik': header, 'baslik_sorunu': problem, 'ust_satirlar': above,
            'sayfadaki_baslik': list(caption) if caption else None, 'onceki_sayfa': onceki}


def kayit_gorunumu(rec, h, pages, header):
    note = _note(rec)
    temel = [[label, rec.get(key)] for key, label in TEMEL_ALANLAR]
    known = {k for k, _ in TEMEL_ALANLAR} | {k for k, _ in DIGER_ALANLAR}
    diger = [[label, rec.get(key)] for key, label in DIGER_ALANLAR if rec.get(key) not in (None, '')]
    diger += [[key, rec[key]] for key in sorted(rec) if key not in known and key not in GOSTERILMEYEN]
    kanit = []
    for part in _evidence_parts(rec):
        kanit.append({**part, 'araliklar': metin_araliklari(pages.get(part['sayfa'], ''), part['metin'])})
    end = rec.get('sayfa_bitis') or rec.get('sayfa')
    if not rec.get('kanit_parcalari') and end != rec.get('sayfa'):
        kanit[0]['bitis'] = end
    cell = None
    if rec.get('kanit_turu') == 'tablo' and header is not None:
        index = _value_index(rec, header)
        if index is not None:
            cell = {'hucre': index[0], 'satir_ici': index[1],
                    'baslik': header[index[0]] if index[0] < len(header) else None}
    return {'hash': h, 'temel': temel, 'diger': diger, 'not': note,
            'otomatik': list(note.get('otomatik_duzeltme') or []), 'kanit': kanit,
            'kanit_cumle': rec.get('kanit_cumle'), 'sayfa': rec.get('sayfa'), 'hucre': cell}


def birimleri_kur(candidates):
    """Metin kaydı = tek birim; tablo kaydı = satır anahtarı (tablo_no, satir_no) başına bir birim. Gruplu satır:
    satırdaki herhangi bir kayıtta hasta_grubu var (rag_aktarim.group_harmony'deki SQL kuralı)."""
    order, by_key = [], {}
    for i, rec in enumerate(candidates):
        key = sql_row_key({**rec, 'taslak_notu': _note(rec)})
        if key is None or key[2] is None:
            key = ('k', i)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(i)
    units = []
    for key in order:
        idx = by_key[key]
        table = key[0] == 't'
        pages = [candidates[i].get('sayfa') for i in idx]
        units.append({'anahtar': key, 'tur': 'tablo' if table else 'metin', 'kayitlar': idx,
                      'grup': table and any(candidates[i].get('hasta_grubu') for i in idx),
                      'sayfa': min(p for p in pages if isinstance(p, int))})
    units.sort(key=lambda u: (u['sayfa'], u['kayitlar'][0]))
    return units


def makale_olustur(taslak, taslak_sha, pdf_sha, pages, taslak_yolu=None, pdf_yolu=None):
    if not isinstance(taslak, dict) or not isinstance(taslak.get('adaylar'), list):
        raise IncelemeHatasi('taslak biçimi beklenmiyor (adaylar listesi yok)')
    if pdf_sha != taslak.get('document_id'):
        raise IncelemeHatasi('PDF SHA-256 değeri taslaktaki document_id ile aynı değil; işlem durdu')
    candidates = taslak['adaylar']
    if not candidates:
        raise IncelemeHatasi('taslakta aday yok')
    for i, rec in enumerate(candidates, 1):
        if not isinstance(rec, dict):
            raise IncelemeHatasi(f'aday {i} nesne değil')
        if rec.get('review_status') != 'pending':
            raise IncelemeHatasi(f'aday {i}: özgün taslakta review_status pending olmalı ({rec.get("review_status")!r})')
        _note(rec)
    hashes = [satir_hash(taslak['document_id'], rec) for rec in candidates]
    if len(set(hashes)) != len(hashes):
        raise IncelemeHatasi('adaylarda mükerrer satir_hash var; karar anahtarı tekil olmalı')
    units = birimleri_kur(candidates)
    for n, unit in enumerate(units, 1):
        first = candidates[unit['kayitlar'][0]]
        unit['id'] = f'b{n}'
        unit['tablo'] = tablo_gorunumu(first, pages) if unit['tur'] == 'tablo' else None
        header = unit['tablo'].get('baslik') if unit['tablo'] else None
        unit['gorunum'] = [kayit_gorunumu(candidates[i], hashes[i], pages, header) for i in unit['kayitlar']]
    review = taslak.get('inceleme') or []
    return {'taslak': taslak, 'taslak_sha256': taslak_sha, 'pdf_sha256': pdf_sha, 'pages': pages,
            'taslak_yolu': Path(taslak_yolu) if taslak_yolu else None, 'pdf_yolu': Path(pdf_yolu) if pdf_yolu else None,
            'hashes': hashes, 'hash_index': {h: i for i, h in enumerate(hashes)}, 'birimler': units,
            'birim_of': {hashes[i]: u for u in units for i in u['kayitlar']},
            'kapsam_disi': {'olasi_tekrar': sum(1 for r in review if r.get('asama') == 'olasi_tekrar'),
                            'diger_inceleme': sum(1 for r in review if r.get('asama') != 'olasi_tekrar'),
                            'kesin_tekrar': len(taslak.get('kesin_tekrar') or [])}}


def makale_yukle(taslak_yolu, pdf_yolu=None, sayfa_okuyucu=None):
    sayfa_okuyucu = sayfa_okuyucu or pdf_pages
    taslak_yolu = Path(taslak_yolu)
    raw = taslak_yolu.read_bytes()
    taslak = strict_loads(raw.decode('utf-8'))
    pdf_yolu = Path(pdf_yolu) if pdf_yolu else varsayilan_pdf(taslak)
    if not pdf_yolu.is_file():
        raise IncelemeHatasi(f'PDF bulunamadı: {pdf_yolu}')
    pdf_sha = sha256_file(pdf_yolu)
    doc, pages = sayfa_okuyucu(str(pdf_yolu))
    if doc.get('document_id') != pdf_sha:
        raise IncelemeHatasi('PDF okunurken document_id farklı çıktı; işlem durdu')
    return makale_olustur(taslak, sha256_bytes(raw), pdf_sha, pages, taslak_yolu, pdf_yolu)


def parmak_izi_kontrol(makale):
    """Özgün taslak ve PDF diskte hâlâ aynı mı (her yazımdan önce)."""
    if makale['taslak_yolu'] is not None and sha256_file(makale['taslak_yolu']) != makale['taslak_sha256']:
        raise IncelemeHatasi('özgün taslak dosyası değişti; işlem durdu')
    if makale['pdf_yolu'] is not None and sha256_file(makale['pdf_yolu']) != makale['pdf_sha256']:
        raise IncelemeHatasi('PDF dosyası değişti; işlem durdu')


# ---------------------------------------------------------------------------
# Karar dosyası
# ---------------------------------------------------------------------------

def bos_karar_dosyasi(makale):
    return {'surum': SURUM, 'taslak': str(makale['taslak_yolu'] or ''), 'taslak_sha256': makale['taslak_sha256'],
            'pdf': makale['taslak']['pdf'], 'pdf_sha256': makale['pdf_sha256'],
            'document_id': makale['taslak']['document_id'], 'aday_sayisi': len(makale['hashes']),
            'olusturma': _now(), 'kararlar': {}}


def _gecerli_zaman(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def kararlari_dogrula(doc, makale):
    """Hata listesi (boşsa geçerli). Bilinmeyen / geçersiz karar, yanlış dosyaya ait karar dosyası ve bölünmüş gruplu
    satır reddedilir. Mükerrer anahtar dosya okunurken (strict_loads) reddedilir."""
    errors = []
    if not isinstance(doc, dict):
        return ['karar dosyası JSON nesnesi değil']
    extra = sorted(set(doc) - UST_ALANLAR)
    if extra:
        errors.append(f'karar dosyasında bilinmeyen alan: {extra}')
    if doc.get('surum') != SURUM:
        errors.append(f"karar dosyası sürümü {doc.get('surum')!r}, beklenen {SURUM!r}")
    if doc.get('taslak_sha256') != makale['taslak_sha256']:
        errors.append('karar dosyası bu taslağa ait değil ya da özgün taslak değişti (taslak_sha256 farklı)')
    if doc.get('pdf_sha256') != makale['pdf_sha256'] or doc.get('document_id') != makale['taslak']['document_id']:
        errors.append('karar dosyası bu PDF\'e ait değil ya da PDF değişti')
    if doc.get('aday_sayisi') != len(makale['hashes']):
        errors.append('karar dosyasındaki aday sayısı taslakla uyuşmuyor')
    decisions = doc.get('kararlar')
    if not isinstance(decisions, dict):
        return errors + ['kararlar nesne değil']
    for h, entry in decisions.items():
        if h not in makale['hash_index']:
            errors.append(f'bilinmeyen kayıt (satir_hash taslakta yok): {h}')
            continue
        if not isinstance(entry, dict):
            errors.append(f'{h}: karar nesne değil')
            continue
        if set(entry) != KARAR_ALANLARI:
            errors.append(f'{h}: karar alanları {sorted(entry)}; beklenen {sorted(KARAR_ALANLARI)}')
            continue
        if entry['karar'] not in KARARLAR:
            errors.append(f"{h}: geçersiz karar {entry['karar']!r}")
        if not isinstance(entry['gerekce'], str) or len(entry['gerekce']) > GEREKCE_MAX:
            errors.append(f'{h}: gerekçe metin değil ya da çok uzun')
        elif entry['karar'] == 'ret' and not entry['gerekce'].strip():
            errors.append(f'{h}: ret kararı gerekçesiz')
        if (not isinstance(entry['inceleyen'], str) or not entry['inceleyen'].strip()
                or len(entry['inceleyen']) > INCELEYEN_MAX):
            errors.append(f'{h}: inceleyen geçersiz')
        if not _gecerli_zaman(entry['zaman']):
            errors.append(f'{h}: zaman saat dilimli ISO-8601 değil')
    for unit in makale['birimler']:
        if unit['grup']:
            got = {_karar_of(decisions, makale['hashes'][i]) for i in unit['kayitlar']}
            if len(got) > 1:
                errors.append(f"gruplu satır bölünmüş ({unit['id']}, sayfa {unit['sayfa']}): bütün grup kayıtları aynı "
                              f'kararı almalı')
    return errors


def _karar_of(decisions, h):
    entry = decisions.get(h)
    karar = entry.get('karar') if isinstance(entry, dict) else None
    return None if karar == 'sonra' else karar


def karar_dosyasi_oku(path, makale):
    path = Path(path)
    raw = path.read_bytes()
    doc = strict_loads(raw.decode('utf-8'))
    errors = kararlari_dogrula(doc, makale)
    if errors:
        raise IncelemeHatasi('karar dosyası reddedildi:\n  - ' + '\n  - '.join(errors[:20]))
    return doc, sha256_bytes(raw)


def ilerleme(doc, makale):
    counts = {'onay': 0, 'ret': 0, 'sonra': 0, 'kararsiz': 0}
    for h in makale['hashes']:
        entry = doc['kararlar'].get(h)
        counts[entry['karar'] if entry else 'kararsiz'] += 1
    counts['toplam'] = len(makale['hashes'])
    return counts


def karar_uygula(doc, makale, birim_id, istek, inceleyen):
    """Bir birim için gelen kararları uygular ve YENİ karar belgesi döndürür (doğrulanmış). istek: {hash: {karar,
    gerekce}}. Birim dışı kayıt, gruplu satırın bölünmesi, geçersiz karar reddedilir."""
    unit = next((u for u in makale['birimler'] if u['id'] == birim_id), None)
    if unit is None:
        raise IncelemeHatasi('bilinmeyen birim')
    if not isinstance(istek, dict) or not istek:
        raise IncelemeHatasi('karar listesi boş')
    members = {makale['hashes'][i] for i in unit['kayitlar']}
    if not isinstance(inceleyen, str) or not inceleyen.strip() or len(inceleyen) > INCELEYEN_MAX:
        raise IncelemeHatasi('inceleyen geçersiz')
    new = copy.deepcopy(doc)
    zaman = _now()
    for h, item in istek.items():
        if h not in members:
            raise IncelemeHatasi('karar bu birimin kaydı için değil')
        if not isinstance(item, dict) or set(item) - {'karar', 'gerekce'}:
            raise IncelemeHatasi('karar isteği biçimi geçersiz')
        karar, gerekce = item.get('karar'), item.get('gerekce', '')
        if karar not in KARARLAR or not isinstance(gerekce, str):
            raise IncelemeHatasi('geçersiz karar')
        new['kararlar'][h] = {'karar': karar, 'gerekce': gerekce.strip(), 'inceleyen': inceleyen.strip(), 'zaman': zaman}
    errors = kararlari_dogrula(new, makale)
    if errors:
        raise IncelemeHatasi('; '.join(errors[:5]))
    return new


# ---------------------------------------------------------------------------
# Dışarı aktarma
# ---------------------------------------------------------------------------

def inceleme_kopyasi(makale, doc, kararlar_yolu=None, kararlar_sha=None):
    """Özgün taslağın kopyası; yalnız 'onay' kararlı adaylarda review_status='verified'. Değişmezlikler ve adaptör
    kontrolleri (source_check, group_harmony) burada doğrulanır."""
    errors = kararlari_dogrula(doc, makale)
    if errors:
        raise IncelemeHatasi('karar dosyası reddedildi: ' + '; '.join(errors[:5]))
    original = makale['taslak']
    out = copy.deepcopy(original)
    approved = [i for i, h in enumerate(makale['hashes']) if _karar_of(doc['kararlar'], h) == 'onay']
    if not approved:
        raise IncelemeHatasi('onaylanan kayıt yok; kopya üretilmedi')
    for i in approved:
        out['adaylar'][i]['review_status'] = 'verified'
    doc_id = original['document_id']
    for i, (a, b) in enumerate(zip(original['adaylar'], out['adaylar'])):
        changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k) or (k in a) != (k in b)}
        if changed - {'review_status'} or (changed and i not in approved):
            raise IncelemeHatasi(f'aday {i + 1}: review_status dışında alan değişti ({sorted(changed)})')
        if b.get('taslak_notu') != a.get('taslak_notu') or satir_hash(doc_id, b) != makale['hashes'][i]:
            raise IncelemeHatasi(f'aday {i + 1}: taslak_notu / satir_hash değişti')
    if len(out['adaylar']) != len(original['adaylar']) or any(out[k] != original[k] for k in original if k != 'adaylar'):
        raise IncelemeHatasi('taslağın aday dışı bölümleri değişti')
    verified = [out['adaylar'][i] for i in approved]
    for i, rec in zip(approved, verified):
        problem = source_check({**rec, 'taslak_notu': _note(rec)}, makale['pages'])
        if problem:
            raise IncelemeHatasi(f'aday {i + 1}: kaynak doğrulaması: {problem}')
    if group_harmony([{**r, 'taslak_notu': _note(r)} for r in verified]):
        raise IncelemeHatasi('onaylananlarda grup karşılaştırması eksik (satırın bütün grupları onaylanmalı)')
    counts = ilerleme(doc, makale)
    out['rag_inceleme'] = {'surum': SURUM, 'kaynak_taslak': str(makale['taslak_yolu'] or ''),
                           'kaynak_taslak_sha256': makale['taslak_sha256'], 'pdf_sha256': makale['pdf_sha256'],
                           'kararlar_dosyasi': str(kararlar_yolu or ''), 'kararlar_sha256': kararlar_sha,
                           'verified': len(approved), 'ret': counts['ret'],
                           'pending_kalan': len(out['adaylar']) - len(approved), 'olusturma': _now()}
    return out


def kopyayi_yaz(out, cikti_dizini, pdf_name):
    cikti_dizini = Path(cikti_dizini)
    path = cikti_dizini / f"{Path(pdf_name).stem}_rag_inceleme_{datetime.now():%Y%m%d-%H%M%S}.json"
    if path.exists():
        raise IncelemeHatasi(f'çıktı zaten var: {path}')
    atomic_write_json(path, out)
    if strict_loads(path.read_text(encoding='utf-8')) != out:
        raise IncelemeHatasi('yazılan kopya geri okunduğunda farklı')
    return path


# ---------------------------------------------------------------------------
# Yerel inceleme sunucusu
# ---------------------------------------------------------------------------

class IncelemeSunucusu(HTTPServer):
    allow_reuse_address = False

    def __init__(self, port, makale, kararlar_yolu, inceleyen):
        super().__init__((HOST, port), IncelemeIstegi)
        if self.server_address[0] != HOST:
            raise IncelemeHatasi('sunucu yalnız 127.0.0.1 üzerinde açılabilir')
        self.makale = makale
        self.kararlar_yolu = Path(kararlar_yolu)
        try:
            self.doc, self.kararlar_sha = karar_dosyasi_oku(self.kararlar_yolu, makale)
        except BaseException:
            self.server_close()
            raise
        self.inceleyen = inceleyen
        self.anahtar = secrets.token_hex(24)
        self.kilit = threading.Lock()
        self.durdu = None
        port = self.server_address[1]
        self.hostlar = {f'{HOST}:{port}', f'localhost:{port}'}
        self.kokenler = {f'http://{h}' for h in self.hostlar}

    def veri(self):
        m = self.makale
        pages = sorted({p for u in m['birimler'] for g in u['gorunum'] for p in
                        [g['sayfa']] + [k['sayfa'] for k in g['kanit']] + [k.get('bitis') for k in g['kanit']]
                        if isinstance(p, int)})
        units = [{'id': u['id'], 'tur': u['tur'], 'grup': u['grup'], 'sayfa': u['sayfa'], 'tablo': u['tablo'],
                  'kayitlar': u['gorunum']} for u in m['birimler']]
        return {'makale': {'pdf': m['taslak']['pdf'], 'aday': len(m['hashes']), 'birim': len(units),
                           'kapsam_disi': m['kapsam_disi']},
                'inceleyen': self.inceleyen, 'birimler': units,
                'sayfalar': {str(p): m['pages'].get(p, '') for p in pages}, 'kararlar': self.doc['kararlar']}

    def karar_kaydet(self, birim_id, istek):
        with self.kilit:
            if self.durdu:
                raise IncelemeHatasi(self.durdu)
            try:
                parmak_izi_kontrol(self.makale)
                if sha256_file(self.kararlar_yolu) != self.kararlar_sha:
                    raise IncelemeHatasi('karar dosyası bu oturum dışında değişti; işlem durdu (sunucuyu yeniden başlatın)')
            except IncelemeHatasi as exc:
                self.durdu = str(exc)
                raise
            new = karar_uygula(self.doc, self.makale, birim_id, istek, self.inceleyen)
            self.kararlar_sha = atomic_write_json(self.kararlar_yolu, new)
            self.doc = new
            unit = next(u for u in self.makale['birimler'] if u['id'] == birim_id)
            return {h: new['kararlar'].get(h) for h in (self.makale['hashes'][i] for i in unit['kayitlar'])}


class IncelemeIstegi(BaseHTTPRequestHandler):
    server_version = 'rag-inceleme'
    sys_version = ''

    def log_request(self, code='-', size='-'):  # başarılı istekler yazılmaz; yalnız 4xx / 5xx
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if status >= 400:
            super().log_request(code, size)

    def _headers(self, status, ctype, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self._headers(status, 'application/json; charset=utf-8', len(body), {'Cache-Control': 'no-store'})
        self.wfile.write(body)

    def _host_ok(self):
        return self.headers.get('Host', '') in self.server.hostlar

    def do_GET(self):
        if not self._host_ok():
            return self._json(421, {'hata': 'izin verilmeyen Host'})
        path = urlsplit(self.path).path
        if path == '/':
            nonce = secrets.token_hex(16)
            body = (SAYFA.replace('__NONCE__', nonce).replace('__ANAHTAR__', self.server.anahtar)).encode('utf-8')
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
                   "frame-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self._headers(200, 'text/html; charset=utf-8', len(body),
                          {'Content-Security-Policy': csp, 'X-Frame-Options': 'DENY', 'Cache-Control': 'no-store'})
            return self.wfile.write(body)
        if path == '/api/veri':
            if self.headers.get('X-Inceleme-Anahtari') != self.server.anahtar:
                return self._json(403, {'hata': 'oturum anahtarı yok'})
            if self.server.durdu:
                return self._json(409, {'hata': self.server.durdu})
            return self._json(200, self.server.veri())
        if path == '/pdf':
            m = self.server.makale
            try:
                data = m['pdf_yolu'].read_bytes()
            except OSError:
                return self._json(404, {'hata': 'PDF okunamadı'})
            if sha256_bytes(data) != m['pdf_sha256']:
                self.server.durdu = 'PDF dosyası değişti; işlem durdu'
                return self._json(409, {'hata': self.server.durdu})
            self._headers(200, 'application/pdf', len(data), {'Content-Disposition': 'inline',
                                                               'X-Frame-Options': 'SAMEORIGIN',
                                                               'Cache-Control': 'private, max-age=600'})
            return self.wfile.write(data)
        return self._json(404, {'hata': 'bulunamadı'})

    def do_POST(self):
        # Gövde (sınır içindeyse) denetimlerden önce okunur: okunmamış veriyle kapanan bağlantı Windows'ta sıfırlanır
        # ve istemci hata yanıtını göremez.
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > GOVDE_MAX:
            self.close_connection = True
            return self._json(413, {'hata': 'istek gövdesi geçersiz boyutta'})
        raw = self.rfile.read(length)
        if not self._host_ok():
            return self._json(421, {'hata': 'izin verilmeyen Host'})
        if urlsplit(self.path).path != '/api/karar':
            return self._json(404, {'hata': 'bulunamadı'})
        origin = self.headers.get('Origin')
        if origin is not None and origin not in self.server.kokenler:
            return self._json(403, {'hata': 'izin verilmeyen Origin'})
        if self.headers.get('X-Inceleme-Anahtari') != self.server.anahtar:
            return self._json(403, {'hata': 'oturum anahtarı geçersiz'})
        if (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
            return self._json(415, {'hata': 'JSON bekleniyor'})
        try:
            body = strict_loads(raw.decode('utf-8'))
            if not isinstance(body, dict) or set(body) != {'birim', 'kararlar'}:
                raise IncelemeHatasi('istek biçimi geçersiz')
            saved = self.server.karar_kaydet(body['birim'], body['kararlar'])
        except IncelemeHatasi as exc:
            return self._json(409 if self.server.durdu else 400, {'hata': str(exc)})
        except UnicodeDecodeError:
            return self._json(400, {'hata': 'UTF-8 değil'})
        return self._json(200, {'kararlar': saved})


SAYFA = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="inceleme-anahtari" content="__ANAHTAR__">
<title>RAG klinik inceleme</title>
<style nonce="__NONCE__">
:root{--bg:#f6f7f9;--fg:#1d232b;--mut:#5d6773;--card:#fff;--line:#d9dde3;--acc:#2a5bd7;--ok:#1d7a3e;--no:#b3261e;
--later:#8a6100;--warn:#fff4d6;--warnline:#e2b84a;--mark:#ffe58a;--sel:#dce8ff;--row:#eef6ee}
@media (prefers-color-scheme:dark){:root{--bg:#15181c;--fg:#e4e7eb;--mut:#9aa4af;--card:#1e2227;--line:#343a42;
--acc:#7ea2ff;--ok:#5fca85;--no:#ff8a80;--later:#e8b44c;--warn:#3a3016;--warnline:#8a6d1f;--mark:#6b5a12;--sel:#23365e;--row:#1f3324}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:15px}.mut{color:var(--mut)}.sayac span{margin-right:10px;white-space:nowrap}
.bar{height:6px;flex:1 1 160px;min-width:120px;background:var(--line);border-radius:3px;overflow:hidden;display:flex}
.bar i{display:block;height:100%}.i-onay{background:var(--ok)}.i-ret{background:var(--no)}.i-sonra{background:var(--later)}
select,button,input{font:inherit;color:inherit}select,input{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 6px}
button{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--acc)}button.onay{border-color:var(--ok);color:var(--ok)}button.ret{border-color:var(--no);color:var(--no)}
button.sonra{border-color:var(--later);color:var(--later)}
main{flex:1;display:flex;min-height:0}
#pdf{flex:1 1 50%;min-width:0;border-right:1px solid var(--line);display:flex;flex-direction:column}
#pdf iframe{flex:1;border:0;width:100%;background:#777}
#pdfbar{padding:4px 8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line);background:var(--card)}
#sag{flex:1 1 50%;min-width:0;overflow:auto;padding:12px}
.kart{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.kart.secili{outline:2px solid var(--acc)}
.rozet{display:inline-block;border-radius:10px;padding:0 8px;font-size:12px;border:1px solid var(--line);margin-right:4px}
.r-onay{color:var(--ok);border-color:var(--ok)}.r-ret{color:var(--no);border-color:var(--no)}.r-sonra{color:var(--later);border-color:var(--later)}
.uyari{background:var(--warn);border:1px solid var(--warnline);border-radius:6px;padding:6px 8px;margin:6px 0}
table.alan{border-collapse:collapse;width:100%;margin:6px 0}table.alan td{border-top:1px solid var(--line);padding:3px 6px;vertical-align:top}
table.alan td:first-child{width:34%;color:var(--mut)}
.tablo{overflow:auto;margin:6px 0}.tablo table{border-collapse:collapse;font-size:13px}
.tablo td{border:1px solid var(--line);padding:2px 6px;vertical-align:top}
.tablo tr.baslik td{font-weight:600}.tablo tr.ana td{background:color-mix(in srgb,var(--line) 40%,transparent)}
.tablo tr.kanit td{background:var(--row)}.tablo td.hucre{outline:2px solid var(--acc);background:var(--sel)}
.tablo td.sutun{background:var(--sel)}.tablo tr.ayirici{display:none}
.kanit{border-left:3px solid var(--acc);padding:4px 8px;margin:4px 0;white-space:pre-wrap}
.sayfametni{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12px;max-height:340px;overflow:auto;border:1px solid var(--line);padding:6px;border-radius:6px}
mark{background:var(--mark);color:inherit}
.dugmeler{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}.dugmeler input{flex:1 1 200px}
#hata{display:none;background:var(--no);color:#fff;padding:6px 12px}
details summary{cursor:pointer;color:var(--mut)}kbd{border:1px solid var(--line);border-radius:4px;padding:0 4px;font-size:12px}
@media (max-width:900px){main{flex-direction:column}#pdf{flex-basis:45vh;border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div id="hata" role="alert"></div>
<header>
  <b id="baslik">RAG klinik inceleme</b>
  <div class="sayac" id="sayac"></div><div class="bar" id="bar"></div>
  <label>Filtre <select id="filtre">
    <option value="acik">Açık (onay/ret yok)</option><option value="kararsiz">Hiç bakılmamış</option>
    <option value="sonra">Sonraya bırakılan</option><option value="onay">Onaylanan</option><option value="ret">Reddedilen</option>
    <option value="otomatik">Otomatik düzeltmeli</option><option value="tablo">Tablo</option><option value="metin">Metin</option>
    <option value="grup">Gruplu satır</option><option value="hepsi">Tümü</option></select></label>
  <span id="konum" class="mut"></span>
  <button id="onceki">◀ K</button><button id="sonraki">J ▶</button>
  <details><summary>Kısayollar</summary><div class="mut"><kbd>J</kbd>/<kbd>K</kbd> sonraki/önceki birim ·
  <kbd>↑</kbd>/<kbd>↓</kbd> birimdeki kayıt · <kbd>A</kbd> onayla · <kbd>R</kbd> reddet (gerekçe, <kbd>Enter</kbd>) ·
  <kbd>S</kbd> sonra · <kbd>Shift</kbd>+A/R/S birimin tamamı · <kbd>Esc</kbd> alandan çık</div></details>
</header>
<main><section id="pdf"><div id="pdfbar"></div><iframe id="pdfcerceve" title="PDF"></iframe></section>
<section id="sag"></section></main>
<script nonce="__NONCE__">
"use strict";
const ANAHTAR = document.querySelector('meta[name="inceleme-anahtari"]').content;
let V = null, sira = [], pos = 0, sec = 0, pdfSayfa = null;
const KARAR_ADI = {onay: 'Onaylandı', ret: 'Reddedildi', sonra: 'Sonra'};

function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
  return e;
}
function goster(v) { return v === null || v === undefined || v === '' ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v)); }
function hata(msg) { const h = document.getElementById('hata'); h.textContent = msg; h.style.display = msg ? 'block' : 'none'; }
function karar(h) { const k = V.kararlar[h]; return k ? k.karar : null; }
function birimKayitlari(u) { return u.kayitlar.map(r => r.hash); }

function uyar(u, f) {
  const ks = birimKayitlari(u).map(karar);
  switch (f) {
    case 'hepsi': return true;
    case 'acik': return ks.some(k => k !== 'onay' && k !== 'ret');
    case 'kararsiz': return ks.some(k => k === null);
    case 'sonra': case 'onay': case 'ret': return ks.some(k => k === f);
    case 'otomatik': return u.kayitlar.some(r => r.otomatik.length);
    case 'tablo': return u.tur === 'tablo';
    case 'metin': return u.tur === 'metin';
    case 'grup': return u.grup;
  }
  return true;
}
function siraKur(koruId) {
  const f = document.getElementById('filtre').value;
  sira = V.birimler.map((u, i) => i).filter(i => uyar(V.birimler[i], f));
  const at = koruId ? sira.findIndex(i => V.birimler[i].id === koruId) : -1;
  pos = at >= 0 ? at : Math.min(pos, Math.max(0, sira.length - 1));
}
function sayac() {
  const c = {onay: 0, ret: 0, sonra: 0, yok: 0};
  for (const u of V.birimler) for (const r of u.kayitlar) { const k = karar(r.hash); c[k || 'yok']++; }
  const n = V.makale.aday, bitti = V.birimler.filter(u => birimKayitlari(u).every(h => ['onay', 'ret'].includes(karar(h)))).length;
  const s = document.getElementById('sayac'); s.replaceChildren(
    el('span', '', `Karar: ${c.onay + c.ret} / ${n}`), el('span', 'r-onay', `onay ${c.onay}`), el('span', 'r-ret', `ret ${c.ret}`),
    el('span', 'r-sonra', `sonra ${c.sonra}`), el('span', 'mut', `kararsız ${c.yok}`),
    el('span', 'mut', `birim ${bitti} / ${V.birimler.length}`));
  const b = document.getElementById('bar'); b.replaceChildren();
  for (const k of ['onay', 'ret', 'sonra']) { const i = el('i', 'i-' + k); i.style.width = (100 * c[k] / n) + '%'; b.append(i); }
}

function pdfGit(sayfa) {
  if (!Number.isInteger(sayfa) || sayfa === pdfSayfa) return;
  pdfSayfa = sayfa;
  document.getElementById('pdfcerceve').setAttribute('src', `/pdf?s=${sayfa}#page=${sayfa}`);
}
function vurgulu(metin, araliklar) {
  const kap = el('div', 'sayfametni'); let at = 0;
  const ar = (araliklar || []).slice().sort((a, b) => a[0] - b[0]);
  for (const [s, e] of ar) { if (s < at) continue; kap.append(metin.slice(at, s), el('mark', '', metin.slice(s, e))); at = e; }
  kap.append(metin.slice(at)); return kap;
}

function tabloCiz(t, kayitlar) {
  const kap = el('div', 'kart');
  kap.append(el('b', '', 'Tablo bağlamı'));
  const n = kayitlar[0].not;
  const ozet = el('table', 'alan');
  for (const [a, v] of [['Tablo no', n.tablo_no], ['Tablo başlığı', n.tablo_basligi], ['Satır no', n.satir_no],
                        ['Satır değişkeni', n.degisken], ['Alt bölüm', n.alt_bolum], ['Sayfa', t.sayfa]])
    ozet.append(el('tr', '', el('td', '', a), el('td', '', goster(v))));
  kap.append(ozet);
  if (t.hata) { kap.append(el('div', 'uyari', t.hata)); return kap; }
  if (t.ust_satirlar && t.ust_satirlar.length) kap.append(el('div', 'mut', 'Tablonun üstündeki satırlar: ' + t.ust_satirlar.join(' · ')));
  if (t.onceki_sayfa) {
    const o = t.onceki_sayfa;
    kap.append(el('div', 'uyari', `Tablo sayfanın başında; başlığı önceki sayfada (s.${o.sayfa})` +
      (o.baslik ? `: Table ${o.baslik[0]} ${o.baslik[1] || ''}` : '') + '. Önceki sayfadaki başlık satırları:'));
    const pt = el('table'); for (const r of o.satirlar) pt.append(el('tr', 'baslik', ...r.map(c => el('td', '', c))));
    kap.append(el('div', 'tablo', pt));
  }
  if (t.baslik_sorunu) kap.append(el('div', 'uyari', 'Başlık satırı: ' + t.baslik_sorunu));
  const secili = kayitlar[sec] && kayitlar[sec].hucre;
  const tb = el('table');
  t.satirlar.forEach((r, i) => {
    let cls = r.ayirici ? 'ayirici' : '';
    if (i === t.ana_baslik) cls += ' baslik ana'; if (i === t.secilen_baslik) cls += ' baslik';
    if (r.kanit) cls += ' kanit';
    const tr = el('tr', cls.trim());
    r.hucreler.forEach((c, j) => {
      const td = el('td', '', c);
      if (secili && j === secili.hucre) td.className = r.kanit ? 'hucre' : (i === t.secilen_baslik || i === t.ana_baslik ? 'sutun' : '');
      tr.append(td);
    });
    tb.append(tr);
  });
  kap.append(el('div', 'tablo', tb));
  kap.append(el('div', 'mut', 'Yeşil satır kanıt satırıdır; seçili kaydın hücresi ve sütun başlığı mavi çerçeveyle gösterilir.'));
  return kap;
}

function kayitCiz(u, r, i) {
  const k = V.kararlar[r.hash];
  const kart = el('div', 'kart' + (i === sec ? ' secili' : ''));
  kart.addEventListener('click', () => { if (sec !== i) { sec = i; ciz(); } });
  const bas = el('div', '', el('b', '', `Kayıt ${i + 1} / ${u.kayitlar.length}`), ' ');
  bas.append(el('span', 'rozet' + (k ? ' r-' + k.karar : ''), k ? KARAR_ADI[k.karar] : 'Kararsız'));
  kart.append(bas);
  if (r.otomatik.length) kart.append(el('div', 'uyari', 'Otomatik düzeltme işareti: ' + r.otomatik.join(', ') + ' — kaynakla karşılaştırın.'));
  const t = el('table', 'alan');
  for (const [a, v] of r.temel) t.append(el('tr', '', el('td', '', a), el('td', '', goster(v))));
  for (const [a, v] of r.diger) t.append(el('tr', '', el('td', '', a), el('td', '', goster(v))));
  if (r.hucre) t.append(el('tr', '', el('td', '', 'Tablo hücresi'), el('td', '',
    `sütun ${r.hucre.hucre + 1}` + (r.hucre.satir_ici !== null ? `, hücre içi satır ${r.hucre.satir_ici + 1}` : '') + ` — başlık: ${goster(r.hucre.baslik)}`)));
  else if (u.tur === 'tablo') t.append(el('tr', '', el('td', '', 'Tablo hücresi'), el('td', '', 'konum belirlenemedi; PDF üzerinde kontrol edin')));
  kart.append(t);
  const notTablo = el('table', 'alan');
  for (const key of Object.keys(r.not)) notTablo.append(el('tr', '', el('td', '', key), el('td', '', goster(r.not[key]))));
  kart.append(el('details', '', el('summary', '', 'taslak_notu (tablo, satır, sütun, bölüm ve kaynak bilgisi)'), notTablo));
  kart.append(el('div', 'mut', r.kanit.length > 1 ? `Kanıt parçaları (${r.kanit.length})` : 'Kanıt'));
  r.kanit.forEach((p, j) => {
    const d = el('div', 'kanit');
    const git = el('button', '', `PDF s.${p.sayfa}`); git.addEventListener('click', ev => { ev.stopPropagation(); pdfSayfa = null; pdfGit(p.sayfa); });
    d.append(git, ` ${r.kanit.length > 1 ? 'Parça ' + (j + 1) + ' · ' : ''}s.${p.sayfa}${p.bitis ? '–' + p.bitis : ''}: `, p.metin);
    if (!p.araliklar.length) d.append(el('div', 'uyari', 'Bu metin sayfa metninde birebir konumlanamadı; PDF üzerinde kontrol edin.'));
    kart.append(d);
  });
  if (k) kart.append(el('div', 'mut', `${KARAR_ADI[k.karar]} — ${k.inceleyen}, ${k.zaman}` + (k.gerekce ? ` — gerekçe: ${k.gerekce}` : '')));
  if (!u.grup) kart.append(dugmeler(u, [r.hash], 'Bu kayıt', i));
  return kart;
}

function dugmeler(u, hashler, etiket, idx) {
  const d = el('div', 'dugmeler');
  const g = el('input'); g.type = 'text'; g.maxLength = 2000; g.placeholder = 'Ret gerekçesi (ret için zorunlu)';
  g.dataset.gerekce = String(idx);
  const ilk = V.kararlar[hashler[0]]; if (ilk && ilk.gerekce) g.value = ilk.gerekce;
  const kisa = idx === 'birim' ? 'Shift+' : '';
  for (const [kk, ad] of [['onay', 'Onayla'], ['ret', 'Reddet'], ['sonra', 'Sonra']]) {
    const b = el('button', kk, `${ad} (${kisa}${ad[0]})`);
    b.addEventListener('click', ev => { ev.stopPropagation(); gonder(u, hashler, kk, g); });
    d.append(b);
  }
  g.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); gonder(u, hashler, 'ret', g); } });
  d.prepend(el('span', 'mut', etiket + ':'));
  d.append(g);
  return d;
}

async function gonder(u, hashler, kk, girdi) {
  const gerekce = (girdi && girdi.value || '').trim();
  if (kk === 'ret' && !gerekce) { hata('Ret için gerekçe yazın ve Enter\'a basın.'); if (girdi) girdi.focus(); return; }
  hata('');
  const istek = {}; for (const h of hashler) istek[h] = {karar: kk, gerekce: kk === 'ret' ? gerekce : ''};
  try {
    const r = await fetch('/api/karar', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Inceleme-Anahtari': ANAHTAR},
      body: JSON.stringify({birim: u.id, kararlar: istek})});
    const j = await r.json();
    if (!r.ok) { hata('Kaydedilmedi: ' + (j.hata || r.status)); return; }
    for (const [h, v] of Object.entries(j.kararlar)) { if (v) V.kararlar[h] = v; else delete V.kararlar[h]; }
    const acik = u.kayitlar.findIndex(x => !V.kararlar[x.hash]);
    if (acik >= 0 && !u.grup) sec = acik;
    ciz();
  } catch (e) { hata('Sunucuya ulaşılamadı: ' + e); }
}

function ciz() {
  sayac();
  const sag = document.getElementById('sag');
  const konum = document.getElementById('konum');
  if (!sira.length) { sag.replaceChildren(el('div', 'kart', 'Bu filtrede birim yok.')); konum.textContent = ''; return; }
  const u = V.birimler[sira[pos]];
  sec = Math.min(sec, u.kayitlar.length - 1);
  konum.textContent = `${pos + 1} / ${sira.length} (filtre) · birim ${u.id}`;
  try { localStorage.setItem('rag-inceleme:' + V.makale.pdf, u.id); } catch (e) { /* isteğe bağlı */ }
  const out = [];
  const bas = el('div', 'kart', el('b', '', u.tur === 'tablo' ? 'Tablo satırı' : 'Metin kaydı'), ` · sayfa ${u.sayfa} · ${u.kayitlar.length} kayıt`);
  if (u.grup) bas.append(el('div', 'uyari', 'Gruplu satır: bu satırdaki grup kayıtları birlikte değerlendirilir. Karar yalnızca satırın tamamı için verilir.'));
  if (u.tur === 'tablo' && u.kayitlar.length > 1 || u.grup) bas.append(dugmeler(u, birimKayitlari(u), 'Satırın tamamı', 'birim'));
  out.push(bas);
  if (u.tablo) out.push(tabloCiz(u.tablo, u.kayitlar));
  u.kayitlar.forEach((r, i) => out.push(kayitCiz(u, r, i)));
  const sayfalar = new Map();
  for (const r of u.kayitlar) for (const p of r.kanit) {
    for (const s of [p.sayfa, p.bitis]) if (Number.isInteger(s) && !sayfalar.has(s)) sayfalar.set(s, []);
    if (sayfalar.has(p.sayfa)) sayfalar.get(p.sayfa).push(...p.araliklar);
  }
  const det = el('details', 'kart'); det.open = u.tur === 'metin';
  det.append(el('summary', '', 'Sayfa metni (kanıt vurgulu)'));
  for (const [s, ar] of sayfalar) det.append(el('div', 'mut', `Sayfa ${s}`), vurgulu(V.sayfalar[String(s)] || '', ar));
  out.push(det);
  sag.replaceChildren(...out);
  const pdfbar = document.getElementById('pdfbar'); pdfbar.replaceChildren(el('span', 'mut', V.makale.pdf));
  for (const s of sayfalar.keys()) { const b = el('button', '', `s.${s}`); b.addEventListener('click', () => { pdfSayfa = null; pdfGit(s); }); pdfbar.append(b); }
  const r = u.kayitlar[sec]; pdfGit(r ? r.sayfa : u.sayfa);
}

function git(d) { if (!sira.length) return; pos = Math.max(0, Math.min(sira.length - 1, pos + d)); sec = 0; ciz(); document.getElementById('sag').scrollTop = 0; }

document.addEventListener('keydown', ev => {
  const t = ev.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) { if (ev.key === 'Escape') t.blur(); return; }
  if (!V || !sira.length || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const u = V.birimler[sira[pos]], key = ev.key.toLowerCase();
  if (key === 'j') git(1); else if (key === 'k') git(-1);
  else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); sec = Math.max(0, Math.min(u.kayitlar.length - 1, sec + (ev.key === 'ArrowDown' ? 1 : -1))); ciz(); }
  else if (['a', 'r', 's'].includes(key)) {
    ev.preventDefault();
    const kk = {a: 'onay', r: 'ret', s: 'sonra'}[key];
    const tum = ev.shiftKey || u.grup;
    if (tum && !(u.grup || (u.tur === 'tablo' && u.kayitlar.length > 1))) return;
    const girdi = document.querySelector(`input[data-gerekce="${tum ? 'birim' : sec}"]`);
    if (kk === 'ret' && !(girdi && girdi.value.trim())) { if (girdi) girdi.focus(); hata('Ret gerekçesi yazıp Enter\'a basın.'); return; }
    gonder(u, tum ? birimKayitlari(u) : [u.kayitlar[sec].hash], kk, girdi);
  }
});
document.getElementById('filtre').addEventListener('change', () => { siraKur(V.birimler[sira[pos]] && V.birimler[sira[pos]].id); sec = 0; ciz(); });
document.getElementById('onceki').addEventListener('click', () => git(-1));
document.getElementById('sonraki').addEventListener('click', () => git(1));

(async () => {
  try {
    const r = await fetch('/api/veri', {headers: {'X-Inceleme-Anahtari': ANAHTAR}});
    const j = await r.json();
    if (!r.ok) { hata(j.hata || 'veri alınamadı'); return; }
    V = j;
    const kd = V.makale.kapsam_disi;
    document.getElementById('baslik').textContent = `${V.makale.pdf} — ${V.makale.aday} aday, ${V.makale.birim} birim · inceleyen: ${V.inceleyen}`;
    document.getElementById('baslik').title = `Kapsam dışı: olası tekrar ${kd.olasi_tekrar}, diğer inceleme ${kd.diger_inceleme}, kesin tekrar ${kd.kesin_tekrar}`;
    let son = null; try { son = localStorage.getItem('rag-inceleme:' + V.makale.pdf); } catch (e) { /* isteğe bağlı */ }
    siraKur(null);
    const acikId = sira.length ? V.birimler[sira[0]].id : null;
    siraKur(son && sira.some(i => V.birimler[i].id === son) ? son : acikId);
    ciz();
  } catch (e) { hata('Veri alınamadı: ' + e); }
})();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

def _ozet(makale):
    units = makale['birimler']
    auto = sum(1 for u in units for g in u['gorunum'] if g['otomatik'])
    kd = makale['kapsam_disi']
    return (f"aday {len(makale['hashes'])} | birim {len(units)} (metin {sum(u['tur'] == 'metin' for u in units)}, "
            f"tablo satırı {sum(u['tur'] == 'tablo' for u in units)}, gruplu satır {sum(u['grup'] for u in units)}) | "
            f"otomatik düzeltmeli kayıt {auto} | kapsam dışı: olası tekrar {kd['olasi_tekrar']}, "
            f"diğer inceleme {kd['diger_inceleme']}, kesin tekrar {kd['kesin_tekrar']}")


def hazirla(args):
    makale = makale_yukle(args.taslak, args.pdf)
    path = Path(args.kararlar) if args.kararlar else varsayilan_kararlar(makale['taslak'])
    print(_ozet(makale))
    if path.exists():
        doc, _ = karar_dosyasi_oku(path, makale)
        print(f'Karar dosyası zaten var, geçerli; üzerine yazılmadı: {path}')
    else:
        doc = bos_karar_dosyasi(makale)
        atomic_write_json(path, doc)
        print(f'Karar dosyası oluşturuldu (bütün kayıtlar kararsız): {path}')
    print('İlerleme:', json.dumps(ilerleme(doc, makale), ensure_ascii=False))
    return path


def incele(args):
    makale = makale_yukle(args.taslak, args.pdf)
    path = Path(args.kararlar) if args.kararlar else varsayilan_kararlar(makale['taslak'])
    if not path.exists():
        raise IncelemeHatasi(f'karar dosyası yok; önce hazirla çalıştırın: {path}')
    server = IncelemeSunucusu(args.port, makale, path, args.inceleyen)
    url = f'http://{HOST}:{server.server_address[1]}/'
    print(_ozet(makale))
    print('İlerleme:', json.dumps(ilerleme(server.doc, makale), ensure_ascii=False))
    print(f'İnceleme adresi: {url}  (yalnız bu bilgisayar; durdurmak için Ctrl+C)')
    if not args.tarayici_acma:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print('Sunucu durdu. Kararlar:', path)


def disari_aktar(args):
    makale = makale_yukle(args.taslak, args.pdf)
    path = Path(args.kararlar) if args.kararlar else varsayilan_kararlar(makale['taslak'])
    doc, sha = karar_dosyasi_oku(path, makale)
    out = inceleme_kopyasi(makale, doc, path, sha)
    parmak_izi_kontrol(makale)
    target = kopyayi_yaz(out, args.cikti_dizini or KARAR_DIZINI, makale['taslak']['pdf'])
    print('İlerleme:', json.dumps(ilerleme(doc, makale), ensure_ascii=False))
    print(f"İnceleme kopyası: {target} (verified {out['rag_inceleme']['verified']}, "
          f"pending kalan {out['rag_inceleme']['pending_kalan']})")
    print(f'Aktarım provası: python -m src.rag_aktarim aktar --pdf "{makale["pdf_yolu"]}" --taslak "{target}"')
    return target


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog='python scripts/rag_inceleme.py', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='komut', required=True)
    for name in ('hazirla', 'incele', 'disari_aktar'):
        p = sub.add_parser(name)
        p.add_argument('--taslak', required=True, help='data/review/rag/<belge>_rag_taslak_<zaman>.json (yalnız okunur)')
        p.add_argument('--pdf', help='varsayılan: data/pdfs/<taslaktaki pdf>')
        p.add_argument('--kararlar', help='varsayılan: data/review/rag_inceleme/<pdf>.kararlar.json')
        if name == 'incele':
            p.add_argument('--inceleyen', required=True, help='kararları veren kişinin kimliği')
            p.add_argument('--port', type=int, default=8765)
            p.add_argument('--tarayici-acma', action='store_true')
        if name == 'disari_aktar':
            p.add_argument('--cikti-dizini', help='varsayılan: data/review/rag_inceleme')
    args = parser.parse_args(argv)
    try:
        return {'hazirla': hazirla, 'incele': incele, 'disari_aktar': disari_aktar}[args.komut](args)
    except IncelemeHatasi as exc:
        print(f'DUR: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
