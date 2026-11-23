"""RAG destekli PDF klinik bilgi çıkarımı (prototip) -> yalnızca JSON dosyaları; veritabanına HİÇ bağlanmaz.

Akış (mevcut modüller yeniden kullanılır):
  1. pdf_pages (pdf_loader.load_pdf)            : PDF -> fiziksel sayfa metinleri
  2. chunking.chunk_document                     : sayfa bazlı, tablo farkındalıklı chunk'lar (bellekte)
  3. embeddings.embed_documents                  : chunk embedding'leri (bellekte; chunk_embeddings'e yazılmaz)
  4. retrieval.hybrid_search                     : 8 kategori sorgusu ile (semantic + BM25 + RRF) ilgili sayfaları
                                                   ve sayfa başına hedef kategorileri seçer
  4b. Kapsama tamamlama (fill_coverage)          : retrieval'ın seçmediği, kaynakça olmayan (pdf_processing.
                                                   remove_document_references) ve metni boş olmayan sayfalar
                                                   ipucusuz tam tarama görevi olarak sona eklenir
  4c. Sınır görevleri (yalnız --sinir)            : cümle ortasında biten ardışık kaynakça dışı sayfa çiftleri;
                                                   boundary_excerpt ile ayrı sol/sağ pencere, LLM 2 parça döndürür,
                                                   sayfa/sayfa_bitis/kanit_cumle/birlestirme kodda türetilir
  4d. Tablo görevleri (yalnız --tablo)            : tablo satırı içeren görev sayfaları; ayrı istemle hücre başına
                                                   kayıt (sütun başlığı hasta_grubu ya da sonuc_olcutu_tr); aynı
                                                   satırın karşılanan eski tablo kayıtlarının yerine geçer
  5. pilot_auto_extract.request_with_retry/SYSTEM: seçilen her sayfa parçası için LLM çıkarımı (JSON)
  6. Temel kaynak kontrolü                       : pilot_clinical.validate_records (kanıt PDF sayfasında birebir,
                                                   klinik bilgi kanıtın içinde birebir) + yapılandırılmış alanlar
                                                   (deger / p kanıtta, birim / hasta grubu sayfada) birebir
  7. 8 sunum kategorisine yönlendirme             : sql/sunum_hoca/*.sql view kurallarının Python karşılığı

Kanıt, chunk metnine göre DEĞİL ham sayfa metnine göre doğrulanır (chunking.clean_text boşlukları değiştirir).
Sayfa sınırını geçen kanıtlar yalnız --sinir ile (iki parçalı kanit_parcalari) çıkarılır.

Çalıştırma:
    python -m src.rag_klinik_cikarim --pdf data/pdfs/<dosya>.pdf --plan        # API yok: görev planı
    python -m src.rag_klinik_cikarim --pdf data/pdfs/<dosya>.pdf --max-calls 30
Çıktı: data/rag_cikarim/<pdf_adı>/<zaman>/ (kategori JSON'ları, kayitlar.json, reddedilenler.json, calistirma.json)
LLM yanıtları data/rag_cikarim/<pdf_adı>/llm_onbellek.jsonl'de saklanır; aynı istek tekrar ücretlendirilmez.
"""
import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from src.pilot_auto_extract import (SYSTEM as BASE_SYSTEM, SPLIT_LIMIT, BOUNDARY_WINDOW, boundary_excerpt,
                                    request_with_retry, split_page)
from src.pilot_clinical import (TYPES, CONTEXTS, INPUT_FIELDS, STRUCTURED_FIELDS, check_evidence_parts, pdf_pages,
                                validate_records)

OUTPUT_ROOT = Path('data/rag_cikarim')
VERSION = 'rag-klinik-cikarim-v1'
CATEGORIES = ('diseases', 'symptoms', 'diagnostics', 'treatments', 'measurements', 'measurements_by_group',
              'clinical_outcomes', 'general_clinical_findings')

# Kategori başına İngilizce retrieval sorguları (makaleler İngilizce; minilm politikası "rewrite").
CATEGORY_QUERIES = {
    'diseases': ['disease diagnosis condition comorbidity patients diagnosed with',
                 'history of hypertension diabetes coronary artery disease heart failure'],
    'symptoms': ['symptoms complaints chest pain dyspnea palpitations syncope fatigue'],
    'diagnostics': ['diagnostic method electrocardiography echocardiography laboratory measurement assay',
                    'diagnostic criteria definition threshold cut-off was defined as'],
    'treatments': ['treatment therapy medication drug dose administered procedure intervention'],
    'measurements': ['table baseline characteristics of groups mean standard deviation median n (%) p value'],
    'measurements_by_group': ['comparison between groups table values per group'],
    'clinical_outcomes': ['outcome mortality survival association odds ratio hazard ratio confidence interval',
                          'sensitivity specificity area under the curve predictive value results'],
    'general_clinical_findings': ['study population patients were included age sex cohort characteristics'],
}
TABLE_LINE = re.compile(r'^\|.*\|\s*$', re.M)
TOTAL_GROUP = re.compile(r'^\s*(?:total|overall|all(?:\s+patients)?|toplam)\b', re.I)

RAG_ADDENDUM = """

RETRIEVAL HINT: payload.hedef_kategoriler lists presentation categories that retrieval found relevant on this
page (diseases, symptoms, diagnostics, treatments, measurements, measurements_by_group, clinical_outcomes,
general_clinical_findings). Use it only to focus attention; still extract every explicit clinical fact on the
page excerpt, and never invent a fact to fill a category.

OPTIONAL STRUCTURED FIELDS (add only when explicitly present; never compute, convert or round):
- deger: the numeric result text copied exactly from kanit_cumle (e.g. "44 (38.6)", "0.83", "<0.001" is p, not deger).
- unit: the unit copied exactly from kanit_cumle or from the table header on this page (e.g. "ng/L", "%").
- hasta_grubu: the patient group label exactly as written on this page (for a table cell: its column header,
  e.g. "ACS Present (n=94)"; for text: the cohort phrase in kanit_cumle).
- p: the p-value text copied exactly from kanit_cumle.
TABLE ROWS: kanit_turu=tablo and kanit_cumle = the exact full table row line as it appears in the excerpt.
For a row reporting values per group, emit ONE record per group column cell (same kanit_cumle, klinik_bilgi_en =
the row variable name, hasta_grubu = that column header, deger = that cell value, p = the row p-value if present).
Emit group cells in the column order of the table. Keep a Total/Overall column as its own record with its header.
"""
SYSTEM = BASE_SYSTEM + RAG_ADDENDUM

# Sınır görevleri (--sinir) için ayrı istem: SYSTEM aynen kalır (sayfa görevlerinin önbellek anahtarları değişmez).
BOUNDARY_ADDENDUM = """

CROSS-PAGE BOUNDARY MODE (overrides the per-record field list above):
payload.left_excerpt is the exact END of physical page payload.start_page; payload.right_excerpt is the exact START
of page payload.end_page (= start_page + 1). They are two separate texts; nothing lies between them.
Extract ONLY clinical facts whose source sentence starts in left_excerpt and continues in right_excerpt. Ignore facts
whose whole sentence lies on one page (they are extracted in the ordinary page pass).
For each record return:
- kanit_parcalari: [{"sayfa": start_page, "metin": <text copied exactly from left_excerpt, running to its very end>},
                    {"sayfa": end_page, "metin": <text copied exactly from right_excerpt, starting at its very beginning>}]
- bilgi_turu, baglam, klinik_bilgi_en (an exact substring of one part, or of the two parts joined by one space)
- optional deger / unit / hasta_grubu / p copied exactly from the parts.
Do not insert spaces, repair hyphenation, or merge the parts into one quote. Do not return sayfa, sayfa_bitis,
kanit_cumle or kanit_turu (they are derived). If there is no such fact, return {"findings": []}.
"""
BOUNDARY_SYSTEM = SYSTEM + BOUNDARY_ADDENDUM

# Tablo görevleri (--tablo) için ayrı istem: SYSTEM/RAG_ADDENDUM/BOUNDARY_SYSTEM aynen kalır.
TABLE_ADDENDUM = """

TABLE MODE (overrides the general instructions above): extract ONLY data cells of the markdown tables (lines
starting with "|") in page_excerpt. Skip header rows, separator rows, captions, footnotes and running text.
For every non-empty data cell emit ONE record:
- kanit_turu: "tablo"; kanit_cumle: the exact full row line as it appears in page_excerpt
- klinik_bilgi_en: the row variable exactly as written in the row (usually its first cell)
- deger: the cell value copied exactly (e.g. "Decreased", "2.42 (2.17–2.71)")
- if the cell's column header names a PATIENT GROUP: hasta_grubu = that header exactly as written on the page
- if the cell's column header names an OUTCOME or MEASURE (e.g. "AF symptoms", "AF burden",
  "Relative risk (95% CI)"): sonuc_olcutu_tr = that header exactly as written on the page (do not translate)
- p: the row's p-value cell if present; unit only if written in the row or header
Cells in different columns of the same row are different records even if their values are identical; always
give the column header so they can be told apart. Never compute, convert, merge or invent values.
"""
TABLE_SYSTEM = SYSTEM + TABLE_ADDENDUM
TABLE_DATA_ROW = re.compile(r'^\|(?!\s*:?-{3,}).*\|\s*$', re.M)


def _norm(text):
    """Karşılaştırma için hafif normalizasyon (yalnız kaynak kontrolündeki başlık/grup eşleşmesi ve kıyas)."""
    text = re.sub(r'<br\s*/?>|<sup>|</sup>|\*\*', ' ', str(text or ''), flags=re.I)
    return re.sub(r'\s+', ' ', text).strip().casefold()


# ---------------------------------------------------------------------------
# 2-4. Chunk + embedding + retrieval (bellekte)
# ---------------------------------------------------------------------------

def build_chunks(doc, embedder=None):
    """chunk_document + embedding; embedder(texts) -> vektör listesi (test için enjekte edilebilir)."""
    from src.chunking import chunk_document
    chunks = chunk_document(doc)
    if embedder is None:
        from src.embeddings import embed_documents
        embedder = embed_documents
    vectors = embedder([c['text'] for c in chunks]) if chunks else []
    for chunk, vector in zip(chunks, vectors):
        chunk['embedding'] = vector
    return chunks


def plan_pages(chunks, *, top_k=6, search=None):
    """Her kategori sorgusu için hybrid_search; sayfa -> {kategori: en iyi sıra}. Tablo chunk'ı olan sayfa her zaman
    measurements/measurements_by_group hedefi alır. search(query, chunks, top_k) test için enjekte edilebilir."""
    if search is None:
        from src.retrieval import hybrid_search

        def search(query, items, k):
            return hybrid_search(query, items, top_k=k, candidate_k=max(20, 2 * k))
    targets = defaultdict(dict)
    hits = []
    for category, queries in CATEGORY_QUERIES.items():
        for query in queries:
            for rank, hit in enumerate(search(query, chunks, top_k), 1):
                page = hit['page_number']
                targets[page][category] = min(rank, targets[page].get(category, rank))
                hits.append({'kategori': category, 'sorgu': query, 'sira': rank, 'sayfa': page,
                             'chunk_index': hit.get('chunk_index')})
    for chunk in chunks:
        if len(TABLE_LINE.findall(chunk['text'])) >= 2:
            for category in ('measurements', 'measurements_by_group'):
                targets[chunk['page_number']].setdefault(category, 0)
    return {page: sorted(cats, key=CATEGORIES.index) for page, cats in sorted(targets.items())}, hits


# Kapsama tamamlama: kaynakça dışı kalan metinde en az bu kadar alfabetik kelime (>=3 harf). Karakter eşiği (ör. 400)
# yerine kelime eşiği: kaynakçası çıkarılmış karma bir sayfada kalan tek klinik paragraf 400 karakterin altında
# olabilir. 8 kelime ~ bir kısa cümle; yalnız boş / neredeyse boş sayfalar dışarıda kalır (yanlış pozitif = 1 çağrı).
MIN_COVERAGE_WORDS = 8
COVERAGE_WORD = re.compile(r'[^\W\d_]{3,}')


def fill_coverage(doc, page_targets, min_words=MIN_COVERAGE_WORDS):
    """Retrieval'ın seçmediği sayfalardan kapsama tamamlamaya girecekleri belirler. (eklenen, kapsama_disi)

    Karar yalnız burada remove_document_references ile verilir; LLM'e giden metin ve kanıt doğrulaması ham sayfadır.
    page_targets DEĞİŞTİRİLMEZ (mevcut görevlerin kategori ipuçları ve önbellek anahtarları aynı kalır)."""
    from src.pdf_processing import remove_document_references
    kept = {p['page_number']: p['text'] for p in remove_document_references(doc)['pages']}
    added, excluded = [], {}
    for page in sorted(p['page_number'] for p in doc['pages']):
        if page in page_targets:
            continue
        if page not in kept:
            excluded[page] = 'kaynakca'
            continue
        words = len(COVERAGE_WORD.findall(kept[page]))
        if words < min_words:
            excluded[page] = f'icerik_yok ({words} kelime)'
            continue
        added.append(page)
    return added, excluded


# Sayfa N'nin son dolu satırı cümle/tablo sonu işaretiyle bitiyorsa sınırı geçen cümle yoktur (maliyet filtresi).
SENTENCE_END = re.compile(r'[.!?:)\]|]\s*(?:\*+|_+)?$')


def ends_mid_sentence(text):
    lines = [line.strip() for line in text.rstrip().splitlines() if line.strip()]
    return bool(lines) and not SENTENCE_END.search(lines[-1])


def build_boundary_tasks(doc, pages, task_pages):
    """Ardışık (N, N+1) görev sayfaları için sınır görevleri: iki sayfa da kaynakça değil (remove_document_references),
    sayfa N cümle ortasında bitiyor. Pencereler pilot_auto_extract.boundary_excerpt ile kesilir, AYRI alanlar olarak
    verilir (sayfa metinleri arasında boşluk yok; tek birleşik alıntı birebir doğrulanamazdı)."""
    from src.pdf_processing import remove_document_references
    kept = {p['page_number'] for p in remove_document_references(doc)['pages']}
    tasks = []
    for left in sorted(task_pages):
        right = left + 1
        if right not in task_pages or right not in pages or not {left, right} <= kept:
            continue
        if not ends_mid_sentence(pages[left]) or not pages[right].strip():
            continue
        excerpt, offset = boundary_excerpt(pages[left], pages[right], window=BOUNDARY_WINDOW)
        tasks.append({'tur': 'sinir', 'sayfa': left, 'sayfa_bitis': right,
                      'left_excerpt': excerpt[:offset], 'right_excerpt': excerpt[offset:]})
    return tasks


def build_table_tasks(pages, task_pages):
    """Yalnız tablo içeren görev sayfaları (en az 2 veri satırı); uzun sayfa sayfa görevleriyle aynı biçimde bölünür
    ve yalnız tablo satırı içeren parçalar görev olur."""
    tasks = []
    for page in sorted(task_pages):
        if page not in pages or len(TABLE_DATA_ROW.findall(pages[page])) < 2:
            continue
        for excerpt in split_page(pages[page], limit=SPLIT_LIMIT):
            if len(TABLE_DATA_ROW.findall(excerpt)) >= 2:
                tasks.append({'tur': 'tablo', 'sayfa': page, 'page_excerpt': excerpt})
    return tasks


def task_request(task):
    """(istem, payload). Sayfa görevi için önceki sürümle birebir aynı -> önbellek anahtarı değişmez."""
    if task.get('tur') == 'sinir':
        return BOUNDARY_SYSTEM, {'start_page': task['sayfa'], 'end_page': task['sayfa_bitis'],
                                 'left_excerpt': task['left_excerpt'], 'right_excerpt': task['right_excerpt']}
    if task.get('tur') == 'tablo':
        return TABLE_SYSTEM, {'gorev': 'tablo', 'sayfa': task['sayfa'], 'page_excerpt': task['page_excerpt']}
    return SYSTEM, {k: task[k] for k in ('sayfa', 'hedef_kategoriler', 'page_excerpt')}


def build_tasks(pages, page_targets):
    """Yalnız retrieval'ın seçtiği sayfalar; uzun sayfa pilot_auto_extract.split_page ile (örtüşmeli) bölünür."""
    tasks = []
    for page in sorted(page_targets):
        for excerpt in split_page(pages[page], limit=SPLIT_LIMIT):
            tasks.append({'sayfa': page, 'hedef_kategoriler': page_targets[page], 'page_excerpt': excerpt})
    return tasks


# ---------------------------------------------------------------------------
# 5. LLM (önbellekli)
# ---------------------------------------------------------------------------

def llm_client():
    """src.rag.get_llm_client ile aynı .env ayarları (src.rag'ın ağır import zinciri olmadan)."""
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv()
    base_url = os.getenv('LLM_BASE_URL')
    if not base_url:
        raise ValueError('LLM_BASE_URL .env dosyasında tanımlanmalı.')
    return OpenAI(api_key=os.getenv('LLM_API_KEY') or 'not-required', base_url=base_url, timeout=120.0, max_retries=0)


def default_model():
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv('LLM_MODEL', 'Qwen/Qwen3-8B')


class Cache:
    """JSONL önbellek: anahtar = sha256(model, system, payload)."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {}
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.data[row['anahtar']] = row['findings']

    @staticmethod
    def key(model, system, payload):
        raw = json.dumps([model, system, payload], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def get(self, key):
        return self.data.get(key)

    def put(self, key, findings):
        self.data[key] = findings
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'anahtar': key, 'findings': findings}, ensure_ascii=False) + '\n')


# ---------------------------------------------------------------------------
# 6. Temel kaynak kontrolü
# ---------------------------------------------------------------------------

def source_problem(item, page_text):
    """Yapılandırılmış alanların açık kaynak desteği; sorun yoksa None. (kanıt/bilgi kontrolü validate_records'ta)
    Parçalı (çok sayfalı) kayıtta deger/p iki parçanın birleşik metninde aranır; tek sayfalı kayıtta kanit_cumle."""
    parts = item.get('kanit_parcalari')
    if isinstance(parts, list) and parts and all(isinstance(p, dict) and isinstance(p.get('metin'), str) for p in parts):
        quote = ' '.join(p['metin'] for p in parts)
    else:
        quote = item.get('kanit_cumle') or ''
    for field in ('deger', 'p'):
        value = item.get(field)
        if value not in (None, '') and (not isinstance(value, str) or value.strip() not in quote):
            return f'{field} kanıt cümlesinde birebir yok: {value!r}'
    unit = item.get('unit')
    # Birim / grup / sonuç ölçütü (tablo başlığı) sayfada aranır; başlıklardaki <br> ve satır kırılmaları _norm ile
    # tek boşluğa indirgenir (kanıt ve değer kontrolleri birebir kalır).
    if unit not in (None, '') and (not isinstance(unit, str) or (_norm(unit) not in _norm(quote)
                                                                 and _norm(unit) not in _norm(page_text))):
        return f'unit sayfada birebir yok: {unit!r}'
    group = item.get('hasta_grubu')
    if group not in (None, ''):
        if not isinstance(group, str) or (_norm(group) not in _norm(quote) and _norm(group) not in _norm(page_text)):
            return f'hasta_grubu sayfada yok: {group!r}'
    outcome = item.get('sonuc_olcutu_tr')
    if outcome not in (None, ''):
        if not isinstance(outcome, str) or (_norm(outcome) not in _norm(quote) and _norm(outcome) not in _norm(page_text)):
            return f'sonuc_olcutu_tr sayfada yok: {outcome!r}'
    if item.get('kanit_turu', 'metin') not in ('metin', 'tablo'):
        return 'kanit_turu metin veya tablo olmalı'
    return None


# Model zaman zaman ETİKET alanlarını benzer görünen Kiril/Azerice harflerle yazıyor (ör. 'digер', 'digər').
# Yalnız sabit etiketler (bilgi_turu/baglam/kanit_turu) düzeltilir; klinik metin ve kanıt hiç değiştirilmez.
HOMOGLYPHS = str.maketrans({'е': 'e', 'р': 'r', 'о': 'o', 'а': 'a', 'с': 'c', 'і': 'i', 'ı': 'i', 'ə': 'e',
                            'ѕ': 's', 'к': 'k', 'т': 't', 'у': 'y', 'х': 'x', 'м': 'm', 'н': 'n'})


# LLM kaydında izinli alan adları: pilot_clinical.import_records'un kabul ettiği küme (başka alan orada da hata verir).
ALLOWED_FIELDS = frozenset(INPUT_FIELDS) | frozenset(STRUCTURED_FIELDS)
# Yalnız açıkça tanımlı bozuk alan adları düzeltilir (Zehra'nın bulguları + önbellekte gözlenenler). Genel bulanık
# eşleme yok: listede olmayan bilinmeyen alan doğrulama hatasıdır.
FIELD_ALIASES = {
    'bagcontext': 'baglam',
    'bag context': 'baglam',
    'bagлан': 'baglam',          # Kiril л, а, н
    'hasta_grupu': 'hasta_grubu',
    'kanit_cумle': 'kanit_cumle',  # Kiril у, м
    'kanit_cumple': 'kanit_cumle',
}


def normalize_fields(item):
    """(kayıt, düzeltme notları, hata). Tek ve çok sayfalı kayıtlar için ortak alan adı doğrulaması.
    - FIELD_ALIASES'taki bozuk ad doğru ada taşınır; değer DEĞİŞTİRİLMEZ.
    - Doğru alan da varsa doğru alan korunur, bozuk alan atılır; değerler farklıysa atılan değer nota yazılır
      (birleştirme yapılmaz, sessizce kaybolmaz).
    - İzinli olmayan ve listede olmayan alan adı -> hata (kayıt reddedilir)."""
    out, notes = {}, []
    for key, value in item.items():
        if key in FIELD_ALIASES:
            continue
        out[key] = value
    for key, value in item.items():
        target = FIELD_ALIASES.get(key)
        if target is None:
            continue
        if target not in item:
            out[target] = value
            notes.append(f'alan_adi:{key}->{target}')
        elif item[target] == value:
            notes.append(f'alan_adi:{key} atildi (={target})')
        else:
            notes.append(f'alan_cakismasi:{key} atildi, {target} korundu; atilan deger={value!r}')
    unknown = sorted(k for k in out if k not in ALLOWED_FIELDS)
    if unknown:
        return out, notes, 'Tanınmayan alan adı: ' + ', '.join(repr(k) for k in unknown)
    return out, notes, None


def fix_label(value, allowed):
    """Geçerli değilse homoglif düzeltmesi; sonuç izinli kümede değilse değer olduğu gibi döner."""
    if not isinstance(value, str) or value in allowed:
        return value
    fixed = value.translate(HOMOGLYPHS)
    return fixed if fixed in allowed else value


# Metin içi atıf işareti (ör. "_(1)_", "_(4,5)_", "_(2–4)_"). <sup>..</sup> bilerek dahil değil: yazar/dipnot
# işaretleri ve "10<sup>9</sup>/L" gibi değerlerin parçası olabilir.
CITATION = re.compile(r'\s*_\(\s*\d+(?:\s*[,–-]\s*\d+)*\s*\)_')
# Model atıfı alıntıda "_(1)_" yerine "(1)" yazabiliyor; yalnız ALINTI tarafında tanınır ve sayfadaki atıf
# numaralarıyla birebir aynı olmak zorundadır (match_without_citations).
QUOTE_CITATION = re.compile(r'\s*_?\(\s*\d+(?:\s*[,–-]\s*\d+)*\s*\)_?')
MIN_CITATION_MATCH = 20


def _citation_numbers(text, pattern):
    return [re.findall(r'\d+', m.group(0)) for m in pattern.finditer(text)]


def _strip_citations(text, pattern=CITATION):
    """(normalize metin, her normalize karakterin özgün indeksi): atıflar silinir, boşluk dizileri tek boşluk,
    noktalama öncesi boşluk silinir. Yalnız atıf işareti ve boşluk farkı yok sayılır."""
    removed = set()
    for match in pattern.finditer(text):
        removed.update(range(match.start(), match.end()))
    chars, index = [], []
    for i, ch in enumerate(text):
        if i in removed:
            continue
        if ch.isspace():
            if chars and chars[-1] != ' ':
                chars.append(' '); index.append(i)
            continue
        if ch in '.,;:!?)' and chars and chars[-1] == ' ':
            chars.pop(); index.pop()
        chars.append(ch); index.append(i)
    while chars and chars[-1] == ' ':
        chars.pop(); index.pop()
    start = 1 if chars and chars[0] == ' ' else 0
    return ''.join(chars[start:]), index[start:]


def match_without_citations(quote, excerpt):
    """Model alıntıdan atıf işaretini çıkardıysa: atıfsız/boşluk-normalize biçimi parçada TEKİL eşleşirse parçanın
    birebir özgün metnini döndürür. Eşleşme yok / birden çok / çok kısa ise None (kayıt reddedilmeye devam eder)."""
    norm_quote, _ = _strip_citations(quote, QUOTE_CITATION)
    if len(norm_quote) < MIN_CITATION_MATCH or not CITATION.search(excerpt):
        return None
    norm_page, index = _strip_citations(excerpt)
    first = norm_page.find(norm_quote)
    if first < 0 or norm_page.find(norm_quote, first + 1) >= 0:
        return None
    end = index[first + len(norm_quote) - 1] + 1
    # Alıntının sonundaki atıf (ör. "rates (1).") sayfada noktadan önce gelir: noktayı içeren aralığa dahildir.
    original = excerpt[index[first]:end]
    # Güvence 1: özgün metin ile alıntı arasında yalnız atıf işareti / boşluk farkı var.
    if _strip_citations(original)[0] != norm_quote:
        return None
    # Güvence 2: alıntıda atıf numarası yazılmışsa, sayfadaki atıflarla birebir aynı sırada olmalı.
    quoted = _citation_numbers(quote, QUOTE_CITATION)
    if quoted and quoted != _citation_numbers(original, CITATION):
        return None
    return original


def repair_quote(quote, excerpt):
    """Kanıt parçada birebir yoksa yalnız kenar boşluğu / tablo satırı başındaki fazladan '|' kırpılır; son çare olarak
    modelin sildiği atıf işaretleri tekil eşleşmeyle geri konur. Sonuç her zaman parçanın birebir alt dizgisidir."""
    if not isinstance(quote, str):
        return None
    stripped = quote.strip()
    for candidate in (quote, stripped, re.sub(r'^\|{2,}', '|', stripped), re.sub(r'\|{2,}$', '|', re.sub(r'^\|{2,}', '|', stripped))):
        if candidate and candidate in excerpt:
            return candidate
    return match_without_citations(stripped, excerpt)


def screen(task, findings, task_no):
    """Ön eleme (sayfa/tür/kanıt parçada) + yapılandırılmış alan kaynak kontrolü. (kabul, ret)"""
    accepted, rejected = [], []
    page, excerpt = task['sayfa'], task['page_excerpt']
    for position, item in enumerate(findings, 1):
        def reject(reason):
            rejected.append({'sayfa': page, 'gorev_no': task_no, 'gorev_ici_sira': position, 'neden': reason,
                             'asama': 'on_eleme', 'kayit': item})
        if not isinstance(item, dict):
            reject('Kayıt nesne değil'); continue
        item, field_notes, problem = normalize_fields(item)
        if problem:
            reject(problem); continue
        original = item
        item = dict(item)
        item['bilgi_turu'] = fix_label(item.get('bilgi_turu'), TYPES)
        if item.get('baglam') is not None:
            item['baglam'] = fix_label(item.get('baglam'), CONTEXTS)
        if item.get('kanit_turu') is not None:
            item['kanit_turu'] = fix_label(item.get('kanit_turu'), {'metin', 'tablo'})
        if item.get('sayfa') != page or item.get('bilgi_turu') not in TYPES or (
                item.get('baglam') is not None and item.get('baglam') not in CONTEXTS):
            reject('Sayfa veya tür geçersiz'); continue
        quote = repair_quote(item.get('kanit_cumle'), excerpt)
        if quote is None:
            reject('Kanıt parçada bulunamadı'); continue
        if quote != item['kanit_cumle']:
            item['kanit_cumle'] = quote
        fixes = [f for f in ('bilgi_turu', 'baglam', 'kanit_turu', 'kanit_cumle') if item.get(f) != original.get(f)]
        fixes += field_notes
        if fixes:
            item['otomatik_duzeltme'] = fixes
        problem = source_problem(item, excerpt)
        if problem:
            reject(problem); continue
        item = {k: v for k, v in item.items() if v not in (None, '')}
        item.setdefault('kanit_turu', 'metin')
        accepted.append((item, {'gorev_no': task_no, 'gorev_ici_sira': position}))
    return accepted, rejected


def screen_boundary(task, findings, task_no, pages):
    """Sınır görevi ön elemesi: tam 2 parça, her parça KENDİ sayfasının penceresinde birebir; sol parça pencerenin
    (= sayfa N'nin) sonunda, sağ parça pencerenin (= sayfa N+1'in) başında. sayfa / sayfa_bitis / kanit_cumle /
    kanit_turu / birlestirme kodda türetilir (LLM değerleri yok sayılır). Sonrasında validate_records aynen uygulanır."""
    accepted, rejected = [], []
    left_page, right_page = task['sayfa'], task['sayfa_bitis']
    windows = {left_page: task['left_excerpt'], right_page: task['right_excerpt']}
    for position, item in enumerate(findings, 1):
        def reject(reason):
            rejected.append({'sayfa': left_page, 'sayfa_bitis': right_page, 'gorev_no': task_no,
                             'gorev_ici_sira': position, 'neden': reason, 'asama': 'on_eleme', 'kayit': item})
        if not isinstance(item, dict):
            reject('Kayıt nesne değil'); continue
        item, field_notes, problem = normalize_fields(item)
        if problem:
            reject(problem); continue
        original = item
        item = dict(item)
        item['bilgi_turu'] = fix_label(item.get('bilgi_turu'), TYPES)
        if item.get('baglam') is not None:
            item['baglam'] = fix_label(item.get('baglam'), CONTEXTS)
        if item.get('bilgi_turu') not in TYPES or (item.get('baglam') is not None and item.get('baglam') not in CONTEXTS):
            reject('Sınır kaydı tür geçersiz'); continue
        parts = item.get('kanit_parcalari')
        if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(p, dict) for p in parts):
            reject('Sınır kaydı tam iki kanıt parçası içermeli'); continue
        clean, problem = [], None
        for part, page in zip(parts, (left_page, right_page)):
            text = part.get('metin')
            if part.get('sayfa') != page or not isinstance(text, str) or not text.strip():
                problem = 'Kanıt parçası sayfası/metni geçersiz'; break
            if text.strip() not in windows[page]:
                problem = f'Kanıt parçası s.{page} penceresinde birebir bulunamadı'; break
            clean.append({'sayfa': page, 'metin': text.strip()})
        if problem:
            reject(problem); continue
        if not (windows[left_page].rstrip().endswith(clean[0]['metin'])
                and windows[right_page].lstrip().startswith(clean[1]['metin'])):
            reject('Kanıt parçaları sayfa sonu / sonraki sayfa başı değil'); continue
        try:
            joined = check_evidence_parts(clean, pages)
        except ValueError as exc:
            reject(str(exc)); continue
        item.update({'kanit_parcalari': clean, 'sayfa': left_page, 'sayfa_bitis': right_page,
                     'kanit_cumle': clean[0]['metin'], 'kanit_turu': 'metin', 'birlestirme': joined})
        fixes = [f for f in ('bilgi_turu', 'baglam') if item.get(f) != original.get(f)]
        if any(c['metin'] != p.get('metin') for c, p in zip(clean, parts)):
            fixes.append('kanit_parcalari')
        fixes += field_notes
        if fixes:
            item['otomatik_duzeltme'] = fixes
        problem = source_problem(item, windows[left_page] + '\n' + windows[right_page])
        if problem:
            reject(problem); continue
        item = {k: v for k, v in item.items() if v not in (None, '')}
        accepted.append((item, {'gorev_no': task_no, 'gorev_ici_sira': position}))
    return accepted, rejected


def extract(pages, tasks, client, model, *, cache=None, max_calls=60, attempts=3):
    """(geçerli kayıtlar, ret, bu çalıştırmadaki API çağrısı). Bütçe yetmezse hiç çağrı yapmadan durur."""
    cache = cache or Cache(None)
    requests = [task_request(t) for t in tasks]
    keys = [Cache.key(model, system, payload) for system, payload in requests]
    pending = sum(cache.get(k) is None for k in keys)
    if pending > max_calls:
        raise RuntimeError(f'API çağrı sınırı yetersiz: {pending} bekleyen çağrı > {max_calls}; hiç çağrı yapılmadı')
    calls, candidates, rejected = 0, [], []
    for number, (task, key, (system, payload)) in enumerate(zip(tasks, keys, requests), 1):
        kind = task.get('tur', 'sayfa')
        findings = cache.get(key)
        if findings is None:
            if kind == 'tablo':
                # Tablo görevi başarısızsa mevcut (sayfa görevinden gelen) tablo kayıtları aynen kalır; çalıştırma durmaz.
                try:
                    findings = request_with_retry(client, model, system, payload, task['sayfa'], attempts=attempts)
                except Exception as exc:  # noqa: BLE001 - hata kaydedilir, eski kayıtlar korunur
                    calls += 1
                    rejected.append({'sayfa': task['sayfa'], 'gorev_no': number, 'neden': f'Tablo görevi başarısız: {exc}',
                                     'asama': 'tablo_gorevi'})
                    continue
            else:
                findings = request_with_retry(client, model, system, payload, task['sayfa'], attempts=attempts)
            calls += 1
            cache.put(key, findings)
        if kind == 'sinir':
            ok, bad = screen_boundary(task, findings, number, pages)
        elif kind == 'tablo':
            ok, bad = screen_table(task, findings, number)
        else:
            ok, bad = screen(task, findings, number)
        candidates.extend((item, {**origin, 'tur': kind}) for item, origin in ok)
        rejected.extend(bad)
    records = [item for item, _ in candidates]
    valid, errors = validate_records(records, pages)
    bad_rows = {e['satir'] - 1: e['hata'] for e in errors}
    for index, message in sorted(bad_rows.items()):
        item, origin = candidates[index]
        origin = {k: v for k, v in origin.items() if k != 'tur'}
        rejected.append({'sayfa': item.get('sayfa'), **origin, 'neden': message, 'asama': 'dogrulama', 'kayit': item})
    kept = [candidates[i] for i in range(len(candidates)) if i not in bad_rows]
    return replace_table_records(kept, rejected), rejected, calls


def screen_table(task, findings, task_no):
    """Tablo görevi ön elemesi: sayfa görevinin ortak ön elemesi (alan adı, etiket, kanıt, kaynak) + kayıt bir tablo
    satırına (| ... |) dayanmalı; kanit_turu kodda 'tablo' yapılır."""
    ok, bad = screen(task, findings, task_no)
    accepted = []
    for item, origin in ok:
        if not item['kanit_cumle'].lstrip().startswith('|'):
            bad.append({'sayfa': task['sayfa'], **origin, 'neden': 'Tablo görevi kaydı tablo satırına dayanmıyor',
                        'asama': 'on_eleme', 'kayit': item})
            continue
        accepted.append(({**item, 'kanit_turu': 'tablo'}, origin))
    return accepted, bad


def _superseded(old, newer):
    """Eski (sayfa görevi) tablo kaydı, aynı satırın tablo görevi kayıtlarıyla karşılanıyor mu?
    - aynı hücre: bilgi ve değer aynı (yeni kayıt sütun bilgisini de taşır), ya da
    - eski kayıt hücre değerini bilgi diye yazmış (değeri yok) ve bu değer yeni kayıtlarda o satırın değeri.
    Karşılanmayan eski kayıt (ör. tablo görevinin atladığı RR hücresi, tedavi sütunu) korunur."""
    fact, value = _norm(old.get('klinik_bilgi_en')), _norm(old.get('deger'))
    for new in newer:
        if fact and _norm(new.get('klinik_bilgi_en')) == fact and _norm(new.get('deger')) == value:
            return True
        if not value and fact and fact == _norm(new.get('deger')):
            return True
    return False


def replace_table_records(kept, rejected):
    """Tablo görevinin geçerli kayıtları olan satırlarda, karşılanan eski tablo kayıtları çıkarılır (ret raporuna
    'tablo_degisimi' olarak yazılır). Tablo görevi yoksa / o satırı vermediyse eski kayıtlar aynen kalır."""
    newer = defaultdict(list)
    for item, origin in kept:
        if origin.get('tur') == 'tablo':
            newer[row_key(item)].append(item)
    out = []
    for item, origin in kept:
        if (origin.get('tur') != 'tablo' and item.get('kanit_turu') == 'tablo' and newer.get(row_key(item))
                and _superseded(item, newer[row_key(item)])):
            rejected.append({'sayfa': item.get('sayfa'), 'gorev_no': origin.get('gorev_no'),
                             'gorev_ici_sira': origin.get('gorev_ici_sira'),
                             'neden': 'Aynı satırın tablo görevi kaydıyla değiştirildi', 'asama': 'tablo_degisimi',
                             'kayit': item})
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 7. 8 sunum kategorisine yönlendirme (sql/sunum_hoca/*.sql karşılığı)
# ---------------------------------------------------------------------------

def row_key(item):
    return (item.get('sayfa'), item.get('kanit_cumle'))


def is_total(item):
    return bool(TOTAL_GROUP.match(_norm(item.get('hasta_grubu'))))


def grouped_rows(records):
    """Karşılaştırma grubu (Total hariç) taşıyan tablo satırları = v_sunum_taban.satir_grup_karsilastirmasi."""
    return {row_key(r) for r in records
            if r.get('kanit_turu') == 'tablo' and r.get('hasta_grubu') and not is_total(r)}


def categories_of(item, grouped):
    kind, evidence = item.get('bilgi_turu'), item.get('kanit_turu', 'metin')
    cats = []
    if kind == 'tani':
        cats.append('diseases')
    if kind == 'semptom':
        cats.append('symptoms')
    if kind in ('teshis_yontemi', 'tani_kriteri'):
        cats.append('diagnostics')
    if kind in ('ilac', 'doz', 'tedavi'):
        cats.append('treatments')
    if evidence == 'metin':
        if kind in ('diger', 'hasta_grubu', 'olcum'):
            cats.append('general_clinical_findings')
        if kind == 'klinik_sonuc':
            cats.append('clinical_outcomes')
    else:
        if item.get('hasta_grubu'):
            cats.append('measurements_by_group')
        if row_key(item) in grouped:
            cats.append('measurements')
        elif not (kind in ('ilac', 'doz', 'tedavi', 'teshis_yontemi')
                  and not any(item.get(f) for f in ('etki_turu', 'deger', 'p'))):
            cats.append('clinical_outcomes')
    return cats


def flat(item, pdf_name, category):
    """Ortak çıktı satırı: PDF, sayfa, kategori, klinik bilgi, değer/birim, hasta grubu, PDF'ten birebir kanıt.
    Çok sayfalı kayıtta sayfa_bitis / kanit_parcalari / birlestirme da eklenir (tek sayfalı çıktı değişmez)."""
    row = {'pdf': pdf_name, 'sayfa': item.get('sayfa'), 'kategori': category,
           'klinik_bilgi': item.get('klinik_bilgi_en') or item.get('klinik_bilgi'),
           'deger': item.get('deger'), 'birim': item.get('unit'), 'hasta_grubu': item.get('hasta_grubu'),
           'p': item.get('p'), 'bilgi_turu': item.get('bilgi_turu'), 'baglam': item.get('baglam'),
           'kanit_turu': item.get('kanit_turu', 'metin'), 'kanit': item.get('kanit_cumle')}
    if item.get('kanit_parcalari') is not None:
        row.update({'sayfa_bitis': item.get('sayfa_bitis'), 'kanit_parcalari': item.get('kanit_parcalari'),
                    'birlestirme': item.get('birlestirme')})
    if item.get('sonuc_olcutu_tr') is not None:
        row['sonuc_olcutu'] = item['sonuc_olcutu_tr']
    return row


def route(records, pdf_name):
    """{kategori: [satır]}; measurements geniş biçim (satır başına gruplar yan yana, Total ayrı)."""
    grouped = grouped_rows(records)
    out = {c: [] for c in CATEGORIES}
    wide = {}
    for item in records:
        for category in categories_of(item, grouped):
            if category != 'measurements':
                out[category].append(flat(item, pdf_name, category))
                continue
            row = wide.setdefault(row_key(item), {
                'pdf': pdf_name, 'sayfa': item.get('sayfa'), 'kategori': 'measurements',
                'klinik_bilgi': item.get('klinik_bilgi_en'), 'gruplar': [], 'total_group': None,
                'total_value': None, 'birim': None, 'p': None, 'kanit_turu': 'tablo', 'kanit': item.get('kanit_cumle')})
            if item.get('hasta_grubu') and is_total(item):
                row['total_group'], row['total_value'] = item['hasta_grubu'], item.get('deger')
            elif item.get('hasta_grubu'):
                row['gruplar'].append({'grup': item['hasta_grubu'], 'deger': item.get('deger')})
            row['birim'] = row['birim'] or item.get('unit')
            row['p'] = row['p'] or item.get('p')
    out['measurements'] = list(wide.values())
    return out


# ---------------------------------------------------------------------------
# Komut
# ---------------------------------------------------------------------------

def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def run(pdf, *, client=None, model=None, max_calls=60, top_k=6, out_root=OUTPUT_ROOT, embedder=None, search=None,
        plan_only=False, stamp=None, boundary=False, table=False):
    doc, pages = pdf_pages(pdf)
    chunks = build_chunks(doc, embedder)
    page_targets, hits = plan_pages(chunks, top_k=top_k, search=search)
    added, excluded = fill_coverage(doc, page_targets)
    # Mevcut retrieval görevleri önce ve aynen (sıra, ipucu, önbellek anahtarı); kapsama görevleri ipucusuz sonra;
    # sınır görevleri (yalnız --sinir) en sonda.
    retrieval_tasks = build_tasks(pages, page_targets)
    coverage_tasks = build_tasks(pages, {page: [] for page in added})
    boundary_tasks = build_boundary_tasks(doc, pages, set(page_targets) | set(added)) if boundary else []
    # Tablo görevleri (yalnız --tablo) en sonda: önceki görevlerin sırası, numarası ve anahtarları değişmez.
    table_tasks = build_table_tasks(pages, set(page_targets) | set(added)) if table else []
    tasks = retrieval_tasks + coverage_tasks + boundary_tasks + table_tasks
    meta = {'surum': VERSION, 'pdf': doc['filename'], 'document_id': doc['document_id'],
            'processing_version': doc['processing_version'], 'sayfa_sayisi': len(pages), 'chunk_sayisi': len(chunks),
            'secilen_sayfalar': page_targets, 'kapsama_tamamlama': added, 'kapsama_disi': excluded,
            'atlanan_sayfalar': sorted(set(pages) - set(page_targets) - set(added)),
            'retrieval_gorev_sayisi': len(retrieval_tasks), 'kapsama_gorev_sayisi': len(coverage_tasks),
            'sinir': boundary, 'sinir_gorev_sayisi': len(boundary_tasks),
            'sinir_sayfa_ciftleri': [[t['sayfa'], t['sayfa_bitis']] for t in boundary_tasks],
            'tablo': table, 'tablo_gorev_sayisi': len(table_tasks),
            'tablo_sayfalari': sorted({t['sayfa'] for t in table_tasks}),
            'gorev_sayisi': len(tasks), 'top_k': top_k}
    if plan_only:
        return meta, None
    model = model or default_model()
    base = Path(out_root) / Path(doc['filename']).stem
    cache = Cache(base / 'llm_onbellek.jsonl')
    client = client or llm_client()
    valid, rejected, calls = extract(pages, tasks, client, model, cache=cache, max_calls=max_calls)
    routed = route(valid, doc['filename'])
    run_dir = base / (stamp or datetime.now().strftime('%Y%m%d-%H%M%S'))
    run_dir.mkdir(parents=True, exist_ok=False)
    for category in CATEGORIES:
        write_json(run_dir / f'{category}.json', routed[category])
    write_json(run_dir / 'kayitlar.json', [{**r, 'kategoriler': categories_of(r, grouped_rows(valid))} for r in valid])
    write_json(run_dir / 'reddedilenler.json', rejected)
    if table:
        meta['tablo_degisimi'] = sum(r.get('asama') == 'tablo_degisimi' for r in rejected)
        meta['tablo_gorevi_hatasi'] = sum(r.get('asama') == 'tablo_gorevi' for r in rejected)
    meta.update({'model': model, 'api_cagrisi': calls, 'gecerli_kayit': len(valid), 'reddedilen': len(rejected),
                 'kategori_sayilari': {c: len(routed[c]) for c in CATEGORIES},
                 'kanit_turu': {t: sum(r.get('kanit_turu') == t for r in valid) for t in ('metin', 'tablo')},
                 'retrieval_isabetleri': hits, 'cikti_dizini': str(run_dir),
                 'not': 'Taslak; insan incelemesi zorunlu. Veritabanına yazılmadı.'})
    write_json(run_dir / 'calistirma.json', meta)
    return meta, run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m src.rag_klinik_cikarim',
                                     description='RAG destekli PDF klinik bilgi çıkarımı (yalnız JSON çıktı)')
    parser.add_argument('--pdf', required=True)
    parser.add_argument('--model', help='LLM modeli (varsayılan .env LLM_MODEL)')
    parser.add_argument('--max-calls', type=int, default=60)
    parser.add_argument('--top-k', type=int, default=6, help='kategori sorgusu başına alınan chunk sayısı')
    parser.add_argument('--cikti', default=str(OUTPUT_ROOT))
    parser.add_argument('--plan', action='store_true', help='API çağırmadan sayfa/görev planını göster')
    parser.add_argument('--sinir', action='store_true',
                        help='ardışık iki sayfaya yayılan kanıtlar için sınır görevleri ekle (varsayılan: kapalı)')
    parser.add_argument('--tablo', action='store_true',
                        help='tablo içeren sayfalar için ayrı tablo görevi ekle (varsayılan: kapalı)')
    args = parser.parse_args(argv)
    meta, run_dir = run(args.pdf, model=args.model, max_calls=args.max_calls, top_k=args.top_k,
                        out_root=args.cikti, plan_only=args.plan, boundary=args.sinir, table=args.tablo)
    print(f"PDF: {meta['pdf']} | sayfa {meta['sayfa_sayisi']} | chunk {meta['chunk_sayisi']} | "
          f"görev {meta['gorev_sayisi']} (retrieval {meta['retrieval_gorev_sayisi']} + kapsama {meta['kapsama_gorev_sayisi']}"
          f" + sınır {meta['sinir_gorev_sayisi']} + tablo {meta['tablo_gorev_sayisi']})")
    print(f"Retrieval ile seçilen {len(meta['secilen_sayfalar'])} sayfa (mevcut görevler):")
    for page, cats in meta['secilen_sayfalar'].items():
        print(f'  s.{page}: {", ".join(cats)}')
    print(f"Kapsama tamamlama (ipucusuz tam tarama, yeni görev): {meta['kapsama_tamamlama'] or 'yok'}")
    print(f"Kapsama dışı: {meta['kapsama_disi'] or 'yok'}")
    if meta['sinir']:
        print(f"Sınır görevleri (yeni, ayrı istem): {meta['sinir_gorev_sayisi']} çift "
              f"{[tuple(c) for c in meta['sinir_sayfa_ciftleri']] or ''}")
    else:
        print('Sınır görevleri: kapalı (--sinir ile açılır)')
    if meta['tablo']:
        print(f"Tablo görevleri (yeni, ayrı istem): {meta['tablo_gorev_sayisi']} görev, sayfalar {meta['tablo_sayfalari']}")
    else:
        print('Tablo görevleri: kapalı (--tablo ile açılır)')
    if run_dir:
        print(f"API çağrısı: {meta['api_cagrisi']} | geçerli: {meta['gecerli_kayit']} {meta['kanit_turu']} | "
              f"reddedilen: {meta['reddedilen']}")
        print('Kategoriler: ' + ', '.join(f'{c} {n}' for c, n in meta['kategori_sayilari'].items()))
        print(f'Çıktı: {run_dir} | veritabanına yazılmadı')


if __name__ == '__main__':
    main()
