"""
Klinik çıkarım toplu işinin saf mantığı (IO yok; bağlantı/istemci dışarıdan gelir).

Mevcut modüller değiştirilmeden yalnızca import edilir:
  clinical_candidates  -> adaylar, kalıplar
  clinical_extraction  -> SYSTEM_PROMPT, FIELDS, _validate_response, ...
                          (kendi yanıt şemamız: FIELDS + procedure)
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

# Kendi yanıt şemamız: mevcut FIELDS + "procedure". clinical_extraction.py
# değişmez; procedure ayrı denetlenip bulgu mevcut şemayla _validate_response'a verilir.
PROCEDURE_FIELD = "procedure"
# Değer alanları (alıntıdan birebir): p, %95 GA, örneklem büyüklüğü, etki ölçütü.
VALUE_FIELDS = ("p_value", "confidence_interval", "sample_size", "effect_measure")
EXTRA_FIELDS = (PROCEDURE_FIELD, *VALUE_FIELDS)
PIPELINE_FIELDS = (*FIELDS, *EXTRA_FIELDS)
INTERVENTION_FIELDS = ("drug", "diagnostic_method", PROCEDURE_FIELD)
PIPELINE_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        **{name: {"type": ["string", "null"]} for name in PIPELINE_FIELDS},
        "evidence_quote": {"type": "string", "minLength": 1},
    },
    "required": [*PIPELINE_FIELDS, "evidence_quote"],
    "additionalProperties": False,
}
PIPELINE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": PIPELINE_FINDING_SCHEMA}},
    "required": ["findings"],
    "additionalProperties": False,
}

PROMPT_ADDENDUM = """
Additional batch rules:
- allowed_quote_spans lists the parts of candidate_text that can be verified
  against the source document. Each evidence_quote must lie entirely inside ONE
  allowed span; never quote across two spans.
- For a table row candidate, evidence_quote is the entire data row (candidate_text).
- procedure: an interventional, surgical or device procedure whose name is
  explicitly stated (e.g. catheter ablation, PCI, CABG, stent implantation,
  cardioversion, left atrial appendage occlusion). It is not a medication and not
  a diagnostic test. Never infer it; copy it verbatim from evidence_quote.
- A finding must name exactly one intervention: a medication (drug), a procedure
  (procedure) or a diagnostic/detection method (diagnostic_method). Emit a
  separate finding for each intervention; findings with none or with more than
  one of these three fields will not be stored.
- Copy patient_group, comparison, clinical_outcome, numeric_value and value_unit
  verbatim only when explicitly stated in the quote; otherwise use null.
- p_value: the p value text exactly as written (e.g. "P < .001", "p=0.03").
  confidence_interval: the confidence interval text exactly as written
  (e.g. "95% CI, 0.62-0.88"). sample_size: the number of patients/participants
  exactly as written (e.g. "n = 372", "1,234 patients"). effect_measure: the name
  of the effect measure exactly as written (e.g. "hazard ratio", "OR", "AUC").
  Copy each verbatim from evidence_quote; never calculate, convert or infer; use
  null when absent.
"""
PIPELINE_PROMPT = SYSTEM_PROMPT + PROMPT_ADDENDUM
SYSTEM_MESSAGE = PIPELINE_PROMPT + "\nSchema: " + json.dumps(PIPELINE_RESPONSE_SCHEMA)
PROMPT_SHA = hashlib.sha256(
    "\n".join([SYSTEM_MESSAGE, json.dumps(PIPELINE_RESPONSE_SCHEMA, sort_keys=True),
               EXTRACTION_SCHEMA_VERSION, USER_MESSAGE_FORMAT]).encode("utf-8")
).hexdigest()[:12]
PIPELINE_VERSION = f"{PIPELINE_NAME}-{PROMPT_SHA}"
TOKEN_PARAMS = ("max_completion_tokens", "max_tokens")
JSON_MODES = ("json_object", "none")

# Sabit, sürümlü eşlemeler (değişince sürüm adı artırılır; taslak_notu'na yazılır).
SAMPLE_SIZE_VERSION = "ornek-buyuklugu-v1"
SAMPLE_SIZE_PATTERN = re.compile(
    r"^\s*(?:n\s*=\s*)?(?P<num>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\s+(?:patients|participants|subjects|individuals|people|hasta|hastalar|kişi))?\s*$", re.IGNORECASE)
EFFECT_MAP_VERSION = "etki-esleme-v1"
EFFECT_MEASURE_MAP = {
    "OR": ("or", "odds ratio", "odds ratios", "adjusted odds ratio", "aor"),
    "RR": ("rr", "relative risk", "risk ratio", "adjusted relative risk"),
    "HR": ("hr", "hazard ratio", "hazard ratios", "adjusted hazard ratio", "ahr"),
    "IRR": ("irr", "incidence rate ratio", "incidence rate ratios"),
    "AUC / C-istatistigi": ("auc", "auroc", "area under the curve", "area under the roc curve",
                            "area under the receiver operating characteristic curve",
                            "c-statistic", "c statistic", "c-index"),
}
ICD_MATCH_VERSION = "icd-birebir-v1"
TABLE_VALUE_NOTE = "tablo: yeni alanlar sütun kaynağı doğrulanamadığı için yazılmadı"


def parse_sample_size(raw):
    """(tamsayı | None, açıklama). Binlik ayırıcılı değer yazılmaz: validate_row "1,234"ü tek sayı görür."""
    if raw is None:
        return None, None
    match = SAMPLE_SIZE_PATTERN.match(str(raw))
    if not match:
        return None, "ayrıştırılamadı (deterministik kalıba uymuyor)"
    number = match.group("num")
    if "," in number:
        return None, (f"binlik ayırıcılı değer ({int(number.replace(',', ''))}); validate_row sayı "
                      "kontrolüyle çakışacağı için hasta_sayisi'na yazılmadı")
    return int(number), "ayrıştırıldı"


def _effect_key(raw):
    return re.sub(r"\s+", " ", re.sub(r"[().:;,]", " ", str(raw).casefold())).strip()


_EFFECT_INDEX = {alias: canonical for canonical, aliases in EFFECT_MEASURE_MAP.items() for alias in aliases}


def map_effect_measure(raw):
    """Sabit eşleme; eşleşmezse None (ham metin taslak_notu'nda kalır)."""
    return None if raw is None else _EFFECT_INDEX.get(_effect_key(raw))


def lookup_icd10(diagnosis, icd_names):
    """(kod | None, açıklama): tanı adı icd10_who'daki bir adla normalize BİREBİR eşitse kod."""
    if not diagnosis or not icd_names:
        return None, None
    from src.data_quality import norm_text

    key = norm_text(diagnosis)
    codes = sorted({code for code, en, tr in icd_names if key and key in (norm_text(en), norm_text(tr))})
    if len(codes) == 1:
        return codes[0], f"icd10_who adıyla birebir: '{diagnosis}'"
    if len(codes) > 1:
        return None, f"birden fazla kod eşleşti {codes}; boş bırakıldı"
    return None, None

MIN_SPAN_CHARS = 20
NO_THINK_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
NOTICE = "Otomatik LLM çıkarımı; insan incelemesi gerekli; onay yok."


class BatchStop(Exception):
    """Bütün işi durduran hata (ayar, sunucu reddi, kilit, ilk çalıştırma kuralı)."""

    def __init__(self, code, message, provider_metadata=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_metadata = provider_metadata or {}


class CandidateError(Exception):
    """Yalnızca o adayı 'hata' yapan sorun; iş devam eder."""

    def __init__(self, code, detail=None, http_status=None, attempts=1, connection_diagnostics=None,
                 provider_metadata=None):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.attempts = attempts
        self.connection_diagnostics = connection_diagnostics
        self.provider_metadata = provider_metadata or {}


def safe_provider_metadata(exc):
    """Fail-closed SDK metadata; no body/header/message serialization."""
    result = {}
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and re.fullmatch(r"req_[a-fA-F0-9]{16,64}", request_id):
        result["request_id"] = request_id
    code = getattr(exc, "code", None)
    allowed = {"server_error", "internal_error", "internal_server_error", "overloaded",
               "rate_limit_exceeded", "insufficient_quota", "invalid_api_key",
               "model_not_found", "invalid_request_error", "unsupported_parameter",
               "invalid_value", "permission_denied", "context_length_exceeded"}
    if isinstance(code, str) and code in allowed:
        result["provider_error_code"] = code
    return result


def safe_connection_diagnostics(exc):
    """Bounded cause metadata only; never serialize exception text or requests."""
    import ssl

    safe_types = {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadError",
                  "WriteError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                  "PoolTimeout", "TimeoutError", "ConnectionError", "OSError",
                  "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
                  "SSLError", "SSLCertVerificationError", "gaierror", "EndOfStream",
                  "RemoteProtocolError", "LocalProtocolError"}
    safe_ssl_reasons = {"CERTIFICATE_VERIFY_FAILED", "WRONG_VERSION_NUMBER",
                        "TLSV1_ALERT_PROTOCOL_VERSION", "TLSV1_ALERT_UNKNOWN_CA",
                        "SSLV3_ALERT_HANDSHAKE_FAILURE", "UNEXPECTED_EOF_WHILE_READING",
                        "TLSV1_ALERT_INTERNAL_ERROR", "TLSV1_ALERT_ACCESS_DENIED"}
    chain, seen = [], {id(exc)}
    current = exc.__cause__ or (exc.__context__ if not exc.__suppress_context__ else None)
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        name = type(current).__name__
        number = getattr(current, "errno", None)
        reason = getattr(current, "reason", None) if isinstance(current, ssl.SSLError) else None
        chain.append({"type": name if name in safe_types else "OtherError",
                      "errno": number if type(number) is int and -65535 <= number <= 65535 else None,
                      "ssl_reason": reason if isinstance(reason, str) and reason in safe_ssl_reasons else None})
        current = current.__cause__ or (current.__context__ if not current.__suppress_context__ else None)
    return {"cause_chain": chain} if chain else None


# ---------------------------------------------------------------------------
# Kaynaklar (salt okuma)
# ---------------------------------------------------------------------------

@dataclass
class Sources:
    articles: list
    ctx: object                      # clinical_validation.ValidationContext
    chunk_index: dict                # chunk_id -> chunk_index (güncel sürüm)
    max_lengths: dict = field(default_factory=dict)
    icd_names: list = field(default_factory=list)          # [(kod, ad_en, ad_tr)]
    article_metadata: dict = field(default_factory=dict)   # document_id -> {"baslik": ...}


def load_sources(conn):
    """Makaleler, doğrulama bağlamı, chunk sıraları, sütun sınırları, ICD adları, metaveri (READ ONLY)."""
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
        cursor.execute("SELECT kod, who_adi_en, saglik_bakanligi_adi_tr FROM icd10_who ORDER BY kod")
        icd_names = [tuple(_decode(v) for v in row) for row in cursor.fetchall()]
        cursor.execute("SELECT document_id, baslik FROM makaleler WHERE document_id IS NOT NULL")
        article_metadata = {_decode(doc): {"baslik": _decode(title)} for doc, title in cursor.fetchall() if title}
    finally:
        conn.rollback()
        cursor.close()
    return Sources(articles=articles, ctx=ctx, chunk_index=chunk_index, max_lengths=max_lengths,
                   icd_names=icd_names, article_metadata=article_metadata)


def _decode(value):
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else value


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
        "document_id": candidate.get("document_id"), "makale_id": candidate.get("makale_id"),
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
        if kind == "metadata":
            excluded.append(_exclusion(candidate, "metaveri", "article front matter or bibliographic metadata; not clinical evidence"))
            continue
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


def server_rejected_message(status, name, token_param="max_completion_tokens"):
    other = "max_tokens" if token_param == "max_completion_tokens" else "max_completion_tokens"
    return (f"LLM sunucusu isteği reddetti (HTTP {status}, {name}). Olası sebepler: "
            "enable_thinking parametresi (--think ile deneyin), "
            "response_format=json_object desteği veya "
            f"{token_param} parametresi desteği (--max-tokens-param {other} ile deneyin).")


def call_llm(client, model, messages, *, max_tokens, no_think, max_retries, sleep,
             token_param="max_completion_tokens", json_mode="json_object"):
    """Tek istek; geçici hatalarda artan beklemeyle yeniden dener.

    Loglara/istisnalara anahtar, header, yanıt gövdesi veya istisna metni
    taşınmaz: yalnızca güvenli tür/kodlar ve sınırlı alt-istisna metaverisi.
    json_mode='none' iken response_format gönderilmez; yanıt yine aynı kurallarla
    ayrıştırılır (parse_llm_json) ve doğrulanır.
    """
    if token_param not in TOKEN_PARAMS:
        raise BatchStop("secenek", f"--max-tokens-param {TOKEN_PARAMS} içinden olmalı.")
    if json_mode not in JSON_MODES:
        raise BatchStop("secenek", f"--json-mode {JSON_MODES} içinden olmalı.")
    kwargs = {"model": model, "messages": messages, token_param: max_tokens}
    if json_mode == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    if no_think:
        kwargs["extra_body"] = NO_THINK_EXTRA_BODY
    for attempt in range(max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs), attempt + 1
        except Exception as exc:  # noqa: BLE001 - sınıflandırılıp güvenli biçimde yeniden fırlatılır
            name = type(exc).__name__
            status = getattr(exc, "status_code", None)
            status = status if type(status) is int else None
            metadata = safe_provider_metadata(exc)
            if status in (400, 422):
                raise BatchStop("sunucu_istegi_reddetti",
                                server_rejected_message(status, name, token_param), metadata) from None
            if status in (401, 403):
                raise BatchStop("yetki_hatasi", f"LLM sunucusu kimlik doğrulamayı reddetti "
                                f"(HTTP {status}, {name}). .env'deki LLM_API_KEY'i kontrol edin.", metadata) from None
            if status == 404:
                raise BatchStop("model_bulunamadi", f"LLM sunucusu 404 döndü ({name}). Model adı "
                                "(--model / LLM_MODEL) veya LLM_BASE_URL yanlış olabilir.", metadata) from None
            retryable = (status in (408, 409, 429) or (status is not None and status >= 500)
                         or (status is None and name in RETRYABLE_NAMES))
            if retryable and attempt < max_retries:
                sleep(2 ** attempt)
                continue
            raise CandidateError("api_hatasi", name, status, attempt + 1,
                                 safe_connection_diagnostics(exc), metadata) from None


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
    if message.startswith("birden_fazla_mudahale"):
        return "birden_fazla_mudahale"
    return "bulgu_gecersiz"


def validate_finding(finding, text, *, table_source=None):
    """Bir bulguyu kendi şemamızla + mevcut _validate_response ile denetler.

    1. Alan kümesi tam olarak PIPELINE_FIELDS + evidence_quote.
    2. procedure doluysa string ve evidence_quote içinde birebir.
    3. drug / diagnostic_method / procedure'den en fazla biri dolu.
    4. procedure çıkarılıp bulgu MEVCUT şemayla _validate_response'a verilir:
       alıntı aday metninde birebir, her dolu alan alıntıda birebir,
       diagnostic_method ilişki kontrolü. Tek istisna: yalnızca procedure dolu
       bulgularda _validate_response'un son kuralı ("hiç klinik alan yok")
       procedure ile sağlanmış sayılır; o kuraldan önceki bütün kontroller
       yine _validate_response içinde çalışmış olur.
    Döndürür: procedure dahil normalize bulgu. Hata: ValueError(mesaj).
    """
    if not isinstance(finding, dict) or set(finding) != {*PIPELINE_FIELDS, "evidence_quote"}:
        raise ValueError("Missing or unexpected finding fields")
    procedure = finding[PROCEDURE_FIELD]
    quote = finding["evidence_quote"]
    # procedure ve değer alanları: string ve evidence_quote içinde BİREBİR (tablo dahil).
    for name in EXTRA_FIELDS:
        value = finding[name]
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or not isinstance(quote, str) or value not in quote):
            raise ValueError(f"{name} must be null or verbatim evidence text")
    filled = [name for name in INTERVENTION_FIELDS if finding[name] not in (None, "")]
    if len(filled) > 1:
        raise ValueError("birden_fazla_mudahale: " + ", ".join(filled))
    # Ek alanlar ayrılır; kalan bulgu MEVCUT şemayla ve kurallarla (tipli tablo dahil) denetlenir.
    base = {key: value for key, value in finding.items() if key not in EXTRA_FIELDS}
    try:
        normalized = _validate_response({"findings": [base]}, text, table_source=table_source)[0]
    except ValueError as exc:
        if not (procedure is not None and str(exc).startswith("Finding has no clinical fields")):
            raise
        normalized = base  # önceki bütün kurallar geçti; klinik alan = procedure
    return {**normalized, **{name: finding[name] for name in EXTRA_FIELDS}}


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

    method, drug, procedure = (finding.get("diagnostic_method"), finding.get("drug"),
                               finding.get(PROCEDURE_FIELD))
    filled = [name for name in INTERVENTION_FIELDS if finding.get(name)]
    if not filled:
        return None, ("mudahale_alani_yok", "drug, procedure ve diagnostic_method boş; mudahale_turu atanamaz")
    if len(filled) > 1:  # validate_finding zaten reddeder; ikinci güvence
        return None, ("birden_fazla_mudahale", ", ".join(filled))

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
    row["mudahale_" + language] = method or drug or procedure
    if language == "tr":
        row["sonuc_olcutu_tr"] = finding["clinical_outcome"]

    # Değer alanları. Tabloda sütun kaynağı (tipli tablo kuralı bu alanları bilmez)
    # doğrulanamadığı için sütunlara YAZILMAZ; değerler nota konur.
    raw_values = {name: finding.get(name) for name in VALUE_FIELDS if finding.get(name)}
    automatic, value_notes, withheld = {}, {}, None
    if candidate["kaynak_turu"] == "tablo":
        if raw_values:
            withheld = {"degerler": raw_values, "not": TABLE_VALUE_NOTE}
            automatic["tablo_yeni_alanlar"] = f"otomatik (sütunlara yazılmadı: {', '.join(sorted(raw_values))})"
    else:
        row["p"] = finding.get("p_value")
        row["ci95"] = finding.get("confidence_interval")
        if finding.get("sample_size"):
            count, reason = parse_sample_size(finding["sample_size"])
            row["hasta_sayisi"] = count
            value_notes["sample_size"] = {"ham": finding["sample_size"], "sonuc": reason,
                                          "surum": SAMPLE_SIZE_VERSION}
            if count is not None:
                automatic["hasta_sayisi"] = f"otomatik (sample_size ayrıştırıldı: '{finding['sample_size']}')"
        if finding.get("effect_measure"):
            mapped = map_effect_measure(finding["effect_measure"])
            row["etki_turu"] = mapped
            value_notes["effect_measure"] = {"ham": finding["effect_measure"], "eslesen": mapped,
                                             "surum": EFFECT_MAP_VERSION}
            if mapped:
                automatic["etki_turu"] = f"otomatik (effect_measure eşlemesi: '{finding['effect_measure']}')"
    icd, icd_reason = lookup_icd10(finding.get("diagnosis"), getattr(sources, "icd_names", None))
    row["icd10"] = icd
    if icd_reason:
        value_notes["icd10"] = {"tani": finding.get("diagnosis"), "sonuc": icd_reason, "surum": ICD_MATCH_VERSION}
    if icd:
        automatic["icd10"] = f"otomatik ({icd_reason})"

    note = {
        "kaynak": "clinical_pipeline",
        "pipeline_version": PIPELINE_VERSION,
        "prompt_sha": PROMPT_SHA,
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "model": model,
        "aday_id": candidate["aday_id"],
        "source_language": language,
        "extracted_fields": {f: finding.get(f) for f in PIPELINE_FIELDS},
        "clinical_result": clinical_result_payload(finding["clinical_outcome"], language),
        "otomatik_alanlar": {
            "mudahale_turu": f"otomatik ({filled[0]} alanından)",
            "source_language": "otomatik (karakter/kelime sezgisi)",
            "chunk_id": f"otomatik ({chunk_rule})",
            **({"kanit_cumle": "otomatik (tam tablo satırına genişletildi)"} if expanded else {}),
            **automatic,
        },
        "kanit_genisletildi": expanded,
        "aday_birlestirme": candidate.get("birlestirme") or None,
        "aday_cumlesi": candidate["cumle"],
        "notice": NOTICE,
    }
    if value_notes:
        note["deger_alanlari"] = value_notes
    if withheld:
        note["tablo_yazilmayan_alanlar"] = withheld
    if "table_context" in candidate:
        note["table_context"] = candidate["table_context"]
        from src.clinical_validation import clinical_table_type
        family = clinical_table_type(candidate["table_context"]["columns"])
        note["table_relationship_rule"] = {"version": "typed-tables-v1", "table_type": family,
            "scope": "local column/row support; not clinical truth or efficacy"}
        if family == "cct_recommendations":
            row["outcome_type"] = "öneri"
            note["table_relationship_rule"]["source_type"] = "guideline recommendation relayed by source"
            note["otomatik_alanlar"]["outcome_type"] = "otomatik (cct_recommendations tablo ailesi)"
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
            table_source = None
            if candidate["kaynak_turu"] == "tablo":
                matches = [text for _, text in sources.ctx.chunks.get(
                    (candidate["document_id"], candidate["sayfa"]), [])
                    if candidate["cumle"] in text]
                if len(matches) != 1:
                    raise ValueError("diagnostic_relationship_uncertain")
                table_source = matches[0]
            normalized = validate_finding(finding, candidate["cumle"], table_source=table_source)
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


def _too_long(row, max_lengths):
    return [c for c, v in row.items() if isinstance(v, str) and max_lengths.get(c) and len(v) > max_lengths[c]]


def validation_trail(row):
    """Geçilen otomatik kontrollerin deterministik listesi (zaman damgası yok)."""
    from src.database import CLINICAL_VALIDATION_VERSION

    note = json.loads(row["taslak_notu"])
    table = row.get("kanit_turu") == "tablo"
    method = (note.get("extracted_fields") or {}).get("diagnostic_method")
    automatic = note.get("otomatik_alanlar") or {}
    checks = [
        "yanit_semasi (kendi şema: FIELDS + procedure + değer alanları)",
        "alinti_aday_metninde_birebir",
        "alanlar_alintida_birebir (procedure, p_value, confidence_interval, sample_size, effect_measure dahil)",
        "tek_mudahale_alani (drug | procedure | diagnostic_method)",
        "tablo_tam_satir" if table else "guvenli_parca_icinde",
        *(["tablo: yeni alanlar sütunlara yazılmadı (sütun kaynağı doğrulanamaz)"]
          if note.get("tablo_yazilmayan_alanlar") else []),
        *([f"sample_size: deterministik tamsayı ayrıştırma ({SAMPLE_SIZE_VERSION})"]
          if "hasta_sayisi" in automatic else []),
        *([f"effect_measure: sabit eşleme ({EFFECT_MAP_VERSION})"] if "etki_turu" in automatic else []),
        *([f"icd10: icd10_who adıyla birebir eşleşme ({ICD_MATCH_VERSION})"] if "icd10" in automatic else []),
        *(["diagnostic_relationship: supported_local"] if method else []),
        "validate_source: makale_belge_eslesmesi",
        "validate_source: tek_sayfa",
        "validate_source: guncel_chunk_alintiyi_iceriyor",
        *(["validate_source: tablo_baglami_chunk_ile_ayni"] if table else []),
        "validate_row: iliski_kontrolu (supported_local)",
        "validate_row: deger_kumeleri",
        "validate_row: icd10 (" + ("icd10_who'da var" if row.get("icd10") else "boş; atanmadı") + ")",
        "validate_row: kanit_sayfada_birebir",
        "validate_row: sayilar_kanitta",
        "sutun_siniri",
    ]
    return {
        "insan_onayi": "yok",
        "gecen_kontroller": checks,
        "surumler": {"pipeline_version": PIPELINE_VERSION, "prompt_sha": PROMPT_SHA,
                     "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
                     "clinical_validation_version": CLINICAL_VALIDATION_VERSION,
                     "ornek_buyuklugu": SAMPLE_SIZE_VERSION, "etki_esleme": EFFECT_MAP_VERSION,
                     "icd_esleme": ICD_MATCH_VERSION},
        "not": "Otomatik kontroller klinik doğruluğu kanıtlamaz; insan incelemesi gerekli.",
    }


def preview_rows(rows, item, sources):
    """validate_row (validate_source dahil) + sütun sınırı; salt okuma.

    Geçen satırın taslak_notu'na otomatik_dogrulama izi eklenir ve son satır
    yeniden denetlenir (kaydedilecek satır = denetlenen satır).
    """
    candidate = item["candidate"]
    passed, rejected = [], []
    for row in rows:
        ok, error_type, detail = validate_row(row, sources.ctx)
        if ok and _too_long(row, sources.max_lengths):
            ok, error_type, detail = False, "alan_cok_uzun", f"sütun sınırını aşan alan: {_too_long(row, sources.max_lengths)}"
        if ok:
            note = json.loads(row["taslak_notu"])
            note["otomatik_dogrulama"] = validation_trail(row)
            final = {**row, "taslak_notu": json.dumps(note, ensure_ascii=False, sort_keys=True)}
            ok, error_type, detail = validate_row(final, sources.ctx)
            if ok and _too_long(final, sources.max_lengths):
                ok, error_type, detail = False, "alan_cok_uzun", f"sütun sınırını aşan alan: {_too_long(final, sources.max_lengths)}"
            row = final
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
    """Deterministik sahte istemci: ilk ilaç/işlem kalıbını içeren parçayı alıntılar.

    'girişimsel' kalıbına uyan eşleşme procedure alanına, diğerleri drug'a yazılır.

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
            for category, regex in TREATMENT_REGEX.items():
                match = regex.search(span)
                if match:
                    target = PROCEDURE_FIELD if category == "girişimsel" else "drug"
                    findings.append({**dict.fromkeys(PIPELINE_FIELDS), target: match.group(0),
                                     "evidence_quote": span})
                    break
            if findings:
                break
        content = json.dumps({"findings": findings}, ensure_ascii=False)
        message = type("Message", (), {"content": content})()
        choice = type("Choice", (), {"finish_reason": "stop", "message": message})()
        return type("Response", (), {"choices": [choice]})()
