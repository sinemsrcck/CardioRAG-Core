
"""
klinik_bulgular satırlarının kaynağa karşı denetimi.

    ctx = load_validation_context(conn)          # bir kez
    ok, hata_turu, aciklama = validate_row(row, ctx)

validate_row(row, conn_or_cache) ikinci argüman olarak bir bağlantı da kabul
eder (bu durumda bağlam her çağrıda yeniden yüklenir; çok satır için ctx verin).

Kontroller (ilk başarısız olan döner):
  deger_kumesi_disi   mudahale_turu / etki_turu / yon / outcome_type /
                      kanit_turu / birlestirme hocanın/izinli değer kümesinde değil
  icd10_yok           icd10 dolu ama icd10_who'da yok
  yanlis_sayfa        kanıt cümlesi belgede birebir var ama belirtilen sayfa(lar)da değil
  kanit_birebir_degil kanıt cümlesi belgede birebir yok (ya da chunk_id pdf_chunks'ta yok)
  kanitta_olmayan_sayi alanlardaki bir sayı kanıt cümlesinde geçmiyor

Kanıt kontrolü makaleler.tam_metin'e (document_id dolu satırlar) göre yapılır:
  - metin : kanıt, kanit_sayfa..kanit_sayfa_bitis sayfalarının (sayfa işaretleri
            çıkarılmış, markdown temizlenmiş, boşluklar normalize) metninde birebir;
            birlestirme='ara_blok' ise parçalar bu metinde sırayla geçmeli.
  - tablo : kanıt, bu sayfalardaki bir markdown tablo satırıyla hücre hücre aynı.
"""

import re
from dataclasses import dataclass, field

from src.clinical_candidates import iter_pages, strip_markdown


MUDAHALE_TURU = {"tedavi", "tanı testi", "arka plan tedavisi"}
# Hocanın sonuclar tablosundaki değerler (boş = belirtilmemiş).
ETKI_TURU = {"", "OR", "RR", "HR", "IRR", "AUC / C-istatistigi", "oran / yuzde",
             "ortalama fark", "diger", "sayisal deger yok"}
YON = {"", "olumlu", "olumsuz", "notr / anlamsiz", "belirsiz"}
OUTCOME_TYPE = {"", "etkinlik", "güvenlik", "tanısal performans", "tanı kriteri",
                "prognostik değer", "öneri", "uygulama oranı", "çalışma popülasyonu"}
KANIT_TURU = {"metin", "tablo"}
BIRLESTIRME = {"", "sayfa", "ara_blok"}

HATA_TURLERI = ("kanit_birebir_degil", "yanlis_sayfa", "kanitta_olmayan_sayi",
                "deger_kumesi_disi", "icd10_yok")

# Sayı kontrolü yapılan serbest metin alanları.
NUMBER_CHECKED = ("hasta_grubu", "hasta_sayisi", "mudahale_tr", "mudahale_en",
                  "comparator", "doz", "uygulama_yolu", "sure", "sonuc_olcutu_tr",
                  "deger", "ci95", "p", "study_name")


def _as_text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def _str(value):
    return "" if value is None else str(value)


def norm_ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


def prose(text):
    """Markdown temizlenmiş, boşlukları normalize edilmiş metin."""
    return norm_ws(strip_markdown(text or ""))


def table_rows(text):
    """Metindeki markdown tablo satırları (aday üreticisiyle aynı biçimde)."""
    rows = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and not re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", stripped):
            cells = [strip_markdown(c) for c in stripped.strip("|").split("|")]
            row = " | ".join(c for c in cells if c)
            if row:
                rows.add(row)
    return rows


def numbers_in(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", _str(text)))


def sequential_parts_match(needle, hay, max_parts=4):
    """needle, hay içinde sırayla geçen en fazla max_parts parçanın birleşimi mi?"""
    position = 0
    for _ in range(max_parts):
        if not needle:
            return True
        lo, hi = 0, len(needle)
        while lo < hi:  # hay[position:] içinde geçen en uzun önek
            mid = (lo + hi + 1) // 2
            if hay.find(needle[:mid], position) >= 0:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            return False
        position = hay.find(needle[:lo], position) + lo
        needle = needle[lo:].lstrip()
    return not needle


# ---------------------------------------------------------------------------
# Bağlam (veritabanından bir kez okunur)
# ---------------------------------------------------------------------------

@dataclass
class ValidationContext:
    pages: dict = field(default_factory=dict)        # document_id -> {sayfa: ham sayfa metni}
    makale_by_doc: dict = field(default_factory=dict)  # document_id -> makale_id
    icd_codes: set = field(default_factory=set)
    chunks: dict = field(default_factory=dict)       # (document_id, sayfa) -> [(chunk_id, text)]
    chunk_ids: set = field(default_factory=set)


def load_validation_context(conn):
    """makaleler.tam_metin, icd10_who ve güncel pdf_chunks'ı okur (salt okuma)."""
    ctx = ValidationContext()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT document_id, makale_id, tam_metin FROM makaleler "
        "WHERE document_id IS NOT NULL"
    )
    for document_id, makale_id, full_text in cursor.fetchall():
        document_id = _as_text(document_id)
        ctx.makale_by_doc[document_id] = _as_text(makale_id)
        ctx.pages[document_id] = dict(iter_pages(_as_text(full_text) or ""))

    cursor.execute("SELECT kod FROM icd10_who")
    ctx.icd_codes = {_as_text(r[0]) for r in cursor.fetchall()}

    cursor.execute(
        "SELECT c.chunk_id, c.document_id, c.page_number, c.text "
        "FROM pdf_chunks AS c JOIN pdf_belgeleri AS b "
        "  ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
        "WHERE b.durum = 'tamamlandi'"
    )
    for chunk_id, document_id, page, text in cursor.fetchall():
        chunk_id = _as_text(chunk_id)
        ctx.chunks.setdefault((_as_text(document_id), page), []).append((chunk_id, _as_text(text)))
        ctx.chunk_ids.add(chunk_id)
    cursor.close()
    return ctx


def _context(conn_or_cache):
    if isinstance(conn_or_cache, ValidationContext):
        return conn_or_cache
    return load_validation_context(conn_or_cache)


# ---------------------------------------------------------------------------
# Kanıt arama
# ---------------------------------------------------------------------------

def _page_span_text(ctx, document_id, start, end):
    pages = ctx.pages.get(document_id, {})
    if any(p not in pages for p in range(start, end + 1)):
        return None
    return "\n".join(pages[p] for p in range(start, end + 1))


def evidence_in_text(evidence, kanit_turu, birlestirme, text):
    if text is None:
        return False
    if kanit_turu == "tablo":
        return evidence in table_rows(text)
    hay = prose(text)
    needle = norm_ws(evidence)
    if needle in hay:
        return True
    return birlestirme == "ara_blok" and sequential_parts_match(needle, hay)


def find_chunk_id(row, conn_or_cache):
    """Kanıtın tamamen içinde geçtiği güncel chunk (kanıt sayfası/sayfalarında)."""
    ctx = _context(conn_or_cache)
    start = int(row["kanit_sayfa"])
    end = int(row.get("kanit_sayfa_bitis") or start)
    for page in range(start, end + 1):
        for chunk_id, text in ctx.chunks.get((row["document_id"], page), []):
            if evidence_in_text(row["kanit_cumle"], row.get("kanit_turu") or "metin", "", text):
                return chunk_id
    return None


# ---------------------------------------------------------------------------
# Denetim
# ---------------------------------------------------------------------------

def validate_row(row, conn_or_cache):
    """
    Döndürür: (gecti_mi, hata_turu, aciklama). Geçerse (True, None, None).
    row: klinik_bulgular sütun adlarıyla sözlük; en az document_id, kanit_sayfa,
    kanit_cumle, mudahale_turu gerekir.
    """
    ctx = _context(conn_or_cache)

    # 1) Değer kümeleri
    for name, allowed, required in (
        ("mudahale_turu", MUDAHALE_TURU, True),
        ("etki_turu", ETKI_TURU, False),
        ("yon", YON, False),
        ("outcome_type", OUTCOME_TYPE, False),
        ("kanit_turu", KANIT_TURU, False),
        ("birlestirme", BIRLESTIRME, False),
    ):
        value = _str(row.get(name))
        if (required and not value) or (value and value not in allowed):
            return False, "deger_kumesi_disi", f"{name}='{value}' izinli değerlerden değil"
    hasta_sayisi = _str(row.get("hasta_sayisi")).strip()
    if hasta_sayisi and not hasta_sayisi.isdigit():
        return False, "deger_kumesi_disi", f"hasta_sayisi='{hasta_sayisi}' tam sayı değil"

    # 2) ICD-10
    icd10 = _str(row.get("icd10"))
    if icd10 and icd10 not in ctx.icd_codes:
        return False, "icd10_yok", f"icd10='{icd10}' icd10_who tablosunda yok"

    # 3) Kanıt
    document_id = row.get("document_id")
    evidence = _str(row.get("kanit_cumle"))
    if document_id not in ctx.pages:
        return False, "kanit_birebir_degil", "document_id için tam_metin yok"
    if not evidence.strip():
        return False, "kanit_birebir_degil", "kanit_cumle boş"
    try:
        start = int(row.get("kanit_sayfa"))
        end = int(row.get("kanit_sayfa_bitis") or start)
    except (TypeError, ValueError):
        return False, "yanlis_sayfa", "kanit_sayfa geçersiz"
    if end < start:
        return False, "yanlis_sayfa", "kanit_sayfa_bitis < kanit_sayfa"

    kanit_turu = _str(row.get("kanit_turu")) or "metin"
    birlestirme = _str(row.get("birlestirme"))
    if not evidence_in_text(evidence, kanit_turu, birlestirme,
                            _page_span_text(ctx, document_id, start, end)):
        pages = sorted(ctx.pages[document_id])
        elsewhere = [p for p in pages
                     if evidence_in_text(evidence, kanit_turu, birlestirme,
                                         _page_span_text(ctx, document_id, p, min(p + 1, pages[-1])))]
        if elsewhere:
            return False, "yanlis_sayfa", (
                f"kanıt s.{start}{'-' + str(end) if end != start else ''}'de yok; "
                f"belgede s.{elsewhere[0]} civarında geçiyor")
        return False, "kanit_birebir_degil", f"kanıt belgede (s.{start}) birebir bulunamadı"

    chunk_id = row.get("chunk_id")
    if chunk_id and chunk_id not in ctx.chunk_ids:
        return False, "kanit_birebir_degil", "chunk_id güncel pdf_chunks'ta yok"

    # 4) Sayılar
    evidence_numbers = numbers_in(evidence)
    for name in NUMBER_CHECKED:
        extra = numbers_in(row.get(name)) - evidence_numbers
        if extra:
            return False, "kanitta_olmayan_sayi", f"{name}: kanıtta olmayan sayı {sorted(extra)}"

    return True, None, None
