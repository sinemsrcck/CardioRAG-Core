
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

SQL kaydından önce makale/belge ilişkisi ve güncel chunk zorunludur. Alıntı,
chunk'ın gerçek metninde yalnızca boşluk normalizasyonuyla bulunmalıdır.
Chunk kimliği yoksa yalnızca tek bir eşleşmeden çözümlenir. Sayfa aralığı
tek sayfalı chunk'ın fiziksel sayfasıyla aynı olmalıdır; çok sayfalı adaylar
ayrı kaynaklar olmadan kaydedilmez.

Ek kanıt kontrolü makaleler.tam_metin'e (document_id dolu satırlar) göre yapılır:
  - metin : kanıt, kanit_sayfa..kanit_sayfa_bitis sayfalarının (sayfa işaretleri
            çıkarılmış, markdown temizlenmiş, boşluklar normalize) metninde birebir;
            birlestirme='ara_blok' ise parçalar bu metinde sırayla geçmeli.
  - tablo : kanıt, bu sayfalardaki bir markdown tablo satırıyla hücre hücre aynı.
"""

import re
import json
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
    chunk_versions: dict = field(default_factory=dict)  # chunk_id -> processing_version
    active_versions: dict = field(default_factory=dict)  # document_id -> processing_version


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
        if document_id in ctx.makale_by_doc:
            cursor.close()
            raise ValueError("ambiguous article/document relationship")
        ctx.makale_by_doc[document_id] = _as_text(makale_id)
        ctx.pages[document_id] = dict(iter_pages(_as_text(full_text) or ""))

    cursor.execute("SELECT kod FROM icd10_who")
    ctx.icd_codes = {_as_text(r[0]) for r in cursor.fetchall()}

    cursor.execute(
        "SELECT c.chunk_id, c.document_id, c.page_number, c.text, c.processing_version "
        "FROM pdf_chunks AS c JOIN pdf_belgeleri AS b "
        "  ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
        "WHERE b.durum = 'tamamlandi'"
    )
    for chunk_id, document_id, page, text, version in cursor.fetchall():
        chunk_id = _as_text(chunk_id)
        document_id = _as_text(document_id)
        version = _as_text(version)
        ctx.chunks.setdefault((_as_text(document_id), page), []).append((chunk_id, _as_text(text)))
        ctx.chunk_ids.add(chunk_id)
        ctx.chunk_versions[chunk_id] = version
        ctx.active_versions[document_id] = version
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
        return (any(norm_ws(evidence) == norm_ws(line) for line in text.splitlines()
                    if line.strip().startswith("|")) or evidence in table_rows(text))
    hay = prose(text)
    needle = norm_ws(evidence)
    if needle in hay:
        return True
    return birlestirme == "ara_blok" and sequential_parts_match(needle, hay)


def find_chunk_id(row, conn_or_cache):
    """Return a unique current chunk containing the whitespace-normalized quote."""
    ctx = _context(conn_or_cache)
    matches = _source_chunks(row, ctx)
    return matches[0] if len(matches) == 1 else None


def _source_chunks(row, ctx):
    """Single-page chunks cannot substantiate a wider claimed page range."""
    start = row.get("kanit_sayfa")
    end = row.get("kanit_sayfa_bitis")
    end = start if end in (None, "") else end
    if type(start) is not int or type(end) is not int or start < 1 or end != start:
        return []
    doc = row.get("document_id")
    version = ctx.active_versions.get(doc)
    quote = norm_ws(row.get("kanit_cumle"))
    if not version or not quote:
        return []
    return list(dict.fromkeys(
        cid for cid, text in ctx.chunks.get((doc, start), [])
        if cid in ctx.chunk_ids and ctx.chunk_versions.get(cid) == version
        and quote in norm_ws(text)
    ))


def validate_source(row, conn_or_cache):
    """Shared SQL-write boundary: document/article/page/current chunk/quote."""
    ctx = _context(conn_or_cache)
    doc = row.get("document_id")
    article = ctx.makale_by_doc.get(doc)
    if not article or row.get("makale_id") not in (None, "", article):
        return False, "kanit_birebir_degil", "article/document relationship mismatch"
    start = row.get("kanit_sayfa")
    end = row.get("kanit_sayfa_bitis")
    end = start if end in (None, "") else end
    if type(start) is not int or type(end) is not int or start < 1 or end != start:
        return False, "yanlis_sayfa", "source must match the chunk's physical page"
    version = ctx.active_versions.get(doc)
    if not version or row.get("processing_version") not in (None, "", version):
        return False, "kanit_birebir_degil", "missing or stale processing version"
    matches = _source_chunks(row, ctx)
    cid = row.get("chunk_id")
    if (cid and cid not in matches) or (not cid and len(matches) != 1):
        return False, "kanit_birebir_degil", "missing, ambiguous or incompatible current chunk/quote"
    if row.get("kanit_turu") == "tablo" and row.get("taslak_notu"):
        try:
            note = json.loads(row["taslak_notu"])
            context = note.get("table_context") if isinstance(note, dict) else None
        except (TypeError, ValueError):
            return False, "kanit_birebir_degil", "invalid table context note"
        if context is not None:
            from src.clinical_candidates import table_contexts
            chosen = cid or matches[0]
            source = next(text for source_id, text in ctx.chunks[(doc, start)] if source_id == chosen)
            if not isinstance(context, dict):
                return False, "kanit_birebir_degil", "invalid table context"
            actual = table_contexts(source).get(context.get("data_row"))
            caption = norm_ws(context.get("caption"))
            actual_caption = norm_ws(actual.get("caption")) if actual else ""
            if (not actual or actual["status"] != "simple" or context.get("status") != "simple"
                or context.get("schema_version") != "table-context-v1"
                or any(context.get(k) != actual.get(k) for k in ("columns", "row_cells", "header_lines", "separator"))
                or actual_caption not in (caption, caption + " (devam)")):
                return False, "kanit_birebir_degil", "table context does not match current chunk"
    return True, None, None


def assess_diagnostic_relationship(fields, evidence):
    """Explicit detection statements only; not diagnostic accuracy or benefit.

    This intentionally small English grammar handles named methods and simple
    coordinated alternatives. Co-occurrence, ambiguous clauses and richer
    diagnostic studies remain review-only. No clinical truth is inferred.
    """
    diagnosis, method = fields.get("diagnosis"), fields.get("diagnostic_method")
    if not isinstance(diagnosis, str) or not diagnosis.strip() or not isinstance(method, str) or not method.strip():
        return "uncertain"
    diagnosis, method, quote = norm_ws(diagnosis), norm_ws(method), norm_ws(evidence)
    if method.casefold() in ("clinical encounters", "clinical encounter"):
        return "uncertain"
    if any(fields.get(k) not in (None, "") for k in ("drug", "dose", "dose_unit", "treatment_duration")):
        return "contradicted"
    if any(fields.get(k) not in (None, "") for k in (
        "patient_group", "comparison", "clinical_outcome", "numeric_value", "value_unit"
    )):
        return "uncertain"
    if not all(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", quote) for v in (diagnosis, method)):
        return "contradicted"
    if re.search(r",|\b(?:and|or)\b", method):
        return "uncertain"  # one method per finding
    for sentence in re.split(r"(?<=[.!?])\s+|;|\b(?:whereas|while|but|and)\b", quote):
        sentence = sentence.strip()
        head = re.match(re.escape(diagnosis) +
            r"\s+(?:can be|may be|is|was)\s+(?:detected|diagnosed|identified)\b(.*)", sentence)
        if head:
            tail = re.sub(r"^\s*(?:incidentally\s*)?(?:during clinical encounters\s*)?,?\s*", "", head[1])
            parts = re.split(r",\s*(?:or\s+)?|\s+or\s+", tail.rstrip(".!?"))
            if any(re.fullmatch(r"(?:with|using|through|by)\s+(?:(?:a|an|the)\s+)?" + re.escape(method),
                                part.strip()) for part in parts):
                return "supported_local"
        if re.fullmatch(re.escape(method) + r"\s+(?:detects|can detect|is used to detect|was used to diagnose)\s+"
                        + re.escape(diagnosis) + r"[.!?]?", sentence):
            return "supported_local"
    return "uncertain"


def assess_clinical_relationships(fields, evidence, source_text=None, source_type="metin"):
    """Local textual support, NOT clinical truth or human approval.

    Only explicit drug/dose adjacency, single-clause observations and simple
    labelled table rows are supported. Complex comparisons/coreference need
    review rather than a guessed relationship. No LLM judgement is consumed.
    """
    if fields.get("diagnostic_method") is not None:
        return assess_diagnostic_relationship(fields, evidence)
    names = ("drug", "dose", "patient_group", "clinical_outcome", "numeric_value")
    if any(fields.get(k) is not None and not isinstance(fields[k], str)
           for k in (*names, "dose_unit")):
        return "uncertain"
    values = {k: norm_ws(fields.get(k)) for k in names}
    if fields.get("dose_unit") and values["dose"]:
        values["dose"] += " " + norm_ws(fields["dose_unit"])
    requested = {k: v for k, v in values.items() if v}
    if not requested:
        return "not_applicable"
    quote = norm_ws(evidence)
    def contains(value, text):
        return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text) is not None
    if any(not contains(v, quote) for v in requested.values()):
        return "contradicted"
    if source_type == "tablo":
        aliases = {
            "drug": {"drug", "intervention", "treatment", "ilaç", "müdahale"},
            "dose": {"dose", "dosage", "doz"},
            "patient_group": {"patient group", "population", "group", "hasta grubu"},
            "clinical_outcome": {"outcome", "clinical outcome", "result", "sonuç"},
            "numeric_value": {"value", "result", "değer"},
        }
        lines = (source_text or "").splitlines()
        candidates = []
        for i in range(len(lines) - 1):
            if not lines[i].strip().startswith("|") or not re.fullmatch(
                r"\|(?:\s*:?-{3,}:?\s*\|)+", lines[i + 1].strip()
            ):
                continue
            header = lines[i].strip()
            headers = [norm_ws(c).strip("* ").casefold() for c in header[1:-1].split("|")]
            indexes = {k: [j for j, h in enumerate(headers) if h in aliases[k]] for k in requested}
            if any(len(v) != 1 for v in indexes.values()):
                continue
            for line in lines[i + 2:]:
                if not line.strip().startswith("|"):
                    break
                raw_row = line.strip()
                cells = [norm_ws(c).strip("* ") for c in raw_row[1:-1].split("|")]
                if len(cells) != len(headers):
                    continue
                # The cited data row itself must be present in the quote.
                if not (norm_ws(line) in quote or norm_ws(" | ".join(cells)) in quote):
                    continue
                candidates.append(all(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)",
                                                cells[indexes[k][0]]) is not None
                                      for k, value in requested.items()))
        if sum(candidates) == 1:
            return "supported_local"
        return "contradicted" if candidates and not any(candidates) else "uncertain"

    # Split observation boundaries; never join separate arms or subjects.
    clauses = re.split(r"(?<=[.!?])\s+|;|,(?=\s+[A-Za-z])|\b(?:and|whereas|while|but|versus|vs\.?|ancak|oysa|ve)\b", quote, flags=re.I)
    anchors = list(requested.values())
    joint = [clause for clause in clauses if all(contains(v, clause) for v in anchors)]
    drug, dose = values["drug"], values["dose"]
    if drug and dose:
        observed = re.findall(re.escape(drug) + r"\s+(?:(?:at|a|dose|of|was|given)\s+)*"
                              r"(\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|g|mL|ml|IU))\b", quote)
        if observed and not any(re.fullmatch(re.escape(dose) + r"(?:\s*(?:mg|mcg|µg|g|mL|ml|IU))?",
                                             norm_ws(v)) for v in observed):
            return "contradicted"
    if not joint:
        return "uncertain"
    group, outcome = values["patient_group"], values["clinical_outcome"]
    if group and any(len(re.findall(r"\b(?:received|took|had|experienced|aldı|kullandı)\b",
                                   clause, flags=re.I)) > 1 for clause in joint):
        return "uncertain"
    if group and drug:
        pattern = re.escape(group) + r"\s+(?:received|took|taking|were treated with|treated with|aldı|kullandı)\s+" + re.escape(drug)
        if not any(re.search(pattern, clause) for clause in joint):
            return "uncertain"
    if outcome and (drug or group):
        subject = drug or group
        # Require an explicit observation predicate; shared vocabulary alone is insufficient.
        pattern = re.escape(subject) + r"[^;.!?]*\b(?:reduced|increased|had|experienced|showed|reported|resulted in|azalttı|artırdı)\b[^;.!?]*" + re.escape(outcome)
        if not any(re.search(pattern, clause) for clause in joint):
            return "uncertain"
    if dose and not drug:
        return "uncertain"
    if drug and dose:
        # Explicit syntax only: a nearby different drug cannot donate its dose.
        pattern = (r"(?<!\w)" + re.escape(drug) + r"\s+(?:(?:at|a|dose|of|received|was|given)\s+)*"
                   + re.escape(dose) + r"(?!\w)")
        if not any(re.search(pattern, clause) for clause in joint):
            return "uncertain"
    outcome, value = values["clinical_outcome"], values["numeric_value"]
    if value and outcome and value not in outcome:
        pattern = re.escape(outcome) + r"\s*(?:(?:was|were|of|by|at|:|=)\s*)*" + re.escape(value) + r"(?!\w)"
        if not any(re.search(pattern, clause) for clause in joint):
            return "uncertain"
    if value and not outcome:
        return "uncertain"
    return "supported_local"


def validate_relationship_row(row, ctx):
    """Validate native SQL fields AND preserved extraction fields independently."""
    cid = row.get("chunk_id") or find_chunk_id(row, ctx)
    text = next((text for source_id, text in ctx.chunks.get(
        (row.get("document_id"), row.get("kanit_sayfa")), []) if source_id == cid), "")
    native = dict(drug=row.get("mudahale_en") or row.get("mudahale_tr"), dose=row.get("doz"),
                  patient_group=row.get("hasta_grubu"), clinical_outcome=row.get("sonuc_olcutu_tr"),
                  numeric_value=row.get("deger"))
    representations = [native]
    note = row.get("taslak_notu")
    payload = {}
    if note:
        try:
            payload = json.loads(note)
        except (TypeError, ValueError):
            return "uncertain"
        if not isinstance(payload, dict):
            return "uncertain"
        extracted = payload.get("extracted_fields", {})
        if not isinstance(extracted, dict):
            return "uncertain"
        if extracted.get("diagnostic_method") and row.get("mudahale_turu") != "tanı testi":
            return "contradicted"
    extracted = payload.get("extracted_fields", {})
    capability = row.get("sonuc_olcutu_tr") or extracted.get("clinical_outcome")
    if row.get("mudahale_turu") == "tanı testi" and capability and re.search(
        r"\b(?:can|may) be (?:detected|diagnosed|identified)\b", norm_ws(capability)
    ):
        return "uncertain"  # diagnostic capability is not an observed clinical outcome
    if row.get("mudahale_turu") == "tanı testi" and (native["drug"] or extracted.get("diagnostic_method")):
        method = row.get("mudahale_en") or row.get("mudahale_tr")
        if (extracted.get("diagnostic_method") and extracted["diagnostic_method"] != method
            or row.get("mudahale_en") and row.get("mudahale_tr") and row["mudahale_en"] != row["mudahale_tr"]):
            return "contradicted"
        diagnostic = dict(diagnosis=row.get("tani_en") or row.get("tani_tr") or extracted.get("diagnosis"),
            diagnostic_method=method, drug=None, dose=row.get("doz"), treatment_duration=row.get("sure"),
            patient_group=row.get("hasta_grubu"), comparison=row.get("comparator"),
            clinical_outcome=row.get("sonuc_olcutu_tr") or extracted.get("clinical_outcome"),
            numeric_value=row.get("deger"), value_unit=row.get("unit"))
        result = assess_diagnostic_relationship(diagnostic, row.get("kanit_cumle"))
        if result != "supported_local":
            return result
        if extracted:
            # A valid SQL representation cannot hide conflicting original fields.
            if not extracted.get("diagnostic_method"):
                return "uncertain"  # do not silently reinterpret legacy drug/outcome
            if diagnostic["diagnosis"] != extracted.get("diagnosis"):
                return "contradicted"
            result = assess_diagnostic_relationship(extracted, row.get("kanit_cumle"))
            if result != "supported_local":
                return result
        stored_result = payload.get("clinical_result")
        if isinstance(stored_result, dict) and stored_result.get("original_text"):
            return "uncertain"
        return "supported_local"
    if note:
        if isinstance(payload, dict) and "extracted_fields" in payload:
            if not isinstance(payload["extracted_fields"], dict):
                return "uncertain"
            representations.append(payload["extracted_fields"])
            # English outcomes and unspecified-language interventions are kept
            # in the existing draft payload, not dedicated SQL columns.
            for key in ("drug", "clinical_outcome"):
                if not native[key]:
                    native[key] = payload["extracted_fields"].get(key)
        if isinstance(payload, dict) and "clinical_result" in payload:
            result = payload["clinical_result"]
            if not isinstance(result, dict) or result.get("schema_version") != "clinical-result-v1":
                return "uncertain"
            original = result.get("original_text")
            extracted = payload.get("extracted_fields", payload.get("untranslated_fields", {}))
            if isinstance(extracted, dict) and "clinical_outcome" in extracted and original != extracted["clinical_outcome"]:
                return "contradicted"
            if original:
                representations.append({**native, "clinical_outcome": original})
    if row.get("mudahale_en") and row.get("mudahale_tr"):
        representations.append({**native, "drug": row["mudahale_tr"]})
    statuses = [assess_clinical_relationships(f, row.get("kanit_cumle"), text,
                row.get("kanit_turu") or "metin") for f in representations]
    return next((s for s in statuses if s in ("contradicted", "uncertain")), "supported_local")


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

    source_check = validate_source(row, ctx)
    if not source_check[0]:
        return source_check

    relationship = validate_relationship_row(row, ctx)
    if relationship in ("contradicted", "uncertain"):
        return False, "deger_kumesi_disi", "clinical_relationship_" + relationship + ": human review required"

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
