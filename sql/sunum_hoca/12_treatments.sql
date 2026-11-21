-- treatments (Zehra). Kaynak: ilac, doz, tedavi kayıtları (Table 1 ilaç satırları dahil).
-- Table 1 ilaç satırı grup başına bir kayıt taşır; burada ilaç başına TEK satır gösterilir
-- (kullanım oranları measurements'ta). Metin kayıtları tek tek.
-- treatment_type: ilac / doz -> medication, tedavi -> procedure.
-- dose sütunu YOK: pilotta doz alanı hiçbir kayıtta dolu değil (makalede doz yok); veri gelirse eklenir.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`treatments` AS
SELECT
    MAX(CASE WHEN f.bilgi_turu = 'tedavi' THEN 'procedure' ELSE 'medication' END) AS treatment_type,
    MIN(COALESCE(f.ilac, f.klinik_bilgi_en)) AS treatment_name,
    MIN(f.clinical_context) AS clinical_context,
    MIN(f.evidence) AS evidence,
    MIN(f.source) AS source,
    MIN(f.page) AS page
FROM `{{SUNUM_DB}}`.`_findings_base` AS f
WHERE f.bilgi_turu IN ('ilac', 'doz', 'tedavi')
GROUP BY f.row_key
ORDER BY MIN(f.source), MIN(f.page_sort), MIN(f.bulgu_id);
