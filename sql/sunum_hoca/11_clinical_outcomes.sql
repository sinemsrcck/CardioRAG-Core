-- clinical_outcomes (Zehra). Kaynak: hasta grubu karşılaştırması OLMAYAN tablo satırları
-- (v_sunum_taban.satir_grup_karsilastirmasi = 0: etki tabloları, ör. univariate / multivariate OR / HR; tanısal
-- performans tabloları) + metin klinik_sonuc kayıtları. Yönlendirme tablo numarasına değil kaydın içeriğine bakar.
-- finding:
--   tanısal performans (outcome_type = 'tanısal performans'): "<test> – <ölçü>", ör. "Delta HS Trop I 1h-0h % – Sensitivity";
--     ikili karşılaştırma (comparator dolu): "<ölçü>: <test A> vs <test B>".
--   diğer tablo satırları: faktör adı (degisken; aynı tabloda tekrar eden adda bölüm öneki). Metin: cümle (klinik_bilgi_en).
-- statistical_result:
--   etki satırı: analiz + etki ölçüsü + değer + %95 GA + p (ör. "univariate OR 3.124 (95% CI 2.213-4.310), p<0.001").
--     Regresyon katsayısı varsa (taslak_notu.regresyon_katsayisi, ör. "B" sütunu) etki ölçüsünden önce "B <değer>,"
--     (ekip kararı: B ayrı sütun değil): "multivariate B 7.912, OR 2,735.621, p=0.002". Yoksa biçim aynı.
--   tanısal performans: değer (ör. "75.0%"); AUC "AUC 0.755 ± 0.072, p<0.001"; OR "OR 11.84 (SE 0.70)";
--     ikili karşılaştırma "p=0.291". P satırı AUC'ye bağlıysa (taslak_notu.p_baglandigi_satir) ayrı satır olarak
--     gösterilmez, p'si AUC satırına eklenir ve evidence'a "; Table N – P" yazılır.
--   etki ölçüsü / GA / p olmayan kayıtta NULL (tanısal ölçüde değer gösterilir).
-- Sıra (her makalede): 1) metin 'study finding' satırları, 2) tablo satırları, 3) diğer metin
--   bağlamları, 4) 'literature' satırları en sonda; her grup içinde sayfaya göre.
-- outcome sütunu YOK: veride ayrı alan yok (etki tablosunun sonucu yalnız tablo başlığında); tahminle doldurulmaz.
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`clinical_outcomes` AS
SELECT
    CASE
        WHEN f.kanit_turu = 'tablo' AND f.outcome_type = 'tanısal performans' AND f.comparator IS NOT NULL
            THEN CONCAT(f.degisken, ': ', f.ilac, ' vs ', f.comparator)
        WHEN f.kanit_turu = 'tablo' AND f.outcome_type = 'tanısal performans'
            THEN CONCAT_WS(' – ', f.ilac, f.degisken)
        WHEN f.kanit_turu = 'tablo' THEN CONCAT_WS(' – ', f.bolum_oneki, f.degisken)
        ELSE f.klinik_bilgi_en
    END AS finding,
    CASE
        WHEN f.kanit_turu = 'tablo' AND f.outcome_type = 'tanısal performans' THEN
            NULLIF(CONCAT_WS(', ',
                NULLIF(CONCAT_WS(' ',
                    CASE WHEN f.deger IS NOT NULL THEN
                        CASE f.etki_turu WHEN 'AUC / C-istatistigi' THEN 'AUC' WHEN 'oran / yuzde' THEN NULL
                                         WHEN 'diger' THEN NULL ELSE f.etki_turu END
                    END,
                    CASE WHEN f.deger IS NOT NULL THEN CONCAT(f.deger,
                        CASE WHEN f.se IS NULL THEN ''
                             WHEN f.se_bicimi = '±' THEN CONCAT(' ± ', f.se)
                             ELSE CONCAT(' (SE ', f.se, ')') END) END,
                    CASE WHEN f.ci95 IS NOT NULL THEN CONCAT('(95% CI ', f.ci95, ')') END), ''),
                CASE WHEN COALESCE(f.p, lp.p) IS NOT NULL THEN
                    CONCAT('p', CASE WHEN LEFT(COALESCE(f.p, lp.p), 1) IN ('<', '>', '=', '≤', '≥') THEN '' ELSE '=' END,
                           COALESCE(f.p, lp.p)) END), '')
        WHEN f.etki_turu IS NOT NULL OR f.ci95 IS NOT NULL OR f.p IS NOT NULL THEN
            CONCAT_WS(', ',
                NULLIF(CONCAT_WS(' ',
                    f.analiz,
                    CASE WHEN f.regresyon_katsayisi IS NOT NULL THEN CONCAT('B ', f.regresyon_katsayisi, ',') END,
                    CASE WHEN f.deger IS NOT NULL THEN CONCAT_WS(' ', f.etki_turu, f.deger) ELSE f.etki_turu END,
                    CASE WHEN f.ci95 IS NOT NULL THEN CONCAT('(95% CI ', f.ci95, ')') END), ''),
                CASE WHEN f.p IS NOT NULL THEN
                    CONCAT('p', CASE WHEN LEFT(f.p, 1) IN ('<', '>', '=', '≤', '≥') THEN '' ELSE '=' END, f.p) END)
    END AS statistical_result,
    f.clinical_context,
    CASE WHEN lp.evidence IS NOT NULL THEN CONCAT(f.evidence, '; ', lp.evidence) ELSE f.evidence END AS evidence,
    f.source,
    f.page
FROM `{{KAYNAK_DB}}`.`v_sunum_taban` AS f
LEFT JOIN `{{KAYNAK_DB}}`.`v_sunum_taban` AS lp
    ON f.kanit_turu = 'tablo' AND f.outcome_type = 'tanısal performans' AND f.p_baglandigi_satir IS NULL
    AND lp.p_baglandigi_satir = f.satir_no AND lp.tablo_no = f.tablo_no AND lp.filename <=> f.filename
    AND lp.ilac <=> f.ilac AND lp.kanit_turu = 'tablo'
WHERE (f.kanit_turu = 'tablo' AND f.satir_grup_karsilastirmasi = 0 AND f.p_baglandigi_satir IS NULL)
    OR (f.kanit_turu = 'metin' AND f.bilgi_turu = 'klinik_sonuc')
ORDER BY f.source,
    CASE
        WHEN f.kanit_turu = 'tablo' THEN 2
        WHEN f.clinical_context = 'study finding' THEN 1
        WHEN f.clinical_context = 'literature' THEN 4
        ELSE 3
    END,
    f.page_sort, f.tablo_no, f.satir_no, f.bulgu_id;
