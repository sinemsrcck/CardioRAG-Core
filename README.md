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
 → metin çıkarma (pymupdf4llm, sayfa sayfa markdown)          src/pdf_loader.py
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

**Sürümler:** `pdf_belgeleri.processing_version` metin çıkarma, kaynakça temizleme ve chunk'lama sürümlerini birlikte içerir (ör. `…|references-v2|chunking-table-v1`). Chunk'lama değişince `python -m src.ingest` yeni sürümü yazar; eski sürümün chunk ve embedding'leri **silinmez**, yalnızca kullanılmaz: `load_chunks_for_rag` ve `validate_row` yalnızca `pdf_belgeleri`'deki güncel sürümün chunk'larını okur.

**Klinik bulgular (ilaç / doz / hasta grubu / tanı yöntemi):**

```
LLM çıkarımı (satırlar, kanıt cümlesi chunk'tan aynen)
 → validate_row (kanıt doğru sayfada birebir mi, sayılar kanıtta mı,
                 icd10 icd10_who'da mı, değerler izinli kümede mi)   src/clinical_validation.py
 → save_clinical_findings
      geçenler     → klinik_bulgular (review_status = 'pending')
      geçemeyenler → halusinasyon_degerlendirme (query_id verilirse)  src/database.py
 → VIEW'lar: v_tani_tedavi, v_sonuclar (hocanın biçiminde, hocanın satırlarıyla birlikte)
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

`SHOW TABLES` en az şu 6 tabloyu göstermeli: `icd10_who`, `makaleler`, `semptom_iliskileri`, `sonuclar`, `tani_tedavi`, `tkd_kurallari`.

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
```

- `src.database --init`: yeni tabloları, eksik sütunları, kısıtları ve VIEW'ları oluşturur; tekrar çalıştırılabilir, hiçbir şey silmez. `--check` (varsayılan) yalnızca durumu raporlar.
- `src.ingest`: 6 PDF'i okur, temizler, parçalar, embedding üretir ve yazar (varsayılan model `minilm`; `--model bge-m3` seçeneği var). Tekrar çalıştırmak güvenlidir; var olan chunk ve embedding'ler yeniden yazılmaz.
- `src.article_metadata --write --tam-metin temiz`: 6 makaleyi `makaleler` tablosuna yeni satır olarak ekler (başlık, dergi, yıl, DOI, abstract, temizlenmiş tam metin). Önce `--dry-run` ile yazmadan görülebilir.

## Tablolar

### Hocanın tabloları (dokunulmaz)

| Tablo | İçerik |
|---|---|
| `makaleler` | Makale künyesi, özet ve tam metin. Hocanın 5 satırı (`document_id` NULL) değiştirilmez. |
| `tani_tedavi` | Tanı – tedavi / tanı testi çiftleri, doz, uygulama yolu, kanıt cümlesi ve sayfası. |
| `sonuclar` | Sonuç ölçütleri: etki türü, değer, %95 GA, p, yön, kanıt. |
| `semptom_iliskileri` | Semptom – tanı ilişkileri ve kanıt. |
| `icd10_who` | ICD-10 kod sözlüğü (İngilizce ve Türkçe adlar). |
| `tkd_kurallari` | Türk Kardiyoloji Derneği terim kuralları. |

`makaleler`'e yalnızca 3 boş sütun eklendi: `document_id`, `doi`, `abstract_en`. Bizim 6 makalemiz bu tabloda `document_id` **dolu** satırlardır; hocanın satırlarında `document_id` NULL'dur.

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
| `v_tani_tedavi` | `tani_tedavi` (hoca) + `klinik_bulgular` (bizim), hocanın sütun adlarıyla; ek sütunlar `kaynak` (`hoca` / `cardiorag`), `review_status`, `cikarma_yontemi`. |
| `v_sonuclar` | `sonuclar` (hoca) + `klinik_bulgular`'daki sonuç ölçütü olan satırlar; aynı ek sütunlar. |

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
| `pdf_loader.py` | PDF'i pymupdf4llm ile sayfa sayfa markdown'a çevirir; döndürülmüş tablo başlıklarını ve dipnotlarını kurtarır. |
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
| `clinical_candidates.py` | Tam metinden doz, ilaç/müdahale ve tanı yöntemi içeren aday cümleleri listeler (`data/review/` altına CSV). |
| `clinical_validation.py` | `validate_row`: klinik bulgu satırlarının kaynağa karşı denetimi. |
| `clinical_draft.py` | `data/clinical/referans_taslak.json`'ı denetleyip yalnızca CSV üretir; veritabanına yazmaz. |

## Testler (`evaluation/`)

Hepsi proje kökünden `python -m evaluation.<dosya_adı>` ile çalıştırılır.

**MySQL gerektirenler** (`.env` ve kurulum adım 5 tamamlanmış olmalı):

| Test | Ne yapar |
|---|---|
| `test_database_schema` | `init_schema`'yı iki kez çalıştırır; tabloları, sütunları, VIEW'ları ve hoca tablolarının satır sayılarını kontrol eder. |
| `test_ingestion` | `src.ingest`'i iki kez çalıştırır (satır sayısı artmamalı); chunk, embedding ve `load_chunks_for_rag` çıktısını doğrular. Birkaç dakika sürer. |
| `test_makaleler` | Hocanın 5 satırının değişmediğini (hash), bizim 6 satırı ve ikinci yazımda çoğalma olmadığını kontrol eder. Not: bizim 6 satırı aynı değerlerle yeniden yazar. |
| `test_rag_kayit` | `save_rag_result`'ı gerçek `answer_question` (`use_llm=False`) çıktısıyla dener; reranker yer tutucuyla değiştirilir, model indirmez. Bütün yazımlar geri alınır. |
| `test_klinik_bulgular` | Referans taslağı `save_clinical_findings` ile yazar, bozuk satırların reddini, VIEW sayılarını ve hoca tablolarını kontrol eder. Bütün yazımlar geri alınır. |

**MySQL gerektirmeyenler:**

| Test | Ne yapar |
|---|---|
| `test_chunking` | Tablo farkındalıklı chunk'lama: tablosuz metin eskisiyle aynı, tablolar ortasından kesilmiyor, devam parçalarında başlık satırı var, 1500 sınırı aşılmıyor; 6 PDF'te 243 chunk (PDF'ler gerekir). |
| `test_pipeline` | `answer_question` ve evidence guard için mock'lu birim testler (model veya LLM gerekmez). |
| `test_pdf_processing` | 6 PDF'in yüklenmesi, kaynakça temizliği ve chunk'ları; ayrıca `test_questions.csv`'deki kanıt eşleştirmelerini yeniden üretilen chunk'larla doğrular (PDF'ler gerekir; `--pdf-dir` ile farklı klasör verilebilir). Chunk'lama sürümü değiştikten sonra eşleştirmeler yenilenene kadar "Stale or unknown chunk id" ile başarısız olur (aşağıya bakın). |
| `test_reference_cleaning` | Kaynakça temizliğinin PDF bazında raporu (PDF'ler gerekir). |
| `test_real_pdfs` | 6 PDF üzerinde embedding + `answer_question(use_llm=False)`; ilk çalıştırmada `minilm` reranker modelini indirir. |
| `test_real_retrieval` | 3 örnek metinle retrieval + rerank denemesi; ilk çalıştırmada `minilm` reranker modelini indirir. |

`evaluate_rag.py` ve `evidence_threshold.py` test değil, değerlendirme araçlarıdır (soru setiyle retrieval raporu ve eşik analizi).

`chunk_remap_report.py` de test değildir: chunk'lama sürümü değişince `test_questions.csv`'deki `chunk_id` atıfları için eski → yeni eşleme raporu üretir (MySQL gerekir; `test_questions.csv`'yi değiştirmez). Bkz. [Chunk sürümü değiştikten sonra](#chunk-sürümü-değiştikten-sonra).

## Chunk sürümü değiştikten sonra

Chunk'lama değiştiğinde (şu an `chunking-table-v1`) bütün `chunk_id`'ler değişir, çünkü kimlik sürüm anahtarını içerir. `evaluation/test_questions.csv`'deki kanıt eşleştirmeleri eski kimliklere işaret ettiği için yenilenmelidir. Değişiklik main'e geldikten sonra:

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

## Bilinen sınırlar

- **Evidence guard şu an yalnızca uzunluk filtresi uyguluyor.** `answer_question` `min_relevance_score=None` ve kelime örtüşmesi kapalı çalışır; 80 karakterden kısa chunk'lar elenir, alakasız ama uzun chunk'lar elenmez. Skor eşiği henüz kalibre edilmedi.
- **`validate_row` sayıları kontrol ediyor, kelimeleri değil.** Kanıt cümlesinin sayfada birebir geçtiğini ve alanlardaki her sayının kanıtta bulunduğunu doğrular; ama ilaç adı, hasta grubu veya yön gibi alanların cümleyle anlamca uyumunu kontrol etmez. Bu, insan kontrolüyle (`review_status = 'verified'`) yapılır.
- **AF makalesindeki döndürülmüş tablolar devrik çıkıyor.** Tablo 2 ve 3 sayfaya yan yatırılmış; metin çıkarımında satır ve sütunlar yer değiştiriyor ve Tablo 2'nin üst başlığı ile bazı dipnotları metne geçmiyor. Tablonun "başlık satırı" aslında bir veri satırı olduğundan, chunk'lamada tekrar edilen başlık bu tablolarda anlamı tam düzeltmez.
- **Metin cümleleri chunk sınırına denk gelebilir.** Tablolar artık ortasından kesilmiyor, ama düz metin hâlâ 1500 karakterlik pencerelerle bölünüyor; bir kanıt cümlesi iki chunk'ın sınırına denk gelirse tek bir chunk'ta tam geçmez (bu durumda `chunk_id` boş kalır).
- **Eski sürüm chunk'ları veritabanında birikir.** Chunk'lama sürümü değişince eski chunk ve embedding'ler silinmez (geçmiş sorgular `rag_sorgu_kaynaklari` üzerinden onlara bağlı olabilir); kullanılmazlar ama yer kaplarlar.
- **`data/clinical/referans_taslak.json` doğrulanmamıştır.** Aday cümlelerden hazırlanmış bir karşılaştırma referansıdır (`meta.durum = "dogrulanmamis_referans"`); insan kontrolünden geçmemiştir ve `klinik_bulgular`'a doğrudan girmez.

## Güvenlik

- `.env` (şifreler, API anahtarı) ve `backups/` (veritabanı yedekleri) `.gitignore`'dadır, git'e girmez. Paylaşılan şablon yalnızca boş değerli `.env.example`'dır.
- `data/pdfs/` ve `data/review/` de git'e girmez.
