-- Hoca sunum veritabanı: ortak taban view.
-- Kurulum: python -m src.sunum_hoca --kur  (dosyalar ada göre sırayla çalışır; tekrar çalıştırılabilir)
-- Yer tutucular kurulumda doldurulur, dosyada veritabanı adı yazılmaz:
--   {{KAYNAK_DB}} = .env MYSQL_DATABASE (klinik_bulgular, v_pilot_bulgular)
--   {{SUNUM_DB}}  = .env MYSQL_SUNUM_DATABASE, varsayılan kardiyo_sunum
-- Bölücü kuralı: satır sonunda ';' yalnız ifade sonunda; yorumlar tam satır '--'.
--
-- _findings_base: bütün hoca view'larının ortak kaynağı (iç kullanım; hoca bunu açmaz).
-- Kaynak: {{KAYNAK_DB}}.v_pilot_bulgular (python -m src.sunum --kur ile kurulur), yalnız
-- review_status = 'verified' satırlar. Ortak kurallar burada BİR KEZ yazılır:
--   clinical_context  : hasta_grubu -> 'study population'; diğerleri baglam'dan
--                       (exclusion criterion, literature, study finding, study method, recommendation)
--   evidence          : metin satırında kanit_gosterim (çok parçalı kanıtta "[s.N] …" gösterimi; birleştirilmiş
--                       alıntı değil). Tablo satırında ham markdown satırı yerine kısa kaynak metni
--                       "Table <tablo_no> – <satır adı>": tablo_no = taslak_notu.tablo_no (v_pilot_bulgular.tablo_no),
--                       satır adı = klinik_bilgi_en (PDF'teki satır adı); <sup>x</sup> -> ^x, <br> ve ** atılır.
--                       Ham tablo satırı kaynak veritabanında (kanit_cumle) birebir duruyor.
--   source            : makale başlığı, yoksa PDF dosya adı
--   page              : "4" ya da sayfa aralığı "2–3"; sıralama için page_sort (sayı)
-- Hoca view'ları teknik sütun (bulgu_id, document_id, satir_hash, taslak_notu …) GÖSTERMEZ;
-- bu taban view'daki bulgu_id / tablo_no / satir_no yalnız birleştirme ve sıralama içindir.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`_findings_base` AS
SELECT
    b.bulgu_id,
    b.filename,
    b.bilgi_turu,
    b.baglam,
    b.kanit_turu,
    b.tablo_no,
    b.satir_no,
    b.analiz,
    b.klinik_bilgi_en,
    b.degisken,
    b.ilac,
    b.mudahale_turu,
    b.hasta_grubu,
    b.grup_n,
    b.deger,
    b.unit,
    b.etki_turu,
    b.ci95,
    b.p,
    CASE
        WHEN b.bilgi_turu = 'hasta_grubu' THEN 'study population'
        WHEN b.baglam = 'exclusion_criterion' THEN 'exclusion criterion'
        WHEN b.baglam = 'literature_background' THEN 'literature'
        WHEN b.baglam = 'study_finding' THEN 'study finding'
        WHEN b.baglam = 'study_method' THEN 'study method'
        WHEN b.baglam = 'recommendation' THEN 'recommendation'
        ELSE b.baglam
    END AS clinical_context,
    CASE
        WHEN b.kanit_turu = 'tablo' AND b.tablo_no IS NOT NULL AND NULLIF(b.klinik_bilgi_en, '') IS NOT NULL
            THEN CONCAT('Table ', b.tablo_no, ' – ',
                 TRIM(REPLACE(REPLACE(REPLACE(REPLACE(b.klinik_bilgi_en, '<sup>', '^'), '</sup>', ''), '<br>', ' '), '**', '')))
        ELSE b.kanit_gosterim
    END AS evidence,
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
WHERE b.review_status = 'verified';
