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


# Dipnot işareti üst simgesi: yalnız harf/sembol (ör. <sup>d</sup>, <sup>**_d_**</sup>, <sup>†</sup>). Sayısal üst
# simge (10<sup>9</sup>/L, m<sup>2</sup>, atıf numaraları) dipnot sayılmaz ve hiçbir karşılaştırmada silinmez.
FOOTNOTE_SUP = re.compile(r'<sup>\s*[*_]*\s*(?:[a-z](?:\s*,\s*[a-z])*|[†‡§¶#*]+)\s*[*_]*\s*</sup>', re.I)
# Kademeli biçim onarımı (en dar -> en geniş): 1) yalnız '**'; 2) + <br> / boşluk farkı; 3) + dipnot harfi üst simgesi.
_MARKUP_LEVELS = (re.compile(r'\*\*'), re.compile(r'\*\*|<br\s*/?>|\s+', re.I),
                  re.compile(FOOTNOTE_SUP.pattern + r'|\*\*|<br\s*/?>|\s+', re.I))


def _cmp(text):
    """Tekrar karşılaştırma anahtarı: kalın işaret, <br>, dipnot harfi üst simgesi ve boşluk farkı yok sayılır;
    sayısal üst simge '^n' olarak korunur (10<sup>9</sup> != 109). Kayıt metnini DEĞİŞTİRMEZ."""
    text = FOOTNOTE_SUP.sub(' ', str(text or ''))
    text = re.sub(r'<sup>\s*(.*?)\s*</sup>', r'^\1', text, flags=re.I)
    text = re.sub(r'<br\s*/?>|\*\*', ' ', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip().casefold()


def _markup_map(text, pattern=_MARKUP_LEVELS[-1]):
    """(sade metin, her sade karakterin özgün indeksi): pattern'deki '**' ve dipnot harfi üst simgesi silinir, <br> ve
    boşluk dizileri tek boşluk olur. Büyük/küçük harf, rakam, sayısal üst simge ve noktalama aynen kalır."""
    chars, index, pos = [], [], 0
    for match in pattern.finditer(text):
        for i in range(pos, match.start()):
            chars.append(text[i]); index.append(i)
        token = match.group(0)
        if (token.isspace() or token[:3].lower() == '<br') and chars and chars[-1] != ' ':
            chars.append(' '); index.append(match.start())
        pos = match.end()
    for i in range(pos, len(text)):
        chars.append(text[i]); index.append(i)
    return ''.join(chars), index


def source_span(value, source):
    """value kaynakta birebir yoksa, yalnız biçim farkı (kalın, <br>, dipnot harfi, boşluk) yok sayılarak kaynakta
    TEKİL eşleşirse kaynağın özgün alt dizgisini döndürür; yoksa / birden çoksa None. Sonuç her zaman kaynağın
    birebir alt dizgisidir, böylece sonraki birebir kontroller (validate_records) değişmeden uygulanır."""
    if not isinstance(value, str) or not isinstance(source, str):
        return None
    if value.strip() and value.strip() in source:
        return value.strip()
    # En dar kademede tekil eşleşen kullanılır: değerde yazılı <br> / dipnot simgesi kaynakla aynen korunur.
    for pattern in _MARKUP_LEVELS:
        target = _markup_map(value, pattern)[0].strip()
        if not target:
            return None
        plain, index = _markup_map(source, pattern)
        first = plain.find(target)
        if first < 0:
            continue
        if plain.find(target, first + 1) >= 0:
            return None                                  # belirsiz: kaynakta birden çok yer
        return source[index[first]:index[first + len(target) - 1] + 1]
    return None


MARKUP_FIELDS = ('klinik_bilgi_en', 'deger', 'p')


def repair_markup(item, quote):
    """Kanıtta birebir olmayan klinik bilgi / değer / p için biçim onarımı (source_span). Onarılan alan adlarını
    döndürür. Onarılamayan alan olduğu gibi kalır ve sonraki kontrollerde reddedilir."""
    fixed = []
    for field in MARKUP_FIELDS:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip() or value in quote:
            continue
        span = source_span(value, quote)
        if span is not None and span != value:
            item[field] = span
            fixed.append(f'{field}:bicim')
    return fixed


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

def _components(value, found):
    """Alan bileşenleri. found(metin) -> kaynakta var mı. Değerin tamamı kaynakta varsa tek bileşendir (önceki
    davranış); yoksa ';' ile ayrılmış bileşenler. Metin değilse ya da boş bileşen varsa None."""
    if not isinstance(value, str) or not value.strip():
        return None
    if found(value.strip()) or ';' not in value:
        return [value.strip()]
    parts = [part.strip() for part in value.split(';')]
    return parts if all(parts) else None


def source_problem(item, page_text):
    """Yapılandırılmış alanların açık kaynak desteği; sorun yoksa None. (kanıt/bilgi kontrolü validate_records'ta)
    Parçalı (çok sayfalı) kayıtta deger/p iki parçanın birleşik metninde aranır; tek sayfalı kayıtta kanit_cumle."""
    parts = item.get('kanit_parcalari')
    if isinstance(parts, list) and parts and all(isinstance(p, dict) and isinstance(p.get('metin'), str) for p in parts):
        quote = ' '.join(p['metin'] for p in parts)
    else:
        quote = item.get('kanit_cumle') or ''
    counts = {}
    for field in ('deger', 'p'):
        value = item.get(field)
        if value in (None, ''):
            continue
        parts = _components(value, lambda text: text in quote)
        # Tek değer kanıtta birebir; ';' ile ayrılmış çok değerde HER bileşen kanıtta birebir olmalı.
        if parts is None or any(part not in quote for part in parts):
            return f'{field} kanıt cümlesinde birebir yok: {value!r}'
        counts[field] = len(parts)
    # Birim / grup / sonuç ölçütü (tablo başlığı) sayfada aranır; başlıklardaki <br> ve satır kırılmaları _norm ile
    # tek boşluğa indirgenir (kanıt ve değer kontrolleri birebir kalır). Çok değerde her bileşen ayrı aranır.
    for field, label in (('unit', 'unit sayfada birebir yok'), ('hasta_grubu', 'hasta_grubu sayfada yok')):
        value = item.get(field)
        if value in (None, ''):
            continue
        on_page = lambda text: bool(_norm(text)) and (_norm(text) in _norm(quote)  # noqa: E731
                                                      or _norm(text) in _norm(page_text))
        parts = _components(value, on_page)
        if parts is None or not all(on_page(part) for part in parts):
            return f'{label}: {value!r}'
        counts[field] = len(parts)
    # Grup / değer / birim ilişkisi: birden çok çok-değerli alan varsa bileşen sayıları aynı olmalı (eşleme sırayla);
    # aksi halde hangi değerin hangi gruba/birime ait olduğu belirsizdir -> kayıt reddedilir.
    multi = {f: n for f, n in counts.items() if n > 1 and f != 'p'}
    if len(set(multi.values())) > 1:
        return 'çok değerli alanların bileşen sayısı uyuşmuyor: ' + ', '.join(f'{f}={n}' for f, n in sorted(multi.items()))
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
        markup_fixes = repair_markup(item, quote)
        fixes = [f for f in ('bilgi_turu', 'baglam', 'kanit_turu', 'kanit_cumle') if item.get(f) != original.get(f)]
        fixes += markup_fixes + field_notes
        if fixes:
            item['otomatik_duzeltme'] = fixes
        problem = source_problem(item, excerpt)
        if problem:
            reject(problem); continue
        item = {k: v for k, v in item.items() if v not in (None, '')}
        item.setdefault('kanit_turu', 'metin')
        accepted.append((item, {'gorev_no': task_no, 'gorev_ici_sira': position}))
    return accepted, rejected


# Sınır kanıtı için cümle sonu: '.', '!' ya da '?' (ardından isteğe bağlı atıf/dipnot üst simgesi) + boşluk/son,
# ya da paragraf/başlık arası boş satır. Ondalık sayı (0.48), kısaltma (et al., vs., eg., Fig., No.) ve tek büyük
# harf baş harfi (J. Smith) cümle sonu sayılmaz.
_SENTENCE_STOP = re.compile(r'[.!?](?:<sup>[^<]{0,20}</sup>)?(?=\s|$)|\n[ \t]*\n')
_NOT_SENTENCE_END = re.compile(r'(?:\bet al|\bvs|\be\.?g|\bi\.?e|\bFigs?|\bNo|\bapprox|(?<![\w.])[A-Z])$')


def _sentence_stops(text):
    """Metindeki cümle sonlarının bitiş konumları (artan)."""
    for match in _SENTENCE_STOP.finditer(text):
        if match.group(0)[0] == '.' and _NOT_SENTENCE_END.search(text[:match.start()]):
            continue
        yield match.end()


def trim_boundary_parts(left, right):
    """Sınır kanıtını sayfa sınırını geçen TEK cümleye indirir: sol parça son cümle sonundan sonra başlar, sağ parça
    ilk cümle sonunda biter. Sonuçlar hâlâ sol pencerenin sonu / sağ pencerenin başıdır (birebir alt dizgi).
    Sol parça bir cümle sonuyla bitiyorsa (sınırı geçen cümle yok) sol sonuç boş döner."""
    stops = [end for end in _sentence_stops(left) if end <= len(left.rstrip())]
    if stops and stops[-1] >= len(left.rstrip()):
        return '', right
    if stops:
        left = left[stops[-1]:].lstrip()
    first = next(_sentence_stops(right), None)
    if first is not None:
        right = right[:first].rstrip()
    return left, right


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
        # Model pencerenin tamamını parça olarak döndürebiliyor: kanıt sınırı geçen tek cümleye kırpılır.
        left_text, right_text = trim_boundary_parts(clean[0]['metin'], clean[1]['metin'])
        if not left_text or not right_text:
            reject('Sınır kanıtı sayfa sınırını geçen bir cümle değil'); continue
        clean = [{'sayfa': left_page, 'metin': left_text}, {'sayfa': right_page, 'metin': right_text}]
        try:
            joined = check_evidence_parts(clean, pages)
        except ValueError as exc:
            reject(str(exc)); continue
        # Klinik bilgi kırpılmış parçalarda aranır (validate_records.fact_in_parts); pencerenin başka cümlesinden
        # gelen bilgi artık doğrulanamaz ve reddedilir.
        markup_fixes = repair_markup(item, left_text + ' ' + right_text)
        item.update({'kanit_parcalari': clean, 'sayfa': left_page, 'sayfa_bitis': right_page,
                     'kanit_cumle': clean[0]['metin'], 'kanit_turu': 'metin', 'birlestirme': joined})
        fixes = [f for f in ('bilgi_turu', 'baglam') if item.get(f) != original.get(f)]
        if any(c['metin'] != p.get('metin') for c, p in zip(clean, parts)):
            fixes.append('kanit_parcalari')
        fixes += markup_fixes + field_notes
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
    candidates = drop_non_english(candidates, rejected)
    candidates = drop_abbreviation_lists(candidates, rejected)
    candidates = normalize_table_cells(candidates, pages, rejected)
    records = [item for item, _ in candidates]
    valid, errors = validate_records(records, pages)
    bad_rows = {e['satir'] - 1: e['hata'] for e in errors}
    for index, message in sorted(bad_rows.items()):
        item, origin = candidates[index]
        origin = {k: v for k, v in origin.items() if k != 'tur'}
        rejected.append({'sayfa': item.get('sayfa'), **origin, 'neden': message, 'asama': 'dogrulama', 'kayit': item})
    kept = [candidates[i] for i in range(len(candidates)) if i not in bad_rows]
    records = replace_table_records(kept, rejected)
    from src.rag_tablo_hizalama import align_tables
    records = align_tables(records, pages, rejected)
    from src.rag_tablo_ozel import apply_special_tables
    records = apply_special_tables(records, pages, rejected)
    records = drop_table_duplicates(records, rejected)
    records = drop_cell_duplicates(records, rejected)
    records = drop_p_duplicates(records, rejected)
    records = drop_aligned_duplicates(records, rejected)
    records = drop_boundary_duplicates(records, rejected)
    records = hold_for_review(records, pages, rejected)
    from src.rag_anlam_denetimi import check_meaning
    records = check_meaning(records, pages, rejected)
    return join_evidence(records), rejected, calls


def _evidence_text(item):
    """Dil kontrolü için kaydın kanıtı: çok parçalı kayıtta bütün parçalar (hiçbir parça atılmaz), yoksa kanit_cumle;
    ikisi de yoksa klinik bilgi (rag_son_filtre.ingilizce_olmayan ile aynı öncelik)."""
    parts = item.get('kanit_parcalari')
    if isinstance(parts, list) and parts:
        return ' '.join(str(p.get('metin', '')) for p in parts if isinstance(p, dict))
    return str(item.get('kanit_cumle') or item.get('klinik_bilgi_en') or '')


_ABBREVIATION_MARK = re.compile(r'\b(?:Abbreviations?|Abbr\.)\s*:', re.I)
# Kısaltma sözlüğü girdisi: '<KISALTMA>, <açılım>' ; girdiler ';' ile ayrılır, liste '.' ya da metin sonuyla biter.
_GLOSSARY_ENTRY = re.compile(r'^\s*[^\s,;]{1,25}(?:\s[^\s,;]{1,25}){0,2},\s*[^,;]{2,}$')


def glossary_span(text):
    """Metindeki şekil / tablo altı kısaltma listesinin [başlangıç, bitiş) aralığı; liste yapısında değilse None.
    Liste 'Abbreviations:' işaretinden sonra başlar, ilk cümle sonunda ('. ' + büyük harf ya da metin sonu) biter ve
    her girdisi '<kısaltma>, <açılım>' biçiminde olmalıdır."""
    match = _ABBREVIATION_MARK.search(text or '')
    if not match:
        return None
    rest = text[match.end():]
    stop = re.search(r'\.(?=\s+[A-Z*#]|\s*$)', rest)
    body = rest[:stop.start()] if stop else rest
    entries = [e for e in body.split(';') if e.strip()]
    if not entries or not all(_GLOSSARY_ENTRY.match(e) for e in entries):
        return None
    return match.start(), match.end() + len(body)


def drop_abbreviation_lists(candidates, rejected):
    """Klinik bilgisi kanıtta YALNIZ kısaltma listesinin içinde geçen metin kaydı reddedilir ('kisaltma_listesi').
    Aynı kanıtın liste dışındaki kısmından (ör. şekil başlığı, şekle atıf yapan cümle) çıkarılan bilgi kalır."""
    out = []
    for item, origin in candidates:
        quote = item.get('kanit_cumle') or ''
        fact = item.get('klinik_bilgi_en') or ''
        span = glossary_span(quote) if item.get('kanit_turu', 'metin') == 'metin' and not item.get('kanit_parcalari') else None
        starts = [m.start() for m in re.finditer(re.escape(fact), quote)] if fact else []
        if span and starts and all(span[0] <= s and s + len(fact) <= span[1] for s in starts):
            info = {k: v for k, v in origin.items() if k != 'tur'}
            rejected.append({'sayfa': item.get('sayfa'), **info, 'asama': 'kisaltma_listesi', 'kayit': item,
                             'neden': 'Klinik bilgi yalnız şekil/tablo altı kısaltma listesinde geçiyor'})
            continue
        out.append((item, origin))
    return out


def drop_non_english(candidates, rejected):
    """Kanıtı İngilizce olmayan kayıt (src.rag_son_filtre.is_non_english: Latin dışı harf oranı yüksek ya da belirgin
    Fransızca) reddedilir; ret raporuna 'dil' aşamasıyla yazılır."""
    from src.rag_son_filtre import is_non_english
    out = []
    for item, origin in candidates:
        if is_non_english(_evidence_text(item)):
            info = {k: v for k, v in origin.items() if k != 'tur'}
            rejected.append({'sayfa': item.get('sayfa'), **info, 'asama': 'dil', 'kayit': item,
                             'neden': 'Kanıt İngilizce değil (rag_son_filtre.is_non_english)'})
            continue
        out.append((item, origin))
    return out


# ---------------------------------------------------------------------------
# Tablo hücresi temsili (genel kurallar; makaleye özel sabit yok)
# ---------------------------------------------------------------------------

_BR = re.compile(r'<br\s*/?>', re.I)
INTERVENTION_TYPES = ('tedavi', 'teshis_yontemi')       # işlem / yöntem hücresi: metin hücrenin kendisidir
MEDICATION_TYPES = ('ilac', 'doz')                       # ilaç / doz: sayısal değer (doz, oran) taşıyabilir
CELL_CONTEXT_FIELDS = ('sonuc_olcutu_tr', 'hasta_grubu', 'unit', 'p', 'ci95', 'etki_turu', 'comparator')


def row_cells(row):
    """Markdown tablo satırının ham hücreleri; tablo satırı değilse []."""
    text = str(row or '').strip()
    if len(text) < 2 or not (text.startswith('|') and text.endswith('|')):
        return []
    return [cell.strip() for cell in text[1:-1].split('|')]


def _cell_lines(cell):
    return [line.strip() for line in _BR.split(cell) if line.strip()]


def is_whole_cell(value, row):
    """value satırda tam bir hücre ya da bir hücrenin <br> ile ayrılmış tam bir satırı mı (biçim farkı _cmp ile yok
    sayılır). Hücre metninin içinden ayrıştırılmış alt değer (ör. 'Ablation: LVEF 35 ± 9% ...' içindeki '35 ± 9%') değil."""
    target = _cmp(value)
    if not target:
        return False
    return any(_cmp(cell) == target or any(_cmp(line) == target for line in _cell_lines(cell))
               for cell in row_cells(row))


def row_parameter(item):
    """Satır değişkeni (ölçüm parametresi): kanıt satırının ilk hücresi. İlk hücre <br> ile birden çok değişken taşıyorsa
    (ör. 'Male<br>Female') kaydın klinik bilgisine birebir (_cmp) karşılık gelen satır; karşılık yoksa belirsizdir ve
    kaydın doğrulanmış klinik bilgisi kullanılır. Dönen metin her zaman kanıt satırının (ya da bilginin) birebir parçasıdır."""
    cells = row_cells(item.get('kanit_cumle'))
    fact = item.get('klinik_bilgi_en')
    if cells and cells[0] and _cmp(cells[0]) == _cmp(fact):
        return cells[0]                                   # tek değişken (<br> yalnız satır kırılması olabilir)
    lines = _cell_lines(cells[0]) if cells else []
    for line in lines:
        if _cmp(line) == _cmp(fact):
            return line
    if len(lines) == 1:
        return lines[0]
    return fact


def _separators(row):
    """Tablo satırındaki gerçek hücre ayırıcılarının konumları ('\\|' kaçışlı karakter ayırıcı değildir)."""
    return [i for i, ch in enumerate(row) if ch == '|' and (i == 0 or row[i - 1] != '\\')]


def crosses_cell_boundary(value, row):
    """value kanıt satırında yalnız birden çok hücreye yayılan konumlarda mı geçiyor? Satırdaki her geçişi denenir:
    ayırıcı içermeyen (tek hücre içinde kalan) bir geçiş varsa ihlal yoktur. Tablo satırı değilse ya da value satırda
    yoksa False (o durum başka kontrollerin işidir)."""
    if not isinstance(value, str) or not value or not row_cells(row) or value not in row:
        return False
    seps = _separators(row)
    start = row.find(value)
    while start >= 0:
        end = start + len(value)
        if not any(start <= s < end for s in seps):
            return False
        start = row.find(value, start + 1)
    return True


def _clean_label(text):
    return _cmp(re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', str(text or ''))).strip('* ')


# İstatistik ölçü / karşılaştırma etiketleri (hasta grubu değil). Tek harf ('B') ve değişken adı olabilen kısaltmalar
# (ör. PLR, NLR) bilerek yok: belirsiz etiket taşınmaz.
STAT_LABEL = re.compile(
    r'^(?:sen|spe|sens(?:itivity)?|spec(?:ificity)?|ppv|npv|auc|auroc|roc auc|c[- ]?statistic|accuracy|'
    r'a?or|a?hr|rr|odds ratio|hazard ratio|relative risk|risk ratio|95% ?ci|ci|p|p[- ]?value|exp ?\(b\)(?: ?\(or\))?|'
    r'beta|se|sd)$', re.I)
COMPARISON_LABEL = re.compile(r'^p ?\( ?[\w.+]+ ?(?:-|–|vs\.?) ?[\w.+]+ ?\)$', re.I)


def is_statistic_label(label):
    text = _clean_label(label)
    return bool(text) and bool(STAT_LABEL.match(text) or COMPARISON_LABEL.match(text))


def separate_statistic_label(item):
    """(kayıt, ret nedeni). hasta_grubu bir istatistik ölçü/karşılaştırma etiketiyse (SEN, AUC, OR, P (A-B) ...) grup
    değildir: sonuc_olcutu_tr boşsa oraya taşınır (ya da aynı etiketse hasta_grubu silinir). Değer–etiket ilişkisi
    kaynaktan yeniden doğrulanır: değer satırda tam hücre olmalı ya da etiketle aynı hücrede geçmeli. sonuc_olcutu_tr
    başka bir etiket taşıyorsa ya da ilişki doğrulanamıyorsa tahmin yapılmaz, kayıt reddedilir."""
    group = item.get('hasta_grubu')
    if not is_statistic_label(group):
        return item, None
    row, value = item.get('kanit_cumle'), item.get('deger')
    same_cell = any(_norm(group) in _norm(cell) and _cmp(value) and _cmp(value) in _cmp(cell) for cell in row_cells(row))
    if not (is_whole_cell(value, row) or same_cell):
        return item, f'İstatistik etiketi hasta_grubu ({group!r}); değerle ilişkisi kaynakta doğrulanamadı'
    outcome = item.get('sonuc_olcutu_tr')
    if outcome not in (None, '') and _clean_label(outcome) != _clean_label(group):
        return item, f'İstatistik etiketi hasta_grubu ({group!r}) ve farklı sonuc_olcutu_tr ({outcome!r}); belirsiz'
    item = {k: v for k, v in item.items() if k != 'hasta_grubu'}
    item['sonuc_olcutu_tr'] = outcome if outcome not in (None, '') else group
    item['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + ['hasta_grubu->sonuc_olcutu_tr:istatistik_etiketi']
    return item, None


# ---------------------------------------------------------------------------
# Tablo başlığı (sütun bağlamı) — yalnız kaynak sayfanın tablo yapısından, deterministik
# ---------------------------------------------------------------------------

_SEPARATOR_ROW = re.compile(r'^\|\s*:?-{3,}')
_BOLD_CELL = re.compile(r'^(?:\*\*[^*]+\*\*|<br\s*/?>|<sup>.*?</sup>|\s)+$', re.I)
# Sayısal hücre satırı: '0.002', '<0.001', '2,735.621', '45%', '7.2±1.1', '1.2-6.1' (aralık / GA), '64 (53-74)'.
_NUMERIC_LINE = re.compile(r'^[<>≤≥=]?\s*[-–]?\d[\d.,]*(?:\s*[-–]\s*[-–]?\d[\d.,]*)?(?:\s*[%±]\s*[\d.,]*)?'
                           r'(?:\s*\([^()]*\d[^()]*\))?\s*%?$')
GROUP_HEADER = re.compile(r'\(\s*n\s*=\s*\d|^n\s*=\s*\d', re.I)
P_LABEL = re.compile(r'^p(?:[- ]?value)?$', re.I)
CI_LABEL = re.compile(r'^(?:95\s*%\s*)?ci$', re.I)


def clean_label(text):
    """Grup / sonuç etiketi için biçim temizliği: '**', italik '_..._', <br> (boşluk) ve dipnot HARFİ üst simgesi
    (<sup>d</sup>) atılır, boşluk sadeleşir. Sayısal üst simge (<sup>2</sup>), birim, parantez, n=, güven aralığı ve
    noktalama aynen kalır. Kanıt (kanit_cumle) değişmez."""
    text = FOOTNOTE_SUP.sub(' ', str(text or ''))
    text = re.sub(r'<br\s*/?>', ' ', text, flags=re.I).replace('**', '')
    text = re.sub(r'(?<![\w*])_([^_\n]+)_(?![\w*])', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip()


def _bold_row(cells):
    filled = [c for c in cells if c.strip()]
    return len(filled) >= 2 and all(_BOLD_CELL.match(c) for c in filled)


def column_header(item, page_text):
    """(başlık hücreleri, belirsizlik nedeni). Kaydın kanıt satırına ait başlık satırı, sayfadaki markdown tablosundan:
    - satır sayfada TEKİL olmalı; yukarı doğru aynı tablonun ayırıcı satırı (|---|) bulunur;
    - ayırıcı ile veri satırı arasında veri satırına en yakın başlık satırı seçilir: tamamı kalın satır (çok katmanlı
      başlığın alt katmanı) ya da ilk hücresi boş olup ana başlıkla aynı ilk satırı paylaşan ara başlık (tablo içinde
      başlığın yeniden tanımlanması). Böyle bir satır yoksa ayırıcının hemen üstündeki satır;
    - başlık satırı veri satırıyla aynı sayıda hücre taşımalı. Üst katmanlar (bölünmüş birleşik başlıklar) kullanılmaz."""
    header, _, problem = locate_header(item, page_text)
    return header, problem


def locate_header(item, page_text):
    """(seçilen başlık hücreleri, ayırıcının üstündeki ana başlık hücreleri, belirsizlik nedeni); column_header'ın
    ayrıntılı biçimi. Seçilen başlık ana başlık değilse tabloda bir üst katman vardır."""
    row = str(item.get('kanit_cumle') or '').strip()
    lines = str(page_text or '').splitlines()
    at = [i for i, line in enumerate(lines) if line.strip() == row]
    if len(at) != 1:
        return None, None, 'kanıt satırı sayfada tekil değil'
    sep = at[0] - 1
    while sep >= 0 and lines[sep].strip().startswith('|') and not _SEPARATOR_ROW.match(lines[sep].strip()):
        sep -= 1
    if sep < 1 or not _SEPARATOR_ROW.match(lines[sep].strip()) or not lines[sep - 1].strip().startswith('|'):
        return None, None, 'tablo başlık satırı bulunamadı'
    main = row_cells(lines[sep - 1])
    header = main
    main_first = [_cmp((_cell_lines(c) or [''])[0]) for c in main]
    for j in range(at[0] - 1, sep, -1):
        cells = row_cells(lines[j])
        if _bold_row(cells):
            header = cells
            break
        filled = [i for i, c in enumerate(cells) if c.strip()]
        if (cells and not cells[0].strip() and len(filled) >= 2 and len(cells) == len(main)
                and any(main_first[i] and _cmp((_cell_lines(cells[i]) or [''])[0]) == main_first[i] for i in filled)):
            header = cells
            break
    if len(header) != len(row_cells(row)):
        return None, main, 'başlık ve veri satırı hücre sayısı uyuşmuyor'
    return header, main, None


def is_fragmented_header(cells):
    """Birleşik başlığın PDF dönüşümünde hücrelere bölünmüş olması: komşu iki hücre kelime ortasından ayrılmış
    ('Characteristi' | 'cs', 'Variable Importa' | 'nce') ya da kısaltma bölünmüş ('Predicted A' | 'MI')."""
    labels = [clean_label(c) for c in cells]
    for left, right in zip(labels, labels[1:]):
        if not left or not right:
            continue
        if re.search(r'[^\W\d_]$', left) and re.match(r'[a-z]', right):
            return True
        if re.search(r'(?:^|\s)[A-Z]$', left) and re.match(r'[A-Z]{1,3}(?:\b|$)', right):
            return True
    return False


_QUALIFIER = re.compile(r'^\([^()]{1,30}\)$')


def continuation_qualifier(item, page_text):
    """(devam satırı, niteleyici) ya da None. Kanıt satırının hemen altındaki satır ilk hücresi boş bir devam satırıysa
    ve değerin (tekil) hücre konumunda yalnız parantezli bir niteleyici taşıyorsa (ör. '(EMS)'), değerin bir kısmının
    alt grubu o satırdadır; tek satırlık kanıt bu ilişkiyi doğrulayamaz."""
    row = str(item.get('kanit_cumle') or '').strip()
    lines = str(page_text or '').splitlines()
    at = [i for i, line in enumerate(lines) if line.strip() == row]
    positions = cell_positions(item.get('deger'), row)
    if len(at) != 1 or len(positions) != 1 or at[0] + 1 >= len(lines):
        return None
    below = row_cells(lines[at[0] + 1])
    index = positions[0][0]
    if below and not below[0].strip() and index < len(below) and _QUALIFIER.match(clean_label(below[index])):
        return lines[at[0] + 1].strip(), clean_label(below[index])
    return None


def review_reason(item, page_text):
    """(inceleme nedeni, devam satırı). Parametre–grup–sütun–değer ilişkisi tek satırlık kanıt ve tablo yapısıyla
    güvenilir kurulamayan tablo değeri otomatik aktarım dışında tutulur. Özellikler tek başına hata sayılmaz; her kural
    ilişkinin kurulamadığı durumu tarif eder:
    - bilgi yalnız sayıdan oluşan satır etiketi (sıra / model no): değerin konusu belirsiz;
    - grup ve sonuç ölçütü yok: değerin sütun bağlamı yok;
    - grup değeri satırda tam hücre değil: hücre içinden ayrıştırılmış alt sonuç;
    - '(n=..)' taşımayan grup etiketi, parçalı (bölünmüş) çok katmanlı başlığın alt katmanından: üst başlık bağlamı
      (ör. 'Predicted AMI') kurulamıyor;
    - değerin alt grup niteleyicisi bir sonraki (devam) satırda."""
    if item.get('kanit_turu') != 'tablo' or item.get('deger') in (None, ''):
        return None, None
    cells = row_cells(item.get('kanit_cumle'))
    label = next((c for c in cells if c.strip()), '')
    if re.fullmatch(r'\d+', clean_label(label)) and _cmp(item.get('klinik_bilgi_en')) == _cmp(label):
        return 'satır etiketi yalnız sayı (sıra / model no); değerin konusu belirsiz', None
    group = item.get('hasta_grubu')
    if not group and not item.get('sonuc_olcutu_tr'):
        return 'sütun bağlamı (hasta grubu / sonuç ölçütü) yok', None
    if group and not is_whole_cell(item.get('deger'), item.get('kanit_cumle')):
        return 'grup değeri hücre içinden ayrıştırılmış alt sonuç; grup–değer ilişkisi tablo yapısıyla doğrulanamıyor', None
    if group and not GROUP_HEADER.search(group):
        header, main, _ = locate_header(item, page_text)
        if header is not None and header is not main and is_fragmented_header(main):
            return 'grup etiketi parçalı çok katmanlı başlığın alt katmanında; üst başlık bağlamı kurulamıyor', None
    found = continuation_qualifier(item, page_text)
    if found:
        return f'değerin alt grup niteleyicisi sonraki satırda ({found[1]})', found[0]
    return None, None


def hold_for_review(records, pages, rejected):
    """review_reason'a takılan kayıtlar silinmez: 'inceleme' aşamasıyla, gerekçesi, ham kaydı ve varsa ilgili devam
    satırıyla ret raporuna yazılır; otomatik aktarım kayıtlarından ayrılır."""
    out = []
    for item in records:
        reason, continuation = review_reason(item, pages.get(item.get('sayfa'), ''))
        if reason:
            entry = {'sayfa': item.get('sayfa'), 'asama': 'inceleme', 'neden': reason, 'kayit': item}
            if continuation:
                entry['devam_satiri'] = continuation
            rejected.append(entry)
            continue
        out.append(item)
    return out


def _header_role(label):
    """Doldurulacak alan: '(n=..)' taşıyan başlık hasta grubu, diğerleri (ölçü / sonuç / istatistik) sonuc_olcutu_tr."""
    return 'hasta_grubu' if GROUP_HEADER.search(label) and not is_statistic_label(label) else 'sonuc_olcutu_tr'


def split_stacked_cell(item, page_text):
    """Alt alta yazılmış sayısal hücre (ör. '7.912<br>2,735.621<br>0.002') ve başlığı aynı sayıda satır taşıyorsa
    ('B<br>Exp (B) (OR)<br>p value') satır satır yapılandırılmış kayıtlara ayrılır: p ve CI satırları diğer kayıtların
    p / ci95 alanına bağlanır, kalan her satır ayrı kayıt (deger = satır, sonuc_olcutu_tr = başlık satırı). Hizalama
    doğrulanamazsa ya da mevcut alanlarla çelişirse None (kayıt değişmez)."""
    value = item.get('deger')
    lines = [line.strip() for line in _BR.split(value or '') if line.strip()]
    if len(lines) < 2 or not all(_NUMERIC_LINE.match(line) for line in lines):
        return None
    positions = cell_positions(value, item.get('kanit_cumle'))
    if len(positions) != 1 or positions[0][1] is not None:
        return None                                       # tekil ve tam hücre değil
    header, problem = column_header(item, page_text)
    if problem:
        return None
    labels = [clean_label(line) for line in _BR.split(header[positions[0][0]]) if clean_label(line)]
    if len(labels) != len(lines) or item.get('hasta_grubu'):
        return None
    if item.get('sonuc_olcutu_tr') and _cmp(clean_label(item['sonuc_olcutu_tr'])) != _cmp(' '.join(labels)):
        return None
    stats = {'p': [v for l, v in zip(labels, lines) if P_LABEL.match(l)],
             'ci95': [v for l, v in zip(labels, lines) if CI_LABEL.match(l)]}
    if any(len(v) > 1 for v in stats.values()):
        return None
    attached = {f: v[0] for f, v in stats.items() if v}
    for field, val in attached.items():
        if item.get(field) not in (None, '') and _cmp(item[field]) != _cmp(val):
            return None                                   # mevcut p / CI ile çelişki: tahmin yok
    base = {k: v for k, v in item.items() if k not in ('deger', 'sonuc_olcutu_tr', 'p', 'ci95')}
    out = []
    for label, val in zip(labels, lines):
        if P_LABEL.match(label) or CI_LABEL.match(label):
            continue
        rec = {**base, 'deger': val, 'sonuc_olcutu_tr': label, **attached}
        rec['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + ['tablo_hucresi:alt_alta_hucre_bolundu']
        out.append(rec)
    return out or None


def is_contextless_value(item):
    """Bağlamsız tablo değeri: değer var, hasta grubu / sonuç ölçütü yok, bilgi = satır etiketi (ilk hücre)."""
    cells = row_cells(item.get('kanit_cumle'))
    return (item.get('kanit_turu') == 'tablo' and item.get('deger') not in (None, '') and bool(cells)
            and not item.get('hasta_grubu') and not item.get('sonuc_olcutu_tr')
            and _cmp(item.get('klinik_bilgi_en')) == _cmp(cells[0]))


def fill_column_context(item, page_text):
    """(kayıt, belirsizlik nedeni). Bağlamsız değerin sütun başlığı yalnız tekil ve güvenilir eşleşmede doldurulur:
    değer satırda TEK konumda ve TAM hücre (alt satır değil), ilk (satır etiketi) sütunda değil, başlık satırı
    column_header ile bulunmuş ve o sütunun başlık hücresi dolu. Aksi halde belirsizdir (kayıt aktarım dışı tutulur).
    Birim ayrı alandaysa hücre 'değer birim' biçiminde de aranır (ör. deger='100', unit='mg', hücre '100 mg')."""
    value, unit, row = item.get('deger'), item.get('unit'), item.get('kanit_cumle')
    positions = cell_positions(value, row)
    if not positions and isinstance(unit, str) and unit.strip():
        positions = cell_positions(f'{value} {unit}', row) or cell_positions(f'{value}{unit}', row)
    if len(positions) != 1:
        return item, 'değer satırda tekil bir hücre konumunda değil'
    index, line = positions[0]
    if line is not None:
        return item, 'değer çok satırlı hücrenin bir satırı; başlık hizası doğrulanamadı'
    if index == 0:
        return item, 'değer satır etiketi sütununda'
    header, problem = column_header(item, page_text)
    if problem:
        return item, problem
    label = clean_label(header[index])
    if not re.search(r'[^\W\d_]', label):
        return item, 'sütunun başlık hücresi boş'
    role = _header_role(label)
    item = {**item, role: label}
    item['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + [f'tablo_hucresi:{role}<-sutun_basligi']
    return item, None


def clean_labels(item):
    """hasta_grubu / sonuc_olcutu_tr biçim temizliği (clean_label); değişen alan notu düşülür."""
    changed = {}
    for field in ('hasta_grubu', 'sonuc_olcutu_tr'):
        value = item.get(field)
        if isinstance(value, str) and value.strip() and clean_label(value) != value:
            changed[field] = clean_label(value)
    if not changed:
        return item
    item = {**item, **changed}
    item['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + [f'etiket_bicim:{f}' for f in changed]
    return item


def normalize_table_cell(item):
    """(kayıt, ret nedeni). Tablo hücresi kayıtlarının temsili; kaynak kontrolleri sonra yeniden uygulanır.
    - Klinik bilgi ya da değer satırda yalnız birden çok hücreye yayılarak geçiyorsa (hücre sınırı ihlali) -> ret.
    - hasta_grubu istatistik etiketiyse (separate_statistic_label) grup alanından ayrılır ya da belirsizse ret.
    - Satır etiketinin kendisini değer diye taşıyan, başka bağlam alanı olmayan hücre: yeni bilgi yok -> ret.
    - Sonuç / grup sütunu ve sayısal bağlamı olmayan işlem/yöntem hücresi (bilgi = satır etiketi, değer = tam hücre
      metni): hücre metni klinik_bilgi_en'e taşınır, deger boşaltılır. İlaç/doz hücresinde yalnız değer rakam
      içermiyorsa yapılır; doz, oran ve birim taşıyan ilaç kaydı olduğu gibi kalır."""
    if item.get('kanit_turu') != 'tablo':
        return item, None
    for field in ('klinik_bilgi_en', 'deger'):
        if crosses_cell_boundary(item.get(field), item.get('kanit_cumle')):
            return item, f'{field} tablo hücre sınırını aşıyor (birden çok hücreye yayılıyor)'
    item, reason = separate_statistic_label(item)
    if reason:
        return item, reason
    cells = row_cells(item.get('kanit_cumle'))
    label = cells[0] if cells else ''
    value, fact = item.get('deger'), item.get('klinik_bilgi_en')
    if not label or not isinstance(value, str) or not value.strip() or _cmp(fact) != _cmp(label):
        return item, None
    if any(item.get(f) not in (None, '') for f in CELL_CONTEXT_FIELDS):
        return item, None
    if _cmp(value) == _cmp(label):
        return item, 'Tablo hücresi satır etiketinin kendisi; yeni klinik bilgi yok'
    kind = item.get('bilgi_turu')
    if (kind in INTERVENTION_TYPES or (kind in MEDICATION_TYPES and not re.search(r'\d', value))) \
            and value.strip() in cells:
        item = {k: v for k, v in item.items() if k != 'deger'}
        item['klinik_bilgi_en'] = value.strip()
        item['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + ['tablo_hucresi:deger->klinik_bilgi_en']
    return item, None


# Değer biçimi açıklaması (grup değil): 'Median (Q1-Q3)', 'Mean ± SD', 'n (%)', '(X±SD) (minimum-maximum)', 'IQR' ...
_FORMAT_TOKEN = (r'(?:median|mean|average|sd|s\.d\.|se|sem|iqr|range|x\s*±\s*sd|mean\s*±\s*sd|±\s*sd|n\s*\(\s*%\s*\)|n|%|'
                 r'q1\.?\s*[-–]\s*q3\.?|minimum\s*[-–]\s*maximum|min\s*[-–]\s*max|95\s*%\s*ci)')
FORMAT_LABEL = re.compile(r'^(?:\(?\s*' + _FORMAT_TOKEN + r'(?:\s*[(\[]\s*' + _FORMAT_TOKEN + r'\s*[)\]])?\s*\)?[\s,;/]*)+$',
                          re.I)


def is_format_label(text):
    """Etiketin tamamı değer biçimi açıklaması mı (ör. 'Median (Q1.-Q3.)', 'n (%)', '(X±SD) (minimum-maximum)')."""
    cleaned = clean_label(text)
    return bool(cleaned) and bool(FORMAT_LABEL.match(cleaned))


def split_format_descriptor(label):
    """(grup adı, biçim açıklaması). Çok satırlı başlıkta (<br>) biçim açıklaması olan satırlar ayrılır; yoksa etiketin
    sonundaki biçim parantezleri ('Total (X±SD) (minimum-maximum)'). Grup adı bulunamazsa ('', açıklama)."""
    lines = [line for line in _BR.split(str(label or '')) if clean_label(line)]
    keep = [line for line in lines if not is_format_label(line)]
    fmt = [clean_label(line) for line in lines if is_format_label(line)]
    name = clean_label('<br>'.join(keep))
    # Tek satırlı etiket: sondan en uzun biçim açıklaması (kelime sınırında, '(n=..)' içermeyen) ayrılır.
    words = name.split(' ')
    for cut in range(1, len(words)):
        suffix = ' '.join(words[cut:])
        if '(' not in suffix and len(words[cut:]) == 1:
            continue                                      # çıplak tek kelime ('SD', 'n') grup adının parçası olabilir
        if is_format_label(suffix) and not GROUP_HEADER.search(suffix):
            fmt.insert(0, suffix)
            name = ' '.join(words[:cut])
            break
    return name, ' '.join(fmt)


def separate_format_descriptor(item, page_text):
    """(kayıt, inceleme nedeni). hasta_grubu değer biçimi açıklaması taşıyorsa:
    - yalnız açıklamaysa ('Median (Q1.-Q3.)') grup değildir: gerçek grup, değerin satırdaki TEKİL tam hücre konumunun
      başlık hücresinden (column_header) alınır; başlıktan da açıklama ayrılır. Kurulamıyorsa inceleme.
    - grup adına eklenmiş açıklama ('Study group (n=96)<br>(X±SD) (minimum-maximum)') ayrılır.
    Ayrılan açıklama kaybolmaz: 'hasta_grubu:deger_bicimi=<açıklama>' notu olarak kayıtta kalır (adaptör için)."""
    group = item.get('hasta_grubu')
    if item.get('kanit_turu') != 'tablo' or not isinstance(group, str) or not group.strip():
        return item, None
    name, fmt = split_format_descriptor(group)
    if not fmt:
        return item, None
    if not name:
        positions = cell_positions(item.get('deger'), item.get('kanit_cumle'))
        if len(positions) != 1:
            return item, f'grup etiketi değer biçimi ({clean_label(group)!r}); değer satırda tekil konumda değil'
        header, problem = column_header(item, page_text)
        if problem:
            return item, f'grup etiketi değer biçimi ({clean_label(group)!r}); {problem}'
        name, header_fmt = split_format_descriptor(header[positions[0][0]])
        fmt = ' '.join(f for f in (fmt, header_fmt) if f)
        if not name or is_statistic_label(name) or not re.search(r'[^\W\d_]', name):
            return item, f'grup etiketi değer biçimi ({clean_label(group)!r}); sütun başlığında grup adı yok'
    item = {**item, 'hasta_grubu': name}
    item['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme', [])) + [f'hasta_grubu:deger_bicimi={fmt}']
    return item, None


def normalize_table_cells(candidates, pages, rejected):
    """normalize_table_cell'i uygular ve dönüşen kayıtta kaynak kontrolünü (source_problem) yeniden çalıştırır;
    validate_records ve tekrar ayıklaması bundan sonra aynen uygulanır."""
    out = []
    for item, origin in candidates:
        info = {k: v for k, v in origin.items() if k != 'tur'}
        page_text = pages.get(item.get('sayfa'), '')
        new, reason = normalize_table_cell(item)
        if reason:
            rejected.append({'sayfa': item.get('sayfa'), **info, 'neden': reason, 'asama': 'tablo_hucresi', 'kayit': item})
            continue
        pieces = (split_stacked_cell(new, page_text) if new.get('kanit_turu') == 'tablo' else None) or [new]
        for piece in pieces:
            piece, unclear = separate_format_descriptor(piece, page_text)
            if unclear:
                rejected.append({'sayfa': piece.get('sayfa'), **info, 'asama': 'inceleme',
                                 'neden': f'Hasta grubu belirlenemedi: {unclear}', 'kayit': piece})
                continue
            if is_contextless_value(piece):
                piece, unclear = fill_column_context(piece, page_text)
                if unclear:
                    # Belirsiz: otomatik aktarım kayıtlarından ayrılır, ham kanıtıyla ret raporunda korunur.
                    rejected.append({'sayfa': piece.get('sayfa'), **info, 'asama': 'tablo_baglami_belirsiz',
                                     'neden': f'Sütun bağlamı belirlenemedi: {unclear}', 'kayit': piece})
                    continue
            piece = clean_labels(piece)
            problem = source_problem(piece, page_text) if piece is not item else None
            if problem:
                rejected.append({'sayfa': piece.get('sayfa'), **info, 'neden': problem, 'asama': 'tablo_hucresi',
                                 'kayit': piece})
                continue
            out.append((piece, origin))
    return out


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
    Karşılanmayan eski kayıt (ör. tablo görevinin atladığı RR hücresi, tedavi sütunu) korunur.
    Karşılaştırma _cmp ile (kalın / <br> / dipnot harfi farkı yok sayılır). Eski kayıt sütun bilgisi (sonuç ölçütü /
    hasta grubu) taşıyorsa yeni kayıtta aynı sütun olmalı: farklı sütundaki aynı değer onu karşılamaz."""
    fact, value = _cmp(old.get('klinik_bilgi_en')), _cmp(old.get('deger'))
    for new in newer:
        if any(old.get(f) and _cmp(old.get(f)) != _cmp(new.get(f)) for f in COLUMN_FIELDS):
            continue
        # Eski kaydın taşıdığı istatistik / birim bilgisi yeni kayıtta yoksa değiştirme bilgi kaybettirir.
        if any(old.get(f) not in (None, '') and _cmp(old.get(f)) != _cmp(new.get(f)) for f in CARRIED_FIELDS):
            continue
        if fact and _cmp(new.get('klinik_bilgi_en')) == fact and _cmp(new.get('deger')) == value:
            return True
        # Hücre değerini bilgi diye yazmış eski kayıt ('Decreased') yalnız sütunu belli yeni hücreyle karşılanır;
        # sütunsuz hücre (ör. öneri/tedavi metni sütunu) eski kaydın bilgi-tür ilişkisini taşımaz, eski kayıt kalır.
        if (not value and fact and fact == _cmp(new.get('deger'))
                and any(new.get(f) for f in COLUMN_FIELDS)):
            return True
    return False


COLUMN_FIELDS = ('sonuc_olcutu_tr', 'hasta_grubu')
CARRIED_FIELDS = ('p', 'unit', 'ci95', 'etki_turu', 'comparator')
# Normalize tekrar anahtarı: satır + sütun + değer + klinik bağlam. Biri farklıysa (ör. başka sonuç sütunu) ayrı kayıt.
TABLE_KEY_FIELDS = ('klinik_bilgi_en', 'deger', 'unit', 'p', 'ci95', 'etki_turu', 'comparator') + COLUMN_FIELDS


def drop_table_duplicates(records, rejected):
    """Yalnız biçimce farklı (kalın, <br>, dipnot harfi) aynı tablo hücresi kayıtlarından ilki kalır; diğerleri ret
    raporuna 'tablo_tekrari' olarak yazılır. Anahtar: sayfa, satır, tür, bağlam ve TABLE_KEY_FIELDS."""
    seen, out = set(), []
    for item in records:
        if item.get('kanit_turu') != 'tablo':
            out.append(item)
            continue
        key = ((item.get('sayfa'), _cmp(item.get('kanit_cumle')), item.get('bilgi_turu'), item.get('baglam'))
               + tuple(_cmp(item.get(f)) for f in TABLE_KEY_FIELDS))
        if key in seen:
            rejected.append({'sayfa': item.get('sayfa'), 'neden': 'Aynı tablo hücresi (yalnız biçim farkı) daha önce geldi',
                             'asama': 'tablo_tekrari', 'kayit': item})
            continue
        seen.add(key)
        out.append(item)
    return out


def cell_positions(value, row):
    """value'nun satırda tam hücre olarak geçtiği konumlar: (hücre, None); hücrenin <br> satırı olarak geçtiği
    konumlar: (hücre, satır). Alt alta yazılmış farklı satırlar (ör. '152<br>37') ayrı konumdur."""
    target = _cmp(value)
    if not target:
        return []
    found = []
    for i, cell in enumerate(row_cells(row)):
        if _cmp(cell) == target:
            found.append((i, None))
            continue
        found.extend((i, n) for n, line in enumerate(_cell_lines(cell)) if _cmp(line) == target)
    return found


def _column_label(item):
    return _clean_label(item.get('hasta_grubu') or item.get('sonuc_olcutu_tr'))


def _same_column(a, b):
    """Normalize sütun etiketi uyumu: eşit, biri diğerinin başlık + alt başlık satırı biçimi ('total (n=114)' ~
    'total (n=114) median (q1-q3)') ya da biri sütun taşımıyor."""
    a, b = _column_label(a), _column_label(b)
    return not a or not b or a == b or a.startswith(b + ' ') or b.startswith(a + ' ')


def _same_row_variable(a, b):
    """İki kaydın klinik bilgisi aynı satır değişkenini mi anlatıyor (biri diğerini kapsıyor, ör. 'Hypertension' ~
    'Hypertension n (%)'); farklı değişkenler (Male / Female) aynı hücre sayılmaz."""
    x, y = _cmp(a.get('klinik_bilgi_en')), _cmp(b.get('klinik_bilgi_en'))
    if not (x and y):
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    # Kelime sınırında kapsama: 'male' 'female' içinde sayılmaz.
    return re.search(r'(?<!\w)' + re.escape(short) + r'(?!\w)', long_) is not None


GENERIC_TYPES = ('olcum', 'diger')


def _keep_rank(item, index):
    """Aynı hücrenin kayıtlarından hangisi kalır: özgül klinik türü olan (tani / ilac / tedavi ...; SQL diseases ve
    treatments görünümleri tablo satırlarını bu türlerle bekler) genel türe (olcum / diger) tercih edilir; sonra sütun
    etiketi olan, temiz (biçimsiz, kısa) etiketli, daha çok yapılandırılmış alanı (p, birim ...) olan; eşitse çıkarım
    sırasında önce gelen."""
    label = item.get('hasta_grubu') or item.get('sonuc_olcutu_tr') or ''
    structured = sum(item.get(f) not in (None, '') for f in ('p', 'unit', 'ci95', 'etki_turu', 'comparator'))
    return (item.get('bilgi_turu') in GENERIC_TYPES, not label, bool(re.search(r'\*\*|<br', label, re.I)),
            len(_column_label(item)), -structured, index)


def drop_cell_duplicates(records, rejected):
    """Aynı tablo hücresinden üretilmiş tekrarlar (ör. sayfa ve tablo görevinin aynı hücreyi farklı etiketle vermesi).
    Hücre kimliği: aynı sayfa + satır, değer satırda TEK bir hücre konumunda tam hücre olarak geçiyor (eşit değer
    birden çok hücredeyse kimlik belirsiz -> ayıklanmaz), aynı satır değişkeni ve uyumlu normalize sütun etiketi.
    Ayıklanan kayıt 'hucre_tekrari' olarak ret raporuna yazılır."""
    rows = defaultdict(list)
    for index, item in enumerate(records):
        if item.get('kanit_turu') == 'tablo' and item.get('deger') not in (None, ''):
            positions = cell_positions(item.get('deger'), item.get('kanit_cumle'))
            if len(positions) == 1:
                rows[(item.get('sayfa'), _cmp(item.get('kanit_cumle')), positions[0])].append(index)
    drop = set()
    for indexes in rows.values():
        kept = []
        for index in sorted(indexes, key=lambda i: _keep_rank(records[i], i)):
            item = records[index]
            if any(_same_row_variable(item, records[k]) and _same_column(item, records[k]) for k in kept):
                drop.add(index)
                continue
            kept.append(index)
    for index in sorted(drop):
        rejected.append({'sayfa': records[index].get('sayfa'), 'asama': 'hucre_tekrari', 'kayit': records[index],
                         'neden': 'Aynı tablo hücresi (aynı satır, tek hücre konumu, aynı değişken ve sütun) daha önce geldi'})
    return [item for index, item in enumerate(records) if index not in drop]


P_COLUMN = re.compile(r'^p(?:[- ]?value)?$', re.I)
INDEPENDENT_STAT_FIELDS = ('hasta_grubu', 'unit', 'ci95', 'etki_turu', 'comparator')


def drop_p_duplicates(records, rejected):
    """p sütunu hücresinin ayrı kaydı, aynı satırın grup kayıtlarında p olarak zaten varsa tekrar sayılır. Koşullar:
    kayıtta grup ve bağımsız istatistik alanı yok, sütunu yok ya da p sütunu, değeri satırda tam hücre ve aynı satırda
    aynı değişkenin en az bir grup kaydının p değeri bu değere eşit. Grup kaydı p taşımıyorsa (bağımsız istatistik)
    ya da değer başka bir hücrede ise kayıt kalır."""
    group_p = defaultdict(set)
    for item in records:
        if item.get('kanit_turu') == 'tablo' and item.get('hasta_grubu') and item.get('p'):
            group_p[(item.get('sayfa'), _cmp(item.get('kanit_cumle')))].add((_cmp(item.get('p')), item.get('klinik_bilgi_en')))
    out = []
    for item in records:
        key = (item.get('sayfa'), _cmp(item.get('kanit_cumle')))
        value = _cmp(item.get('deger'))
        column = _clean_label(item.get('sonuc_olcutu_tr'))
        duplicate = (item.get('kanit_turu') == 'tablo' and value and key in group_p
                     and not any(item.get(f) not in (None, '') for f in INDEPENDENT_STAT_FIELDS)
                     and (not column or P_COLUMN.match(column))
                     and is_whole_cell(item.get('deger'), item.get('kanit_cumle'))
                     and any(p == value and _same_row_variable(item, {'klinik_bilgi_en': fact}) for p, fact in group_p[key]))
        if duplicate:
            rejected.append({'sayfa': item.get('sayfa'), 'asama': 'p_tekrari', 'kayit': item,
                             'neden': 'p değeri aynı satırın grup kayıtlarında zaten var (p sütunu hücresi)'})
            continue
        out.append(item)
    return out


def _model_of(item):
    return item.get('tablo_modeli') or {}


def _same_model_table_row(a, b):
    return (a.get('sayfa') == b.get('sayfa') and _cmp(a.get('kanit_cumle')) == _cmp(b.get('kanit_cumle'))
            and _model_of(a).get('tablo_no') == _model_of(b).get('tablo_no'))


def drop_aligned_duplicates(records, rejected):
    """Tablo modeline hizalanmış kayıtlarda (src.rag_tablo_hizalama) eşdeğerliği DOĞRULANMIŞ tekrarlar:
    - etki_p_tekrari: p sütunu hücresinin ayrı kaydı; aynı model satırında aynı p sütununa sahip (etki / grup) kaydın
      p'si bu değerle aynı. Sahip kayıt yoksa ya da p farklıysa bağımsız p bulgusu kalır.
    - bolunmemis_tekrar: alt satırları tek kayıtta taşıyan hücre ('Male<br>Female' / '152<br>37'); HER alt satır için
      aynı sütunda, aynı değerde ve (kayıtta varsa) aynı grup ve p ile bölünmüş kayıt var. Eşdeğerlik kurulamazsa kayıt
      silinmez, incelemeye ayrılır.
    - satir_etiketi: değersiz, yalnız satır adı olan kayıt; aynı model satırının en az bir değer kaydı var (satır adı
      orada sonuç ölçütü). Müdahale / ilaç / doz türleri kalır.
    Farklı değer, grup ya da istatistik türü taşıyan kayıtlar tekrar sayılmaz."""
    from src.rag_tablo_hizalama import HOLD_STAGE, _c
    matched = [r for r in records if _model_of(r).get('durum') == 'eslesti']
    out = []
    for item in records:
        model = _model_of(item)
        state = model.get('durum')
        if state == 'p_hucresi':
            owners = [o for o in matched if _same_model_table_row(o, item)
                      and _model_of(o).get('satir_no') == model.get('satir_no')
                      and _model_of(o).get('p_sutun') == model.get('p_sutun') and _c(o.get('p')) == _c(item.get('deger'))]
            if owners and not any(item.get(f) not in (None, '') for f in ('ci95', 'comparator', 'hasta_grubu')):
                rejected.append({'sayfa': item.get('sayfa'), 'asama': 'etki_p_tekrari', 'kayit': item,
                                 'neden': f"p değeri ({item.get('deger')}) aynı satırın {model.get('p_sutun')!r} sütununa "
                                          f"sahip kaydında p olarak var"})
                continue
        elif state == 'bolunmemis':
            missing = None
            if not (model.get('sutun') and model.get('hucreler')):
                missing = 'alt satırların sütunu tablo modelinde tekil belirlenemedi'
            else:
                for number, cell in zip(model['satirlar'], model['hucreler']):
                    peers = [o for o in matched if _same_model_table_row(o, item)
                             and _model_of(o).get('satir_no') == number and _model_of(o).get('sutun') == model['sutun']
                             and _c(o.get('deger')) == _c(cell)
                             and (not item.get('hasta_grubu') or _c(o.get('hasta_grubu')) == _c(item.get('hasta_grubu')))
                             and (not item.get('p') or not o.get('p') or _c(o.get('p')) == _c(item.get('p')))]
                    if not peers:
                        missing = f'alt satır {number} ({cell!r}) için eşdeğer bölünmüş kayıt yok'
                        break
            if missing:
                rejected.append({'sayfa': item.get('sayfa'), 'asama': HOLD_STAGE, 'kayit': item,
                                 'neden': f'bölünmemiş alt satır hücresi; {missing}'})
            else:
                rejected.append({'sayfa': item.get('sayfa'), 'asama': 'bolunmemis_tekrar', 'kayit': item,
                                 'neden': 'alt satırları tek kayıtta taşıyan hücre; her alt satır aynı sütun / değer / '
                                          'grup / p ile bölünmüş kayıtlarda var'})
            continue
        elif state == 'satir_etiketi' and item.get('bilgi_turu') not in INTERVENTION_TYPES + MEDICATION_TYPES:
            if any(_same_model_table_row(o, item) and _model_of(o).get('satir_no') == model.get('satir_no')
                   for o in matched):
                rejected.append({'sayfa': item.get('sayfa'), 'asama': 'satir_etiketi', 'kayit': item,
                                 'neden': 'değersiz satır adı kaydı; aynı satırın değer kayıtlarında sonuç ölçütü olarak var'})
                continue
        out.append(item)
    return out


SAME_FACT_FIELDS = ('bilgi_turu', 'baglam')
STRUCTURED_COMPARE = ('deger', 'unit', 'p', 'ci95', 'etki_turu', 'comparator', 'hasta_grubu', 'sonuc_olcutu_tr')


def _same_sentence(single, boundary):
    """Tek sayfa kaydının kanıtı, sınır kaydının aynı sayfadaki parçasıyla aynı cümle parçası mı (biri diğerini içerir)."""
    quote = _cmp(single.get('kanit_cumle'))
    for part in boundary.get('kanit_parcalari') or []:
        text = _cmp(part.get('metin'))
        if part.get('sayfa') == single.get('sayfa') and quote and text and (quote in text or text in quote):
            return True
    return False


def drop_boundary_duplicates(records, rejected):
    """Sınır kaydının (tam cümle, iki sayfa) karşıladığı tek sayfa kaydı çıkarılır ('sinir_tekrari'). Gerçek tekrar:
    sayfa sınır çiftinde, aynı klinik bilgi (_cmp), aynı tür ve bağlam, aynı yapılandırılmış alanlar (grup, değer,
    birim, sonuç ölçütü ...) ve kanıtı sınır kaydının o sayfadaki parçasıyla aynı cümle. Biri farklıysa iki kayıt kalır."""
    boundary = [r for r in records if r.get('kanit_parcalari')]
    if not boundary:
        return records
    out = []
    for item in records:
        duplicate = not item.get('kanit_parcalari') and any(
            item.get('sayfa') in (b.get('sayfa'), b.get('sayfa_bitis'))
            and _cmp(item.get('klinik_bilgi_en')) == _cmp(b.get('klinik_bilgi_en'))
            and all(item.get(f) == b.get(f) for f in SAME_FACT_FIELDS)
            and all(_cmp(item.get(f)) == _cmp(b.get(f)) for f in STRUCTURED_COMPARE)
            and _same_sentence(item, b)
            for b in boundary)
        if duplicate:
            rejected.append({'sayfa': item.get('sayfa'), 'neden': 'Aynı bilgi sınır kaydında tam cümleyle var',
                             'asama': 'sinir_tekrari', 'kayit': item})
            continue
        out.append(item)
    return out


def join_evidence(records):
    """Doğrulanmış çok parçalı kayıtta kanit_cumle = parçaların okuma sırasıyla tek boşlukla birleşimi (validate_records
    / fact_in_parts'ın sınır araması ile aynı birleşim). Parçalar (kanit_parcalari) ham kaynak olarak aynen kalır.
    Not: pilot_clinical.import_records kanit_cumle = ilk parça bekler; SQL aktarımında kanit_parcalari[0] kullanılmalı."""
    out = []
    for item in records:
        parts = item.get('kanit_parcalari')
        if parts:
            item = {**item, 'kanit_cumle': ' '.join(p['metin'].strip() for p in parts)}
        out.append(item)
    return out


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


def measurement_key(item):
    """Grup karşılaştırmasının birimi: (tablo satırı, satır değişkeni). Aynı satırda <br> ile alt alta yazılmış farklı
    değişkenler (ör. 'Male<br>Female') ayrı karşılaştırmadır; farklı parametreler aynı karşılaştırmaya girmez."""
    return row_key(item) + (_cmp(row_parameter(item)),)


def is_group_cell(item):
    """Gerçek grup hücresi: tablo kaydı, hasta grubu var ve değeri satırda tam bir hücre / hücre satırı (hücre içinden
    ayrıştırılmış alt sonuç değil)."""
    return (item.get('kanit_turu') == 'tablo' and bool(item.get('hasta_grubu'))
            and is_whole_cell(item.get('deger'), item.get('kanit_cumle')))


def grouped_rows(records):
    """Grup karşılaştırması olan (satır, değişken) anahtarları (measurement_key): aynı değişken için en az iki farklı
    grup hücresi (is_group_cell) ve en az biri Total dışı. v_sunum_taban.satir_grup_karsilastirmasi'nin daraltılmış
    karşılığı: SQL satırı, satırda herhangi bir hasta_grubu (Total ya da hücre içi alt sonuç dahil) varsa işaretler
    ve değişkene bölmez; SQL dosyaları bu çalışmada değiştirilmedi."""
    groups = defaultdict(set)
    for r in records:
        if is_group_cell(r):
            groups[measurement_key(r)].add((_cmp(r['hasta_grubu']), is_total(r)))
    return {key for key, found in groups.items() if len(found) >= 2 and any(not total for _, total in found)}


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
        # Hücre içinden ayrıştırılmış grup alt sonucu geniş karşılaştırmaya girmez (yalnız measurements_by_group).
        if measurement_key(item) in grouped and (not item.get('hasta_grubu') or is_group_cell(item)):
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
            # Parametre: kaydın değil satırın doğrulanmış değişkeni (row_parameter); satır+değişken başına tek satır.
            row = wide.setdefault(measurement_key(item), {
                'pdf': pdf_name, 'sayfa': item.get('sayfa'), 'kategori': 'measurements',
                'klinik_bilgi': row_parameter(item), 'gruplar': [], 'total_group': None,
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
    model = model or default_model()
    base = Path(out_root) / Path(doc['filename']).stem
    cache = Cache(base / 'llm_onbellek.jsonl')
    # Önbellekte olmayan görev sayısı (API yok): toplu işlemede bütçe planı ve kaldığı yerden devam için.
    meta['bekleyen_cagri'] = sum(cache.get(Cache.key(model, system, payload)) is None
                                 for system, payload in (task_request(t) for t in tasks))
    if plan_only:
        return meta, None
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
