-- CardioRAG-Core: veri kalitesi işaretleri (türetilmiş veri).
-- Kurulum (her veritabanında bir kez; tekrar çalıştırılabilir):
--     python -m src.data_quality --kur
-- klinik_bulgular'a YAZMAZ; işaretler ayrı tabloda tutulur. Aynı kontrol
-- sürümüyle yeniden çalıştırınca o sürümün işaretleri silinip yeniden hesaplanır.

CREATE TABLE IF NOT EXISTS kalite_isaretleri (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    klinik_bulgu_id   INT UNSIGNED  NULL,
    satir_hash        CHAR(64)      NOT NULL,
    kontrol_kodu      VARCHAR(64)   NOT NULL,
    kategori          ENUM('tekrar', 'sayisal', 'kaynak', 'tutarsizlik') NOT NULL,
    seviye            ENUM('hata', 'uyari', 'bilgi') NOT NULL,
    grup_id           VARCHAR(40)   NOT NULL DEFAULT '',
    aciklama          TEXT          NULL,
    ayrinti           JSON          NULL,
    kontrol_surumu    VARCHAR(64)   NOT NULL,
    olusturma_zamani  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_kalite_isaret (satir_hash, kontrol_kodu, grup_id, kontrol_surumu),
    KEY idx_kalite_surum (kontrol_surumu),
    KEY idx_kalite_bulgu (klinik_bulgu_id),
    KEY idx_kalite_grup (grup_id),
    CONSTRAINT fk_kalite_isaret_bulgu
        FOREIGN KEY (klinik_bulgu_id) REFERENCES klinik_bulgular (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


CREATE TABLE IF NOT EXISTS kalite_calistirmalari (
    kontrol_surumu  VARCHAR(64)   NOT NULL,
    kapsam          VARCHAR(255)  NOT NULL,
    son_calisma     DATETIME      NOT NULL,
    satir_sayisi    INT UNSIGNED  NOT NULL,
    isaret_sayisi   INT UNSIGNED  NOT NULL,
    sayilar         JSON          NULL,
    PRIMARY KEY (kontrol_surumu)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- İşaret + bulgu + PDF adı; yalnızca en son çalıştırılan kontrol sürümü.
CREATE OR REPLACE VIEW v_kalite_ozet AS
SELECT
    q.klinik_bulgu_id, b.makale_id, p.filename, b.kanit_sayfa, b.kanit_turu,
    COALESCE(b.mudahale_en, b.mudahale_tr) AS mudahale,
    b.doz, b.deger, b.unit, b.hasta_grubu, b.review_status, b.cikarma_yontemi,
    q.kategori, q.seviye, q.kontrol_kodu, q.grup_id, q.aciklama, q.kontrol_surumu
FROM kalite_isaretleri AS q
JOIN klinik_bulgular AS b ON b.id = q.klinik_bulgu_id
LEFT JOIN pdf_belgeleri AS p ON p.document_id = b.document_id
WHERE q.kontrol_surumu = (
    SELECT c.kontrol_surumu FROM kalite_calistirmalari AS c
    ORDER BY c.son_calisma DESC, c.kontrol_surumu LIMIT 1
);


-- Bulgu başına işaret sayıları (en son sürüm).
CREATE OR REPLACE VIEW v_kalite_bulgu AS
SELECT
    o.klinik_bulgu_id, o.makale_id, o.filename, o.kanit_sayfa, o.mudahale, o.review_status,
    SUM(o.seviye = 'hata')  AS hata_sayisi,
    SUM(o.seviye = 'uyari') AS uyari_sayisi,
    SUM(o.seviye = 'bilgi') AS bilgi_sayisi,
    GROUP_CONCAT(DISTINCT o.kontrol_kodu ORDER BY o.kontrol_kodu SEPARATOR ', ') AS kontroller
FROM v_kalite_ozet AS o
GROUP BY o.klinik_bulgu_id, o.makale_id, o.filename, o.kanit_sayfa, o.mudahale, o.review_status;
