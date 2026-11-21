-- diagnostics (Sinem). Kaynak: teshis_yontemi ve tani_kriteri kayıtları.
-- type: teshis_yontemi -> method, tani_kriteri -> criterion. name: klinik_bilgi_en.
-- detail sütunu YOK: veride ayrı bir ayrıntı alanı yok; tahminle doldurulmaz.
-- GROUP BY f.row_key: pilotta bu türlerin hepsi metin (row_key tekil, satırlar tek tek görünür);
-- ileride grup başına kayıt taşıyan tablo satırı gelirse yöntem / ölçüt başına tek satır kalır (diseases ile aynı).
-- Sıra (her makalede): 'literature' satırları en sonda; diğerleri ve literature kendi içinde sayfaya göre.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`diagnostics` AS
SELECT
    MIN(CASE f.bilgi_turu WHEN 'teshis_yontemi' THEN 'method' WHEN 'tani_kriteri' THEN 'criterion' END) AS type,
    MIN(f.klinik_bilgi_en) AS name,
    MIN(f.evidence) AS evidence,
    MIN(f.source) AS source,
    MIN(f.page) AS page
FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
WHERE f.bilgi_turu IN ('teshis_yontemi', 'tani_kriteri')
GROUP BY f.row_key
ORDER BY MIN(f.source), MAX(CASE WHEN f.clinical_context = 'literature' THEN 1 ELSE 0 END),
    MIN(f.page_sort), MIN(f.bulgu_id);
