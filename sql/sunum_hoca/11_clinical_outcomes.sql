-- clinical_outcomes (Zehra). Kaynak: Table 2'nin bütün satırları + metin klinik_sonuc kayıtları.
-- factor: tablo satırında satır adı (degisken), metinde klinik_bilgi_en.
-- statistical_result: analiz + etki ölçüsü + değer + %95 GA + p (ör. "univariate OR 3.124 (95% CI 2.213-4.310), p<0.001");
--   etki ölçüsü / GA / p olmayan kayıtta NULL.
-- outcome sütunu YOK: veride ayrı alan yok (Table 2'nin sonucu yalnız tablo başlığında); tahminle doldurulmaz.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`clinical_outcomes` AS
SELECT
    CASE WHEN f.tablo_no IS NOT NULL THEN f.degisken ELSE f.klinik_bilgi_en END AS factor,
    CASE WHEN f.etki_turu IS NOT NULL OR f.ci95 IS NOT NULL OR f.p IS NOT NULL THEN
        CONCAT_WS(', ',
            NULLIF(CONCAT_WS(' ',
                f.analiz,
                CASE WHEN f.deger IS NOT NULL THEN CONCAT_WS(' ', f.etki_turu, f.deger) ELSE f.etki_turu END,
                CASE WHEN f.ci95 IS NOT NULL THEN CONCAT('(95% CI ', f.ci95, ')') END), ''),
            CASE WHEN f.p IS NOT NULL THEN
                CONCAT('p', CASE WHEN LEFT(f.p, 1) IN ('<', '>', '=', '≤', '≥') THEN '' ELSE '=' END, f.p) END)
    END AS statistical_result,
    f.evidence,
    f.source,
    f.page
FROM `{{SUNUM_DB}}`.`_findings_base` AS f
WHERE f.tablo_no = 2 OR (f.kanit_turu = 'metin' AND f.bilgi_turu = 'klinik_sonuc')
ORDER BY f.source, f.page_sort, f.tablo_no, f.satir_no, f.bulgu_id;
