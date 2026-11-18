# CardioRAG-Core: kaynaklı retrieval demosu

Doğrulama tarihi: 18 Kasım 2026

## Gösterimin kapsamı

Bu gösterim yalnızca kaynak getirme ve seçme akışını çalıştırır (`use_llm=False`).
Gerçek LLM yanıtı üretilmedi; SQL'e veri yazılmadı. Aşağıdaki alıntılar güncel SQL
chunk metinlerinde birebir doğrulandı.

| Kontrol | Sonuç |
|---|---|
| PDF / aktif chunk / aktif MiniLM embedding | 6 / 242 / 242 |
| Embedding modeli | paraphrase-multilingual-MiniLM-L12-v2 |
| Reranker | ms-marco-MiniLM-L-6-v2, yerel önbellekten |
| Arama adayı / azami bağlam kaynağı | 20 / 5 |
| Belge başına sınır | 2 |
| Yerel/mock testler | 242 başarılı |
| Gerçek LLM çağrısı / SQL yazımı | 0 / 0 |

Aktif işleme sürümü:

`markdown-rotated-v3|pymupdf=2.1|pymupdf4llm=2.1|references-v2|chunking-table-v1`

Eski sürümler SQL'de korunur; bu gösterimin okuma yolu yalnızca aktif sürümü seçer.
Belge çeşitliliği nedeniyle çıkarılan parçaların yerine alt sıralardan kaynak
eklenmez. Bu nedenle azami kaynak sayısı beş olsa da daha az sonuç dönebilir.

## 1. Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?

Durum: `retrieval_only`. Seçilen kaynak sayısı: **1**.
LLM yanıtı: **üretilmedi**.

### Kaynak 1

- PDF: `pubM-Atrial Fibrillation.pdf`
- Fiziksel sayfa: **2**
- Chunk indeksi: `0` (sıfırdan başlar)
- Chunk kimliği: `9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0`

Birebir destekleyici alıntı:

> AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

**Gösterimde söylenebilecek:** Kaynak AF'nin klinik karşılaşmalarda,
giyilebilir cihazlarla veya implante kardiyak elektronik cihazların sorgulanmasıyla
saptanabileceğini belirtiyor. Bu pasajdan tanısal duyarlılık/özgüllük, tedavi dozu
veya klinik yarar sonucu çıkarılmıyor.

## 2. Troponin değişimi akut miyokard enfarktüsü tanısında nasıl değerlendirilir?

Durum: `retrieval_only`. Seçilen kaynak sayısı: **2**.
LLM yanıtı: **üretilmedi**.

### Kaynak 1

- PDF: `who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf`
- Fiziksel sayfa: **1**
- Chunk indeksi: `0` (sıfırdan başlar)
- Chunk kimliği: `23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p1_c0_tb8e3c9ac1365bdbecc92bbc28a9bf7e3a520fba4c4d3696ac8eb1d7d75a64967`

Birebir destekleyici alıntı:

> The delta HS Troponin I (0h-1h) and delta HS Troponin I (0h-3h) values of the patients were calculated as percentage changes.

### Kaynak 2

- PDF: `who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf`
- Fiziksel sayfa: **4**
- Chunk indeksi: `2` (sıfırdan başlar)
- Chunk kimliği: `23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1`

Birebir destekleyici alıntı:

> In patients diagnosed with AMI determination was done as follows: 16 patients (80%) with delta HS troponin I (0-1h) >20% (p <0.001 odds ratio: 7.8 (2.4-25.1)), delta HS troponin I (0-3h) >20% in 17 patients [85%, p <0.001, odds ratio: 9.5 (2.6-34.9)].

**Gösterimde söylenebilecek:** Makale troponin değişimini başlangıçtan birinci
ve üçüncü saate kadar yüzde değişimleriyle inceliyor. İkinci alıntı araştırmadaki
AMI grubuna ait bulguları gösteriyor. Zaman aralıkları, hasta sayıları ve eşikler
birbirine karıştırılmamalı; bu çalışma bulguları evrensel bir tanı protokolü
olarak sunulmamalı.

## Sistem ne zaman yanıt vermekten kaçınır?

- Kullanılabilir kaynak bulunmazsa veya son bağlama kaynak sığmazsa.
- Soru amacı mevcut sınırlı demo politikasında desteklenmiyorsa.
- Kaynakta soru konusu ve gereken görev/yöntem bağlamı birlikte bulunmuyorsa.
- Örneğin AF tespit sorusuna yalnızca ablasyon sonuçları getirilirse,
  koroner tanı sorusuna yalnızca karotis verileri gelirse veya troponin değişimi
  sorusunda değişim değerlendirmesini destekleyen kaynak bulunmazsa.

Bu durumda sonuç `insufficient_evidence` olur ve LLM üretim kapısı açılmaz.
Pozitif reranker skoru veya kaynak sayısı tek başına yeterlilik sayılmaz.

`sufficient=True`, yalnızca sınırlı demo konu–görev kontrollerinin geçildiğini
gösterir; klinik doğruluk veya iddia–kaynak ilişkisinin insan tarafından
doğrulandığı anlamına gelmez. Bu retrieval-only gösterimde yeterli kaynak bulunsa
bile cevap üretimi kapalıdır.

## Henüz tamamlanmayan çalışmalar

- Soru–chunk etiketlerinin uzman onayı: CSV'de kalibrasyona uygun insan onaylı
  eşleşme henüz yok. Klinik pilot sınıflandırma onayı bu etiketlerin yerine geçmez.
- MiniLM ve BGE-M3 için ayrı kalibrasyon ve bağımsız test; üretim eşiği belirlenmedi.
- Genel amaçlı Türkçe/İngilizce ilgililik denetimi: mevcut kapı sınırlı AF/CAD/troponin
  demo amaçlarını kapsar. Diğer sorular çekimser sonuç verebilir.
- İddia düzeyinde yanıt–kaynak doğrulaması ve daha geniş, bağımsız değerlendirme.
- RAG yanıt üretiminin ayrı izinle canlı denenmesi; RAG LLM çağrısında açık
  timeout/retry sınırlarının tamamlanması.
- Yeni `diagnostic_method` modeliyle klinik çıkarımın gerçek API denemesi.
  Önceki P01 yanıtı otomatik dönüştürülmedi veya klinik olarak onaylanmadı.

## Kısa sunum akışı

1. Türkçe AF sorusunu ve seçilen tek kaynağı göster.
2. PDF'nin fiziksel ikinci sayfasını açarak alıntıyı karşılaştır.
3. Türkçe troponin sorusunu, fiziksel birinci ve dördüncü sayfa kaynaklarıyla göster.
4. Sistemin kaynakları beşe tamamlamak için ilgisiz parçalar eklemediğini açıkla.
5. Bunun kaynak getirme demosu olduğunu; üretken yanıtın ve otomatik klinik
   doğruluk iddiasının bu gösterime dahil olmadığını belirt.
