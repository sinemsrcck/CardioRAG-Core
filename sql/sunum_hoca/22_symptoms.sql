-- symptoms (Sinem). Kaynak: semptom kayıtları. symptom: klinik_bilgi_en, patient_group: hasta_grubu.
-- Pilot makalede semptom kaydı yok: view boş döner; yeni makaleler aktarıldığında dolar.
-- patient_group veride yoksa NULL kalır; tahminle doldurulmaz.
-- Grup başına ayrı satır doğru gösterimdir (patient_group sütunu grubu ayırır); GROUP BY yapılmaz.
-- Sıra (her makalede): 'literature' satırları en sonda; diğerleri ve literature kendi içinde sayfaya göre.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`symptoms` AS
SELECT
    f.klinik_bilgi_en AS symptom,
    f.hasta_grubu AS patient_group,
    f.evidence,
    f.source,
    f.page
FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
WHERE f.bilgi_turu = 'semptom'
ORDER BY f.source, CASE WHEN f.clinical_context = 'literature' THEN 1 ELSE 0 END, f.page_sort, f.satir_no, f.bulgu_id;
