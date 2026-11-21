-- general_clinical_findings (Zehra). Kaynak: diger, hasta_grubu ve olcum METİN kayıtları; tablo satırları girmez.
-- Metin kaynaklı ölçüm cümleleri (olcum) measurements'ta değil burada görünür (measurements yalnız Table 1).
-- Klinik değeri olmayan genel cümleler SQL'e hiç aktarılmaz (onaylanmış JSON'dan çıkarılır);
-- bu view aktarılmış her diger / hasta_grubu / olcum metin kaydını gösterir.
-- clinical_context: ortak kural (_findings_base); hasta_grubu -> 'study population',
--   hastalık olmayan dışlama kriteri -> 'exclusion criterion', diğerleri baglam'dan.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`general_clinical_findings` AS
SELECT
    f.klinik_bilgi_en AS clinical_finding,
    f.clinical_context,
    f.evidence,
    f.source,
    f.page
FROM `{{SUNUM_DB}}`.`_findings_base` AS f
WHERE f.kanit_turu = 'metin' AND f.bilgi_turu IN ('diger', 'hasta_grubu', 'olcum')
ORDER BY f.source, f.page_sort, f.bulgu_id;
