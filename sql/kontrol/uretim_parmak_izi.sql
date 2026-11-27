-- Üretim parmak izi (YALNIZ OKUMA: CHECKSUM TABLE / SELECT). Elle ek kontrol içindir; aşama betikleri aynı bilgiyi
-- uretim_asama1_yedek.ps1 PY_DURUM ile toplar. Çalıştırma:
--     mysql --default-character-set=utf8mb4 -N -B -e "source sql/kontrol/uretim_parmak_izi.sql"
CHECKSUM TABLE kardiyo_tam_metin.chunk_embeddings, kardiyo_tam_metin.cikarim_reddedilenler,
    kardiyo_tam_metin.halusinasyon_degerlendirme, kardiyo_tam_metin.hata_onlem, kardiyo_tam_metin.icd10_who,
    kardiyo_tam_metin.kalite_calistirmalari, kardiyo_tam_metin.kalite_isaretleri, kardiyo_tam_metin.klinik_bulgular,
    kardiyo_tam_metin.makaleler, kardiyo_tam_metin.pdf_belgeleri, kardiyo_tam_metin.pdf_chunks,
    kardiyo_tam_metin.pdf_isleme_durumu, kardiyo_tam_metin.rag_sorgu_kaynaklari, kardiyo_tam_metin.rag_sorgulari,
    kardiyo_tam_metin.tkd_kurallari, kardiyo_tam_metin.semptom_iliskileri, kardiyo_tam_metin.sonuclar,
    kardiyo_tam_metin.tani_tedavi,
    kardiyo_clinical_data.clinical_outcomes, kardiyo_clinical_data.diagnostics, kardiyo_clinical_data.diseases,
    kardiyo_clinical_data.general_clinical_findings, kardiyo_clinical_data.measurements,
    kardiyo_clinical_data.measurements_by_group, kardiyo_clinical_data.symptoms, kardiyo_clinical_data.treatments;

SELECT 'tablo_sayisi', TABLE_SCHEMA, TABLE_NAME, '' FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data') AND TABLE_TYPE = 'BASE TABLE'
ORDER BY 2, 3;

SELECT 'gorunum', TABLE_SCHEMA, TABLE_NAME, MD5(VIEW_DEFINITION) FROM information_schema.VIEWS
WHERE TABLE_SCHEMA IN ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data') ORDER BY 2, 3;

SELECT 'rutin', ROUTINE_SCHEMA, ROUTINE_NAME, MD5(ROUTINE_DEFINITION) FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA IN ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data') ORDER BY 2, 3;

-- İnsan onayı dağılımı (verified sayısı hiçbir fazda kendiliğinden artmamalı)
SELECT 'insan_durumu', review_status, COALESCE(cikarma_yontemi, '(NULL)'), COUNT(*)
FROM kardiyo_tam_metin.klinik_bulgular GROUP BY review_status, cikarma_yontemi ORDER BY 2, 3;

-- Otomatik doğrulama satırları (rag- ad alanı; klinik_bulgu_id her zaman NULL olmalı)
SELECT 'rag_kalite', kontrol_surumu, (klinik_bulgu_id IS NULL), COUNT(*)
FROM kardiyo_tam_metin.kalite_isaretleri WHERE kontrol_surumu LIKE 'rag-%' GROUP BY 2, 3 ORDER BY 2, 3;

-- Hoca sunum view'ları (yalnız verified; satır sayıları değişmemeli)
SELECT 'hoca_satir', 'clinical_outcomes', COUNT(*), '' FROM kardiyo_sunum.clinical_outcomes
UNION ALL SELECT 'hoca_satir', 'diagnostics', COUNT(*), '' FROM kardiyo_sunum.diagnostics
UNION ALL SELECT 'hoca_satir', 'diseases', COUNT(*), '' FROM kardiyo_sunum.diseases
UNION ALL SELECT 'hoca_satir', 'general_clinical_findings', COUNT(*), '' FROM kardiyo_sunum.general_clinical_findings
UNION ALL SELECT 'hoca_satir', 'measurements', COUNT(*), '' FROM kardiyo_sunum.measurements
UNION ALL SELECT 'hoca_satir', 'measurements_by_group', COUNT(*), '' FROM kardiyo_sunum.measurements_by_group
UNION ALL SELECT 'hoca_satir', 'symptoms', COUNT(*), '' FROM kardiyo_sunum.symptoms
UNION ALL SELECT 'hoca_satir', 'treatments', COUNT(*), '' FROM kardiyo_sunum.treatments;
