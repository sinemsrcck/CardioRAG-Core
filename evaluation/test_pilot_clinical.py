"""
src/pilot_clinical.import_records testi (gerçek veritabanı, kalıcı değişiklik YOK).

Çalıştırma:
    python -m evaluation.test_pilot_clinical

Ön koşul: klinik_bulgular'da bilgi_turu / baglam / klinik_bilgi / klinik_bilgi_en sütunları
(python -m src.database --init). Bu test init_schema ÇAĞIRMAZ (DDL örtük commit yapar).

Bütün yazımlar tek transaction içinde, import_records'a connection verilerek yapılır
(import_records commit etmez) ve sonunda GERİ ALINIR. Başlangıç/son satır sayısı ve
tablo özeti aynı olmalı.

Kontroller (data/review/pilot/pilot_tablo_aktarim.json'dan 6 kayıt, test kopyasında verified):
- Yapılandırılmış alanlar doğru yazılıyor; hasta_sayisi NULL, n'ler taslak_notu'da.
- HbA1c'nin iki grup satırı (aynı kanıt) ayrı satır olarak giriyor.
- import_records commit etmiyor (ikinci bağlantı yeni satırları görmüyor).
- Aynı kayıt iki kez: validate_records "tekrar" hatası; ikinci import 0 satır yazar.
- Şemada olmayan / desteklenmeyen alan açık hata; verified olmayan ve geçersiz bilgi_turu reddedilir.
- Çok parçalı kanıt (pilot_findings_v3_2.json'daki 6 kayıt): kanit_sayfa_bitis, birlestirme ve
  taslak_notu["kanit_parcalari"] yazılıyor, kanit_cumle ilk parça; tutarsız parçalı kayıt reddediliyor.
"""

import hashlib
import json
import sys
from pathlib import Path

from evaluation.pilot_izolasyon import gercek_pilot_durumu, gercek_pilotu_ayir, guvenlik_kontrolu, korumayi_kaldir
from src.database import count_rows, get_connection, get_table_columns
from src.pilot_clinical import import_records, pdf_pages, validate_records

PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')
TRANSFER = Path('data/review/pilot/pilot_tablo_aktarim.json')
V32 = Path('data/review/pilot/pilot_findings_v3_2.json')
PILOT_COLUMNS = ('bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en')
SELECT_COLUMNS = ('id', 'document_id', 'bilgi_turu', 'baglam', 'klinik_bilgi_en', 'kanit_sayfa', 'kanit_cumle',
                  'kanit_turu', 'review_status', 'cikarma_yontemi', 'hasta_grubu', 'hasta_sayisi', 'comparator',
                  'deger', 'unit', 'p', 'ci95', 'etki_turu', 'sonuc_olcutu_tr', 'mudahale_en', 'mudahale_turu',
                  'outcome_type', 'taslak_notu', 'satir_hash')


def table_hash(cursor):
    cursor.execute('SELECT * FROM klinik_bulgular ORDER BY id')
    rows = [[None if v is None else str(v) for v in row] for row in cursor.fetchall()]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode('utf-8')).hexdigest()


def text(value):
    return value.decode('utf-8') if isinstance(value, (bytes, bytearray)) else value


def pick(transfer, table_no, variable):
    """Aktarım kayıtlarından bir tablo satırının grup/analiz kayıtları (taslak_notu.tablo_no + degisken)."""
    notes = [json.loads(r['taslak_notu']) for r in transfer]
    return [r for r, n in zip(transfer, notes) if n['tablo_no'] == table_no and n['degisken'] == variable]


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    def raises(name, func, needle):
        try:
            func()
        except ValueError as exc:
            check(name, f'ValueError ~ "{needle}"', str(exc)[:60], needle in str(exc))
        else:
            check(name, f'ValueError ~ "{needle}"', 'hata yok', False)

    for path in (PDF, TRANSFER, V32):
        if not path.exists():
            print(f'Bulunamadı: {path} (aktarım için: python -m src.pilot_tablo --pdf {PDF} --overwrite)')
            sys.exit(1)

    conn = get_connection()
    cur = conn.cursor()
    missing = [c for c in PILOT_COLUMNS if c not in get_table_columns(cur, 'klinik_bulgular')]
    if missing:
        print(f'klinik_bulgular sütunları eksik: {missing}. Önce: python -m src.database --init')
        conn.close()
        sys.exit(1)

    doc, pages = pdf_pages(PDF)
    transfer = json.loads(TRANSFER.read_text(encoding='utf-8'))
    sample = pick(transfer, 1, 'HbA1c') + pick(transfer, 2, 'HCR') + pick(transfer, 1, 'Warfarin')
    verified = [dict(r, review_status='verified') for r in sample]  # test kopyası; dosya değişmez

    initial_count = count_rows(cur, 'klinik_bulgular')
    initial_hash = table_hash(cur)
    cur.execute('SELECT COALESCE(MAX(id), 0) FROM klinik_bulgular')
    max_id = cur.fetchone()[0]
    conn.rollback()  # okuma anlık görüntüsünü bırak; yazımlar tek transaction'da başlar

    real_pilot = gercek_pilot_durumu()  # ayrı bağlantıdan, silmeden önce
    conn.start_transaction()
    try:
        # Gerçek pilot verisi veritabanındaysa test kayıtları satir_hash ile atlanırdı: belgenin gerçek
        # pilot satırları bu transaction içinde ayrılır, sonda ROLLBACK ile geri gelir.
        removed = gercek_pilotu_ayir(conn, doc['document_id'])
        check('gerçek pilot satırları transaction içinde ayrıldı (bilgi)', '-', f'{removed} satır', True)
        initial_pilot_free = count_rows(cur, 'klinik_bulgular')
        check('örnek kayıt (HbA1c x2, HCR x2, Warfarin x2)', 6, len(verified))
        check('aktarım dosyasında review_status yok', 0, sum('review_status' in r for r in transfer))
        accepted, errors = validate_records(verified, pages)
        check('validate_records: 6 kabul, 0 hata', (6, 0), (len(accepted), len(errors)))

        written = import_records(doc, verified, connection=conn)
        check('import_records yazılan', 6, written)
        cur.execute(f"SELECT {', '.join(SELECT_COLUMNS)} FROM klinik_bulgular WHERE id > %s ORDER BY id", (max_id,))
        rows = [dict(zip(SELECT_COLUMNS, map(text, row))) for row in cur.fetchall()]
        check('yeni satır (transaction içinde)', 6, len(rows))

        hb = [r for r in rows if r['sonuc_olcutu_tr'] == 'HbA1c']
        check('HbA1c: iki ayrı satır, aynı kanıt', (2, 1, 2),
              (len(hb), len({r['kanit_cumle'] for r in hb}), len({r['satir_hash'] for r in hb})))
        check('HbA1c grup satırları',
              [('Ischemic event', 'No ischemic event', '7.83±1.2', '<0.001', 'diger', 'çalışma popülasyonu'),
               ('No ischemic event', 'Ischemic event', '6.23±1.2', '<0.001', 'diger', 'çalışma popülasyonu')],
              [(r['hasta_grubu'], r['comparator'], r['deger'], r['p'], r['etki_turu'], r['outcome_type']) for r in hb])
        check('HbA1c taslak_notu grup_n / comparator_n', [(372, 853), (853, 372)],
              [(json.loads(r['taslak_notu'])['grup_n'], json.loads(r['taslak_notu'])['comparator_n']) for r in hb])
        hcr = [r for r in rows if r['sonuc_olcutu_tr'] == 'HCR']
        check('Table 2 HCR: OR / CI / p / analiz',
              [('OR', '3.124', '2.213-4.310', '<0.001', 'univariate', None),
               ('OR', '3.216', '2.334-4.365', '<0.001', 'multivariate', None)],
              [(r['etki_turu'], r['deger'], r['ci95'], r['p'], json.loads(r['taslak_notu'])['analiz'], r['hasta_grubu'])
               for r in hcr])
        wf = [r for r in rows if r['mudahale_en'] == 'Warfarin']
        check('Warfarin: ilaç alanları',
              [('arka plan tedavisi', 'uygulama oranı', 'kullanım oranı', '21 (5.64)', 'oran / yuzde'),
               ('arka plan tedavisi', 'uygulama oranı', 'kullanım oranı', '42 (4.92)', 'oran / yuzde')],
              [(r['mudahale_turu'], r['outcome_type'], r['sonuc_olcutu_tr'], r['deger'], r['etki_turu']) for r in wf])
        check('ortak alanlar (verified, manuel, tablo, belge)', True, all(
            r['review_status'] == 'verified' and r['cikarma_yontemi'] == 'manuel' and r['kanit_turu'] == 'tablo'
            and r['document_id'] == doc['document_id'] for r in rows))
        check('bilgi_turu / baglam / klinik_bilgi_en kayıttan', True, all(
            (r['bilgi_turu'], r['baglam'], r['klinik_bilgi_en'], int(r['kanit_sayfa'])) ==
            (s['bilgi_turu'], s['baglam'], s['klinik_bilgi_en'], s['sayfa'])
            for r, s in zip(rows, verified)))
        check('hasta_sayisi hep NULL', True, all(r['hasta_sayisi'] is None for r in rows))
        check('Table 2 mudahale_turu / outcome_type boş', True,
              all(r['mudahale_turu'] is None and r['outcome_type'] is None for r in hcr))

        other = get_connection()
        try:
            other_cur = other.cursor()
            check('import_records commit etmedi (2. bağlantı)', initial_count, count_rows(other_cur, 'klinik_bulgular'))
            check('import_records commit etmedi: bu bağlantıda +6', initial_pilot_free + 6, count_rows(cur, 'klinik_bulgular'))
            other_cur.close()
        finally:
            other.close()

        # Tekrar: aynı kayıt iki kez.
        accepted, errors = validate_records(verified + [dict(verified[0])], pages)
        check('validate_records: aynı kayıt -> "tekrar"', (6, 1, True),
              (len(accepted), len(errors), bool(errors) and errors[0]['hata'].startswith('tekrar')))
        check('aynı kayıt 2. import: 0 satır (satir_hash)', 0, import_records(doc, verified[:1], connection=conn))

        # Açık hatalar (hiçbiri yazmadan önce durmalı).
        after_valid = count_rows(cur, 'klinik_bulgular')
        raises('şemada olmayan alan -> hata', lambda: import_records(
            doc, [dict(verified[0], olmayan_sutun='x')], connection=conn), 'olmayan_sutun')
        raises('hasta_sayisi alanı -> hata (ekip kararı)', lambda: import_records(
            doc, [dict(verified[0], hasta_sayisi=372)], connection=conn), 'hasta_sayisi')
        raises('pending kayıt reddedilir', lambda: import_records(
            doc, [dict(verified[0], review_status='pending')], connection=conn), 'manuel onaylı')
        raises('review_status yok (aktarım dosyası olduğu gibi) reddedilir', lambda: import_records(
            doc, [sample[0]], connection=conn), 'manuel onaylı')
        raises('geçersiz bilgi_turu reddedilir', lambda: import_records(
            doc, [dict(verified[0], bilgi_turu='uydurma')], connection=conn), 'bilgi_turu')
        check('hatalı çağrılar satır eklemedi', after_valid, count_rows(cur, 'klinik_bulgular'))

        # Çok parçalı kanıt (v3.2): 6 kayıt, test kopyasında verified; aynı transaction, sonda ROLLBACK.
        v32 = json.loads(V32.read_text(encoding='utf-8'))
        multi = [dict(r, review_status='verified') for r in v32 if 'kanit_parcalari' in r]
        check('v3.2 çok parçalı kayıt', 6, len(multi))
        cur.execute('SELECT COALESCE(MAX(id), 0) FROM klinik_bulgular')
        before_multi = cur.fetchone()[0]
        check('çok parçalı: import_records yazdı (sayfalar pdf_pages doc\'undan)', 6,
              import_records(doc, multi, connection=conn))
        cur.execute('SELECT kanit_sayfa, kanit_sayfa_bitis, birlestirme, kanit_cumle, taslak_notu, klinik_bilgi_en, '
                    'deger, unit, satir_hash FROM klinik_bulgular WHERE id > %s ORDER BY id', (before_multi,))
        stored = [tuple(map(text, row)) for row in cur.fetchall()]
        check('çok parçalı: kanit_sayfa / kanit_sayfa_bitis / birlestirme',
              [(r['sayfa'], r['sayfa_bitis'], r['birlestirme']) for r in multi],
              [(int(s[0]), int(s[1]), s[2]) for s in stored])
        check('çok parçalı: kanit_cumle = ilk parça (birleştirilmiş alıntı yok)', True,
              all(s[3] == r['kanit_parcalari'][0]['metin'] for s, r in zip(stored, multi)))
        check('çok parçalı: taslak_notu["kanit_parcalari"] kayıttaki parçalar', True,
              all(json.loads(s[4]) == {'kanit_parcalari': r['kanit_parcalari']} for s, r in zip(stored, multi)))
        check('çok parçalı: klinik_bilgi_en / deger / unit kayıttan', True,
              all((s[5], s[6], s[7]) == (r['klinik_bilgi_en'], r.get('deger'), r.get('unit')) for s, r in zip(stored, multi)))
        check('çok parçalı: 6 ayrı satir_hash (aynı parçalı farklı bilgiler ayrı)', 6, len({s[8] for s in stored}))
        check('çok parçalı: aynı kayıt 2. import 0 satır', 0, import_records(doc, multi[:1], connection=conn))
        after_multi = count_rows(cur, 'klinik_bulgular')
        bad = dict(multi[0], kanit_cumle=multi[0]['kanit_parcalari'][0]['metin'] + ' '
                   + multi[0]['kanit_parcalari'][1]['metin'])
        raises('tutarsız parçalı kayıt (birleştirilmiş kanit_cumle) reddedilir', lambda: import_records(
            doc, [bad], connection=conn), 'ilk kanıt parçası')
        raises('tutarsız parçalı kayıt (yanlış sayfa_bitis) reddedilir', lambda: import_records(
            doc, [dict(multi[0], sayfa_bitis=multi[0]['sayfa'])], connection=conn), 'sayfa_bitis')
        raises('sayfası olmayan doc ile çok parçalı kayıt reddedilir', lambda: import_records(
            {k: v for k, v in doc.items() if k != 'pages'}, multi[:1], connection=conn), 'PDF sayfaları gerekli')
        check('reddedilen çok parçalı kayıtlar satır eklemedi', after_multi, count_rows(cur, 'klinik_bulgular'))
    finally:
        conn.rollback()
        korumayi_kaldir(conn)
        # Güvenlik: ayrı bağlantıdan gerçek pilot satırları başlangıçla aynı mı (değilse IzolasyonHatasi).
        check('güvenlik: gerçek pilot satırları ayrı bağlantıdan başlangıçla aynı', real_pilot,
              guvenlik_kontrolu(real_pilot, 'test_pilot_clinical'))

    final_count = count_rows(cur, 'klinik_bulgular')
    final_hash = table_hash(cur)
    cur.close()
    conn.close()
    check('ROLLBACK sonrası satır sayısı', initial_count, final_count)
    check('ROLLBACK sonrası tablo özeti aynı', initial_hash[:12], final_hash[:12], initial_hash == final_hash)

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
    print(f'BAŞARILI: {len(results)} kontrolün hepsi geçti. (başlangıç = son = {initial_count} satır)')


if __name__ == '__main__':
    main()
