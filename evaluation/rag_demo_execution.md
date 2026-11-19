# RAG ve kaynak değerlendirmesi için güvenli gösterim

Varsayılan işlem yalnızca yerel MiniLM retrieval/reranking ve Evidence Guard
çalıştırır. SQL kaydı ve LLM üretimi ayrı, açık işlemlerdir. Bu belge komutları
hazırlar; kendiliğinden hiçbirini çalıştırmaz. Klinik çıkarım akışı kullanılmaz.

## 1. Ücretsiz retrieval ve arşiv

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.run_rag run --question "Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?"
.\.venv\Scripts\python.exe -X utf8 -m src.run_rag run --question "Troponin değişimi akut miyokard enfarktüsü tanısında nasıl değerlendirilir?"
.\.venv\Scripts\python.exe -X utf8 -m src.run_rag run --question "Mars'ın yüzey sıcaklığı kaç derecedir?"
```

Model indirme kapalıdır; cache eksikse çalıştırma hata verir. Modeller açıkça
embedding_model=minilm, reranker_model=minilm seçilir. SQL kaynak yüklemesi
READ ONLY transaction kullanır. Varsayılan çıktı `data/review/rag_runs/<query_id>.json`;
önceki dosyalar ezilmez. Her yeni `run` yeni denemedir ve farklı query_id alır.
Mükerrer kayıt koruması aynı arşivin tekrar `persist` edilmesi içindir.

Gerçek üretim için ayrıca kullanıcı API izni gerekir; ancak bundan sonra aynı
`run` komutuna `--use-llm` eklenebilir. Timeout 60 saniye, SDK retry=0; fallback yok.
`answer` güvenlik kontrolünden geçmediyse None kalır. Reddedilmiş özgün çıktı
yalnızca arşivin **audit.generated_answer** alanındadır; CLI kullanıcı yanıtında
bu alanı göstermez. Arşivler denetim dosyalarıdır ve erişimleri sınırlandırılmalıdır.

## 2. Arşivi inceleme ve ayrı SQL izni

Kaynak PDF, fiziksel sayfa, chunk_id, kaynak metni/hash/sürüm, süre, tüm reranker
sırası, guard, atıf ve claim raporunu inceleyin. Soru türünü guard kararından
türetmeyin: yetersiz retrieval, makalelerde cevabın bulunmadığını kanıtlamaz.

SQL yazımı ayrıca açıkça onaylandıktan sonra:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.run_rag persist --archive data/review/rag_runs/GERCEK_QUERY_ID.json --question-type cevaplanabilir --write-sql
```

`--question-type`: cevaplanabilir / cevaplanamaz / celiskili; kaynak incelemesine
dayanan operatör sınıflandırmasıdır, uzman onayı değildir. Aynı query_id ve aynı
arşiv/sınıflandırma tekrar verilirse yeniden INSERT yapılmaz. Değişmiş içerikle
aynı query_id veya önceki eksik kayıt reddedilir. Yeni API çağrısı yapılmaz.

Üç tablo tek transaction içindedir: rag_sorgulari, rag_sorgu_kaynaklari,
halusinasyon_degerlendirme. Herhangi bir hata tüm yeni yazımları geri alır.
Caller-owned connection kullanılırsa savepoint ile korunur ve commit çağırana aittir.
Aktif kaynak sürümü, gerçek SQL metni/hash'i ve foreign key ilişkileri eski
save_rag_result sınırında tekrar denetlenir. SQL şeması değişmez.

## 3. Değerlendirme anlamları

| Gerçek gözlem | SQL claim_supported | Hata / olay |
|---|---|---|
| supported | supported | NULL |
| not_supported / açık uyuşmazlık | not_supported | claim_not_supported vb. |
| partial | partially_supported | claim_partial |
| uncertain | NULL | claim_uncertain; özgün karar ham JSON'da |
| retrieval_only | NULL | retrieval_only_not_evaluated |
| yetersiz kaynak | NULL | insufficient_evidence |
| atıf hatası | NULL | citation_validation_failed |
| LLM hatası | NULL | generation_failed |

Çalıştırma olayları yapay klinik iddia değildir; claim_text NULL kalır. Claim
validator hatası ayrıca claim_validator_error olur. Her otomatik kayıt
evaluation_method=automated, review_status=pending, kayit_turu=soru_cevap taşır.
NULL, yanlışlık veya halüsinasyon kanıtı değildir. Kaynak sadakati kontrolü
bilimsel/klinik doğruluk sertifikasyonu veya uzman onayı anlamına gelmez.
Birden fazla destek kaynağı varsa tamamı ham JSON'daki claim_report.matches
içinde korunur; tekli supporting_chunk_id ilk kaynağa işaret eder.

## 4. Salt okunur kayıt sonrası kontrol

```sql
SELECT * FROM rag_sorgulari WHERE query_id = 'GERCEK_QUERY_ID';
SELECT * FROM rag_sorgu_kaynaklari WHERE query_id = 'GERCEK_QUERY_ID' ORDER BY source_rank;
SELECT claim_text, citation_correct, claim_supported, hata_turu,
       evaluation_method, review_status, ham_cikti
FROM halusinasyon_degerlendirme WHERE query_id = 'GERCEK_QUERY_ID' ORDER BY id;
```

## Sentetik arşiv örneği (kısaltılmış)

Aşağıdaki yalnızca test biçimini gösterir; gerçek model yanıtı değildir, tam
arşiv değildir ve SQL'e kaydedilemez. Testler tam metaverili karşılığını geçici
klasörde üretip doğrular; gerçek veri klasörlerine mock sonuç yazılmaz.

```json
{
  "run_provenance": {"kind": "mock_test", "real_llm": false},
  "status": "claim_validation_failed",
  "answer": null,
  "audit": {
    "scope": "restricted_audit_not_user_answer",
    "generated_answer": "Synthetic clinical claim [1]."
  },
  "claim_verification": {
    "scope": "synthetic mocked evaluation",
    "claims": [{"claim_index": 1, "status": "uncertain", "matches": []}]
  }
}
```

Mock testler yalnızca sentetik sonuç üretir; `run_provenance.kind=mock_test` olarak
etiketlenir ve normal SQL yazıcısı bunları reddeder. Test izni yalnızca açıkça
verilen mock bağlantı ile kullanılabilir. Bu geliştirme sırasında gerçek API
ve gerçek SQL yazımı yapılmamıştır. Gerçek API ve MySQL yazma uçtan uca denemesi
henüz kullanıcı izni bekler.
