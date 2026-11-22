-- =====================================================================
-- kardiyo_clinical_data: hoca view'larının anlık kopyası (fiziksel tablolar)
-- =====================================================================
-- Mimari:
--   kardiyo_tam_metin     : ana veritabanı. Bu betik ona yazmaz.
--   kardiyo_sunum         : 8 hoca view'ı. Bu betik yalnız OKUR; view'lar
--                           silinmez, değiştirilmez. Burada tablo oluşmaz,
--                           bu yüzden evaluation/test_sunum_hoca.py'deki
--                           "yalnız view" kontrolü geçerli kalır.
--   kardiyo_clinical_data : sunumda kullanılacak 8 fiziksel tablo
--                           (view'larla aynı adlar).
--
-- Ne yapar:
--   1. kardiyo_clinical_data yoksa oluşturur (IF NOT EXISTS).
--   2. Her hedef tablo için önce DROP TABLE IF EXISTS kardiyo_clinical_data.<ad>,
--      ardından view'ın o anki sonucunu CREATE TABLE ... AS SELECT ile
--      kardiyo_clinical_data'da aynı adlı tabloya kopyalar.
--
-- Tekrar çalıştırma:
--   Her çalıştırma 8 hedef tabloyu silip view'ların güncel sonucuyla yeniden
--   oluşturur (anlık kopya yenilenir). DROP yalnız kardiyo_clinical_data'daki
--   bu 8 tabloya uygulanır; kardiyo_tam_metin ve kardiyo_sunum'a DROP, DELETE,
--   UPDATE, INSERT veya TRUNCATE yok. Eski kopya korunmak istenirse betik
--   çalıştırılmadan önce yedeklenmelidir.
--   DROP + CREATE ifadeleri ayrı ayrı (otomatik COMMIT ile) çalışır; bir
--   CREATE başarısız olursa o tablo yok kalır, betik yeniden çalıştırılmalıdır.
--
-- Dikkat:
--   - Bu dosya bilerek sql/sunum_hoca/ klasörünün DIŞINDADIR;
--     python -m src.sunum_hoca --kur o klasördeki bütün *.sql dosyalarını
--     çalıştırır.
--   - Kopyalar view'ların o anki hâlidir; sonradan aktarılan veriyi içermez.
--   - Tablolarda satır sırası garanti değildir (view'lardaki ORDER BY tabloya
--     taşınmaz). Sıralı gösterim için ORDER BY kullanın.
--   - measurements'taki group_N sütunları, view'ın o anki sütunlarıyla aynıdır.
--
-- Gereken yetki: CREATE (veritabanı), kardiyo_clinical_data üzerinde CREATE,
-- DROP ve INSERT, kardiyo_sunum view'larında SELECT.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `kardiyo_clinical_data`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`measurements`;
CREATE TABLE `kardiyo_clinical_data`.`measurements`
    AS SELECT * FROM `kardiyo_sunum`.`measurements`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`measurements_by_group`;
CREATE TABLE `kardiyo_clinical_data`.`measurements_by_group`
    AS SELECT * FROM `kardiyo_sunum`.`measurements_by_group`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`clinical_outcomes`;
CREATE TABLE `kardiyo_clinical_data`.`clinical_outcomes`
    AS SELECT * FROM `kardiyo_sunum`.`clinical_outcomes`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`treatments`;
CREATE TABLE `kardiyo_clinical_data`.`treatments`
    AS SELECT * FROM `kardiyo_sunum`.`treatments`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`general_clinical_findings`;
CREATE TABLE `kardiyo_clinical_data`.`general_clinical_findings`
    AS SELECT * FROM `kardiyo_sunum`.`general_clinical_findings`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`diseases`;
CREATE TABLE `kardiyo_clinical_data`.`diseases`
    AS SELECT * FROM `kardiyo_sunum`.`diseases`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`diagnostics`;
CREATE TABLE `kardiyo_clinical_data`.`diagnostics`
    AS SELECT * FROM `kardiyo_sunum`.`diagnostics`;

DROP TABLE IF EXISTS `kardiyo_clinical_data`.`symptoms`;
CREATE TABLE `kardiyo_clinical_data`.`symptoms`
    AS SELECT * FROM `kardiyo_sunum`.`symptoms`;

-- ---------------------------------------------------------------------
-- Kontrol 1 (salt okunur): kardiyo_clinical_data'daki nesneler.
-- 8 satır, hepsi TABLE_TYPE = 'BASE TABLE' olmalı; CREATE_TIME bu çalıştırma.
-- ---------------------------------------------------------------------
SELECT TABLE_NAME, TABLE_TYPE, CREATE_TIME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'kardiyo_clinical_data'
ORDER BY TABLE_NAME;

-- ---------------------------------------------------------------------
-- Kontrol 2 (salt okunur): kardiyo_sunum'da hâlâ yalnız view var.
-- Beklenen: 0.
-- ---------------------------------------------------------------------
SELECT COUNT(*) AS sunum_base_tables
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'kardiyo_sunum'
  AND TABLE_TYPE <> 'VIEW';

-- ---------------------------------------------------------------------
-- Kontrol 3 (salt okunur): tablo satır sayısı ile kaynak view'ın şu anki
-- satır sayısı. Tablolar her çalıştırmada yeniden oluşturulduğundan
-- 8 satırın hepsinde ayni = 1 olmalı.
-- ---------------------------------------------------------------------
SELECT name, table_rows, view_rows, (table_rows = view_rows) AS ayni
FROM (
    SELECT 'measurements' AS name,
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`measurements`) AS table_rows,
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`measurements`) AS view_rows
    UNION ALL
    SELECT 'measurements_by_group',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`measurements_by_group`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`measurements_by_group`)
    UNION ALL
    SELECT 'clinical_outcomes',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`clinical_outcomes`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`clinical_outcomes`)
    UNION ALL
    SELECT 'treatments',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`treatments`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`treatments`)
    UNION ALL
    SELECT 'general_clinical_findings',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`general_clinical_findings`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`general_clinical_findings`)
    UNION ALL
    SELECT 'diseases',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`diseases`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`diseases`)
    UNION ALL
    SELECT 'diagnostics',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`diagnostics`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`diagnostics`)
    UNION ALL
    SELECT 'symptoms',
           (SELECT COUNT(*) FROM `kardiyo_clinical_data`.`symptoms`),
           (SELECT COUNT(*) FROM `kardiyo_sunum`.`symptoms`)
) AS counts;
