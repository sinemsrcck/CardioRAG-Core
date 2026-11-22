"""Pilot tablo kayıtlarını tablo/sütun başlıklarıyla eşleyen deterministik modül (LLM yok, SQL yazımı yok).

Sorun: pilot çıkarım (src/pilot_auto_extract.py) tablo satırını başlıksız saklıyor;
"|HbA1c|7.83±1.2|6.23±1.2|<0.001|" hangi değerin hangi gruba ait olduğunu söylemiyor.
Bu modül pdf_pages metnindeki markdown tabloları ayrıştırır, kanit_turu=tablo kayıtlarını
ilgili satıra bağlar ve klinik_bulgular sütunlarına (yeni tablo/sütun açmadan) bir SQL
ÖNİZLEMESİ üretir. Ayrıca src.pilot_clinical.import_records'a doğrudan verilebilecek aktarım
kayıtları (grup/analiz başına bir kayıt, review_status YOK) yazar. Veritabanına bağlanmaz,
bilgi_turu/baglam değerlerini değiştirmez.

Kurallar:
- Ham kanıt (tablo satırı) birebir korunur; ayrıca <br>, <sup>, ** temizlenmiş metin üretilir.
- Bozuk başlık parçaları (ör. "Dhi f", "emograpcs eatures") düzeltilmez, rapora yazılır.
- Hiyerarşi/alt kırılım (ör. CCB -> Dihydropyridine) tablo yapısında yazmadığı için çıkarılmaz.
- Tablo kaydının klinik_bilgi_en'i ham tablo satırı değil, temiz satır adıdır (row_name):
  "CAD n (%)|119 (31.98)|…" -> "CAD"; "Age (years)" -> "Age (years)" (klinik birim kalır).
  İstatistik etiketi (n (%), mean ± SD, median (IQR) …) atılır. Ad kanıt satırının birebir alt
  dizesidir: <sup>2</sup> gibi etiketler adda ham kalır, çünkü kanit_cumle ham satırdır ve
  validate_records / data_quality --pilot (pilot_bilgi_kanitta_yok) birebir alt dize arar;
  "^2" gösterimi sunum katmanında (sql/sunum_hoca v_sunum_taban evidence) üretilir.

Çalıştırma:
    python -m src.pilot_tablo --pdf data/pdfs/TK-cardiovascular36-2-1.pdf --findings data/review/pilot/pilot_findings_v3_2.json

--findings zorunludur (varsayılan pilot girdisine dönülmez) ve PDF ile aynı belgeye ait olmalıdır
(check_findings_document). Varsayılan çıktı adı belgeye göredir; başka belgenin çıktısının üzerine
--overwrite ile bile yazılmaz.

Genel tablo kuralları (makaleye özel değil; pilot çıktısı değişmez, evaluation/test_pilot_tablo.py sınar):
- "Table I:" gibi Roma rakamlı başlık sayıya çevrilir.
- Dipnot satırı (bütün dolu hücreleri _…_) ve ara başlık / biçim satırı ("n (%)", "Median (Q1-Q3)", başlık
  tekrarı) veri değildir; biçim satırı sonraki satırların biçimini belirler (taslak_notu.bicim_etiketi,
  değerle tutarlıysa) ve önceki bölümün kapsamını bitirir. Başlıktan önceki kalın etiket satırı ("( A )")
  sütun etiketidir (taslak_notu.sutun_etiketi).
- Başlığı boş, değerleri etiket olan sütun (ör. "A-B") grup değildir: etiket satır adına eklenir, birleşik
  hücrenin adı bloktaki satırlara taşınır ("Pairwise comparation A-B"). Boş değerli grup / analiz kaydı
  üretilmez.
- "Sex<br>Female<br>Male" gibi alt satırlar (ad ve değer hücreleri aynı sayıda parça, en az 2 alt satır):
  ilk parça bölüm, kalanlar ayrı satır.
- Hücre sonundaki işaretler değerden ayrılır: p hücresinde test işareti (u, p, f…; yalnız kalan değer bir p
  ise), ad hücresinde test işareti, grup hücresinde post-hoc işareti (I, II…) -> taslak_notu.
- Karmaşık tablo (kalın ikinci başlık satırı, metin değerli değer sütunu, sayısal satır adı) otomatik
  ayrıştırılmaz: "elle_inceleme_gerekli" sorunu, kayıt üretilmez.

Ekip kararları (Troponin tablo denemesi; Zehra + Sinem). Hepsi genel kural; pilot kayıtları değişmez:
- (6) Total sütunu: (n=…) başlığın her yerinden okunur; " / Median (Q1-Q3)" gibi biçim ifadesi addan ayrılır ve
  değer biçimi olur (ara satırdaki biçim değerle uymuyorsa). Adı TOTAL_GROUP'a uyan sütun ("Total", "Overall",
  "All patients" …) veri olarak kalır ama karşılaştırma grubu sayılmaz: taslak_notu.sutun_rolu = 'toplam',
  comparator / p yok; comparator yalnız gerçek gruplar arasında (tam 2 grup varsa).
- (8) p sütununda "7.8 (2.4-25.1)<br>or" (or/hr/rr işareti): p değil, satırın etki ölçüsü -> ayrı kayıt
  (etki_turu OR, deger, ci95; hasta_grubu yok; taslak_notu.etki_kaynagi = 'p_sutunu'); o satırın grup kayıtlarında
  p boş. p sütununun asıl p değerleri olduğu gibi kalır.
- (11) Aynı tabloda birden fazla bölümde geçen ad ("Non risc"): bölüm taslak_notu.alt_bolum'da, klinik_bilgi_en
  değişmez; sunumda yalnız bu adlara bölüm öneki eklenir (sql/sunum_hoca v_sunum_taban.bolum_oneki).
- (12) Tanısal performans tablosu (_mark_diagnostic: p / etki sütunu yok, satırlarda en az iki çekirdek ölçü —
  sensitivity, specificity, PPV, NPV, accuracy, AUC): sütunlar test (rol 'test', "( A )" etiket harfi), kayıt =
  test x ölçü (_diagnostic_preview): mudahale_en = test, mudahale_turu 'tanı testi', sonuc_olcutu_tr = ölçü,
  outcome_type 'tanısal performans'; AUC 'AUC / C-istatistigi', odds ratio OR, oranlar 'oran / yuzde', diğerleri
  'diger'; "±SE" / "(SE.)" değerden ayrılır (taslak_notu.se, se_bicimi). P satırı p_link ile AUC'ye bağlanır
  (taslak_notu.p_baglandigi_satir; p sayısı AUC satırının kanıtında olmadığı için AUC kaydına yazılmaz), bağlanamazsa
  ayrı kayıt + 'p_satiri_baglanamadi' sorunu. "P (A-B)=0.291" satırı karşılaştırma kaydı (mudahale_en A testi,
  comparator B testi, p).
- "Odds Ratio (SE.)": "(SE.)" istatistik etiketi, birim değil (STAT_LABEL).
- Grup içi test satırı (within_group_reason; ör. "P value for intra groups", "Pairwise comparison A-B"): grup
  sütunlarındaki (Total dahil) bütün dolu hücreler p değeri ve satırın p sütunu boş; ayrıca satır adı / bölüm grup
  içi diyor (WITHIN_GROUP_NAME) ya da satırın test işaretinin dipnot anlamı grup içi / tekrarlı ölçüm testi
  (WITHIN_GROUP_TEST: Friedman, Dunn, Wilcoxon signed-rank, paired t, repeated measures …). Her sütun o grubun
  kendi içindeki karşılaştırmasıdır: comparator / p / p_sutun yazılmaz; taslak_notu.karsilastirma_turu =
  'grup_ici' ve grup_ici_nedeni. src.data_quality grup çifti kontrollerini bu satırlara uygulamaz.
- Not: Troponin Table II'de "Accuracyrate" kanıt metninde de boşluksuz: PDF metin katmanında "Accuracy rate"
  (gerçek boşluk karakteri) var, ama pymupdf4llm 2.1'in layout yolu (pymupdf_layout kuruluyken varsayılan) bu
  hücrenin boşluğunu düşürüyor; eski rag yolu ve page.find_tables().to_markdown() boşluğu koruyor. Ad kanıtta
  birebir olmak zorunda olduğu için burada düzeltilmez (src/pdf_loader.py / kütüphane düzeyinde bir karar).
"""
import argparse
import csv
import json
import re
from pathlib import Path

from src.pilot_clinical import STRUCTURED_FIELDS

PILOT_DIR = Path('data/review/pilot')
# Güncel pilot çıktısı (PDF yükleyici v4 ile 190/190 geçerli); yoksa eski v2. --findings ile başka girdi verilir.
# Not: taslak_notu.pilot_v2_sira adı geriye uyum için korunur; değeri girdi dosyasındaki 1 tabanlı sıradır.
DEFAULT_JSON = next((p for p in (PILOT_DIR / 'pilot_findings_v3_2.json', PILOT_DIR / 'pilot_findings_v2.json')
                     if p.exists()), PILOT_DIR / 'pilot_findings_v3_2.json')
DEFAULT_CSV = PILOT_DIR / '02_zehra_klinik_incelemesi.csv'
DEFAULT_OUTPUT = PILOT_DIR / 'pilot_tablo_zengin.json'
VERSION = 'pilot-tablo-v1'

# SQL önizlemesinde kullanılan küme değerleri. src.clinical_validation'daki izinli
# kümelerin elemanları olmalı (evaluation/test_pilot_tablo.py doğrular). Burada içe
# aktarılmıyor: o modül src.database'i (mysql) yüklüyor, bu modül DB'den bağımsız kalmalı.
ETKI_N_PCT = 'oran / yuzde'
ETKI_OTHER = 'diger'
MUDAHALE_ILAC = 'arka plan tedavisi'
MUDAHALE_BOS_NEDENI = ('İlaç satırı değil (grup ölçümü / ilişki satırı); aktarım yolu '
                       '(pilot_clinical.import_records) mudahale_turu\'yu zorunlu tutmuyor, boş kalması sorun değil')
# import_records'a gidecek temel (Sinem'in) alanlar; pilot kaydından aynen alınır. Tablo kaydında
# klinik_bilgi_en istisnadır: ham satır yerine temiz satır adı (row_name) yazılır.
TRANSFER_BASE_FIELDS = ('bilgi_turu', 'baglam', 'klinik_bilgi', 'klinik_bilgi_en', 'kanit_cumle', 'sayfa', 'kanit_turu')
OUTCOME_KULLANIM = 'uygulama oranı'
OUTCOME_POPULASYON = 'çalışma popülasyonu'
EFFECT_TYPES = (('odds ratio', 'OR'), ('hazard ratio', 'HR'), ('relative risk', 'RR'),
                ('risk ratio', 'RR'), ('incidence rate ratio', 'IRR'))
ANALYSIS_LABELS = ('univariate', 'multivariate', 'univariable', 'multivariable', 'unadjusted', 'adjusted')
# Ekip kararları (Troponin tablo denemesi; madde 6, 8, 12). Küme değerleri src.clinical_validation'da.
SUTUN_TOPLAM = 'toplam'  # taslak_notu.sutun_rolu: Total sütunu veri olarak kalır, karşılaştırma grubu sayılmaz
ETKI_AUC = 'AUC / C-istatistigi'
MUDAHALE_TANI = 'tanı testi'
OUTCOME_TANISAL = 'tanısal performans'
TABLO_TANISAL = 'tanisal_performans'  # table['tablo_turu']
# Toplam sütunu adı (makaleye özel değil): "Total", "Overall", "All patients", "Whole cohort", "Toplam" …
TOTAL_GROUP = re.compile(
    r'^(?:total|overall|all|all\s+(?:patients|subjects|participants|cases|individuals|groups)|combined'
    r'|(?:whole|entire|total|overall)\s+(?:cohort|sample|population|group|study\s+population)|study\s+population'
    r'|toplam|tüm\s+hastalar|tümü)$', re.IGNORECASE)
GROUP_N_ANY = re.compile(r'\s*\(\s*n\s*=\s*(\d+)\s*\)', re.IGNORECASE)
# p sütununda değer + etki işareti ("7.8 (2.4-25.1)<br>or"): p değil, o satırın etki ölçüsü (dipnot: "or Odds Ratio").
EFFECT_MARKS = {'or': 'OR', 'hr': 'HR', 'rr': 'RR'}
# Tanısal performans tablosu satır türleri (satır adına göre, makaleye özel değil). Sıra önemli: ilk eşleşen.
DIAG_KINDS = (
    ('cutoff', r'cut[\s-]?off|threshold|kesim'),
    ('sensitivity', r'sensitivity|duyarl'),
    ('specificity', r'specificity|özgüll|ozgull'),
    ('ppv', r'\bppv\b|positive\s+predictive'),
    ('npv', r'\bnpv\b|negative\s+predictive'),
    ('accuracy', r'accuracy|doğruluk|dogruluk'),
    ('auc', r'\bauc\b|area\s+under|c[\s-]?statistic|c[\s-]?index'),
    ('youden', r'youden'),
    ('lr', r'likelihood\s+ratio'),
    ('or', r'odds\s+ratio'),
    ('hr', r'hazard\s+ratio'),
    ('rr', r'relative\s+risk|risk\s+ratio'),
)
DIAG_CORE = {'sensitivity', 'specificity', 'ppv', 'npv', 'accuracy', 'auc'}
DIAG_PROPORTION = {'sensitivity', 'specificity', 'ppv', 'npv', 'accuracy'}
DIAG_EFFECT = {'auc': ETKI_AUC, 'or': 'OR', 'hr': 'HR', 'rr': 'RR'}
P_ROW = re.compile(r'^p(?:\s*-?\s*value)?\.?$|^p\s+değeri$', re.IGNORECASE)
# İkili karşılaştırma hücresi: "P (A-B)=0.291" (harfler sütun etiketleri: "( A )").
PAIR_P = re.compile(r'^p\s*\(\s*([A-Za-z])\s*[-–]\s*([A-Za-z])\s*\)\s*([=<>≤≥]?)\s*(\d*\.?\d+)$', re.IGNORECASE)
LETTER_LABEL = re.compile(r'^\(?\s*([A-Za-z])\s*\)?$')
# Satır adının sonunda standart hata etiketi ("AUC±SE.", "Odds Ratio (SE.)"): değer "0.755 ± 0.072" / "11.84 (0.70)".
SE_LABEL = re.compile(r'(?:±\s*|\(\s*)s\.?e\.?m?\.?\s*\)?\s*$', re.IGNORECASE)
VALUE_PM = re.compile(r'^([<>]?\s*-?\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)$')
VALUE_PAREN = re.compile(r'^(-?\d+(?:\.\d+)?)\s*\(\s*(\d+(?:\.\d+)?)\s*\)$')
ROC_FOOTNOTE = re.compile(r'\broc\b|\bauc\b|area\s+under|hanley|honley|mc\s*ne', re.IGNORECASE)
# Grup içi test satırı (makaleye özel değil): her grup sütununda o grubun KENDİ içindeki karşılaştırmasının p'si
# (zaman noktaları / tekrarlı ölçümler; ör. "P value for intra groups", "Pairwise comparison A-B"). Gruplar arası
# karşılaştırma değildir: comparator yazılmaz, taslak_notu.karsilastirma_turu = 'grup_ici'.
KARSILASTIRMA_GRUP_ICI = 'grup_ici'
WITHIN_GROUP_NAME = re.compile(
    r'\bintra[\s-]?groups?\b|\bwithin[\s-]+(?:the\s+)?(?:groups?|subjects?)\b|\bpairwise\b|\bpost[\s-]?hoc\b'
    r'|\bgrup\s*içi\b|\bikili\s+karşılaştırma', re.IGNORECASE)
# Dipnotta test işaretinin anlamı grup içi / tekrarlı ölçüm testi mi ("f Friedman Test; Post Hoc Test: Dunn's Test").
WITHIN_GROUP_TEST = re.compile(
    r'friedman|\bdunn|wilcoxon\s+signed|signed[\s-]?rank|paired[\s-]+(?:samples?\s+)?t\b|paired\s+samples?'
    r'|repeated[\s-]+measures?|cochran\W*s?\s+q\b|mcnemar|tekrarl[ıi]\s+ölçüm', re.IGNORECASE)

SEPARATOR = re.compile(r'^\|(?:\s*:?-{3,}:?\s*\|)+\s*$')
# Tablo başlığı: "TABLE 1:", "Table 2.", Roma rakamıyla "Table I:", "Table II." (sayıya çevrilir).
CAPTION = re.compile(r'^[\s#>*_]*table\s*(\d+|[IVXLC]+)\s*[:.]', re.IGNORECASE)
ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
GROUP_N = re.compile(r'^(.*?)\s*\(\s*n\s*=\s*(\d+)\s*\)\s*$', re.IGNORECASE)
MEAN_SD = re.compile(r'^-?\d+(?:\.\d+)?\s*±\s*\d+(?:\.\d+)?$')
N_PCT = re.compile(r'^\d+\s*\(\s*\d+(?:\.\d+)?\s*\)$')
VAL_RANGE = re.compile(r'^(-?\d+(?:\.\d+)?)\s*\(\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*\)$')
N_PCT_SUFFIX = re.compile(r'\s+n\s*\(%\)\s*$', re.IGNORECASE)
UNIT_PAREN = re.compile(r'^(.*?)\s*\(([^()]+)\)\s*$')
UNIT_TOKEN = re.compile(r'^[A-Za-zµ%]+(?:/[A-Za-zµ0-9^]+)+$')
# Satır adının sonundaki istatistik etiketi (klinik birim değil): "n (%)", "n, %", "mean ± SD",
# "mean (SD)", "median (IQR)", "median [IQR]", "median (range)", "median (Q1-Q3)"; isteğe bağlı parantez
# ve önündeki virgül/boşlukla. "(%)" tek başına birimdir (ör. "LvEF (%)") ve kalır.
# "±SE", "±SD", "± SEM" (ör. "AUC±SE.") da istatistik etiketidir.
STAT_LABEL = re.compile(
    r'[\s,]*\(?\s*(?:n\s*\(\s*%\s*\)|n\s*,\s*%|mean\s*(?:±|\+/-|\+-)\s*s\.?d\.?|mean\s*\(\s*s\.?d\.?\s*\)'
    r'|median\s*[(\[]\s*(?:iqr|range|q1\s*[-–,]\s*q3)\s*[)\]]|median\s*,\s*iqr'
    r'|±\s*s\.?(?:d|e|em)\.?)\s*\)?\s*$'
    # Parantezli standart hata / sapma etiketi: "Odds Ratio (SE.)" -> "Odds Ratio" (SE birim değil).
    r'|[\s,]*\(\s*s\.?(?:d|e|em)\.?\s*\)\s*$', re.IGNORECASE)
# Değer hücresinin TAMAMI bir istatistik / biçim etiketi mi (ara başlık satırı için): "n (%)", "%",
# "Median (Q1-Q3)", "Median (Q1.-Q3.)", "Median (IQR)", "Mean ± SD", "X±SD", "(X±SD) (minimum-maximum)".
CELL_STAT = re.compile(
    r'^(?:\(?\s*(?:n\s*\(\s*%\s*\)|n\s*,\s*%|%|n|(?:mean|x)\s*(?:±|\+/-|\+-)\s*s\.?[de]\.?m?'
    r'|(?:mean|median)\s*[(\[]\s*(?:iqr|range|s\.?d\.?|q1\.?\s*[-–,]\s*q3\.?|min(?:imum)?\.?\s*[-–]\s*max(?:imum)?\.?)\s*[)\]]'
    r'|median\s*,\s*iqr|min(?:imum)?\.?\s*[-–]\s*max(?:imum)?\.?)\s*\)?\s*)+$', re.IGNORECASE)
# Hücrenin sonundaki ayrı <br> parçası olarak işaretler: test işareti (p hücresinde / ad hücresinde, ör. "u", "p",
# "f", "or"; anlamı tablo dipnotunda) ve grup hücresinde post-hoc anlamlılık işareti (I, II, …).
TEST_MARK = re.compile(r'^[a-z]{1,2}$')
POSTHOC_MARK = re.compile(r'^(?:I|II|III|IV|V|VI)$')
P_LIKE = re.compile(r'^[<>=≤≥]?\s*(?:\d+(?:\.\d+)?|\.\d+)$')
# Değer hücresi sayısal bir değerle başlıyor mu (karmaşık tablo tespiti için).
VALUE_START = re.compile(r'^[<>=≤≥±~+\-]?\s*\.?\d')
ITALIC_PART = re.compile(r'^_[^_](?:.*[^_])?_$|^_[^_]_$', re.DOTALL)
SINGLE_LABEL_PAREN = re.compile(r'^(.*?)\s*\(\s*([A-Z])\s*\)\s*$')
BOLD_EDGE = re.compile(r'^(?:\*\*|__)+|(?:\*\*|__)+$')
# Başlıkta tek harfin kelimeden kopması ("u nivariate"); a/I gerçek kelime olabilir.
SPLIT_LETTER = re.compile(r'(?<![A-Za-z])[b-hj-zB-HJ-Z] [a-z]{3,}')


# ---------------------------------------------------------------------------
# Metin temizleme
# ---------------------------------------------------------------------------

def clean(text):
    """<br>, <sup>/<sub>, ** ve __ işaretlerini kaldırır; boşlukları sadeleştirir."""
    text = re.sub(r'<br\s*/?>', ' ', text or '', flags=re.IGNORECASE)
    # Üst simge anlamı korunur: x10<sup>3</sup> -> x10^3 (x103 değil).
    text = re.sub(r'<sup>\s*(.*?)\s*</sup>', r'^\1', text, flags=re.IGNORECASE)
    text = re.sub(r'</?sub>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?[A-Za-z][^>]*>', ' ', text)
    text = text.replace('**', '').replace('__', '')
    return re.sub(r'\s+', ' ', text).strip()


def row_name(first_cell):
    """Tablo satırının temiz adı (klinik_bilgi_en için), ham ilk hücreden.

    - <br> ile bölünmüşse son parça (öndeki parçalar bölüm başlığı kalıntısıdır; bkz. satira_yapismis_parca).
    - Sondaki istatistik etiketi atılır (STAT_LABEL); klinik birim kalır ("Glucose (mg/dL)").
    - Kenardaki ** / __ atılır; başka hiçbir şey değişmez (boşluk, <sup> ham kalır), böylece ad ham
      kanıt satırının birebir alt dizesidir.
    """
    parts = br_parts(first_cell)
    name = (parts[-1] if parts else '').strip()
    previous = None
    while name != previous:
        previous = name
        name = BOLD_EDGE.sub('', STAT_LABEL.sub('', name)).strip()
    return name


def split_cells(line):
    inner = line.strip()
    if inner.startswith('|'):
        inner = inner[1:]
    if inner.endswith('|'):
        inner = inner[:-1]
    return inner.split('|')


def br_parts(cell):
    return [p for p in re.split(r'<br\s*/?>', cell, flags=re.IGNORECASE) if clean(p)]


def header_cell(cell):
    """(başlık, başlık dışı parçalar). Çok satırlı başlıkta kalın parçalar ' / ' ile birleşir;
    kalın olmayan artık parça başlığa katılmaz (ör. '**Variable**<br>Dhi f' -> 'Variable', ['Dhi f'])."""
    parts = br_parts(cell)
    bold = [p for p in parts if p.strip().startswith('**') or p.strip().startswith('__')]
    if not bold:
        return clean(cell), []
    stray = [clean(p) for p in parts if p not in bold]
    return ' / '.join(clean(p) for p in bold), stray


def split_unit(name):
    """'Glucose (mg/dL)' -> ('Glucose', 'mg/dL'); 'Female gender n (%)' -> ('Female gender', 'n (%)');
    'BMI kg/m2' -> ('BMI', 'kg/m2'); tek kelimelik adlar (E/e') bölünmez.

    Tek büyük harfli parantez birim değil etikettir: 'hs Trop I 0h ng/l (A)' -> ('hs Trop I 0h (A)', 'ng/l').
    Sondaki diğer istatistik etiketleri (STAT_LABEL, ör. '±SE') birim sayılmaz, adda tutulmaz."""
    name = name.strip()
    match = N_PCT_SUFFIX.search(name)
    if match:
        return name[:match.start()].strip().rstrip(','), 'n (%)'
    label = SINGLE_LABEL_PAREN.match(name)
    if label and label.group(1).strip():
        base, unit = split_unit(label.group(1))
        return f'{base} ({label.group(2)})', unit
    stripped = STAT_LABEL.sub('', name).strip()
    if stripped and stripped != name:
        return split_unit(stripped)
    match = UNIT_PAREN.match(name)
    if match and match.group(1).strip():
        return match.group(1).strip().rstrip(',').strip(), match.group(2).strip()
    tokens = name.split()
    if len(tokens) > 1 and UNIT_TOKEN.match(tokens[-1]):
        return ' '.join(tokens[:-1]), tokens[-1]
    return name, None


def roman_to_int(text):
    """'IV' -> 4; geçersizse None."""
    text = text.upper()
    if not text or any(ch not in ROMAN_VALUES for ch in text):
        return None
    total = 0
    for index, ch in enumerate(text):
        value = ROMAN_VALUES[ch]
        total += -value if index + 1 < len(text) and ROMAN_VALUES[text[index + 1]] > value else value
    return total


def table_number(token):
    return int(token) if token.isdigit() else roman_to_int(token)


def is_footnote_row(cells):
    """Bütün dolu hücreleri (her <br> parçası) italik _…_ olan satır tablo dipnotudur, veri değildir."""
    parts = [part.strip() for cell in cells for part in br_parts(cell)]
    return bool(parts) and all(ITALIC_PART.match(part) for part in parts)


def is_stat_label(value):
    return bool(value) and bool(CELL_STAT.match(value.strip()))


def _header_base(text):
    """Başlık / başlık tekrarı karşılaştırması için: ' / ' sonrası ve sondaki istatistik etiketi atılır."""
    text = clean(text).split(' / ')[0]
    previous = None
    while text != previous:
        previous = text
        text = STAT_LABEL.sub('', text).strip()
        text = re.sub(r'\s*(?:\(?\s*(?:median|mean|x)\s*[(\[±][^)\]]*[)\]]?\s*\)?|n\s*\(\s*%\s*\))\s*$', '',
                      text, flags=re.IGNORECASE).strip()
    return re.sub(r'\s+', '', text).casefold()


def stat_suffix(value):
    """'Total (n=114) n (%)' -> 'n (%)'; 'Median (Q1.-Q3.)' -> 'Median (Q1.-Q3.)'; etiket yoksa None."""
    value = clean(value)
    if is_stat_label(value):
        return value
    for start in range(len(value)):
        if value[start] == ' ' and is_stat_label(value[start + 1:]):
            return value[start + 1:]
    return None


def split_marker(cell, pattern, require=None):
    """(işaretsiz hücre, işaret). Son <br> parçası işaret desenine uyuyorsa ayrılır; require verilirse kalan
    değer o desene uymalı (ör. p hücresinde yalnız p değeri: '0.100<br>u' ayrılır, '7.8 (2.4-25.1)<br>or' ayrılmaz)."""
    parts = br_parts(cell)
    if len(parts) < 2 or not pattern.match(clean(parts[-1])):
        return cell, None
    rest = '<br>'.join(parts[:-1])
    if require is not None and not require.match(clean(rest)):
        return cell, None
    return rest, clean(parts[-1])


def value_format(value):
    if MEAN_SD.match(value):
        return 'ortalama±SS'
    if N_PCT.match(value):
        return 'n (%)'
    if VAL_RANGE.match(value):
        return 'değer (aralık)'
    return 'diger'


# ---------------------------------------------------------------------------
# Tablo ayrıştırma
# ---------------------------------------------------------------------------

def _caption(lines, start):
    for k in range(start - 1, max(-1, start - 6), -1):
        match = CAPTION.match(lines[k])
        if match and table_number(match.group(1)) is not None:
            return table_number(match.group(1)), lines[k].strip()
    return None, None


def _columns(headers):
    """Sütun rolleri: degisken / grup / p / etki (OR, HR...). p sütunu önceki etki sütununa bağlanır.
    Her sütunun 'anahtar'ı benzersizdir: aynı başlık tekrar ederse (Table 2'de iki 'p value')
    sahibi etki sütununun ilk başlık parçası eklenir: 'p value [Univariate analysis]'."""
    roles = _column_roles(headers)
    counts = {}
    for col in roles:
        counts[col['baslik']] = counts.get(col['baslik'], 0) + 1
    for index, col in enumerate(roles):
        key = col['baslik']
        if counts[key] > 1:
            owner = col.get('etki_sutunu')
            suffix = roles[owner]['baslik'].split(' / ')[0] if owner is not None else f'sütun {index + 1}'
            key = f'{key} [{suffix}]'
        col['anahtar'] = key
    return roles


def _column_roles(headers):
    roles = []
    for index, title in enumerate(headers):
        low = title.casefold()
        effect = next((code for key, code in EFFECT_TYPES if key in low), None)
        if index == 0:
            roles.append({'rol': 'degisken', 'baslik': title})
        elif effect:
            label = title.split(' / ')[0].strip()
            first = label.split()[0].casefold() if label else ''
            roles.append({'rol': 'etki', 'baslik': title, 'etki_turu': effect,
                          'analiz': first if first in ANALYSIS_LABELS else label})
        elif re.fullmatch(r'p(?:\s*-?\s*value)?', low.strip()):
            owner = next((i for i in range(index - 1, 0, -1) if roles[i]['rol'] == 'etki'), None)
            roles.append({'rol': 'p', 'baslik': title, 'etki_sutunu': owner})
        else:
            name, n, fmt = group_title(title)
            col = {'rol': 'grup', 'baslik': title, 'grup': name, 'n': n}
            if fmt:
                col['bicim'] = fmt
            if name and TOTAL_GROUP.match(name):
                col['toplam'] = True  # veri olarak kalır; comparator ve grup sayısına girmez
            roles.append(col)
    return roles


def group_title(title):
    """(grup adı, n, başlıktaki değer biçimi). (n=…) başlığın her yerinden okunur; ' / ' sonrası biçim ifadesi
    ("Median (Q1-Q3)", "n (%)") addan ayrılır: 'Total (n=114) / Median (Q1-Q3)' -> ('Total', 114, 'Median (Q1-Q3)').
    'Ischemic event (n=372)' -> ('Ischemic event', 372, None) (eski kuralla aynı)."""
    parts = [p.strip() for p in title.split(' / ')]
    name_parts = [parts[0]] + [p for p in parts[1:] if not is_stat_label(p)]
    fmt = ' / '.join(p for p in parts[1:] if is_stat_label(p)) or None
    name = ' / '.join(name_parts)
    match = GROUP_N_ANY.search(name)
    if not match:
        return name, None, fmt
    return re.sub(r'\s+', ' ', (name[:match.start()] + name[match.end():])).strip(), int(match.group(1)), fmt


def parse_tables(pages):
    """pages: {sayfa: metin}. Her markdown tabloyu başlık, sütunlar, bölümler ve satırlarla döndürür."""
    tables = []
    for page in sorted(pages):
        lines = pages[page].splitlines()
        i = 0
        while i < len(lines):
            if not (lines[i].strip().startswith('|') and i + 1 < len(lines) and SEPARATOR.match(lines[i + 1].strip())):
                i += 1
                continue
            start = i
            end = i + 2
            while end < len(lines) and lines[end].strip().startswith('|'):
                end += 1
            tables.append(_parse_table(page, lines, start, end, len(tables) + 1))
            i = end
    return tables


def _parse_table(page, lines, start, end, ordinal):
    no, caption_raw = _caption(lines, start)
    issues = []
    if no is None:
        issues.append({'tur': 'tablo_basligi_yok', 'aciklama': f's.{page}: tablonun üstünde "TABLE n:" başlığı bulunamadı; sıra no kullanıldı'})
    caption = clean(caption_raw) if caption_raw else None
    if caption and SPLIT_LETTER.search(caption):
        issues.append({'tur': 'baslikta_olasi_harf_kopmasi', 'parca': SPLIT_LETTER.search(caption).group(0),
                       'aciklama': 'Tablo başlığında kelimeden kopmuş tek harf; düzeltilmedi'})
    header_raw = lines[start]
    headers, stray_header = [], []
    for cell in split_cells(header_raw):
        title, stray = header_cell(cell)
        headers.append(title)
        stray_header.extend(stray)
    if stray_header:
        issues.append({'tur': 'sutun_basliginda_artik_parca', 'parca': stray_header, 'ham': header_raw.strip(),
                       'aciklama': 'Başlık hücresinde kalın olmayan parça; başlığa katılmadı, düzeltme yapılmadı'})
    columns = _columns(headers)
    rows, sections, section, first_data = [], [], None, True
    skipped, footnotes, formats, complex_reasons = [], [], {}, []
    for line_no in range(start + 2, end):
        raw = lines[line_no]
        cells = split_cells(raw)
        cleaned = [clean(c) for c in cells]
        if is_footnote_row(cells):  # dipnot (_…_): veri değil, kayıt üretmez
            footnotes.append(' '.join(c for c in cleaned if c))
            skipped.append({'tur': 'dipnot', 'ham': raw.strip()})
            continue
        if cleaned and cleaned[0] and not any(cleaned[1:]):
            section = cleaned[0]
            sections.append(section)
            continue
        if not any(cleaned):
            continue
        filled = [(col, v) for col, v in zip(columns[1:], cleaned[1:]) if v]
        if not cleaned[0] and filled and all(is_stat_label(v) or _header_base(v) == _header_base(col['baslik'])
                                             for col, v in filled):
            # Ara başlık / değer biçimi satırı: sonraki satırların biçimini belirler, önceki bölümün kapsamı biter.
            formats = {col['anahtar']: stat_suffix(v) for col, v in filled if stat_suffix(v)}
            section = None
            skipped.append({'tur': 'bicim_satiri', 'ham': raw.strip(), 'bicim': formats})
            continue
        nonempty = [c.strip() for c in cells if clean(c)]
        all_bold = all(c.startswith(('**', '__')) for c in nonempty)
        if first_data and not cleaned[0] and filled and all_bold \
                and not any(ch.isdigit() for _, v in filled for ch in v):
            for col, v in filled:  # sütun etiketi satırı, ör. "( A ) | ( B ) | ( C )"
                col['etiket'] = v
            skipped.append({'tur': 'sutun_etiketi_satiri', 'ham': raw.strip()})
            continue
        if first_data and cleaned[0] and len(filled) >= 2 and all_bold:
            complex_reasons.append('çok seviyeli başlık: ilk veri satırı kalın ikinci bir başlık satırı')
        name_cell, name_mark = split_marker(cells[0], TEST_MARK)
        parts = br_parts(name_cell)
        marks, cell_parts, p_effects = {}, {}, {}
        for col, cell in zip(columns[1:], cells[1:]):
            mark = None
            if col['rol'] == 'p':
                cell, mark = split_marker(cell, TEST_MARK, require=P_LIKE)
                if mark is None:
                    # p sütununda etki ölçüsü: "7.8 (2.4-25.1)<br>or" -> p değil, satırın OR'u (ekip kararı 8).
                    value_cell, effect_mark = split_marker(cell, TEST_MARK, require=VAL_RANGE)
                    if effect_mark in EFFECT_MARKS:
                        found = VAL_RANGE.match(clean(value_cell))
                        p_effects[col['anahtar']] = {'sutun': col['anahtar'], 'etki_turu': EFFECT_MARKS[effect_mark],
                                                     'isaret': effect_mark, 'deger': found.group(1),
                                                     'ci95': f'{found.group(2)}-{found.group(3)}'}
                        cell = value_cell
            elif col['rol'] == 'grup':
                cell, mark = split_marker(cell, POSTHOC_MARK)
            if mark:
                marks[col['anahtar']] = mark
            cell_parts[col['anahtar']] = br_parts(cell)
        group_parts = [cell_parts[c['anahtar']] for c in columns[1:]
                       if c['rol'] == 'grup' and cell_parts.get(c['anahtar'])]
        sub = len(parts) - 1
        if sub >= 2 and group_parts and all(len(g) == sub for g in group_parts):
            # <br> ile alt satırlar: "Sex<br>Female<br>Male" + "44 (38.6)<br>70 (61.4)" -> bölüm "Sex", iki satır.
            local = clean(parts[0])
            for k in range(sub):
                cells_map = {key: clean(vals[k] if len(vals) == sub else '<br>'.join(vals))
                             for key, vals in cell_parts.items()}
                temiz = ' | '.join(c for c in [clean(parts[k + 1]), *cells_map.values()] if c)
                rows.append(_row(len(rows) + 1, page, raw, temiz, parts[k + 1], local, cells_map, marks,
                                 name_mark, formats, p_effects))
            first_data = False
            continue
        name_raw = clean(parts[-1]) if parts else ''
        fused = [clean(p) for p in parts[:-1]]
        if fused:
            issues.append({'tur': 'satira_yapismis_parca', 'parca': fused, 'degisken': name_raw, 'ham': raw.strip(),
                           'aciklama': 'Değişken hücresinde <br> ile ayrılmış ek parça (olası bölüm başlığı kalıntısı); '
                                       'değişken adı son parçadan alındı, parça bölüm başlığı olarak kullanılmadı'})
        if section is None and first_data and (fused or stray_header):
            issues.append({'tur': 'ilk_bolum_basligi_okunamadi',
                           'parca': {'sutun_basligi': stray_header, 'ilk_satir': fused},
                           'aciklama': 'İlk alt bölüm başlığı başlık hücresine ve ilk satıra bölünmüş görünüyor; '
                                       'alt_bolum=None bırakıldı, ad uydurulmadı'})
        first_data = False
        cells_map = {}
        for col, value in zip(columns[1:], cleaned[1:]):
            key = col['anahtar']
            cells_map[key] = clean('<br>'.join(cell_parts[key])) if key in marks or key in p_effects else value
        temiz = ' | '.join(c for c in cleaned if c)
        rows.append(_row(len(rows) + 1, page, raw, temiz, name_cell, section, cells_map, marks, name_mark, formats,
                         p_effects))
    _label_columns(columns, rows)
    rows, empty = _merge_labels(columns, rows)
    skipped += empty
    diagnostic = _mark_diagnostic(columns, rows)
    complex_reasons += _complex_reasons(columns, rows)
    table = {'tablo_no': no if no is not None else ordinal, 'sayfa': page, 'tablo_basligi': caption,
             'tablo_basligi_ham': caption_raw, 'sutun_basliklari_ham': header_raw.strip(),
             'sutunlar': columns, 'alt_bolumler': sections, 'satirlar': rows, 'sorunlar': issues,
             'atlanan_satirlar': skipped}
    if diagnostic:
        table['tablo_turu'] = TABLO_TANISAL
    if footnotes:
        table['dipnot'] = footnotes
    if complex_reasons:
        # Otomatik ayrıştırılmaz: satırlar kayda dönüşmez; ham satırlar elle inceleme için saklanır.
        table['elle_inceleme_satirlari'] = [lines[i].strip() for i in range(start + 2, end) if lines[i].strip()]
        table['satirlar'] = []
        issues.append({'tur': 'elle_inceleme_gerekli', 'parca': sorted(set(complex_reasons)),
                       'aciklama': 'Karmaşık tablo (çok seviyeli / bölünmüş başlık, iç içe tablo); otomatik '
                                   'ayrıştırılmadı, kayıt üretilmedi; elle inceleme gerekli'})
    return table


def _row(number, page, raw, temiz, name_cell, section, cells_map, marks, name_mark, formats, p_effects=None):
    """Satır sözlüğü. Mevcut anahtarlar aynı; yeni anahtarlar (isaretler, ad_isareti, bicim_etiketi, p_etki) yalnız
    doluysa. p_etki: p sütununda p yerine duran etki ölçüsü ({p sütunu: {etki_turu, deger, ci95, isaret}})."""
    parts = br_parts(name_cell)
    name_raw = clean(parts[-1]) if parts else ''
    variable, unit = split_unit(name_raw)
    row = {'satir_no': number, 'sayfa': page, 'ham': raw.strip(), 'temiz': temiz, 'degisken_ham': name_raw,
           'ad': row_name(name_cell), 'degisken': variable, 'unit': unit, 'alt_bolum': section,
           'sutun_degerleri': cells_map}
    if marks:
        row['isaretler'] = dict(marks)
    if name_mark:
        row['ad_isareti'] = name_mark
    if formats:
        row['bicim_etiketi'] = dict(formats)
    if p_effects:
        row['p_etki'] = {key: dict(value) for key, value in p_effects.items()}
    return row


def _label_columns(columns, rows):
    """Başlığı boş sütun: değerleri etiketse (rakamsız, ör. 'A-B') hasta grubu değil etiket sütunu ('etiket');
    hiç değer taşımıyorsa boş sütun ('bos'). İkisi de grup kaydı üretmez."""
    for col in columns[1:]:
        if col['rol'] != 'grup' or clean(col['baslik']):
            continue
        filled = [v for v in (r['sutun_degerleri'].get(col['anahtar'], '') for r in rows) if v]
        if not filled:
            col['rol'] = 'bos'
        elif not any(ch.isdigit() for v in filled for ch in v):
            col['rol'] = 'etiket'


def _merge_labels(columns, rows):
    """Etiket sütunlu satır blokları: etiket satır adına eklenir; birleşik hücrenin adı (blokta tek dolu ad, ör.
    ortadaki 'Pairwise comparation') bloktaki bütün satırlara taşınır. Ad yine boşsa satır atılır ('bos_ad')."""
    label_keys = [c['anahtar'] for c in columns if c['rol'] == 'etiket']

    def label_of(row):
        return next((row['sutun_degerleri'][key] for key in label_keys if row['sutun_degerleri'].get(key)), None)

    kept, skipped, k = [], [], 0
    while k < len(rows):
        if label_of(rows[k]) is None:
            if rows[k]['ad'] or rows[k]['degisken']:
                kept.append(rows[k])
            else:
                skipped.append({'tur': 'bos_ad', 'ham': rows[k]['ham']})
            k += 1
            continue
        block = [rows[k]]
        k += 1
        while k < len(rows) and label_of(rows[k]) is not None \
                and not (rows[k]['degisken'] and any(r['degisken'] for r in block)):
            block.append(rows[k])
            k += 1
        names = [r['degisken'] for r in block if r['degisken']]
        base = names[0] if len(names) == 1 else None
        for r in block:
            own = label_of(r)
            r['degisken'] = f'{base} {own}' if base else own
            r['ad'] = own  # kanıtta birebir geçen parça; birleşik ad başka satırın kanıtında olabilir
            r['etiket'] = own
            kept.append(r)
    for number, r in enumerate(kept, 1):
        r['satir_no'] = number
    return kept, skipped


def diag_kind(row):
    """Tanısal performans satırının türü (DIAG_KINDS): 'sensitivity', 'auc', 'cutoff' …; P satırı 'p'; bütün değerleri
    "P (A-B)=0.291" olan satır 'karsilastirma'; tanınmayan 'diger'."""
    values = [v for v in row['sutun_degerleri'].values() if v]
    if values and all(PAIR_P.match(v) for v in values):
        return 'karsilastirma'
    name = (row['degisken'] or row['ad'] or '').strip()
    if P_ROW.match(name):
        return 'p'
    low = name.casefold()
    return next((kind for kind, pattern in DIAG_KINDS if re.search(pattern, low)), 'diger')


def _mark_diagnostic(columns, rows):
    """Genel kural (makaleye özel değil): sütunları test / zaman aralığı, satırları tanısal ölçü (en az iki farklı
    çekirdek ölçü: sensitivity, specificity, PPV, NPV, accuracy, AUC) olan, p / etki sütunu olmayan tablo tanısal
    performans tablosudur. Grup sütunları 'test' olur (hasta grubu değil); satırlara tani_olcusu yazılır."""
    if any(c['rol'] in ('p', 'etki') for c in columns[1:]) or not any(c['rol'] == 'grup' for c in columns[1:]):
        return False
    kinds = [diag_kind(r) for r in rows]
    if len(set(kinds) & DIAG_CORE) < 2:
        return False
    for col in columns[1:]:
        if col['rol'] == 'grup':
            col['rol'] = 'test'
            letter = LETTER_LABEL.match(clean(col.get('etiket') or ''))
            if letter:
                col['etiket_harf'] = letter.group(1).upper()
    for row, kind in zip(rows, kinds):
        row['tani_olcusu'] = kind
    return True


def _complex_reasons(columns, rows):
    """Genel karmaşık tablo kuralları (makaleye özel değil): değer sütununda çoğunluk metin (iç içe / yan yana
    tablo) ya da sayısal satır adı (birleşik hücreli model / grup numarası)."""
    reasons = []
    for col in columns[1:]:
        if col['rol'] in ('etiket', 'bos'):
            continue
        filled = [v for v in (r['sutun_degerleri'].get(col['anahtar'], '') for r in rows) if v]
        if filled and sum(not VALUE_START.match(v) for v in filled) * 2 > len(filled):
            reasons.append(f'metin değerli sütun (iç içe tablo): {col["baslik"] or col["anahtar"]}')
    if any(re.fullmatch(r'\d+', r['degisken'] or '') for r in rows):
        reasons.append('sayısal satır adı (birleşik hücre / model numarası)')
    return reasons


def row_analyses(table, row):
    """Etki tablosu satırı için analiz başına {analiz, etki_turu, deger, ci95, p}."""
    out = []
    for index, col in enumerate(table['sutunlar']):
        if col['rol'] != 'etki':
            continue
        value = row['sutun_degerleri'].get(col['anahtar'], '')
        match = VAL_RANGE.match(value)
        p_col = next((c for c in table['sutunlar'] if c['rol'] == 'p' and c.get('etki_sutunu') == index), None)
        out.append({'analiz': col['analiz'], 'etki_turu': col['etki_turu'], 'sutun': col['anahtar'],
                    'deger': match.group(1) if match else value,
                    'ci95': f'{match.group(2)}-{match.group(3)}' if match else None,
                    'p': row['sutun_degerleri'].get(p_col['anahtar']) if p_col else None,
                    'p_sutun': p_col['anahtar'] if p_col else None})
    return out


def _is_p_value(value):
    """Hücre tek başına bir p değeri mi: "<0.001", "0.027", "=0.05" (0–1 arası)."""
    text = clean(value or '')
    if not P_LIKE.match(text):
        return False
    try:
        return 0 <= float(re.sub(r'^[<>=≤≥]?\s*', '', text)) <= 1
    except ValueError:
        return False


def footnote_mark_meaning(table, mark):
    """Tablo dipnotunda test işaretinin anlamı ("f Friedman Test; Post Hoc Test : Dunn's Test" -> 'f' için
    "Friedman Test; Post Hoc Test : Dunn's Test"); yoksa None. Dipnot italik parçalara bölünmüş olabilir."""
    if not mark:
        return None
    text = re.sub(r'\s+', ' ', ' '.join(table.get('dipnot') or []).replace('_', ' ').replace('*', ' ')).strip()
    found = re.search(rf'(?:^|[\s,;(]){re.escape(mark)}\s+([A-Za-z][^,]*)', text)
    return found.group(1).strip() if found else None


def within_group_reason(table, row):
    """Grup içi test satırı mı (genel kural, makaleye özel değil); öyleyse gerekçe, değilse None.

    Yapı şartı: satırın grup sütunlarındaki (Total dahil) bütün dolu hücreler p değeri ve satırın p sütunu boş:
    her sütun kendi grubunun içindeki karşılaştırmayı verir, gruplar arası p yoktur. Ayrıca şunlardan biri:
    - satır adı / birleşik ad / bölüm grup içi bir test söylüyor ("intra group", "within group", "pairwise",
      "post hoc", "grup içi" …),
    - satırın test işaretinin dipnottaki anlamı grup içi / tekrarlı ölçüm testi (Friedman, Dunn, Wilcoxon
      signed-rank, paired t, repeated measures, Cochran Q, McNemar …).
    Tanısal performans tablosunda uygulanmaz (sütunlar grup değil test)."""
    if table.get('tablo_turu') == TABLO_TANISAL:
        return None
    columns = table['sutunlar']
    values = [row['sutun_degerleri'].get(c['anahtar'], '') for c in columns if c['rol'] == 'grup']
    filled = [v for v in values if v]
    if not filled or not all(_is_p_value(v) for v in filled):
        return None
    if any(row['sutun_degerleri'].get(c['anahtar']) for c in columns if c['rol'] == 'p'):
        return None
    for text in (row.get('degisken'), row.get('degisken_ham'), row.get('alt_bolum')):
        found = WITHIN_GROUP_NAME.search(text or '')
        if found:
            return f"satır adı '{text}' grup içi karşılaştırma ('{found.group(0)}'); grup hücreleri p değeri, p sütunu boş"
    meaning = footnote_mark_meaning(table, row.get('ad_isareti'))
    found = WITHIN_GROUP_TEST.search(meaning or '')
    if found:
        return (f"test işareti '{row['ad_isareti']}' dipnotta grup içi test ('{meaning}'); grup hücreleri p değeri, "
                f"p sütunu boş")
    return None


def row_groups(table, row):
    """Grup tablosu satırı için grup başına {grup, n, deger, deger_bicimi, comparator, comparator_n, p}."""
    """Total sütunu (col['toplam']) veri olarak döner ('toplam': True) ama karşılaştırma grubu sayılmaz: comparator'ı
    yok, karşılaştırma p'si ona yazılmaz, diğer grupların comparator'ına girmez. p sütununda p yerine etki ölçüsü
    varsa (row['p_etki']) grup kayıtlarında p boştur. Grup içi test satırında (within_group_reason) hiçbir kayda
    comparator / p sütunu yazılmaz; kayıtlar karsilastirma_turu = 'grup_ici' taşır."""
    groups = [c for c in table['sutunlar'] if c['rol'] == 'grup']
    comparison = [c for c in groups if not c.get('toplam')]
    p_cols = [c for c in table['sutunlar'] if c['rol'] == 'p' and c.get('etki_sutunu') is None]
    p_key = p_cols[0]['anahtar'] if len(p_cols) == 1 else None
    p_value = row['sutun_degerleri'].get(p_key) if p_key and p_key not in (row.get('p_etki') or {}) else None
    within = within_group_reason(table, row)
    out = []
    for col in groups:
        total = bool(col.get('toplam'))
        others = [] if total or within else [c for c in comparison if c is not col]
        value = row['sutun_degerleri'].get(col['anahtar'], '')
        item = {'sutun': col['anahtar'], 'grup': col['grup'], 'n': col['n'], 'deger': value,
                'deger_bicimi': value_format(value),
                'comparator': others[0]['grup'] if len(others) == 1 else None,
                'comparator_n': others[0]['n'] if len(others) == 1 else None,
                'comparator_sutun': others[0]['anahtar'] if len(others) == 1 else None,
                'p': None if total or within else p_value, 'p_sutun': None if total or within else p_key}
        if total:
            item['toplam'] = True
        if within:
            item['karsilastirma_turu'] = KARSILASTIRMA_GRUP_ICI
            item['grup_ici_nedeni'] = within
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Kayıt eşleme ve zenginleştirme
# ---------------------------------------------------------------------------

class FindingsError(ValueError):
    """Bulgu girdisi (findings) eksik ya da bu PDF'e ait değil."""


def load_records(path):
    """(kayıtlar, kaynak). JSON ana girdi (kayıt listesi ya da {"document_id", "kayitlar"}); CSV yedeği.
    Her kayda v2_sira eklenir (JSON'da indeks+1). Varsayılan girdi YOK: başka makalenin kayıtlarına sessizce
    dönülmez (path=None -> FindingsError)."""
    if path is None:
        raise FindingsError('Bulgu girdisi (--findings) zorunlu: pilot_auto_extract çıktısı gibi, bu PDF\'in '
                            'kayıtlarını içeren JSON/CSV verin. Varsayılan pilot girdisine dönülmez.')
    path = Path(path)
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('kayitlar'), list):
            data = data['kayitlar']
        if not isinstance(data, list):
            raise ValueError('JSON kökünde kayıt listesi (ya da "kayitlar" listesi) olmalı')
        records = [dict(item, v2_sira=item.get('v2_sira', index)) for index, item in enumerate(data, 1)]
    else:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            records = [dict(row, v2_sira=int(row['v2_sira']), sayfa=int(row['sayfa'])) for row in csv.DictReader(handle)]
    return records, str(path)


def declared_document_ids(path):
    """Findings dosyasının beyan ettiği belge kimlikleri: JSON kökünde "document_id", kayıtlarda "document_id",
    pilot_auto_extract checkpoint'inde (<ad>.checkpoint.json) "source_id". Hiçbiri yoksa boş küme."""
    path = Path(path)
    ids = set()
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            ids.add(data.get('document_id'))
            data = data.get('kayitlar') or []
        ids.update(item.get('document_id') for item in data if isinstance(item, dict))
    checkpoint = path.with_suffix('.checkpoint.json')
    if checkpoint.exists():
        try:
            ids.add(json.loads(checkpoint.read_text(encoding='utf-8')).get('source_id'))
        except (ValueError, AttributeError):
            pass
    return {i for i in ids if i}


def check_findings_document(records, declared, doc, pages):
    """Findings ile PDF aynı belge mi. Beyan edilen document_id varsa PDF'inkiyle aynı olmalı; yoksa içerikten:
    tablo kayıtlarının (pilot_tablo yalnız bunları kullanır) kanıtı bu PDF'in belirtilen sayfasında birebir olmalı."""
    document_id = doc.get('document_id')
    if declared:
        if declared != {document_id}:
            raise FindingsError(f'Findings başka belgeye ait: beyan edilen document_id {sorted(declared)}, '
                                f'PDF ({doc.get("filename")}) {document_id}')
        return 'document_id'
    tables = [r for r in records if r.get('kanit_turu') == 'tablo']
    if not tables:
        raise FindingsError('Findings document_id taşımıyor ve tablo kaydı yok; bu PDF\'e ait olduğu doğrulanamadı')
    missing = [r.get('v2_sira') for r in tables
               if not _needle(r.get('kanit_cumle')) or _needle(r.get('kanit_cumle')) not in (pages.get(r.get('sayfa')) or '')]
    if missing:
        raise FindingsError(f'Findings bu PDF\'e ait görünmüyor: {len(missing)}/{len(tables)} tablo kaydının kanıtı '
                            f'{doc.get("filename")} sayfasında birebir yok (ilk sıra no: {missing[:5]})')
    return 'icerik'


def _needle(quote):
    text = (quote or '').strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|'):
        text = text[:-1]
    return text.strip()


def match_rows(record, tables):
    """('eslesti', tablo, [satırlar]) | ('eslesmedi', None, None) | ('belirsiz', None, adaylar) |
    ('elle_inceleme', tablo, None). <br> ile alt satırlara bölünmüş tek PDF satırı (aynı ham satır) birden fazla
    satır olarak eşleşir; karmaşık (elle inceleme) tablonun satırı kayıt üretmez."""
    needle = _needle(record.get('kanit_cumle'))
    if not needle:
        return 'eslesmedi', None, None
    on_page = [t for t in tables if t['sayfa'] == record.get('sayfa')]
    hits = [(t, r) for t in on_page for r in t['satirlar'] if needle in r['ham']]
    if not hits:
        manual = next((t for t in on_page if any(needle in h for h in t.get('elle_inceleme_satirlari', []))), None)
        return ('elle_inceleme', manual, None) if manual else ('eslesmedi', None, None)
    if len(hits) == 1 or (len({id(t) for t, _ in hits}) == 1 and len({r['ham'] for _, r in hits}) == 1):
        return 'eslesti', hits[0][0], [r for _, r in hits]
    return 'belirsiz', None, [(t['tablo_no'], r['satir_no']) for t, r in hits]


def match_record(record, tables):
    """('eslesti', tablo, satır) | ('eslesmedi', None, None) | ('belirsiz', None, adaylar) | ('elle_inceleme', tablo, None).
    Alt satırlara bölünmüş PDF satırı için match_rows kullanılır."""
    status, table, rows = match_rows(record, tables)
    if status == 'eslesti':
        if len(rows) == 1:
            return status, table, rows[0]
        return 'belirsiz', None, [(table['tablo_no'], r['satir_no']) for r in rows]
    return status, table, rows


def table_info(table, row):
    info = {'tablo_no': table['tablo_no'], 'tablo_basligi': table['tablo_basligi'], 'sayfa': table['sayfa'],
            'satir_no': row['satir_no'], 'alt_bolum': row['alt_bolum'], 'degisken': row['degisken'],
            'degisken_ham': row['degisken_ham'], 'ad': row['ad'], 'unit': row['unit'], 'sutun_degerleri': row['sutun_degerleri'],
            'temiz_satir': row['temiz']}
    analyses = row_analyses(table, row)
    if table.get('tablo_turu') == TABLO_TANISAL:
        info['tablo_turu'] = TABLO_TANISAL
        info['tani_olcusu'] = row.get('tani_olcusu')
        info['testler'] = [{'sutun': c['anahtar'], 'test': c['grup'], 'etiket': c.get('etiket_harf'),
                            'deger': row['sutun_degerleri'].get(c['anahtar'], '')}
                           for c in table['sutunlar'] if c['rol'] == 'test']
    elif analyses:
        info['analizler'] = analyses
    else:
        info['gruplar'] = row_groups(table, row)
        formats = {g['deger_bicimi'] for g in info['gruplar']}
        info['deger_bicimi'] = formats.pop() if len(formats) == 1 else sorted(formats)
        if is_medication(row):
            info['not'] = 'Kullanım oranı n (%); doz değil (makalede doz bilgisi yok).'
    return info


def is_medication(row):
    return bool(row['alt_bolum']) and row['alt_bolum'].casefold().startswith('medication')


# ---------------------------------------------------------------------------
# SQL önizleme (yazma yok)
# ---------------------------------------------------------------------------

def format_matches(label, value):
    """Ara başlıktaki biçim etiketi bu değerle tutarlı mı: n (%) -> "44 (38.6)"; median/IQR/aralık -> "64 (53-74)";
    ortalama ± SS -> "62±10.3". Tutarsızsa (ör. n (%) etiketi altındaki medyan satırı) etiket yazılmaz."""
    low = (label or '').casefold()
    if 'n' in low.replace('min', '').replace('mean', '') and '%' in low:
        return bool(N_PCT.match(value or ''))
    if any(k in low for k in ('median', 'iqr', 'q1', 'range', 'min')):
        return bool(VAL_RANGE.match(value or ''))
    if '±' in low or 'sd' in low:
        return bool(MEAN_SD.match(value or ''))
    return False


def _extra_note(row, column_key=None, value=None, column=None):
    """Yalnız doluysa eklenen taslak_notu alanları (pilot satırlarında hiçbiri yok, kayıtlar değişmez)."""
    extra = {}
    marks = row.get('isaretler') or {}
    if column_key and marks.get(column_key):
        extra['posthoc_isareti'] = marks[column_key]
    p_marks = sorted({v for k, v in marks.items() if k != column_key and TEST_MARK.match(v)})
    if p_marks:
        extra['p_test_isareti'] = p_marks[0] if len(p_marks) == 1 else p_marks
    if row.get('ad_isareti'):
        extra['test_isareti'] = row['ad_isareti']
    # Biçim: önce ara başlık satırındaki etiket, uymuyorsa sütun başlığındaki biçim ("Total (n=114) / Median (Q1-Q3)");
    # ikisi de değerle tutarlı değilse yazılmaz.
    formats = row.get('bicim_etiketi') or {}
    candidates = [formats.get(column_key) if column_key else None, (column or {}).get('bicim')]
    label = next((c for c in candidates if c and format_matches(c, value)), None)
    if label:
        extra['bicim_etiketi'] = label
    if row.get('etiket'):
        extra['etiket'] = row['etiket']
    if column and column.get('etiket'):
        extra['sutun_etiketi'] = column['etiket']
    return extra


def sql_preview(record, table, row, document_id=None, skipped=None):
    """klinik_bulgular sütunlarıyla önizleme satırları. Mevcut sütunlar; yeni sütun yok.

    - Boş değerli grup / analiz için kayıt üretilmez; skipped listesi verilirse oraya yazılır.

    - Küme alanları (etki_turu, mudahale_turu, outcome_type) src.clinical_validation kümelerinden.
    - Grup n'leri hasta_sayisi'na yazılmaz (ekip kararı); taslak_notu'da (grup_n, comparator_n) kalır.
    - mudahale_turu yalnız ilaç satırlarında ("arka plan tedavisi"); diğerleri boş. Aktarım yolu
      (pilot_clinical.import_records) mudahale_turu'yu zorunlu tutmuyor.
    """
    base = {'document_id': document_id, 'kanit_sayfa': table['sayfa'], 'kanit_cumle': record['kanit_cumle'],
            'kanit_turu': 'tablo', 'review_status': 'pending', 'cikarma_yontemi': None, 'doz': None, 'yon': None}
    note_base = {'kaynak': VERSION, 'pilot_v2_sira': record.get('v2_sira'), 'bilgi_turu': record.get('bilgi_turu'),
                 'baglam': record.get('baglam'), 'tablo_no': table['tablo_no'], 'tablo_basligi': table['tablo_basligi'],
                 'alt_bolum': row['alt_bolum'], 'degisken': row['degisken'], 'satir_no': row['satir_no']}
    out = []
    columns = {c['anahtar']: c for c in table['sutunlar']}
    if table.get('tablo_turu') == TABLO_TANISAL:
        return _diagnostic_preview(table, row, base, note_base, skipped)
    analyses = row_analyses(table, row)
    if analyses:
        for item in analyses:
            if not item['deger']:
                if skipped is not None:
                    skipped.append({'tur': 'bos_deger', 'tablo_no': table['tablo_no'], 'satir_no': row['satir_no'],
                                    'degisken': row['degisken'], 'sutun': item['sutun']})
                continue
            note = dict(note_base, analiz=item['analiz'], sutun=item['sutun'], p_sutun=item['p_sutun'],
                        outcome_type_bos_nedeni='Retrospektif ilişki/prediktör analizi; "prognostik değer" '
                                                'boylamsal izlem gerektirir, emin olunamadı',
                        mudahale_turu_bos_nedeni=MUDAHALE_BOS_NEDENI,
                        **_extra_note(row, item['sutun'], item['deger'], columns.get(item['sutun'])))
            out.append(dict(base, sonuc_olcutu_tr=row['degisken'], unit=row['unit'], hasta_grubu=None,
                            hasta_sayisi=None, comparator=None, mudahale_en=None, mudahale_turu=None,
                            etki_turu=item['etki_turu'], deger=item['deger'], ci95=item['ci95'], p=item['p'] or None,
                            outcome_type=None, taslak_notu=json.dumps(note, ensure_ascii=False)))
        return out
    medication = is_medication(row)
    for group in row_groups(table, row):
        if not group['deger']:
            if skipped is not None:
                skipped.append({'tur': 'bos_deger', 'tablo_no': table['tablo_no'], 'satir_no': row['satir_no'],
                                'degisken': row['degisken'], 'sutun': group['sutun']})
            continue
        note = dict(note_base, sutun=group['sutun'], grup_n=group['n'], comparator_sutun=group['comparator_sutun'],
                    comparator_n=group['comparator_n'], p_sutun=group['p_sutun'], deger_bicimi=group['deger_bicimi'],
                    hasta_sayisi_bos_nedeni='Ekip kararı: grup n\'leri taslak_notu\'da (grup_n, comparator_n) kalır',
                    **_extra_note(row, group['sutun'], group['deger'], columns.get(group['sutun'])))
        if group.get('toplam'):
            note['sutun_rolu'] = SUTUN_TOPLAM  # veri olarak kalır; comparator / grup sayısına girmez (ekip kararı 6)
        if group.get('karsilastirma_turu'):
            # Grup içi test (zaman noktaları / tekrarlı ölçüm): gruplar arası karşılaştırma değil, comparator yok.
            note['karsilastirma_turu'] = group['karsilastirma_turu']
            note['grup_ici_nedeni'] = group['grup_ici_nedeni']
        if medication:
            note['not'] = 'Kullanım oranı n (%); doz değil.'
            mudahale_turu, mudahale_en, outcome, measure = MUDAHALE_ILAC, row['degisken'], OUTCOME_KULLANIM, 'kullanım oranı'
        else:
            note['mudahale_turu_bos_nedeni'] = MUDAHALE_BOS_NEDENI
            mudahale_turu, mudahale_en, outcome, measure = None, None, OUTCOME_POPULASYON, row['degisken']
        out.append(dict(base, sonuc_olcutu_tr=measure, unit=row['unit'], hasta_grubu=group['grup'], hasta_sayisi=None,
                        comparator=group['comparator'], mudahale_en=mudahale_en, mudahale_turu=mudahale_turu,
                        etki_turu=ETKI_N_PCT if group['deger_bicimi'] == 'n (%)' else ETKI_OTHER,
                        deger=group['deger'], ci95=None, p=group['p'] or None, outcome_type=outcome,
                        taslak_notu=json.dumps(note, ensure_ascii=False)))
    for effect in (row.get('p_etki') or {}).values():
        # p sütunundaki etki ölçüsü (ekip kararı 8): satırın ayrı etki kaydı; grup kayıtlarının p'si boş kalır.
        note = dict(note_base, sutun=effect['sutun'], etki_kaynagi='p_sutunu', etki_isareti=effect['isaret'],
                    outcome_type_bos_nedeni='p sütunundaki etki ölçüsü; karşılaştırılan gruplar tabloda yazmıyor, '
                                            'sonuç türü tahmin edilmedi',
                    mudahale_turu_bos_nedeni=MUDAHALE_BOS_NEDENI)
        out.append(dict(base, sonuc_olcutu_tr=row['degisken'], unit=row['unit'], hasta_grubu=None, hasta_sayisi=None,
                        comparator=None, mudahale_en=None, mudahale_turu=None, etki_turu=effect['etki_turu'],
                        deger=effect['deger'], ci95=effect['ci95'], p=None, outcome_type=None,
                        taslak_notu=json.dumps(note, ensure_ascii=False)))
    return out


def _diag_value(row, value):
    """(deger, se, se_bicimi). Satır adı SE etiketli ("AUC±SE.", "Odds Ratio (SE.)") ise SE değerden ayrılır:
    "0.755 ± 0.072" -> ('0.755', '0.072', '±'); "11.84 (0.70)" -> ('11.84', '0.70', 'parantez')."""
    if SE_LABEL.search(row.get('degisken_ham') or ''):
        for pattern, form in ((VALUE_PM, '±'), (VALUE_PAREN, 'parantez')):
            match = pattern.match(value)
            if match:
                return match.group(1).replace(' ', ''), match.group(2), form
    return value, None, None


def p_link(table, row):
    """Tanısal tablodaki P satırının bağlandığı ölçü satırı: (satır, neden) ya da (None, neden).
    Genel kural: tabloda tek AUC satırı varsa ve P satırı onun hemen altındaysa ya da dipnot ROC / AUC analizinden
    söz ediyorsa (Hanley & McNeil vb.) P o AUC'nin p'sidir. Değilse bağlanmaz (ayrı kayıt, raporlanır)."""
    aucs = [r for r in table['satirlar'] if r.get('tani_olcusu') == 'auc']
    if len(aucs) != 1:
        return None, f'tabloda {len(aucs)} AUC satırı var; P satırı tek bir ölçüye bağlanamadı'
    reasons = []
    if aucs[0]['satir_no'] == row['satir_no'] - 1:
        reasons.append('P satırı AUC satırının hemen altında')
    if ROC_FOOTNOTE.search(' '.join(table.get('dipnot') or [])):
        reasons.append('dipnot ROC / AUC analizinden söz ediyor')
    if not reasons:
        return None, 'P satırı AUC satırına bitişik değil ve dipnotta ROC / AUC yok; bağlanamadı'
    return aucs[0], '; '.join(reasons)


def _add_issue(table, issue):
    if issue not in table['sorunlar']:
        table['sorunlar'].append(issue)


def _diagnostic_preview(table, row, base, note_base, skipped=None):
    """Tanısal performans tablosu (ekip kararı 12): kayıt = test x ölçü. Test adı mudahale_en, mudahale_turu
    'tanı testi', ölçü sonuc_olcutu_tr, outcome_type 'tanısal performans'. AUC -> 'AUC / C-istatistigi', odds ratio
    -> OR, oranlar (sensitivity, specificity, accuracy …, değer %'li) -> 'oran / yuzde', diğerleri 'diger'.

    - P satırı: p_link ile AUC'ye bağlanırsa kayıt sonuc_olcutu_tr = AUC satırının adı, p dolu, deger boş;
      taslak_notu.p_baglandigi_satir AUC satırını gösterir (kanıt kuralı: p sayısı AUC satırının kanıtında yok, bu
      yüzden AUC kaydının p alanına yazılmaz; sunum ikisini birleştirir). Bağlanamazsa ayrı P kaydı + sorun.
    - "P (A-B)=0.291" satırı karşılaştırma kaydıdır: harfler sütun etiketlerinden ("( A )") teste çözülür;
      mudahale_en = A testi, comparator = B testi, p = 0.291."""
    kind = row.get('tani_olcusu') or diag_kind(row)
    tests = [c for c in table['sutunlar'] if c['rol'] == 'test']
    by_letter = {c['etiket_harf']: c for c in tests if c.get('etiket_harf')}
    link, why = p_link(table, row) if kind == 'p' else (None, None)
    if kind == 'p' and link is None:
        _add_issue(table, {'tur': 'p_satiri_baglanamadi', 'satir_no': row['satir_no'], 'aciklama': why})
    out = []
    for col in tests:
        value = row['sutun_degerleri'].get(col['anahtar'], '')
        if not value:
            if skipped is not None:
                skipped.append({'tur': 'bos_deger', 'tablo_no': table['tablo_no'], 'satir_no': row['satir_no'],
                                'degisken': row['degisken'], 'sutun': col['anahtar']})
            continue
        note = dict(note_base, sutun=col['anahtar'], tablo_turu=TABLO_TANISAL, tani_olcusu=kind,
                    **_extra_note(row, col['anahtar'], value, col))
        record = dict(base, sonuc_olcutu_tr=row['degisken'], unit=row['unit'], hasta_grubu=None, hasta_sayisi=None,
                      comparator=None, mudahale_en=col['grup'], mudahale_turu=MUDAHALE_TANI, etki_turu=None,
                      deger=None, ci95=None, p=None, outcome_type=OUTCOME_TANISAL)
        if kind == 'karsilastirma':
            match = PAIR_P.match(value)
            first, second = by_letter.get(match.group(1).upper()), by_letter.get(match.group(2).upper())
            note['karsilastirma'] = f'{match.group(1).upper()}-{match.group(2).upper()}'
            if first and second:
                record.update(mudahale_en=first['grup'], comparator=second['grup'])
                note['karsilastirilan_sutunlar'] = [first['anahtar'], second['anahtar']]
            else:
                record['mudahale_en'] = None
                note['karsilastirma_cozulemedi'] = 'Harfler sütun etiketleriyle eşleşmedi; testler yazılmadı'
                _add_issue(table, {'tur': 'karsilastirma_cozulemedi', 'satir_no': row['satir_no'], 'parca': value,
                                   'aciklama': 'İkili karşılaştırmadaki harfler sütun etiketleriyle eşleşmedi'})
            record.update(p=(match.group(3) if match.group(3) not in ('', '=') else '') + match.group(4),
                          etki_turu=ETKI_AUC if re.search(r'\bauc\b', (row['degisken'] or '').casefold()) else None)
        elif kind == 'p':
            record['p'] = value
            if link is not None:
                record.update(sonuc_olcutu_tr=link['degisken'], etki_turu=ETKI_AUC)
                note.update(p_baglandigi_satir=link['satir_no'], p_baglandigi_olcu=link['degisken'],
                            p_baglama_nedeni=why)
            else:
                note['p_baglanamadi'] = why
        else:
            deger, se, se_form = _diag_value(row, value)
            record.update(deger=deger, etki_turu=DIAG_EFFECT.get(kind) or (
                ETKI_N_PCT if kind in DIAG_PROPORTION and '%' in value else ETKI_OTHER))
            if se is not None:
                note.update(se=se, se_bicimi=se_form)
        record['taslak_notu'] = json.dumps(note, ensure_ascii=False)
        out.append(record)
    return out


def record_unit(record, analyses):
    """Bir tablo kaydının satır içindeki birimi: analiz tablosunda taslak_notu.analiz, diğerlerinde taslak_notu.sutun
    (grup / Total / p sütunu etkisi / tanısal test sütunu)."""
    try:
        note = json.loads(record.get('taslak_notu') or '{}')
    except (TypeError, ValueError):
        note = {}
    note = note if isinstance(note, dict) else {}
    return note.get('analiz') if analyses else note.get('sutun')


def expected_units(table, row):
    """(analiz_tablosu_mu, {birim: {alan: beklenen değer}}): PDF satırından bu modülün üreteceği kayıtlar
    (sql_preview ile, boş değerli birim yok) ve data_quality.check_table_groups'un karşılaştıracağı alanlar.
    Analiz: deger / p / ci95. Grup ve Total: deger / p / hasta_grubu / comparator. p sütunu etkisi: deger / p / ci95.
    Tanısal test x ölçü: deger / p / mudahale_en / comparator / sonuc_olcutu_tr."""
    analyses = table.get('tablo_turu') != TABLO_TANISAL and bool(row_analyses(table, row))
    out = {}
    for record in sql_preview({'kanit_cumle': row['ham']}, table, row):
        note = json.loads(record['taslak_notu'])
        if analyses:
            fields = ('deger', 'p', 'ci95')
        elif note.get('tani_olcusu'):
            fields = ('deger', 'p', 'mudahale_en', 'comparator', 'sonuc_olcutu_tr')
        elif note.get('etki_kaynagi'):
            fields = ('deger', 'p', 'ci95')
        else:
            fields = ('deger', 'p', 'hasta_grubu', 'comparator')
        out[record_unit(record, analyses)] = {name: record.get(name) for name in fields}
    return analyses, out


def transfer_record(record, preview_row, row=None):
    """import_records'a verilecek kayıt: pilot kaydının temel alanları + önizleme satırının dolu
    yapılandırılmış alanları. review_status konmaz (onay sonra insan tarafından verilir).

    row (eşleşen tablo satırı) verilirse klinik_bilgi_en temiz satır adıdır (row['ad']); ad kanıtta
    birebir geçmiyorsa kayıttaki değer kalır (enrich bunu 'temiz_ad_kanitta_yok' sorunu olarak yazar)."""
    item = {key: record[key] for key in TRANSFER_BASE_FIELDS if record.get(key) is not None}
    if row is not None and row.get('ad') and row['ad'] in (record.get('kanit_cumle') or ''):
        item['klinik_bilgi_en'] = row['ad']
    item.update({key: preview_row[key] for key in STRUCTURED_FIELDS if preview_row.get(key) is not None})
    return item


# ---------------------------------------------------------------------------
# Akış ve rapor
# ---------------------------------------------------------------------------

def enrich(records, pages, document_id=None):
    tables = parse_tables(pages)
    enriched, preview, transfer, by_row, skipped = [], [], [], {}, []
    for record in records:
        if record.get('kanit_turu') != 'tablo':
            continue
        status, table, rows_or_candidates = match_rows(record, tables)
        item = dict(record, eslesme=status)
        if status == 'eslesti':
            matched = rows_or_candidates
            item['tablo'] = table_info(table, matched[0])
            if len(matched) > 1:  # <br> ile alt satırlara bölünmüş PDF satırı
                item['alt_satirlar'] = [table_info(table, r) for r in matched[1:]]
            for row in matched:
                by_row.setdefault((table['tablo_no'], row['satir_no']), []).append(record.get('v2_sira'))
                rows = sql_preview(record, table, row, document_id, skipped)
                preview.extend(rows)
                transfer.extend(transfer_record(record, p, row) for p in rows)
                if not row['ad'] or row['ad'] not in (record.get('kanit_cumle') or ''):
                    table['sorunlar'].append({'tur': 'temiz_ad_kanitta_yok', 'satir_no': row['satir_no'], 'ad': row['ad'],
                                              'aciklama': 'Temiz satır adı kanit_cumle içinde birebir yok; '
                                                          'klinik_bilgi_en kayıttaki değerle bırakıldı'})
        elif status == 'belirsiz':
            item['adaylar'] = rows_or_candidates
        elif status == 'elle_inceleme':
            item['tablo_no'] = table['tablo_no']
        enriched.append(item)
    missing = [{'tablo_no': t['tablo_no'], 'sayfa': t['sayfa'], 'satir_no': r['satir_no'], 'alt_bolum': r['alt_bolum'],
                'degisken': r['degisken'], 'ham': r['ham']}
               for t in tables for r in t['satirlar'] if (t['tablo_no'], r['satir_no']) not in by_row]
    duplicates = {f'Table {k[0]} satır {k[1]}': v for k, v in by_row.items() if len(v) > 1}
    return {'tablolar': tables, 'kayitlar': enriched, 'sql_onizleme': preview, 'aktarim': transfer,
            'pilot_kaydinda_yok': missing, 'ayni_satira_birden_fazla_kayit': duplicates,
            'atlanan_kayitlar': skipped}


def mudahale_pending(preview):
    """mudahale_turu boş önizleme satırları, tablo satırı başına gruplanmış (bilgi amaçlı; aktarım için sorun değil)."""
    groups = {}
    for row in preview:
        if row['mudahale_turu']:
            continue
        info = json.loads(row['taslak_notu'])
        key = (info['tablo_no'], info['satir_no'])
        item = groups.setdefault(key, {'tablo_no': info['tablo_no'], 'satir_no': info['satir_no'],
                                       'alt_bolum': info['alt_bolum'], 'degisken': info['degisken'],
                                       'bilgi_turu': info['bilgi_turu'], 'pilot_v2_sira': info['pilot_v2_sira'],
                                       'sql_satiri': 0})
        item['sql_satiri'] += 1
    return [groups[k] for k in sorted(groups)]


def summarize(result, source):
    records = result['kayitlar']
    preview = result['sql_onizleme']
    count = lambda status: sum(r['eslesme'] == status for r in records)
    return {
        'girdi': source,
        'tablo_sayisi': len(result['tablolar']),
        'tablo_veri_satiri': sum(len(t['satirlar']) for t in result['tablolar']),
        'tablo_kaydi': len(records),
        'eslesen': count('eslesti'), 'eslesmeyen': count('eslesmedi'), 'belirsiz': count('belirsiz'),
        'elle_inceleme': count('elle_inceleme'),
        'elle_inceleme_tablolari': [t['tablo_no'] for t in result['tablolar'] if 'elle_inceleme_satirlari' in t],
        'atlanan_satirlar': [dict(s, tablo_no=t['tablo_no']) for t in result['tablolar']
                             for s in t.get('atlanan_satirlar', [])],
        'atlanan_kayitlar': list(result.get('atlanan_kayitlar', [])),
        'eslesmeyen_kayitlar': [r.get('v2_sira') for r in records if r['eslesme'] != 'eslesti'],
        'pilot_kaydinda_yok': len(result['pilot_kaydinda_yok']),
        'ayni_satira_birden_fazla_kayit': len(result['ayni_satira_birden_fazla_kayit']),
        'sql_onizleme_satiri': len(preview),
        'aktarim_kaydi': len(result['aktarim']),
        'sql_mudahale_turu_bos': sum(not r['mudahale_turu'] for r in preview),
        'sql_mudahale_turu_bos_satirlar': mudahale_pending(preview),
        'sql_outcome_type_bos': sum(not r['outcome_type'] for r in preview),
        'sorunlu_basliklar': [dict(s, tablo_no=t['tablo_no'], sayfa=t['sayfa'])
                              for t in result['tablolar'] for s in t['sorunlar']],
    }


def report_markdown(summary, result):
    s = summary
    lines = [
        '# Pilot tablo eşleme raporu', '',
        f'Sürüm `{VERSION}` · deterministik (LLM yok) · veritabanına yazılmadı · girdi `{s["girdi"]}`', '',
        '| Ölçü | Sayı |', '|---|---|',
        f'| Tablo | {s["tablo_sayisi"]} |',
        f'| Tablo veri satırı | {s["tablo_veri_satiri"]} |',
        f'| kanit_turu=tablo kaydı | {s["tablo_kaydi"]} |',
        f'| Eşleşen | {s["eslesen"]} |',
        f'| Eşleşmeyen | {s["eslesmeyen"]} |',
        f'| Belirsiz | {s["belirsiz"]} |',
        f'| Elle inceleme gereken tablodaki kayıt (kayıt üretilmedi) | {s["elle_inceleme"]} |',
        f'| Atlanan tablo satırı (dipnot / biçim satırı / sütun etiketi / boş ad) | {len(s["atlanan_satirlar"])} |',
        f'| Üretilmeyen kayıt (boş değer) | {len(s["atlanan_kayitlar"])} |',
        f'| Pilot kaydında olmayan tablo satırı | {s["pilot_kaydinda_yok"]} |',
        f'| Aynı satıra birden fazla kayıt | {s["ayni_satira_birden_fazla_kayit"]} |',
        f'| SQL önizleme satırı | {s["sql_onizleme_satiri"]} |',
        f'| import_records aktarım kaydı (`pilot_tablo_aktarim.json`, review_status yok) | {s["aktarim_kaydi"]} |',
        f'| …mudahale_turu dolu (yalnız ilaç: "arka plan tedavisi") | {s["sql_onizleme_satiri"] - s["sql_mudahale_turu_bos"]} |',
        f'| …mudahale_turu boş (aktarım yolu zorunlu tutmuyor; sorun değil) | {s["sql_mudahale_turu_bos"]} |',
        f'| …outcome_type boş | {s["sql_outcome_type_bos"]} |', '',
    ]
    for t in result['tablolar']:
        lines.append(f'- Table {t["tablo_no"]} (s.{t["sayfa"]}): {len(t["satirlar"])} satır; sütunlar: '
                     + ' · '.join(f'`{c["anahtar"]}`' for c in t['sutunlar'])
                     + (f'; alt bölümler: {", ".join(t["alt_bolumler"])}' if t['alt_bolumler'] else ''))
    lines += ['', '## Eşleşmeyen / belirsiz kayıtlar', '']
    lines += [f'- v2_sira {r.get("v2_sira")} ({r["eslesme"]}): `{r.get("kanit_cumle")}`'
              for r in result['kayitlar'] if r['eslesme'] != 'eslesti'] or ['- Yok']
    if s['elle_inceleme_tablolari']:
        lines += ['', '## Elle inceleme gereken tablolar (otomatik ayrıştırılmadı, kayıt üretilmedi)', '']
        lines += [f'- Table {t["tablo_no"]} (s.{t["sayfa"]}): '
                  + '; '.join(next(i['parca'] for i in t['sorunlar'] if i['tur'] == 'elle_inceleme_gerekli'))
                  for t in result['tablolar'] if 'elle_inceleme_satirlari' in t]
    if s['atlanan_satirlar'] or s['atlanan_kayitlar']:
        lines += ['', '## Atlanan satırlar ve üretilmeyen kayıtlar', '']
        lines += [f'- Table {a["tablo_no"]} `{a["tur"]}`: `{a.get("ham", "")}`' for a in s['atlanan_satirlar']]
        lines += [f'- Table {a["tablo_no"]} satır {a["satir_no"]} ({a["degisken"]}) `{a["tur"]}`: sütun `{a["sutun"]}`'
                  for a in s['atlanan_kayitlar']]
    lines += ['', '## Pilot kaydında olmayan tablo satırları', '']
    lines += [f'- Table {m["tablo_no"]} satır {m["satir_no"]} ({m["alt_bolum"] or "bölüm yok"}): {m["degisken"]} — `{m["ham"]}`'
              for m in result['pilot_kaydinda_yok']] or ['- Yok']
    if result['ayni_satira_birden_fazla_kayit']:
        lines += ['', '## Aynı satıra birden fazla kayıt', '']
        lines += [f'- {k}: v2_sira {v}' for k, v in result['ayni_satira_birden_fazla_kayit'].items()]
    pending = s['sql_mudahale_turu_bos_satirlar']
    lines += ['', '## mudahale_turu boş satırlar (bilgi amaçlı)', '',
              f'Aktarım yolu `src.pilot_clinical.import_records` `mudahale_turu`\'yu zorunlu tutmuyor; eski '
              f'`validate_row` yolu bu pilot için kullanılmıyor. Yalnız ilaç satırları "arka plan tedavisi"; aşağıdaki '
              f'{len(pending)} tablo satırı ({s["sql_mudahale_turu_bos"]} SQL satırı) boş kalır ve bu sorun değildir. '
              f'Grup ortalaması / oran ölçümleri tanı testi değerlendirmesi olmadığı için "tanı testi" atanmadı.', '',
              '| Tablo | Satır | Alt bölüm | Değişken | bilgi_turu (pilot) | v2_sira | SQL satırı |',
              '|---|---|---|---|---|---|---|']
    lines += [f'| {p["tablo_no"]} | {p["satir_no"]} | {p["alt_bolum"] or "—"} | {p["degisken"]} | {p["bilgi_turu"]} | '
              f'{p["pilot_v2_sira"]} | {p["sql_satiri"]} |' for p in pending]
    lines += ['', '## Sorunlu başlıklar (düzeltme yapılmadı)', '']
    for issue in s['sorunlu_basliklar']:
        part = issue.get('parca')
        lines.append(f'- Table {issue["tablo_no"]} (s.{issue["sayfa"]}) `{issue["tur"]}`'
                     + (f' — parça: `{json.dumps(part, ensure_ascii=False)}`' if part else '') + f'. {issue["aciklama"]}')
    lines += ['', '## SQL önizleme eşlemesi (klinik_bulgular, mevcut sütunlar)', '',
              '- Grup tablosu (Table 1): grup hücresi başına 1 satır. `hasta_grubu`=grup, `comparator`=diğer grup, `deger`=hücre, '
              '`p`=grup karşılaştırma p\'si, `unit` değişken adından. `etki_turu`: n (%) -> "oran / yuzde", diğerleri -> "diger"; '
              'biçim `taslak_notu.deger_bicimi`. `outcome_type`="çalışma popülasyonu".',
              '- İlaç satırları (Medications): `mudahale_en`=ilaç, `mudahale_turu`="arka plan tedavisi", '
              '`outcome_type`="uygulama oranı", `sonuc_olcutu_tr`="kullanım oranı", `doz`=NULL (kullanım oranı, doz değil).',
              '- İlaç dışı tüm satırlar (demografik, tanı, laboratuvar, eko, Table 2): `mudahale_turu` boş; '
              'aktarım yolu zorunlu tutmuyor.',
              '- Etki tablosu (Table 2): analiz başına 1 satır; `etki_turu`="OR", `deger`=OR, `ci95`, `p` ayrı; '
              'analiz türü `taslak_notu.analiz`; `hasta_grubu` NULL. `outcome_type` boş: retrospektif prediktör analizi '
              'için "prognostik değer" uygunluğundan emin olunamadı.',
              '- `hasta_sayisi` NULL ve `comparator` n\'siz (ekip kararı): n değerleri `taslak_notu.grup_n` / `comparator_n`.',
              '- Total sütunu ("Total", "Overall", "All patients" …): kayıt kalır, `taslak_notu.sutun_rolu`="toplam"; '
              'comparator / p yok, comparator yalnız gerçek karşılaştırma grupları arasında.',
              '- p sütunundaki etki ölçüsü ("7.8 (2.4-25.1) or"): satırın ayrı etki kaydı (`etki_turu`="OR", `deger`, '
              '`ci95`; `taslak_notu.etki_kaynagi`="p_sutunu"); o satırın grup kayıtlarında `p` boş.',
              '- Tanısal performans tablosu: kayıt = test x ölçü; `mudahale_en`=test, `mudahale_turu`="tanı testi", '
              '`sonuc_olcutu_tr`=ölçü, `outcome_type`="tanısal performans"; P satırı AUC\'ye bağlanır '
              '(`taslak_notu.p_baglandigi_satir`), "P (A-B)=…" satırı karşılaştırma kaydı.',
              '- Aktarım (`pilot_tablo_aktarim.json`): önizleme satırı başına bir kayıt; pilot kaydının `bilgi_turu`, `baglam`, '
              '`kanit_cumle`, `sayfa`, `kanit_turu` alanları aynen, `klinik_bilgi_en` temiz satır adı (ör. "CAD", '
              '"Age (years)"; ham tablo satırı değil) + dolu yapılandırılmış alanlar '
              '(`src.pilot_clinical.STRUCTURED_FIELDS`). `review_status` yok: manuel doğrulamadan sonra insan verir; '
              '`import_records` yalnız `verified` kayıt yazar.']
    return '\n'.join(lines) + '\n'


def run(pdf, findings, doc=None, pages=None):
    """Findings zorunlu; findings ile PDF aynı belge olmalı (check_findings_document)."""
    if doc is None or pages is None:
        from src.pilot_clinical import pdf_pages
        doc, pages = pdf_pages(pdf)
    records, source = load_records(findings)
    check = check_findings_document(records, declared_document_ids(findings), doc, pages)
    result = enrich(records, pages, doc.get('document_id'))
    summary = summarize(result, source)
    summary['belge_kontrolu'] = check
    return dict({'surum': VERSION, 'pdf': doc.get('filename'), 'document_id': doc.get('document_id'),
                 'rapor': summary}, **result)


def output_paths(output):
    output = Path(output)
    report = output.with_name(output.stem.replace('_zengin', '') + '_raporu.md')
    transfer = output.with_name(output.stem.replace('_zengin', '') + '_aktarim.json')
    return output, report, transfer


def default_output(doc):
    """Bu belgenin varsayılan çıktısı. Var olan pilot çıktısı (pilot_tablo_zengin.json) yalnız aynı belgeye aittse
    kullanılır; başka belge kendi adını alır (pilot_tablo_<pdf adı>_zengin.json), pilot çıktısının üzerine yazılmaz."""
    if DEFAULT_OUTPUT.exists() and _output_document(DEFAULT_OUTPUT) == doc.get('document_id'):
        return DEFAULT_OUTPUT
    stem = re.sub(r'[^A-Za-z0-9]+', '_', Path(doc.get('filename') or 'belge').stem).strip('_')[:60] or 'belge'
    return PILOT_DIR / f'pilot_tablo_{stem}_zengin.json'


def _output_document(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8')).get('document_id')
    except (OSError, ValueError, AttributeError):
        return None


def check_outputs(paths, doc, overwrite):
    """Var olan çıktı: --overwrite yoksa hata; --overwrite varsa yalnız AYNI belgenin çıktısının üzerine yazılır
    (zengin JSON'daki document_id). Başka belgenin ya da belgesi doğrulanamayan çıktının üzerine yazılmaz."""
    output = paths[0]
    existing = [p for p in paths if p.exists()]
    if not existing:
        return
    if not overwrite:
        raise FileExistsError(f'Mevcut çıktı var (--overwrite ile yazılır): {" / ".join(map(str, existing))}')
    owner = _output_document(output) if output.exists() else None
    if owner != doc.get('document_id'):
        raise FileExistsError(f'{output} başka bir belgenin çıktısı (document_id {owner}) ya da belgesi doğrulanamadı; '
                              f'--overwrite ile bile üzerine yazılmaz. Başka bir --output verin.')


def main():
    parser = argparse.ArgumentParser(description='Pilot tablo kayıtlarını başlıklarla eşle (deterministik, SQL yazmaz)')
    parser.add_argument('--pdf', required=True)
    parser.add_argument('--findings', required=True,
                        help='Bu PDF\'in bulgu kayıtları (pilot_auto_extract çıktısı, JSON/CSV); varsayılan yok')
    parser.add_argument('--output', help='Varsayılan: pilot_tablo_<pdf adı>_zengin.json (pilot belgesinde '
                                         'mevcut pilot_tablo_zengin.json)')
    parser.add_argument('--overwrite', action='store_true', help='Aynı belgenin mevcut çıktılarının üzerine yaz')
    args = parser.parse_args()
    from src.pilot_clinical import pdf_pages
    doc, pages = pdf_pages(args.pdf)
    paths = output_paths(args.output or default_output(doc))
    check_outputs(paths, doc, args.overwrite)
    try:
        result = run(args.pdf, args.findings, doc, pages)
    except FindingsError as exc:
        raise SystemExit(f'Hata: {exc}')
    output, report, transfer = paths
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report.write_text(report_markdown(result['rapor'], result), encoding='utf-8')
    transfer.write_text(json.dumps(result['aktarim'], ensure_ascii=False, indent=2), encoding='utf-8')
    s = result['rapor']
    print(f"Girdi: {s['girdi']} (belge kontrolü: {s['belge_kontrolu']}) | Tablo: {s['tablo_sayisi']} "
          f"({s['tablo_veri_satiri']} satır) | Tablo kaydı: {s['tablo_kaydi']} | Eşleşen: {s['eslesen']} | "
          f"Eşleşmeyen: {s['eslesmeyen']} | Belirsiz: {s['belirsiz']} | Elle inceleme: {s['elle_inceleme']} | "
          f"Pilot kaydında yok: {s['pilot_kaydinda_yok']} | SQL önizleme: {s['sql_onizleme_satiri']} satır | "
          f"Aktarım kaydı: {s['aktarim_kaydi']}")
    print(f'Çıktı: {output} | Rapor: {report} | Aktarım: {transfer} | SQL değişmedi')


if __name__ == '__main__':
    main()
