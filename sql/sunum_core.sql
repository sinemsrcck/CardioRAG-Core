-- CardioRAG-Core: sunum görünümleri ve reddedilen çıkarımlar.
-- Kurulum (her veritabanında bir kez; tekrar çalıştırılabilir):
--     python -m src.sunum --kur
-- klinik_bulgular'ın yapısı değişmez; v_tani_tedavi / v_sonuclar ve
-- halusinasyon_degerlendirme'ye dokunulmaz. kaynak_bul procedure'ü src/sunum.py kurar.

-- LLM öncesi ayrılan adaylar ve doğrulamadan geçemeyen bulgular (kayda girmeyenler).
CREATE TABLE IF NOT EXISTS cikarim_reddedilenler (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    satir_hash        CHAR(64)      NOT NULL,
    aday_id           VARCHAR(100)  NOT NULL,
    pipeline_version  VARCHAR(64)   NOT NULL,
    model             VARCHAR(100)  NOT NULL DEFAULT '',
    document_id       CHAR(64)      NULL,
    makale_id         VARCHAR(64)   NULL,
    dosya             VARCHAR(255)  NULL,
    sayfa             INT UNSIGNED  NULL,
    kaynak_turu       VARCHAR(20)   NULL,
    asama             ENUM('on_eleme', 'yanit_dogrulama', 'satir_olusturma', 'validate_row', 'save') NOT NULL,
    hata_turu         VARCHAR(64)   NOT NULL,
    alt_tur           VARCHAR(64)   NOT NULL DEFAULT '',
    aciklama          TEXT          NULL,
    bulgu             JSON          NULL,
    satir             JSON          NULL,
    aday_cumlesi      TEXT          NULL,
    kanit_cumle       TEXT          NULL,
    olusturma_zamani  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cikarim_red_hash (satir_hash),
    KEY idx_cikarim_red_hata (hata_turu),
    KEY idx_cikarim_red_surum (pipeline_version),
    KEY idx_cikarim_red_aday (aday_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- Hata türü -> halüsinasyon türü ve uygulanan önlem (src/sunum.py ONLEM_ESLEME'den yazılır).
CREATE TABLE IF NOT EXISTS hata_onlem (
    hata_turu          VARCHAR(64)   NOT NULL,
    halusinasyon_turu  VARCHAR(255)  NOT NULL,
    onlem              TEXT          NOT NULL,
    kural_yeri         VARCHAR(255)  NOT NULL,
    esleme_surumu      VARCHAR(32)   NOT NULL,
    PRIMARY KEY (hata_turu)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- klinik_bulgular + taslak_notu'dan okunan alanlar. Bozuk JSON VIEW'u düşürmez
-- (JSON_VALID önce kontrol edilir); JSON null değerleri NULL döner.
CREATE OR REPLACE VIEW v_bulgu_notu AS
SELECT
    x.*,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.extracted_fields.clinical_outcome')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.extracted_fields.clinical_outcome')) END END AS sonuc_en,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.aday_id')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.aday_id')) END END AS aday_id,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.aday_cumlesi')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.aday_cumlesi')) END END AS aday_cumlesi,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.pipeline_version')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.pipeline_version')) END END AS pipeline_version,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.model')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.model')) END END AS model,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.otomatik_dogrulama.insan_onayi')) = 'STRING'
        THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.otomatik_dogrulama.insan_onayi')) END END AS insan_onayi,
    CASE WHEN JSON_VALID(x.taslak_notu) THEN JSON_EXTRACT(x.taslak_notu, '$.otomatik_dogrulama.gecen_kontroller') END AS gecen_kontroller
FROM klinik_bulgular AS x;


-- Sunum: bulgu + PDF + makale başlığı + DOI + bütün klinik alanlar + kalite işaret sayıları.
CREATE OR REPLACE VIEW v_cardiorag_bulgular AS
SELECT
    b.id AS bulgu_id, b.makale_id, p.filename, m.baslik AS makale_basligi,
    COALESCE(NULLIF(m.baglanti, ''), CASE WHEN NULLIF(m.doi, '') IS NOT NULL THEN CONCAT('https://doi.org/', m.doi) END) AS doi_link,
    b.kanit_sayfa AS sayfa, b.kanit_turu,
    b.mudahale_tr, b.mudahale_en, b.mudahale_turu,
    COALESCE(NULLIF(b.tani_tr, ''), b.tani_en) AS tani, b.icd10,
    b.hasta_grubu, b.hasta_sayisi, b.comparator, b.doz, b.sure,
    COALESCE(NULLIF(b.sonuc_olcutu_tr, ''), b.sonuc_en) AS sonuc,
    CASE WHEN NULLIF(b.sonuc_olcutu_tr, '') IS NOT NULL THEN 'sonuc_olcutu_tr'
         WHEN b.sonuc_en IS NOT NULL THEN 'taslak_notu (İngilizce clinical_outcome, çevrilmedi)' END AS sonuc_kaynagi,
    b.etki_turu, b.deger, b.unit, b.ci95, b.p, b.outcome_type,
    b.review_status, b.cikarma_yontemi, b.kanit_cumle, b.aday_id,
    COALESCE(k.hata_sayisi, 0) AS kalite_hata, COALESCE(k.uyari_sayisi, 0) AS kalite_uyari,
    COALESCE(k.bilgi_sayisi, 0) AS kalite_bilgi, k.kontroller AS kalite_kontrolleri
FROM v_bulgu_notu AS b
LEFT JOIN pdf_belgeleri AS p ON p.document_id = b.document_id
LEFT JOIN makaleler AS m ON m.document_id = b.document_id
LEFT JOIN v_kalite_bulgu AS k ON k.klinik_bulgu_id = b.id;


-- Kaynak izleme: bulgu -> PDF, başlık, DOI, sayfa, chunk, kanıt ve aday cümlesi, kontroller, sürüm, model.
CREATE OR REPLACE VIEW v_bulgu_kaynak AS
SELECT
    b.id AS bulgu_id, b.makale_id, p.filename, m.baslik AS makale_basligi,
    COALESCE(NULLIF(m.baglanti, ''), CASE WHEN NULLIF(m.doi, '') IS NOT NULL THEN CONCAT('https://doi.org/', m.doi) END) AS doi_link,
    b.kanit_sayfa AS sayfa, b.chunk_id, c.page_number AS chunk_sayfasi, c.chunk_index,
    b.kanit_turu, b.kanit_cumle, b.aday_id, b.aday_cumlesi,
    COALESCE(NULLIF(b.mudahale_tr, ''), b.mudahale_en) AS mudahale, b.mudahale_turu,
    COALESCE(NULLIF(b.tani_tr, ''), b.tani_en) AS tani, b.hasta_grubu,
    COALESCE(NULLIF(b.sonuc_olcutu_tr, ''), b.sonuc_en) AS sonuc,
    b.gecen_kontroller, b.insan_onayi, b.pipeline_version, b.model,
    b.review_status, b.cikarma_yontemi
FROM v_bulgu_notu AS b
LEFT JOIN pdf_belgeleri AS p ON p.document_id = b.document_id
LEFT JOIN makaleler AS m ON m.document_id = b.document_id
LEFT JOIN pdf_chunks AS c ON c.chunk_id = b.chunk_id;


-- Halüsinasyon/hata önlemleri: hangi hata kaç kez yakalandı, hangi önlemle; örnek aday.
-- Eşlemesi olmayan kod gizlenmez ('eşleme yok').
CREATE OR REPLACE VIEW v_halusinasyon_onlemleri AS
SELECT
    g.pipeline_version, g.asama, g.hata_turu, NULLIF(g.alt_tur, '') AS alt_tur,
    COALESCE(o.halusinasyon_turu, 'eşleme yok') AS halusinasyon_turu,
    COALESCE(o.onlem, 'eşleme yok') AS uygulanan_onlem, o.kural_yeri,
    g.yakalanan, 'kayda girmedi' AS sonuc,
    e.aday_id AS ornek_aday, e.dosya AS ornek_dosya, e.sayfa AS ornek_sayfa,
    LEFT(COALESCE(e.kanit_cumle, e.aday_cumlesi), 300) AS ornek_cumle
FROM (
    SELECT pipeline_version, asama, hata_turu, alt_tur, COUNT(*) AS yakalanan, MIN(satir_hash) AS ornek_hash
    FROM cikarim_reddedilenler
    GROUP BY pipeline_version, asama, hata_turu, alt_tur
) AS g
JOIN cikarim_reddedilenler AS e ON e.satir_hash = g.ornek_hash
LEFT JOIN hata_onlem AS o ON o.hata_turu = COALESCE(NULLIF(g.alt_tur, ''), g.hata_turu);
