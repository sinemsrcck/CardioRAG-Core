"""
Veritabanı testleri için pilot veri izolasyonu (yalnız evaluation/ testleri kullanır).

Gerçek pilot verisi (bilgi_turu dolu satırlar, ör. TK-cardiovascular36-2-1.pdf'in 243 satırı)
veritabanındayken testlerin aynı kayıtları yeniden yazması INSERT IGNORE (satir_hash) ile atlanır
ve sayımlar tutmaz; --pilot kalite koşusu da gerçek satırları test satırlarıyla karıştırır.

gercek_pilotu_ayir(), testin KENDİ açık transaction'ı içinde o belgenin gerçek pilot satırlarını
siler; test sonundaki ROLLBACK satırları geri getirir (kalite_isaretleri bağlı işaretleri
ON DELETE CASCADE ile aynı transaction'da gider ve ROLLBACK'le geri gelir).

Silme yalnız ROLLBACK ile biterse güvenlidir. Bu yüzden:
1. Silmeden önce: bağlantıda autocommit kapalı (istemci ve sunucu @@autocommit) ve açık bir
   transaction var mı, doğrulanır; değilse hiçbir şey silinmez, hata verilir.
2. Silmeden sonra bağlantı KORUMAYA alınır (korumayi_kaldir'a kadar): commit(), autocommit
   açma, start_transaction() ve örtük commit yapan her ifade (CREATE / DROP / ALTER / TRUNCATE /
   RENAME, BEGIN / START TRANSACTION / COMMIT, SET autocommit, LOCK / UNLOCK TABLES, GRANT /
   REVOKE, ANALYZE / OPTIMIZE / REPAIR / CHECK TABLE, LOAD, FLUSH, RESET, XA …) çalışmadan önce
   IzolasyonHatasi verir. Bütün cursor'lar (silmeden önce açılmışlar da) sorguyu bağlantının
   cmd_query'sinden geçirdiği için koruma hepsini kapsar; src/ fonksiyonlarına aynı bağlantı
   verilse bile commit edemezler. ROLLBACK serbesttir.
3. Test sonunda (finally, ROLLBACK'ten sonra) guvenlik_kontrolu() AYRI bir bağlantıdan gerçek
   pilot satırlarını (sayı, id aralığı, id+satir_hash parmak izi, bağlı kalite işaretleri)
   başlangıçla karşılaştırır; farklıysa açık bir mesajla IzolasyonHatasi verir.
Testlerde şema kurulumu (ensure_schema / init_schema) ve bilerek commit eden alt testler silme
yapılan bağlantıda değil, ayrı bağlantıda ve silmeden ÖNCE çalışır.
"""

import re

# MySQL'de örtük commit yapan ifadeler (ilk anahtar sözcük; baştaki yorumlar atlanır).
IMPLICIT_COMMIT = re.compile(
    r"^(?:CREATE|DROP|ALTER|TRUNCATE|RENAME|GRANT|REVOKE|BEGIN|START|COMMIT|LOCK|UNLOCK|ANALYZE|OPTIMIZE|"
    r"REPAIR|CHECK|LOAD|FLUSH|RESET|CACHE|INSTALL|UNINSTALL|XA|IMPORT)\b"
    r"|^SET\s+(?:(?:SESSION|GLOBAL|LOCAL|PERSIST)\s+|@@(?:SESSION\.|GLOBAL\.|LOCAL\.)?)?AUTOCOMMIT\b",
    re.IGNORECASE)
LEADING_COMMENTS = re.compile(r"^(?:\s+|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)*", re.DOTALL)

PILOT_STATE_SQL = (
    "SELECT COUNT(*), MIN(id), MAX(id), "
    "COALESCE(SUM(CRC32(CONCAT_WS('|', id, satir_hash, review_status))), 0) "
    "FROM klinik_bulgular WHERE bilgi_turu IS NOT NULL")
PILOT_FLAGS_SQL = ("SELECT COUNT(*) FROM kalite_isaretleri AS q JOIN klinik_bulgular AS b "
                   "ON b.id = q.klinik_bulgu_id WHERE b.bilgi_turu IS NOT NULL")


class IzolasyonHatasi(RuntimeError):
    pass


def _statement_text(query):
    if isinstance(query, (bytes, bytearray)):
        query = bytes(query).decode("utf-8", "replace")
    return LEADING_COMMENTS.sub("", str(query), count=1)


def _blocked(what):
    raise IzolasyonHatasi(
        f"{what}: gerçek pilot satırları bu bağlantının transaction'ında silinmiş durumda; commit / örtük commit "
        f"silmeyi kalıcı yapar. Bu işlem silmeden önce ve ayrı bir bağlantıda yapılmalı.")


def _guard(conn):
    base = type(conn)
    if getattr(base, "_pilot_izolasyon_korumasi", False):
        return

    class KorumaliBaglanti(base):
        _pilot_izolasyon_korumasi = True
        _korunan_sinif = base

        def commit(self):
            _blocked("commit()")

        def start_transaction(self, *args, **kwargs):
            _blocked("start_transaction() (açık transaction'ı örtük commit eder)")

        @property
        def autocommit(self):
            return base.autocommit.fget(self)

        @autocommit.setter
        def autocommit(self, value):
            if value:
                _blocked("autocommit = True")
            base.autocommit.fset(self, value)

        def cmd_query(self, query, *args, **kwargs):
            text = _statement_text(query)
            if IMPLICIT_COMMIT.match(text):
                _blocked(f"örtük commit yapan ifade ({text[:60]!r})")
            return base.cmd_query(self, query, *args, **kwargs)

    KorumaliBaglanti.__name__ = f"Korumali{base.__name__}"
    conn.__class__ = KorumaliBaglanti


def korumada_mi(conn):
    return bool(getattr(type(conn), "_pilot_izolasyon_korumasi", False))


def korumayi_kaldir(conn):
    """Korumayı kaldırır. Yalnız ROLLBACK'ten SONRA çağrılmalı (silme geri alınmış olmalı)."""
    if korumada_mi(conn):
        conn.__class__ = type(conn)._korunan_sinif


def gercek_pilot_durumu():
    """AYRI bir bağlantıdan gerçek pilot satırlarının durumu (sayı, id aralığı, parmak izi, bağlı işaretler)."""
    from src.database import get_connection

    other = get_connection()
    try:
        cursor = other.cursor()
        cursor.execute(PILOT_STATE_SQL)
        count, low, high, fingerprint = cursor.fetchone()
        cursor.execute(PILOT_FLAGS_SQL)
        flags = cursor.fetchone()[0]
        cursor.close()
        other.rollback()
        return {"pilot_satir": int(count), "id_araligi": (low, high), "parmak_izi": int(fingerprint),
                "pilot_kalite_isareti": int(flags)}
    finally:
        other.close()


def guvenlik_kontrolu(baslangic, test_adi):
    """Test sonunda (ROLLBACK'ten sonra) ayrı bağlantıdan: gerçek pilot satırları başlangıçla aynı mı."""
    simdi = gercek_pilot_durumu()
    if simdi != baslangic:
        raise IzolasyonHatasi(
            f"GÜVENLİK KONTROLÜ BAŞARISIZ ({test_adi}): gerçek pilot satırları test sonunda başlangıçla aynı değil! "
            f"başlangıç={baslangic} şimdi={simdi}. Silme kalıcı olmuş olabilir; veritabanını yedekten kontrol edin.")
    return simdi


def gercek_pilotu_ayir(conn, document_id):
    """Açık transaction içinde belgenin pilot satırlarını (bilgi_turu dolu) siler ve bağlantıyı korumaya alır.

    Silinen satır sayısını döndürür. Sonda: conn.rollback(); korumayi_kaldir(conn); guvenlik_kontrolu(...)."""
    if conn.autocommit:
        raise IzolasyonHatasi("gercek_pilotu_ayir: bağlantıda autocommit açık; silme hemen kalıcı olurdu.")
    if not conn.in_transaction:
        raise IzolasyonHatasi("gercek_pilotu_ayir yalnız açık bir transaction içinde çağrılabilir "
                              "(önce conn.start_transaction(), sonda ROLLBACK).")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT @@SESSION.autocommit")
        if int(cursor.fetchone()[0]) != 0:
            raise IzolasyonHatasi("gercek_pilotu_ayir: sunucuda @@autocommit açık; silme hemen kalıcı olurdu.")
        if not conn.in_transaction:
            raise IzolasyonHatasi("gercek_pilotu_ayir: sunucu transaction'ı açık değil.")
        _guard(conn)  # silmeden ÖNCE: silme ile koruma arasında commit fırsatı yok
        cursor.execute("DELETE FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL", (document_id,))
        removed = cursor.rowcount
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL",
                       (document_id,))
        if cursor.fetchone()[0]:
            raise IzolasyonHatasi("gerçek pilot satırları transaction içinde ayrılamadı")
        return removed
    finally:
        cursor.close()
