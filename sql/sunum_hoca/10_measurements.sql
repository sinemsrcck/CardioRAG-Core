-- measurements (Zehra). Kaynak: grup karşılaştırması olan TABLO satırları (kanit_turu = 'tablo' ve aynı tablo
-- satırında hasta grubu kaydı var: v_sunum_taban.satir_grup_karsilastirmasi = 1), türüne ve tablo numarasına
-- bakılmadan. Etki tabloları (univariate / multivariate OR / HR) ve tanısal performans tabloları hasta grubu
-- taşımadığı için clinical_outcomes'tadır. Metin kaynaklı olcum kayıtları general_clinical_findings'te görünür.
-- parameter: COALESCE(ilac, degisken); aynı tabloda birden fazla bölümde geçen adlarda bölüm öneki
--   ("delta hs Trop I (0-1)h ±20% – Non risc"); tekrar etmeyen adlar değişmez.
--
-- measurements_by_group: grup başına bir satır; bütün gruplar (Total dahil), veri kaybı yok.
--   group_order = aynı tablo satırının grupları içinde PDF başlık sırası (1, 2, …).
--   Sıra güvencesi: src/pilot_tablo.py grupları PDF başlık sütun sırasıyla üretir; aktarım dosyası bu
--   sırayı korur; import_records kayıtları dosya sırasıyla tek transaction'da yazar, bu yüzden bulgu_id
--   (AUTO_INCREMENT) başlık sırasıyla artar. evaluation/test_sunum_hoca.py bu sırayı PDF başlığıyla
--   karşılaştırarak doğrular.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`measurements_by_group` AS
SELECT
    CONCAT_WS(' – ', f.bolum_oneki, COALESCE(f.ilac, f.degisken)) AS parameter,
    ROW_NUMBER() OVER (PARTITION BY f.row_key ORDER BY f.bulgu_id) AS group_order,
    f.hasta_grubu AS group_name,
    f.deger AS group_value,
    f.unit,
    f.p AS p_value,
    f.evidence,
    f.source,
    f.page
FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
WHERE f.kanit_turu = 'tablo' AND f.hasta_grubu IS NOT NULL
ORDER BY f.source, f.page_sort, f.tablo_no, f.satir_no, f.bulgu_id;

-- measurements: aynı tablo satırının KARŞILAŞTIRMA grupları yan yana (group_N). group_N sütunlarının sayısı
-- sabit değil: kurulum verideki en fazla karşılaştırma grubu sayısını bulur (Total sayılmaz) ve yer tutucunun
-- yerine o kadar group_N / group_N_value sütunu yazar (en az 2). Yeni veride daha çok grup varsa kurulum
-- yeniden çalıştırılmalı (python -m src.sunum_hoca --kur); kurulum bunu her seferinde yeniden hesaplar.
-- total_group / total_value: Total sütunu (taslak_notu.sutun_rolu = 'toplam'), ör. "Total (n=114)" / "64 (53-74)";
--   karşılaştırma grubu değildir, group_N'e girmez. Total sütunu olmayan tabloda boş.
-- effect: aynı satırın p sütunundaki etki ölçüsü (hasta grubu olmayan etki kaydı), ör. "OR 7.8 (95% CI 2.4–25.1)";
--   yoksa boş.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`measurements` AS
SELECT
    MIN(x.parameter) AS parameter,
    {{MEASUREMENT_GROUP_COLUMNS}},
    MAX(x.total_group) AS total_group,
    MAX(x.total_value) AS total_value,
    MAX(x.unit) AS unit,
    MAX(x.p_value) AS p_value,
    MAX(x.effect) AS effect,
    MAX(x.evidence) AS evidence,
    MAX(x.source) AS source,
    MAX(x.page) AS page
FROM (
    SELECT
        f.row_key,
        f.bulgu_id,
        f.page_sort,
        f.tablo_no,
        f.satir_no,
        f.source,
        CONCAT_WS(' – ', f.bolum_oneki, COALESCE(f.ilac, f.degisken)) AS parameter,
        CASE WHEN f.hasta_grubu IS NOT NULL AND COALESCE(f.sutun_rolu, '') <> 'toplam' THEN
            ROW_NUMBER() OVER (PARTITION BY f.row_key, (f.hasta_grubu IS NOT NULL AND COALESCE(f.sutun_rolu, '') <> 'toplam')
                               ORDER BY f.bulgu_id)
        END AS group_order,
        f.hasta_grubu AS group_name,
        f.deger AS group_value,
        CASE WHEN f.sutun_rolu = 'toplam' THEN
            CONCAT(f.hasta_grubu, CASE WHEN f.grup_n IS NOT NULL THEN CONCAT(' (n=', f.grup_n, ')') ELSE '' END)
        END AS total_group,
        CASE WHEN f.sutun_rolu = 'toplam' THEN f.deger END AS total_value,
        f.unit,
        CASE WHEN f.hasta_grubu IS NOT NULL THEN f.p END AS p_value,
        CASE WHEN f.hasta_grubu IS NULL AND f.etki_turu IS NOT NULL AND f.deger IS NOT NULL THEN
            CONCAT(f.etki_turu, ' ', f.deger,
                   CASE WHEN f.ci95 IS NOT NULL
                       THEN CONCAT(' (95% CI ', REGEXP_REPLACE(f.ci95, '([0-9])-', '$1–', 1, 1), ')') ELSE '' END)
        END AS effect,
        f.evidence,
        f.page
    FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
    WHERE f.kanit_turu = 'tablo' AND f.satir_grup_karsilastirmasi = 1
) AS x
GROUP BY x.row_key
ORDER BY MIN(x.source), MIN(x.page_sort), MIN(x.tablo_no), MIN(x.satir_no), MIN(x.bulgu_id);
