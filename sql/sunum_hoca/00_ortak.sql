-- Hoca sunum veritabanı: ortak taban view.
-- Kurulum: python -m src.sunum_hoca --kur  (dosyalar ada göre sırayla çalışır; tekrar çalıştırılabilir)
-- Yer tutucular kurulumda doldurulur, dosyada veritabanı adı yazılmaz:
--   {{KAYNAK_DB}} = .env MYSQL_DATABASE (klinik_bulgular, v_pilot_bulgular)
--   {{SUNUM_DB}}  = .env MYSQL_SUNUM_DATABASE, varsayılan kardiyo_sunum
-- Bölücü kuralı: satır sonunda ';' yalnız ifade sonunda; yorumlar tam satır '--'.
--
-- v_sunum_taban: bütün hoca view'larının ortak kaynağı. KAYNAK veritabanında durur (iç kullanım);
-- sunum veritabanında yalnız hocaya gösterilen view'lar kalır. Kaynaktaki tablolara ve diğer
-- view'lara dokunulmaz; yalnız bu view eklenir / yenilenir.
-- Kaynak: {{KAYNAK_DB}}.v_pilot_bulgular (python -m src.sunum --kur ile kurulur), yalnız
-- review_status = 'verified' satırlar. pilot_tablo'nun taslak_notu alanlarından v_pilot_bulgular'da
-- olmayanlar (sutun_rolu, etiket, se, se_bicimi, p_baglandigi_satir, tani_olcusu, regresyon_katsayisi) klinik_bulgular.taslak_notu'dan
-- JSON_VALID korumalı okunur (yalnız okuma; alan yoksa NULL).
-- Ortak kurallar burada BİR KEZ yazılır:
--   clinical_context  : hasta_grubu -> 'study population'; diğerleri baglam'dan
--                       (exclusion criterion, literature, study finding, study method, recommendation)
--   satir_adi         : tablo satırının gösterilen adı. klinik_bilgi_en (PDF'teki satır adı); etiket sütunlu
--                       satırda (taslak_notu.etiket, ör. "A-B") kanıttaki parça kısa olduğu için birleşik ad
--                       (degisken, ör. "Pairwise comparation A-B"). klinik_bilgi_en değişmez (kanıt kuralı).
--   bolum_oneki       : aynı tabloda aynı ad (COALESCE(ilac, degisken)) birden fazla bölümde (alt_bolum) geçiyorsa
--                       o satırın bölümü (ör. "delta hs Trop I (0-1)h ±20%" + "Non risc"); tekrar etmeyen adlarda NULL.
--   evidence          : metin satırında kanit_gosterim (çok parçalı kanıtta "[s.N] …" gösterimi; birleştirilmiş
--                       alıntı değil). Tablo satırında ham markdown satırı yerine kısa kaynak metni
--                       "Table <tablo_no> – [<bölüm> – ]<satır adı>"; <sup>x</sup> -> ^x, <br> ve ** atılır.
--                       Ham tablo satırı kaynak veritabanında (kanit_cumle) birebir duruyor.
--   satir_grup_karsilastirmasi : aynı tablo satırında (row_key) hasta grubu kaydı var mı (1/0). View yönlendirmesi
--                       tablo numarasına değil buna bakar: 1 -> measurements (grup karşılaştırması; satırın p
--                       sütunundaki etki ölçüsü effect sütununda), 0 -> clinical_outcomes (etki / tanısal performans).
--   source            : makale başlığı, yoksa PDF dosya adı
--   page              : "4" ya da sayfa aralığı "2–3"; sıralama için page_sort (sayı)
-- Hoca view'ları teknik sütun (bulgu_id, document_id, satir_hash, taslak_notu …) GÖSTERMEZ;
-- bu taban view'daki bulgu_id / tablo_no / satir_no yalnız birleştirme ve sıralama içindir.
CREATE OR REPLACE VIEW `{{KAYNAK_DB}}`.`v_sunum_taban` AS
SELECT
    x.*,
    CASE
        WHEN x.kanit_turu = 'tablo' AND x.tablo_no IS NOT NULL AND NULLIF(x.satir_adi, '') IS NOT NULL
            THEN CONCAT('Table ', x.tablo_no, ' – ', CONCAT_WS(' – ', x.bolum_oneki, x.satir_adi))
        ELSE x.kanit_gosterim
    END AS evidence,
    MAX(CASE WHEN x.kanit_turu = 'tablo' AND x.hasta_grubu IS NOT NULL THEN 1 ELSE 0 END)
        OVER (PARTITION BY x.row_key) AS satir_grup_karsilastirmasi
FROM (
    SELECT
        b.bulgu_id,
        b.filename,
        b.bilgi_turu,
        b.baglam,
        b.kanit_turu,
        b.tablo_no,
        b.satir_no,
        b.alt_bolum,
        b.analiz,
        b.klinik_bilgi_en,
        b.degisken,
        b.ilac,
        b.mudahale_turu,
        b.hasta_grubu,
        b.grup_n,
        b.comparator,
        b.deger,
        b.unit,
        b.etki_turu,
        b.ci95,
        b.p,
        b.outcome_type,
        b.kanit_gosterim,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.sutun_rolu')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.sutun_rolu')) END AS sutun_rolu,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.tani_olcusu')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.tani_olcusu')) END AS tani_olcusu,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.se')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.se')) END AS se,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.se_bicimi')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.se_bicimi')) END AS se_bicimi,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.regresyon_katsayisi')) = 'STRING'
            THEN JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.regresyon_katsayisi')) END AS regresyon_katsayisi,
        CASE WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.p_baglandigi_satir')) IN ('INTEGER', 'UNSIGNED INTEGER')
            THEN CAST(JSON_EXTRACT(n.notu, '$.p_baglandigi_satir') AS UNSIGNED) END AS p_baglandigi_satir,
        CASE
            WHEN JSON_TYPE(JSON_EXTRACT(n.notu, '$.etiket')) = 'STRING' AND NULLIF(b.degisken, '') IS NOT NULL
                THEN TRIM(REPLACE(REPLACE(REPLACE(REPLACE(b.degisken, '<sup>', '^'), '</sup>', ''), '<br>', ' '), '**', ''))
            ELSE TRIM(REPLACE(REPLACE(REPLACE(REPLACE(b.klinik_bilgi_en, '<sup>', '^'), '</sup>', ''), '<br>', ' '), '**', ''))
        END AS satir_adi,
        -- bolum_goster: yalnız RAG aktarım adaptörünün (src/rag_aktarim.py) yazdığı kayıtlarda bulunur; adı tekrar
        -- etmeyen satırda da klinik açıdan önemli bölüm (ör. 'Paediatric cases') gösterilir. Bayrağı olmayan mevcut
        -- pilot kayıtlarında davranış değişmez (yalnız aynı adın birden çok bölümde geçtiği satırlar).
        CASE WHEN t.ad IS NOT NULL
                  OR (JSON_TYPE(JSON_EXTRACT(n.notu, '$.bolum_goster')) = 'BOOLEAN'
                      AND JSON_UNQUOTE(JSON_EXTRACT(n.notu, '$.bolum_goster')) = 'true')
             THEN b.alt_bolum END AS bolum_oneki,
        CASE
            WHEN b.bilgi_turu = 'hasta_grubu' THEN 'study population'
            WHEN b.baglam = 'exclusion_criterion' THEN 'exclusion criterion'
            WHEN b.baglam = 'literature_background' THEN 'literature'
            WHEN b.baglam = 'study_finding' THEN 'study finding'
            WHEN b.baglam = 'study_method' THEN 'study method'
            WHEN b.baglam = 'recommendation' THEN 'recommendation'
            ELSE b.baglam
        END AS clinical_context,
        COALESCE(NULLIF(b.makale_basligi, ''), b.filename) AS source,
        CASE
            WHEN b.kanit_sayfa_bitis IS NOT NULL AND b.kanit_sayfa_bitis <> b.kanit_sayfa
                THEN CONCAT(b.kanit_sayfa, '–', b.kanit_sayfa_bitis)
            ELSE CAST(b.kanit_sayfa AS CHAR)
        END AS page,
        b.kanit_sayfa AS page_sort,
        CASE
            WHEN b.tablo_no IS NOT NULL THEN CONCAT('t:', b.filename, ':', b.tablo_no, ':', b.satir_no)
            ELSE CONCAT('k:', b.bulgu_id)
        END AS row_key
    FROM `{{KAYNAK_DB}}`.v_pilot_bulgular AS b
    LEFT JOIN (
        SELECT k.id, CASE WHEN JSON_VALID(k.taslak_notu) THEN k.taslak_notu END AS notu
        FROM `{{KAYNAK_DB}}`.klinik_bulgular AS k
        WHERE k.bilgi_turu IS NOT NULL
    ) AS n ON n.id = b.bulgu_id
    -- Aynı tabloda birden fazla bölümde geçen satır adları (madde 11): yalnız bunlarda bölüm öneki gösterilir.
    LEFT JOIN (
        SELECT r.filename, r.tablo_no, COALESCE(r.ilac, r.degisken) AS ad
        FROM `{{KAYNAK_DB}}`.v_pilot_bulgular AS r
        WHERE r.review_status = 'verified' AND r.kanit_turu = 'tablo' AND r.tablo_no IS NOT NULL
            AND COALESCE(r.ilac, r.degisken) IS NOT NULL
        GROUP BY r.filename, r.tablo_no, COALESCE(r.ilac, r.degisken)
        HAVING COUNT(DISTINCT COALESCE(r.alt_bolum, '')) > 1
    ) AS t ON t.filename <=> b.filename AND t.tablo_no = b.tablo_no AND t.ad = COALESCE(b.ilac, b.degisken)
    WHERE b.review_status = 'verified'
) AS x;

-- Eski taban view (sunum veritabanındaydı, Workbench menüsünde görünüyordu): varsa silinir.
DROP VIEW IF EXISTS `{{SUNUM_DB}}`.`_findings_base`;
