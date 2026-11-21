"""
evaluation/pilot_izolasyon.py güvenlik testleri.

Çalıştırma:
    python -m evaluation.test_pilot_izolasyon

Gerçek veritabanına bağlanır. Silme yalnız korumalı transaction içinde yapılır ve sonda ROLLBACK
edilir; korumanın engellemesi gereken işlemler (commit, örtük commit) sınanırken hiçbiri sunucuya
ulaşmadan durdurulmalıdır. Sonda ayrı bağlantıdan gerçek pilot satırları başlangıçla aynı olmalı.

Kontroller:
- autocommit açık / transaction yok -> gercek_pilotu_ayir hiçbir şey silmeden hata verir.
- Koruma altında: commit(), start_transaction(), autocommit = True, CREATE / DROP / ALTER / TRUNCATE /
  START TRANSACTION / BEGIN / COMMIT / SET autocommit / LOCK TABLES, silmeden önce açılmış cursor'dan
  DDL, dictionary cursor'dan DDL -> IzolasyonHatasi ve transaction açık kalır. Bu sınama GÜVENLİ yapılır:
  gerçek satırı olmayan bir belge kimliği (0 satır silinir) ve var olmayan tablo / şema adları.
- SELECT / UPDATE / SAVEPOINT ve ROLLBACK serbest.
- Gerçek belgede silme başka bağlantıdan görünmez.
- ROLLBACK + korumayi_kaldir sonrası satırlar geri gelir; guvenlik_kontrolu geçer.
- guvenlik_kontrolu, başlangıçtan farklı bir durumda açık mesajla hata verir.
"""

import sys

from evaluation.pilot_izolasyon import (IzolasyonHatasi, gercek_pilot_durumu, gercek_pilotu_ayir, guvenlik_kontrolu,
                                        korumada_mi, korumayi_kaldir)

EMPTY_DOCUMENT = "izolasyon-test-belgesi-yok"  # klinik_bulgular'da satırı olmayan belge kimliği
results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def blocked(func):
    try:
        func()
    except IzolasyonHatasi:
        return True
    return False


def pilot_document(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT document_id, COUNT(*) FROM klinik_bulgular WHERE bilgi_turu IS NOT NULL "
                   "GROUP BY document_id ORDER BY COUNT(*) DESC LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row:
        return (bytes(row[0]).decode() if isinstance(row[0], (bytes, bytearray)) else row[0]), row[1]
    cursor = conn.cursor()
    cursor.execute("SELECT document_id FROM pdf_belgeleri ORDER BY document_id LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    return (bytes(row[0]).decode() if isinstance(row[0], (bytes, bytearray)) else row[0]), 0


def visible_elsewhere(document_id):
    from src.database import get_connection

    other = get_connection()
    try:
        cursor = other.cursor()
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL",
                       (document_id,))
        n = cursor.fetchone()[0]
        cursor.close()
        other.rollback()
        return n
    finally:
        other.close()


def main():
    from src.database import get_connection

    start = gercek_pilot_durumu()
    probe = get_connection()
    document_id, real_rows = pilot_document(probe)
    probe.close()

    # 1) Ön koşullar: hiçbir şey silinmeden reddedilir.
    conn = get_connection()
    try:
        conn.autocommit = True
        check("autocommit açık -> reddedilir", True, blocked(lambda: gercek_pilotu_ayir(conn, document_id)))
        conn.autocommit = False
        conn.rollback()
        check("transaction yok -> reddedilir", True, blocked(lambda: gercek_pilotu_ayir(conn, document_id)))
        check("reddedilen çağrılar koruma kurmadı", False, korumada_mi(conn))
    finally:
        conn.rollback()
        conn.close()
    check("reddedilen çağrılar satır silmedi (ayrı bağlantı)", start, gercek_pilot_durumu())

    # 2a) Koruma: yasaklı işlemler. GÜVENLİ sınama: gerçek satırı olmayan bir belge kimliği (0 satır silinir) ve
    #     var olmayan tablo / şema adları; koruma bir yerde çalışmasa bile gerçek veri silinmiş olmaz.
    conn = get_connection()
    early_cursor = conn.cursor()  # silmeden önce açılmış cursor da korumada olmalı
    conn.start_transaction()
    try:
        check("boş belge: 0 satır silindi", 0, gercek_pilotu_ayir(conn, EMPTY_DOCUMENT))
        check("koruma kuruldu", True, korumada_mi(conn))

        def run(sql, cursor=None):
            def go():
                cur = cursor or conn.cursor()
                cur.execute(sql)
                if cur.with_rows:
                    cur.fetchall()
            return go

        forbidden = {
            "commit()": conn.commit,
            "start_transaction()": conn.start_transaction,
            "autocommit = True": lambda: setattr(conn, "autocommit", True),
            "CREATE TEMPORARY TABLE": run("CREATE TEMPORARY TABLE izolasyon_test_yok (x INT)"),
            "CREATE VIEW": run("CREATE OR REPLACE VIEW izolasyon_sema_yok.v AS SELECT 1 AS x"),
            "DROP VIEW": run("DROP VIEW IF EXISTS izolasyon_sema_yok.v"),
            "ALTER TABLE": run("ALTER TABLE izolasyon_test_yok COMMENT = 'x'"),
            "TRUNCATE": run("TRUNCATE TABLE izolasyon_test_yok"),
            "START TRANSACTION (SQL)": run("START TRANSACTION"),
            "BEGIN": run("BEGIN"),
            "COMMIT (SQL)": run("COMMIT"),
            "commit (küçük harf, yorumlu)": run("/* yorum */ -- satır\n commit"),
            "SET autocommit = 1": run("SET autocommit = 1"),
            "SET @@SESSION.autocommit": run("SET @@SESSION.autocommit = 1"),
            "LOCK TABLES": run("LOCK TABLES izolasyon_test_yok READ"),
            "erken cursor'dan DROP": run("DROP VIEW IF EXISTS izolasyon_sema_yok.v", early_cursor),
            "dictionary cursor'dan CREATE": run("CREATE OR REPLACE VIEW izolasyon_sema_yok.v AS SELECT 1",
                                                conn.cursor(dictionary=True)),
        }
        for label, func in forbidden.items():
            check(f"engellendi: {label}", True, blocked(func))
            check(f"engellendi: {label} -> transaction hâlâ açık", True, conn.in_transaction)
        allowed_cursor = conn.cursor()
        allowed_cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE id < 0")
        allowed_cursor.fetchall()
        allowed_cursor.execute("SAVEPOINT izolasyon_sp")
        allowed_cursor.execute("UPDATE klinik_bulgular SET review_status = review_status WHERE id < 0")
        allowed_cursor.execute("ROLLBACK TO SAVEPOINT izolasyon_sp")
        check("serbest: SELECT / SAVEPOINT / UPDATE / ROLLBACK TO SAVEPOINT", True, conn.in_transaction)
        allowed_cursor.close()
    finally:
        conn.rollback()
        korumayi_kaldir(conn)
        early_cursor.close()
        conn.close()
    check("koruma kaldırıldı", False, korumada_mi(conn))
    check("yasaklı işlem sınaması gerçek pilot satırlarına dokunmadı (ayrı bağlantı)", start, gercek_pilot_durumu())

    # 2b) Gerçek belge: silme yalnız bu transaction'da görünür; ROLLBACK geri getirir.
    conn = get_connection()
    conn.start_transaction()
    try:
        removed = gercek_pilotu_ayir(conn, document_id)
        check("silinen = belgenin gerçek pilot satırları", real_rows, removed)
        check("silme başka bağlantıdan görünmüyor (commit yok)", real_rows, visible_elsewhere(document_id))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL",
                       (document_id,))
        check("bu bağlantıda silinmiş görünür", 0, cursor.fetchone()[0])
        cursor.close()
    finally:
        conn.rollback()
        korumayi_kaldir(conn)
        conn.close()
    check("ROLLBACK sonrası satırlar geri geldi (ayrı bağlantı)", real_rows, visible_elsewhere(document_id))
    check("güvenlik kontrolü geçer (başlangıçla aynı)", start, guvenlik_kontrolu(start, "test_pilot_izolasyon"))

    # 3) Güvenlik kontrolü farkı yakalar (veriye dokunmadan: farklı başlangıç durumu verilir).
    wrong = dict(start, pilot_satir=start["pilot_satir"] + 1)
    try:
        guvenlik_kontrolu(wrong, "test_pilot_izolasyon (bilerek yanlış başlangıç)")
        message = ""
    except IzolasyonHatasi as exc:
        message = str(exc)
    check("güvenlik kontrolü fark varsa açık mesajla hata verir", True, message[:40],
          "GÜVENLİK KONTROLÜ BAŞARISIZ" in message and "başlangıç=" in message)

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<30}  {'Bulunan':<30}  Sonuç")
    print("-" * (width + 70))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:30]:<30}  {act[:30]:<30}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
