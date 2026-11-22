"""
Doğrulanmış klinik kayıt JSON'larını veritabanına aktarma komutu (pilot akışı; klinik_bulgular, bilgi_turu dolu satırlar).

Kullanım:
    python -m src.aktarim --makale <pdf> <json> [<json> ...] [--makale <pdf> <json> ...]   # prova (varsayılan)
    python -m src.aktarim --makale ... --yaz --yedek backups/yedek.sql [--yeniden] [--kalite] [--kur]

- Varsayılan mod PROVA: her şey tek transaction'da yapılır ve sonunda ROLLBACK; bağlantı korumaya alınır
  (commit ve örtük commit yapan ifadeler hata verir). Sonda ayrı bağlantıdan tablo sayıları başlangıçla karşılaştırılır.
- --yaz: kalıcı yazma (COMMIT). --yedek <dosya> zorunlu: var, boş değil ve son 24 saatte oluşturulmuş olmalı.
- --yeniden: makalenin veritabanındaki mevcut pilot satırları (aynı document_id, bilgi_turu IS NOT NULL) ve bağlı
  kalite işaretleri aynı transaction'da silinip yeniden aktarılır. Diğer makalelere ve RAG satırlarına dokunulmaz
  (öncesi / sonrası parmak iziyle doğrulanır).
- --kalite (yalnız --yaz): COMMIT sonrası python -m src.data_quality --pilot kalıcı çalışır.
- --kur (yalnız --yaz): COMMIT sonrası python -m src.sunum_hoca --kur.

Kontroller (biri tutmazsa ROLLBACK, açık hata; kontrol numarası mesajda):
 1. PDF pdf_belgeleri'nde kayıtlı (document_id ve dosya adı).
 2. JSON o makalenin PDF'iyle eşleşiyor (kanıtlar o PDF'te birebir; JSON kökü document_id beyan ediyorsa aynı).
 3. Bütün kayıtlar review_status = 'verified'.
 4. validate_records her dosyada ve makalenin dosyalarının birleşiminde hatasız. İstisna: "tekrar" verilen kayıt,
    çarptığı önceki kayıttan mudahale_en (test adı) bakımından farklıysa gerçek tekrar sayılmaz; raporda ayrıca
    listelenir (validate_records değişmedi; aynı anahtar burada yeniden kurulur).
 5. Dosyalar arası: aynı kanıt + aynı bilgi ama farklı bilgi_turu -> dur ve listele.
 6. Makalenin zaten pilot satırları varsa dur (--yeniden ile sil + yeniden aktar).
 7. Aktarım sonrası: yazılan = beklenen, atlanan 0, makalede çift satir_hash yok, pilot satırların hepsi verified,
    diğer makaleler / RAG satırları ve onların kalite işaretleri değişmedi.
 8. Aynı bağlantıda data_quality --pilot --dry-run: makale makale hata / uyarı / bilgi ve tablo eksiksizliği;
    herhangi bir makalede hata varsa dur.

--yaz ile COMMIT sonrası her makale için data/review/aktarim/<belge>_satirlari_<zaman>.json yazılır (var olan dosya
silinmez): aktarım zamanı, PDF, document_id, girdi dosyaları, sayılar ve her satır için id, satir_hash, kaynak dosya,
bilgi_turu, baglam, kanit_turu, kanit_sayfa.

Sinem'in modülü (src.pilot_clinical) değiştirilmez; import_records, validate_records ve pdf_pages oradan kullanılır.
data_quality kaynak PDF'leri data/pdfs/ altında arar (pdf_belgeleri.filename); PDF başka yerdeyse kalite raporu
"pilot_pdf_okunamadi" uyarısı verir.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from src.pilot_clinical import _structured_key, evidence_end_page, import_records, pdf_pages, validate_records

AKTARIM_DIR = Path('data/review/aktarim')
YEDEK_EN_FAZLA_YAS = timedelta(hours=24)
TEKRAR_ONEKI = 'tekrar:'
# validate_records'un kanıt / sayfa hataları (kontrol 2: JSON bu PDF'le eşleşmiyor).
KANIT_HATALARI = ('Kanıt cümlesi', 'Geçersiz fiziksel sayfa', 'Geçersiz kanıt bitiş sayfası', 'kanıt parça',
                  'Kanıt parça')
# MySQL'de örtük commit yapan ifadeler (prova korumasında engellenir).
ORTUK_COMMIT = re.compile(
    r"^(?:CREATE|DROP|ALTER|TRUNCATE|RENAME|GRANT|REVOKE|BEGIN|START|COMMIT|LOCK|UNLOCK|ANALYZE|OPTIMIZE|"
    r"REPAIR|CHECK|LOAD|FLUSH|RESET|CACHE|INSTALL|UNINSTALL|XA|IMPORT)\b"
    r"|^SET\s+(?:(?:SESSION|GLOBAL|LOCAL|PERSIST)\s+|@@(?:SESSION\.|GLOBAL\.|LOCAL\.)?)?AUTOCOMMIT\b",
    re.IGNORECASE)
BASTAKI_YORUM = re.compile(r"^(?:\s+|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)*", re.DOTALL)


class AktarimHatasi(RuntimeError):
    """Bir kontrol tutmadı. kontrol: numara ya da ad; ayrinti: listelenecek kayıtlar."""

    def __init__(self, kontrol, mesaj, ayrinti=None):
        super().__init__(f'[kontrol {kontrol}] {mesaj}')
        self.kontrol = kontrol
        self.ayrinti = ayrinti or []


# ---------------------------------------------------------------------------
# Girdi
# ---------------------------------------------------------------------------

def kayitlari_oku(path):
    """(kayıtlar, beyan edilen document_id'ler). Kök: kayıt listesi ya da {"document_id"?, "kayitlar": [...]}."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AktarimHatasi('girdi', f'{path}: JSON okunamadı ({exc})') from None
    declared = set()
    if isinstance(data, dict):
        if data.get('document_id'):
            declared.add(data['document_id'])
        data = data.get('kayitlar')
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) for r in data):
        raise AktarimHatasi('girdi', f'{path}: kökte boş olmayan kayıt listesi (ya da "kayitlar") olmalı')
    declared |= {r['document_id'] for r in data if r.get('document_id')}
    return data, declared


def makaleleri_hazirla(makale_args):
    """--makale PDF JSON [JSON ...] listesi -> [{pdf, dosyalar}]; dosyalar var olmalı, aynı PDF iki kez verilemez."""
    makaleler, seen = [], set()
    for item in makale_args:
        if len(item) < 2:
            raise AktarimHatasi('girdi', f'--makale için PDF ve en az bir JSON gerekli: {item}')
        pdf, files = Path(item[0]), [Path(f) for f in item[1:]]
        for path in (pdf, *files):
            if not path.is_file():
                raise AktarimHatasi('girdi', f'dosya yok: {path}')
        if pdf.resolve() in seen:
            raise AktarimHatasi('girdi', f'aynı PDF iki kez verildi: {pdf}')
        if len({f.resolve() for f in files}) != len(files):
            raise AktarimHatasi('girdi', f'aynı JSON aynı makalede iki kez verildi: {pdf}')
        seen.add(pdf.resolve())
        makaleler.append({'pdf': pdf, 'dosyalar': files})
    if not makaleler:
        raise AktarimHatasi('girdi', 'en az bir --makale gerekli')
    return makaleler


def _olusturma_zamani(stat):
    """Dosyanın oluşturulma zamanı: st_birthtime varsa o; Windows'ta st_ctime; diğerlerinde st_mtime."""
    if getattr(stat, 'st_birthtime', None):
        return stat.st_birthtime
    return stat.st_ctime if os.name == 'nt' else stat.st_mtime


def yedek_kontrolu(path, simdi=None):
    """--yaz öncesi yedek: dosya var, boş değil, son 24 saatte oluşturulmuş. Bilgi sözlüğü ya da AktarimHatasi."""
    if not path:
        raise AktarimHatasi('yedek', '--yaz için --yedek <dosya> zorunlu (önce mysqldump ile yedek alın)')
    path = Path(path)
    if not path.is_file():
        raise AktarimHatasi('yedek', f'yedek dosyası yok: {path}')
    stat = path.stat()
    if stat.st_size == 0:
        raise AktarimHatasi('yedek', f'yedek dosyası boş: {path}')
    created = datetime.fromtimestamp(_olusturma_zamani(stat))
    simdi = simdi or datetime.now()
    if simdi - created > YEDEK_EN_FAZLA_YAS:
        raise AktarimHatasi('yedek', f'yedek dosyası 24 saatten eski ({created:%Y-%m-%d %H:%M}): {path}; yeni yedek alın')
    return {'dosya': str(path), 'boyut': stat.st_size, 'olusturma': created.isoformat(timespec='seconds')}


# ---------------------------------------------------------------------------
# Dosya kontrolleri (veritabanı yok)
# ---------------------------------------------------------------------------

def tekrar_anahtari(item):
    """validate_records'un tekrar anahtarı (aynı kurulum; validate_records değişmedi)."""
    parts = item.get('kanit_parcalari')
    part_key = () if parts is None else tuple((p.get('sayfa'), p.get('metin')) for p in parts)
    fact = (item.get('klinik_bilgi_en') or item.get('klinik_bilgi') or '').strip().casefold()
    return ((item.get('sayfa'), evidence_end_page(item), item.get('bilgi_turu'), item.get('baglam'), fact)
            + _structured_key(item) + (part_key,))


def _mudahale(item):
    return (item.get('mudahale_en') or '').strip().casefold()


def dogrula(records, pages, etiketler):
    """validate_records + tekrar istisnası. etiketler: her kayıt için (dosya, sıra) gösterimi.
    {'kanit': [...], 'diger': [...], 'gercek_tekrar': [...], 'mudahale_farkli_tekrar': [...]}"""
    _, errors = validate_records(records, pages)
    keys = []
    for item in records:
        try:
            keys.append(tekrar_anahtari(item))
        except Exception:  # noqa: BLE001 - geçersiz kayıt zaten validate hatası verir
            keys.append(None)
    out = {'kanit': [], 'diger': [], 'gercek_tekrar': [], 'mudahale_farkli_tekrar': []}
    for error in errors:
        index = error['satir'] - 1
        label = etiketler[index]
        message = error['hata']
        if message.startswith(TEKRAR_ONEKI):
            earlier = next((j for j in range(index) if keys[j] is not None and keys[j] == keys[index]), None)
            if earlier is not None and _mudahale(records[earlier]) != _mudahale(records[index]):
                out['mudahale_farkli_tekrar'].append({
                    'kayit': label, 'onceki': etiketler[earlier],
                    'klinik_bilgi_en': records[index].get('klinik_bilgi_en'),
                    'mudahale_en': [records[earlier].get('mudahale_en'), records[index].get('mudahale_en')],
                    'deger': records[index].get('deger') or records[index].get('p')})
            else:
                out['gercek_tekrar'].append({'kayit': label, 'onceki': etiketler[earlier] if earlier is not None else None,
                                             'klinik_bilgi_en': records[index].get('klinik_bilgi_en')})
        elif message.startswith(KANIT_HATALARI):
            out['kanit'].append({'kayit': label, 'hata': message})
        else:
            out['diger'].append({'kayit': label, 'hata': message})
    return out


def tur_cakismalari(files):
    """Dosyalar arası: aynı kanıt (sayfa + kanit_cumle) ve aynı bilgi (klinik_bilgi_en) ama farklı bilgi_turu.
    files: [(etiket, kayıtlar)]."""
    seen = defaultdict(list)
    for name, records in files:
        for index, item in enumerate(records, 1):
            fact = (item.get('klinik_bilgi_en') or item.get('klinik_bilgi') or '').strip().casefold()
            seen[(item.get('sayfa'), item.get('kanit_cumle'), fact)].append((name, index, item.get('bilgi_turu')))
    conflicts = []
    for (page, _, fact), entries in seen.items():
        for a in range(len(entries)):
            for b in range(a + 1, len(entries)):
                (fa, ia, ta), (fb, ib, tb) = entries[a], entries[b]
                if fa != fb and ta != tb:
                    conflicts.append({'sayfa': page, 'klinik_bilgi_en': fact[:120],
                                      'kayitlar': [f'{fa}#{ia} ({ta})', f'{fb}#{ib} ({tb})']})
    return conflicts


def makaleyi_kontrol_et(makale):
    """PDF'i okur, dosyaları yükler; kontrol 2–5 (veritabanı yok). makale sözlüğüne doc, pages, files ekler."""
    doc, pages = pdf_pages(makale['pdf'])
    files = []
    for path in makale['dosyalar']:
        records, declared = kayitlari_oku(path)
        if declared and declared != {doc['document_id']}:
            raise AktarimHatasi(2, f'{path} başka belgeye ait: beyan edilen document_id {sorted(declared)}, '
                                   f'PDF ({doc["filename"]}) {doc["document_id"]}')
        files.append((path, records))
    pdf_name = doc['filename']
    # 3. verified
    not_verified = [f'{p.name}#{i}' for p, records in files for i, r in enumerate(records, 1)
                    if r.get('review_status') != 'verified']
    if not_verified:
        raise AktarimHatasi(3, f'{pdf_name}: review_status = verified olmayan {len(not_verified)} kayıt', not_verified)
    # 2 + 4: her dosya ayrı, sonra birleşim
    report = {'dosya': {}, 'mudahale_farkli_tekrar': []}
    for path, records in files:
        result = dogrula(records, pages, [f'{path.name}#{i}' for i in range(1, len(records) + 1)])
        if result['kanit']:
            raise AktarimHatasi(2, f'{path} {pdf_name} ile eşleşmiyor: {len(result["kanit"])} kaydın kanıtı bu PDF\'te '
                                   f'birebir yok', result['kanit'])
        if result['diger']:
            raise AktarimHatasi(4, f'{path}: validate_records hatası ({len(result["diger"])})', result['diger'])
        if result['gercek_tekrar']:
            raise AktarimHatasi(4, f'{path}: gerçek tekrar ({len(result["gercek_tekrar"])})', result['gercek_tekrar'])
        report['dosya'][str(path)] = {'kayit': len(records), 'mudahale_farkli_tekrar': len(result['mudahale_farkli_tekrar'])}
    union = [r for _, records in files for r in records]
    labels = [f'{p.name}#{i}' for p, records in files for i in range(1, len(records) + 1)]
    result = dogrula(union, pages, labels)
    for kind, kontrol in (('kanit', 2), ('diger', 4), ('gercek_tekrar', 4)):
        if result[kind]:
            raise AktarimHatasi(kontrol, f'{pdf_name}: dosyaların birleşiminde {kind} hatası ({len(result[kind])})',
                                result[kind])
    report['mudahale_farkli_tekrar'] = result['mudahale_farkli_tekrar']
    # 5. dosyalar arası tür çakışması
    conflicts = tur_cakismalari([(p.name, records) for p, records in files])
    if conflicts:
        raise AktarimHatasi(5, f'{pdf_name}: dosyalar arası aynı kanıt + aynı bilgi, farklı bilgi_turu '
                               f'({len(conflicts)})', conflicts)
    makale.update(doc=doc, pages=pages, files=files, beklenen=len(union), kontrol=report)
    return makale


# ---------------------------------------------------------------------------
# Veritabanı
# ---------------------------------------------------------------------------

def _scalar(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor.fetchone()[0]


def tablo_sayilari(cursor):
    return {t: int(_scalar(cursor, f'SELECT COUNT(*) FROM `{t}`')) for t in ('klinik_bulgular', 'kalite_isaretleri')}


def diger_satirlar_izi(cursor, document_ids):
    """Aktarılan makalelerin pilot satırları DIŞINDAKİ satırların ve kalite işaretlerinin parmak izi."""
    ids = sorted(document_ids)
    placeholder = ', '.join(['%s'] * len(ids))
    outside = f'NOT (b.document_id IN ({placeholder}) AND b.bilgi_turu IS NOT NULL)'
    cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(CRC32(CONCAT_WS('|', b.id, b.satir_hash, b.review_status))), 0) "
                   f"FROM klinik_bulgular AS b WHERE b.document_id IS NULL OR {outside}", ids)
    rows, rows_fp = cursor.fetchone()
    cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(CRC32(CONCAT_WS('|', q.id, q.klinik_bulgu_id, q.kontrol_kodu))), 0) "
                   f"FROM kalite_isaretleri AS q JOIN klinik_bulgular AS b ON b.id = q.klinik_bulgu_id "
                   f"WHERE b.document_id IS NULL OR {outside}", ids)
    flags, flags_fp = cursor.fetchone()
    return {'satir': int(rows), 'satir_izi': int(rows_fp), 'kalite_isareti': int(flags), 'kalite_izi': int(flags_fp)}


def _kalite_kontrolu(conn, makaleler, out):
    """Kontrol 8: aynı bağlantıda data_quality --pilot --dry-run (rapor geçici dizine)."""
    from src.data_quality import parse_args, run_quality

    loaded = {m['doc']['filename']: (m['doc'], m['pages']) for m in makaleler}

    def page_loader(path):
        return loaded.get(Path(path).name) or pdf_pages(path)

    with tempfile.TemporaryDirectory() as tmp:
        opts = parse_args(['--pilot', '--dry-run', '--rapor-dir', str(Path(tmp) / 'kalite')])
        summary, flags, rows = run_quality(opts, connection=conn, out=lambda *_: None, page_loader=page_loader)
    pilot = {r['satir_hash']: r for r in rows if r.get('bilgi_turu')}
    per_doc = defaultdict(Counter)
    for flag in flags:
        row = pilot.get(flag['satir_hash'])
        if row is not None:
            per_doc[row.get('document_id')][flag['seviye']] += 1
    rows_per_doc = Counter(r.get('document_id') for r in pilot.values())
    completeness = {}
    for item in (summary.get('pilot') or {}).get('eksiksizlik', []):
        completeness[item.get('document_id')] = item
    result = {}
    for doc_id in sorted(rows_per_doc):
        item = completeness.get(doc_id) or {}
        result[doc_id] = {
            'dosya': item.get('dosya'), 'pilot_satir': rows_per_doc[doc_id],
            'hata': per_doc[doc_id].get('hata', 0), 'uyari': per_doc[doc_id].get('uyari', 0),
            'bilgi': per_doc[doc_id].get('bilgi', 0),
            'tablo': (f"{item.get('temsil_edilen')}/{item.get('pdf_tablo_satiri')}" if 'pdf_tablo_satiri' in item
                      else item.get('hata')),
            'tablolar': [(t['tablo_no'], t['temsil_edilen'], t['pdf_satir']) for t in item.get('tablolar', [])],
            'eksik_satir': len(item.get('eksik_satirlar') or [])}
    return result


def aktar(conn, makaleler, *, yeniden=False, out=print):
    """Kontroller + aktarım, çağıranın AÇIK transaction'ında (commit / rollback çağıranın). AktarimHatasi ya da rapor.
    makaleler: makaleleri_hazirla() çıktısı (makaleyi_kontrol_et burada çağrılır)."""
    if conn.autocommit or not conn.in_transaction:
        raise AktarimHatasi('baglanti', 'aktar() autocommit kapalı, açık bir transaction içinde çağrılmalı')
    for makale in makaleler:
        makaleyi_kontrol_et(makale)
    doc_ids = [m['doc']['document_id'] for m in makaleler]
    if len(set(doc_ids)) != len(doc_ids):
        raise AktarimHatasi('girdi', 'aynı belge (document_id) iki --makale ile verildi')
    cursor = conn.cursor()
    try:
        report = {'baslangic': tablo_sayilari(cursor), 'makaleler': []}
        others_before = diger_satirlar_izi(cursor, doc_ids)
        for makale in makaleler:
            doc = makale['doc']
            doc_id, name = doc['document_id'], doc['filename']
            # 1. pdf_belgeleri
            cursor.execute('SELECT filename FROM pdf_belgeleri WHERE document_id = %s', (doc_id,))
            found = cursor.fetchone()
            if not found:
                raise AktarimHatasi(1, f'{name} pdf_belgeleri\'nde kayıtlı değil (document_id {doc_id[:12]}…); '
                                       f'önce ingestion akışıyla kaydedin')
            if found[0] != name:
                raise AktarimHatasi(1, f'{name}: pdf_belgeleri\'ndeki dosya adı farklı ({found[0]})')
            # 6. mevcut pilot satırları
            cursor.execute('SELECT id, satir_hash FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL',
                           (doc_id,))
            existing = cursor.fetchall()
            item = {'pdf': name, 'document_id': doc_id, 'girdiler': [str(p) for p, _ in makale['files']],
                    'beklenen': makale['beklenen'], 'kontrol': makale['kontrol'], 'silinen_onceki': 0,
                    'silinen_kalite_isareti': 0}
            if existing and not yeniden:
                raise AktarimHatasi(6, f'{name}: veritabanında zaten {len(existing)} pilot satırı var; yeniden aktarmak '
                                       f'için --yeniden kullanın')
            if existing:
                # Bağlı kalite işaretleri açıkça silinir (ON DELETE CASCADE'e güvenilmez), sonra satırlar.
                cursor.execute('DELETE q FROM kalite_isaretleri AS q JOIN klinik_bulgular AS b ON b.id = q.klinik_bulgu_id '
                               'WHERE b.document_id = %s AND b.bilgi_turu IS NOT NULL', (doc_id,))
                item['silinen_kalite_isareti'] = cursor.rowcount
                cursor.execute('DELETE FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL', (doc_id,))
                item['silinen_onceki'] = cursor.rowcount
                if item['silinen_onceki'] != len(existing):
                    raise AktarimHatasi(6, f'{name}: silinen eski pilot satırı {item["silinen_onceki"]}, beklenen {len(existing)}')
            # Aktarım: dosya dosya, aynı bağlantı; her dosyanın satırları id aralığından.
            rows, per_file = [], {}
            for path, records in makale['files']:
                last = _scalar(cursor, 'SELECT COALESCE(MAX(id), 0) FROM klinik_bulgular')
                try:
                    written = import_records(doc, records, connection=conn, pages=makale['pages'])
                except ValueError as exc:
                    raise AktarimHatasi(7, f'{name} / {path}: import_records reddetti: {exc}') from None
                if written != len(records):
                    raise AktarimHatasi(7, f'{name} / {path}: yazılan {written}, beklenen {len(records)} '
                                           f'(atlanan {len(records) - written}; satir_hash çakışması)')
                cursor.execute('SELECT id, satir_hash, bilgi_turu, baglam, kanit_turu, kanit_sayfa FROM klinik_bulgular '
                               'WHERE document_id = %s AND bilgi_turu IS NOT NULL AND id > %s ORDER BY id', (doc_id, last))
                new = [dict(zip(('id', 'satir_hash', 'bilgi_turu', 'baglam', 'kanit_turu', 'kanit_sayfa'), r), kaynak=str(path))
                       for r in cursor.fetchall()]
                if len(new) != written:
                    raise AktarimHatasi(7, f'{name} / {path}: yeni satır {len(new)}, yazılan {written}')
                rows += new
                per_file[str(path)] = written
            # 7. aktarım sonrası
            total = _scalar(cursor, 'SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL',
                            (doc_id,))
            verified = _scalar(cursor, "SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL "
                                       "AND review_status = 'verified'", (doc_id,))
            duplicate = _scalar(cursor, 'SELECT COUNT(*) FROM (SELECT satir_hash FROM klinik_bulgular WHERE document_id = %s '
                                        'GROUP BY satir_hash HAVING COUNT(*) > 1) AS x', (doc_id,))
            if (total, verified, duplicate, len(rows)) != (makale['beklenen'], makale['beklenen'], 0, makale['beklenen']):
                raise AktarimHatasi(7, f'{name}: aktarım sonrası sayılar tutmuyor (pilot satır {total}, verified {verified}, '
                                       f'çift satir_hash {duplicate}, yazılan {len(rows)}; beklenen {makale["beklenen"]})')
            old = {h for _, h in existing}
            new_hashes = {r['satir_hash'] for r in rows}
            item.update(yazilan=len(rows), dosya_basina=per_file, satirlar=rows,
                        id_araligi=[rows[0]['id'], rows[-1]['id']] if rows else None,
                        hash_karsilastirma={'ayni': len(old & new_hashes), 'yeni': len(new_hashes - old),
                                            'cikan': len(old - new_hashes)} if existing else None)
            report['makaleler'].append(item)
        others_after = diger_satirlar_izi(cursor, doc_ids)
        if others_after != others_before:
            raise AktarimHatasi(7, f'aktarılan makaleler dışındaki satırlar / kalite işaretleri değişti '
                                   f'(önce {others_before}, sonra {others_after})')
        report['diger_satirlar'] = others_after
        report['sonra'] = tablo_sayilari(cursor)
    finally:
        cursor.close()
    # 8. kalite (dry-run, aynı bağlantı)
    quality = _kalite_kontrolu(conn, makaleler, out)
    report['kalite'] = quality
    bad = {k: v for k, v in quality.items() if v['hata']}
    if bad:
        raise AktarimHatasi(8, 'data_quality --pilot hata verdi: ' + '; '.join(
            f"{v['dosya'] or k[:12]} hata {v['hata']}" for k, v in bad.items()), list(bad.values()))
    return report


# ---------------------------------------------------------------------------
# Prova koruması
# ---------------------------------------------------------------------------

def _korumaya_al(conn):
    """Prova bağlantısı: commit / autocommit açma / start_transaction / örtük commit yapan ifade hata verir."""
    base = type(conn)
    if getattr(base, '_aktarim_prova', False):
        return

    def blocked(what):
        raise AktarimHatasi('prova', f'prova modunda {what} yapılamaz (kalıcı yazma yalnız --yaz ile)')

    class ProvaBaglantisi(base):
        _aktarim_prova = True
        _korunan_sinif = base

        def commit(self):
            blocked('commit()')

        def start_transaction(self, *args, **kwargs):
            blocked('start_transaction()')

        @property
        def autocommit(self):
            return base.autocommit.fget(self)

        @autocommit.setter
        def autocommit(self, value):
            if value:
                blocked('autocommit = True')
            base.autocommit.fset(self, value)

        def cmd_query(self, query, *args, **kwargs):
            text = query.decode('utf-8', 'replace') if isinstance(query, (bytes, bytearray)) else str(query)
            text = BASTAKI_YORUM.sub('', text, count=1)
            if ORTUK_COMMIT.match(text):
                blocked(f'örtük commit yapan ifade ({text[:50]!r})')
            return base.cmd_query(self, query, *args, **kwargs)

    conn.__class__ = ProvaBaglantisi


def _korumayi_kaldir(conn):
    if getattr(type(conn), '_aktarim_prova', False):
        conn.__class__ = type(conn)._korunan_sinif


# ---------------------------------------------------------------------------
# Çıktı
# ---------------------------------------------------------------------------

def _belge_adi(filename):
    return re.sub(r'[^A-Za-z0-9]+', '_', Path(filename).stem).strip('_')[:60] or 'belge'


def satir_dosyalarini_yaz(report, zaman, klinik_once, klinik_sonra, hedef=AKTARIM_DIR):
    """COMMIT sonrası makale başına <belge>_satirlari_<zaman>.json (var olan dosya silinmez)."""
    hedef = Path(hedef)
    hedef.mkdir(parents=True, exist_ok=True)
    paths = []
    stamp = zaman.strftime('%Y%m%d-%H%M%S')
    for item in report['makaleler']:
        path = hedef / f'{_belge_adi(item["pdf"])}_satirlari_{stamp}.json'
        k = 1
        while path.exists():
            k += 1
            path = hedef / f'{_belge_adi(item["pdf"])}_satirlari_{stamp}_{k}.json'
        data = {'aktarim_zamani': zaman.isoformat(timespec='seconds'), 'pdf': item['pdf'], 'document_id': item['document_id'],
                'girdiler': item['girdiler'],
                'sayilar': {'dosya_basina': item['dosya_basina'], 'toplam': item['yazilan'],
                            'klinik_bulgular_once': klinik_once, 'klinik_bulgular_sonra': klinik_sonra,
                            'id_araligi': item['id_araligi'], 'silinen_onceki_pilot_satiri': item['silinen_onceki']},
                'satirlar': [{k2: r[k2] for k2 in ('id', 'satir_hash', 'kaynak', 'bilgi_turu', 'baglam', 'kanit_turu',
                                                    'kanit_sayfa')} for r in item['satirlar']]}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
        paths.append(path)
    return paths


def raporu_yaz(report, out=print):
    for item in report['makaleler']:
        out(f"\n== {item['pdf']} (document_id {item['document_id'][:12]}…)")
        for path, count in item['dosya_basina'].items():
            out(f'   {path}: {count} kayıt')
        out(f"   yazılan {item['yazilan']} / beklenen {item['beklenen']}; id {item['id_araligi']}; "
            f"silinen önceki pilot satırı {item['silinen_onceki']} (kalite işareti {item['silinen_kalite_isareti']})")
        if item['hash_karsilastirma']:
            h = item['hash_karsilastirma']
            out(f"   satir_hash önceki satırlarla: aynı {h['ayni']}, yeni {h['yeni']}, çıkan {h['cikan']}")
        tekrar = item['kontrol']['mudahale_farkli_tekrar']
        out(f"   validate_records: hatasız; 'tekrar' sayılıp mudahale_en farkı nedeniyle kabul edilen: {len(tekrar)}")
        for t in tekrar:
            out(f"     - {t['kayit']} ↔ {t['onceki']}: {t['klinik_bilgi_en']} = {t['deger']} "
                f"(mudahale_en: {t['mudahale_en'][0]} / {t['mudahale_en'][1]})")
    out('\nKalite (data_quality --pilot --dry-run, aynı transaction):')
    for doc_id, q in report['kalite'].items():
        out(f"   {q['dosya'] or doc_id[:12]}: pilot satır {q['pilot_satir']}; hata {q['hata']} / uyarı {q['uyari']} / "
            f"bilgi {q['bilgi']}; tablo eksiksizliği {q['tablo']} {q['tablolar']} (eksik satır {q['eksik_satir']})")
    out(f"\nklinik_bulgular {report['baslangic']['klinik_bulgular']} -> {report['sonra']['klinik_bulgular']}; "
        f"kalite_isaretleri {report['baslangic']['kalite_isaretleri']} -> {report['sonra']['kalite_isaretleri']} "
        f"(transaction içinde); diğer satırlar değişmedi ({report['diger_satirlar']['satir']} satır)")


# ---------------------------------------------------------------------------
# Komut
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog='python -m src.aktarim',
                                     description='Doğrulanmış klinik kayıt JSON\'larını veritabanına aktarır (varsayılan: prova)')
    parser.add_argument('--makale', nargs='+', action='append', metavar=('PDF', 'JSON'), required=True,
                        help='PDF ve bir ya da birden fazla JSON; birden fazla makale için tekrarlanır')
    parser.add_argument('--yaz', action='store_true', help='kalıcı yaz (COMMIT); --yedek zorunlu')
    parser.add_argument('--yedek', help='son 24 saatte alınmış, boş olmayan veritabanı yedeği (mysqldump)')
    parser.add_argument('--yeniden', action='store_true',
                        help='makalenin mevcut pilot satırlarını (ve bağlı kalite işaretlerini) silip yeniden aktar')
    parser.add_argument('--kalite', action='store_true', help='--yaz sonrası data_quality --pilot kalıcı çalıştır')
    parser.add_argument('--kur', action='store_true', help='--yaz sonrası python -m src.sunum_hoca --kur')
    args = parser.parse_args(argv)
    if (args.kalite or args.kur) and not args.yaz:
        parser.error('--kalite ve --kur yalnız --yaz ile kullanılır')
    return args


def main(argv=None, out=print):
    args = parse_args(argv)
    from src.database import get_connection

    try:
        backup = yedek_kontrolu(args.yedek) if args.yaz else None
        makaleler = makaleleri_hazirla(args.makale)
    except AktarimHatasi as exc:
        out(f'DURDU: {exc}')
        return 1
    mode = 'YAZ (COMMIT)' if args.yaz else 'PROVA (ROLLBACK)'
    out(f'Aktarım — mod: {mode}' + (f"; yedek {backup['dosya']} ({backup['boyut']} bayt, {backup['olusturma']})"
                                     if backup else '') + (' ; --yeniden' if args.yeniden else ''))
    conn = get_connection()
    report, zaman = None, datetime.now()
    try:
        conn.autocommit = False
        if conn.in_transaction:
            conn.rollback()
        conn.start_transaction()
        if not args.yaz:
            _korumaya_al(conn)
        report = aktar(conn, makaleler, yeniden=args.yeniden, out=out)
        raporu_yaz(report, out)
        if args.yaz:
            conn.commit()
            out('\nCOMMIT edildi.')
        else:
            conn.rollback()
    except AktarimHatasi as exc:
        conn.rollback()
        out(f'\nDURDU (ROLLBACK): {exc}')
        for detail in exc.ayrinti[:20]:
            out(f'   - {detail}')
        if len(exc.ayrinti) > 20:
            out(f'   … +{len(exc.ayrinti) - 20}')
        return 1
    except Exception:
        conn.rollback()
        raise
    finally:
        _korumayi_kaldir(conn)
        conn.close()
    check = get_connection()
    try:
        cursor = check.cursor()
        after = tablo_sayilari(cursor)
        cursor.close()
        check.rollback()
    finally:
        check.close()
    if not args.yaz:
        same = after == report['baslangic']
        out(f"\nPROVA: ROLLBACK edildi; veritabanı {'değişmedi' if same else 'DEĞİŞTİ!'} "
            f"(klinik_bulgular {after['klinik_bulgular']}, kalite_isaretleri {after['kalite_isaretleri']})")
        return 0 if same else 2
    paths = satir_dosyalarini_yaz(report, zaman, report['baslangic']['klinik_bulgular'], after['klinik_bulgular'])
    for path in paths:
        out(f'Satır listesi: {path}')
    quality_text = 'çalıştırılmadı (--kalite yok)'
    if args.kalite:
        from src.data_quality import parse_args as dq_args, print_summary, run_quality
        summary, flags, _ = run_quality(dq_args(['--pilot']), out=out)
        print_summary(summary, flags, out=out)
        pilot = summary.get('pilot') or {}
        levels = Counter()
        for category in (pilot.get('kategori_seviye') or {}).values():
            levels.update(category)
        quality_text = (f"pilot hata {levels.get('hata', 0)} / uyarı {levels.get('uyari', 0)} / bilgi {levels.get('bilgi', 0)}; "
                        f"eksiksizlik {pilot.get('temsil_edilen')}/{pilot.get('pdf_tablo_satiri')}; sürüm "
                        f"{summary.get('kontrol_surumu')}")
    if args.kur:
        subprocess.run([sys.executable, '-m', 'src.sunum_hoca', '--kur'], check=True)
    out('\nÖZET')
    for item in report['makaleler']:
        out(f"   {item['pdf']}: {item['yazilan']} kayıt yazıldı")
    out(f"   klinik_bulgular toplam: {after['klinik_bulgular']}")
    out(f'   kalite: {quality_text}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
