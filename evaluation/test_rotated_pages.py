
"""
Yan yatırılmış (döndürülmüş) tablo sayfalarının düzeltilerek okunması testi.
MySQL gerektirmez; 6 PDF data/pdfs/ altında olmalı.

Çalıştırma:
    python -m evaluation.test_rotated_pages

Kontroller:
- Kural yalnızca AF s.24, 25, 27, 28 ve OHCA s.4'ü tetikliyor (genel kural, istisna yok).
- Diğer bütün sayfalar düzeltme kapalıyken okunan metinle birebir aynı.
- Tablolar doğru yönde (Tablo 3 başlık satırı "Source", Tablo 2 "Risk factor").
- AF s.25'te kaybolan 6 parça geri geldi; başlıklar tablonun önünde tek satır.
- Yinelenen metin yok; kelime kaybı azaldı.
- Devam chunk'ları tablo başlığını ve "(devam)" taşıyor.
- _text_key yalnızca gerçek etiketleri siliyor ("P < .001 ... >15 s" arası korunuyor).
"""

import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

import src.pdf_loader as loader
from src.chunking import CONTINUATION_SUFFIX, chunk_document
from src.pdf_loader import PDF_DIR, PDF_PROCESSING_VERSION, _text_key, load_pdf, rotated_page_direction
from src.pdf_processing import remove_document_references


AF = "pubM-Atrial Fibrillation.pdf"
OHCA = "who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf"
EXPECTED_TRIGGERED = {AF: [24, 25, 27, 28], OHCA: [4]}
BANNER = re.compile(r"Author Manuscript|JAMA\. Author manuscript.*|Ko et al\.|Page \d+")


def words(text):
    text = re.sub(r"</?[A-Za-z][^>]*>", " ", text)
    return Counter(w.casefold() for w in re.findall(r"[A-Za-z]{3,}|\d+(?:\.\d+)?", text))


def missing_words(raw_text, text):
    raw, have = words(BANNER.sub(" ", raw_text)), words(text)
    return sum(n - have.get(w, 0) for w, n in raw.items() if n > have.get(w, 0))


def load_without_derotation(path):
    original = loader.rotated_page_direction
    loader.rotated_page_direction = lambda page, *a, **k: None
    try:
        return load_pdf(path)
    finally:
        loader.rotated_page_direction = original


def main():
    results = []

    def check(name, expected, actual, passed=None):
        if passed is None:
            passed = expected == actual
        results.append((name, str(expected), str(actual), bool(passed)))

    paths = sorted(Path(PDF_DIR).glob("*.pdf"))

    # 1) Hangi sayfalar tetikleniyor?
    triggered = {}
    for path in paths:
        with pymupdf.open(path) as pdf:
            pages = [i + 1 for i, page in enumerate(pdf) if rotated_page_direction(page)]
        if pages:
            triggered[path.name] = pages
    check("tetiklenen sayfalar", EXPECTED_TRIGGERED, triggered)

    # 2) Diğer sayfalar düzeltme kapalıyken okunanla birebir aynı
    docs, plain_docs, other_changed = {}, {}, []
    for path in paths:
        docs[path.name] = load_pdf(path)
        plain_docs[path.name] = load_without_derotation(path)
        for new, old in zip(docs[path.name]["pages"], plain_docs[path.name]["pages"]):
            if new["page_number"] not in EXPECTED_TRIGGERED.get(path.name, []) and new["text"] != old["text"]:
                other_changed.append((path.name[:20], new["page_number"]))
    check("tetiklenmeyen sayfalar değişmedi", 0, len(other_changed))
    check("processing_version", PDF_PROCESSING_VERSION,
          docs[AF]["processing_version"].split("|")[0])

    af = {p["page_number"]: p["text"] for p in docs[AF]["pages"]}
    ohca = {p["page_number"]: p["text"] for p in docs[OHCA]["pages"]}

    # 3) Doğru yön
    check("Tablo 3 (s.27) başlık satırı: Source | Enrollment period",
          True, any(l.startswith("|**Source**|**Enrollment**") for l in af[27].split("\n")))
    check("Tablo 3 (s.27) bir satır = bir çalışma (RAAFT-1)",
          True, any(l.startswith("|RAAFT-1,") for l in af[27].split("\n")))
    check("Tablo 3 devamı (s.28) başlık satırı: Source",
          True, any(l.startswith("|**Source**|**Enrollment**") for l in af[28].split("\n")))
    check("Tablo 2 (s.25) başlık satırı: Risk factor",
          True, any(l.startswith("|**Risk factor**|") for l in af[25].split("\n")))
    check("OHCA Tablo 1 başlık satırında Study ve Country", True,
          "**Study**" in ohca[4] and "|Country|" in ohca[4].replace("**", ""))

    # 4) AF s.25'te kaybolan 6 parça
    key25 = _text_key(af[25])
    for label, phrase in (
        ("Tablo 2 üst başlığı", "Associated benefits of adhering to recommendations"),
        ("başlığın 2. satırı", "Secondary Prevention of Atrial Fibrillation (AF)"),
        ("Abbreviations dipnotu", "Abbreviations: ACC, American College of Cardiology"),
        ("aLevel of evidence dipnotu (1. satır)", "Level of evidence A is high quality from >1 RCT"),
        ("aLevel of evidence dipnotu (2. satır)", "moderate-quality RCTs; B-NR, moderate-quality evidence"),
        ("eAF recurrence dipnotu", "AF recurrence includes studies postablation"),
    ):
        check(f"s.25: {label}", "var", "var" if _text_key(phrase) in key25 else "yok")

    # 5) Başlıklar tablonun önünde tek satır
    for name, text, start in ((f"AF s.24", af[24], "**Table 1.** Relative"),
                              (f"AF s.25", af[25], "# **Table 2.** Benefits"),
                              (f"AF s.27", af[27], "**Table 3.** Randomized"),
                              (f"OHCA s.4", ohca[4], "**Table 1 Characteristics")):
        check(f"{name}: başlık tek satır", True, any(l.startswith(start) for l in text.split("\n")))

    # 6) Yinelenen metin yok
    dup = {k: af[p].count(k) for p, k in ((24, "Table 1."), (25, "Table 2."), (27, "Table 3."),
                                          (25, "Abbreviations: ACC"), (25, "postablation"))
           if af[p].count(k) != 1}
    check("yinelenen başlık/dipnot", {}, dup)

    # 7) Kelime kaybı azaldı
    with pymupdf.open(PDF_DIR / AF) as pdf_af, pymupdf.open(PDF_DIR / OHCA) as pdf_ohca:
        for name, pdf, doc, plain, page in ((f"AF s.24", pdf_af, docs[AF], plain_docs[AF], 24),
                                            (f"AF s.25", pdf_af, docs[AF], plain_docs[AF], 25),
                                            (f"AF s.27", pdf_af, docs[AF], plain_docs[AF], 27),
                                            (f"AF s.28", pdf_af, docs[AF], plain_docs[AF], 28),
                                            (f"OHCA s.4", pdf_ohca, docs[OHCA], plain_docs[OHCA], 4)):
            raw = pdf[page - 1].get_text()
            before = missing_words(raw, plain["pages"][page - 1]["text"])
            after = missing_words(raw, doc["pages"][page - 1]["text"])
            check(f"{name}: eksik kelime azaldı/eşit", f"<= {before}", after, after <= before)

    # 8) Chunk'lar: devam parçaları başlık ve "(devam)" taşıyor
    af_chunks = [c for c in chunk_document(remove_document_references(docs[AF])) if c["page_number"] == 27]
    check("AF s.27: her chunk Tablo 3 başlığıyla başlıyor", True,
          all(c["text"].startswith("**Table 3.**") for c in af_chunks))
    check("AF s.27: devam parçalarında '(devam)'", True,
          len(af_chunks) > 1 and all(CONTINUATION_SUFFIX in c["text"].split("\n")[0] for c in af_chunks[1:]))
    ohca_chunks = [c for c in chunk_document(remove_document_references(docs[OHCA])) if c["page_number"] == 4]
    table_chunks = [c for c in ohca_chunks if "|" in c["text"]]
    check("OHCA s.4: tablo chunk'ları başlıkla başlıyor", True,
          bool(table_chunks) and all(c["text"].startswith("**Table 1 Characteristics") for c in table_chunks))

    # 9) _text_key
    check("_text_key: '<' ile '>' arasındaki metin korunuyor", True,
          "castleaf" in _text_key("(P < .001) CASTLE-AF Recurrence >15 s"))
    check("_text_key: gerçek etiketler siliniyor", "a1b", _text_key("a<sup>1</sup><br>b"))

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<30}  {'Bulunan':<30}  Sonuç")
    print("-" * (width + 74))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:30]:<30}  {act[:30]:<30}  {'OK' if passed else 'HATA'}")
    if other_changed:
        print("\nBeklenmeden değişen sayfalar:", other_changed[:10])
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
