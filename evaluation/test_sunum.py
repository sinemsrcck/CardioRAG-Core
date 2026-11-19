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
"""

import json
import re
import sys
import tempfile
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
             "clinical_relationship_contradicted", "icd10_yok", "alan_cok_uzun"}
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


def find_span(to_send, pattern, prefer=None):
    """(item, span, intervention_field, intervention) — müdahale + desen aynı güvenli parçada."""
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
                    return item, span, field, hit.group(0), m.group(0)
    return None


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
                                           "v_bulgu_kaynak", "v_halusinasyon_onlemleri", "kaynak_bul"} <= names)
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
    for label, (pattern, field, column) in cases.items():
        found = find_span(to_send, pattern)
        if not found:
            check(f"yeni alan {label}: uygun aday bulundu", True, False)
            continue
        item, span, ifield, intervention, value = found
        rows, rejected, _ = process_response(findings_json({**BLANK, ifield: intervention, field: value,
                                                            "evidence_quote": span}), item, sources, model="m")
        passed, failed = preview_rows(rows, item, sources)
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

    # Reddedilenler: gerçek mod + sahte istemci, dış transaction (ROLLBACK)
    def responder(user, kwargs):
        good = json.loads(MockLLMClient()._create(**kwargs).choices[0].message.content)["findings"]
        bad = [{**BLANK, "drug": "uydurma", "evidence_quote": "metinde olmayan alıntı"}]
        return make_response(json.dumps({"findings": good + bad}, ensure_ascii=False))
    conn = tx_connection()
    try:
        options = Options(base_dir=Path(tmp) / "b1", limit=4, max_consecutive_errors=50)
        first = run_batch(options, client=ScriptedClient(responder), connection=conn, prepared=prepared, out=quiet)
        cursor = conn.cursor()
        cursor.execute("SELECT asama, COUNT(*) FROM cikarim_reddedilenler WHERE pipeline_version = %s GROUP BY asama",
                       (PIPELINE_VERSION,))
        by_stage = dict(cursor.fetchall())
        cursor.execute("SELECT satir_hash FROM cikarim_reddedilenler WHERE pipeline_version = %s ORDER BY satir_hash",
                       (PIPELINE_VERSION,))
        hashes = [r[0] for r in cursor.fetchall()]
        second = run_batch(Options(base_dir=Path(tmp) / "b1b", limit=4, max_consecutive_errors=50),
                           client=ScriptedClient(responder), connection=conn, prepared=prepared, out=quiet)
        cursor.execute("SELECT satir_hash FROM cikarim_reddedilenler WHERE pipeline_version = %s ORDER BY satir_hash",
                       (PIPELINE_VERSION,))
        hashes_again = [r[0] for r in cursor.fetchall()]
        write_rejections(conn, {"aday_id": "test-aday", "dosya": "t.pdf", "cumle": "c"},
                         [{"asama": "validate_row", "hata_turu": "yeni_kod", "aciklama": "test"}],
                         pipeline_version=PIPELINE_VERSION, model="m")
        cursor.execute("SELECT hata_turu, halusinasyon_turu, uygulanan_onlem, yakalanan, sonuc, ornek_aday "
                       "FROM v_halusinasyon_onlemleri WHERE pipeline_version = %s", (PIPELINE_VERSION,))
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
            stopped = run_batch(Options(base_dir=Path(tmp) / "b2", limit=1), client=ScriptedClient(responder),
                                connection=conn, prepared=prepared, out=quiet)
        finally:
            conn.rollback()
            conn.close()
    check("tablo yoksa: durur ve --kur'u söyler", ("sunum_tablolari_yok", True),
          (stopped.get("durma_sebebi"), "python -m src.sunum --kur" in stopped.get("mesaj", "")))

    # --mock ve --no-save kalıcı yazmaz
    mid = db_counts()
    mock = run_batch(Options(base_dir=Path(tmp) / "b3", mock=True, limit=5), prepared=prepared, out=quiet)
    check("--mock: transaction içinde red yazdı", True, mock["sayaclar"].get("red_tablosu_on_eleme", 0) == 64)
    check("--mock: sonra veritabanı aynı", mid, db_counts())
    run_batch(Options(base_dir=Path(tmp) / "b4", no_save=True, limit=3, force=True),
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


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_offline(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_database(tmp)
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
