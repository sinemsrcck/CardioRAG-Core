"""
Klinik çıkarım toplu işi (660 aday -> klinik_bulgular, review_status='pending').

Çalıştırma örnekleri:
    python -m src.run_clinical_batch --dry-run
    python -m src.run_clinical_batch --mock --limit 20          # veritabanına yazmaz
    python -m src.run_clinical_batch --limit 20 --no-save       # gerçek LLM, yalnızca önbellek + önizleme
    python -m src.run_clinical_batch --from-cache               # LLM yok; önbellekten kayıt

Çıktılar data/review/clinical_batch/ altında (git'e girmez):
    state.jsonl                     aday bazında ilerleme (devam etme)
    responses/<sürüm>/<model>/      taşınabilir LLM yanıt önbelleği
    runs/<run_id>/                  summary.json, would_save.jsonl, rejected.jsonl,
                                    excluded.jsonl, errors.jsonl, warnings.jsonl
    run.lock                        aynı anda tek iş

API anahtarı ve şifre yalnızca .env'den okunur; hiçbir dosyaya yazılmaz.
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.clinical_pipeline import (
    PIPELINE_VERSION,
    PROMPT_SHA,
    BatchStop,
    CandidateError,
    MockLLMClient,
    build_messages,
    call_llm,
    load_sources,
    order_round_robin,
    prepare_candidates,
    preview_rows,
    process_response,
    response_text,
)

DEFAULT_BASE_DIR = Path("data/review/clinical_batch")
TERMINAL = {"kaydedildi", "bulgu_yok", "reddedildi"}


@dataclass
class Options:
    limit: int = None
    makale: list = field(default_factory=list)
    aday: list = field(default_factory=list)
    dry_run: bool = False
    mock: bool = False
    from_cache: bool = False
    no_save: bool = False
    retry_failed: bool = False
    reprocess: bool = False
    force: bool = False
    think: bool = False
    model: str = None
    timeout: float = 60.0
    max_tokens: int = 2400
    max_tokens_param: str = "max_completion_tokens"
    max_retries: int = 3
    max_consecutive_errors: int = 5
    base_dir: Path = DEFAULT_BASE_DIR
    pipeline_version: str = PIPELINE_VERSION   # testlerde sürüm değişimi için


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSONL yardımcıları
# ---------------------------------------------------------------------------

class JsonlWriter:
    """Satır ekler; dosya newline ile bitmiyorsa (yarım satır) önce newline koyar."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as file:
                file.seek(-1, os.SEEK_END)
                needs_newline = file.read(1) != b"\n"
            if needs_newline:
                with self.path.open("ab") as file:
                    file.write(b"\n")

    def write(self, record):
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())


def read_state(path, warn):
    """{(aday_id, sürüm, model): son kayıt}. Bozuk satırlar atlanır ve uyarı loglanır."""
    entries = {}
    path = Path(path)
    if not path.exists():
        return entries
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = (record["aday_id"], record["pipeline_version"], record["model"])
                record["durum"]
            except (ValueError, KeyError, TypeError):
                warn(f"state.jsonl satır {number} bozuk veya yarım; yok sayıldı")
                continue
            entries[key] = record
    return entries


# ---------------------------------------------------------------------------
# Kilit
# ---------------------------------------------------------------------------

def pid_alive(pid):
    """Süreç çalışıyor mu? Windows'ta os.kill(pid, 0) süreci ÖLDÜRÜR; ctypes kullanılır."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # erişim reddi: süreç var
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(path, run_id, warn):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": os.getpid(), "host": socket.gethostname(), "started_at": _now(), "run_id": run_id}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            same_host = existing.get("host") == info["host"]
            if same_host and not pid_alive(existing.get("pid")):
                path.unlink(missing_ok=True)
                warn(f"eski kilit temizlendi (pid {existing.get('pid')} çalışmıyor, "
                     f"başlangıç {existing.get('started_at')})")
                continue
            raise BatchStop("kilitli", (
                f"Başka bir iş çalışıyor olabilir (pid {existing.get('pid')}, "
                f"makine {existing.get('host')}, başlangıç {existing.get('started_at')}). "
                "Çalışmadığından eminseniz: python -m src.run_clinical_batch --unlock"))
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(info, file)
        return
    raise BatchStop("kilitli", "Kilit alınamadı.")


def release_lock(path):
    path = Path(path)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Taşınabilir yanıt önbelleği
# ---------------------------------------------------------------------------

def model_slug(model):
    return re.sub(r"[^A-Za-z0-9._-]", "_", model.replace("/", "__"))


def cache_path(base, version, model, aday_id):
    """Göreli yapı: responses/<sürüm>/<model>/<aday hash>.json (':' Windows'ta geçersiz)."""
    return Path(base) / "responses" / version / model_slug(model) / (aday_id.split(":")[-1] + ".json")


def read_cache(base, version, model, aday_id, warn):
    path = cache_path(base, version, model, aday_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        warn(f"önbellek dosyası okunamadı, yok sayıldı: {path.name}")
        return None
    if (not isinstance(data, dict) or data.get("aday_id") != aday_id
            or data.get("pipeline_version") != version or data.get("model") != model
            or not isinstance(data.get("raw_text"), str)):
        warn(f"önbellek dosyası anahtarla uyuşmuyor, yok sayıldı: {path.name}")
        return None
    return data


def write_cache(base, version, model, aday_id, raw_text, finish_reason, think):
    path = cache_path(base, version, model, aday_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"aday_id": aday_id, "pipeline_version": version, "model": model,
            "prompt_sha": PROMPT_SHA, "think": think, "finish_reason": finish_reason,
            "raw_text": raw_text, "created_at": _now()}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def cached_models(base, version):
    root = Path(base) / "responses" / version
    models = set()
    if root.exists():
        for folder in root.iterdir():
            sample = next(folder.glob("*.json"), None)
            if sample:
                try:
                    models.add(json.loads(sample.read_text(encoding="utf-8"))["model"])
                except (OSError, ValueError, KeyError):
                    pass
    return models


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

def prepare(sources=None):
    """(sources, gönderilecek, ayrılan). Testler sonucu tekrar kullanabilir."""
    from src.clinical_candidates import collect_candidates
    from src.database import get_connection

    if sources is None:
        conn = get_connection()
        try:
            sources = load_sources(conn)
        finally:
            conn.close()
    candidates = collect_candidates(sources.articles)
    to_send, excluded = prepare_candidates(candidates, sources)
    return sources, to_send, excluded, len(candidates)


def _select(to_send, opts):
    items = to_send
    if opts.makale:
        wanted = [m.casefold() for m in opts.makale]
        items = [i for i in items if any(i["candidate"]["makale_id"].casefold() == w
                                         or i["candidate"]["dosya"].casefold().startswith(w) for w in wanted)]
    if opts.aday:
        items = [i for i in items if i["candidate"]["aday_id"] in set(opts.aday)]
    return order_round_robin(items)


def _resolve_model(opts, base):
    if opts.mock:
        return MockLLMClient.model
    if opts.model:
        return opts.model
    if opts.from_cache:
        models = cached_models(base, opts.pipeline_version)
        if not models:
            raise BatchStop("onbellek_bos", f"Önbellekte bu sürüm için yanıt yok: "
                            f"{base / 'responses' / opts.pipeline_version}")
        if len(models) > 1:
            raise BatchStop("model_belirsiz", f"Önbellekte birden fazla model var ({sorted(models)}); "
                            "--model verin.")
        return models.pop()
    from src.rag import DEFAULT_LLM
    return DEFAULT_LLM


def _real_client(opts, client):
    from src.clinical_extraction import _require_api_key

    if client is None:
        from src.rag import get_llm_client
        try:
            client = get_llm_client()
        except Exception as exc:  # noqa: BLE001
            raise BatchStop("istemci_ayari", f"LLM istemcisi oluşturulamadı ({type(exc).__name__}). "
                            ".env'de LLM_BASE_URL ve LLM_API_KEY'i kontrol edin.") from None
    try:
        _require_api_key(client)
    except ValueError as exc:
        raise BatchStop("api_anahtari", f"LLM API anahtarı eksik veya geçersiz ({exc}). "
                        ".env'de LLM_API_KEY doldurulmalı (yerel sunucu anahtarsızsa herhangi bir "
                        "değer, ör. 'local').") from None
    return client, client.with_options(max_retries=0, timeout=float(opts.timeout))


def run_batch(opts, *, client=None, connection=None, prepared=None, sleep=time.sleep, out=print):
    """Toplu işi çalıştırır ve özet sözlüğünü döndürür.

    connection verilirse yazımlar onun içinde SAVEPOINT ile yapılır ve COMMIT
    EDİLMEZ (çağıran rollback/commit eder). --mock kendi bağlantısını açar ve
    sonunda HER ZAMAN ROLLBACK yapar.
    """
    if opts.mock and opts.from_cache:
        raise BatchStop("secenek", "--mock ve --from-cache birlikte kullanılamaz.")
    if opts.dry_run and opts.no_save:
        raise BatchStop("secenek", "--dry-run zaten yazmaz; --no-save ile birlikte kullanılamaz.")

    started = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    base = Path(opts.base_dir) / ("mock" if opts.mock else "")
    run_dir = base / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    warnings_log = JsonlWriter(run_dir / "warnings.jsonl")

    def warn(message):
        warnings_log.write({"zaman": _now(), "uyari": message})
        out(f"UYARI: {message}")

    summary = {
        "run_id": run_id, "baslangic": _now(), "pipeline_version": opts.pipeline_version,
        "prompt_sha": PROMPT_SHA, "mod": ("dry-run" if opts.dry_run else "mock" if opts.mock
                                          else "from-cache" if opts.from_cache else "llm"),
        "no_save": opts.no_save, "think": opts.think, "notlar": [], "durum": "basladi",
    }
    lock_path = Path(opts.base_dir) / "run.lock"
    lock_held = False
    write_conn = None
    own_write_conn = False
    counters = Counter()
    rejected_types, excluded_reasons, error_types = Counter(), Counter(), Counter()
    try:
        if prepared is None:
            prepared = prepare()
        sources, to_send, excluded, total = prepared
        model = _resolve_model(opts, base)
        summary.update(model=model, toplam_aday=total, llm_e_gidecek=len(to_send),
                       ayrilan=len(excluded))
        excluded_reasons.update(e["sebep"] for e in excluded)
        excluded_log = JsonlWriter(run_dir / "excluded.jsonl")
        for record in excluded:
            excluded_log.write(record)

        state_path = (run_dir if opts.mock else base) / "state.jsonl"
        state = read_state(state_path, warn)
        version_seen = any(k[1] == opts.pipeline_version and k[2] == model for k in state)

        selected = _select(to_send, opts)
        work, skipped = [], Counter()
        for item in selected:
            last = state.get((item["candidate"]["aday_id"], opts.pipeline_version, model))
            status = last["durum"] if last else None
            if not opts.reprocess:
                if status in TERMINAL or (status == "yanit_alindi" and opts.no_save):
                    skipped[status] += 1
                    continue
                if status == "hata" and not opts.retry_failed:
                    skipped[status] += 1
                    continue
            work.append((item, status))
        if opts.limit is not None:
            work = work[:opts.limit]
        summary.update(secilen=len(selected), onceden_islenmis_atlanan=dict(skipped), islenecek=len(work))

        if opts.dry_run:
            hits = sum(read_cache(base, opts.pipeline_version, model, i["candidate"]["aday_id"],
                                  lambda _m: None) is not None for i, _ in work)
            summary.update(onbellekte_var=hits, onbellekte_yok=len(work) - hits, durum="tamamlandi")
            summary["notlar"].append("dry-run: LLM çağrılmadı, veritabanına yazılmadı")
            return summary

        real_llm = not opts.mock and not opts.from_cache
        if real_llm and not version_seen and opts.limit is None and not opts.force:
            raise BatchStop("ilk_calistirma", (
                f"Bu sürümle ({opts.pipeline_version}, model {model}) hiç aday işlenmemiş. "
                "Önce --limit 20 ile deneyin (aşmak için --force)."))

        acquire_lock(lock_path, run_id, warn)
        lock_held = True

        request_client = None
        if opts.mock:
            client = client or MockLLMClient()
            request_client = client.with_options(max_retries=0, timeout=float(opts.timeout))
        elif real_llm:
            client, request_client = _real_client(opts, client)

        if not opts.no_save:
            if connection is not None:
                write_conn = connection
            else:
                from src.database import get_connection
                write_conn, own_write_conn = get_connection(), True
                write_conn.autocommit = False
                if opts.mock:
                    write_conn.start_transaction()
        from src.database import save_clinical_findings

        state_log = JsonlWriter(state_path)
        would_save_log = JsonlWriter(run_dir / "would_save.jsonl")
        rejected_log = JsonlWriter(run_dir / "rejected.jsonl")
        errors_log = JsonlWriter(run_dir / "errors.jsonl")
        consecutive_errors = 0

        def record_state(item, status, **detail):
            state_log.write({"aday_id": item["candidate"]["aday_id"], "pipeline_version": opts.pipeline_version,
                             "model": model, "durum": status, "run_id": run_id, "zaman": _now(), **detail})

        for number, (item, previous) in enumerate(work, start=1):
            candidate = item["candidate"]
            aday_id = candidate["aday_id"]
            try:
                cached = read_cache(base, opts.pipeline_version, model, aday_id, warn)
                if cached is not None and real_llm and previous == "hata" and opts.retry_failed:
                    cached = None  # hatalı yanıtı yeniden iste
                if cached is not None:
                    raw_text = cached["raw_text"]
                    counters["onbellekten"] += 1
                elif opts.from_cache:
                    counters["onbellekte_yok"] += 1
                    continue
                else:
                    response, attempts = call_llm(
                        request_client, model, build_messages(item), max_tokens=opts.max_tokens,
                        no_think=not opts.think, max_retries=opts.max_retries, sleep=sleep,
                        token_param=opts.max_tokens_param)
                    counters["llm_cagrisi"] += attempts
                    raw_text, finish = response_text(response)
                    write_cache(base, opts.pipeline_version, model, aday_id, raw_text, finish, opts.think)

                rows, rejections, n_findings = process_response(raw_text, item, sources, model=model)
                passed, failed = preview_rows(rows, item, sources)
                rejections += failed
                counters["bulgu"] += n_findings
                for record in rejections:
                    rejected_types[record["hata_turu"]] += 1
                    if record["hata_turu"] == "deger_kumesi_disi" and str(record.get("aciklama", "")).startswith(
                            "clinical_relationship_"):
                        rejected_types["  (iliski: " + record["aciklama"].split(":")[0] + ")"] += 1
                    rejected_log.write({**record, "run_id": run_id})
                for row in passed:
                    would_save_log.write({"aday_id": aday_id, "dosya": candidate["dosya"], "satir": row})
                counters["would_save"] += len(passed)

                if not passed:
                    status = "bulgu_yok" if n_findings == 0 else "reddedildi"
                    record_state(item, status, bulgu=n_findings, reddedilen=len(rejections))
                elif opts.no_save:
                    record_state(item, "yanit_alindi", bulgu=n_findings, kaydedilebilir=len(passed),
                                 reddedilen=len(rejections))
                else:
                    try:
                        result = save_clinical_findings(passed, "rag", connection=write_conn)
                        if own_write_conn and not opts.mock:
                            write_conn.commit()
                    except Exception:
                        if own_write_conn and not opts.mock:
                            write_conn.rollback()
                        raise
                    for record in result["reddedilen"]:
                        rejected_types[record["hata_turu"]] += 1
                        rejected_log.write({"aday_id": aday_id, "asama": "save_clinical_findings",
                                            "hata_turu": record["hata_turu"], "aciklama": record["aciklama"],
                                            "satir": record["satir"], "run_id": run_id})
                    counters["yazilan"] += result["yazilan"]
                    counters["zaten_var"] += result["zaten_var"]
                    record_state(item, "kaydedildi", bulgu=n_findings, yazilan=result["yazilan"],
                                 zaten_var=result["zaten_var"], reddedilen=len(rejections) + len(result["reddedilen"]))
                counters["islenen"] += 1
                consecutive_errors = 0
            except CandidateError as exc:
                counters["hata"] += 1
                error_types[exc.code] += 1
                errors_log.write({"aday_id": aday_id, "dosya": candidate["dosya"], "hata": exc.code,
                                  "detay": exc.detail, "http_status": exc.http_status,
                                  "deneme": exc.attempts, "run_id": run_id, "zaman": _now()})
                record_state(item, "hata", hata=exc.code)
                consecutive_errors += 1
            except BatchStop:
                raise
            except Exception as exc:  # noqa: BLE001 - veritabanı vb.; aday hatası olarak kaydedilir
                counters["hata"] += 1
                error_types[type(exc).__name__] += 1
                errors_log.write({"aday_id": aday_id, "dosya": candidate["dosya"], "hata": type(exc).__name__,
                                  "run_id": run_id, "zaman": _now()})
                record_state(item, "hata", hata=type(exc).__name__)
                consecutive_errors += 1
            if consecutive_errors >= opts.max_consecutive_errors:
                raise BatchStop("ust_uste_hata", f"Üst üste {consecutive_errors} aday hata verdi; iş durdu. "
                                "errors.jsonl'a bakın; düzelttikten sonra --retry-failed ile devam edin.")
            if number % 25 == 0:
                out(f"  {number}/{len(work)} aday işlendi")
        summary["durum"] = "tamamlandi"
    except BatchStop as stop:
        summary.update(durum="durduruldu", durma_sebebi=stop.code, mesaj=stop.message)
    except KeyboardInterrupt:
        summary.update(durum="kesildi", mesaj="Kullanıcı kesti; aynı komutla kaldığı yerden devam eder.")
    finally:
        if write_conn is not None and own_write_conn:
            try:
                if opts.mock:
                    write_conn.rollback()
                else:
                    write_conn.rollback()  # commit edilmemiş yarım aday kalmasın
            finally:
                write_conn.close()
        if lock_held:
            release_lock(lock_path)
        if opts.mock:
            summary["notlar"].append("mock: veritabanına yazılmadı (tek transaction, sonunda ROLLBACK)")
        if opts.no_save:
            summary["notlar"].append("no-save: veritabanına yazılmadı; önizleme would_save.jsonl'da")
        if connection is not None and not opts.no_save and not opts.dry_run:
            summary["notlar"].append("dış bağlantı: commit edilmedi (çağıran yönetir)")
        if opts.from_cache and counters["onbellekte_yok"] and counters["onbellekte_yok"] >= max(
                1, (summary.get("islenecek") or 0) // 2):
            summary["notlar"].append("Adayların yarısından fazlası önbellekte yok: PDF'ler veya kod sürümü "
                                     "farklı olabilir.")
        summary.update(
            sayaclar=dict(counters), ayrilan_sebep=dict(excluded_reasons),
            reddedilen_hata_turu=dict(rejected_types), hata_turu=dict(error_types),
            mudahale_alani_yok=rejected_types.get("mudahale_alani_yok", 0),
            sure_sn=round(time.monotonic() - started, 1), bitis=_now(), run_dir=str(run_dir),
        )
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def print_summary(summary, out=print):
    out("")
    out(f"Durum: {summary['durum']}" + (f" ({summary.get('durma_sebebi')})" if summary.get("durma_sebebi") else ""))
    if summary.get("mesaj"):
        out(f"  {summary['mesaj']}")
    out(f"Mod: {summary['mod']}{' + no-save' if summary.get('no_save') else ''}   "
        f"Sürüm: {summary['pipeline_version']}   Model: {summary.get('model')}")
    if "toplam_aday" in summary:
        out(f"Aday: {summary['toplam_aday']} toplam, {summary['llm_e_gidecek']} LLM'e gidebilir, "
            f"{summary['ayrilan']} ayrıldı {summary.get('ayrilan_sebep')}")
        out(f"Seçilen: {summary.get('secilen')}  önceden işlenmiş (atlandı): "
            f"{summary.get('onceden_islenmis_atlanan')}  bu koşuda işlenecek: {summary.get('islenecek')}")
    if summary["mod"] == "dry-run":
        out(f"Önbellekte yanıtı olan: {summary.get('onbellekte_var')}, olmayan: {summary.get('onbellekte_yok')}")
    else:
        out(f"Sayaçlar: {summary.get('sayaclar')}")
        out(f"Reddedilen bulgular (hata_turu): {summary.get('reddedilen_hata_turu')}")
        out(f"Aday hataları: {summary.get('hata_turu')}")
    for note in summary.get("notlar", []):
        out(f"Not: {note}")
    out(f"Çıktılar: {summary.get('run_dir')}  (süre {summary.get('sure_sn')} sn)")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.run_clinical_batch",
                                     description="Klinik çıkarım toplu işi (sonuçlar review_status='pending').")
    parser.add_argument("--limit", type=int, help="bu koşuda en fazla N aday işle")
    parser.add_argument("--makale", action="append", default=[], help="makale_id veya dosya adı öneki (tekrarlanabilir)")
    parser.add_argument("--aday", action="append", default=[], help="yalnızca bu aday_id (tekrarlanabilir)")
    parser.add_argument("--dry-run", action="store_true", help="yalnızca hazırlık raporu; LLM ve veritabanı yazımı yok")
    parser.add_argument("--mock", action="store_true", help="sahte LLM; veritabanı yazımı sonunda geri alınır")
    parser.add_argument("--from-cache", action="store_true", help="LLM çağırmaz, API anahtarı istemez; yalnızca önbellek")
    parser.add_argument("--no-save", action="store_true", help="LLM + önbellek + önizleme; veritabanına yazmaz")
    parser.add_argument("--retry-failed", action="store_true", help="'hata' durumundaki adayları yeniden dene")
    parser.add_argument("--reprocess", action="store_true", help="bu sürümle işlenmiş adayları yeniden işle")
    parser.add_argument("--force", action="store_true", help="ilk çalıştırma (--limit) kuralını aş")
    parser.add_argument("--unlock", action="store_true", help="kilit dosyasını sil ve çık")
    parser.add_argument("--think", action=argparse.BooleanOptionalAction, default=False,
                        help="Qwen3 thinking (varsayılan kapalı: --no-think, enable_thinking=false gönderilir)")
    parser.add_argument("--model", help="model adı (varsayılan .env LLM_MODEL; --from-cache'te önbellekteki tek model)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument("--max-tokens-param", choices=("max_completion_tokens", "max_tokens"),
                        default="max_completion_tokens",
                        help="token sınırı parametresinin adı (eski sunucular max_tokens ister)")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-consecutive-errors", type=int, default=5)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.unlock:
        lock = Path(args.base_dir) / "run.lock"
        if lock.exists():
            lock.unlink()
            print(f"Kilit silindi: {lock}")
        else:
            print("Kilit yok.")
        return 0
    opts = Options(limit=args.limit, makale=args.makale, aday=args.aday, dry_run=args.dry_run,
                   mock=args.mock, from_cache=args.from_cache, no_save=args.no_save,
                   retry_failed=args.retry_failed, reprocess=args.reprocess, force=args.force,
                   think=args.think, model=args.model, timeout=args.timeout, max_tokens=args.max_tokens,
                   max_tokens_param=args.max_tokens_param,
                   max_retries=args.max_retries, max_consecutive_errors=args.max_consecutive_errors,
                   base_dir=args.base_dir)
    try:
        summary = run_batch(opts)
    except BatchStop as stop:
        print(f"Durduruldu ({stop.code}): {stop.message}")
        return 2
    print_summary(summary)
    return 0 if summary["durum"] == "tamamlandi" else 2


if __name__ == "__main__":
    sys.exit(main())
