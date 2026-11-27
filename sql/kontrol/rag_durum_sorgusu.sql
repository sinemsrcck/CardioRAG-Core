-- RAG aday durumları (YALNIZ OKUMA). sql/rag_dogrulama_core.sql kurulduktan sonra kaynak veritabanında çalışır:
--     mysql --default-character-set=utf8mb4 kardiyo_tam_metin -e "source sql/kontrol/rag_durum_sorgusu.sql"
-- Satırlar yalnız GÜNCEL adaylar (belge başına en son deterministik parti = güncel aday dosyası). Adayların tam içeriği
-- aday dosyasındadır (JSON; aday_dosyasi / aday_dosyasi_sha256). Üç eksen birbirinden bağımsızdır:
--   deterministik : gecti / belirsiz / basarisiz
--   semantik      : destekleniyor / kismen_destekleniyor / kaynakla_celisiyor / yetersiz_kanit / belirsiz
--                   (NULL = semantik kontrol YAPILMADI; bütçe dışı ya da başarısız çağrı da NULL kalır)
--   insan         : klinik_bulgular.review_status           (NULL = aday; içe aktarılmadı, insan onayı YOK)
-- insan_onayli yalnız klinik_bulgular.review_status = 'verified' ise 1 olur; otomatik sonuçlardan türetilmez.

-- 1) Eksen dağılımı
SELECT COALESCE(otomatik_deterministik, '(deterministik ozet yok)') AS deterministik,
       COALESCE(semantik_karar, '(semantik kontrol yapilmadi)')    AS semantik,
       COALESCE(insan_durumu, '(aday: insan onayi yok)')           AS insan,
       COUNT(*) AS kayit
FROM v_rag_dogrulama_durumu
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 2) Aday dosyası ve parti başına sayılar (izlenebilirlik)
SELECT belge_kimligi, aday_dosyasi, aday_dosyasi_sha256, deterministik_parti, deterministik_surum, COUNT(*) AS aday,
       SUM(semantik_karar IS NOT NULL) AS semantik_kontrol_edilen
FROM v_rag_dogrulama_durumu
GROUP BY belge_kimligi, aday_dosyasi, aday_dosyasi_sha256, deterministik_parti, deterministik_surum
ORDER BY belge_kimligi;

-- 3) İnsan onayı olmayan adaylar: ayrı durum bayrakları + özet (belge, sayfa, iddia, değer)
SELECT d.satir_hash,
       (d.otomatik_deterministik <=> 'basarisiz')          AS deterministik_basarisiz,
       (d.otomatik_deterministik <=> 'belirsiz')           AS deterministik_belirsiz,
       (d.semantik_karar <=> 'kaynakla_celisiyor')         AS semantik_celiskili,
       (d.semantik_karar <=> 'kismen_destekleniyor')       AS semantik_kismen,
       (d.semantik_karar IS NULL)                          AS semantik_kontrol_yapilmadi,
       JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.semantik_dogrulama_gerekli')) AS semantik_gerekli,
       d.belge_kimligi,
       JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.sayfa'))           AS sayfa,
       JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.kanit_turu'))      AS kanit_turu,
       JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.klinik_bilgi_en')) AS klinik_bilgi_en,
       JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.deger'))           AS deger,
       d.aday_dosyasi
FROM v_rag_dogrulama_durumu AS d
JOIN kalite_isaretleri AS q
  ON q.satir_hash = d.satir_hash AND q.kontrol_kodu = 'rag_det_ozet' AND q.grup_id = d.deterministik_parti
     AND q.kontrol_surumu = d.deterministik_surum
WHERE COALESCE(d.insan_onayli, 0) = 0
ORDER BY d.belge_kimligi, CAST(JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.sayfa')) AS UNSIGNED), d.satir_hash;

-- 4) Güvenlik (beklenen 0): otomatik satır bir klinik_bulgular satırına bağlanmamış; parti kimliği boş değil
SELECT SUM(klinik_bulgu_id IS NOT NULL) AS bagli_otomatik_satir, SUM(grup_id = '') AS partisiz_satir
FROM kalite_isaretleri WHERE kontrol_surumu LIKE 'rag-%';
