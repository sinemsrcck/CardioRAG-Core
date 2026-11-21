-- diseases (Sinem). Kaynak: tani kayıtları, metin + tablo (Table 1 tani satırları).
-- disease: metin kaydında klinik_bilgi_en, tablo satırında degisken (PDF'teki satır adı, örn. "CAD").
-- Table 1 tani satırı hasta grubu başına bir kayıt taşır; burada hastalık başına TEK satır gösterilir
-- (GROUP BY f.row_key). Grup değerleri measurements'ta. Metin kayıtlarının row_key'i tekil, tek tek görünür.
-- clinical_context: ortak kural (v_sunum_taban); dışlama listesindeki hastalıklar 'exclusion criterion'.
-- Sıra (her makalede): 'literature' satırları en sonda; diğerleri ve literature kendi içinde sayfaya göre.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`diseases` AS
SELECT
    MIN(CASE WHEN f.kanit_turu = 'tablo' THEN f.degisken ELSE f.klinik_bilgi_en END) AS disease,
    MIN(f.clinical_context) AS clinical_context,
    MIN(f.evidence) AS evidence,
    MIN(f.source) AS source,
    MIN(f.page) AS page
FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
WHERE f.bilgi_turu = 'tani'
GROUP BY f.row_key
ORDER BY MIN(f.source), MAX(CASE WHEN f.clinical_context = 'literature' THEN 1 ELSE 0 END),
    MIN(f.page_sort), MIN(f.bulgu_id);
