# CardioRAG-Core

## CardioRAG nedir?

CardioRAG, kardiyovasküler makalelerden (PDF) Türkçe sorulara **kaynak göstererek** yanıt veren bir RAG (Retrieval-Augmented Generation) sistemidir. Amaç halüsinasyonu azaltmaktır: LLM yalnızca seçilen makale parçalarına dayanarak yanıt verir, her iddiaya `[1]`, `[2]` biçiminde atıf ekler ve kanıt yetersizse yanıt üretmez.

Kullanılan 6 PDF (`data/pdfs/`; dosya adı öneki kaynağı gösterir):

| Kaynak | Dosya | Makale |
|---|---|---|
| PubMed | `pubM-Atrial Fibrillation.pdf` | Atrial Fibrillation — JAMA, 2025 |
| PubMed | `pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf` | Clinical Updates in Coronary Artery Disease: A Comprehensive Review — Journal of Clinical Medicine, 2024 |
| WHO | `who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf` | Diagnostic Performance of One-hour Delta High Sensitive Troponin in Acute Coronary Syndrome — J Coll Physicians Surg Pak, 2020 |
| WHO | `who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf` | Out-of-hospital cardiac arrest in countries of the Gulf Cooperation Council: a scoping review — East Mediterr Health J, 2021 |
| Türkiye Klinikleri | `TK-cardiovascular36-2-1.pdf` | The Relationship Between HbA1c/C-Peptide Ratio and Ischemic Event in a Patient with Atrial Fibrillation — 2024 |
| Türkiye Klinikleri | `TK-cardiovascular37-2-1.pdf` | Eosinophil-to-Lymphocyte Ratio as a Potential Biomarker in Carotid Atherosclerosis — 2026 |

> `who-` önekli iki dosya WHO kaynağından alınmıştır; makaleler JCPSP ve EMHJ dergilerinde yayımlanmıştır.

## Akış

**Soru-cevap (RAG):**

```
PDF (data/pdfs/)
 → metin çıkarma (pymupdf4llm, sayfa sayfa markdown;
          yan yatırılmış tablo sayfaları düzeltilerek okunur)  src/pdf_loader.py
 → temizleme (kaynakça çıkarılır, sonraki tablolar korunur)    src/pdf_processing.py
 → chunk (sayfa bazında, 1500 karakter, 250 örtüşme;
          markdown tabloları ortasından kesilmez)              src/chunking.py
 → embedding (minilm, 384 boyut, normalize float32)           src/embeddings.py
 → MySQL (pdf_belgeleri, pdf_isleme_durumu, pdf_chunks,
          chunk_embeddings)                                   src/ingest.py, src/database.py
 → load_chunks_for_rag
 → retrieval: semantic (cosine) + BM25 → RRF (k=60), 20 aday  src/retrieval.py
 → reranker (Cross-Encoder; varsayılan bge-reranker-v2-m3)    src/reranker.py
 → evidence guard                                             src/evidence_guard.py
 → kaynak seçimi (en fazla 5 chunk, belge başına en fazla 2)  src/source_diversity.py
 → LLM (OpenAI uyumlu sunucu, varsayılan Qwen/Qwen3-8B)       src/rag.py
 → citation kontrolü ([n] atıfları geçerli mi?)               src/citation_validator.py
 → SQL kaydı: save_rag_result → rag_sorgulari,
              rag_sorgu_kaynaklari                            src/database.py
```

`answer_question` sonucu kendisi kaydetmez; kayıt için `save_rag_result` ayrıca çağrılmalıdır.

**Tablolar ve chunk'lama:** Tablosuz sayfalar 1500 karakterlik örtüşmeli pencerelerle bölünür. Markdown tablosu olan sayfalarda metin aynı yöntemle, her tablo ise ayrı bölünür: sığan tablo, varsa üstündeki "Table N" başlık satırıyla birlikte tek chunk olur; sığmayan tablo yalnızca satır aralarından bölünür ve her devam parçasının başına `<başlık> (devam)` (başlık yoksa `(tablo devamı)`), tablonun başlık satırı ve `|---|` ayraç satırı tekrar eklenir. Tek başına sığmayan bir satır hücre sınırından bölünür.

**Sürümler:** `pdf_belgeleri.processing_version` metin çıkarma, kaynakça temizleme ve chunk'lama sürümlerini birlikte içerir (ör. `markdown-rotated-v3|…|references-v2|chunking-table-v1`). Metin çıkarma veya chunk'lama değişince `python -m src.ingest` yeni sürümü yazar; eski sürümün chunk ve embedding'leri **silinmez**, yalnızca kullanılmaz: `load_chunks_for_rag` ve `validate_row` yalnızca `pdf_belgeleri`'deki güncel sürümün chunk'larını okur.

**Klinik bulgular (ilaç / doz / hasta grubu / tanı yöntemi):**

```
LLM çıkarımı (satırlar, kanıt cümlesi chunk'tan aynen)
 → validate_row (kanıt doğru sayfada birebir mi, sayılar kanıtta mı,
                 icd10 icd10_who'da mı, değerler izinli kümede mi)   src/clinical_validation.py
 → save_clinical_findings
      geçenler     → klinik_bulgular (review_status = 'pending')
      geçemeyenler → halusinasyon_degerlendirme (query_id verilirse)  src/database.py
 → VIEW'lar: v_tani_tedavi, v_sonuclar (hocanın sütun biçiminde; yalnızca kaynak='cardiorag' satırları)
```

## Kurulum

### 1. MySQL 8 ve hocanın veritabanı

MySQL 8 (geliştirmede 8.4) kurulu ve çalışıyor olmalı. Hocanın `kardiyo_tam_metin.sql` dosyası **repoda yoktur**; bir kez yüklenir:

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe" -u root -p
```

MySQL istemcisinde:

```sql
CREATE DATABASE IF NOT EXISTS kardiyo_tam_metin CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE kardiyo_tam_metin;
SOURCE C:/dosyanin/yolu/kardiyo_tam_metin.sql;
SHOW TABLES;
```

`SHOW TABLES` en az şu 6 tabloyu göstermeli: `icd10_who`, `makaleler`, `semptom_iliskileri`, `sonuclar`, `tani_tedavi`, `tkd_kurallari`. Bu tablolardaki hocanın eski satırları 6. adımda temizlenecek.

### 2. PDF'ler

6 PDF'i `data/pdfs/` klasörüne koyun (klasör repoda yoktur). **Aynı dosyalar** olmalıdır: `document_id`, PDF içeriğinin SHA-256 hash'idir; farklı bir dosya farklı bir belge sayılır.

### 3. Ortam değişkenleri

```powershell
Copy-Item .env.example .env
```

`.env` içinde `MYSQL_PASSWORD` ve (LLM kullanılacaksa) `LLM_BASE_URL`, `LLM_API_KEY` doldurulur. `LLM_MODEL` şablonda `src/rag.py`'deki varsayılanla aynı değere (`Qwen/Qwen3-8B`) ayarlıdır; bu satır boş bırakılmamalıdır, çünkü `.env`'de boş tanımlı bir değişken için `os.getenv` varsayılanı değil boş metni döndürür. `LLM_BASE_URL` boşsa LLM çağrısı açık bir hata verir (`use_llm=False` ile retrieval yine çalışır); `LLM_API_KEY` boşsa `"not-required"` gönderilir.

### 4. Python ortamı

```powershell
conda create -n cardiorag python=3.11
conda activate cardiorag
pip install -r requirements.txt
```

İlk çalıştırmada embedding ve reranker modelleri Hugging Face'ten indirilir (bge-reranker-v2-m3 yaklaşık 2.2 GB).

### 5. Veritabanını kurma ve doldurma (sırayla)

```powershell
python -m src.database --init
python -m src.ingest
python -m src.article_metadata --write --tam-metin temiz
python -m src.load_translations
```

- `src.database --init`: yeni tabloları, eksik sütunları, kısıtları ve VIEW'ları oluşturur; tekrar çalıştırılabilir, hiçbir şey silmez. `--check` (varsayılan) yalnızca durumu raporlar.
- `src.ingest`: 6 PDF'i okur, temizler, parçalar, embedding üretir ve yazar (varsayılan model `minilm`; `--model bge-m3` seçeneği var). Tekrar çalıştırmak güvenlidir; var olan chunk ve embedding'ler yeniden yazılmaz.
- `src.article_metadata --write --tam-metin temiz`: 6 makaleyi `makaleler` tablosuna yeni satır olarak ekler (başlık, dergi, yıl, DOI, abstract, temizlenmiş tam metin). Önce `--dry-run` ile yazmadan görülebilir.
- `src.load_translations`: Türkçe özeti olmayan 4 makalenin LLM çevirilerini `data/clinical/abstract_translations.json`'dan `makaleler.ozet_tr`'ye yükler. Çeviride `abstract_en`'de olmayan bir sayı varsa o kayıt yazılmaz; PDF'ten gelen orijinal Türkçe özetin üzerine asla yazılmaz. Önce `--dry-run` ile görülebilir; tekrar çalıştırmak bir şey değiştirmez. Bu adım `article_metadata`'dan sonra çalıştırılır; `article_metadata`'yı daha sonra yeniden çalıştırmak çevirileri silmez.

### 6. Hocanın eski verisini temizleme

Bu adım, 5. adımdaki komutların hepsi çalıştıktan sonra yapılır.

Hocanın SQL dosyası tablo yapıları ile `icd10_who` ve `tkd_kurallari` referans verisi için gereklidir; ama içindeki eski makale ve bulgu satırları kullanılmaz (6 PDF baştan indexlenir). Bu yüzden eski satırlar silinir. Hiçbir tablo DROP edilmez; `icd10_who` ve `tkd_kurallari` olduğu gibi kalır.

Önce yedek alınması önerilir (`backups/` git'e girmez; PowerShell'de `>` dosyayı UTF-16 yazacağı için `--result-file` kullanılır):

```powershell
& "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqldump.exe" -u root -p --single-transaction --routines --result-file=backups/kardiyo_tam_metin_temizlik_oncesi.sql kardiyo_tam_metin
```

Sonra MySQL istemcisinde, tek transaction içinde:

```sql
USE kardiyo_tam_metin;
SET SQL_SAFE_UPDATES = 0;
START TRANSACTION;

DELETE FROM sonuclar;
DELETE FROM semptom_iliskileri;
DELETE FROM tani_tedavi;
DELETE FROM makaleler WHERE document_id IS NULL;

-- Kontrol: ilk dört 0, icd10_who 43, tkd_kurallari 15 olmalı.
SELECT
  (SELECT COUNT(*) FROM makaleler WHERE document_id IS NULL) AS makaleler_hoca,
  (SELECT COUNT(*) FROM tani_tedavi)        AS tani_tedavi,
  (SELECT COUNT(*) FROM sonuclar)           AS sonuclar,
  (SELECT COUNT(*) FROM semptom_iliskileri) AS semptom_iliskileri,
  (SELECT COUNT(*) FROM icd10_who)          AS icd10_who,
  (SELECT COUNT(*) FROM tkd_kurallari)      AS tkd_kurallari;

COMMIT;   -- sayılar yanlışsa COMMIT yerine: ROLLBACK;
SET SQL_SAFE_UPDATES = 1;
```

**Bu adımdan sonra hocanın SQL dosyası tekrar yüklenmemelidir**; yüklenirse eski satırlar geri gelir ve testler (`test_database_schema`, `test_ingestion`, `test_makaleler`) başarısız olur.

## Tablolar

### Hocanın tabloları

Tablo yapıları hocanın SQL dosyasından gelir. Eski satırlar kurulum adım 6'da temizlendi; kod bu tablolara satır eklemez veya silmez (yalnızca `makaleler`'e bizim 6 makalemiz eklenir) ve testler bu sayıların işlemlerle değişmediğini kontrol eder.

| Tablo | İçerik | Durum |
|---|---|---|
| `makaleler` | Makale künyesi, özet ve tam metin. | Hocanın satırı yok (`document_id` NULL = 0); yalnızca bizim 6 makalemiz. |
| `tani_tedavi` | Tanı – tedavi / tanı testi çiftleri, doz, uygulama yolu, kanıt cümlesi ve sayfası. | Boş (0 satır); yalnızca yapı ve `v_tani_tedavi` için sütun biçimi. |
| `sonuclar` | Sonuç ölçütleri: etki türü, değer, %95 GA, p, yön, kanıt. | Boş (0 satır); yalnızca yapı ve `v_sonuclar` için sütun biçimi. |
| `semptom_iliskileri` | Semptom – tanı ilişkileri ve kanıt. | Boş (0 satır). |
| `icd10_who` | ICD-10 kod sözlüğü (İngilizce ve Türkçe adlar). | 43 satır; referans (`validate_row` ICD-10 kontrolü). |
| `tkd_kurallari` | Türk Kardiyoloji Derneği terim kuralları. | 15 satır; referans. |

`makaleler`'e yalnızca 5 sütun eklendi: `document_id`, `doi`, `abstract_en`, `ozet_tr_kaynak`, `ozet_tr_model`. Bizim 6 makalemiz bu tabloda `document_id` **dolu** satırlardır; `document_id` NULL satır (hocanın eski verisi) artık yoktur.

`ozet_tr_kaynak`, `ozet_tr`'nin nereden geldiğini gösterir:

| Değer | Anlamı | `ozet_tr_model` | Satırlar |
|---|---|---|---|
| `orijinal` | PDF'te yazarların kendi Türkçe özeti | NULL | 2 TK makalesi (`src.article_metadata`) |
| `llm_ceviri` | `abstract_en`'in LLM çevirisi; sayılar kontrol edildi, insan onaylı değil | çeviriyi yapan model (`gpt-6-luna`) | AF, OHCA, troponin, KAH (`src.load_translations`) |
| NULL | Türkçe özet yok | NULL | — (şu an böyle satır yok) |

### Bizim tablolarımız (`sql/schema_core.sql`)

| Tablo | İçerik |
|---|---|
| `pdf_belgeleri` | Her PDF için bir satır (SHA-256 `document_id`, sayfa sayısı, işleme sürümü, durum, hata mesajı). |
| `pdf_isleme_durumu` | Her PDF'in her sayfası: ham/temiz karakter sayısı ve durum (islendi, bos_sayfa, kaynakca_silindi, kismen_kaynakca, hata). |
| `pdf_chunks` | Chunk metni, sayfa, sıra, metin hash'i, işleme sürümü ve chunk ayarları. |
| `chunk_embeddings` | Chunk başına ve model başına normalize float32 vektör (BLOB). |
| `klinik_bulgular` | Yapılandırılmış klinik bulgular (müdahale, doz, hasta grubu, sonuç, tanı, ICD-10, kanıt cümlesi ve sayfası, `cikarma_yontemi`, `review_status`). |
| `rag_sorgulari` | Her RAG sorgusu: soru, yanıt, modeller, evidence durumu/skoru, süre, citation kontrolü. |
| `rag_sorgu_kaynaklari` | Sorguda aday olan chunk'lar: retrieval/rerank skoru, sıra, LLM bağlamına girip girmediği. |
| `halusinasyon_degerlendirme` | Halüsinasyon değerlendirmeleri ve denetimi geçemeyen klinik bulgu satırları (`kayit_turu = 'klinik_cikarim'`, ham çıktı ile). Otomatik değerlendirme `verified` olamaz. |

### VIEW'lar (`sql/views_core.sql`)

| VIEW | İçerik |
|---|---|
| `v_tani_tedavi` | `tani_tedavi` (hoca) + `klinik_bulgular` (bizim), hocanın sütun adlarıyla; ek sütunlar `kaynak` (`hoca` / `cardiorag`), `review_status`, `cikarma_yontemi`. `tani_tedavi` boş olduğundan şu an yalnızca `kaynak = 'cardiorag'` satırları vardır. |
| `v_sonuclar` | `sonuclar` (hoca) + `klinik_bulgular`'daki sonuç ölçütü olan satırlar; aynı ek sütunlar. `sonuclar` boş olduğundan şu an yalnızca `kaynak = 'cardiorag'` satırları vardır. |

## Önemli fonksiyonlar

**`load_chunks_for_rag`** — RAG için chunk'ları embedding'leriyle SQL'den okur (her belgenin en güncel işleme sürümü; farklı sürümler ve modeller karışmaz).

```python
from src.database import load_chunks_for_rag
chunks = load_chunks_for_rag("minilm")   # chunk_id, text, page_number, embedding (np.float32) ...
```

**`save_rag_result`** — `answer_question` sonucunu `rag_sorgulari` ve `rag_sorgu_kaynaklari` tablolarına kaydeder, `query_id` döndürür.

```python
from src.rag import answer_question, DEFAULT_LLM
result = answer_question(question, chunks, embedding_model="minilm")
query_id = save_rag_result(result, question, llm_model=DEFAULT_LLM)
```

**`save_clinical_findings`** — klinik bulgu satırlarını denetler; geçenleri `klinik_bulgular`'a `pending` olarak yazar (tekrar çağrıda çoğaltmaz), geçemeyenleri döndürür ve `query_id` verilirse `halusinasyon_degerlendirme`'ye yazar.

```python
from src.database import save_clinical_findings
sonuc = save_clinical_findings(satirlar, "rag", query_id=query_id)
print(sonuc["yazilan"], sonuc["zaten_var"], len(sonuc["reddedilen"]))
```

**`validate_row`** — tek bir satırı kaynağa karşı denetler: `(gecti_mi, hata_turu, aciklama)`.

```python
from src.clinical_validation import load_validation_context, validate_row
ctx = load_validation_context(conn)            # bir kez
ok, hata_turu, aciklama = validate_row(satir, ctx)
```

`hata_turu`: `kanit_birebir_degil`, `yanlis_sayfa`, `kanitta_olmayan_sayi`, `deger_kumesi_disi`, `icd10_yok`.

`save_rag_result` ve `save_clinical_findings`'e `connection` verilirse commit etmezler (çağıran yönetir, yazım SAVEPOINT içinde yapılır); verilmezse kendi bağlantılarını açıp commit ederler.

## Modüller (`src/`)

| Dosya | Görev |
|---|---|
| `pdf_loader.py` | PDF'i pymupdf4llm ile sayfa sayfa markdown'a çevirir; yan yatırılmış tablo sayfalarını (metnin ≥%80'i aynı yönde döndürülmüş) bellek içinde düzelterek okur; döndürülmüş tablo başlıklarını ve dipnotlarını kurtarır. |
| `pdf_processing.py` | Kaynakçayı bulur ve çıkarır; kaynakçadan sonra gelen tablo/şekil içeriğini korur. |
| `chunking.py` | Sayfaları örtüşmeli chunk'lara böler; markdown tablolarını ortasından kesmez, devam parçalarına başlık satırını tekrar ekler; sürüm ve metin hash'i içeren `chunk_id` üretir (`CHUNKING_VERSION`). |
| `embeddings.py` | Sentence-transformers ile normalize embedding üretir (`minilm`, `bge-m3`). |
| `ingest.py` | PDF → temizleme → chunk → embedding → MySQL akışını çalıştırır (`python -m src.ingest`). |
| `database.py` | MySQL bağlantısı, şema kurulumu/geçişleri, VIEW'lar ve okuma/yazma fonksiyonları. |
| `retrieval.py` | Semantic search, BM25 ve RRF ile hybrid search. |
| `reranker.py` | Cross-Encoder ile yeniden sıralama (`bge-m3`, `minilm`) ve RRF ile karma sıralama. |
| `evidence_guard.py` | Kanıtın temel kullanılabilirliğini kontrol eder (uzunluk, isteğe bağlı skor eşiği ve kelime örtüşmesi). |
| `source_diversity.py` | Belge çeşitliliğini koruyarak bağlama girecek chunk'ları seçer. |
| `rag.py` | `answer_question`: retrieval, rerank, evidence guard, bağlam, LLM yanıtı ve citation kontrolü. |
| `citation_validator.py` | Yanıttaki `[n]` atıflarını doğrular; isteğe bağlı LLM ile iddia–kanıt uyumu (`verify_claim`). |
| `article_metadata.py` | 6 makalenin künye bilgilerini PDF'ten çıkarıp `makaleler`'e ekler (`--dry-run` / `--write`). |
| `load_translations.py` | Hazır LLM özet çevirilerini (JSON) denetleyip `makaleler.ozet_tr`'ye yazar (`--dry-run`); LLM çağırmaz. Sayı ve doğrulama yardımcıları `abstract_translation.py`'den gelir. |
| `clinical_candidates.py` | Tam metinden doz, ilaç/müdahale ve tanı yöntemi içeren aday cümleleri listeler (`data/review/` altına CSV). |
| `clinical_validation.py` | `validate_row`: klinik bulgu satırlarının kaynağa karşı denetimi. |
| `clinical_draft.py` | `data/clinical/referans_taslak.json`'ı denetleyip yalnızca CSV üretir; veritabanına yazmaz. |
| `clinical_pipeline.py` | Klinik çıkarım toplu işinin mantığı: aday hazırlama (güvenli alıntı parçaları), LLM çağrısı, bulgu bazında doğrulama, `klinik_bulgular` satırı. IO yapmaz. |
| `run_clinical_batch.py` | Toplu iş CLI'si: ilerleme, devam etme, önbellek, kilit, loglar (bkz. [Klinik çıkarım toplu işi](#klinik-çıkarım-toplu-işi)). |
| `data_quality.py` | `klinik_bulgular` için tekrar / sayısal / kaynak / tutarlılık işaretleri; `kalite_isaretleri`'ne yazar, bulgulara dokunmaz (bkz. [Veri kalitesi](#veri-kalitesi)). |
| `gold_set.py` | Gold set: alan listesi, deterministik katmanlı örneklem, etiketleme xlsx'i üretme/okuma, insan etiketlerinin doğrulanması. |
| `evaluate_extraction.py` | Gold set'e karşı alan bazında P/R/F1 (ham ve doğrulanmış seviye, birebir/gevşek; `insan_dogrulanmis` ve `llm_etiketli` ayrı kümeler), rapor ve CLI (bkz. [Çıkarım değerlendirmesi](#çıkarım-değerlendirmesi)). |
| `sunum.py` | Sunum VIEW'ları, `cikarim_reddedilenler` / `hata_onlem` tabloları, `kaynak_bul` procedure'ü ve red kayıt yazıcısı (`python -m src.sunum --kur`; bkz. [Hocaya gösterim](#hocaya-gösterim)). |
| `sunum_hoca.py` | Hoca sunum veritabanı (`kardiyo_sunum`): `sql/sunum_hoca/*.sql` dosyalarını sırayla, veritabanı adlarını `.env`'den doldurarak kurar (`python -m src.sunum_hoca --kur`). |

## Testler (`evaluation/`)

Hepsi proje kökünden `python -m evaluation.<dosya_adı>` ile çalıştırılır.

**MySQL gerektirenler** (`.env` ve kurulum adım 5 ile 6 tamamlanmış olmalı):

| Test | Ne yapar |
|---|---|
| `test_database_schema` | `init_schema`'yı iki kez çalıştırır; tabloları, sütunları, VIEW'ları ve hoca tablolarının satır sayılarının işlemden önce ve sonra aynı ve temizlik sonrası değerlerde olduğunu kontrol eder (`makaleler` `document_id` NULL, `tani_tedavi`, `sonuclar`, `semptom_iliskileri` = 0; `icd10_who` = 43; `tkd_kurallari` = 15). |
| `test_ingestion` | `src.ingest`'i iki kez çalıştırır (satır sayısı artmamalı); chunk, embedding ve `load_chunks_for_rag` çıktısını doğrular; hoca tablolarının satır sayılarını aynı değerlerle kontrol eder. Birkaç dakika sürer. |
| `test_makaleler` | Hocanın satırı (`document_id` NULL) olmadığını ve işlemlerden önce/sonra parmak izinin aynı kaldığını, bizim 6 satırı, `ozet_tr_kaynak` dağılımını (`orijinal` yalnızca TK, 4 `llm_ceviri` + model), ikinci yazımda çoğalma olmadığını ve çevirilerin silinmediğini kontrol eder. Not: bizim 6 satırı aynı değerlerle yeniden yazar. |
| `test_rag_kayit` | `save_rag_result`'ı gerçek `answer_question` (`use_llm=False`) çıktısıyla dener; reranker yer tutucuyla değiştirilir, model indirmez. Bütün yazımlar geri alınır. |
| `test_klinik_bulgular` | Referans taslağı `save_clinical_findings` ile yazar, bozuk satırların reddini, VIEW sayılarını ve hoca tablolarını kontrol eder. Bütün yazımlar geri alınır. |
| `test_clinical_pipeline` | Klinik çıkarım toplu işi, sahte LLM ile: aday ayırma, bulgu bazında red, `procedure` senaryoları, otomatik doğrulama izi, `alan_cok_uzun`, `<think>`/HTTP 400 ve `--max-tokens-param` davranışı, devam etme, kilit, `--no-save`, `--mock`, `--from-cache` (iki "bilgisayarda" aynı satırlar), `--dry-run`. Bütün yazımlar geri alınır; sonda sayılar karşılaştırılır. |
| `test_data_quality` | Veri kalitesi: normalizasyon, her kontrol için pozitif/negatif örnek, determinizm (ters ekleme sırası → aynı `grup_id`), sürüm hash'i, `satir_hash` çakışması, JSONL ve `--referans` modlarının tabloya yazmaması. Kalite tabloları yoksa transaction dışında kurar; sahte satırlar geri alınır. |
| `test_evaluate_extraction` | Çıkarım değerlendirmesi: elle hesaplanmış örnekle birebir TP/FP/FN/P/R/F1, birebir/gevşek mod, eşleme, "bulgu yok", yalnız `dogrulandi` adaylar, yanıtsız aday, örneklem determinizmi, xlsx üret-oku turu (Türkçe karakter), ham vs doğrulanmış, `--mock` raporu, `llm_etiketli` / `dogrulandi` ayrımı. Veritabanına yazmaz. |
| `test_sunum` | Değer alanları (p, GA, örneklem, etki ölçütü, ICD) pozitif/negatif ve gerçek adaylarla `validate_row`; tablo bulgusunda değer alanlarının yazılmaması; `--json-mode`; reddedilenler tablosuna deterministik ve tekrar etmeyen yazım; VIEW'lar ve `kaynak_bul` (harf duyarsız, joker ve SQL injection kapalı); `--mock` / `--no-save` kalıcı yazmaz. Sunum şeması yoksa transaction dışında kurar; yazımlar geri alınır. |
| `test_sunum_hoca` | Hoca sunum veritabanı: kurulum iki kez çalışınca view'lar aynı kalır; her view'da beklenen sütunlar, teknik sütun yok, satır sayıları doğrudan `klinik_bulgular` sorgusuyla tutarlı, ad sütunlarında `\|` yok; tablo satırı `evidence`'ı "Table N – satır adı" (`\|`, `<br>`, `**`, `<sup>` yok), metin `evidence`'ı değişmemiş; `measurements` yalnız Table 1 tablo satırları, metin ölçümleri `general_clinical_findings`'te; grup sırası PDF başlık sırasıyla aynı; 3 gruplu örnek veriyle yan yana view 3 grubu gösterir ve taban view'da veri kaybı olmaz. Sunum veritabanında yalnız hoca view'ları (`_` ile başlayan view ve teknik sütun yok), taban view `v_sunum_taban` kaynakta, eski `_findings_base` kurulumda silinir; sabit satır sayıları (51 / 102 / 66 / 13 / 44); `clinical_outcomes` sırası study finding → Table 2 → literature, `general_clinical_findings`'te literature sonda. Örnek veri transaction içinde yazılıp geri alınır. |
| `test_pilot_izolasyon` | Pilot testlerinin gerçek veri koruması (`evaluation/pilot_izolasyon.py`): gerçek pilot satırları yalnız testin açık transaction'ında silinir; autocommit açıksa / transaction yoksa hiçbir şey silinmez; silmeden sonra aynı bağlantıda `commit()`, `start_transaction()`, autocommit açma ve örtük commit yapan ifadeler (CREATE / DROP / ALTER / TRUNCATE / BEGIN / COMMIT / SET autocommit / LOCK …) engellenir; ROLLBACK sonrası ayrı bağlantıdan güvenlik kontrolü (sayı, id aralığı, parmak izi). Yasaklı işlemler gerçek satırı olmayan belge kimliği ve var olmayan tablo adlarıyla güvenli sınanır. |
| `test_pilot_gosterim` | `v_pilot_bulgular`: kurulu olduğunu, `import_records` ile yazılan pilot kayıtlarının (HbA1c iki grup, Table 2 HCR univariate/multivariate, bir ilaç satırı, iki metin kaydı) grup/değer/p, `taslak_notu`'dan çıkan `tablo_no` / `analiz` / `grup_n` sütunlarını, Türkçe etiketleri, iki bağlamın aynı listede göründüğünü, sıralamayı, `kaynak_bul`'un pilot satırlarını bulduğunu ve kalite sütunlarının `kalite_isaretleri` ile tuttuğunu doğrular. VIEW yoksa transaction dışında kurar; yazımlar geri alınır. |

**MySQL gerektirmeyenler:**

| Test | Ne yapar |
|---|---|
| `test_chunking` | Tablo farkındalıklı chunk'lama: tablosuz metin eskisiyle aynı, tablolar ortasından kesilmiyor, devam parçalarında başlık satırı var, 1500 sınırı aşılmıyor; 6 PDF'te 242 chunk (PDF'ler gerekir). |
| `test_rotated_pages` | Yan yatırılmış tablo sayfaları: yalnızca AF s.24, 25, 27, 28 ve OHCA s.4 düzeltiliyor, diğer sayfalar birebir aynı; tablolar doğru yönde, kaybolan başlık/dipnotlar geri geliyor, yinelenen metin yok (PDF'ler gerekir). |
| `test_pipeline` | `answer_question` ve evidence guard için mock'lu birim testler (model veya LLM gerekmez). |
| `test_pdf_processing` | 6 PDF'in yüklenmesi, kaynakça temizliği ve chunk'ları; ayrıca `test_questions.csv`'deki kanıt eşleştirmelerini yeniden üretilen chunk'larla doğrular (PDF'ler gerekir; `--pdf-dir` ile farklı klasör verilebilir). Chunk'lama sürümü değiştikten sonra eşleştirmeler yenilenene kadar "Stale or unknown chunk id" ile başarısız olur (aşağıya bakın). |
| `test_reference_cleaning` | Kaynakça temizliğinin PDF bazında raporu (PDF'ler gerekir). |
| `test_real_pdfs` | 6 PDF üzerinde embedding + `answer_question(use_llm=False)`; ilk çalıştırmada `minilm` reranker modelini indirir. |
| `test_real_retrieval` | 3 örnek metinle retrieval + rerank denemesi; ilk çalıştırmada `minilm` reranker modelini indirir. |

`evaluate_rag.py` ve `evidence_threshold.py` test değil, değerlendirme araçlarıdır (soru setiyle retrieval raporu ve eşik analizi).

`chunk_remap_report.py` de test değildir: chunk'lama sürümü değişince `test_questions.csv`'deki `chunk_id` atıfları için eski → yeni eşleme raporu üretir (MySQL gerekir; `test_questions.csv`'yi değiştirmez). Bkz. [Chunk sürümü değiştikten sonra](#chunk-sürümü-değiştikten-sonra).

## Chunk sürümü değiştikten sonra

Metin çıkarma veya chunk'lama sürümü değiştiğinde (şu an `markdown-rotated-v3` + `chunking-table-v1`) bütün `chunk_id`'ler değişir, çünkü kimlik sürüm anahtarını içerir. Rapor her zaman en eski atıftan doğrudan `pdf_belgeleri`'deki **en güncel** sürüme eşler; aradaki sürümlerin veritabanında bulunup bulunmaması sonucu değiştirmez. `evaluation/test_questions.csv`'deki kanıt eşleştirmeleri eski kimliklere işaret ettiği için yenilenmelidir. Değişiklik main'e geldikten sonra:

```powershell
git pull
python -m src.ingest
python -m evaluation.chunk_remap_report
```

1. `src.ingest` yeni sürümün chunk ve embedding'lerini yazar; eski sürüm veritabanında kalır (rapor eski metni oradan okur, bu yüzden ingest'ten önce veritabanında eski sürümün bulunması gerekir).
2. `chunk_remap_report` `data/review/chunk_remap.csv` dosyasını üretir:
   - `kimlik_degisti`: aynı sayfada metni birebir aynı yeni chunk var; atıf `yeni_chunk_id` ile değiştirilebilir.
   - `elle_bakilmali`: metin değişmiş (tablolu sayfalar); aynı sayfadaki yeni chunk'lar benzerlik oranıyla aday olarak listelenir, eşleştirme elle yapılır.
   - `veritabaninda_yok`: atıf veritabanındaki hiçbir sürümde yok (daha eski bir metinden kalmış).
   - `atif_yerleri`: atfın geçtiği soru ve sütun (ör. `4:evidence_matches_json`).
3. Eşleştirmeler `test_questions.csv`'de güncellendikten sonra `python -m evaluation.test_pdf_processing` yeniden geçmelidir.

## Klinik çıkarım toplu işi

`src.clinical_candidates`'in 660 adayını LLM ile işler; `validate_row`'dan geçen bulgular `klinik_bulgular`'a **`review_status = 'pending'`** ile yazılır. İnsan onayı üretilmez; otomatik atanan değerler (`mudahale_turu`, dil, `chunk_id` seçimi, tablo alıntısının genişletilmesi) `taslak_notu.otomatik_alanlar`'da işaretlenir.

- **Yanıt şeması:** `clinical_extraction.FIELDS` + `procedure` (adı açıkça geçen girişimsel/cerrahi/cihaz işlemi: catheter ablation, PCI, CABG, stent, cardioversion, LAAO…). Bir bulguda `drug`, `procedure`, `diagnostic_method`'dan **tam olarak biri** dolu olmalıdır: hiçbiri yoksa `mudahale_alani_yok`, birden fazlaysa `birden_fazla_mudahale` ile reddedilip loglanır. `procedure` → `mudahale_<dil>`, `mudahale_turu = 'tedavi'`; `diagnostic_method` → `'tanı testi'`; `drug` → `'tedavi'`. Her dolu alan (procedure dahil) alıntıda birebir geçmelidir; `procedure` ayrıldıktan sonra bulgu mevcut `_validate_response`'tan aynen geçer.
- **Değer alanları:** `p_value` → `p`, `confidence_interval` → `ci95` (alıntıdaki metin aynen); `sample_size` → `hasta_sayisi` (deterministik tamsayı: "n = 372", "372 patients" → 372; "1,234 patients" gibi binlik ayırıcılı değer `validate_row`'un sayı kontrolüyle çakıştığı için **yazılmaz**, ham değer ve sayı `taslak_notu.deger_alanlari`'nda); `effect_measure` → `etki_turu` (sabit eşleme `etki-esleme-v1`: odds ratio → OR, risk ratio → RR, hazard ratio → HR, incidence rate ratio → IRR, AUC / C-statistic → "AUC / C-istatistigi"; eşleşmezse boş, ham metin notta). `icd10`: tanı adı `icd10_who`'daki İngilizce veya Türkçe adla normalize **birebir** eşitse kod; birden fazla kod eşleşirse boş; fuzzy yok. Hepsi alıntıda birebir olmalıdır. `yon` model tarafından doldurulmaz; `outcome_type` aşağıdaki deterministik eşlemeyle doldurulur.
- **Tablo bulgularında değer alanları sütunlara yazılmaz:** tipli tablo kuralı bu alanların hangi sütundan geldiğini doğrulayamaz. Değerler `taslak_notu.tablo_yazilmayan_alanlar`'a "tablo: yeni alanlar sütun kaynağı doğrulanamadığı için yazılmadı" notuyla konur, `otomatik_alanlar`'da işaretlenir ve `summary.json`'da `tablo_yeni_alan_yazilmadi` olarak sayılır; bulgunun geri kalanı tipli tablo kuralından geçerse normal kaydedilir.
- **Sürüm:** `pipeline_version` = `clinical-pipeline-v1-<prompt+şema hash'i>`. Prompt veya şema değişince kendiliğinden değişir; önceki sürümün `state.jsonl` kayıtları ve `responses/<eski sürüm>/` önbelleği kullanılmaz (adaylar yeniden işlenir, eski satırlar silinmez). Değer alanlarının eklenmesiyle sürüm `clinical-pipeline-v1-a8c57cae5612` olmuştu; çıkarım talimatı v2 (aşağıda) prompt'u değiştirdiği için sürüm yine değişti (şu an `clinical-pipeline-v1-2eb4f4f5413e`). Önceki sürümlerle alınmış yanıtlar (ör. 20 adaylık pilot) geçersizdir; adaylar yeniden işlenir.
- **Çıkarım talimatı v2 (prompt'a eklenen genel kurallar):** `diagnostic_method` tanı için kullanılan test, cihaz, skor veya biyobelirteçtir; istatistik/analiz yöntemleri (ROC analysis, logistic regression, multivariate analysis, cut-off analysis) bu alana yazılmaz. `diagnosis` testin tespit ettiği durumdur; hasta grubunu tanımlayan hastalık (`patients with X`) `patient_group`'a gider. Bir cümlede birden fazla performans ölçüsü (sensitivity, specificity, PPV, NPV, accuracy, AUC) veya test varsa her (test, ölçü, sayı) ayrı bulgudur (`clinical_outcome` = ölçünün cümledeki adı, `numeric_value` = sayısı). `clinical_outcome` cümledeki ifadenin birebir kopyasıdır; açık sonuç varsa boş bırakılmaz. Çalışma amacı, tanım, sınıflama/evre tanımı, tasarım/hasta seçimi cümleleri bulgu değildir (`findings: []`). Doğrulama kuralları gevşetilmedi.
- **`yontem_istatistik` (sıkılaştırma):** `diagnostic_method` sabit istatistik yöntemi listesine (ROC, regression, multivariate/univariate, cut-off analysis, Kaplan-Meier, Cox…) uyuyorsa bulgu `validate_finding`'de reddedilir ve `hata_turu = 'yontem_istatistik'` ile loglanır; `hata_onlem` eşlemesine eklendi (`onlem-v2`).
- **`outcome_type` / `etki_turu` eşlemesi (`sonuc-turu-v1`, `build_row`; model doldurmaz):** öncelik CCT tablo ailesi `'öneri'` > **tanısal performans** > **uygulama oranı**. *Tanısal performans:* `clinical_outcome` veya `effect_measure` şu terimlerden birini içerir: sensitivity, specificity, positive/negative predictive value, PPV, NPV, accuracy, area under the curve, AUC, AUROC, C-statistic, duyarlılık, özgüllük, doğruluk ("sensitivity analysis" hariç); `etki_turu` boşsa AUC türleri → `AUC / C-istatistigi`, diğerleri → `oran / yuzde` (tablo satırlarında `etki_turu` sütuna yazılmaz). *Uygulama oranı:* müdahale dolu ve `clinical_outcome` "use of / utilization / uptake / uygulanma oranı / kullanım oranı" içeriyor, ya da `clinical_outcome` boşken alıntı müdahaleye bağlı dar kalıbı içeriyor ("rate of <müdahale>", "<müdahale> rate", "use of <müdahale>", "<müdahale> kullanım oranı"…); emin değilse boş. Hiçbiri tutmazsa `outcome_type` boş kalır; `yon` doldurulmaz. Atanan değerler `otomatik_alanlar`'da ve `taslak_notu.sonuc_turu_eslemesi`'nde işaretlenir, doğrulama izine eklenir ve `validate_row`'un değer kümesi kontrolünden geçer.
- **Otomatik doğrulama izi:** kaydedilecek her satırın `taslak_notu.otomatik_dogrulama` alanında geçtiği kontrollerin listesi (alıntı ve alanlar birebir, güvenli parça içinde, `validate_source`, `validate_row`: ilişki / değer kümeleri / ICD / kanıt sayfada / sayılar kanıtta, sütun sınırı), kontrol sürümleri ve `"insan_onayi": "yok"` bulunur. Zaman damgası içermez; aynı önbellekle her bilgisayarda aynıdır. Otomatik kontroller klinik doğruluğu kanıtlamaz.

```powershell
python -m src.run_clinical_batch --dry-run              # hazırlık raporu; LLM ve yazım yok
python -m src.run_clinical_batch --mock --limit 20      # sahte LLM; yazımlar sonunda geri alınır
python -m src.run_clinical_batch --limit 20 --no-save   # gerçek LLM; yalnızca önbellek + önizleme
python -m src.run_clinical_batch                        # gerçek LLM; kayıt (kaldığı yerden devam eder)
python -m src.run_clinical_batch --from-cache           # LLM yok; önbellekteki yanıtlardan kayıt
```

- **LLM'e gitmeden ayrılanlar** (sebebiyle `excluded.jsonl`'a ve `cikarim_reddedilenler`'e yazılır): makale metaverisi (7; `clinical_candidates.mark_article_metadata`, makale başlığı `makaleler.baslik`'tan iletilir), başlık satırları (22), sayfa aşan cümleler (18), karmaşık tablolar (16) ve doğrulanabilir parçasında kalıp kalmayanlar (`guvenli_parca_yok`, 1). Kalan 596 aday LLM'e gider (aday başına bir çağrı).
- **Güvenli alıntı parçaları:** `validate_row` kanıtı markdown'u temizlenmiş sayfada, `validate_source` ham chunk'ta arar. LLM'e, aday cümlesinin ikisinde de birebir geçen parçaları (`allowed_quote_spans`) verilir; alıntı tek bir parçanın içinde olmalıdır. Alıntı birden fazla chunk'ta geçiyorsa en küçük `chunk_index`'li chunk seçilir.
- **Hata yönetimi:** bir bulgunun reddi yalnızca o bulguyu, bozuk yanıt veya API hatası yalnızca o adayı etkiler (geçici hatalar 3 kez yeniden denenir). İş yalnızca ayar hatalarında, HTTP 400/422'de (olası sebepler: `enable_thinking`, `json_object` desteği veya `max_completion_tokens` parametresi; mesaj hangi seçenekle deneneceğini söyler) ve üst üste 5 aday hatasında durur; aynı komut kaldığı yerden devam eder. Sütun sınırını aşan satır `alan_cok_uzun` ile reddedilir.

| Seçenek | Anlamı |
|---|---|
| `--limit N` | Bu koşuda en fazla N aday (makaleler arasında sırayla dağıtılır) |
| `--makale X` / `--aday ID` | Yalnızca bu makale (`makale_id` veya dosya adı öneki) / aday; tekrarlanabilir |
| `--dry-run` | Ayrılan/işlenecek/önbellekte olan sayıları; LLM ve veritabanı yazımı yok |
| `--mock` | Sahte LLM; veritabanı yazımı tek transaction'da yapılıp **ROLLBACK** edilir |
| `--no-save` | Gerçek LLM; yanıt önbelleğe, kaydedilecek satırlar `would_save.jsonl`'a; veritabanına yazmaz |
| `--from-cache` | LLM'i hiç çağırmaz, API anahtarı istemez; önbellekte olmayan aday `onbellekte_yok` |
| `--retry-failed` / `--reprocess` | `hata` durumundakileri yeniden dene / bu sürümle işlenmişleri yeniden işle |
| `--force` | İlk çalıştırma kuralını aş |
| `--think` / `--no-think` | Qwen3 thinking (varsayılan `--no-think`: `enable_thinking=false` gönderilir; `<think>` blokları her durumda ayıklanır) |
| `--max-tokens-param {max_completion_tokens,max_tokens}` | Token sınırı parametresinin adı (varsayılan `max_completion_tokens`; eski sunucular `max_tokens` ister) |
| `--json-mode {json_object,none}` | `none`: `response_format` gönderilmez (bazı sunucularda uzun klinik cümlelerde HTTP 500 görüldü); yanıt yine aynı JSON kurallarıyla ayrıştırılıp doğrulanır. Sürüme ve önbellek anahtarına girmez; önbellek dosyasına ve `summary.json`'a yazılır |
| `--model`, `--timeout`, `--max-retries`, `--max-consecutive-errors` | Model adı (varsayılan `.env` `LLM_MODEL`) ve sınırlar |
| `--unlock` | Kalmış kilit dosyasını siler |

**İlk çalıştırma kuralı:** gerçek LLM ile bu sürümde hiç aday işlenmemişse ve `--limit` verilmemişse iş başlamaz ("önce --limit 20 ile deneyin"); `--force` ile aşılır.

**Çıktılar** (`data/review/clinical_batch/`, git'e girmez):

- `state.jsonl`: aday başına durum (`kaydedildi`, `yanit_alindi`, `bulgu_yok`, `reddedildi`, `hata`); bozuk satırlar uyarıyla atlanır.
- `responses/<sürüm>/<model>/`: LLM yanıt önbelleği (aday_id, sürüm, model, ham yanıt; göreli yol, başka bilgisayara kopyalanabilir).
- `runs/<run_id>/`: `summary.json` (sebep ve `hata_turu` bazında sayılar), `would_save.jsonl`, `rejected.jsonl`, `excluded.jsonl`, `errors.jsonl`, `warnings.jsonl`.
- `run.lock`: PID, makine ve zaman; sahibi çalışmayan kilit otomatik temizlenir.
- `--mock` çıktıları `mock/` alt klasörüne gider; gerçek `state.jsonl` ve önbelleğe dokunmaz.

**Reddedilenler veritabanında:** gerçek LLM ve `--from-cache` koşuları, LLM öncesi ayrılan adayları (`on_eleme`) ve doğrulamadan geçemeyen bulguları (`yanit_dogrulama`, `satir_olusturma`, `validate_row`, `save`) `cikarim_reddedilenler` tablosuna da yazar (`rejected.jsonl` aynen kalır). Aday redleri kayıtla aynı transaction'da yazılır; `satir_hash` çalıştırma kimliği ve zamandan bağımsızdır, `UNIQUE` + `INSERT IGNORE` ile aynı red ikinci kez yazılmaz. `--mock`'ta yazım geri alınır; `--no-save` ve `--dry-run` yazmaz. Bozuk JSON / kapanmamış `<think>` gibi iletim hataları yalnızca `errors.jsonl`'dadır. Tablo yoksa iş LLM çağrısından önce durur: **önce bir kez `python -m src.sunum --kur`**. `LLM_API_KEY` dolu olmalıdır; yerel sunucu anahtarsız çalışıyorsa herhangi bir değer (ör. `local`) yazılabilir.

**LLM'i çalıştıran kişi için:**

1. Kurulumu yapın (adım 1–6; aynı 6 PDF, aynı kod sürümü) ve `.env`'de `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`'i doldurun.
2. Kendi veritabanınıza da yazacaksanız (`--no-save` olmadan) bir kez: `python -m src.sunum --kur` (yalnız `--no-save` ile çalışacaksanız gerekmez).
3. Deneme: `python -m src.run_clinical_batch --limit 20 --no-save` (sunucu uzun cümlelerde HTTP 500 verirse `--json-mode none` ile deneyin).
4. `data/review/clinical_batch/runs/<run_id>/` altındaki `would_save.jsonl` ve `rejected.jsonl`'a bakın; sorun yoksa devam edin.
5. Tam koşu: `python -m src.run_clinical_batch --no-save` (kesilirse aynı komut kaldığı yerden devam eder; kendi veritabanınıza da yazmak isterseniz `--no-save` olmadan çalıştırın).
6. `data/review/clinical_batch/responses/` klasörünü zip'leyip gönderin.

**Önbellekten yükleyen kişi için:**

1. Bu veritabanında bir kez: `python -m src.sunum --kur` (reddedilenler tablosu ve sunum VIEW'ları; yoksa kayıt adımı açık bir mesajla durur).
2. Zip'i `data/review/clinical_batch/responses/` olacak şekilde açın.
3. Kontrol: `python -m src.run_clinical_batch --from-cache --dry-run` (`onbellekte_yok` 0'a yakın olmalı; yüksekse PDF'ler veya kod sürümü farklıdır).
4. Kayıt: `python -m src.run_clinical_batch --from-cache`

## Veri kalitesi

`klinik_bulgular`'daki satırlarda tekrarları, şüpheli sayıları, eksik kaynakları ve tutarsızlıkları bulur ve **işaretler**. Hiçbir bulgu silinmez veya değiştirilmez, `review_status`'a dokunulmaz; `klinik_bulgular`'a yalnızca `SELECT` atılır. LLM kullanılmaz.

**Kurulum — her veritabanında bir kez:**

```powershell
python -m src.data_quality --kur
```

`sql/kalite_core.sql`'i çalıştırır (tekrar çalıştırılabilir): `kalite_isaretleri`, `kalite_calistirmalari` tabloları ve `v_kalite_ozet` (işaret + bulgu + PDF adı, en son kontrol sürümü), `v_kalite_bulgu` (bulgu başına hata/uyarı/bilgi sayıları) VIEW'ları. `init_schema`'dan bağımsızdır.

```powershell
python -m src.data_quality --dry-run                                   # rapor; tabloya yazmaz
python -m src.data_quality                                             # kalite_isaretleri'ni yeniler (rag satırları)
python -m src.data_quality --hepsi --makale DOI-10.1001/jama.2024.22451
python -m src.data_quality --jsonl data/review/clinical_batch/runs/<run_id>/would_save.jsonl
python -m src.data_quality --batch-dir data/review/clinical_batch      # + satir_hash çakışmaları
python -m src.data_quality --referans --dry-run                        # referans taslak (doğrulanmamış)
```

- **İşaretler türetilmiş veridir.** Kimlik `satir_hash`'tir (veritabanı `id`'si makineden makineye değişir); `grup_id` üyelerin `satir_hash`'lerinden türetilir. Aynı `kontrol_surumu` ile yeniden çalıştırınca o sürümün kapsamdaki işaretleri silinip tek transaction'da yeniden yazılır; sonuç deterministiktir. `kontrol_surumu = kalite-v1-<hash>`: kontrol listesi, müdahale eşanlamlıları, birim yazımları ve eşikler değişince kendiliğinden değişir; kontrol mantığı (ayrıştırma, kural koşulları) değişince `src/data_quality.py`'deki `RULES_REVISION` elle artırılır. Bulgu silinirse işaretleri de silinir (`ON DELETE CASCADE`).
- **Kategoriler ve seviyeler** (`hata` / `uyari` / `bilgi`):
  - `tekrar`: aynı makale + müdahale + doz + değer/birim + hasta grubu + sonuç (normalize; temsilci `bilgi`, diğerleri `uyari`), metin–tablo ve farklı sayfa kopyaları, yalnızca müdahale adı tekrarları, benzer hasta grubu/sonuç (`tekrar_benzer`: Jaccard ≥ 0.8; `--embedding` ile yerel minilm cosine ≥ 0.92 — ikisi de yalnızca `bilgi`), `satir_hash` çakışması.
  - `sayisal`: yüzde > 100, p 0–1 dışında, "p<001" biçimi, GA alt > üst, değer GA dışında, OR/RR/HR/IRR ≤ 0, AUC 0–1 dışında, negatif değer, birimsiz değer (`etki_turu` boşsa `bilgi`), birimsiz doz, ayrıştırılamayan / binlik-ondalık belirsiz sayı.
  - `kaynak`: boş kaynak alanı, `chunk_id` yok, belge/chunk yok, chunk eski sürümde, chunk başka belgede, kanıt chunk'ta değil, sayfa uyuşmuyor, makale–belge uyuşmuyor, `validate_row` artık geçmiyor.
  - `tutarsizlik`: aynı makale/müdahale/hasta grubu için farklı doz, aynı hasta grubu için farklı `hasta_sayisi`, aynı sonuç için farklı değer, `mudahale_turu` ile `taslak_notu`'daki alan kaynağı uyuşmuyor, okunamayan `taslak_notu`, dil uyuşmazlığı.
- **Normalizasyon:** NFKC + küçük harf (Türkçe İ/ı korunur), boşluk/noktalama/tire, markdown kalıntıları; birim yazımları (`µg`/`ug` → `mcg`, `/d`/`daily` → `/day`, `percent` → `%`); ondalık virgül; müdahale eşanlamlıları yalnızca kesin eşdeğerler (PCI, CABG, DOAC/NOAC, LAAO, VKA, beta-blocker yazımları, aspirin/acetylsalicylic acid, ACE inhibitor).
- **Girdiler:** varsayılan veritabanı (`cikarma_yontemi = 'rag'`; `--hepsi` ile hepsi). `--jsonl` toplu işin önizlemesini **tabloya yazmadan** inceler (`--jsonl-ile-db` kayıtlı satırlarla birlikte; `--offline` veritabanısız, kaynak kontrolü yok). `--referans` yalnızca `--dry-run` ile çalışır; satırlar `validate_row`'a sokulmadan okunur ve bütün çıktılar "**referans_taslak: doğrulanmamış veri**" etiketini taşır.
- **`satir_hash` çakışması:** `satir_hash` `taslak_notu`'nu kapsamadığı için aynı adaydan farklı İngilizce sonuçlu iki bulgu tek satıra iner. `--batch-dir`, toplu işin `would_save.jsonl` kayıtlarında aynı hash'e sahip farklı bulguları sayar, kayıtlı satırı `hash_cakismasi_kayip_bulgu` ile işaretler ve `state.jsonl`'daki `zaten_var` ipucunu özete yazar.
- **Çıktılar** (`data/review/kalite/<zaman>-<mod>/`, git'e girmez): `isaretler.csv` (insan incelemesi için), `isaretler.json`, `gruplar.csv`, `ozet.json` (kategori × seviye sayıları, tekrar grubu, çakışma).

## Çıkarım değerlendirmesi

Otomatik çıkarımı insan tarafından doğrulanmış bir **gold set**'e karşı alan bazında precision / recall / F1 ile ölçer. LLM çağrılmaz, veritabanına yazılmaz. `openpyxl` gerekir (`requirements.txt`'te).

```powershell
python -m src.evaluate_extraction --gold-olustur                       # data/review/gold/gold_etiketleme.xlsx
python -m src.evaluate_extraction --gold-yukle data/review/gold/gold_etiketleme.xlsx   # -> data/clinical/gold_set.json
python -m src.evaluate_extraction --degerlendir --from-cache           # toplu iş önbelleğinden (model koşusundan sonra)
python -m src.evaluate_extraction --degerlendir --db                   # kayıtlı klinik_bulgular (yalnız dogrulanmis seviye)
python -m src.evaluate_extraction --degerlendir --mock                 # sahte LLM; gerçek performans DEĞİL
```

- **Gold set yalnızca insan tarafından doğrulanır.** Kod hiçbir satırı `dogrulandi` yapmaz; üretilen xlsx'te her satır `taslak` başlar, referans taslaktan gelen değerler sarı ve "referans_taslak (doğrulanmadı)" işaretlidir. Yalnızca `durum = dogrulandi` ve hatasız adaylar metriğe girer. `data/clinical/gold_set.json` git'e girer; xlsx ve raporlar `data/review/` altındadır (git'e girmez).
- **`llm_etiketli` durumu:** yapay zekânın (PDF'e bakarak) doldurduğu, insan doğrulaması olmayan adaylar. Değerlendirmede iki ayrı küme raporlanır ve asla karıştırılmaz: **`insan_dogrulanmis`** (yalnız `dogrulandi`) ve **`llm_etiketli`**. İkincisi her yerde "*yapay zekâ etiketli, insan doğrulaması yok; sonuç sistem-etiketleyici uyumudur, gerçek doğruluk değildir*" uyarısıyla basılır; ayrı bir hash'i vardır ve insan `gold_hash`'ini etkilemez. `dogrulandi` yalnızca elle kontrol eden insan tarafından yazılır.
- `p`, `ci95`, `hasta_sayisi` ve `etki_turu` artık sistem tarafından çıkarıldığı için değerlendirilir; yalnız `icd10` (tanıdan türetilir, gold'da sütunu yok) "değerlendirilemez".
- **Birim = aday cümlesi.** Bir adayın bütün bulguları etiketlenir; bulgusu yoksa `bulgu_yok = EVET`. Değerler aday cümlesinden **birebir** kopyalanır (cümlede geçmeyen değer uyarı verir); bir bulgu = tek müdahale. Referansın cümlede birebir geçmeyen önerileri `taslak_referans` sütunundadır. Var olan xlsx'in üzerine yazılmaz; `gold_set.json` varsa yeni dosya ondaki etiket ve durumlarla doldurulur.
- **Örneklem** (`--n 60 --seed 42`): LLM'e giden adaylardan ~2/3 referans taslakla eşleşenlerden, ~1/3 diğerlerinden; her havuzda (dosya, `kaynak_turu`) katmanlarına orantılı, her katmandan en az 1; katman içinde `sha256(seed|aday_id)` sırası (deterministik).
- **Alan listesi** tek yerde: `src/gold_set.py` `GOLD_FIELDS`. Sistemin henüz çıkarmadığı alanlar (`p`, `ci95`, `hasta_sayisi`, `etki_turu`, `icd10`) etiketlenir ama "değerlendirilemez" raporlanır ve ortalamalara girmez; çıkarıma eklenince yalnızca o satırdaki eşleme doldurulur.
- **İki seviye:** `ham` (LLM yanıtındaki bütün bulgular, doğrulama öncesi) ve `dogrulanmis` (`validate_row`'dan geçen, kaydedilecek satırlar); aradaki fark doğrulamanın etkisidir.
- **Eşleme:** aynı aday içinde gold ve sistem bulguları normalize müdahale ile eşlenir (en çok çift, sonra en çok alan uyuşması; deterministik). Normalizasyon `data_quality` ile aynıdır.
- **Sayım** (alan f): eşlenmiş çiftte ikisi dolu ve eşit → TP; ikisi dolu ve farklı → FP + FN; yalnız sistem dolu → FP; yalnız gold dolu → FN; eşlenmemiş sistem bulgusunun dolu alanları FP, eşlenmemiş gold bulgusununkiler FN. P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2PR/(P+R); 0/0 → tanımsız. Micro = aktif alanların toplamı; macro = tanımlı F1'lerin ortalaması; ayrıca bulgu ve aday ("bulgu yok" isabeti) düzeyi. **Birebir** (ana) ve **gevşek** (içerme veya token Jaccard ≥ 0.5) mod; micro F1 için seed'li bootstrap %95 aralığı (`--bootstrap 1000`, 0 = kapalı).
- **Yanıtı olmayan gold adayları** (önbellekte yok / LLM'e gitmeyen) raporda listelenir: ana metrik yanıtı olan adaylar üzerindendir, "konservatif" satırda boş çıktı (FN) sayılırlar.
- **Rapor** (`data/review/degerlendirme/<zaman>-<kaynak>/`): `metrics.json` (sürümler: `pipeline_version`, model, `gold_hash`, alan listesi ve normalizasyon sürümü), `alanlar.csv`, `kirilim.csv` (makale ve `kaynak_turu`), `hatalar.csv` (FP/FN/yanlış değer örnekleri).

## Hocaya gösterim

**Kurulum (her veritabanında bir kez; tekrar çalıştırılabilir):**

```powershell
python -m src.sunum --kur
```

Kurar: `cikarim_reddedilenler`, `hata_onlem` (hata türü → halüsinasyon türü → önlem eşlemesi, `onlem-v2`), kalite tabloları, `v_bulgu_notu`, `v_cardiorag_bulgular`, `v_bulgu_kaynak`, `v_halusinasyon_onlemleri`, RAG için `v_rag_sorgular`, `v_rag_kaynaklar`, `v_rag_iddialar` VIEW'ları, pilot kayıtlar için `v_pilot_bulgular` ve `kaynak_bul(arama)` procedure'ü. `klinik_bulgular`'ın yapısına, `v_tani_tedavi` / `v_sonuclar`'a ve `halusinasyon_degerlendirme`'ye dokunmaz. Tablolar model koşusu (`python -m src.run_clinical_batch` veya `--from-cache`) ve kalite koşusundan (`python -m src.data_quality`) sonra dolar. Bütün satırlar `review_status = 'pending'`; İngilizce sonuçlar çevrilmemiştir (`sonuc_kaynagi` sütunu belirtir).

MySQL Workbench'te:

```sql
USE kardiyo_tam_metin;

-- 1) Bütün bulgular, kaynağıyla (PDF, başlık, sayfa, DOI)
SELECT bulgu_id, filename, makale_basligi, sayfa, kanit_turu, COALESCE(mudahale_tr, mudahale_en) AS mudahale,
       mudahale_turu, tani, icd10, hasta_grubu, hasta_sayisi, doz, sonuc, sonuc_kaynagi, etki_turu, deger, unit,
       ci95, p, review_status, doi_link
FROM v_cardiorag_bulgular ORDER BY filename, sayfa;

-- 2) Tanı testleri
SELECT filename, sayfa, COALESCE(mudahale_tr, mudahale_en) AS tani_yontemi, tani, icd10, hasta_grubu, deger, unit, kanit_cumle
FROM v_cardiorag_bulgular WHERE mudahale_turu = 'tanı testi' ORDER BY filename, sayfa;

-- 3) Tedaviler (ilaç ve girişimsel işlem)
SELECT filename, sayfa, COALESCE(mudahale_tr, mudahale_en) AS tedavi, doz, sure, hasta_grubu, comparator, sonuc, kanit_cumle
FROM v_cardiorag_bulgular WHERE mudahale_turu IN ('tedavi', 'arka plan tedavisi') ORDER BY filename, sayfa;

-- 4) Sonuçlar (değer, %95 GA, p, örneklem)
SELECT filename, sayfa, COALESCE(mudahale_tr, mudahale_en) AS mudahale, sonuc, sonuc_kaynagi, etki_turu, deger, unit,
       ci95, p, hasta_sayisi
FROM v_cardiorag_bulgular WHERE sonuc IS NOT NULL OR deger IS NOT NULL OR p IS NOT NULL ORDER BY filename, sayfa;

-- 5) Halüsinasyon önlemleri: hangi hata kaç kez yakalandı, hangi önlemle (hepsi kayda girmedi)
SELECT halusinasyon_turu, uygulanan_onlem, hata_turu, alt_tur, asama, yakalanan, sonuc, ornek_dosya, ornek_sayfa, ornek_cumle
FROM v_halusinasyon_onlemleri ORDER BY yakalanan DESC;

SELECT asama, COUNT(*) AS kayda_girmeyen FROM cikarim_reddedilenler GROUP BY asama ORDER BY kayda_girmeyen DESC;

-- 6) Kaynak bulma: müdahale / tanı / hasta grubu / sonuç / kanıt cümlesinde arar (büyük/küçük harf duyarsız)
CALL kaynak_bul('apixaban');

-- Tek bir bulgunun izi: PDF, sayfa, chunk, kanıt ve aday cümlesi, geçtiği otomatik kontroller, sürüm, model
SELECT * FROM v_bulgu_kaynak WHERE bulgu_id = 1;

-- 7) Kalite işaretleri (önce: python -m src.data_quality)
SELECT * FROM v_kalite_bulgu ORDER BY hata_sayisi DESC, uyari_sayisi DESC;
SELECT kategori, seviye, kontrol_kodu, COUNT(*) AS sayi FROM v_kalite_ozet
GROUP BY kategori, seviye, kontrol_kodu ORDER BY sayi DESC;

-- 8) RAG soru-cevap: her soru tek satır (durum, cevap, kanıt, kaynaklar, iddia sayıları)
SELECT query_id, soru, soru_tipi, durum, durum_aciklama, cevap, evidence_status, guard_reason, llm_model,
       bulunan_kaynak, kullanilan_kaynak, kullanilan_kaynaklar, citation_correct,
       iddia_sayisi, iddia_supported, iddia_partially_supported, iddia_not_supported, iddia_belirsiz,
       hata_turu, uygulanan_onlem, duzeldi_mi, created_at
FROM v_rag_sorgular ORDER BY created_at DESC;

-- Durum dağılımı
SELECT durum, durum_aciklama, COUNT(*) AS soru_sayisi FROM v_rag_sorgular GROUP BY durum, durum_aciklama;

-- Bir sorunun kaynakları (bulunan bütün adaylar; used_in_context = 1 olanlar LLM bağlamına girdi)
SELECT source_rank, used_in_context, retrieval_score, rerank_score, dosya, sayfa, chunk_baslangic
FROM v_rag_kaynaklar WHERE query_id = '...' ORDER BY source_rank;

-- İddialar ve destekleyen kaynak; desteklenmeyen / belirsiz olanlar
SELECT query_id, soru, iddia_no, claim_text, claim_supported, citation_correct, destekleyen_dosya, destekleyen_sayfa, hata_turu
FROM v_rag_iddialar WHERE claim_supported IS NULL OR claim_supported <> 'supported' ORDER BY query_id, iddia_no;
```

RAG VIEW'ları salt okunurdur; değerleri tablolarda (`rag_sorgulari`, `rag_sorgu_kaynaklari`, `halusinasyon_degerlendirme`) nasılsa öyle gösterir, yorum katmaz. `halusinasyon_degerlendirme`'den yalnız `kayit_turu = 'soru_cevap'` satırları okunur.

- **`v_rag_sorgular`** soru başına tek satırdır; kaynak ve iddia sayıları alt sorgularla toplanır. `durum` = `ham_cikti.$.generation_status`. `durum_aciklama` ayrı bir Türkçe açıklama sütunudur: generated → "Cevap üretildi; atıf ve iddia kontrollerinden geçti", citation_validation_failed → "Atıf kontrolünden geçmediği için cevap gösterilmedi", claim_validation_failed → "İddia kontrolünden geçmedi ya da kontrol hata verdi; cevap gösterilmedi", insufficient_evidence → "Yeterli kanıt bulunamadı; cevap verilmedi", retrieval_only → "Yalnız kaynak araması yapıldı; LLM çağrılmadı", generation_failed → "LLM cevabı üretilemedi".
- **`cevap` reddedilen cevapta boştur** (NULL); modelin ürettiği ama reddedilen metin yalnız `halusinasyon_degerlendirme.ham_cikti` içinde (`audit.generated_answer`) durur, VIEW'da gösterilmez.
- Bir soruda birden fazla değerlendirme satırı varsa `citation_correct`, `hata_turu`, `uygulanan_onlem`, `duzeldi_mi` farklı değerleri virgülle yan yana gösterir; hepsi boşsa boştur. `hata_turu`'ndaki `insufficient_evidence` / `retrieval_only_not_evaluated` hata değil, olay kaydıdır.
- `iddia_*` sayıları iddia satırlarından (`ham_cikti.$.evaluation.claim_index` ya da `claim_text` dolu) sayılır; `iddia_belirsiz` = `claim_supported` boş olan iddia.
- **RAG dışı kullanım:** `sql_integration` ile kaydedilen bir soruya klinik çıkarım bağlanabilir. Bu sorular da listelenir; `klinik_bulgu_sayisi` ve `klinik_cikarim_red_sayisi` sütunları bunu gösterir. Değerlendirme satırı olmayan soruda (`degerlendirme_satiri = 0`) `durum` boştur.
- `retrieval_score` RRF birleşik sıralama puanıdır (kosinüs benzerliği değil); sıra `source_rank`'tir (reranker sırası).

`kaynak_bul` arama metnini yalnızca değer olarak kullanır (dinamik SQL yok); `%`, `_` ve `\` joker değil, düz karakterdir.

### Pilot kayıtlar (`v_pilot_bulgular`)

Pilot aktarımıyla (`src.pilot_clinical.import_records`) gelen, `bilgi_turu` dolu `klinik_bulgular` satırları; satır başına bir satır, salt okunur. Literatür bilgisi ve çalışmanın kendi bulguları **aynı listededir**: `baglam` yalnız bir sütundur, filtre değildir.

```sql
USE kardiyo_tam_metin;

-- 1) Bütün pilot bulgular (literatür + çalışmanın bulguları birlikte)
SELECT bulgu_id, filename, makale_basligi, kanit_sayfa, bilgi_turu_tr, baglam_tr, kanit_turu_tr,
       tablo_no, alt_bolum, degisken, ilac, hasta_grubu, grup_n, comparator, comparator_n, analiz,
       deger, deger_bicimi, unit, etki_turu, ci95, p, klinik_bilgi_en, kanit_cumle, review_status,
       kalite_en_yuksek, kalite_kontrolleri
FROM v_pilot_bulgular ORDER BY filename, kanit_sayfa, tablo_no, bulgu_id;

-- 2) Bir tablonun satırları gruplarıyla (Table 1: grup başına satır; Table 2: analiz başına satır)
SELECT satir_no, alt_bolum, degisken, ilac, hasta_grubu, grup_n, comparator, comparator_n, analiz,
       deger, deger_bicimi, unit, etki_turu, ci95, p, kanit_cumle
FROM v_pilot_bulgular
WHERE filename = 'TK-cardiovascular36-2-1.pdf' AND tablo_no = 1
ORDER BY satir_no, hasta_grubu, analiz;

-- 3) Yalnız ilaçlar (kullanım oranı n (%); makalede doz yok)
SELECT filename, kanit_sayfa, tablo_no, ilac, mudahale_turu, hasta_grubu, grup_n, deger, deger_bicimi, p, kanit_cumle
FROM v_pilot_bulgular
WHERE bilgi_turu = 'ilac' OR mudahale_turu = 'arka plan tedavisi'
ORDER BY filename, ilac, hasta_grubu;

-- 4) Bir sayfadaki bütün bilgiler (metin kayıtları önce, sonra tablo satırları)
SELECT bulgu_id, bilgi_turu_tr, baglam_tr, kanit_turu_tr, tablo_no, satir_no, degisken, ilac, hasta_grubu,
       analiz, deger, p, klinik_bilgi_en, kanit_cumle
FROM v_pilot_bulgular
WHERE filename = 'TK-cardiovascular36-2-1.pdf' AND kanit_sayfa = 4
ORDER BY tablo_no, satir_no, bulgu_id;

-- 5) Bir bilgiyi PDF'te göstermek: kanıtta arama (dosya + sayfa + birebir kanıt cümlesi)
SELECT bulgu_id, filename, kanit_sayfa, tablo_no, satir_no, hasta_grubu, analiz, deger, p, kanit_cumle
FROM v_pilot_bulgular WHERE kanit_cumle LIKE '%HbA1c%' ORDER BY filename, kanit_sayfa, bulgu_id;

-- kaynak_bul pilot satırlarını da kapsar (kanıt, değişken, hasta grubu, ilaç adında arar);
-- bulunan bulgu_id'lerin pilot ayrıntısı:
CALL kaynak_bul('HbA1c');
SELECT * FROM v_pilot_bulgular WHERE bulgu_id IN (...);

-- 6) Kalite işareti olan pilot satırları (önce: python -m src.data_quality --pilot)
SELECT bulgu_id, kanit_sayfa, tablo_no, degisken, hasta_grubu, analiz, kalite_hata, kalite_uyari, kalite_bilgi,
       kalite_en_yuksek, kalite_kontrolleri
FROM v_pilot_bulgular WHERE kalite_en_yuksek IS NOT NULL
ORDER BY kalite_hata DESC, kalite_uyari DESC, bulgu_id;
```

- **`taslak_notu` sütunları:** `tablo_no`, `satir_no`, `alt_bolum`, `analiz`, `grup_n`, `comparator_n`, `deger_bicimi` pilot tablo aktarımının notundan okunur (`JSON_VALID` korumalı). Metin kayıtlarında, alan yoksa ya da JSON bozuksa NULL olurlar.
- **Grup ve analiz:** Table 1 satırları grup başına ayrıdır (`hasta_grubu`, `comparator`, `grup_n`, `comparator_n`; aynı satırın p'si iki grupta da aynıdır). Table 2 satırları analiz başına ayrıdır (`analiz` = univariate / multivariate, `etki_turu` = OR, `ci95`). İlaç satırlarında ilaç adı `ilac` sütunundadır, `degisken` "kullanım oranı"dır.
- **Türkçe etiketler** ek sütunlardır; asıl değerler yanında durur. `bilgi_turu_tr`: tani → Tanı, semptom → Semptom, teshis_yontemi → Teşhis yöntemi, tani_kriteri → Tanı kriteri, tedavi → Tedavi, ilac → İlaç, doz → Doz, hasta_grubu → Hasta grubu, klinik_sonuc → Klinik sonuç, olcum → Ölçüm, diger → Diğer. `baglam_tr`: study_finding → Çalışmanın bulgusu, study_method → Çalışmanın yöntemi, exclusion_criterion → Dışlama kriteri, literature_background → Literatür bilgisi, recommendation → Öneri. `kanit_turu_tr`: metin → Metin, tablo → Tablo.
- **Kalite sütunları** `v_kalite_bulgu`'dan gelir ve yalnız **en son kalite koşusunun** sürümünü gösterir. Son koşu `--pilot` olmadan yapıldıysa pilot satırlarının işaretleri görünmez (`data_quality` bu durumda uyarı verir).
- **Çok parçalı kanıt** (pilot v3.1+; sayfa sonu, tablo sayfası ya da şekil/tablo başlığıyla bölünmüş tek cümle): `import_records` bitiş sayfasını `kanit_sayfa_bitis`'e, birleşme türünü (`sayfa` / `ara_blok`) `birlestirme`'ye, parçaları `taslak_notu.kanit_parcalari`'ya yazar; `kanit_cumle` ilk parçadır. VIEW'ın sonundaki sütunlar: `kanit_sayfa_bitis`, `birlestirme`, `kanit_parca_sayisi` (parçasız kayıtta 1) ve `kanit_gosterim`. **`kanit_gosterim` yalnız gösterim içindir, birleştirilmiş alıntı değildir**: parçaları `[s.N] metin` işaretiyle sırayla yan yana yazar (parçasız kayıtta `kanit_cumle`). Birebir kanıt `kanit_cumle` ve parçaların kendisidir. `python -m src.data_quality --pilot` her parçayı kendi sayfasında arar; doğrulanan kayıt `pilot_cok_parcali_cozuldu` (bilgi), tutarsız parça `pilot_parca_tutarsiz` (hata) alır.

```sql
-- Çok parçalı kanıtlı pilot bulgular (parçalar sayfa işaretiyle)
SELECT bulgu_id, kanit_sayfa, kanit_sayfa_bitis, birlestirme, kanit_parca_sayisi, klinik_bilgi_en, kanit_gosterim
FROM v_pilot_bulgular WHERE kanit_parca_sayisi > 1 ORDER BY filename, kanit_sayfa, bulgu_id;
```

### Hoca sunum veritabanı (`kardiyo_sunum`)

Hoca için ayrı bir veritabanında İngilizce, salt okunur view'lar. Teknik sütun yoktur; her view'da `evidence`, `source`, `page` bulunur. Tablo satırlarında `evidence` kısa bir kaynak metnidir ("Table 1 – Age (years)"); ham tablo satırı kaynak veritabanında durur. Metin satırlarında `evidence` kanıt cümlesinin kendisidir.

**Kurulum** (kaynak sunum nesneleri kurulduktan sonra; tekrar çalıştırılabilir):

```powershell
python -m src.sunum --kur        # kaynak veritabanında v_pilot_bulgular (bir kez)
python -m src.sunum_hoca --kur   # kardiyo_sunum'u yoksa oluşturur, sql/sunum_hoca/*.sql dosyalarını sırayla kurar
python -m src.sunum_hoca --durum # kurulu view'lar ve satır sayıları
```

- **Veritabanı adları:** Kaynak veritabanı `.env`'deki `MYSQL_DATABASE`. Sunum veritabanının adı `MYSQL_SUNUM_DATABASE`, varsayılanı `kardiyo_sunum`. SQL dosyalarında veritabanı adı yazmaz.
- **Kuran kullanıcının yetkileri:** CREATE (veritabanı oluşturma), sunum veritabanında CREATE VIEW ve DROP, kaynakta SELECT ve SHOW VIEW, ayrıca kaynakta `v_sunum_taban` için CREATE VIEW ve DROP.
- **Hocanın kullanıcısı:** Yalnız `GRANT SELECT ON kardiyo_sunum.* TO <HOCA_KULLANICI>;` yeter.
- **Taşıma:** View'lar `mysqldump` ile başka sunucuya taşınmamalı (DEFINER); her makinede bu komutla kurulmalı.
- **Yeni veri:** Yeni veri geldiğinde view'lar kendiliğinden güncellenir. Yalnız `measurements`'taki `group_N` sütun sayısı kurulumda verideki en fazla grup sayısına göre üretilir; daha çok gruplu bir tablo eklenirse `--kur` yeniden çalıştırılmalı.

**MySQL Workbench'te açmak:** Sol paneldeki Schemas listesini yenileyin (🔄), **kardiyo_sunum > Views** altından istediğiniz view'a sağ tıklayıp **Select Rows** seçin. Ya da:

```sql
SELECT * FROM kardiyo_sunum.measurements;               -- Table 1 ölçümleri, gruplar yan yana
SELECT * FROM kardiyo_sunum.measurements_by_group;      -- aynı veri, grup başına satır
SELECT * FROM kardiyo_sunum.clinical_outcomes;          -- klinik sonuçlar: metin bulguları, Table 2 (OR, GA, p), literatür en sonda
SELECT * FROM kardiyo_sunum.treatments;                 -- ilaçlar
SELECT * FROM kardiyo_sunum.general_clinical_findings;  -- metin ölçüm cümleleri, kohort, yöntem, hastalık olmayan dışlama kriterleri
```

Sunum veritabanında yalnız bu hoca view'ları bulunur. Ortak taban view `v_sunum_taban` kaynak veritabanındadır (iç kullanım; hoca menüsünde görünmez); eski `_findings_base` kurulumda silinir. Kaynaktaki diğer tablo ve view'lara dokunulmaz. Sinem'in `diseases`, `diagnostics` ve `symptoms` view'ları `sql/sunum_hoca/20–22_*.sql` dosyaları eklenince aynı komutla kurulur; ayrıntı `sql/sunum_hoca/README.md`'de.

## Bilinen sınırlar

- **Evidence guard şu an yalnızca uzunluk filtresi uyguluyor.** `answer_question` `min_relevance_score=None` ve kelime örtüşmesi kapalı çalışır; 80 karakterden kısa chunk'lar elenir, alakasız ama uzun chunk'lar elenmez. Skor eşiği henüz kalibre edilmedi.
- **`validate_row` sayıları kontrol ediyor, kelimeleri değil.** Kanıt cümlesinin sayfada birebir geçtiğini ve alanlardaki her sayının kanıtta bulunduğunu doğrular; ama ilaç adı, hasta grubu veya yön gibi alanların cümleyle anlamca uyumunu kontrol etmez. Bu, insan kontrolüyle (`review_status = 'verified'`) yapılır.
- **Yan yatırılmış tablolarda küçük biçim kusurları kalıyor.** Sayfaya yan çizilmiş tablolar (AF s.24, 25, 27, 28; OHCA s.4) artık bellek içinde düzeltilerek doğru yönde okunuyor; başlık ve dipnotlar metne geçiyor. Kalanlar: birden fazla sütuna yayılan üst başlıklar hücrelere bölünmüş çıkıyor (ör. AF Tablo 2: "Associated b \| enefits of adhe \| ring to recommen \| dations"); bazı hücrelerin devamı yan hücreye kayabiliyor (AF s.24); dipnot harfleri `<sup>` olarak ayrı yazılıyor; OHCA Tablo 1'de ilk başlık satırı bir grup başlığı olduğundan devam chunk'larında yalnızca o tekrar ediliyor (asıl sütun adları ilk gövde satırında).
- **Metin cümleleri chunk sınırına denk gelebilir.** Tablolar artık ortasından kesilmiyor, ama düz metin hâlâ 1500 karakterlik pencerelerle bölünüyor; bir kanıt cümlesi iki chunk'ın sınırına denk gelirse tek bir chunk'ta tam geçmez (bu durumda `chunk_id` boş kalır).
- **Eski sürüm chunk'ları veritabanında birikir.** Chunk'lama sürümü değişince eski chunk ve embedding'ler silinmez (geçmiş sorgular `rag_sorgu_kaynaklari` üzerinden onlara bağlı olabilir); kullanılmazlar ama yer kaplarlar.
- **4 Türkçe özet LLM çevirisi (gpt-6-luna), sayılar kontrol edildi ama insan onaylı değil.** AF, OHCA, troponin ve KAH makalelerinin `ozet_tr`'si (`ozet_tr_kaynak = 'llm_ceviri'`) `abstract_en`'in makine çevirisidir. Yüklemeden önce çevirideki her sayının İngilizce özette geçtiği ve sayı/işaret adetlerinin aynı olduğu kontrol edildi; anlam, terim ve klinik doğruluk insan tarafından kontrol edilmedi.
- **`data/clinical/referans_taslak.json` doğrulanmamıştır.** Aday cümlelerden hazırlanmış bir karşılaştırma referansıdır (`meta.durum = "dogrulanmamis_referans"`); insan kontrolünden geçmemiştir ve `klinik_bulgular`'a doğrudan girmez.
- **Klinik çıkarımda `satir_hash` `taslak_notu`'nu kapsamıyor.** Aynı kanıttan, SQL sütunları aynı ama yalnızca `taslak_notu`'ndaki alanlarda (ör. İngilizce sonuç ölçütü) farklı iki bulgu tek satıra iner; ikincisi `zaten_var` sayılır.
- **İngilizce sonuç ölçütleri VIEW'larda görünmez.** `clinical_outcome` yalnızca Türkçe kaynakta `sonuc_olcutu_tr`'ye yazılır; İngilizce olanlar `taslak_notu.extracted_fields`'tadır, `v_tani_tedavi` / `v_sonuclar` bunları göstermez.
- **Tablo bulguları yalnızca tipli tablo ailelerinin kabul ettiği biçimde kaydedilebiliyor.** 31 basit tablo satırından: `ischemic_group_variables` (13 satır; tanı yöntemi + grup başlığı + o sütunun değeri) 3 satır `validate_row`'dan geçebiliyor; `study_summary` (13 satır) tablosunda "Intervention" sütunu olmadığı için kural hiçbir müdahaleyi kabul etmiyor; `cct_recommendations` (5 satır) endikasyon metinleri "X can be detected with Y" kalıbında değil. `test_clinical_pipeline` her koşuda aile bazında raporlar.
- **Tablo bulgularında p / GA / n / etki ölçütü sütunlara yazılmaz** (sütun kaynağı doğrulanamaz); değerler `taslak_notu`'nda kalır.
- **Binlik ayırıcılı örneklem büyüklüğü ("1,234 patients") `hasta_sayisi`'na yazılmaz:** `validate_row` kanıtta "1,234"ü tek sayı görür, 1234 yazılırsa satırın tamamı reddedilirdi. Ham değer ve ayrıştırılan sayı `taslak_notu.deger_alanlari`'ndadır.

## Güvenlik

- `.env` (şifreler, API anahtarı) ve `backups/` (veritabanı yedekleri) `.gitignore`'dadır, git'e girmez. Paylaşılan şablon yalnızca boş değerli `.env.example`'dır.
- `data/pdfs/` ve `data/review/` de git'e girmez.
