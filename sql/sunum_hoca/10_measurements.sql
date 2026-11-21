-- measurements (Zehra). Kaynak: Table 1'in bütün satırları (türüne bakılmadan) + metin olcum kayıtları.
--
-- measurements_by_group: grup başına bir satır; bütün gruplar, veri kaybı yok.
--   group_order = aynı tablo satırının grupları içinde PDF başlık sırası (1, 2, …).
--   Sıra güvencesi: src/pilot_tablo.py grupları PDF başlık sütun sırasıyla üretir; aktarım dosyası bu
--   sırayı korur; import_records kayıtları dosya sırasıyla tek transaction'da yazar, bu yüzden bulgu_id
--   (AUTO_INCREMENT) başlık sırasıyla artar. evaluation/test_sunum_hoca.py bu sırayı PDF başlığıyla
--   karşılaştırarak doğrular. Metin kayıtlarında grup yok (group_order / group_name / group_value NULL).
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`measurements_by_group` AS
SELECT
    CASE WHEN f.tablo_no IS NOT NULL THEN COALESCE(f.ilac, f.degisken) ELSE f.klinik_bilgi_en END AS parameter,
    CASE WHEN f.tablo_no IS NOT NULL
        THEN ROW_NUMBER() OVER (PARTITION BY f.row_key ORDER BY f.bulgu_id) END AS group_order,
    f.hasta_grubu AS group_name,
    f.deger AS group_value,
    f.unit,
    f.p AS p_value,
    f.evidence,
    f.source,
    f.page
FROM `{{SUNUM_DB}}`.`_findings_base` AS f
WHERE f.tablo_no = 1 OR (f.kanit_turu = 'metin' AND f.bilgi_turu = 'olcum')
ORDER BY f.source, f.page_sort, f.tablo_no, f.satir_no, f.bulgu_id;

-- measurements: aynı tablo satırının grupları yan yana. group_N sütunlarının sayısı sabit değil:
-- kurulum verideki en fazla grup sayısını bulur ve {{MEASUREMENT_GROUP_COLUMNS}} yerine o kadar
-- group_N / group_N_value sütunu yazar (en az 2). Yeni veride daha çok grup varsa kurulum yeniden
-- çalıştırılmalı (python -m src.sunum_hoca --kur); kurulum bunu her seferinde yeniden hesaplar.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`measurements` AS
SELECT
    MIN(x.parameter) AS parameter,
    {{MEASUREMENT_GROUP_COLUMNS}},
    MAX(x.unit) AS unit,
    MAX(x.p_value) AS p_value,
    MAX(x.evidence) AS evidence,
    MAX(x.source) AS source,
    MAX(x.page) AS page
FROM (
    SELECT
        f.row_key,
        f.bulgu_id,
        f.page_sort,
        f.source,
        CASE WHEN f.tablo_no IS NOT NULL THEN COALESCE(f.ilac, f.degisken) ELSE f.klinik_bilgi_en END AS parameter,
        CASE WHEN f.tablo_no IS NOT NULL
            THEN ROW_NUMBER() OVER (PARTITION BY f.row_key ORDER BY f.bulgu_id) END AS group_order,
        f.hasta_grubu AS group_name,
        f.deger AS group_value,
        f.unit,
        f.p AS p_value,
        f.evidence,
        f.page
    FROM `{{SUNUM_DB}}`.`_findings_base` AS f
    WHERE f.tablo_no = 1 OR (f.kanit_turu = 'metin' AND f.bilgi_turu = 'olcum')
) AS x
GROUP BY x.row_key
ORDER BY MIN(x.source), MIN(x.page_sort), MIN(x.bulgu_id);
