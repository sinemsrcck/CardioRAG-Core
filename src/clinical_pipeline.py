"""
Klinik çıkarım toplu işinin saf mantığı (IO yok; bağlantı/istemci dışarıdan gelir).

Mevcut modüller değiştirilmeden yalnızca import edilir:
  clinical_candidates  -> adaylar, kalıplar
  clinical_extraction  -> SYSTEM_PROMPT, RESPONSE_SCHEMA, _validate_response, ...
  clinical_validation  -> validate_row, prose, norm_ws (kanıt kuralları)
  clinical_review      -> _source_binding (yalnızca tablo bağlamı denetimi;
                          insan onayı ÜRETİLMEZ ve istenmez)

İnsan onayı alanları (review_source, approval_status, reviewer_id) hiçbir
yerde üretilmez. Bütün satırlar review_status='pending'; otomatik atanan
değerler taslak_notu.otomatik_alanlar içinde işaretlenir.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from src.clinical_candidates import TREATMENT_REGEX, find_patterns
from src.clinical_extraction import (
    EXTRACTION_SCHEMA_VERSION,
    FIELDS,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    _validate_response,
    clinical_result_payload,
)
from src.clinical_validation import norm_ws, prose, validate_row


# ---------------------------------------------------------------------------
# Sürüm ve prompt
# ---------------------------------------------------------------------------

PIPELINE_NAME = "clinical-pipeline-v1"
USER_MESSAGE_FORMAT = "user-json-v1: candidate_text, allowed_quote_spans, table_context?"
PROMPT_ADDENDUM = """
Additional batch rules:
- allowed_quote_spans lists the parts of candidate_text that can be verified
  against the source document. Each evidence_quote must lie entirely inside ONE
  allowed span; never quote across two spans.
- For a table row candidate, evidence_quote is the entire data row (candidate_text).
- A finding must name either a medication (drug) or a diagnostic/detection method
  (diagnostic_method); findings with neither will not be stored.
- Copy patient_group, comparison, clinical_outcome, numeric_value and value_unit
  verbatim only when explicitly stated in the quote; otherwise use null.
"""
PIPELINE_PROMPT = SYSTEM_PROMPT + PROMPT_ADDENDUM
SYSTEM_MESSAGE = PIPELINE_PROMPT + "\nSchema: " + json.dumps(RESPONSE_SCHEMA)
PROMPT_SHA = hashlib.sha256(
    "\n".join([SYSTEM_MESSAGE, json.dumps(RESPONSE_SCHEMA, sort_keys=True),
               EXTRACTION_SCHEMA_VERSION, USER_MESSAGE_FORMAT]).encode("utf-8")
).hexdigest()[:12]
PIPELINE_VERSION = f"{PIPELINE_NAME}-{PROMPT_SHA}"

MIN_SPAN_CHARS = 20
NO_THINK_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
NOTICE = "Otomatik LLM çıkarımı; insan incelemesi gerekli; onay yok."


class BatchStop(Exception):
    """Bütün işi durduran hata (ayar, sunucu reddi, kilit, ilk çalıştırma kuralı)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class CandidateError(Exception):
    """Yalnızca o adayı 'hata' yapan sorun; iş devam eder."""

    def __init__(self, code, detail=None, http_status=None, attempts=1):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Kaynaklar (salt okuma)
# ---------------------------------------------------------------------------

@dataclass
class Sources:
    articles: list
    ctx: object                      # clinical_validation.ValidationContext
    chunk_index: dict                # chunk_id -> chunk_index (güncel sürüm)
    max_lengths: dict = field(default_factory=dict)


def load_sources(conn):
    """Makaleler, doğrulama bağlamı, chunk sıraları ve sütun sınırları (READ ONLY)."""
    from src.clinical_candidates import load_our_articles
    from src.clinical_validation import load_validation_context
    from src.database import _column_max_lengths

    articles = load_our_articles(conn)          # kendi READ ONLY + rollback'i var
    cursor = conn.cursor()
    try:
        cursor.execute("START TRANSACTION READ ONLY")
        ctx = load_validation_context(conn)
        cursor.execute(
            "SELECT c.chunk_id, c.chunk_index FROM pdf_chunks AS c JOIN pdf_belgeleri AS b "
            "ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
            "WHERE b.durum = 'tamamlandi'"
        )
        chunk_index = {(bytes(cid).decode() if isinstance(cid, (bytes, bytearray)) else cid): int(idx)
                       for cid, idx in cursor.fetchall()}
        max_lengths = _column_max_lengths(cursor, "klinik_bulgular")
    finally:
        conn.rollback()
        cursor.close()
    return Sources(articles=articles, ctx=ctx, chunk_index=chunk_index, max_lengths=max_lengths)


# ---------------------------------------------------------------------------
# Aday hazırlama
# ---------------------------------------------------------------------------

def safe_spans(text, page_prose, chunk_texts, min_len=MIN_SPAN_CHARS):
    """text'in hem sayfa prose'unda hem tek bir ham chunk'ta birebir geçen en uzun parçaları.

    validate_row kanıtı markdown'u temizlenmiş sayfada (prose), validate_source
    ham chunk'ta arar; ikisinden de geçebilecek alıntı yalnızca bu parçaların içindedir.
    """
    spans, i, n = [], 0, len(text)
    while i < n:
        if text[i] == " ":
            i += 1
            continue
        lo, hi = i, n
        while lo < hi:  # koşul önek uzunluğuna göre monoton
            mid = (lo + hi + 1) // 2
            piece = text[i:mid]
            if piece in page_prose and any(piece in chunk for chunk in chunk_texts):
                lo = mid
            else:
                hi = mid - 1
        if lo == i:
            i += 1
            continue
        span = text[i:lo].strip()
        if len(span) >= min_len:
            spans.append(span)
        i = lo
    return spans


def _exclusion(candidate, reason, detail):
    return {
        "aday_id": candidate.get("aday_id"), "dosya": candidate.get("dosya"),
        "sayfa": candidate.get("sayfa"), "sayfa_bitis": candidate.get("sayfa_bitis"),
        "kaynak_turu": candidate.get("kaynak_turu"), "birlestirme": candidate.get("birlestirme"),
        "sebep": reason, "aciklama": detail, "cumle": candidate.get("cumle"),
    }


def prepare_candidates(candidates, sources):
    """Adayları LLM'e gidecekler ve sebebiyle ayrılanlar olarak ikiye böler.

    Hiçbir aday sessizce düşmez: len(gidecek) + len(ayrilan) == len(candidates).
    """
    from src.clinical_review import ClinicalReviewError, _source_binding

    ctx = sources.ctx
    to_send, excluded = [], []
    for index, candidate in enumerate(candidates):
        kind = candidate.get("kaynak_turu")
        if kind == "başlık":
            excluded.append(_exclusion(candidate, "baslik",
                                       "kanit_turu yalnızca metin/tablo olabilir (validate_row)"))
            continue
        if candidate.get("sayfa_bitis") not in ("", None):
            excluded.append(_exclusion(candidate, "sayfa_asan",
                                       "validate_source kanit_sayfa_bitis != kanit_sayfa kabul etmez"))
            continue
        if kind == "tablo":
            try:
                _source_binding(candidate, index)
            except ClinicalReviewError as exc:
                reason = ("tablo_karmasik" if exc.code == "table_context_requires_review"
                          else "tablo_baglami_gecersiz")
                excluded.append(_exclusion(candidate, reason, exc.code))
                continue

        doc, page = candidate["document_id"], candidate["sayfa"]
        chunk_texts = [norm_ws(text) for _, text in ctx.chunks.get((doc, page), [])]
        text = candidate["cumle"]
        if kind == "tablo":
            spans = [text] if any(norm_ws(text) in chunk for chunk in chunk_texts) else []
        else:
            page_text = ctx.pages.get(doc, {}).get(page)
            spans = safe_spans(norm_ws(text), prose(page_text), chunk_texts) if page_text else []
        if not any(any(find_patterns(span)) for span in spans):
            excluded.append(_exclusion(
                candidate, "guvenli_parca_yok",
                "doğrulanabilir (prose + tek chunk) parçalarda doz/ilaç/tanı kalıbı yok"))
            continue
        to_send.append({"candidate": candidate, "allowed_quote_spans": spans})

    if len(to_send) + len(excluded) != len(candidates):
        raise RuntimeError("Aday sayımı tutmuyor: sessiz kayıp var")
    return to_send, excluded


def order_round_robin(prepared):
    """Makaleler arasında sırayla dağıtır (--limit denemesi tek makaleye yığılmasın)."""
    groups = {}
    for item in prepared:
        groups.setdefault(item["candidate"]["dosya"], []).append(item)
    queues = [groups[name] for name in sorted(groups)]
    ordered = []
    for position in range(max((len(q) for q in queues), default=0)):
        ordered.extend(q[position] for q in queues if position < len(q))
    return ordered


# ---------------------------------------------------------------------------
# LLM çağrısı
# ---------------------------------------------------------------------------

def build_messages(item):
    candidate = item["candidate"]
    user = {"candidate_text": candidate["cumle"], "allowed_quote_spans": item["allowed_quote_spans"]}
    if "table_context" in candidate:
        user["table_context"] = candidate["table_context"]
    return [{"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


RETRYABLE_NAMES = {"APITimeoutError", "APIConnectionError", "Timeout", "TimeoutError",
                   "ConnectTimeout", "ReadTimeout", "ConnectionError"}


def server_rejected_message(status, name):
    return (f"LLM sunucusu isteği reddetti (HTTP {status}, {name}). Olası sebepler: "
            "enable_thinking parametresi (--think ile deneyin) veya "
            "response_format=json_object desteği.")


def call_llm(client, model, messages, *, max_tokens, no_think, max_retries, sleep):
    """Tek istek; geçici hatalarda artan beklemeyle yeniden dener.

    Loglara/istisnalara anahtar, header, yanıt gövdesi veya istisna metni
    taşınmaz: yalnızca istisna tür adı ve HTTP durum kodu.
    """
    kwargs = dict(model=model, messages=messages, max_completion_tokens=max_tokens,
                  response_format={"type": "json_object"})
    if no_think:
        kwargs["extra_body"] = NO_THINK_EXTRA_BODY
    for attempt in range(max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs), attempt + 1
        except Exception as exc:  # noqa: BLE001 - sınıflandırılıp güvenli biçimde yeniden fırlatılır
            name = type(exc).__name__
            status = getattr(exc, "status_code", None)
            status = status if type(status) is int else None
            if status in (400, 422):
                raise BatchStop("sunucu_istegi_reddetti", server_rejected_message(status, name)) from None
            if status in (401, 403):
                raise BatchStop("yetki_hatasi", f"LLM sunucusu kimlik doğrulamayı reddetti "
                                f"(HTTP {status}, {name}). .env'deki LLM_API_KEY'i kontrol edin.") from None
            if status == 404:
                raise BatchStop("model_bulunamadi", f"LLM sunucusu 404 döndü ({name}). Model adı "
                                "(--model / LLM_MODEL) veya LLM_BASE_URL yanlış olabilir.") from None
            retryable = (status in (408, 409, 429) or (status is not None and status >= 500)
                         or (status is None and name in RETRYABLE_NAMES))
            if retryable and attempt < max_retries:
                sleep(2 ** attempt)
                continue
            raise CandidateError("api_hatasi", name, status, attempt + 1) from None


def response_text(response):
    """(ham_metin, finish_reason); tamamlanmamış yanıt aday hatasıdır."""
    choices = getattr(response, "choices", None)
    if not choices:
        raise CandidateError("bos_yanit")
    choice = choices[0]
    finish = getattr(choice, "finish_reason", None)
    content = getattr(getattr(choice, "message", None), "content", None)
    if finish != "stop" or not isinstance(content, str):
        raise CandidateError("yanit_tamamlanmadi", str(finish))
    return content, finish


THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(raw):
    """<think>…</think> bloklarını ayıklar. Kapanmamış blok -> CandidateError."""
    text = THINK_BLOCK.sub("", raw)
    if re.search(r"<think>", text, re.IGNORECASE):
        raise CandidateError("think_kapanmamis")
    if re.search(r"</think>", text, re.IGNORECASE):
        # Bazı şablonlar açılış etiketini istemde verir; yanıtta yalnızca kapanış kalır.
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


def parse_llm_json(raw):
    text = strip_think(raw)
    try:
        payload = json.loads(text)
    except ValueError:
        raise CandidateError("json_gecersiz") from None
    if not isinstance(payload, dict) or set(payload) != {"findings"} or not isinstance(payload["findings"], list):
        raise CandidateError("yanit_semasi")
    return payload


# ---------------------------------------------------------------------------
# Bulgu -> satır
# ---------------------------------------------------------------------------

def _finding_error_code(message):
    if message.startswith("diagnostic_relationship_"):
        return message
    if message.startswith("evidence_quote"):
        return "alinti_birebir_degil"
    if "must be null or verbatim" in message:
        return "alan_alintida_yok"
    if message.startswith("Finding has no clinical fields"):
        return "bos_bulgu"
    if message.startswith("Missing or unexpected finding fields"):
        return "bulgu_alanlari"
    return "bulgu_gecersiz"


TR_CHARS = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
TR_WORDS = re.compile(r"\b(?:ve|ile|bir|bu|için|olan|olarak|daha|hastalarda|hastaların)\b", re.IGNORECASE)


def detect_language(text):
    """Basit ve deterministik dil sezgisi (otomatik; taslak_notu'nda işaretlenir)."""
    return "tr" if len(TR_CHARS.findall(text)) >= 2 or len(TR_WORDS.findall(text)) >= 2 else "en"


def select_chunk(quote, candidate, sources):
    """Alıntıyı içeren güncel chunk: tek eşleşme ya da en küçük chunk_index."""
    needle = norm_ws(quote)
    matches = [cid for cid, text in sources.ctx.chunks.get((candidate["document_id"], candidate["sayfa"]), [])
               if needle in norm_ws(text)]
    if not matches:
        return None, None
    matches = sorted(set(matches), key=lambda cid: (sources.chunk_index.get(cid, 10 ** 9), cid))
    return matches[0], ("tek_eslesme" if len(matches) == 1 else f"otomatik_ilk ({len(matches)} chunk)")


def build_row(finding, item, sources, *, model):
    """Bir doğrulanmış bulgudan klinik_bulgular satırı (deterministik)."""
    from src.database import KLINIK_INSERT_COLUMNS

    candidate = item["candidate"]
    quote = finding["evidence_quote"]
    expanded = False
    if candidate["kaynak_turu"] == "tablo" and quote != candidate["cumle"]:
        quote, expanded = candidate["cumle"], True
    elif candidate["kaynak_turu"] != "tablo" and not any(
            norm_ws(quote) in norm_ws(span) for span in item["allowed_quote_spans"]):
        return None, ("guvenli_parca_disinda", "alıntı tek bir doğrulanabilir parçanın içinde değil")

    method, drug = finding.get("diagnostic_method"), finding.get("drug")
    if not method and not drug:
        return None, ("mudahale_alani_yok", "drug ve diagnostic_method boş; mudahale_turu atanamaz")

    chunk_id, chunk_rule = select_chunk(quote, candidate, sources)
    if chunk_id is None:
        return None, ("kanit_chunk_yok", "alıntı güncel chunk'larda yok")

    language = detect_language(candidate["cumle"])
    mudahale_turu = "tanı testi" if method else "tedavi"
    row = dict.fromkeys(KLINIK_INSERT_COLUMNS)
    row.update(
        makale_id=candidate["makale_id"], document_id=candidate["document_id"], chunk_id=chunk_id,
        kanit_sayfa=candidate["sayfa"], kanit_sayfa_bitis=None, kanit_turu=candidate["kaynak_turu"],
        birlestirme=None, kanit_cumle=quote, review_status="pending", cikarma_yontemi="rag",
        mudahale_turu=mudahale_turu,
        hasta_grubu=finding["patient_group"], comparator=finding["comparison"],
        doz=(finding["dose"] + " " + finding["dose_unit"]) if finding["dose"] and finding["dose_unit"]
        else finding["dose"],
        sure=finding["treatment_duration"], deger=finding["numeric_value"], unit=finding["value_unit"],
    )
    row["tani_" + language] = finding["diagnosis"]
    row["mudahale_" + language] = method or drug
    if language == "tr":
        row["sonuc_olcutu_tr"] = finding["clinical_outcome"]

    note = {
        "kaynak": "clinical_pipeline",
        "pipeline_version": PIPELINE_VERSION,
        "prompt_sha": PROMPT_SHA,
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "model": model,
        "aday_id": candidate["aday_id"],
        "source_language": language,
        "extracted_fields": {f: finding.get(f) for f in FIELDS},
        "clinical_result": clinical_result_payload(finding["clinical_outcome"], language),
        "otomatik_alanlar": {
            "mudahale_turu": f"otomatik ({'diagnostic_method' if method else 'drug'} alanından)",
            "source_language": "otomatik (karakter/kelime sezgisi)",
            "chunk_id": f"otomatik ({chunk_rule})",
            **({"kanit_cumle": "otomatik (tam tablo satırına genişletildi)"} if expanded else {}),
        },
        "kanit_genisletildi": expanded,
        "aday_birlestirme": candidate.get("birlestirme") or None,
        "notice": NOTICE,
    }
    if "table_context" in candidate:
        note["table_context"] = candidate["table_context"]
    row["taslak_notu"] = json.dumps(note, ensure_ascii=False, sort_keys=True)
    return row, None


def process_response(raw_text, item, sources, *, model):
    """Ham yanıt -> (satırlar, red kayıtları, bulgu_sayısı). Bulgu bazında izolasyon."""
    candidate = item["candidate"]
    payload = parse_llm_json(raw_text)
    rows, rejections = [], []
    for position, finding in enumerate(payload["findings"]):
        base = {"aday_id": candidate["aday_id"], "dosya": candidate["dosya"],
                "sayfa": candidate["sayfa"], "bulgu_sirasi": position, "bulgu": finding}
        try:
            normalized = _validate_response({"findings": [finding]}, candidate["cumle"])[0]
        except ValueError as exc:
            rejections.append({**base, "asama": "yanit_dogrulama",
                               "hata_turu": _finding_error_code(str(exc)), "aciklama": str(exc)})
            continue
        row, problem = build_row(normalized, item, sources, model=model)
        if problem:
            rejections.append({**base, "asama": "satir_olusturma",
                               "hata_turu": problem[0], "aciklama": problem[1]})
            continue
        rows.append(row)
    return rows, rejections, len(payload["findings"])


def preview_rows(rows, item, sources):
    """validate_row (validate_source dahil) + sütun sınırı; salt okuma."""
    candidate = item["candidate"]
    passed, rejected = [], []
    for row in rows:
        ok, error_type, detail = validate_row(row, sources.ctx)
        if ok:
            too_long = [c for c, v in row.items() if isinstance(v, str)
                        and sources.max_lengths.get(c) and len(v) > sources.max_lengths[c]]
            if too_long:
                ok, error_type, detail = False, "deger_kumesi_disi", f"sütun sınırını aşan alan: {too_long}"
        if ok:
            passed.append(row)
        else:
            rejected.append({"aday_id": candidate["aday_id"], "dosya": candidate["dosya"],
                             "sayfa": candidate["sayfa"], "asama": "validate_row",
                             "hata_turu": error_type, "aciklama": detail, "satir": row})
    return passed, rejected


# ---------------------------------------------------------------------------
# Sahte LLM (--mock ve testler)
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Deterministik sahte istemci: ilk ilaç kalıbını içeren parçayı alıntılar.

    Gerçek istemcinin kullandığı arayüzü taklit eder (api_key, with_options,
    chat.completions.create). Hiçbir ağ çağrısı yapmaz.
    """

    model = "mock-llm"

    def __init__(self):
        self.api_key = "mock-not-a-secret"
        self.requests = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self._create

    def with_options(self, **_):
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        user = json.loads(kwargs["messages"][1]["content"])
        findings = []
        spans = [user["candidate_text"]] if "table_context" in user else user["allowed_quote_spans"]
        for span in spans:
            for regex in TREATMENT_REGEX.values():
                match = regex.search(span)
                if match:
                    findings.append({**dict.fromkeys(FIELDS), "drug": match.group(0),
                                     "evidence_quote": span})
                    break
            if findings:
                break
        content = json.dumps({"findings": findings}, ensure_ascii=False)
        message = type("Message", (), {"content": content})()
        choice = type("Choice", (), {"finish_reason": "stop", "message": message})()
        return type("Response", (), {"choices": [choice]})()
