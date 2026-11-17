-- CardioRAG-Core: hocanın formatında birleşik görünümler (salt okunur).
-- src/database.py init_schema, tablo geçişlerinden SONRA çalıştırır.
-- Hocanın tablolarına yazmaz; yalnızca okur.
--
-- kaynak          : 'hoca' (tani_tedavi / sonuclar) veya 'cardiorag' (klinik_bulgular)
-- review_status   : hocanın satırlarında NULL; bizim satırlarda pending/verified
-- cikarma_yontemi : hocanın satırlarında NULL; bizim satırlarda rag/referans_taslak/manuel


CREATE OR REPLACE VIEW v_tani_tedavi AS
SELECT
    t.makale_id, t.tani_tr, t.tani_en, t.icd10,
    t.tedavi_tr, t.tedavi_en, t.tedavi_turu,
    t.doz, t.uygulama_yolu, t.sure,
    t.kanit_sayfa, t.kanit_cumle,
    'hoca' AS kaynak,
    CAST(NULL AS CHAR(16)) AS review_status,
    CAST(NULL AS CHAR(32)) AS cikarma_yontemi
FROM tani_tedavi AS t
UNION ALL
SELECT
    k.makale_id, k.tani_tr, k.tani_en, k.icd10,
    k.mudahale_tr, k.mudahale_en, k.mudahale_turu,
    k.doz, k.uygulama_yolu, k.sure,
    k.kanit_sayfa, k.kanit_cumle,
    'cardiorag',
    k.review_status,
    k.cikarma_yontemi
FROM klinik_bulgular AS k;


-- Bizim satırlardan yalnızca bir sonuç ölçütü olanlar (sonuc_olcutu_tr dolu)
-- sonuclar biçimine girer.
CREATE OR REPLACE VIEW v_sonuclar AS
SELECT
    s.makale_id, s.olcut_tr, s.etki_turu, s.deger, s.ci95, s.p, s.yon,
    s.kanit_sayfa, s.kanit_cumle,
    'hoca' AS kaynak,
    CAST(NULL AS CHAR(16)) AS review_status,
    CAST(NULL AS CHAR(32)) AS cikarma_yontemi
FROM sonuclar AS s
UNION ALL
SELECT
    k.makale_id, k.sonuc_olcutu_tr, k.etki_turu, k.deger, k.ci95, k.p, k.yon,
    k.kanit_sayfa, k.kanit_cumle,
    'cardiorag',
    k.review_status,
    k.cikarma_yontemi
FROM klinik_bulgular AS k
WHERE k.sonuc_olcutu_tr IS NOT NULL AND k.sonuc_olcutu_tr <> '';
