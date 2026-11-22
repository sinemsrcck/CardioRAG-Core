# sql/sunum_hoca — hoca sunum veritabanı view'ları

`python -m src.sunum_hoca --kur` bu klasördeki **`*.sql` dosyalarını ada göre sırayla** çalıştırır ve sunum veritabanını (varsayılan `kardiyo_sunum`) kurar. Klasöre sonradan eklenen dosyalar da otomatik alınır. Bu `README.md` çalıştırılmaz.

## Dosya numaraları

| Aralık | Kim | Dosyalar |
|---|---|---|
| 00–19 | Zehra (ortak altyapı ve kendi view'ları) | `00_ortak.sql` (taban view `v_sunum_taban`, kaynak veritabanında), `10_measurements.sql`, `11_clinical_outcomes.sql`, `12_treatments.sql`, `13_general_clinical_findings.sql` |
| 20–22 | Sinem | `20_diseases.sql`, `21_diagnostics.sql`, `22_symptoms.sql` |

Herkes yalnız kendi dosyalarını düzenler; böylece çakışma olmaz.

## Kurallar
- **Yer tutucular:** Veritabanı adı yazılmaz; `` `{{SUNUM_DB}}` `` ve `` `{{KAYNAK_DB}}` `` yer tutucuları kullanılır. Kurulum bunları `.env`'den doldurur: `MYSQL_DATABASE`, ve varsayılanı `kardiyo_sunum` olan `MYSQL_SUNUM_DATABASE`.
- **Tekrar çalıştırma:** Her view `CREATE OR REPLACE VIEW` ile yazılır, böylece kurulum tekrar çalıştırılabilir.
- **Sıra:** Her makalede önce çalışmanın kendi bulguları, `literature` satırları en sonda; grup içinde sayfaya göre (`f.page_sort`). Örn. `clinical_outcomes`: metin `study finding` → tablo satırları → `literature`.
- **Bölücü:** Satır sonunda `;` yalnız ifadenin sonunda olur; yorumlar tam satır `--` ile yazılır. Kurulum ifadeleri bu kurala göre böler.
- **Taban view:** Kaynak olarak `` `{{KAYNAK_DB}}`.`v_sunum_taban` `` kullanılır; `v_pilot_bulgular`'a doğrudan gidilmez. Ortak kurallar orada bir kez yazıldı. `v_sunum_taban` yalnızca `review_status = 'verified'` satırları içerir.
  - Taban view **kaynak veritabanında** durur (`.env` `MYSQL_DATABASE`), sunum veritabanında değil. Sunum veritabanında yalnız hocaya gösterilen view'lar olur; iç / yardımcı view (`_` ile başlayan ya da teknik sütunlu) oraya konmaz.
  - Eski `_findings_base` (sunum veritabanındaydı) kurulumda `DROP VIEW IF EXISTS` ile silinir.
  - Kaynak veritabanında `v_sunum_taban` dışında hiçbir tablo / view değiştirilmez.
- **Sütun adları:** İngilizce, snake_case. Son üç sütun her view'da `evidence, source, page` olur.
- **Teknik sütun yok:** `bulgu_id`, `document_id`, `satir_hash`, `taslak_notu`, `tablo_no`, `satir_no`, `bilgi_turu`, `baglam`, `kanit_turu`, `row_key`, `page_sort`. Bunlar yalnız WHERE / GROUP BY / ORDER BY içinde kullanılır.
- **Boş sütun yok:** Veride hiç dolu olmayan bir sütun view'a konmaz.
- **Tablo / metin ayrımı:** `f.kanit_turu` (`'tablo'` / `'metin'`) kullanılır.
  - Tablo satırları **kaydın içeriğine** göre gider, tablo numarasına bakılmaz (`f.satir_grup_karsilastirmasi`):
    - Aynı tablo satırında hasta grubu kaydı var (grup karşılaştırması) → `measurements` / `measurements_by_group`. O satırın p sütunundaki etki ölçüsü (hasta grubu olmayan OR kaydı) `measurements.effect`'te görünür.
    - Hasta grubu kaydı yok → `clinical_outcomes`: etki tabloları (univariate / multivariate OR / HR) ve tanısal performans kayıtları (`outcome_type = 'tanısal performans'`, test × ölçü).
    - Pilotta bu yine Table 1 → `measurements`, Table 2 → `clinical_outcomes` demektir; pilot verisinde yeni alan gerekmez.
  - Ad türündeki tablo satırları (tani, ilac) ayrıca kendi view'larında görünür (`diseases`, `treatments`). `diagnostics` yalnız tanı yöntemi / kriteri gösterir (tanısal performans değerleri `clinical_outcomes`'ta).
  - Metin kayıtları türüne göre gider. `olcum`, `diger` ve `hasta_grubu` metin kayıtları `general_clinical_findings`'e gider; `measurements` yalnız tablo satırıdır.
- **Total sütunu:** `taslak_notu.sutun_rolu = 'toplam'` olan kayıt (src/pilot_tablo.py; "Total", "Overall", "All patients" …) `measurements`'ta `total_group` ("Total (n=114)") / `total_value` sütunlarında görünür; `group_N`'e ve grup sayısına (kurulumdaki `group_N` sütun sayısı) girmez. `measurements_by_group` Total dahil bütün grupları gösterir.
- **Tekrar eden ad:** Aynı tabloda aynı ad birden fazla bölümde geçiyorsa (örn. "Non risc") yalnız o adlarda bölüm öneki eklenir: "delta hs Trop I (0-1)h ±20% – Non risc" (`bolum_oneki`; `parameter`, `finding`, `evidence`). Tekrar etmeyen adlar değişmez.
- **Tablo satırının evidence'ı:** Ham markdown satırı değildir. `v_sunum_taban` bunu `"Table <tablo_no> – [<bölüm> – ]<satır adı>"` olarak üretir (örn. "Table 1 – Age (years)", "Table 2 – Diabetes mellitus"), view'da ayrıca bir şey yapmak gerekmez.
  - `tablo_no` ← `taslak_notu.tablo_no`; satır adı ← `klinik_bilgi_en`. Etiket sütunlu satırda (`taslak_notu.etiket`, örn. "A-B") kanıttaki parça kısa olduğu için birleşik ad (`degisken`, örn. "Pairwise comparation A-B"); `klinik_bilgi_en` değişmez.
  - `<sup>x</sup>` → `^x` olur (örn. "BMI kg/m^2"); `<br>` ve `**` atılır.
  - Metin satırının evidence'ı `kanit_gosterim`'dir; değişmez.
  - Ham tablo satırı kaynak veritabanında (`kanit_cumle`) birebir duruyor.

## v_sunum_taban'da hazır olan alanlar

| Alan | Anlamı |
|---|---|
| `clinical_context` | `hasta_grubu` → "study population"; baglam'dan: exclusion criterion, literature, study finding, study method, recommendation |
| `evidence` | Metin: `kanit_gosterim` (çok parçalı kanıtta "[s.N] …" gösterimi, birleştirilmiş alıntı değil). Tablo: "Table N – satır adı" |
| `source` | Makale başlığı, yoksa PDF dosya adı |
| `page` / `page_sort` | Gösterim ("4" ya da "2–3") / sıralama sayısı |
| `row_key` | Tablo satırı için `t:dosya:tablo_no:satir_no`; metin için `k:bulgu_id`. Grup kayıtlarını tek satıra indirmek için `GROUP BY f.row_key`. |
| `satir_grup_karsilastirmasi` | 1: aynı tablo satırında hasta grubu kaydı var (→ `measurements`); 0: yok (→ `clinical_outcomes`) |
| `sutun_rolu` | `'toplam'`: Total sütunu kaydı; diğerleri NULL |
| `satir_adi` / `bolum_oneki` | Gösterilen satır adı (etiketli satırda birleşik ad) / tekrar eden adda bölüm (yoksa NULL) |
| `tani_olcusu`, `se`, `se_bicimi`, `p_baglandigi_satir` | Tanısal performans kaydı: ölçü türü, standart hata ve biçimi (`±` / `parantez`), AUC'ye bağlı P kaydında AUC satırının no'su |
| Diğer | `bilgi_turu`, `baglam`, `kanit_turu`, `tablo_no`, `satir_no`, `alt_bolum`, `klinik_bilgi_en`, `degisken`, `ilac`, `mudahale_turu`, `hasta_grubu`, `grup_n`, `comparator`, `deger`, `unit`, `etki_turu`, `ci95`, `p`, `outcome_type`, `analiz`, `bulgu_id` |

`v_pilot_bulgular`'da olmayan `taslak_notu` alanları (`sutun_rolu`, `etiket`, `se`, `se_bicimi`, `p_baglandigi_satir`, `tani_olcusu`) taban view'da `klinik_bulgular.taslak_notu`'dan JSON_VALID korumalı okunur (yalnız okuma).

## Örnek: `20_diseases.sql` (Sinem için iskelet; bu klasörde çalıştırılmaz)

Kurallar `data/review/pilot/sunum_eslestirme.md`'de:
- **Filtre:** `bilgi_turu = 'tani'`, metin ve tablo.
- **Tablo satırları:** Table 1 tani satırları grup başına bir kayıt taşıdığı için hastalık başına tek satır gösterilir (`GROUP BY f.row_key`). Bu satırların evidence'ı kendiliğinden "Table 1 – CAD" gibi gelir.

```sql
-- diseases (Sinem). Kaynak: tani kayıtları (metin + Table 1 tani satırları).
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
ORDER BY MIN(f.source), MAX(f.clinical_context = 'literature'), MIN(f.page_sort), MIN(f.bulgu_id);
```

**diagnostics** aynı düzende yazılır:
- `type`: `CASE f.bilgi_turu WHEN 'teshis_yontemi' THEN 'method' WHEN 'tani_kriteri' THEN 'criterion' END`
- `name`: `f.klinik_bilgi_en`
- Filtre: `bilgi_turu IN ('teshis_yontemi', 'tani_kriteri')`
- `detail` sütunu yok.

**symptoms** için `bilgi_turu = 'semptom'`; pilotta kayıt yok, view boş döner.

## Test
- `evaluation/test_sunum_hoca.py` Zehra'nın view'larını sınar: sütunlar, teknik sütun yok, satır sayıları doğrudan `klinik_bulgular` sorgusuyla (içeriğe göre yönlendirme) ve sabit sayılarla (51 / 102 / 66 / 13 / 44), sıra (study finding → tablo → literature), tablo evidence kuralı, grup sırası PDF başlığıyla, 3 gruplu örnek ve Troponin örneği (Total, effect, bölüm öneki, tanısal performans; transaction + ROLLBACK).
- Test ayrıca sunum veritabanında **yalnız** `measurements` ve `EXPECTED_COLUMNS`'taki view'ların olduğunu, `_` ile başlayan view ve teknik sütun kalmadığını denetler.
- Sinem yeni view eklediğinde onu `EXPECTED_COLUMNS`'a (`src/sunum_hoca.py`) eklemeli; yoksa bu kontrol "fazla view" diye düşer. Ayrıca kendi test dosyasını açabilir.
- Kurulum kodunda değişiklik gerekmez.
