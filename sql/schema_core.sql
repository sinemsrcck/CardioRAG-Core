-- CardioRAG-Core yeni tabloları (kardiyo_tam_metin, MySQL 8.4).
-- Tekrar çalıştırılabilir: yalnızca CREATE TABLE IF NOT EXISTS.
-- Hoca tablolarına (icd10_who, makaleler, semptom_iliskileri, sonuclar,
-- tani_tedavi, tkd_kurallari) bu dosyada dokunulmaz. makaleler'e eklenen
-- sütunlar src/database.py içinde information_schema kontrolüyle eklenir.
--
-- Foreign key politikası:
--   CASCADE  : sahiplik ilişkisi (belge -> sayfa/chunk, chunk -> embedding,
--              sorgu -> sorgu kaynakları).
--   RESTRICT : kanıt/denetim kaydı. Bir RAG sorgusunda ya da değerlendirmede
--              kullanılmış chunk veya belge silinemez; geçmiş kaybolmaz.


-- 1) PDF belgeleri. document_id = PDF dosyasının SHA-256 hash'i.
CREATE TABLE IF NOT EXISTS pdf_belgeleri (
    document_id           CHAR(64)      NOT NULL,
    filename              VARCHAR(255)  NOT NULL,
    page_count            INT UNSIGNED  NOT NULL,
    processing_version    VARCHAR(255)  NOT NULL,
    durum                 ENUM('tamamlandi', 'hatali') NOT NULL,
    islenen_sayfa_sayisi  INT UNSIGNED  NOT NULL DEFAULT 0,
    -- Son işlemenin hata mesajı (durum = 'hatali' ise). Mevcut kurulumlara
    -- src/database.py CORE_COLUMN_MIGRATIONS ile eklenir.
    hata_mesaji           TEXT          NULL,
    olusturulma_zamani    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    guncellenme_zamani    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (document_id),
    KEY idx_pdf_belgeleri_filename (filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 2) Her PDF'in her sayfasının işlenme kaydı.
CREATE TABLE IF NOT EXISTS pdf_isleme_durumu (
    document_id     CHAR(64)      NOT NULL,
    page_number     INT UNSIGNED  NOT NULL,
    ham_karakter    INT UNSIGNED  NOT NULL DEFAULT 0,
    temiz_karakter  INT UNSIGNED  NOT NULL DEFAULT 0,
    durum           ENUM('islendi', 'bos_sayfa', 'kaynakca_silindi',
                         'kismen_kaynakca', 'hata') NOT NULL,
    hata_mesaji     TEXT          NULL,
    islenme_zamani  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (document_id, page_number),
    CONSTRAINT fk_isleme_durumu_belge
        FOREIGN KEY (document_id) REFERENCES pdf_belgeleri (document_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 3) src/chunking.py çıktısı (en uzun chunk_id ~155 karakter).
CREATE TABLE IF NOT EXISTS pdf_chunks (
    chunk_id            VARCHAR(255)  NOT NULL,
    document_id         CHAR(64)      NOT NULL,
    page_number         INT UNSIGNED  NOT NULL,
    chunk_index         INT UNSIGNED  NOT NULL,
    text                MEDIUMTEXT    NOT NULL,
    text_sha256         CHAR(64)      NOT NULL,
    char_count          INT UNSIGNED  NOT NULL,
    processing_version  VARCHAR(255)  NOT NULL,
    chunk_size          INT UNSIGNED  NOT NULL,
    chunk_overlap       INT UNSIGNED  NOT NULL,
    olusturulma_zamani  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id),
    KEY idx_pdf_chunks_document (document_id),
    KEY idx_pdf_chunks_document_page (document_id, page_number),
    CONSTRAINT fk_chunks_belge
        FOREIGN KEY (document_id) REFERENCES pdf_belgeleri (document_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 4) Normalize edilmiş vektörün bytes hali (numpy float32 .tobytes()).
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id            VARCHAR(255)  NOT NULL,
    embedding_model     VARCHAR(255)  NOT NULL,
    dimension           INT UNSIGNED  NOT NULL,
    vector_dtype        VARCHAR(16)   NOT NULL DEFAULT 'float32',
    vector              BLOB          NOT NULL,
    olusturulma_zamani  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, embedding_model),
    CONSTRAINT fk_embeddings_chunk
        FOREIGN KEY (chunk_id) REFERENCES pdf_chunks (chunk_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    -- float32 vektörün byte uzunluğu boyutla tutarlı olmalı.
    CONSTRAINT chk_embeddings_float32_length
        CHECK (vector_dtype <> 'float32' OR OCTET_LENGTH(vector) = dimension * 4)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 5) Makalelerden çıkarılacak klinik bulgular (şimdilik boş).
-- makale_id için makaleler tablosuna FK tanımlanmadı: hoca tablosuna
-- kısıt bağlamamak için. Tipi/collation'ı aynıdır (init_schema kontrol eder).
-- query_id -> rag_sorgulari FK'si, rag_sorgulari bu dosyada daha sonra
-- oluşturulduğu için src/database.py CORE_CONSTRAINT_MIGRATIONS ile eklenir.
-- Yazım yalnızca src.database.save_clinical_findings ile (review_status='pending').
CREATE TABLE IF NOT EXISTS klinik_bulgular (
    id               INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    makale_id        VARCHAR(64)   NULL,
    document_id      CHAR(64)      NULL,
    chunk_id         VARCHAR(255)  NULL,
    study_name       VARCHAR(255)  NULL,
    hasta_grubu      VARCHAR(255)  NULL,
    hasta_sayisi     INT UNSIGNED  NULL,
    mudahale_tr      VARCHAR(255)  NULL,
    mudahale_en      VARCHAR(255)  NULL,
    mudahale_turu    VARCHAR(100)  NULL,
    comparator       VARCHAR(255)  NULL,
    doz              VARCHAR(255)  NULL,
    uygulama_yolu    VARCHAR(255)  NULL,
    sure             VARCHAR(255)  NULL,
    sonuc_olcutu_tr  VARCHAR(255)  NULL,
    outcome_type     VARCHAR(100)  NULL,
    etki_turu        VARCHAR(100)  NULL,
    deger            VARCHAR(255)  NULL,
    unit             VARCHAR(50)   NULL,
    ci95             VARCHAR(255)  NULL,
    p                VARCHAR(255)  NULL,
    yon              VARCHAR(50)   NULL,
    kanit_sayfa      INT UNSIGNED  NULL,
    kanit_cumle      TEXT          NULL,
    review_status    ENUM('pending', 'verified') NOT NULL DEFAULT 'pending',
    -- Sonradan eklenenler (mevcut kurulumlara CORE_COLUMN_MIGRATIONS ile):
    tani_tr            VARCHAR(255)  NULL,
    tani_en            VARCHAR(255)  NULL,
    icd10              VARCHAR(64)   NULL,
    kanit_turu         ENUM('metin', 'tablo') NULL,
    kanit_sayfa_bitis  INT UNSIGNED  NULL,
    birlestirme        ENUM('sayfa', 'ara_blok') NULL,
    cikarma_yontemi    ENUM('rag', 'referans_taslak', 'manuel') NULL,
    taslak_notu        TEXT          NULL,
    query_id           CHAR(36)      NULL,
    -- Tekrar yazımda çoğaltmayı önler: kanıt + alanların SHA-256'sı.
    satir_hash         CHAR(64)      NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_klinik_bulgular_satir_hash (satir_hash),
    KEY idx_klinik_bulgular_makale (makale_id),
    CONSTRAINT fk_klinik_bulgular_belge
        FOREIGN KEY (document_id) REFERENCES pdf_belgeleri (document_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_klinik_bulgular_chunk
        FOREIGN KEY (chunk_id) REFERENCES pdf_chunks (chunk_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 6) Her RAG sorgusu (src/rag.py answer_question çıktısı).
CREATE TABLE IF NOT EXISTS rag_sorgulari (
    query_id         CHAR(36)      NOT NULL,
    question         TEXT          NOT NULL,
    answer           MEDIUMTEXT    NULL,
    llm_model        VARCHAR(255)  NULL,
    embedding_model  VARCHAR(255)  NOT NULL,
    reranker_model   VARCHAR(255)  NULL,
    evidence_status  ENUM('sufficient', 'insufficient') NOT NULL,
    evidence_score   DOUBLE        NULL,
    guard_reason     TEXT          NULL,
    latency_ms       INT UNSIGNED  NOT NULL,
    created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Sonradan eklenenler (CORE_COLUMN_MIGRATIONS):
    rerank_method    VARCHAR(32)   NULL,
    citation_check   JSON          NULL,
    PRIMARY KEY (query_id),
    KEY idx_rag_sorgulari_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 7) Sorguda bulunan / LLM'ye gönderilen chunk'lar.
CREATE TABLE IF NOT EXISTS rag_sorgu_kaynaklari (
    query_id         CHAR(36)      NOT NULL,
    chunk_id         VARCHAR(255)  NOT NULL,
    retrieval_score  DOUBLE        NULL,
    rerank_score     DOUBLE        NULL,
    source_rank      INT UNSIGNED  NOT NULL,
    used_in_context  BOOLEAN       NOT NULL DEFAULT FALSE,
    PRIMARY KEY (query_id, chunk_id),
    KEY idx_rag_sorgu_kaynaklari_chunk (chunk_id),
    CONSTRAINT fk_sorgu_kaynaklari_sorgu
        FOREIGN KEY (query_id) REFERENCES rag_sorgulari (query_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_sorgu_kaynaklari_chunk
        FOREIGN KEY (chunk_id) REFERENCES pdf_chunks (chunk_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;


-- 8) Halüsinasyon değerlendirmesi. Otomatik değerlendirme hiçbir zaman
-- 'verified' olamaz (CHECK): insan onaylı sonuçlarla karışmaz.
-- kayit_turu='klinik_cikarim': klinik bulgu çıkarımında denetimi geçemeyen
-- satırlar (ham_cikti = LLM'in ham satırı). Bu kayıtlarda soru_tipi boş
-- olabilir; soru-cevap kayıtlarında zorunludur (CHECK).
CREATE TABLE IF NOT EXISTS halusinasyon_degerlendirme (
    id                   INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    query_id             CHAR(36)      NOT NULL,
    soru_tipi            ENUM('cevaplanabilir', 'cevaplanamaz', 'celiskili') NULL,
    claim_text           TEXT          NULL,
    supporting_chunk_id  VARCHAR(255)  NULL,
    citation_correct     BOOLEAN       NULL,
    claim_supported      ENUM('supported', 'partially_supported', 'not_supported') NULL,
    hata_turu            VARCHAR(100)  NULL,
    uygulanan_onlem      VARCHAR(255)  NULL,
    onceki_sonuc         TEXT          NULL,
    sonraki_sonuc        TEXT          NULL,
    duzeldi_mi           BOOLEAN       NULL,
    evaluation_method    ENUM('manual', 'automated') NOT NULL,
    review_status        ENUM('pending', 'verified') NOT NULL DEFAULT 'pending',
    evaluation_version   VARCHAR(100)  NULL,
    aciklama             TEXT          NULL,
    created_at           DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Sonradan eklenenler (CORE_COLUMN_MIGRATIONS):
    kayit_turu           ENUM('soru_cevap', 'klinik_cikarim') NOT NULL DEFAULT 'soru_cevap',
    ham_cikti            JSON          NULL,
    PRIMARY KEY (id),
    KEY idx_halusinasyon_review (evaluation_method, review_status),
    CONSTRAINT fk_halusinasyon_sorgu
        FOREIGN KEY (query_id) REFERENCES rag_sorgulari (query_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_halusinasyon_chunk
        FOREIGN KEY (supporting_chunk_id) REFERENCES pdf_chunks (chunk_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_halusinasyon_automated_not_verified
        CHECK (evaluation_method = 'manual' OR review_status = 'pending'),
    CONSTRAINT chk_halusinasyon_soru_tipi
        CHECK (kayit_turu <> 'soru_cevap' OR soru_tipi IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
