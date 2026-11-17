"""Explicit SQL integration entry points; importing this module performs no IO.

RAG and Evidence Guard remain unchanged. Only callers of these wrappers persist
results. Clinical extraction and SQL writes are never triggered by RAG itself.
"""
import json

from src.clinical_extraction import (
    FIELDS, _metadata, _validate_response, extract_clinical_findings,
    prepare_rag_save_kwargs,
)
from src.clinical_review import ClinicalReviewError, CLASSIFICATION_FIELDS, validate_clinical_reviews


def to_clinical_sql_row(finding, *, reviewed_fields=None, source_language=None):
    """Adapt an extracted candidate to the CURRENT SQL/validation contract.

    No diagnosis/enum/translation is inferred. Versioned human classification
    approval is required and checked against the full source candidate. Legacy
    enum-only reviewed_fields records are rejected, not upgraded to approvals.
    The full untranslated spans are retained in taslak_notu.
    """
    from src.database import KLINIK_INSERT_COLUMNS

    if not isinstance(finding, dict) or finding.get("review_status") != "pending":
        raise ValueError("Only pending clinical candidates can be persisted")
    source_candidate = finding.get("source_candidate")
    if not isinstance(source_candidate, dict):
        raise ClinicalReviewError("missing_source_candidate")
    review = finding.get("classification_review") if reviewed_fields is None else reviewed_fields
    reviews = validate_clinical_reviews([source_candidate], {source_candidate.get("aday_id"): review})
    review = reviews[source_candidate["aday_id"]]
    if finding.get("classification_review") is not None and finding["classification_review"] != review:
        raise ClinicalReviewError("review_provenance_mismatch")
    metadata = _metadata(finding)
    source_metadata = _metadata(source_candidate)
    for field in ("filename", "makale_id", "document_id", "sayfa", "sayfa_bitis", "aday_id", "kaynak_turu", "birlestirme"):
        if metadata.get(field) != source_metadata.get(field):
            raise ClinicalReviewError("finding_source_metadata_mismatch")
    payload = {key: finding.get(key) for key in (*FIELDS, "evidence_quote")}
    quote = payload["evidence_quote"]
    _validate_response({"findings": [payload]}, source_candidate["cumle"])
    if source_language not in (None, "en", "tr"):
        raise ValueError("source_language must be en, tr or None")
    if not isinstance(metadata.get("kaynak_turu"), str) or not metadata["kaynak_turu"]:
        raise ValueError("Source evidence type is required")
    if metadata.get("chunk_id") not in (None, "") and (
        not isinstance(metadata["chunk_id"], str) or not metadata["chunk_id"].strip()
    ):
        raise ValueError("chunk_id must be a nonempty source string or absent")
    reviewed_fields = {key: review[key] for key in CLASSIFICATION_FIELDS if key in review}
    row = dict.fromkeys(KLINIK_INSERT_COLUMNS)
    row.update(
        makale_id=metadata["makale_id"], document_id=metadata["document_id"],
        chunk_id=metadata.get("chunk_id") or None,
        kanit_sayfa=metadata["sayfa"], kanit_sayfa_bitis=metadata["sayfa_bitis"],
        kanit_turu=metadata["kaynak_turu"], birlestirme=metadata.get("birlestirme") or None,
        kanit_cumle=quote, review_status="pending", cikarma_yontemi="rag",
        hasta_grubu=finding["patient_group"], comparator=finding["comparison"],
        doz=finding["dose"], sure=finding["treatment_duration"],
        deger=finding["numeric_value"], unit=finding["value_unit"],
        **reviewed_fields,
    )
    if finding["dose"] and finding["dose_unit"]:
        row["doz"] = finding["dose"] + " " + finding["dose_unit"]
    if source_language is not None:
        row["tani_" + source_language] = finding["diagnosis"]
        row["mudahale_" + source_language] = finding["drug"]
    if source_language == "tr":
        row["sonuc_olcutu_tr"] = finding["clinical_outcome"]
    row["taslak_notu"] = json.dumps({
        "filename": metadata["filename"], "aday_id": metadata.get("aday_id"),
        "source_language": source_language,
        "extracted_fields": {field: finding[field] for field in FIELDS},
        "classification_review": review,
        "notice": "Automated candidate; clinical relationships require human review",
    }, ensure_ascii=False)
    return row


def _check_source(row, ctx):
    """Reject a valid-looking ID that belongs to another document/page/evidence."""
    from src.clinical_validation import evidence_in_text, find_chunk_id

    document_id = row["document_id"]
    if ctx.makale_by_doc.get(document_id) != row["makale_id"]:
        raise ValueError("Article/document metadata mismatch")
    chunk_id = row.get("chunk_id")
    if not chunk_id:
        row["chunk_id"] = find_chunk_id(row, ctx)
        return
    start = row["kanit_sayfa"]
    end = row.get("kanit_sayfa_bitis") or start
    for page in range(start, end + 1):
        for source_id, text in ctx.chunks.get((document_id, page), []):
            if source_id == chunk_id and evidence_in_text(
                row["kanit_cumle"], row["kanit_turu"], "", text,
            ):
                return
    raise ValueError("chunk_id does not support this document/page/quotation")


def extract_and_save_clinical_findings(
    articles, *, client=None, model=None, source_language=None,
    reviewed_fields_by_candidate=None, query_id=None, connection=None,
):
    """collect_candidates -> extract -> adapter -> current validator/SQL writer.

    articles are the existing (filename, makale_id, document_id, full_text)
    tuples; this function does not load them or roll back caller-owned reads.
    SQL failures are returned separately from extracted findings. Own connection
    commits atomically; supplied connection uses savepoints and NEVER commits.
    No automatic verified labels. Repeated findings use the writer's stable hash.
    """
    from src.clinical_candidates import collect_candidates
    from src.clinical_validation import load_validation_context, validate_row
    from src.database import _transaction_scope, _column_max_lengths, KLINIK_INSERT_COLUMNS, save_clinical_findings

    candidates = collect_candidates(list(articles))
    reviews = validate_clinical_reviews(candidates, reviewed_fields_by_candidate)
    extraction = extract_clinical_findings(
        candidates, client=client, model=model, reviewed_fields_by_candidate=reviews,
    )
    report = {**extraction, "adapter_rejected": [], "sql_result": None,
              "sql_status": "not_attempted", "sql_error": None}
    if extraction.get("stopped") or extraction.get("rejected"):
        report.update(stopped=True, findings=[],
                      partial_findings=extraction.get("partial_findings", extraction["findings"]))
        report.setdefault("stop_reason", "extraction_rejected")
        return report
    rows = []
    for finding in extraction["findings"]:
        try:
            rows.append(to_clinical_sql_row(
                finding, source_language=source_language,
                reviewed_fields=reviews.get(finding.get("aday_id")),
            ))
        except ValueError as exc:
            report["adapter_rejected"].append({"aday_id": finding.get("aday_id"), "reason": str(exc)})
    if report["adapter_rejected"]:
        report.update(stopped=True, stop_reason="adapter_validation_error", partial_findings=report["findings"], findings=[])
        return report
    if not rows:
        return report
    try:
        # Reuse the existing transaction implementation. A distinct outer
        # savepoint includes our context checks and the writer's own savepoint.
        with _transaction_scope(connection, "clinical_llm_pipeline") as (conn, cursor):
            ctx = load_validation_context(conn)
            max_lengths = _column_max_lengths(cursor, "klinik_bulgular")
            checked = []
            for row in rows:
                try:
                    _check_source(row, ctx)
                    ok, error_type, _ = validate_row(row, ctx)
                    if not ok:
                        raise ValueError(error_type)
                    if any(isinstance(row.get(column), str) and max_lengths.get(column)
                           and len(row[column]) > max_lengths[column] for column in KLINIK_INSERT_COLUMNS):
                        raise ValueError("clinical_field_too_long")
                    checked.append(row)
                except ValueError as exc:
                    report["adapter_rejected"].append({"document_id": row["document_id"],
                                                       "chunk_id": row.get("chunk_id"), "reason": str(exc)})
            if report["adapter_rejected"]:
                raise ValueError("Source validation failed; no clinical rows may be saved")
            if checked:
                sql_result = save_clinical_findings(
                    checked, cikarma_yontemi="rag", query_id=query_id, connection=conn,
                )
                if sql_result["reddedilen"]:
                    report["sql_rejected"] = sql_result["reddedilen"]
                    raise ValueError("Clinical rows rejected; rolling back the entire write scope")
            else:
                sql_result = {"yazilan": 0, "zaten_var": 0, "reddedilen": [], "halusinasyon_kaydi": 0}
        report["sql_result"] = sql_result
        report["sql_status"] = "committed" if connection is None else "pending_caller_commit"
    except Exception as exc:
        report.update(sql_status="error", sql_error=type(exc).__name__)
    return report


def answer_question_with_sql(question, chunks=None, *, connection=None, **rag_options):
    """Run unchanged RAG, then best-effort persist its result.

    Omit chunks to read load_chunks_for_rag with the requested embedding model.
    Loading failure cannot produce a RAG answer without evidence; it is reported
    as data_unavailable. Logging failures always preserve the completed answer.
    Repeated RAG calls are distinct queries (the SQL writer generates a UUID).
    """
    from src.database import load_chunks_for_rag, save_rag_result
    from src.rag import DEFAULT_LLM, answer_question

    if chunks is None:
        try:
            chunks = load_chunks_for_rag(
                embedding_model=rag_options.get("embedding_model", "minilm"), connection=connection,
            )
        except Exception as exc:
            return {"question": question, "answer": None, "status": "data_unavailable",
                    "sql_persistence": {"status": "error", "stage": "load_chunks", "error": type(exc).__name__}}
    result = answer_question(question=question, chunks=chunks, **rag_options)
    try:
        kwargs = prepare_rag_save_kwargs(result, llm_model=DEFAULT_LLM)
        query_id = save_rag_result(**kwargs, connection=connection)
        persistence = {"status": "committed" if connection is None else "pending_caller_commit",
                       "query_id": query_id}
    except Exception as exc:
        persistence = {"status": "error", "stage": "save_rag", "error": type(exc).__name__}
    return {**result, "sql_persistence": persistence}
