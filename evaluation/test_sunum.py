"""
Toplantı hazırlığı testleri: yeni çıkarım alanları, json_mode, reddedilenler tablosu,
sunum VIEW'ları, kaynak_bul procedure'ü, metaveri iletimi.

Çalıştırma:
    python -m evaluation.test_sunum

A) Veritabanı gerektirmeyenler: sample_size / effect_measure / ICD eşlemeleri, yeni
   alanların birebir kuralı, tablo bulgusunda yeni alanların yazılmaması, json_mode,
   red kaydı hash'inin determinizmi, önlem eşlemesinin kapsamı.
B) MySQL gerektirenler: sunum şeması yoksa transaction DIŞINDA kurulur (DDL örtük
   commit yapar). Bütün yazımlar transaction içinde ve ROLLBACK ile biter; --mock
   kendi ROLLBACK'ini yapar. Sonda klinik_bulgular, cikarim_reddedilenler, kalite
   tabloları, halusinasyon_degerlendirme ve hoca tabloları başlangıçtakiyle aynı.
   Veriden bağımsızlık: red kayıtları ve --mock teste özgü bir pipeline_version kullanır
   (aynı sürümle kayıtlı gerçek koşu INSERT IGNORE sayaçlarını sıfırlamasın);
   hasta_sayisi için f00bddb hasta seçimi kuralını geçen ilk gerçek aday seçilir.
"""

import json
import re
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.clinical_candidates import TREATMENT_REGEX, table_contexts
from src.clinical_pipeline import (
    PIPELINE_FIELDS,
    PIPELINE_VERSION,
    BatchStop,
    MockLLMClient,
    build_row,
    call_llm,
    lookup_icd10,
    map_effect_measure,
    parse_sample_size,
    preview_rows,
    process_response,
    validate_finding,
)
from src.clinical_validation import ValidationContext, validate_row
from src.run_clinical_batch import Options, read_cache, run_batch, write_cache
from src.sunum import ONLEM_ESLEME, rejection_record

results = []


def check(name, expected, actual, passed=None):
    if passed is None:
        passed = expected == actual
    results.append((name, str(expected), str(actual), bool(passed)))


def quiet(*_):
    pass


def make_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


class ScriptedClient:
    def __init__(self, responder, api_key="test-only-key"):
        self.api_key, self.responder, self.requests = api_key, responder, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **_):
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responder(json.loads(kwargs["messages"][1]["content"]), kwargs)


BLANK = dict.fromkeys(PIPELINE_FIELDS)


def findings_json(*findings):
    return json.dumps({"findings": [f for f in findings if f]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# A) Veritabanı gerektirmeyenler
# ---------------------------------------------------------------------------

def test_offline(tmp):
    # sample_size: deterministik tamsayı ayrıştırma
    for raw, expected in (("n = 372", 372), ("N=372", 372), ("372 patients", 372), ("n = 1500 participants", 1500)):
        check(f"sample_size '{raw}' -> {expected}", expected, parse_sample_size(raw)[0])
    count, reason = parse_sample_size("1,234 patients")
    check("sample_size '1,234 patients' -> boş, sayı notta", (None, True), (count, "1234" in reason))
    for raw in ("two-thirds", "12.5%", "372-400 patients", "about 300"):
        check(f"sample_size '{raw}' -> boş", None, parse_sample_size(raw)[0])

    # effect_measure: sabit eşleme
    for raw, expected in (("hazard ratio", "HR"), ("Adjusted Odds Ratio", "OR"), ("RR", "RR"),
                          ("incidence rate ratio", "IRR"), ("AUC", "AUC / C-istatistigi"),
                          ("C-statistic", "AUC / C-istatistigi"), ("risk difference", None), ("mean", None)):
        check(f"effect_measure '{raw}' -> {expected}", expected, map_effect_measure(raw))

    # ICD: yalnız normalize birebir; birden fazla kod -> boş; fuzzy yok
    icd = [("I50", "Heart failure", "Kalp Yetmezliği"), ("I21", "Acute myocardial infarction", "Akut Miyokard Enfarktüsü"),
           ("X1", "Duplicate name", "a"), ("X2", "Duplicate name", "b")]
    check("ICD: İngilizce ad birebir", "I50", lookup_icd10("heart failure", icd)[0])
    check("ICD: Türkçe ad birebir (İ/ı normalize)", "I21", lookup_icd10("akut miyokard enfarktüsü", icd)[0])
    check("ICD: fuzzy yok", None, lookup_icd10("heart failure with reduced ejection fraction", icd)[0])
    code, reason = lookup_icd10("duplicate name", icd)
    check("ICD: birden fazla kod -> boş", (None, True), (code, "birden fazla" in (reason or "")))

    # Yeni alanlar alıntıda birebir olmalı (kural gevşemez)
    quote = "Apixaban reduced stroke (HR, 0.80 [95% CI, 0.61-1.05]; P = .01) in 372 patients."
    good = {**BLANK, "drug": "Apixaban", "p_value": "P = .01", "confidence_interval": "95% CI, 0.61-1.05",
            "sample_size": "372 patients", "effect_measure": "HR", "evidence_quote": quote}
    check("yeni alanlar birebir: kabul", "P = .01", validate_finding(good, quote)["p_value"])
    for name, bad in (("p_value", "P < .001"), ("confidence_interval", "95% CI, 0.5-0.9"),
                      ("sample_size", "400 patients"), ("effect_measure", "odds ratio")):
        try:
            validate_finding({**good, name: bad}, quote)
            got = "kabul"
        except ValueError as exc:
            got = str(exc)
        check(f"{name} alıntıda yok -> red", True, got.startswith(f"{name} must be null or verbatim"))
    try:
        validate_finding({**BLANK, "p_value": "P = .01", "evidence_quote": quote}, quote)
        got = "kabul"
    except ValueError as exc:
        got = str(exc)
    check("yalnız değer alanı (müdahalesiz) -> red", "Finding has no clinical fields", got)

    # Tablo: yeni alanlar sütunlara yazılmaz, nota konur; kalan bulgu tipli tablo kuralından geçer.
    table = ("|Variable|Ischemic event (n=372)|No ischemic event (n=853)|p value|\n"
             "|---|---|---|---|\n|HbA1c|7.1|5.5|0.01|")
    row = "|HbA1c|7.1|5.5|0.01|"
    ctx = ValidationContext(pages={"doc": {1: table}}, makale_by_doc={"doc": "article"},
                            chunks={("doc", 1): [("cid", table)]}, chunk_ids={"cid"},
                            chunk_versions={"cid": "v"}, active_versions={"doc": "v"})
    item = {"candidate": {"aday_id": "synthetic-table", "dosya": "fixture.pdf", "document_id": "doc",
                          "makale_id": "article", "sayfa": 1, "kaynak_turu": "tablo", "cumle": row,
                          "table_context": table_contexts(table)[row]}, "allowed_quote_spans": [row]}
    sources = SimpleNamespace(ctx=ctx, chunk_index={"cid": 0}, max_lengths={})
    finding = {**BLANK, "diagnostic_method": "HbA1c", "patient_group": "Ischemic event", "numeric_value": "7.1",
               "p_value": "0.01", "evidence_quote": row}
    rows, rejected, _ = process_response(findings_json(finding), item, sources, model="m")
    note = json.loads(rows[0]["taslak_notu"]) if rows else {}
    check("tablo + yeni alan: bulgu reddedilmez", (1, 0), (len(rows), len(rejected)))
    check("tablo + yeni alan: p sütuna yazılmaz, notta", (None, {"p_value": "0.01"}),
          (rows[0]["p"] if rows else "?", note.get("tablo_yazilmayan_alanlar", {}).get("degerler")))
    check("tablo + yeni alan: not metni ve otomatik işaret", (True, True),
          ("sütun kaynağı doğrulanamadığı için yazılmadı" in note.get("tablo_yazilmayan_alanlar", {}).get("not", ""),
           "tablo_yeni_alanlar" in note.get("otomatik_alanlar", {})))
    passed, failed = preview_rows(rows, item, sources)
    check("tablo + yeni alan: kalan bulgu validate_row'dan geçer", (1, 0), (len(passed), len(failed)))
    bad_rows, bad_rej, _ = process_response(findings_json({**finding, "p_value": "0.05"}), item, sources, model="m")
    check("tablo: satırda olmayan yeni alan -> red (kural gevşemez)", (0, "alan_alintida_yok"),
          (len(bad_rows), bad_rej[0]["hata_turu"] if bad_rej else None))

    # CCT: outcome_type='öneri' otomatik_alanlar'da işaretli
    cct_table = "|COR|LOE|Indications for CCT|\n|---|---|---|\n|IIa|B|CAD can be detected with CCT.|"
    cct_row = "|IIa|B|CAD can be detected with CCT.|"
    cct_ctx = ValidationContext(pages={"doc": {1: cct_table}}, makale_by_doc={"doc": "article"},
                                chunks={("doc", 1): [("cid", cct_table)]}, chunk_ids={"cid"},
                                chunk_versions={"cid": "v"}, active_versions={"doc": "v"})
    cct_item = {"candidate": {**item["candidate"], "cumle": cct_row, "table_context": table_contexts(cct_table)[cct_row]},
                "allowed_quote_spans": [cct_row]}
    built, _, _ = process_response(findings_json({**BLANK, "diagnosis": "CAD", "diagnostic_method": "CCT",
                                                  "evidence_quote": cct_row}), cct_item,
                                   SimpleNamespace(ctx=cct_ctx, chunk_index={"cid": 0}), model="m")
    check("CCT: outcome_type='öneri' otomatik_alanlar'da", ("öneri", "otomatik (cct_recommendations tablo ailesi)"),
          (built[0]["outcome_type"], json.loads(built[0]["taslak_notu"])["otomatik_alanlar"].get("outcome_type"))
          if built else None)

    # --json-mode
    ok = ScriptedClient(lambda *_: make_response(findings_json()))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "{}"}]
    call_llm(ok, "m", messages, max_tokens=5, no_think=True, max_retries=0, sleep=quiet, json_mode="none")
    call_llm(ok, "m", messages, max_tokens=5, no_think=True, max_retries=0, sleep=quiet)
    check("--json-mode none: response_format gönderilmez", False, "response_format" in ok.requests[0])
    check("--json-mode json_object (varsayılan): gönderilir", {"type": "json_object"}, ok.requests[1].get("response_format"))
    try:
        call_llm(ok, "m", messages, max_tokens=5, no_think=True, max_retries=0, sleep=quiet, json_mode="xml")
        got = "kabul"
    except BatchStop as exc:
        got = exc.code
    check("--json-mode geçersiz -> durur", "secenek", got)
    aday = "clinical-candidate-v1:" + "cd" * 32
    write_cache(tmp, PIPELINE_VERSION, "m", aday, "{}", "stop", False, json_mode="none")
    check("önbellek dosyasında json_mode", "none", read_cache(tmp, PIPELINE_VERSION, "m", aday, quiet)["json_mode"])

    # Red kaydı: deterministik satir_hash (run_id/zaman yok); alt_tur
    source = {"aday_id": "a1", "document_id": "d", "makale_id": "m", "dosya": "f.pdf", "sayfa": 3,
              "kaynak_turu": "metin", "cumle": "cümle"}
    record = {"asama": "validate_row", "hata_turu": "deger_kumesi_disi",
              "aciklama": "clinical_relationship_uncertain: human review required", "satir": {"kanit_cumle": "k"}}
    first = rejection_record(source, record, pipeline_version="v1", model="gpt")
    check("red kaydı: aynı girdi -> aynı hash", first["satir_hash"],
          rejection_record(dict(source), dict(record), pipeline_version="v1", model="gpt")["satir_hash"])
    check("red kaydı: alt_tur", "clinical_relationship_uncertain", first["alt_tur"])
    check("red kaydı: farklı model -> farklı hash", True,
          first["satir_hash"] != rejection_record(source, record, pipeline_version="v1", model="x")["satir_hash"])

    # Önlem eşlemesi bilinen bütün kodları kapsıyor
    known = {"metaveri", "baslik", "sayfa_asan", "tablo_karmasik", "tablo_baglami_gecersiz", "guvenli_parca_yok",
             "alinti_birebir_degil", "alan_alintida_yok", "bos_bulgu", "bulgu_alanlari", "bulgu_gecersiz",
             "birden_fazla_mudahale", "diagnostic_relationship_uncertain", "diagnostic_relationship_contradicted",
             "guvenli_parca_disinda", "mudahale_alani_yok", "kanit_chunk_yok", "kanit_birebir_degil", "yanlis_sayfa",
             "kanitta_olmayan_sayi", "deger_kumesi_disi", "clinical_relationship_uncertain",
             "clinical_relationship_contradicted", "icd10_yok", "alan_cok_uzun", "yontem_istatistik"}
    check("önlem eşlemesi bilinen kodları kapsıyor", [], sorted(known - set(ONLEM_ESLEME)))


# ---------------------------------------------------------------------------
# B) Veritabanı gerektirenler
# ---------------------------------------------------------------------------

TABLES = ("klinik_bulgular", "cikarim_reddedilenler", "halusinasyon_degerlendirme", "kalite_isaretleri",
          "kalite_calistirmalari", "makaleler WHERE document_id IS NULL", "tani_tedavi", "sonuclar",
          "semptom_iliskileri", "icd10_who", "tkd_kurallari")


def db_counts():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        out = {}
        for table in TABLES:
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


def iter_spans(to_send, pattern):
    """Sırayla (item, span, intervention_field, intervention, value) — müdahale + desen aynı güvenli parçada."""
    for item in to_send:
        if item["candidate"]["kaynak_turu"] != "metin":
            continue
        for span in item["allowed_quote_spans"]:
            m = re.search(pattern, span, re.I)
            if not m:
                continue
            for category, regex in TREATMENT_REGEX.items():
                hit = regex.search(span)
                if hit:
                    field = "procedure" if category == "girişimsel" else "drug"
                    yield item, span, field, hit.group(0), m.group(0)
                    break


def find_span(to_send, pattern, prefer=None):
    """İlk uygun parça (iter_spans ile aynı sıra) ya da None."""
    return next(iter_spans(to_send, pattern), None)


# f00bddb (Sinem): müdahale + hasta seçimi/kayıt cümlesi gözlenmiş ilişki değil -> insan incelemesi.
RELATIONSHIP_REVIEW = ("clinical_relationship_uncertain", "clinical_relationship_contradicted")
SELECTION_SENTENCE = r"In this (?:retrospective|prospective) (?:case-control|cohort) study, \d{2,4} patients\b"


def test_database(tmp):
    from src.database import KLINIK_INSERT_COLUMNS, clinical_row_hash, get_connection
    from src.run_clinical_batch import prepare
    from src.sunum import ensure_schema, installed_objects, schema_ready, write_rejections

    conn = get_connection()
    try:
        if not schema_ready(conn):
            ensure_schema(conn)  # DDL: transaction dışında
        objects, mappings = installed_objects(conn)
    finally:
        conn.close()
    names = {name for name, _ in objects}
    check("sunum nesneleri kurulu", True, {"cikarim_reddedilenler", "hata_onlem", "v_cardiorag_bulgular",
                                           "v_bulgu_kaynak", "v_halusinasyon_onlemleri", "kaynak_bul",
                                           "v_rag_sorgular", "v_rag_kaynaklar", "v_rag_iddialar"} <= names)
    check("hata_onlem eşleme sayısı", len(ONLEM_ESLEME), mappings)
    before = db_counts()

    sources, to_send, excluded, total = prepared = prepare()
    check("metaveri iletimi: 6 makale başlığı", 6, len(sources.article_metadata))
    from src.clinical_candidates import collect_candidates
    plain = collect_candidates(sources.articles)
    with_meta = collect_candidates(sources.articles, article_metadata=sources.article_metadata)
    newly = [c for c, d in zip(with_meta, plain) if c["kaynak_turu"] == "metadata" and d["kaynak_turu"] != "metadata"]
    check("metaveri iletimi: yeni elenen aday (liste raporda)", 0, len(newly))
    reasons = {}
    for e in excluded:
        reasons[e["sebep"]] = reasons.get(e["sebep"], 0) + 1
    check("ayrılan sayıları (metaveri / başlık / toplam / LLM'e)", (7, 22, 64, 596),
          (reasons.get("metaveri"), reasons.get("baslik"), len(excluded), len(to_send)))

    # Yeni alanlar gerçek adaylarla validate_row'dan geçiyor
    cases = {
        "p": (r"\bP\s*=\s*\.\d+", "p_value", "p"),
        "ci95": (r"95% CI, [\d.]+[–-][\d.]+", "confidence_interval", "ci95"),
        "hasta_sayisi": (r"\b\d{2,4} patients\b", "sample_size", "hasta_sayisi"),
        "etki_turu": (r"\bHR\b", "effect_measure", "etki_turu"),
    }
    def attempt(found, field):
        item, span, ifield, intervention, value = found
        rows, rejected, _ = process_response(findings_json({**BLANK, ifield: intervention, field: value,
                                                            "evidence_quote": span}), item, sources, model="m")
        passed, failed = preview_rows(rows, item, sources)
        return rows, rejected, passed, failed

    def relationship_only(rows, rejected, failed):
        """Red yalnız ilişki kuralından mı (ayrıştırma/kanıt/sütun hatası değil)?"""
        return (len(rows) == 1 and not rejected and len(failed) == 1
                and failed[0]["hata_turu"] == "deger_kumesi_disi"
                and str(failed[0]["aciklama"]).startswith(RELATIONSHIP_REVIEW))

    for label, (pattern, field, column) in cases.items():
        if label == "hasta_sayisi":
            # f00bddb sonrası ilk parça (hasta seçimi cümlesi) bilerek insan incelemesine düşüyor;
            # ilk GEÇEN parça kullanılır, atlananların yalnız ilişki kuralıyla reddedildiği doğrulanır.
            found, skipped = None, []
            for option in iter_spans(to_send, pattern):
                rows, rejected, passed, failed = attempt(option, field)
                if passed:
                    found = option
                    break
                skipped.append((option[4], relationship_only(rows, rejected, failed),
                                failed[0]["aciklama"].split(":")[0] if failed else (rejected or ["?"])[0]))
            # f00bddb (Sinem) hasta seçimi kuralını belgeler: "In this retrospective case-control study,
            # 189 patients …" + müdahale -> clinical_relationship_uncertain (yalnız bu kural; başka hata değil).
            # Aynı kontrol, ilk geçen parçadan önce atlanan her parçanın yalnız ilişki kuralıyla düştüğünü de ister.
            selection = next((o for o in iter_spans(to_send, pattern) if re.search(SELECTION_SENTENCE, o[1])), None)
            sel_rows, sel_rejected, sel_passed, sel_failed = attempt(selection, field) if selection else ([], [], [], [])
            check("f00bddb (Sinem) hasta seçimi kuralı: '189 patients' seçim cümlesi -> "
                  "clinical_relationship_uncertain; atlanan parçalar yalnız ilişki kuralıyla reddedildi",
                  ("189 patients", 0, "clinical_relationship_uncertain", True, True),
                  (selection[4] if selection else None, len(sel_passed),
                   str(sel_failed[0]["aciklama"]).split(":")[0] if sel_failed else None,
                   relationship_only(sel_rows, sel_rejected, sel_failed), [s[1:] for s in skipped]),
                  bool(selection) and selection[4] == "189 patients" and not sel_passed
                  and str(sel_failed[0]["aciklama"]).startswith("clinical_relationship_uncertain")
                  and relationship_only(sel_rows, sel_rejected, sel_failed) and all(ok for _, ok, _ in skipped))
        else:
            found = find_span(to_send, pattern)
        if not found:
            check(f"yeni alan {label}: uygun aday bulundu", True, False)
            continue
        item, span, ifield, intervention, value = found
        rows, rejected, passed, failed = attempt(found, field)
        stored = passed[0][column] if passed else None
        expected = (int(re.match(r"\d+", value).group(0)) if label == "hasta_sayisi"
                    else "HR" if label == "etki_turu" else value)
        check(f"yeni alan {label}: validate_row'dan geçer ve sütuna yazılır", (1, expected),
              (len(passed), stored), len(passed) == 1 and stored == expected)
        if passed and label in ("hasta_sayisi", "etki_turu"):
            note = json.loads(passed[0]["taslak_notu"])
            check(f"yeni alan {label}: otomatik_alanlar ve doğrulama izi", True,
                  label in note["otomatik_alanlar"]
                  and any(("sample_size" if label == "hasta_sayisi" else "effect_measure") in c
                          for c in note["otomatik_dogrulama"]["gecen_kontroller"]))
    icd_found = None
    for code, en, _tr in sources.icd_names:
        icd_found = find_span(to_send, r"(?<!\w)" + re.escape(en) + r"(?!\w)") if en else None
        if icd_found:
            break
    if icd_found:
        item, span, ifield, intervention, diagnosis = icd_found
        rows, _, _ = process_response(findings_json({**BLANK, ifield: intervention, "diagnosis": diagnosis,
                                                     "evidence_quote": span}), item, sources, model="m")
        passed, _ = preview_rows(rows, item, sources)
        check("ICD: tanı adı birebir -> kod, validate_row'dan geçer", (1, code),
              (len(passed), passed[0]["icd10"] if passed else None))
    else:
        check("ICD: uygun aday bulundu", True, False)

    # Reddedilenler: gerçek mod + sahte istemci, dış transaction (ROLLBACK).
    # Teste özgü pipeline_version: red satir_hash'i sürümü içerir; veritabanında aynı sürümle kayıtlı
    # gerçek koşu varsa INSERT IGNORE 0 ekler ve sayaçlar ölçülemez. Benzersiz sürümle tablo boş da
    # dolu da olsa ilk koşu gerçekten yazar, ikinci koşu gerçekten atlar.
    test_pv = f"{PIPELINE_VERSION}-test-{uuid.uuid4().hex[:8]}"

    def responder(user, kwargs):
        good = json.loads(MockLLMClient()._create(**kwargs).choices[0].message.content)["findings"]
        bad = [{**BLANK, "drug": "uydurma", "evidence_quote": "metinde olmayan alıntı"}]
        return make_response(json.dumps({"findings": good + bad}, ensure_ascii=False))
    conn = tx_connection()
    try:
        options = Options(base_dir=Path(tmp) / "b1", limit=4, max_consecutive_errors=50, pipeline_version=test_pv)
        first = run_batch(options, client=ScriptedClient(responder), connection=conn, prepared=prepared, out=quiet)
        cursor = conn.cursor()
        cursor.execute("SELECT asama, COUNT(*) FROM cikarim_reddedilenler WHERE pipeline_version = %s GROUP BY asama",
                       (test_pv,))
        by_stage = dict(cursor.fetchall())
        cursor.execute("SELECT satir_hash FROM cikarim_reddedilenler WHERE pipeline_version = %s ORDER BY satir_hash",
                       (test_pv,))
        hashes = [r[0] for r in cursor.fetchall()]
        second = run_batch(Options(base_dir=Path(tmp) / "b1b", limit=4, max_consecutive_errors=50,
                                   pipeline_version=test_pv),
                           client=ScriptedClient(responder), connection=conn, prepared=prepared, out=quiet)
        cursor.execute("SELECT satir_hash FROM cikarim_reddedilenler WHERE pipeline_version = %s ORDER BY satir_hash",
                       (test_pv,))
        hashes_again = [r[0] for r in cursor.fetchall()]
        write_rejections(conn, {"aday_id": "test-aday", "dosya": "t.pdf", "cumle": "c"},
                         [{"asama": "validate_row", "hata_turu": "yeni_kod", "aciklama": "test"}],
                         pipeline_version=test_pv, model="m")
        cursor.execute("SELECT hata_turu, halusinasyon_turu, uygulanan_onlem, yakalanan, sonuc, ornek_aday "
                       "FROM v_halusinasyon_onlemleri WHERE pipeline_version = %s", (test_pv,))
        view = {r[0]: r for r in cursor.fetchall()}
        cursor.close()
    finally:
        conn.rollback()
        conn.close()
    check("reddedilenler: ön eleme kayıtları (64)", 64, by_stage.get("on_eleme"))
    check("reddedilenler: yanıt doğrulama kayıtları var", True, by_stage.get("yanit_dogrulama", 0) >= 4)
    check("reddedilenler: özetteki sayaçlar", (64, True),
          (first["sayaclar"].get("red_tablosu_on_eleme"), first["sayaclar"].get("red_tablosu_aday", 0) >= 4))
    check("reddedilenler: ikinci koşu kayıt eklemez (UNIQUE + INSERT IGNORE)", hashes, hashes_again)
    check("reddedilenler: ikinci koşuda yeni ekleme 0", (0, 0),
          (second["sayaclar"].get("red_tablosu_on_eleme", 0), second["sayaclar"].get("red_tablosu_aday", 0)))
    meta = view.get("metaveri")
    check("v_halusinasyon_onlemleri: metaveri satırı", (7, "kayda girmedi", True),
          (meta[3], meta[4], bool(meta[5])) if meta else None)
    check("v_halusinasyon_onlemleri: önlem eşleşiyor", ONLEM_ESLEME["alinti_birebir_degil"][1],
          view.get("alinti_birebir_degil", [None] * 3)[2])
    check("v_halusinasyon_onlemleri: eşlemesiz kod gizlenmez", "eşleme yok", view.get("yeni_kod", [None] * 2)[1])

    # Tablo yoksa açık mesajla durur
    with patch("src.sunum.schema_ready", lambda _c: False):
        conn = tx_connection()
        try:
            stopped = run_batch(Options(base_dir=Path(tmp) / "b2", limit=1, pipeline_version=test_pv),
                                client=ScriptedClient(responder), connection=conn, prepared=prepared, out=quiet)
        finally:
            conn.rollback()
            conn.close()
    check("tablo yoksa: durur ve --kur'u söyler", ("sunum_tablolari_yok", True),
          (stopped.get("durma_sebebi"), "python -m src.sunum --kur" in stopped.get("mesaj", "")))

    # --mock ve --no-save kalıcı yazmaz
    mid = db_counts()
    mock = run_batch(Options(base_dir=Path(tmp) / "b3", mock=True, limit=5, pipeline_version=test_pv),
                     prepared=prepared, out=quiet)
    check("--mock: transaction içinde red yazdı", True, mock["sayaclar"].get("red_tablosu_on_eleme", 0) == 64)
    check("--mock: sonra veritabanı aynı", mid, db_counts())
    run_batch(Options(base_dir=Path(tmp) / "b4", no_save=True, limit=3, force=True, pipeline_version=test_pv),
              client=ScriptedClient(responder), prepared=prepared, out=quiet)
    check("--no-save: veritabanına yazmaz", mid, db_counts())

    # VIEW'lar ve kaynak_bul: sahte bulgular transaction içinde
    conn = tx_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT c.chunk_id, c.document_id, c.page_number, c.text, m.makale_id FROM pdf_chunks c "
                       "JOIN pdf_belgeleri b ON b.document_id = c.document_id AND b.processing_version = c.processing_version "
                       "JOIN makaleler m ON m.document_id = c.document_id ORDER BY c.chunk_id LIMIT 1")
        chunk_id, doc, page, text, makale = cursor.fetchone()
        from src.clinical_validation import norm_ws
        quote = norm_ws(text)[:80]
        notu = {"aday_id": "test-aday-1", "aday_cumlesi": "aday cümlesi", "pipeline_version": "pv-test", "model": "mm",
                "extracted_fields": {"clinical_outcome": "stroke"},
                "otomatik_dogrulama": {"insan_onayi": "yok", "gecen_kontroller": ["validate_row: kanit_sayfada_birebir"]}}
        specs = [
            dict(mudahale_en="Aspirin", taslak_notu=json.dumps(notu)),
            dict(mudahale_tr="Varfarin", sonuc_olcutu_tr="inme", taslak_notu=json.dumps({**notu, "aday_id": "test-aday-2"})),
            dict(mudahale_en="Statin", taslak_notu="bozuk {json"),
        ]
        ids = []
        for spec in specs:
            row = {c: None for c in KLINIK_INSERT_COLUMNS}
            row.update(makale_id=makale, document_id=doc, chunk_id=chunk_id, kanit_sayfa=page, kanit_cumle=quote,
                       kanit_turu="metin", mudahale_turu="tedavi", review_status="pending", cikarma_yontemi="rag", **spec)
            row["satir_hash"] = clinical_row_hash(row, "rag")
            cursor.execute(f"INSERT INTO klinik_bulgular ({', '.join(KLINIK_INSERT_COLUMNS)}) VALUES "
                           f"({', '.join(['%s'] * len(KLINIK_INSERT_COLUMNS))})", [row[c] for c in KLINIK_INSERT_COLUMNS])
            ids.append(cursor.lastrowid)
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(f"SELECT bulgu_id, filename, makale_basligi, doi_link, sonuc, sonuc_kaynagi, aday_id, kalite_hata "
                       f"FROM v_cardiorag_bulgular WHERE bulgu_id IN ({placeholders}) ORDER BY bulgu_id", ids)
        bulgular = cursor.fetchall()
        cursor.execute(f"SELECT bulgu_id, chunk_sayfasi, sayfa, aday_cumlesi, insan_onayi, gecen_kontroller, "
                       f"pipeline_version, model FROM v_bulgu_kaynak WHERE bulgu_id IN ({placeholders}) ORDER BY bulgu_id",
                       ids)
        kaynak = cursor.fetchall()

        def call(term):
            cursor.callproc("kaynak_bul", (term,))
            rows = [r for res in cursor.stored_results() for r in res.fetchall()]
            return [r for r in rows if r[0] in ids]
        searches = {term: len(call(term)) for term in ("aspirin", "ASPIRIN", "varfarin", "%", "Asp_rin",
                                                        "x' OR 1=1 -- ", "")}
        cursor.close()
    finally:
        conn.rollback()
        conn.close()
    check("v_cardiorag_bulgular: 3 satır, bozuk taslak_notu VIEW'u düşürmez", 3, len(bulgular))
    check("v_cardiorag_bulgular: PDF adı, başlık, DOI linki", True,
          all(r[1] and r[2] and str(r[3]).startswith("https://") for r in bulgular))
    check("v_cardiorag_bulgular: sonuç ve kaynağı", [("stroke", "taslak_notu (İngilizce clinical_outcome, çevrilmedi)"),
                                                     ("inme", "sonuc_olcutu_tr"), (None, None)],
          [(r[4], r[5]) for r in bulgular])
    check("v_cardiorag_bulgular: aday_id ve kalite sayısı", [("test-aday-1", 0), ("test-aday-2", 0), (None, 0)],
          [(r[6], r[7]) for r in bulgular])
    check("v_bulgu_kaynak: chunk sayfası = kanıt sayfası, aday cümlesi, insan_onayi, sürüm", True,
          all(r[1] == r[2] for r in kaynak) and kaynak[0][3] == "aday cümlesi" and kaynak[0][4] == "yok"
          and kaynak[0][6] == "pv-test" and kaynak[0][7] == "mm" and "kanit_sayfada" in str(kaynak[0][5]))
    check("kaynak_bul: büyük/küçük harf duyarsız", (1, 1, 1),
          (searches["aspirin"], searches["ASPIRIN"], searches["varfarin"]))
    check("kaynak_bul: '%' ve '_' joker değil", (0, 0), (searches["%"], searches["Asp_rin"]))
    check("kaynak_bul: SQL injection denemesi sonuç döndürmez", 0, searches["x' OR 1=1 -- "])
    check("kaynak_bul: boş arama sonuç döndürmez", 0, searches[""])

    check("sonda: bütün tablolar başlangıçtakiyle aynı", before, db_counts())


# ---------------------------------------------------------------------------
# RAG sunum VIEW'ları (v_rag_sorgular, v_rag_kaynaklar, v_rag_iddialar)
# ---------------------------------------------------------------------------

RAG_TABLES = ("rag_sorgulari", "rag_sorgu_kaynaklari", "halusinasyon_degerlendirme")
RAG_VIEWS = ("v_rag_sorgular", "v_rag_kaynaklar", "v_rag_iddialar")
ABSTENTION = "Bu soruyu yanıtlamak için yeterli kullanılabilir kanıt bulunamadı."


def rag_counts():
    from src.database import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        out = {}
        for table in RAG_TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = cursor.fetchone()[0]
        conn.rollback()
        return out
    finally:
        conn.close()


def rag_view_rows(cursor, view, query_id, order="1"):
    cursor.execute(f"SELECT * FROM {view} WHERE query_id = %s ORDER BY {order}", (query_id,))
    names = [d[0] for d in cursor.description]
    decode = lambda v: bytes(v).decode() if isinstance(v, (bytes, bytearray)) else v  # noqa: E731
    return [{n: decode(v) for n, v in zip(names, row)} for row in cursor.fetchall()]


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rag_fixture_chunks(cursor, count=3):
    """Gerçek, aktif chunk'lar (FK + metin/hash doğrulaması sahte chunk'la geçmez).

    URL / 'sk-' içeren metinler atlanır: public_rag_payload bunları maskeler ve hash bozulur.
    """
    cursor.execute("""
        SELECT c.chunk_id, c.document_id, c.page_number, c.processing_version, c.text, c.text_sha256, b.filename
        FROM pdf_chunks AS c JOIN pdf_belgeleri AS b ON b.document_id = c.document_id
        WHERE b.durum = 'tamamlandi' AND c.processing_version = b.processing_version
          AND CHAR_LENGTH(c.text) > 300 AND c.text NOT LIKE '%%http%%' AND c.text NOT LIKE '%%://%%'
          AND c.text NOT LIKE '%%sk-%%'
        ORDER BY c.document_id, c.page_number, c.chunk_id""")
    rows, seen = [], set()
    for chunk_id, doc, page, version, text, digest, filename in cursor.fetchall():
        decode = lambda v: bytes(v).decode() if isinstance(v, (bytes, bytearray)) else v  # noqa: E731
        chunk_id, doc, version, text, digest, filename = map(decode, (chunk_id, doc, version, text, digest, filename))
        if doc in seen or _sha(text) != digest:
            continue
        seen.add(doc)
        rows.append(dict(chunk_id=chunk_id, document_id=doc, page_number=page, processing_version=version,
                         source_text=text, text_sha256=digest, filename=filename))
        if len(rows) == count:
            break
    return rows


def rag_result(status, chunks, used, claims, *, question):
    """rag_persistence.save_rag_bundle için sentetik (mock_test) sonuç; kaynaklar gerçek chunk."""
    import uuid

    sources = [{**chunks[i], "number": n} for n, i in enumerate(used, 1)]
    generated = " ".join(f"{c['claim_text']} [{c.get('citations', [1])[0]}]." for c in claims) or None
    llm = status in ("generated", "claim_validation_failed", "citation_validation_failed")
    return dict(
        query_id=str(uuid.uuid4()), question=question, status=status,
        answer=generated if status == "generated" else (ABSTENTION if status == "insufficient_evidence" else None),
        audit={"generated_answer": generated if llm else None, "scope": "restricted_audit_not_user_answer"},
        sources=sources,
        citation_check={"valid": True, "used_citations": list(range(1, len(sources) + 1))} if llm else None,
        claim_verification={"scope": "mock source-grounded fixture, not expert approval", "claims": claims},
        claims_verified=status == "generated", response_time_seconds=0.25,
        retrieved_count=len(chunks), embedding_model="minilm", reranker_model="minilm",
        rerank_method="cross_encoder", llm_enabled=llm, llm_model="mock-test-model" if llm else None,
        evidence_check={"sufficient": status != "insufficient_evidence", "reason": "mock_test"},
        top_rerank_score=1.5 if status != "insufficient_evidence" else -2.0,
        debug={"retrieved_hits": [{"chunk_id": c["chunk_id"], "rrf_score": 0.03 - i / 1000}
                                  for i, c in enumerate(chunks)],
               "reranked_hits": [{"chunk_id": c["chunk_id"], "rerank_score": 1.5 - i}
                                 for i, c in enumerate(chunks)]},
        run_provenance={"kind": "mock_test", "real_llm": False})


def rag_claim(index, text, status, chunk=None, citation=1):
    claim = dict(claim_index=index, claim_text=text, claim_text_sha256=_sha(text), citations=[citation],
                 status=status, reasons=[] if status == "supported" else ["mock_test_reason"], matches=[])
    if chunk is not None:
        claim["matches"] = [dict(chunk_id=chunk["chunk_id"], evidence_quote=text)]
    return claim


def test_rag_views():
    from src.database import get_connection
    from src.rag_persistence import save_rag_bundle
    from src.sunum import ensure_schema, installed_objects

    conn = get_connection()
    try:
        names = {name for name, _ in installed_objects(conn)[0]}
        if not set(RAG_VIEWS) <= names:
            ensure_schema(conn)  # DDL: transaction dışında, tekrar çalıştırılabilir
            names = {name for name, _ in installed_objects(conn)[0]}
        check("RAG VIEW'ları kurulu", True, set(RAG_VIEWS) <= names)
        cursor = conn.cursor()
        queryable = []
        for view in RAG_VIEWS:
            cursor.execute(f"SELECT COUNT(*) FROM {view}")
            queryable.append(cursor.fetchone()[0] >= 0)
        check("RAG VIEW'ları sorgulanabilir", [True] * 3, queryable)
        conn.rollback()
        cursor.close()
    finally:
        conn.close()

    before = rag_counts()
    conn = get_connection()
    conn.autocommit = False
    try:
        conn.start_transaction()
        cursor = conn.cursor()
        chunks = rag_fixture_chunks(cursor)
        check("RAG fixture: 3 gerçek aktif chunk", 3, len(chunks))
        if len(chunks) < 3:
            return
        q1, q2 = chunks[0]["source_text"][:120].strip(), chunks[1]["source_text"][:120].strip()
        generated = rag_result("generated", chunks, [0, 1],
                               [rag_claim(1, q1, "supported", chunks[0], 1), rag_claim(2, q2, "supported", chunks[1], 2)],
                               question="Sentetik test sorusu (generated)")
        failed = rag_result("claim_validation_failed", chunks[:2], [0],
                            [rag_claim(1, q1, "supported", chunks[0]),
                             rag_claim(2, "Sentetik kısmen desteklenen iddia", "partially_supported"),
                             rag_claim(3, "Sentetik desteklenmeyen iddia", "not_supported"),
                             rag_claim(4, "Sentetik belirsiz iddia", "uncertain")],
                            question="Sentetik test sorusu (claim_validation_failed)")
        insufficient = rag_result("insufficient_evidence", chunks[:2], [], [],
                                  question="Sentetik test sorusu (insufficient_evidence)")
        saved = [save_rag_bundle(generated, question_type="cevaplanabilir", connection=conn, allow_test_data=True),
                 save_rag_bundle(failed, question_type="cevaplanabilir", connection=conn, allow_test_data=True),
                 save_rag_bundle(insufficient, question_type="cevaplanamaz", connection=conn, allow_test_data=True)]
        check("save_rag_bundle: 3 örnek yazıldı (commit yok)", ["pending_caller_commit"] * 3,
              [s["status"] for s in saved])

        def page(chunk):
            return f"{chunk['filename']} s.{chunk['page_number']}"

        # v_rag_sorgular: soru başına tek satır, toplamalar çoğalmadan
        per_query = {q["query_id"]: rag_view_rows(cursor, "v_rag_sorgular", q["query_id"])
                     for q in (generated, failed, insufficient)}
        rows = {qid: found[0] for qid, found in per_query.items() if found}
        counts = [len(per_query[q["query_id"]]) for q in (generated, failed, insufficient)]
        check("v_rag_sorgular: soru başına 1 satır (iddia satırları çoğaltmaz)", [1, 1, 1], counts)
        keys = ("soru_tipi", "durum", "bulunan_kaynak", "kullanilan_kaynak", "kullanilan_kaynaklar", "citation_correct",
                "iddia_sayisi", "iddia_supported", "iddia_partially_supported", "iddia_not_supported", "iddia_belirsiz",
                "hata_turu", "uygulanan_onlem", "duzeldi_mi", "degerlendirme_satiri", "klinik_bulgu_sayisi",
                "klinik_cikarim_red_sayisi")

        def view(qid):
            from decimal import Decimal
            row = rows.get(qid) or {}
            return tuple(int(row[k]) if isinstance(row.get(k), (int, Decimal)) else row.get(k) for k in keys)

        check("v_rag_sorgular: generated",
              ("cevaplanabilir", "generated", 3, 2, f"{page(chunks[0])}; {page(chunks[1])}", "1",
               2, 2, 0, 0, 0, None, None, None, 2, 0, 0), view(generated["query_id"]))
        check("v_rag_sorgular: claim_validation_failed (iddia sayıları)",
              ("cevaplanabilir", "claim_validation_failed", 2, 1, page(chunks[0]), "1",
               4, 1, 1, 1, 1, "claim_not_supported, claim_partially_supported, claim_uncertain", None, None, 4, 0, 0),
              view(failed["query_id"]))
        check("v_rag_sorgular: insufficient_evidence",
              ("cevaplanamaz", "insufficient_evidence", 2, 0, None, None,
               0, 0, 0, 0, 0, "insufficient_evidence", None, None, 1, 0, 0), view(insufficient["query_id"]))
        check("v_rag_sorgular: cevap yalnız generated/ret metninde dolu, reddedilen cevap gösterilmez",
              (generated["answer"], None, ABSTENTION),
              tuple(rows.get(q["query_id"], {}).get("cevap") for q in (generated, failed, insufficient)))
        check("v_rag_sorgular: durum_aciklama ayrı sütun",
              ("Cevap üretildi; atıf ve iddia kontrollerinden geçti",
               "İddia kontrolünden geçmedi ya da kontrol hata verdi; cevap gösterilmedi",
               "Yeterli kanıt bulunamadı; cevap verilmedi"),
              tuple(rows.get(q["query_id"], {}).get("durum_aciklama") for q in (generated, failed, insufficient)))
        check("v_rag_sorgular: kanıt / model sütunları tablodan",
              (("sufficient", "mock-test-model"), ("insufficient", None)),
              tuple((rows.get(q["query_id"], {}).get("evidence_status"), rows.get(q["query_id"], {}).get("llm_model"))
                    for q in (generated, insufficient)))

        # v_rag_kaynaklar: source_rank sırası, kullanılan işaretleri, dosya/sayfa, metin başlangıcı
        sources = rag_view_rows(cursor, "v_rag_kaynaklar", generated["query_id"], "source_rank")
        check("v_rag_kaynaklar: generated 3 kaynak, sıra ve bağlam işareti",
              [(1, 1), (2, 1), (3, 0)], [(r["source_rank"], int(r["used_in_context"])) for r in sources])
        check("v_rag_kaynaklar: dosya / sayfa / chunk_baslangic",
              [(c["filename"], c["page_number"], c["source_text"][:200]) for c in chunks],
              [(r["dosya"], r["sayfa"], r["chunk_baslangic"]) for r in sources])
        check("v_rag_kaynaklar: insufficient_evidence 2 kaynak, hiçbiri bağlamda", [0, 0],
              [int(r["used_in_context"]) for r in rag_view_rows(cursor, "v_rag_kaynaklar", insufficient["query_id"],
                                                                "source_rank")])

        # v_rag_iddialar: iddia başına satır; olay satırı (insufficient_evidence) iddia değildir
        claims = rag_view_rows(cursor, "v_rag_iddialar", failed["query_id"], "iddia_no")
        check("v_rag_iddialar: claim_validation_failed 4 iddia",
              [(1, "supported", chunks[0]["filename"], chunks[0]["page_number"]),
               (2, "partially_supported", None, None), (3, "not_supported", None, None), (4, None, None, None)],
              [(r["iddia_no"], r["claim_supported"], r["destekleyen_dosya"], r["destekleyen_sayfa"]) for r in claims])
        check("v_rag_iddialar: claim_text ve hata_turu tablodaki gibi",
              [(q1, None), ("Sentetik kısmen desteklenen iddia", "claim_partially_supported"),
               ("Sentetik desteklenmeyen iddia", "claim_not_supported"), ("Sentetik belirsiz iddia", "claim_uncertain")],
              [(r["claim_text"], r["hata_turu"]) for r in claims])
        check("v_rag_iddialar: satır sayıları (generated, failed, insufficient)", [2, 4, 0],
              [len(rag_view_rows(cursor, "v_rag_iddialar", q["query_id"])) for q in (generated, failed, insufficient)])
        cursor.close()
    finally:
        conn.rollback()
        conn.close()
    check("RAG VIEW testi: ROLLBACK sonrası tablolar başlangıçla aynı", before, rag_counts())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_offline(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)
    test_rag_views()
    width = max(len(r[0]) for r in results)
    print(f"\n{'Kontrol':<{width}}  {'Beklenen':<26}  {'Bulunan':<40}  Sonuç")
    print("-" * (width + 80))
    for name, exp, act, passed in results:
        print(f"{name:<{width}}  {exp[:26]:<26}  {act[:40]:<40}  {'OK' if passed else 'HATA'}")
    failed = [r for r in results if not r[3]]
    print()
    if failed:
        print(f"BAŞARISIZ: {len(failed)}/{len(results)} kontrol geçmedi.")
        sys.exit(1)
    print(f"BAŞARILI: {len(results)} kontrolün hepsi geçti.")


if __name__ == "__main__":
    main()
