-- CardioRAG-Core: sunum görünümleri ve reddedilen çıkarımlar.
-- Kurulum (her veritabanında bir kez; tekrar çalıştırılabilir):
--     python -m src.sunum --kur
-- klinik_bulgular'ın yapısı değişmez; v_tani_tedavi / v_sonuclar ve
-- halusinasyon_degerlendirme'ye dokunulmaz. kaynak_bul procedure'ü src/sunum.py kurar.
-- Pilot kayıtlar için salt okunur v_pilot_bulgular dosyanın sonundadır.

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


-- ---------------------------------------------------------------------------
-- RAG soru-cevap sunumu (salt okunur). Tablolar Sinem'in yazıcılarından gelir
-- (rag_persistence.save_rag_bundle / sql_integration); burada yalnız okunur.
-- Değerler tabloda nasılsa öyle gösterilir; bir soruda birden fazla değerlendirme
-- satırı varsa farklı değerler GROUP_CONCAT(DISTINCT) ile yan yana yazılır.
-- halusinasyon_degerlendirme'den yalnız kayit_turu = 'soru_cevap' alınır.
-- ---------------------------------------------------------------------------

-- Soru başına bir satır. Toplamalar alt sorgularda (satır çoğalmaz).
-- durum = ham_cikti.$.generation_status; durum_aciklama ayrı, Türkçe açıklama sütunudur.
-- klinik_bulgu_sayisi / klinik_cikarim_red_sayisi: soruya bağlanmış klinik çıkarım (RAG dışı kullanım).
CREATE OR REPLACE VIEW v_rag_sorgular AS
SELECT
    q.query_id,
    q.question AS soru,
    h.soru_tipi,
    h.durum,
    CASE h.durum
        WHEN 'generated' THEN 'Cevap üretildi; atıf ve iddia kontrollerinden geçti'
        WHEN 'citation_validation_failed' THEN 'Atıf kontrolünden geçmediği için cevap gösterilmedi'
        WHEN 'claim_validation_failed' THEN 'İddia kontrolünden geçmedi ya da kontrol hata verdi; cevap gösterilmedi'
        WHEN 'insufficient_evidence' THEN 'Yeterli kanıt bulunamadı; cevap verilmedi'
        WHEN 'retrieval_only' THEN 'Yalnız kaynak araması yapıldı; LLM çağrılmadı'
        WHEN 'generation_failed' THEN 'LLM cevabı üretilemedi'
    END AS durum_aciklama,
    q.answer AS cevap,
    q.evidence_status, q.evidence_score, q.guard_reason, q.llm_model, q.created_at,
    COALESCE(k.bulunan_kaynak, 0) AS bulunan_kaynak,
    COALESCE(k.kullanilan_kaynak, 0) AS kullanilan_kaynak,
    k.kullanilan_kaynaklar,
    h.citation_correct,
    COALESCE(h.iddia_sayisi, 0) AS iddia_sayisi,
    COALESCE(h.iddia_supported, 0) AS iddia_supported,
    COALESCE(h.iddia_partially_supported, 0) AS iddia_partially_supported,
    COALESCE(h.iddia_not_supported, 0) AS iddia_not_supported,
    COALESCE(h.iddia_belirsiz, 0) AS iddia_belirsiz,
    h.hata_turu, h.uygulanan_onlem, h.duzeldi_mi,
    COALESCE(h.degerlendirme_satiri, 0) AS degerlendirme_satiri,
    COALESCE(kb.klinik_bulgu_sayisi, 0) AS klinik_bulgu_sayisi,
    COALESCE(kc.klinik_cikarim_red_sayisi, 0) AS klinik_cikarim_red_sayisi
FROM rag_sorgulari AS q
LEFT JOIN (
    SELECT
        s.query_id,
        COUNT(*) AS bulunan_kaynak,
        SUM(s.used_in_context) AS kullanilan_kaynak,
        GROUP_CONCAT(CASE WHEN s.used_in_context THEN CONCAT(b.filename, ' s.', c.page_number) END
                     ORDER BY s.source_rank SEPARATOR '; ') AS kullanilan_kaynaklar
    FROM rag_sorgu_kaynaklari AS s
    LEFT JOIN pdf_chunks AS c ON c.chunk_id = s.chunk_id
    LEFT JOIN pdf_belgeleri AS b ON b.document_id = c.document_id
    GROUP BY s.query_id
) AS k ON k.query_id = q.query_id
LEFT JOIN (
    SELECT
        d.query_id,
        COUNT(*) AS degerlendirme_satiri,
        GROUP_CONCAT(DISTINCT d.soru_tipi ORDER BY d.soru_tipi SEPARATOR ', ') AS soru_tipi,
        GROUP_CONCAT(DISTINCT d.durum ORDER BY d.durum SEPARATOR ', ') AS durum,
        GROUP_CONCAT(DISTINCT d.citation_correct ORDER BY d.citation_correct SEPARATOR ', ') AS citation_correct,
        SUM(d.iddia) AS iddia_sayisi,
        SUM(d.iddia = 1 AND d.claim_supported = 'supported') AS iddia_supported,
        SUM(d.iddia = 1 AND d.claim_supported = 'partially_supported') AS iddia_partially_supported,
        SUM(d.iddia = 1 AND d.claim_supported = 'not_supported') AS iddia_not_supported,
        SUM(d.iddia = 1 AND d.claim_supported IS NULL) AS iddia_belirsiz,
        GROUP_CONCAT(DISTINCT d.hata_turu ORDER BY d.hata_turu SEPARATOR ', ') AS hata_turu,
        GROUP_CONCAT(DISTINCT d.uygulanan_onlem ORDER BY d.uygulanan_onlem SEPARATOR ', ') AS uygulanan_onlem,
        GROUP_CONCAT(DISTINCT d.duzeldi_mi ORDER BY d.duzeldi_mi SEPARATOR ', ') AS duzeldi_mi
    FROM (
        SELECT
            x.query_id, x.soru_tipi, x.citation_correct, x.claim_supported,
            x.hata_turu, x.uygulanan_onlem, x.duzeldi_mi,
            CASE WHEN JSON_VALID(x.ham_cikti) THEN
                CASE WHEN JSON_TYPE(JSON_EXTRACT(x.ham_cikti, '$.generation_status')) = 'STRING'
                    THEN JSON_UNQUOTE(JSON_EXTRACT(x.ham_cikti, '$.generation_status')) END END AS durum,
            CASE WHEN x.claim_text IS NOT NULL THEN 1
                 WHEN JSON_VALID(x.ham_cikti) THEN
                    CASE WHEN JSON_TYPE(JSON_EXTRACT(x.ham_cikti, '$.evaluation.claim_index')) IN ('INTEGER', 'UNSIGNED INTEGER')
                        THEN 1 ELSE 0 END
                 ELSE 0 END AS iddia
        FROM halusinasyon_degerlendirme AS x
        WHERE x.kayit_turu = 'soru_cevap'
    ) AS d
    GROUP BY d.query_id
) AS h ON h.query_id = q.query_id
LEFT JOIN (
    SELECT query_id, COUNT(*) AS klinik_bulgu_sayisi
    FROM klinik_bulgular WHERE query_id IS NOT NULL GROUP BY query_id
) AS kb ON kb.query_id = q.query_id
LEFT JOIN (
    SELECT query_id, COUNT(*) AS klinik_cikarim_red_sayisi
    FROM halusinasyon_degerlendirme WHERE kayit_turu = 'klinik_cikarim' GROUP BY query_id
) AS kc ON kc.query_id = q.query_id;


-- Soru başına kaynaklar (bulunan bütün aday chunk'lar; used_in_context = LLM bağlamına girdi).
CREATE OR REPLACE VIEW v_rag_kaynaklar AS
SELECT
    s.query_id, q.question AS soru, s.source_rank, s.used_in_context,
    s.retrieval_score, s.rerank_score,
    b.filename AS dosya, c.page_number AS sayfa, s.chunk_id,
    LEFT(c.text, 200) AS chunk_baslangic
FROM rag_sorgu_kaynaklari AS s
JOIN rag_sorgulari AS q ON q.query_id = s.query_id
LEFT JOIN pdf_chunks AS c ON c.chunk_id = s.chunk_id
LEFT JOIN pdf_belgeleri AS b ON b.document_id = c.document_id;


-- İddia başına bir satır (yalnız soru_cevap iddia satırları; olay satırları v_rag_sorgular'da).
CREATE OR REPLACE VIEW v_rag_iddialar AS
SELECT
    x.query_id, q.question AS soru,
    CASE WHEN JSON_VALID(x.ham_cikti) THEN
        CASE WHEN JSON_TYPE(JSON_EXTRACT(x.ham_cikti, '$.evaluation.claim_index')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(x.ham_cikti, '$.evaluation.claim_index') AS UNSIGNED) END END AS iddia_no,
    x.claim_text, x.claim_supported, x.citation_correct,
    b.filename AS destekleyen_dosya, c.page_number AS destekleyen_sayfa, x.supporting_chunk_id,
    x.hata_turu, x.id AS degerlendirme_id
FROM halusinasyon_degerlendirme AS x
JOIN rag_sorgulari AS q ON q.query_id = x.query_id
LEFT JOIN pdf_chunks AS c ON c.chunk_id = x.supporting_chunk_id
LEFT JOIN pdf_belgeleri AS b ON b.document_id = c.document_id
WHERE x.kayit_turu = 'soru_cevap'
  AND (x.claim_text IS NOT NULL
       OR (JSON_VALID(x.ham_cikti)
           AND JSON_TYPE(JSON_EXTRACT(x.ham_cikti, '$.evaluation.claim_index')) IN ('INTEGER', 'UNSIGNED INTEGER')));


-- ---------------------------------------------------------------------------
-- Pilot kayıtları (klinik_bulgular.bilgi_turu dolu; src.pilot_clinical.import_records).
-- Salt okunur. Literatür bilgisi ve çalışmanın kendi bulguları AYNI listededir:
-- baglam yalnız sütundur, filtre değildir (ekip kararı). Türkçe *_tr sütunları
-- yalnız etikettir, asıl değerler yanında durur; eşlemesi olmayan değer olduğu gibi görünür.
-- taslak_notu alanları (pilot_tablo aktarımı) JSON_VALID korumalı okunur: alan yoksa,
-- JSON null ise ya da JSON bozuksa NULL döner (metin kayıtlarında hepsi NULL).
-- Kalite sütunları v_kalite_bulgu'dan gelir, yani yalnız EN SON kalite koşusunun
-- sürümü görünür: pilot işaretleri için son koşu python -m src.data_quality --pilot olmalı.
-- v3.1 notu: çok parçalı kanıt (kanit_parcalari) yapısı belli olunca ayrı bir VIEW
-- ya da buraya JSON_VALID korumalı yeni sütunlar eklenir, bugünkü sütunlar değişmez.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_pilot_bulgular AS
SELECT
    b.id AS bulgu_id, p.filename, m.baslik AS makale_basligi, b.kanit_sayfa,
    b.bilgi_turu,
    CASE b.bilgi_turu
        WHEN 'tani' THEN 'Tanı' WHEN 'semptom' THEN 'Semptom' WHEN 'teshis_yontemi' THEN 'Teşhis yöntemi'
        WHEN 'tani_kriteri' THEN 'Tanı kriteri' WHEN 'tedavi' THEN 'Tedavi' WHEN 'ilac' THEN 'İlaç'
        WHEN 'doz' THEN 'Doz' WHEN 'hasta_grubu' THEN 'Hasta grubu' WHEN 'klinik_sonuc' THEN 'Klinik sonuç'
        WHEN 'olcum' THEN 'Ölçüm' WHEN 'diger' THEN 'Diğer' ELSE b.bilgi_turu
    END AS bilgi_turu_tr,
    b.baglam,
    CASE b.baglam
        WHEN 'study_finding' THEN 'Çalışmanın bulgusu' WHEN 'study_method' THEN 'Çalışmanın yöntemi'
        WHEN 'exclusion_criterion' THEN 'Dışlama kriteri' WHEN 'literature_background' THEN 'Literatür bilgisi'
        WHEN 'recommendation' THEN 'Öneri' ELSE b.baglam
    END AS baglam_tr,
    b.kanit_turu,
    CASE b.kanit_turu WHEN 'metin' THEN 'Metin' WHEN 'tablo' THEN 'Tablo' ELSE b.kanit_turu END AS kanit_turu_tr,
    b.tablo_no, b.satir_no, b.alt_bolum, b.analiz,
    b.klinik_bilgi_en,
    b.sonuc_olcutu_tr AS degisken,
    b.mudahale_en AS ilac, b.mudahale_turu,
    b.hasta_grubu, b.grup_n, b.comparator, b.comparator_n,
    b.deger, b.deger_bicimi, b.unit, b.etki_turu, b.ci95, b.p, b.outcome_type,
    b.kanit_cumle, b.review_status,
    COALESCE(k.hata_sayisi, 0) AS kalite_hata, COALESCE(k.uyari_sayisi, 0) AS kalite_uyari,
    COALESCE(k.bilgi_sayisi, 0) AS kalite_bilgi,
    CASE WHEN k.hata_sayisi > 0 THEN 'hata' WHEN k.uyari_sayisi > 0 THEN 'uyari'
         WHEN k.bilgi_sayisi > 0 THEN 'bilgi' END AS kalite_en_yuksek,
    k.kontroller AS kalite_kontrolleri
FROM (
    SELECT
        x.*,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.tablo_no')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(x.taslak_notu, '$.tablo_no') AS UNSIGNED) END END AS tablo_no,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.satir_no')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(x.taslak_notu, '$.satir_no') AS UNSIGNED) END END AS satir_no,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.alt_bolum')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.alt_bolum')) END END AS alt_bolum,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.analiz')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.analiz')) END END AS analiz,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.grup_n')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(x.taslak_notu, '$.grup_n') AS UNSIGNED) END END AS grup_n,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.comparator_n')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(x.taslak_notu, '$.comparator_n') AS UNSIGNED) END END AS comparator_n,
        CASE WHEN JSON_VALID(x.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(x.taslak_notu, '$.deger_bicimi')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(x.taslak_notu, '$.deger_bicimi')) END END AS deger_bicimi
    FROM klinik_bulgular AS x
    WHERE x.bilgi_turu IS NOT NULL
) AS b
LEFT JOIN pdf_belgeleri AS p ON p.document_id = b.document_id
LEFT JOIN makaleler AS m ON m.document_id = b.document_id
LEFT JOIN v_kalite_bulgu AS k ON k.klinik_bulgu_id = b.id
ORDER BY p.filename, b.kanit_sayfa, b.tablo_no, b.id;
