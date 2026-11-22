"""Pilot PDF clinical evidence preparation and safe SQL import.

No LLM inference. Every proposed clinical fact must have an exact source quote.
Default mode is read-only preview. --write explicitly enables SQL insertion.
"""
import argparse
import hashlib
import json
import re
from itertools import product
from pathlib import Path

TYPES = {'tani', 'semptom', 'teshis_yontemi', 'tani_kriteri', 'tedavi', 'ilac', 'doz', 'hasta_grubu', 'klinik_sonuc', 'olcum', 'diger'}
CONTEXTS = {'study_finding', 'study_method', 'exclusion_criterion', 'literature_background', 'recommendation'}
# import_records'un kayıtta varsa yazdığı yapılandırılmış klinik_bulgular sütunları (ör. src/pilot_tablo aktarımı).
# hasta_sayisi bilerek yok: grup n'leri taslak_notu'da kalır.
STRUCTURED_FIELDS = ('hasta_grubu', 'deger', 'unit', 'p', 'ci95', 'etki_turu', 'comparator', 'taslak_notu',
                     'sonuc_olcutu_tr', 'mudahale_en', 'mudahale_turu', 'outcome_type')
# import_records'un okuduğu temel alanlar; bunlar ve STRUCTURED_FIELDS dışındaki alanlar açık hata verir.
INPUT_FIELDS = {'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_cumle', 'sayfa', 'sayfa_bitis',
                'kanit_turu', 'review_status', 'kanit_parcalari', 'birlestirme'}
# Çok parçalı kanıt (kanit_parcalari): metni araya giren sayfa sonu, tablo sayfası veya şekil/tablo başlığıyla
# bölünmüş tek bir cümle. Parçalar asla birleştirilip aranmaz; her biri kendi fiziksel sayfasında birebir
# doğrulanır. import_records çok sayfalı/çok parçalı kaydı yazmadan önce validate_records ile aynı kurallarla
# denetler (PDF sayfaları gerekir) ve şöyle yazar: sayfa_bitis -> kanit_sayfa_bitis, birlestirme -> birlestirme,
# parçalar -> taslak_notu["kanit_parcalari"]; kanit_cumle ilk parça olarak kalır (birleştirilmiş alıntı yok).
# Klinik bilgi (fact_in_parts) tek bir parçada birebir geçmelidir; tek istisna, birlestirme = 'sayfa' olan
# kayıtta ardışık iki fiziksel sayfa arasında (önceki parça sayfanın son metni, sonraki parça bir sonraki
# sayfanın ilk metni) kelime sınırında bölünmüş ifadedir. Yalnızca o iki parça tek boşlukla yan yana konup aranır ve eşleşme
# sınırı gerçekten geçmelidir. Tireyle bölünmüş kelime, tablo/şekil üzerinden ('ara_blok') bağ ve ardışık
# olmayan parçalar birleştirilmez; kanıt alıntısının kendisi yine birleştirilmez.
CAPTION_RE = re.compile(r'\*\*(?:TABLE|FIGURE)\s*\d+\s*:\*\*', re.IGNORECASE)
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?')
MAX_PARTS = 3
MAX_INLINE_GAP = 1000  # aynı sayfada iki parça arasındaki şekil/tablo bloğunun azami uzunluğu
PART_NUMERIC_FIELDS = ('deger', 'p', 'ci95')


def _structured_key(item):
    """Tekrar anahtarının yapılandırılmış kısmı: aynı kanıtın farklı grup/analiz satırları tekrar sayılmaz."""
    note = item.get('taslak_notu')
    if isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            note = None
    if not isinstance(note, dict):
        note = {}
    # Aynı kanıt cümlesini paylaşan farklı grup/karşılaştırma/analiz/tablo satırları ayrı kayıttır;
    # yalnızca bütün bu alanları aynı olan kayıt tekrar sayılır.
    return (item.get('hasta_grubu'), item.get('comparator'), item.get('deger'), item.get('unit'),
            item.get('etki_turu'), item.get('p'), item.get('ci95'), item.get('sonuc_olcutu_tr'),
            note.get('analiz'), note.get('tablo_no'), note.get('satir_no'))


def evidence_end_page(item):
    """sayfa_bitis yoksa tek sayfalı kanıt; varsa int olmalı."""
    end = item.get('sayfa_bitis', item.get('sayfa'))
    return item.get('sayfa') if end is None else end


def quote_in_pages(quote, pages, page, end_page):
    """Kanıt birebir (normalizasyonsuz) aranır. İki sayfalı kanıt sayfa sınırını gerçekten geçmelidir."""
    if end_page == page:
        return quote in pages[page]
    left = pages[page]
    text = left + pages[end_page]
    boundary = len(left)
    start = text.find(quote)
    while start >= 0:
        if start < boundary < start + len(quote):
            return True
        start = text.find(quote, start + 1)
    return False


def _occurrences(text, page_text):
    start = page_text.find(text)
    while start >= 0:
        yield start
        start = page_text.find(text, start + 1)


def _part_link(pages, prev_page, prev_end, page, start):
    """İki ardışık parçanın okuma sırasında kesintisiz olup olmadığı: 'sayfa' | 'ara_blok' | None."""
    if page == prev_page:
        gap = pages[page][prev_end:start]
        if start >= prev_end and len(gap) <= MAX_INLINE_GAP and CAPTION_RE.match(gap.strip()):
            return 'ara_blok'  # aynı sayfada araya şekil/tablo başlığı girmiş
        return None
    # Sayfa geçişi: önceki parça sayfanın son metni, sonraki parça sayfanın ilk metni olmalı.
    if pages[prev_page][prev_end:].strip() or pages[page][:start].strip():
        return None
    if page == prev_page + 1:
        return 'sayfa'
    skipped = prev_page + 1
    if page == prev_page + 2 and skipped in pages and CAPTION_RE.match(pages[skipped].lstrip()):
        return 'ara_blok'  # araya yalnızca tablo/şekil sayfası girmiş
    return None


def check_evidence_parts(parts, pages):
    """kanit_parcalari doğrulaması. Döndürür: birlestirme ('sayfa' | 'ara_blok')."""
    links = _evidence_links(parts, pages)
    return 'ara_blok' if 'ara_blok' in links else 'sayfa'


def fact_in_parts(fact, parts, pages):
    """Klinik bilgi kanıt parçalarında birebir var mı (normalizasyon yok).

    Önce her parça tek başına aranır. Bulunmazsa ve kaydın bütün bağları 'sayfa' ise (birlestirme = 'sayfa';
    tek bir 'ara_blok' bağı bile varsa sınır araması yapılmaz) ardışık iki parça (önceki parça
    sayfa N'nin son metni, sonraki parça sayfa N+1'in ilk metni) tek boşlukla yan yana konur ve bilgi bu
    sınırı gerçekten geçerek (iki parçadan da en az bir karakter alarak) geçmelidir. Tireyle biten parça
    (bölünmüş kelime) birleştirilmez. Parçalar doğrulanamıyorsa yalnızca tek parça
    araması yapılır."""
    if not isinstance(fact, str) or not fact.strip():
        return False
    if any(fact in part['metin'] for part in parts):
        return True
    try:
        links = _evidence_links(parts, pages)
    except ValueError:
        return False
    if 'ara_blok' in links:
        return False  # yalnızca birlestirme = 'sayfa' olan kayıtta sınır araması yapılır
    for prev, cur in zip(parts, parts[1:]):
        if cur['sayfa'] != prev['sayfa'] + 1:
            continue
        left, right = prev['metin'].rstrip(), cur['metin'].lstrip()
        if left.endswith('-'):
            continue  # bölünmüş kelimenin doğru birleşimi belirsiz ('C-pep-' + 'tide')
        joined = left + ' ' + right
        for start in _occurrences(fact, joined):
            if start < len(left) and start + len(fact) > len(left) + 1:
                return True
    return False


def _evidence_links(parts, pages):
    """kanit_parcalari doğrulaması. Döndürür: ardışık parça çiftlerinin bağları ('sayfa' | 'ara_blok')."""
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARTS:
        raise ValueError(f'kanit_parcalari 2-{MAX_PARTS} parçalı liste olmalı')
    for part in parts:
        if not isinstance(part, dict) or set(part) != {'sayfa', 'metin'}:
            raise ValueError('Her kanıt parçası yalnızca sayfa ve metin içermeli')
        page, text = part['sayfa'], part['metin']
        if not isinstance(page, int) or isinstance(page, bool) or page not in pages:
            raise ValueError('Kanıt parçasında geçersiz fiziksel sayfa')
        if not isinstance(text, str) or not text.strip() or text not in pages[page]:
            raise ValueError(f'Kanıt parçası s.{page} metninde birebir bulunamadı')
    candidates = [list(_occurrences(part['metin'], pages[part['sayfa']])) for part in parts]
    for starts in product(*candidates):
        links = []
        for prev, cur, prev_start, start in zip(parts, parts[1:], starts, starts[1:]):
            link = _part_link(pages, prev['sayfa'], prev_start + len(prev['metin']), cur['sayfa'], start)
            if link is None:
                break
            links.append(link)
        else:
            return links
    raise ValueError('Kanıt parçaları okuma sırasında kesintisiz değil (sayfa sonu/başı, tablo sayfası '
                     'veya şekil/tablo başlığı dışında metin atlanıyor)')


def validate_records(records, pages):
    """Return (validated, errors). Pages map physical page number -> extracted text."""
    accepted, errors, seen = [], [], set()
    for index, item in enumerate(records, 1):
        try:
            if not isinstance(item, dict):
                raise ValueError('Kayıt nesne olmalı')
            kind = item.get('bilgi_turu')
            fact = item.get('klinik_bilgi_en') or item.get('klinik_bilgi')
            quote = item.get('kanit_cumle')
            page = item.get('sayfa')
            if kind not in TYPES:
                raise ValueError('Geçersiz bilgi_turu')
            context = item.get('baglam')
            if context is not None and context not in CONTEXTS:
                raise ValueError('Geçersiz baglam')
            if not isinstance(page, int) or isinstance(page, bool) or page not in pages:
                raise ValueError('Geçersiz fiziksel sayfa numarası')
            end_page = evidence_end_page(item)
            parts = item.get('kanit_parcalari')
            if parts is None:
                if item.get('birlestirme') is not None:
                    raise ValueError('birlestirme yalnızca kanit_parcalari ile kullanılabilir')
                if (not isinstance(end_page, int) or isinstance(end_page, bool) or end_page not in pages
                        or end_page not in (page, page + 1)):
                    raise ValueError('Geçersiz kanıt bitiş sayfası (sayfa_bitis yalnızca sayfa veya sayfa+1 olabilir)')
                if not isinstance(quote, str) or not quote.strip() or not quote_in_pages(quote, pages, page, end_page):
                    raise ValueError('Kanıt cümlesi belirtilen PDF sayfasında/sayfa sınırında birebir bulunamadı')
                if not isinstance(fact, str) or not fact.strip() or fact not in quote:
                    raise ValueError('Klinik bilgi kanıt cümlesinde birebir bulunamadı; çıkarımsal/çeviri bilgi doğrulanmadı')
                part_key = ()
            else:
                joined = check_evidence_parts(parts, pages)
                if item.get('kanit_turu', 'metin') != 'metin':
                    raise ValueError('kanit_parcalari yalnızca metin kanıtı için kullanılabilir')
                # kanit_cumle tek sayfalı birebir alıntıdır (ilk parça); parçalar tek alıntıymış gibi sunulmaz.
                if quote != parts[0]['metin']:
                    raise ValueError('kanit_cumle ilk kanıt parçasıyla birebir aynı olmalı')
                if page != parts[0]['sayfa'] or end_page != parts[-1]['sayfa']:
                    raise ValueError('sayfa/sayfa_bitis ilk ve son kanıt parçasının sayfalarıyla aynı olmalı')
                if item.get('birlestirme') != joined:
                    raise ValueError(f'birlestirme kanıt parçalarına göre {joined!r} olmalı')
                if not fact_in_parts(fact, parts, pages):
                    raise ValueError('Klinik bilgi tek bir kanıt parçasında ya da ardışık iki sayfanın kelime '
                                     'sınırında birebir bulunamadı; başka parçalar birleştirilmez')
                part_numbers = {n for part in parts for n in NUMBER_RE.findall(part['metin'])}
                for field in PART_NUMERIC_FIELDS:
                    value = item.get(field)
                    missing = [n for n in NUMBER_RE.findall(str(value))] if value is not None else []
                    missing = [n for n in missing if n not in part_numbers]
                    if missing:
                        raise ValueError(f'{field} içindeki sayı kanıt parçalarında yok: {", ".join(missing)}')
                part_key = tuple((part['sayfa'], part['metin']) for part in parts)
            key = (page, end_page, kind, context, fact.strip().casefold()) + _structured_key(item) + (part_key,)
            if key in seen:
                raise ValueError('tekrar: aynı sayfa/tür/bağlam/metin ve grup/değer/analiz daha önce geldi')
            seen.add(key)
            accepted.append(item)
        except ValueError as exc:
            errors.append({'satir': index, 'hata': str(exc)})
    return accepted, errors


def pdf_pages(pdf_path):
    from src.pdf_loader import load_pdf
    doc = load_pdf(pdf_path)
    return doc, {p['page_number']: p['text'] for p in doc['pages']}


def is_multi_evidence(item):
    """Çok parçalı, çok sayfalı (sayfa_bitis != sayfa) ya da birlestirme dolu kayıt."""
    return (item.get('kanit_parcalari') is not None or item.get('birlestirme') is not None
            or evidence_end_page(item) != item.get('sayfa'))


def _note_with_parts(item):
    """Mevcut taslak_notu (dict ya da JSON metni) + kanit_parcalari; diğer anahtarlar korunur."""
    note = item.get('taslak_notu')
    if note is None:
        note = {}
    elif isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            raise ValueError('taslak_notu JSON değil; kanit_parcalari ile birleştirilemez') from None
    if not isinstance(note, dict):
        raise ValueError('taslak_notu JSON nesnesi olmalı; kanit_parcalari ile birleştirilemez')
    parts = item['kanit_parcalari']
    if 'kanit_parcalari' in note and note['kanit_parcalari'] != parts:
        raise ValueError('taslak_notu["kanit_parcalari"] kayıttaki kanit_parcalari ile farklı')
    return json.dumps(dict(note, kanit_parcalari=parts), ensure_ascii=False)


def _check_multi_evidence(records, doc, pages):
    """Çok sayfalı/çok parçalı kayıtları yazımdan önce validate_records ile aynı kurallarla denetler."""
    multi = [(index, item) for index, item in enumerate(records, 1) if is_multi_evidence(item)]
    if not multi:
        return
    if pages is None:
        pages = {p['page_number']: p['text'] for p in doc.get('pages') or []} or None
    if not pages:
        raise ValueError('Çok sayfalı kanıt / çok parçalı kanıt için PDF sayfaları gerekli (pdf_pages() doc\'u ya da '
                         'pages=); sayfasız doğrulanamaz, aktarım durduruldu')
    for index, item in multi:
        _, errors = validate_records([item], pages)
        if errors:
            raise ValueError(f'Kayıt {index}: çok sayfalı/çok parçalı kanıt doğrulanamadı: {errors[0]["hata"]}')
        if item.get('kanit_parcalari') is not None:
            _note_with_parts(item)  # taslak_notu birleştirilemiyorsa bağlantıdan önce dur


def import_records(doc, records, connection=None, pages=None):
    """Only called with explicit --write. Never updates existing rows.

    connection verilirse onu kullanır ve commit/rollback/close yapmaz (çağıran yönetir).
    pages: {fiziksel sayfa: metin}; verilmezse doc['pages'] (pdf_pages() çıktısı). Yalnız çok
    sayfalı/çok parçalı kayıtlar için gerekir; tek sayfalı kayıtların yolu değişmedi."""
    if not records or any(item.get('review_status') != 'verified' or item.get('baglam') not in CONTEXTS for item in records):
        raise ValueError('SQL aktarımı yalnızca bağlamı geçerli, manuel onaylı kayıtlarla yapılabilir')
    if any(item.get('bilgi_turu') not in TYPES for item in records):
        raise ValueError('SQL aktarımı için her kayıtta geçerli bilgi_turu zorunlu')
    if any(not isinstance(item.get('sayfa'), int) or isinstance(item.get('sayfa'), bool) for item in records):
        raise ValueError('SQL aktarımı için her kayıtta geçerli sayfa zorunlu')
    unknown = sorted({key for item in records for key in item} - INPUT_FIELDS - set(STRUCTURED_FIELDS))
    if unknown:
        raise ValueError(f'import_records bu alanları desteklemiyor (sessizce atlanmaz): {", ".join(unknown)}')
    _check_multi_evidence(records, doc, pages)
    own = connection is None
    if own:
        from src.database import get_connection
        conn = get_connection()
    else:
        conn = connection
    try:
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT filename FROM pdf_belgeleri WHERE document_id=%s', (doc['document_id'],))
            match = cursor.fetchone()
            if not match or match[0] != doc['filename']:
                raise ValueError('PDF önce pdf_belgeleri tablosuna mevcut ingestion akışıyla kaydedilmeli')
            cursor.execute('SHOW COLUMNS FROM klinik_bulgular')
            columns = {row[0] for row in cursor.fetchall()}
            required = {'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'document_id', 'kanit_sayfa', 'kanit_cumle', 'kanit_turu', 'satir_hash', 'review_status'}
            if not required <= columns:
                raise ValueError('SQL klinik_bulgular sütunları eksik')
            used = {key for item in records for key in STRUCTURED_FIELDS if item.get(key) is not None}
            if any(is_multi_evidence(item) for item in records):
                used |= {'kanit_sayfa_bitis', 'birlestirme', 'taslak_notu'}
            if not used <= columns:
                raise ValueError(f'SQL klinik_bulgular sütunları eksik: {", ".join(sorted(used - columns))}')
            written = 0
            for item in records:
                payload = {
                    'document_id': doc['document_id'],
                    'bilgi_turu': item['bilgi_turu'],
                    'baglam': item['baglam'],
                    'klinik_bilgi': item.get('klinik_bilgi'),
                    'klinik_bilgi_en': item.get('klinik_bilgi_en'),
                    'kanit_sayfa': item['sayfa'],
                    'kanit_cumle': item['kanit_cumle'],
                    'kanit_turu': item.get('kanit_turu', 'metin'),
                    'review_status': 'verified',
                    'cikarma_yontemi': 'manuel',
                }
                for key in STRUCTURED_FIELDS:
                    value = item.get(key)
                    if value is not None:
                        payload[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                multi = None
                if is_multi_evidence(item):
                    # Parçalar taslak_notu'da; kanit_cumle ilk parça olarak kalır (_check_multi_evidence denetledi).
                    if item.get('kanit_parcalari') is not None:
                        payload['taslak_notu'] = _note_with_parts(item)
                    payload['kanit_sayfa_bitis'] = evidence_end_page(item)
                    if item.get('birlestirme') is not None:
                        payload['birlestirme'] = item['birlestirme']
                    multi = {'kanit_sayfa_bitis': payload['kanit_sayfa_bitis'], 'birlestirme': item.get('birlestirme'),
                             'kanit_parcalari': item.get('kanit_parcalari')}
                identity = [payload[k] for k in ('document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_sayfa', 'kanit_cumle')]
                # Yapılandırılmış alanlar kimliğe girer (aynı kanıtlı grup satırları ayrı kalır);
                # yapılandırılmış alanı olmayan kayıtların hash'i öncekiyle aynıdır.
                structured = {k: payload[k] for k in STRUCTURED_FIELDS if k in payload}
                if structured:
                    identity.append(structured)
                # Çok sayfalı/çok parçalı kayıtta bitiş sayfası ve parçalar kimliğe girer;
                # tek sayfalı, parçasız kayıtların kimliği (ve hash'i) değişmez.
                if multi is not None:
                    identity.append(multi)
                payload['satir_hash'] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                fields = list(payload)
                cursor.execute(
                    f"INSERT IGNORE INTO klinik_bulgular ({', '.join('`'+k+'`' for k in fields)}) VALUES ({', '.join(['%s']*len(fields))})",
                    tuple(payload[k] for k in fields),
                )
                written += cursor.rowcount
            if own:
                conn.commit()
            return written
        finally:
            cursor.close()
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description='Pilot PDF kanıt kontrolü ve isteğe bağlı SQL aktarımı')
    parser.add_argument('--pdf', required=True)
    parser.add_argument('--findings', help='Klinik kayıtları içeren JSON dosyası')
    parser.add_argument('--write', action='store_true', help='Doğrulanmış taslak kayıtları SQL\'e yaz')
    args = parser.parse_args()
    if args.write and not args.findings:
        parser.error('--write için --findings gerekli')
    doc, pages = pdf_pages(args.pdf)
    print(f"PDF: {doc['filename']} | Sayfa: {doc['page_count']} | SHA256: {doc['document_id']}")
    if not args.findings:
        print('PDF okundu. Klinik kayıt JSON dosyası verildiğinde kanıt kontrolü yapılabilir.')
        return
    records = json.loads(Path(args.findings).read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('JSON kökünde kayıt listesi olmalı')
    accepted, errors = validate_records(records, pages)
    print(f'Geçerli: {len(accepted)} | Hatalı: {len(errors)}')
    for error in errors:
        print(f"Satır {error['satir']}: {error['hata']}")
    if args.write:
        if len(accepted) != len(records):
            raise ValueError('Tekrarlı veya elenen kayıtlar var; SQL aktarımı durduruldu')
        if not accepted:
            raise ValueError('SQL aktarımı için onaylanmış kayıt bulunamadı')
        if any(item.get('baglam') not in CONTEXTS for item in accepted):
            raise ValueError('SQL aktarımı için her kayıtta geçerli baglam zorunlu')
        if any(item.get('review_status') != 'verified' for item in accepted):
            raise ValueError('SQL aktarımı için tüm kayıtlar manuel inceleme sonrası review_status=verified olmalı')
        if errors:
            raise ValueError('Hatalı kayıtlar varken SQL yazımı yapılmaz')
        print(f'SQL\'e eklenen yeni kayıt: {import_records(doc, accepted)}')
    else:
        print('Önizleme tamamlandı; SQL değişmedi.')


if __name__ == '__main__':
    main()
