-- CardioRAG-Core: RAG doğrulama durumu ve taslak_notu ek bilgileri (yalnız VIEW; tablo / sütun / mevcut view değişmez).
-- Kurulum (kaynak veritabanında, kalite_core.sql'den sonra; tekrar çalıştırılabilir):
--     mysql --default-character-set=utf8mb4 <veritabani> < sql/rag_dogrulama_core.sql
--
-- Durumlar AYRI sütunlardadır ve birbirinin yerine geçmez:
--   insan_durumu            : klinik_bulgular.review_status (yalnız 'verified' insan onayıdır; anlamı değişmez).
--                             NULL = aday henüz içe aktarılmadı (yalnız otomatik doğrulama sonucu var).
--   otomatik_deterministik  : kalite_isaretleri, kontrol_surumu 'rag-dogrulama-*', kontrol_kodu 'rag_det_ozet'
--                             (gecti / belirsiz / basarisiz). Klinik ya da semantik doğrulama DEĞİLDİR.
--   semantik_karar          : kalite_isaretleri, kontrol_surumu 'rag-semantik-*', kontrol_kodu 'rag_sem_ozet'
--                             (destekleniyor / kismen_destekleniyor / kaynakla_celisiyor / ...). İnsan doğrulaması DEĞİLDİR.
--                             NULL = semantik kontrol YAPILMADI (tamamlanmamış semantik sonuç yazılmaz).
-- Güncel aday: her yazım bir parti (grup_id) eklenir, eski partiler silinmez. Satırlar belge başına EN SON deterministik
-- partiden (güncel aday dosyası: aday_dosyasi / aday_dosyasi_sha256) gelir; yeniden çıkarımda artık bulunmayan adayların
-- eski sonuçları görünmez. Semantik karar yalnız aynı satir_hash'e (birebir aynı içerik) ve en son semantik partiden bağlanır.
-- Otomatik doğrulama sonuçları kalite_calistirmalari'na yazılmaz; v_kalite_ozet / v_kalite_bulgu (data_quality) aynı kalır.

CREATE OR REPLACE VIEW v_rag_dogrulama_durumu AS
SELECT
    d.satir_hash,
    d.belge_kimligi,
    d.otomatik_deterministik,
    d.deterministik_surum,
    d.deterministik_isaret_sayisi,
    d.parti                                                                              AS deterministik_parti,
    d.aday_dosyasi,
    d.aday_dosyasi_sha256,
    s.semantik_karar,
    s.semantik_surum,
    s.parti                                                                              AS semantik_parti,
    b.id                                                                                 AS klinik_bulgu_id,
    b.review_status                                                                      AS insan_durumu,
    (b.review_status = 'verified')                                                       AS insan_onayli
FROM (
    SELECT q.satir_hash, p.belge AS belge_kimligi, q.grup_id AS parti,
        MAX(CASE WHEN q.kontrol_kodu = 'rag_det_ozet'
                 THEN JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.genel_sonuc')) END)     AS otomatik_deterministik,
        MAX(CASE WHEN q.kontrol_kodu = 'rag_det_ozet' THEN q.kontrol_surumu END)        AS deterministik_surum,
        SUM(q.kontrol_kodu LIKE 'rag\_det:%')                                            AS deterministik_isaret_sayisi,
        MAX(CASE WHEN q.kontrol_kodu = 'rag_det_ozet'
                 THEN JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.kaynak.aday_dosyasi')) END) AS aday_dosyasi,
        MAX(CASE WHEN q.kontrol_kodu = 'rag_det_ozet'
                 THEN JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.kaynak.aday_dosyasi_sha256')) END) AS aday_dosyasi_sha256
    FROM kalite_isaretleri AS q
    JOIN (SELECT JSON_UNQUOTE(JSON_EXTRACT(x.ayrinti, '$.belge_kimligi')) AS belge, MAX(x.grup_id) AS parti
          FROM kalite_isaretleri AS x
          WHERE x.kontrol_surumu LIKE 'rag-dogrulama-%' AND x.kontrol_kodu = 'rag_det_ozet' AND x.klinik_bulgu_id IS NULL
          GROUP BY JSON_UNQUOTE(JSON_EXTRACT(x.ayrinti, '$.belge_kimligi'))) AS p
      ON p.parti = q.grup_id AND p.belge = JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.belge_kimligi'))
    WHERE q.kontrol_surumu LIKE 'rag-dogrulama-%' AND q.klinik_bulgu_id IS NULL
    GROUP BY q.satir_hash, p.belge, q.grup_id
) AS d
LEFT JOIN (
    SELECT q.satir_hash, q.grup_id AS parti,
        MAX(JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.karar')))                          AS semantik_karar,
        MAX(q.kontrol_surumu)                                                            AS semantik_surum
    FROM kalite_isaretleri AS q
    JOIN (SELECT y.satir_hash, MAX(y.grup_id) AS parti
          FROM kalite_isaretleri AS y
          WHERE y.kontrol_surumu LIKE 'rag-semantik-%' AND y.kontrol_kodu = 'rag_sem_ozet' AND y.klinik_bulgu_id IS NULL
          GROUP BY y.satir_hash) AS m ON m.satir_hash = q.satir_hash AND m.parti = q.grup_id
    WHERE q.kontrol_surumu LIKE 'rag-semantik-%' AND q.kontrol_kodu = 'rag_sem_ozet' AND q.klinik_bulgu_id IS NULL
    GROUP BY q.satir_hash, q.grup_id
) AS s ON s.satir_hash = d.satir_hash
LEFT JOIN klinik_bulgular AS b ON b.satir_hash = d.satir_hash;


-- RAG kayıtlarının taslak_notu'ndaki sunum bilgileri (yalnız JSON geçerliyse; anahtar yoksa NULL).
CREATE OR REPLACE VIEW v_rag_ek_bilgi AS
SELECT
    b.id                                                                                  AS bulgu_id,
    b.document_id,
    b.review_status,
    b.kanit_turu,
    b.etki_turu,
    b.unit,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kaynak_calisma.etiket')), 'null')        AS calisma_kimligi,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kaynak_calisma.ad')), 'null')            AS calisma_adi,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kaynak_calisma.yil')), 'null')           AS calisma_yili,
    JSON_EXTRACT(b.notu, '$.kaynak_calisma.kaynakca_no')                                  AS kaynakca_no,
    JSON_EXTRACT(b.notu, '$.kaynak_calisma.ozellikler')                                   AS calisma_ozellikleri,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.alt_grup.etiket')), 'null')              AS alt_grup,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.alt_grup.acilim')), 'null')              AS alt_grup_acilimi,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kol.etiket')), 'null')                   AS tedavi_kolu,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kol.rol')), 'null')                      AS tedavi_kolu_rolu,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.olcu')), 'null')                         AS olcu_turu,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.istatistik')), 'null')                   AS istatistik_turu,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.oneri_sinifi')), 'null')                 AS oneri_sinifi,
    COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.kanit_duzeyi')), 'null'),
             NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.tablo_iliskisi.kanit_duzeyi')), 'null')) AS kanit_duzeyi,
    CASE WHEN JSON_EXTRACT(b.notu, '$.kanit_duzeyi') IS NOT NULL THEN 'oneri_tablosu'
         WHEN JSON_EXTRACT(b.notu, '$.tablo_iliskisi.kanit_duzeyi') IS NOT NULL THEN 'oneriye_bagli'
    END                                                                                   AS kanit_duzeyi_kaynagi,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.tablo_iliskisi.oneri_metni')), 'null')   AS oneri_metni,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.oneri_on_kosulu')), 'null')              AS oneri_on_kosulu,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.alt_bolum')), 'null')                    AS alt_bolum,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.tahmin_modeli')), 'null')                AS tahmin_modeli,
    NULLIF(JSON_UNQUOTE(JSON_EXTRACT(b.notu, '$.model')), 'null')                        AS cikarim_modeli
FROM (SELECT k.*, CASE WHEN JSON_VALID(k.taslak_notu) THEN k.taslak_notu END AS notu
      FROM klinik_bulgular AS k WHERE k.cikarma_yontemi = 'rag') AS b;
