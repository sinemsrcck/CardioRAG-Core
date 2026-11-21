"""
Veritabanı testleri için pilot veri izolasyonu (yalnız evaluation/ testleri kullanır).

Gerçek pilot verisi (bilgi_turu dolu satırlar, ör. TK-cardiovascular36-2-1.pdf'in 243 satırı)
veritabanındayken testlerin aynı kayıtları yeniden yazması INSERT IGNORE (satir_hash) ile atlanır
ve sayımlar tutmaz; --pilot kalite koşusu da gerçek satırları test satırlarıyla karıştırır.

gercek_pilotu_ayir(), testin KENDİ açık transaction'ı içinde o belgenin gerçek pilot satırlarını
siler. Böylece test, veritabanında gerçek pilot verisi olsa da olmasa da aynı başlangıç durumunu
görür. Silme commit edilmez: test sonundaki ROLLBACK satırları geri getirir (kalite_isaretleri
bağlı işaretleri ON DELETE CASCADE ile aynı transaction'da gider ve ROLLBACK'le geri gelir).
Diğer bağlantılar silmeyi görmez. Transaction açık değilse hiçbir şey silinmez, hata verilir.
"""


class IzolasyonHatasi(RuntimeError):
    pass


def gercek_pilotu_ayir(conn, document_id):
    """Açık transaction içinde belgenin pilot satırlarını (bilgi_turu dolu) siler; silinen sayısını döndürür."""
    if not conn.in_transaction:
        raise IzolasyonHatasi("gercek_pilotu_ayir yalnız açık bir transaction içinde çağrılabilir (sonda ROLLBACK).")
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL", (document_id,))
        removed = cursor.rowcount
        cursor.execute("SELECT COUNT(*) FROM klinik_bulgular WHERE document_id = %s AND bilgi_turu IS NOT NULL",
                       (document_id,))
        if cursor.fetchone()[0]:
            raise IzolasyonHatasi("gerçek pilot satırları transaction içinde ayrılamadı")
        return removed
    finally:
        cursor.close()
