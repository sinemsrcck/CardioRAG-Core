"""Pilot PDF clinical evidence preparation and safe SQL import.

No LLM inference. Every proposed clinical fact must have an exact source quote.
Default mode is read-only preview. --write explicitly enables SQL insertion.
"""
import argparse
import hashlib
import json
from pathlib import Path

TYPES = {'tani', 'semptom', 'teshis_yontemi', 'tani_kriteri', 'tedavi', 'ilac', 'doz', 'hasta_grubu', 'klinik_sonuc', 'olcum', 'diger'}
CONTEXTS = {'study_finding', 'study_method', 'exclusion_criterion', 'literature_background', 'recommendation'}


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
            if not isinstance(quote, str) or not quote.strip() or quote not in pages[page]:
                raise ValueError('Kanıt cümlesi bu PDF sayfasında birebir bulunamadı')
            if not isinstance(fact, str) or not fact.strip() or fact not in quote:
                raise ValueError('Klinik bilgi kanıt cümlesinde birebir bulunamadı; çıkarımsal/çeviri bilgi doğrulanmadı')
            key = (page, kind, context, fact.strip().casefold())
            if key in seen:
                continue
            seen.add(key)
            accepted.append(item)
        except ValueError as exc:
            errors.append({'satir': index, 'hata': str(exc)})
    return accepted, errors


def pdf_pages(pdf_path):
    from src.pdf_loader import load_pdf
    doc = load_pdf(pdf_path)
    return doc, {p['page_number']: p['text'] for p in doc['pages']}


def import_records(doc, records):
    """Only called with explicit --write. Never updates existing rows."""
    if not records or any(item.get('review_status') != 'verified' or item.get('baglam') not in CONTEXTS for item in records):
        raise ValueError('SQL aktarımı yalnızca bağlamı geçerli, manuel onaylı kayıtlarla yapılabilir')
    from src.database import get_connection
    conn = get_connection()
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
                identity = [payload[k] for k in ('document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_sayfa', 'kanit_cumle')]
                payload['satir_hash'] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                fields = list(payload)
                cursor.execute(
                    f"INSERT IGNORE INTO klinik_bulgular ({', '.join('`'+k+'`' for k in fields)}) VALUES ({', '.join(['%s']*len(fields))})",
                    tuple(payload[k] for k in fields),
                )
                written += cursor.rowcount
            conn.commit()
            return written
        finally:
            cursor.close()
    except Exception:
        conn.rollback()
        raise
    finally:
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
