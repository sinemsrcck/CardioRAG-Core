"""
src/aktarim.py testi (python -m src.aktarim): doğrulanmış JSON -> klinik_bulgular aktarım komutu.

Çalıştırma:
    python -m evaluation.test_aktarim

Gerçek veri değişmez:
- Veritabanı adımları tek transaction'da ve sonunda ROLLBACK. Bağlantı evaluation/pilot_izolasyon korumasıyla
  (commit / örtük commit hata verir) sarılır; sonda AYRI bağlantıdan gerçek pilot satırları (sayı, id aralığı,
  id + satir_hash parmak izi, bağlı kalite işaretleri) ve tablo sayıları başlangıçla karşılaştırılır.
- Dosya kontrolleri (verified, PDF eşleşmesi, tekrar, tür çakışması) geçici JSON kopyalarıyla, veritabanısız.
- data/review/ JSON'larına yazılmaz; geçici dosyalar sistem geçici dizininde.

Kontroller:
- Prova (main, --yaz yok): ROLLBACK, tablolar ve pilot satırları değişmedi; prova koruması commit'i engelliyor.
- --yaz: --yedek olmadan / yok / boş / 24 saatten eski yedekle reddediliyor; --kalite / --kur yalnız --yaz ile.
- Kontrol 3: verified olmayan kayıt; kontrol 2: başka PDF'in JSON'u; kontrol 4: gerçek tekrar;
  kontrol 5: dosyalar arası aynı kanıt + bilgi, farklı bilgi_turu -> reddediliyor.
- Kontrol 6: zaten aktarılmış makale (--yeniden olmadan) reddediliyor.
- --yeniden + ikinci makale tek çağrıda: pilot 243 (satir_hash'ler mevcutla aynı, eski 87 kalite işareti silindi),
  Troponin 84; diğer satırlar (RAG) ve kalite işaretleri değişmedi; Troponin'deki mudahale_en farkı olan 3 "tekrar"
  kabul edildi ve listelendi; kalite hata 0 (pilot uyarı 48 / bilgi 39; Troponin eksiksizlik 27/27).
"""

import copy
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from evaluation.pilot_izolasyon import _guard, gercek_pilot_durumu, guvenlik_kontrolu, korumayi_kaldir
import src.aktarim as ak

PILOT_PDF = Path('data/pdfs/TK-cardiovascular36-2-1.pdf')
PILOT_FILES = [Path('data/review/pilot/zehra_dogrulanmis_v3_3.json'),
               Path('data/review/pilot/pilot_findings_v3_3_sinem_sql_verified_v2.json')]
TROP_PDF = Path('data/pdfs/who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf')
TROP_FILE = Path('data/review/troponin/zehra_troponin_tablo_dogrulanmis.json')

results = []


def check(name, expected, actual, passed=None):
    ok = (expected == actual) if passed is None else passed
    results.append(ok)
    print(f"{name:<110} {str(expected)[:40]:<42} {str(actual)[:40]:<42} {'OK' if ok else 'HATA'}")


def raises(func, kontrol=None):
    """(hata verdi mi, kontrol numarası, mesaj)."""
    try:
        func()
    except ak.AktarimHatasi as exc:
        return True, exc.kontrol, str(exc)
    return False, None, ''


def table_counts():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        counts = ak.tablo_sayilari(cursor)
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE cikarma_yontemi = 'rag'")
        counts['rag'] = int(cursor.fetchone()[0])
        cursor.close()
        conn.rollback()
        return counts
    finally:
        conn.close()


def write_json(folder, name, records):
    path = Path(folder) / name
    path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    return path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def test_backup_and_args(tmp):
    check('--yaz, --yedek yok -> reddedildi', (True, 'yedek'), raises(lambda: ak.yedek_kontrolu(None))[:2])
    check('--yedek dosyası yok -> reddedildi', (True, 'yedek'),
          raises(lambda: ak.yedek_kontrolu(Path(tmp) / 'yok.sql'))[:2])
    empty = Path(tmp) / 'bos.sql'
    empty.write_text('', encoding='utf-8')
    check('--yedek boş -> reddedildi', (True, 'yedek'), raises(lambda: ak.yedek_kontrolu(empty))[:2])
    good = Path(tmp) / 'yedek.sql'
    good.write_text('-- MySQL dump\nCREATE TABLE klinik_bulgular (id int);\n', encoding='utf-8')
    check('--yedek 24 saatten eski -> reddedildi', (True, 'yedek'),
          raises(lambda: ak.yedek_kontrolu(good, simdi=datetime.now() + timedelta(hours=25)))[:2])
    check('--yedek yeni ve dolu -> kabul', True, ak.yedek_kontrolu(good)['boyut'] > 0)
    lines = []
    code = ak.main(['--makale', str(TROP_PDF), str(TROP_FILE), '--yaz'], out=lines.append)
    check('main --yaz --yedek olmadan: çıkış 1, veritabanına dokunmadan durdu', (1, True),
          (code, any('--yedek' in x for x in lines)))
    for flag in ('--kalite', '--kur'):
        try:
            ak.parse_args(['--makale', str(TROP_PDF), str(TROP_FILE), flag])
            rejected = False
        except SystemExit:
            rejected = True
        check(f'{flag} yalnız --yaz ile (yoksa argüman hatası)', True, rejected)


def test_file_checks(tmp):
    trop = load(TROP_FILE)
    pending = copy.deepcopy(trop)
    pending[10]['review_status'] = 'pending'
    path = write_json(tmp, 'pending.json', pending)
    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': TROP_PDF, 'dosyalar': [path]}))
    check('kontrol 3: verified olmayan kayıt reddedildi', (True, 3), result[:2])

    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': PILOT_PDF, 'dosyalar': [TROP_FILE]}))
    check('kontrol 2: başka PDF\'in JSON\'u (Troponin JSON + pilot PDF) reddedildi', (True, 2), result[:2])
    wrapped = write_json(tmp, 'beyan.json', {'document_id': 'f' * 64, 'kayitlar': trop})
    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': TROP_PDF, 'dosyalar': [wrapped]}))
    check('kontrol 2: JSON kökü başka document_id beyan ediyor -> reddedildi', (True, 2), result[:2])

    dup = trop + [copy.deepcopy(trop[0])]
    path = write_json(tmp, 'tekrar.json', dup)
    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': TROP_PDF, 'dosyalar': [path]}))
    check('kontrol 4: gerçek tekrar (aynı kayıt iki kez) reddedildi', (True, 4, True),
          (result[0], result[1], 'gerçek tekrar' in result[2]))

    # Dosyalar arası: bir dosyadaki kayıt, ikinci dosyada farklı bilgi_turu ile.
    zehra = load(PILOT_FILES[0])
    text = [r for r in zehra if r.get('kanit_turu', 'metin') == 'metin' and not r.get('kanit_parcalari')][:5]
    other = copy.deepcopy(text[0])
    other['bilgi_turu'] = 'diger' if text[0]['bilgi_turu'] != 'diger' else 'olcum'
    a = write_json(tmp, 'a.json', text)
    b = write_json(tmp, 'b.json', [other])
    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': PILOT_PDF, 'dosyalar': [a, b]}))
    check('kontrol 5: dosyalar arası aynı kanıt + bilgi, farklı bilgi_turu reddedildi', (True, 5), result[:2])

    same_file = write_json(tmp, 'ayni_dosya.json', text + [other])
    result = raises(lambda: ak.makaleyi_kontrol_et({'pdf': PILOT_PDF, 'dosyalar': [same_file]}))
    check('kontrol 5 yalnız dosyalar arası: aynı dosyada farklı tür çakışma sayılmaz', False, result[0])

    report = ak.makaleyi_kontrol_et({'pdf': TROP_PDF, 'dosyalar': [TROP_FILE]})
    accepted = report['kontrol']['mudahale_farkli_tekrar']
    check('Troponin: mudahale_en farkı olan 3 "tekrar" kabul edildi ve listelendi (#66, #77, #78)',
          ['#66', '#77', '#78'], [t['kayit'].split('.json')[1] for t in accepted])


def test_prova_main(start):
    lines = []
    code = ak.main(['--makale', str(TROP_PDF), str(TROP_FILE)], out=lines.append)
    check('prova (main): çıkış 0, "veritabanı değişmedi"', (0, True), (code, any('değişmedi' in x for x in lines)))
    check('prova (main): tablo sayıları başlangıçla aynı', start, table_counts())

    from src.database import get_connection
    conn = get_connection()
    try:
        conn.autocommit = False
        if conn.in_transaction:
            conn.rollback()
        conn.start_transaction()
        ak._korumaya_al(conn)
        blocked = [raises(conn.commit)[0], raises(lambda: conn.cmd_query('CREATE TABLE x_aktarim_test (id INT)'))[0]]
        conn.rollback()
        ak._korumayi_kaldir(conn)
        check('prova koruması: commit() ve örtük commit (CREATE) engellendi', [True, True], blocked)
    finally:
        conn.close()


def test_database(start_pilot):
    from src.database import get_connection

    conn = get_connection()
    conn.autocommit = False
    if conn.in_transaction:
        conn.rollback()
    conn.start_transaction()
    _guard(conn)  # commit / örtük commit test boyunca hata verir
    try:
        pilot = {'pdf': PILOT_PDF, 'dosyalar': list(PILOT_FILES)}
        result = raises(lambda: ak.aktar(conn, [dict(pilot)]))
        check('kontrol 6: zaten aktarılmış makale --yeniden olmadan reddedildi', (True, 6), result[:2])

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE cikarma_yontemi = 'rag'")
        rag_before = cursor.fetchone()[0]
        report = ak.aktar(conn, [dict(pilot), {'pdf': TROP_PDF, 'dosyalar': [TROP_FILE]}], yeniden=True,
                          out=lambda *_: None)
        by_pdf = {m['pdf']: m for m in report['makaleler']}
        p, t = by_pdf[PILOT_PDF.name], by_pdf[TROP_PDF.name]
        check('--yeniden: pilot 243 yazıldı, eski 243 satır + 87 kalite işareti silindi, satir_hash\'ler aynı',
              (243, 243, 87, {'ayni': 243, 'yeni': 0, 'cikan': 0}),
              (p['yazilan'], p['silinen_onceki'], p['silinen_kalite_isareti'], p['hash_karsilastirma']))
        check('--yeniden: pilot dosya başına (zehra 207, sinem 36)', [207, 36], list(p['dosya_basina'].values()))
        check('ikinci makale: Troponin 84 yazıldı, önceki satır yok', (84, 0), (t['yazilan'], t['silinen_onceki']))
        check('Troponin: mudahale_en farklı 3 "tekrar" kabul edildi', 3, len(t['kontrol']['mudahale_farkli_tekrar']))
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE cikarma_yontemi = 'rag'")
        check('--yeniden yalnız o makaleye dokundu: RAG satırları ve diğer satırların parmak izi aynı',
              (rag_before, report['baslangic']['klinik_bulgular'] - 243),
              (cursor.fetchone()[0], report['diger_satirlar']['satir']))
        check('transaction içinde klinik_bulgular 292 -> 376', (292, 376),
              (report['baslangic']['klinik_bulgular'], report['sonra']['klinik_bulgular']))
        cursor.close()
        q = {v['dosya']: v for v in report['kalite'].values()}
        check('kalite (dry-run): pilot hata 0 / uyarı 48 / bilgi 39, 58/58; Troponin hata 0, 27/27',
              ((0, 48, 39, '58/58'), (0, '27/27')),
              ((q[PILOT_PDF.name]['hata'], q[PILOT_PDF.name]['uyari'], q[PILOT_PDF.name]['bilgi'], q[PILOT_PDF.name]['tablo']),
               (q[TROP_PDF.name]['hata'], q[TROP_PDF.name]['tablo'])))
        # --yaz sonrası satır listesi (geçici dizine; COMMIT yok): makale başına dosya, ikinci yazım öncekini silmez.
        with tempfile.TemporaryDirectory() as out_dir:
            when = datetime.now()
            first = ak.satir_dosyalarini_yaz(report, when, 292, 376, hedef=out_dir)
            second = ak.satir_dosyalarini_yaz(report, when, 292, 376, hedef=out_dir)
            data = json.loads(first[0].read_text(encoding='utf-8'))
            check('satır listesi: makale başına zaman damgalı dosya; ikinci yazım yeni ad (öncekini silmez)',
                  (2, 4, True), (len(first), len(list(Path(out_dir).iterdir())), set(first).isdisjoint(second)))
            check('satır listesi: 243 satır (id, satir_hash, kaynak, bilgi_turu, baglam, kanit_turu, kanit_sayfa), '
                  'girdiler ve sayılar', (243, ['baglam', 'bilgi_turu', 'id', 'kanit_sayfa', 'kanit_turu', 'kaynak',
                                                'satir_hash'], 2, 243),
                  (len(data['satirlar']), sorted(data['satirlar'][0]), len(data['girdiler']), data['sayilar']['toplam']))
        # Aynı makale ikinci kez (aynı transaction'da) --yeniden'siz: yine kontrol 6.
        result = raises(lambda: ak.aktar(conn, [{'pdf': TROP_PDF, 'dosyalar': [TROP_FILE]}]))
        check('aktarılan makale ikinci kez --yeniden olmadan reddedildi', (True, 6), result[:2])
    finally:
        conn.rollback()
        korumayi_kaldir(conn)
        conn.close()
    guvenlik_kontrolu(start_pilot, 'test_aktarim')
    check('ROLLBACK sonrası gerçek pilot satırları başlangıçla aynı (ayrı bağlantı)', True, True)


def main():
    for path in (PILOT_PDF, TROP_PDF, TROP_FILE, *PILOT_FILES):
        if not path.exists():
            print(f'Gerekli dosya yok: {path}')
            sys.exit(1)
    start = table_counts()
    start_pilot = gercek_pilot_durumu()
    with tempfile.TemporaryDirectory() as tmp:
        test_backup_and_args(tmp)
        test_file_checks(tmp)
    test_prova_main(start)
    test_database(start_pilot)
    check('sonda tablo sayıları başlangıçla aynı (klinik_bulgular, kalite_isaretleri, rag)', start, table_counts())
    print(f"\nBaşlangıç: {start}")
    failed = results.count(False)
    if failed:
        print(f'BAŞARISIZ: {failed}/{len(results)} kontrol geçmedi.')
        sys.exit(1)
    print(f'BAŞARILI: {len(results)} kontrolün hepsi geçti.')


if __name__ == '__main__':
    main()
