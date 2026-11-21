# sql/sunum_hoca — hoca sunum veritabanı view'ları

`python -m src.sunum_hoca --kur` bu klasördeki **`*.sql` dosyalarını ada göre sırayla** çalıştırır ve sunum veritabanını (varsayılan `kardiyo_sunum`) kurar. Klasöre sonradan eklenen dosyalar da otomatik alınır. Bu `README.md` çalıştırılmaz.

## Dosya numaraları

| Aralık | Kim | Dosyalar |
|---|---|---|
| 00–19 | Zehra (ortak altyapı ve kendi view'ları) | `00_ortak.sql` (taban view `_findings_base`), `10_measurements.sql`, `11_clinical_outcomes.sql`, `12_treatments.sql`, `13_general_clinical_findings.sql` |
| 20–22 | Sinem | `20_diseases.sql`, `21_diagnostics.sql`, `22_symptoms.sql` |

Herkes yalnız kendi dosyalarını düzenler; böylece çakışma olmaz.

## Kurallar
- **Yer tutucular:** Veritabanı adı yazılmaz; `` `{{SUNUM_DB}}` `` ve `` `{{KAYNAK_DB}}` `` yer tutucuları kullanılır. Kurulum bunları `.env`'den doldurur: `MYSQL_DATABASE`, ve varsayılanı `kardiyo_sunum` olan `MYSQL_SUNUM_DATABASE`.
- **Tekrar çalıştırma:** Her view `CREATE OR REPLACE VIEW` ile yazılır, böylece kurulum tekrar çalıştırılabilir.
- **Bölücü:** Satır sonunda `;` yalnız ifadenin sonunda olur; yorumlar tam satır `--` ile yazılır. Kurulum ifadeleri bu kurala göre böler.
- **Taban view:** Kaynak olarak `` `{{SUNUM_DB}}`.`_findings_base` `` kullanılır; `v_pilot_bulgular`'a doğrudan gidilmez. Ortak kurallar orada bir kez yazıldı. `_findings_base` yalnızca `review_status = 'verified'` satırları içerir.
- **Sütun adları:** İngilizce, snake_case. Son üç sütun her view'da `evidence, source, page` olur.
- **Teknik sütun yok:** `bulgu_id`, `document_id`, `satir_hash`, `taslak_notu`, `tablo_no`, `satir_no`, `bilgi_turu`, `baglam`, `kanit_turu`, `row_key`, `page_sort`. Bunlar yalnız WHERE / GROUP BY / ORDER BY içinde kullanılır.
- **Boş sütun yok:** Veride hiç dolu olmayan bir sütun view'a konmaz.

## _findings_base'te hazır olan alanlar

| Alan | Anlamı |
|---|---|
| `clinical_context` | `hasta_grubu` → "study population"; baglam'dan: exclusion criterion, literature, study finding, study method, recommendation |
| `evidence` | `kanit_gosterim`: çok parçalı kanıtta "[s.N] …" gösterimi (birleştirilmiş alıntı değil) |
| `source` | Makale başlığı, yoksa PDF dosya adı |
| `page` / `page_sort` | Gösterim ("4" ya da "2–3") / sıralama sayısı |
| `row_key` | Tablo satırı için `t:dosya:tablo_no:satir_no`; metin için `k:bulgu_id`. Grup kayıtlarını tek satıra indirmek için `GROUP BY f.row_key`. |
| Diğer | `bilgi_turu`, `baglam`, `kanit_turu`, `tablo_no`, `satir_no`, `klinik_bilgi_en`, `degisken`, `ilac`, `hasta_grubu`, `grup_n`, `deger`, `unit`, `etki_turu`, `ci95`, `p`, `analiz`, `bulgu_id` |

## Örnek: `20_diseases.sql` (Sinem için iskelet; bu klasörde çalıştırılmaz)

Kurallar `data/review/pilot/sunum_eslestirme.md`'de:
- **Filtre:** `bilgi_turu = 'tani'`, metin ve tablo.
- **Tablo satırları:** Table 1 tani satırları grup başına bir kayıt taşıdığı için hastalık başına tek satır gösterilir (`GROUP BY f.row_key`).

```sql
-- diseases (Sinem). Kaynak: tani kayıtları (metin + Table 1 tani satırları).
CREATE OR REPLACE VIEW `{{SUNUM_DB}}`.`diseases` AS
SELECT
    MIN(CASE WHEN f.tablo_no IS NOT NULL THEN f.degisken ELSE f.klinik_bilgi_en END) AS disease,
    MIN(f.clinical_context) AS clinical_context,
    MIN(f.evidence) AS evidence,
    MIN(f.source) AS source,
    MIN(f.page) AS page
FROM `{{SUNUM_DB}}`.`_findings_base` AS f
WHERE f.bilgi_turu = 'tani'
GROUP BY f.row_key
ORDER BY MIN(f.source), MIN(f.page_sort), MIN(f.bulgu_id);
```

**diagnostics** aynı düzende yazılır:
- `type`: `CASE f.bilgi_turu WHEN 'teshis_yontemi' THEN 'method' WHEN 'tani_kriteri' THEN 'criterion' END`
- `name`: `f.klinik_bilgi_en`
- Filtre: `bilgi_turu IN ('teshis_yontemi', 'tani_kriteri')`
- `detail` sütunu yok.

**symptoms** için `bilgi_turu = 'semptom'`; pilotta kayıt yok, view boş döner.

## Test
- `evaluation/test_sunum_hoca.py` Zehra'nın view'larını sınar: sütunlar, teknik sütun yok, satır sayıları doğrudan `klinik_bulgular` sorgusuyla, grup sırası PDF başlığıyla ve 3 gruplu örnek.
- Sinem kendi view'ları için `EXPECTED_COLUMNS`'a (`src/sunum_hoca.py`) ekleme yapabilir ya da ayrı bir test dosyası açabilir.
- Kurulum kodunda değişiklik gerekmez.
