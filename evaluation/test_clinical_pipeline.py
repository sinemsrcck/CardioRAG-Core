"""
Klinik çıkarım toplu işi testleri (sahte LLM; gerçek LLM gerekmez).

Çalıştırma:
    python -m evaluation.test_clinical_pipeline

A) Veritabanı gerektirmeyenler: <think> ayıklama, istek parametreleri,
   HTTP 400/422 mesajı, yeniden deneme, state.jsonl dayanıklılığı, kilit, önbellek.
B) MySQL gerektirenler (.env + kurulum): aday hazırlama, senaryolar, devam etme,
   --no-save -> önbellekten kayıt, --mock, --from-cache determinizmi, --dry-run.
   Bütün veritabanı yazımları ya dış transaction'da ROLLBACK edilir ya da
   --mock'un kendi ROLLBACK'i ile geri alınır; sonda sayılar karşılaştırılır.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.clinical_candidates import TREATMENT_REGEX
from src.clinical_extraction import FIELDS
from src.clinical_pipeline import (
    NO_THINK_EXTRA_BODY,
    BatchStop,
    CandidateError,
    MockLLMClient,
    call_llm,
    parse_llm_json,
    preview_rows,
    process_response,
)
from src.clinical_validation import norm_ws
from src.run_clinical_batch import (
    JsonlWriter,
    Options,
    acquire_lock,
    cache_path,
    main as cli_main,
    prepare,
    read_cache,
    read_state,
    run_batch,
    write_cache,
)

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def quiet(*_):
    pass


# ---------------------------------------------------------------------------
# Sahte istemci yardımcıları
# ---------------------------------------------------------------------------

def make_response(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                                                    message=SimpleNamespace(content=content))])


def http_error(status, name="BadRequestError"):
    exc = type(name, (Exception,), {})("response body with secret sk-test-SECRET")
    exc.status_code = status
    return exc


class ScriptedClient:
    """Gerçek istemci arayüzü: api_key, with_options, chat.completions.create."""

    def __init__(self, responder, api_key="test-only-key"):
        self.api_key = api_key
        self.responder = responder
        self.requests = []
        self.with_options_calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **kwargs):
        self.with_options_calls.append(kwargs)
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        user = json.loads(kwargs["messages"][1]["content"])
        return self.responder(user, kwargs, len(self.requests))


def drug_finding(span, **extra):
    for regex in TREATMENT_REGEX.values():
        match = regex.search(span)
        if match:
            return {**dict.fromkeys(FIELDS), "drug": match.group(0), "evidence_quote": span, **extra}
    return None


def findings_json(*findings):
    return json.dumps({"findings": [f for f in findings if f is not None]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# A) Veritabanı gerektirmeyen testler
# ---------------------------------------------------------------------------

def test_offline(tmp):
    # <think> ayıklama ve JSON.
    check("think bloğu ayıklanıyor", [], parse_llm_json('<think>x {"a":1}</think>\n{"findings": []}')["findings"])
    check("yalnız kapanış </think> (şablon açılışı)", [], parse_llm_json('düşünce</think>{"findings": []}')["findings"])
    for raw, code in (('<think>bitmedi {"findings": []}', "think_kapanmamis"),
                      ("bu json değil", "json_gecersiz"), ('{"x": []}', "yanit_semasi")):
        try:
            parse_llm_json(raw)
            got = "hata yok"
        except CandidateError as exc:
            got = exc.code
        check(f"geçersiz yanıt -> {code}", code, got)

    # İstek parametreleri: --no-think (varsayılan) extra_body gönderir; --think göndermez.
    ok = ScriptedClient(lambda *_: make_response(findings_json()))
    call_llm(ok, "m", [{"role": "user", "content": "{}"}] * 2, max_tokens=10, no_think=True,
             max_retries=0, sleep=quiet)
    call_llm(ok, "m", [{"role": "user", "content": "{}"}] * 2, max_tokens=10, no_think=False,
             max_retries=0, sleep=quiet)
    check("--no-think: extra_body enable_thinking=false", NO_THINK_EXTRA_BODY, ok.requests[0].get("extra_body"))
    check("--think: extra_body yok", None, ok.requests[1].get("extra_body"))
    check("response_format=json_object", {"type": "json_object"}, ok.requests[0].get("response_format"))

    # HTTP 400/422: iş durur; mesaj iki olasılığı söyler; gövde/anahtar sızmaz.
    for status in (400, 422):
        bad = ScriptedClient(lambda *_a, s=status: (_ for _ in ()).throw(http_error(s)))
        try:
            call_llm(bad, "m", [{}, {"content": "{}"}], max_tokens=10, no_think=True, max_retries=3, sleep=quiet)
            stop = None
        except BatchStop as exc:
            stop = exc
        message = stop.message if stop else ""
        check(f"HTTP {status} -> iş durur", "sunucu_istegi_reddetti", stop.code if stop else None)
        check(f"HTTP {status} mesajı (kod, tür, iki olasılık)", True,
              all(s in message for s in (f"HTTP {status}", "BadRequestError", "enable_thinking", "--think",
                                         "response_format=json_object")))
        check(f"HTTP {status}: gövde/anahtar mesajda yok", True, "SECRET" not in message and "body" not in message)
        check(f"HTTP {status}: yeniden denenmedi", 1, len(bad.requests))

    # Geçici hata: yeniden dener, sonra yalnızca aday hatası.
    sleeps = []
    timeout = ScriptedClient(lambda *_: (_ for _ in ()).throw(type("APITimeoutError", (Exception,), {})()))
    try:
        call_llm(timeout, "m", [{}, {"content": "{}"}], max_tokens=10, no_think=True, max_retries=3,
                 sleep=sleeps.append)
        got = None
    except CandidateError as exc:
        got = (exc.code, exc.attempts)
    check("zaman aşımı: 3 yeniden deneme sonra aday hatası", ("api_hatasi", 4), got)
    check("artan bekleme", [1, 2, 4], sleeps)

    # state.jsonl: yarım son satır çökertmez, uyarı loglanır; sonraki ekleme yeni satırda.
    state = Path(tmp) / "state.jsonl"
    good = {"aday_id": "a1", "pipeline_version": "v", "model": "m", "durum": "kaydedildi"}
    state.write_text(json.dumps(good) + "\n" + json.dumps({**good, "aday_id": "a2"}) + "\n"
                     + '{"aday_id": "a3", "pipeline_ver', encoding="utf-8")
    warnings = []
    entries = read_state(state, warnings.append)
    check("state: yarım son satır yok sayıldı", 2, len(entries))
    check("state: uyarı loglandı", True, len(warnings) == 1 and "satır 3" in warnings[0])
    JsonlWriter(state).write({**good, "aday_id": "a4"})
    warnings.clear()
    entries = read_state(state, warnings.append)
    check("state: yarım satırdan sonra ekleme ayrı satırda", ["a1", "a2", "a4"], sorted(k[0] for k in entries))

    # Kilit: ölü PID'li eski kilit temizlenir; canlı PID durdurur; başka makine durdurur; --unlock.
    lock = Path(tmp) / "lockdir" / "run.lock"
    lock.parent.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    import socket
    lock.write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname(), "started_at": "x"}), encoding="utf-8")
    warnings.clear()
    acquire_lock(lock, "test", warnings.append)
    check("kilit: ölü PID'li eski kilit otomatik temizlendi", True,
          json.loads(lock.read_text())["pid"] != dead.pid and any("eski kilit" in w for w in warnings))
    lock.write_text(json.dumps({"pid": dead.pid, "host": "baska-makine", "started_at": "x"}), encoding="utf-8")
    for label, info in (("canlı PID", {"pid": __import__("os").getpid(), "host": socket.gethostname()}),
                        ("başka makine", {"pid": dead.pid, "host": "baska-makine"})):
        lock.write_text(json.dumps({**info, "started_at": "x"}), encoding="utf-8")
        try:
            acquire_lock(lock, "test", quiet)
            got = "alındı"
        except BatchStop as exc:
            got = exc.code
        check(f"kilit: {label} -> durur", "kilitli", got)
    cli_main(["--unlock", "--base-dir", str(lock.parent)])
    check("--unlock kilidi siler", False, lock.exists())

    # Önbellek: göreli yol, ':' yok, anahtar uyuşmazlığı yok sayılır.
    aday = "clinical-candidate-v1:" + "ab" * 32
    write_cache(tmp, "v1", "Qwen/Qwen3-8B", aday, "{}", "stop", False)
    path = cache_path(tmp, "v1", "Qwen/Qwen3-8B", aday)
    check("önbellek yolu responses/<sürüm>/<model>/<hash>.json", ("responses", "v1", "Qwen__Qwen3-8B"),
          path.relative_to(tmp).parts[:3])
    check("önbellek dosya adında ':' yok", True, ":" not in path.name)
    data = json.loads(path.read_text(encoding="utf-8"))
    check("önbellek alanları", True, {"aday_id", "pipeline_version", "model", "raw_text"} <= set(data)
          and not any(Path(str(v)).is_absolute() for v in data.values() if isinstance(v, str) and len(v) > 2))
    data["model"] = "başka"
    path.write_text(json.dumps(data), encoding="utf-8")
    warnings.clear()
    check("önbellek: anahtar uyuşmazlığı yok sayılır", None, read_cache(tmp, "v1", "Qwen/Qwen3-8B", aday, warnings.append))


# ---------------------------------------------------------------------------
# B) Veritabanı gerektiren testler
# ---------------------------------------------------------------------------

PROTECTED = {"makaleler WHERE document_id IS NULL": 0, "tani_tedavi": 0, "sonuclar": 0,
             "semptom_iliskileri": 0, "icd10_who": 43, "tkd_kurallari": 15}


def db_counts():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        out = {}
        for table in ("klinik_bulgular", "halusinasyon_degerlendirme", *PROTECTED):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = cursor.fetchone()[0]
        return out
    finally:
        conn.close()


def tx_connection():
    from src.database import get_connection

    conn = get_connection()
    conn.autocommit = False
    conn.start_transaction()
    return conn


def opts(base, **kwargs):
    return Options(base_dir=Path(base), max_consecutive_errors=50, **kwargs)


def read_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.exists() else []


def pick_scenarios(prepared):
    sources, to_send, _, _ = prepared
    chunks_for = lambda c: [norm_ws(t) for _, t in sources.ctx.chunks.get((c["document_id"], c["sayfa"]), [])]

    def passes(item, finding):
        rows, rejections, _ = process_response(findings_json(finding), item, sources, model="m")
        return rows and not rejections and preview_rows(rows, item, sources)[0]

    picked = {}
    for item in to_send:
        cand, spans = item["candidate"], item["allowed_quote_spans"]
        good = next((f for f in (drug_finding(s) for s in spans) if f), None)
        if cand["kaynak_turu"] == "tablo":
            picked.setdefault("tablo", item)
            continue
        if not good or not passes(item, good):
            continue
        if len(spans) > 1 and drug_finding(cand["cumle"]):
            picked.setdefault("markdown", item)
        elif spans == [cand["cumle"]] and sum(norm_ws(cand["cumle"]) in c for c in chunks_for(cand)) >= 2:
            picked.setdefault("cok_chunk", item)
        else:
            for key in ("karisik", "json_bozuk", "yarim", "tani", "mudahale_yok", "gecerli"):
                if key not in picked:
                    picked[key] = item
                    break
    return picked


def scenario_responder(picked, prepared):
    sources = prepared[0]
    by_text = {item["candidate"]["cumle"]: (key, item) for key, item in picked.items()}

    def respond(user, *_):
        key, item = by_text[user["candidate_text"]]
        spans = item["allowed_quote_spans"]
        good = next((f for f in (drug_finding(s) for s in spans) if f), None)
        if key == "karisik":
            return make_response(findings_json(good, {**good, "evidence_quote": "metinde olmayan alıntı"}))
        if key == "json_bozuk":
            return make_response("<think>…</think> bu JSON değil")
        if key == "yarim":
            return make_response('{"findings": [', finish="length")
        if key == "tani":
            words = spans[0].split()
            return make_response(findings_json({**dict.fromkeys(FIELDS), "diagnosis": words[0],
                                                "diagnostic_method": words[-1], "evidence_quote": spans[0]}))
        if key == "mudahale_yok":
            return make_response(findings_json({**dict.fromkeys(FIELDS), "diagnosis": spans[0].split()[0],
                                                "evidence_quote": spans[0]}))
        if key == "markdown":
            whole = drug_finding(item["candidate"]["cumle"])
            return make_response(findings_json({**whole, "evidence_quote": item["candidate"]["cumle"]}, good))
        if key == "tablo":
            row = item["candidate"]["cumle"]
            cell = next(c.strip() for c in row.strip("|").split("|") if c.strip())
            return make_response(findings_json({**dict.fromkeys(FIELDS), "drug": cell, "evidence_quote": cell}))
        return make_response(findings_json(good))
    return respond


def test_database(tmp):
    before = db_counts()
    check("başlangıç: hoca tabloları", list(PROTECTED.values()), [before[t] for t in PROTECTED])

    prepared = prepare()
    sources, to_send, excluded, total = prepared
    reasons = {}
    for record in excluded:
        reasons[record["sebep"]] = reasons.get(record["sebep"], 0) + 1
    check("hazırlık: 660 = gönderilecek + ayrılan", 660, len(to_send) + len(excluded), total == 660)
    check("ayrılan: başlık", 24, reasons.get("baslik"))
    check("ayrılan: sayfa aşan", 18, reasons.get("sayfa_asan"))
    check("ayrılan: karmaşık tablo", 16, reasons.get("tablo_karmasik"))
    check("ayrılan: guvenli_parca_yok (bilgi)", "-", reasons.get("guvenli_parca_yok", 0), True)
    check("her ayrılanın sebebi ve aday_id'si var", True, all(r["sebep"] and r["aday_id"] for r in excluded))

    picked = pick_scenarios(prepared)
    check("senaryo adayları bulundu", 9, len(picked))
    ids = [item["candidate"]["aday_id"] for item in picked.values()]
    key_of = {item["candidate"]["aday_id"]: key for key, item in picked.items()}

    # --- B1: senaryolar (gerçek mod + sahte istemci), dış transaction'da ROLLBACK ---
    base1 = Path(tmp) / "b1"
    client = ScriptedClient(scenario_responder(picked, prepared))
    conn = tx_connection()
    try:
        summary = run_batch(opts(base1, aday=ids, force=True), client=client, connection=conn,
                            prepared=prepared, out=quiet)
    finally:
        conn.rollback()
        conn.close()
    run_dir = Path(summary["run_dir"])
    would = read_jsonl(run_dir / "would_save.jsonl")
    rejected = read_jsonl(run_dir / "rejected.jsonl")
    errors = read_jsonl(run_dir / "errors.jsonl")
    states = {k[0]: v["durum"] for k, v in read_state(base1 / "state.jsonl", quiet).items()}
    saved_keys = {key_of[w["aday_id"]] for w in would}
    rej = {(key_of[r["aday_id"]], r["hata_turu"]) for r in rejected}

    check("B1: iş tamamlandı (hatalar durdurmadı)", "tamamlandi", summary["durum"])
    check("B1: her aday için 1 çağrı", len(ids), len(client.requests))
    check("B1: with_options(max_retries=0)", 0, client.with_options_calls[0].get("max_retries"))
    check("karışık yanıt: geçerli bulgu kaydedilecek", True, "karisik" in saved_keys)
    check("karışık yanıt: geçersiz bulgu reddedildi", True, ("karisik", "alinti_birebir_degil") in rej)
    check("bozuk JSON: yalnız o aday hata", "json_gecersiz",
          next((e["hata"] for e in errors if key_of[e["aday_id"]] == "json_bozuk"), None))
    check("finish_reason=length: yalnız o aday hata", "yanit_tamamlanmadi",
          next((e["hata"] for e in errors if key_of[e["aday_id"]] == "yarim"), None))
    check("hatalı adayların durumu 'hata'", ["hata", "hata"],
          [states[picked[k]["candidate"]["aday_id"]] for k in ("json_bozuk", "yarim")])
    check("diagnostic_method kalıp dışı: yalnız bulgu reddi", True,
          any(k == "tani" and t.startswith("diagnostic_relationship_") for k, t in rej))
    check("mudahale_alani_yok: reddedildi ve ayrı sayıldı", True,
          ("mudahale_yok", "mudahale_alani_yok") in rej and summary["mudahale_alani_yok"] >= 1)
    check("markdown: parça dışı alıntı reddedildi", True, ("markdown", "guvenli_parca_disinda") in rej)
    check("markdown: parça içi alıntı validate_row'dan geçti", True, "markdown" in saved_keys)
    multi = next((w["satir"] for w in would if key_of[w["aday_id"]] == "cok_chunk"), None)
    if multi:
        cand = picked["cok_chunk"]["candidate"]
        holders = [cid for cid, t in sources.ctx.chunks[(cand["document_id"], cand["sayfa"])]
                   if norm_ws(multi["kanit_cumle"]) in norm_ws(t)]
        smallest = min(holders, key=lambda c: sources.chunk_index[c])
    check("çok chunk: en küçük chunk_index seçildi", True,
          bool(multi) and multi["chunk_id"] == smallest and len(holders) >= 2
          and "otomatik_ilk" in json.loads(multi["taslak_notu"])["otomatik_alanlar"]["chunk_id"])
    table_rows = [r.get("satir") for r in rejected + would if key_of[r["aday_id"]] == "tablo" and r.get("satir")]
    check("tablo: alıntı tam satıra genişletildi (işaretli)", True,
          bool(table_rows) and all(r["kanit_cumle"] == picked["tablo"]["candidate"]["cumle"]
                                   and json.loads(r["taslak_notu"])["kanit_genisletildi"] for r in table_rows))
    check("summary.json hata_turu sayıları", True,
          json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["reddedilen_hata_turu"].get(
              "mudahale_alani_yok", 0) >= 1)

    # --- B2: ilk çalıştırma kuralı ve API anahtarı ---
    fresh = Path(tmp) / "b2"
    never = ScriptedClient(lambda *_: make_response(findings_json()))
    summary = run_batch(opts(fresh), client=never, prepared=prepared, out=quiet)
    check("ilk çalıştırma: --limit yoksa durur", ("durduruldu", "ilk_calistirma"),
          (summary["durum"], summary.get("durma_sebebi")))
    check("ilk çalıştırma mesajı '--limit 20'", True, "--limit 20" in summary.get("mesaj", ""))
    check("ilk çalıştırma: hiç çağrı yok", 0, len(never.requests))
    for label, extra in (("--limit 2", {"limit": 2}), ("--force (tek aday)", {"force": True, "aday": ids[:1]})):
        base = Path(tmp) / ("b2" + label[2:5])
        summary = run_batch(opts(base, no_save=True, **extra), client=never, prepared=prepared, out=quiet)
        check(f"ilk çalıştırma: {label} ile başlar", "tamamlandi", summary["durum"])
    nokey = ScriptedClient(lambda *_: make_response(findings_json()), api_key="not-required")
    summary = run_batch(opts(Path(tmp) / "b2k", limit=1, no_save=True), client=nokey, prepared=prepared, out=quiet)
    check("API anahtarı yok: çağrısız durur", ("durduruldu", "api_anahtari", 0),
          (summary["durum"], summary.get("durma_sebebi"), len(nokey.requests)))

    # --- B3: 400 ile yarıda kalma -> kaldığı yerden devam; tekrar koşu; sürüm değişimi ---
    base3 = Path(tmp) / "b3"
    five = [i["candidate"]["aday_id"] for i in to_send[:5]]

    def fail_third(user, kwargs, n):
        if n == 3:
            raise http_error(400)
        return MockLLMClient()._create(**kwargs)
    first = ScriptedClient(fail_third)
    summary = run_batch(opts(base3, aday=five, force=True, no_save=True), client=first, prepared=prepared, out=quiet)
    check("400: iş açık mesajla durdu", ("durduruldu", "sunucu_istegi_reddetti"),
          (summary["durum"], summary.get("durma_sebebi")))
    check("400: mesaj sonraki adayı çağırmadı", 3, len(first.requests))
    second = ScriptedClient(lambda u, kw, n: MockLLMClient()._create(**kw))
    summary = run_batch(opts(base3, aday=five, no_save=True), client=second, prepared=prepared, out=quiet)
    check("devam: yalnız kalan 3 aday çağrıldı", 3, len(second.requests))
    third = ScriptedClient(lambda u, kw, n: MockLLMClient()._create(**kw))
    summary = run_batch(opts(base3, aday=five, no_save=True), client=third, prepared=prepared, out=quiet)
    check("tekrar koşu: 0 çağrı, 0 işlenecek", (0, 0), (len(third.requests), summary["islenecek"]))
    fourth = ScriptedClient(lambda u, kw, n: MockLLMClient()._create(**kw))
    run_batch(opts(base3, aday=five, no_save=True, pipeline_version="test-v2", force=True), client=fourth,
              prepared=prepared, out=quiet)
    check("sürüm değişince yeniden işlendi", 5, len(fourth.requests))

    # --- B4: --no-save -> yanit_alindi; sonra aynı makinede önbellekten kayıt ---
    base4 = Path(tmp) / "b4"
    six = [i["candidate"]["aday_id"] for i in to_send[:40]]
    mock_like = ScriptedClient(lambda u, kw, n: MockLLMClient()._create(**kw))
    mid = db_counts()
    summary = run_batch(opts(base4, aday=six, force=True, no_save=True), client=mock_like, prepared=prepared, out=quiet)
    states = read_state(base4 / "state.jsonl", quiet)
    would = read_jsonl(Path(summary["run_dir"]) / "would_save.jsonl")
    check("--no-save: veritabanı değişmedi", mid, db_counts())
    check("--no-save: kaydedilebilir satırı olanlar 'yanit_alindi'", True,
          bool(would) and all(states[(w["aday_id"], summary["pipeline_version"], summary["model"])]["durum"]
                              == "yanit_alindi" for w in would))
    check("--no-save: hiçbiri 'kaydedildi' değil", True, all(v["durum"] != "kaydedildi" for v in states.values()))
    check("--no-save: summary.json dolu", True, summary["sayaclar"].get("would_save") == len(would))
    exploding = ScriptedClient(lambda *_: (_ for _ in ()).throw(AssertionError("LLM çağrılmamalı")))
    conn = tx_connection()
    try:
        summary = run_batch(opts(base4, aday=six), client=exploding, connection=conn, prepared=prepared, out=quiet)
    finally:
        conn.rollback()
        conn.close()
    states = read_state(base4 / "state.jsonl", quiet)
    check("sonraki kayıt koşusu: LLM çağrılmadı", 0, len(exploding.requests))
    check("sonraki kayıt koşusu: önbellekten yazıldı", len(would), summary["sayaclar"].get("yazilan"))
    check("sonraki kayıt koşusu: durum 'kaydedildi'", True,
          all(states[(w["aday_id"], summary["pipeline_version"], summary["model"])]["durum"] == "kaydedildi"
              for w in would))

    # --- B5: --mock gerçek veritabanına kalıcı yazmaz ---
    base5 = Path(tmp) / "b5"
    mid = db_counts()
    summary = run_batch(opts(base5, mock=True, limit=15), prepared=prepared, out=quiet)
    check("--mock: satır yazdı (transaction içinde)", True, summary["sayaclar"].get("yazilan", 0) > 0)
    check("--mock: sonra veritabanı aynı (ROLLBACK)", mid, db_counts())
    check("--mock: özet notu", True, any("mock: veritabanına yazılmadı" in n for n in summary["notlar"]))
    check("--mock: gerçek state/önbellek dokunulmadı", (False, False),
          ((base5 / "state.jsonl").exists(), (base5 / "responses").exists()))

    # --- B6: --from-cache iki 'bilgisayarda' aynı satırları üretir ---
    base_a = Path(tmp) / "pc_a"
    eleven = [i["candidate"]["aday_id"] for i in to_send[40:80]]
    run_batch(opts(base_a, aday=eleven, model="det-model", no_save=True, force=True),
              client=ScriptedClient(lambda u, kw, n: MockLLMClient()._create(**kw)), prepared=prepared, out=quiet)
    produced = []
    for name in ("pc_b", "pc_c"):
        target = Path(tmp) / name
        shutil.copytree(base_a / "responses", target / "responses")
        extra = [i["candidate"]["aday_id"] for i in to_send[80:83]]   # önbellekte yok
        conn = tx_connection()
        try:
            summary = run_batch(opts(target, aday=eleven + extra, from_cache=True), connection=conn,
                                prepared=prepared, out=quiet)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM klinik_bulgular WHERE taslak_notu LIKE %s ORDER BY satir_hash",
                           ('%"kaynak": "clinical_pipeline"%',))
            rows = [{k: v for k, v in r.items() if k not in ("id", "created_at")} for r in cursor.fetchall()]
            cursor.close()
        finally:
            conn.rollback()
            conn.close()
        produced.append((summary, rows))
    (sb, rows_b), (sc, rows_c) = produced
    check("--from-cache: model önbellekten çözüldü, API anahtarı istenmedi", ("tamamlandi", "det-model"),
          (sb["durum"], sb["model"]))
    check("--from-cache: LLM çağrısı yok", 0, sb["sayaclar"].get("llm_cagrisi", 0))
    check("--from-cache: önbellekte olmayan 'onbellekte_yok'", 3, sb["sayaclar"].get("onbellekte_yok"))
    check("--from-cache: iki bilgisayarda aynı satırlar", True, bool(rows_b) and rows_b == rows_c)
    check("--from-cache: taslak_notu'nda run_id yok", True, all("run_id" not in json.loads(r["taslak_notu"])
                                                                 for r in rows_b))

    # --- B7: --dry-run ---
    mid = db_counts()
    counting = ScriptedClient(lambda *_: make_response(findings_json()))
    summary = run_batch(opts(Path(tmp) / "b7", dry_run=True), client=counting, prepared=prepared, out=quiet)
    check("--dry-run: veritabanı ve çağrı yok", (mid, 0), (db_counts(), len(counting.requests)))
    check("--dry-run: state yazılmadı", False, (Path(tmp) / "b7" / "state.jsonl").exists())

    # --- B8: sahte insan onayı yok; hepsi pending; otomatik işaretli ---
    texts = "\n".join(p.read_text(encoding="utf-8") for p in Path(tmp).rglob("*.json*"))
    check("hiçbir çıktıda onay alanı yok", True,
          not any(f'"{s}":' in texts for s in ("approval_status", "reviewer_id", "review_source")))
    all_rows = [json.loads(line)["satir"] for p in Path(tmp).rglob("would_save.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines()]
    check("bütün satırlar review_status='pending'", True,
          bool(all_rows) and all(r["review_status"] == "pending" for r in all_rows))
    check("otomatik alanlar taslak_notu'nda işaretli", True,
          all("otomatik" in json.loads(r["taslak_notu"])["otomatik_alanlar"]["mudahale_turu"] for r in all_rows))
    check("API anahtarı dosyalarda yok", True, "test-only-key" not in texts and "SECRET" not in texts)

    after = db_counts()
    check("sonda: klinik_bulgular / halusinasyon / hoca tabloları değişmedi", before, after)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "offline").mkdir()
        test_offline(Path(tmp) / "offline")
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)

    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<24}  {'Bulunan':<40}  Sonuç")
    print("-" * (width + 78))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:24]:<24}  {act[:40]:<40}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
