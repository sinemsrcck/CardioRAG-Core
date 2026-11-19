"""Source-scoped RAG evaluations and atomic persistence. No clinical extraction.

Nothing executes on import. Callers must explicitly authorize real SQL writes.
Uncertain is NULL in the existing SQL enum, never not_supported.
"""
import hashlib
import json

from src import database
from src.claim_validator import _sentences
from src.citation_validator import CITATION_PATTERN

VERSION = "rag-source-evaluation-v1"
QUESTION_TYPES = {"cevaplanabilir", "cevaplanamaz", "celiskili"}
STATUSES = {"generated", "retrieval_only", "insufficient_evidence", "generation_failed",
            "citation_validation_failed", "claim_validation_failed"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def evaluation_rows(result, question_type):
    """Translate an actual report; does not infer question answerability."""
    if question_type not in QUESTION_TYPES:
        raise ValueError("Explicit corpus answerability classification required")
    status = result.get("status")
    if status not in STATUSES:
        raise ValueError("Unknown RAG status")
    if status in ("citation_validation_failed", "claim_validation_failed", "generation_failed") and result.get("answer") is not None:
        raise ValueError("Rejected answer must not be a user answer")
    source_by_id = {s["chunk_id"]: s for s in result.get("sources", [])}
    citation = result.get("citation_check")
    citation_correct = citation.get("valid") if isinstance(citation, dict) else None
    if citation_correct is not None and type(citation_correct) is not bool:
        raise ValueError("Citation verdict must be bool or null")
    # Abstention boilerplate is not a generated answer with failed citations.
    # Preserve its raw syntax report in the audit, but do not score it.
    if status in ("retrieval_only", "insufficient_evidence", "generation_failed"):
        citation_correct = None
    report = result.get("claim_verification") or {}
    claims = report.get("claims", [])
    if not isinstance(claims, list):
        raise ValueError("Invalid claim report")
    if status == "generated" and (citation_correct is not True or not claims or
                                   any(c.get("status") != "supported" for c in claims)):
        raise ValueError("Generated answer has not passed source checks")
    if status == "claim_validation_failed" and not claims and not report.get("error"):
        raise ValueError("Missing failed claim report")
    raw = (result.get("audit") or {}).get("generated_answer") or result.get("answer")
    text_by_hash = {}
    if isinstance(raw, str) and status not in ("insufficient_evidence", "retrieval_only", "generation_failed"):
        for sentence in _sentences(raw):
            text = CITATION_PATTERN.sub("", sentence).strip().rstrip(".").strip()
            text_by_hash[hashlib.sha256(text.encode()).hexdigest()] = text
    base = {"query_id": result["query_id"], "soru_tipi": question_type,
            "evaluation_method": "automated", "review_status": "pending",
            "evaluation_version": VERSION, "kayit_turu": "soru_cevap",
            "citation_correct": citation_correct}
    rows = []
    # Gate/transport failures are run events, not fabricated unsupported claims.
    evaluated = status in ("generated", "claim_validation_failed")
    if evaluated and claims:
        for ordinal, claim in enumerate(claims, 1):
            label = claim.get("status")
            labels = {"supported": "supported", "not_supported": "not_supported",
                      "unsupported": "not_supported", "contradicted": "not_supported",
                      "partial": "partially_supported", "partially_supported": "partially_supported",
                      "uncertain": None}
            if label not in labels:
                raise ValueError("Unknown claim verdict")
            matches = claim.get("matches") or []
            if label == "supported" and not matches:
                raise ValueError("Supported claim requires auditable source matches")
            for match in matches:
                source = source_by_id.get(match.get("chunk_id"))
                if not source:
                    raise ValueError("Claim match references an unselected source")
                for key in ("document_id", "page_number", "processing_version", "text_sha256"):
                    if key in match and match[key] != source.get(key):
                        raise ValueError("Claim/source metadata mismatch")
                quote = match.get("evidence_quote")
                if not isinstance(quote, str) or not quote.strip() or quote not in source.get("source_text", ""):
                    raise ValueError("Claim evidence quote is not in source text")
            text = claim.get("claim_text") or text_by_hash.get(claim.get("claim_text_sha256"))
            if text and claim.get("claim_text_sha256") and hashlib.sha256(text.encode()).hexdigest() != claim["claim_text_sha256"]:
                raise ValueError("Claim text/hash mismatch")
            rows.append({**base, "claim_text": text, "claim_supported": labels[label],
                "supporting_chunk_id": matches[0]["chunk_id"] if matches else None,
                "hata_turu": None if label == "supported" else "claim_" + label,
                "detail": {"claim_index": claim.get("claim_index", ordinal), "decision": label,
                           "claim_report": claim, "scope": report.get("scope"),
                           "missing_claim_text": text is None}})
    else:
        event = "retrieval_only_not_evaluated" if status == "retrieval_only" else status
        if status == "claim_validation_failed":
            event = "claim_validator_error"
        rows.append({**base, "claim_text": None, "claim_supported": None,
                     "supporting_chunk_id": None, "hata_turu": event,
                     "detail": {"decision": "not_evaluated", "event": event,
                                "generation_error": result.get("generation_error")}})
    return rows


def save_rag_bundle(result, *, question_type, connection=None, allow_test_data=False):
    """Explicit SQL write: query, sources and evaluation rows in one transaction.

    Same UUID + identical archived content/classification is idempotent. A UUID
    collision with different content or a legacy incomplete run is rejected.
    Caller-owned connections use a savepoint and are never committed here.
    """
    result = database.public_rag_payload(result)
    if (result.get("run_provenance") or {}).get("kind") == "mock_test":
        if not allow_test_data or connection is None:
            raise ValueError("Mock results cannot be written as real RAG runs")
    elif allow_test_data:
        raise ValueError("Test override requires explicit mock provenance")
    rows = evaluation_rows(result, question_type)
    digest = fingerprint({"result": result, "question_type": question_type, "version": VERSION})
    with database._transaction_scope(connection, "save_rag_bundle") as (conn, cursor):
        if connection is None:
            conn.start_transaction()
        cursor.execute("SELECT query_id FROM rag_sorgulari WHERE query_id=%s FOR UPDATE", (result["query_id"],))
        if cursor.fetchone() is not None:
            cursor.execute("SELECT ham_cikti FROM halusinasyon_degerlendirme WHERE query_id=%s AND evaluation_version=%s ORDER BY id",
                           (result["query_id"], VERSION))
            prior = cursor.fetchall()
            def prior_digest(value):
                if isinstance(value, bytes):
                    value = value.decode("utf-8")
                return (json.loads(value) if isinstance(value, str) else value).get("result_sha256")
            if len(prior) != len(rows) or any(prior_digest(r[0]) != digest for r in prior):
                raise ValueError("Duplicate UUID with different or incomplete content")
            return {"query_id": result["query_id"], "status": "already_recorded", "evaluation_rows": len(rows)}
        database.save_rag_result(result, result["question"], connection=conn)
        for row in rows:
            audit = {"result_sha256": digest, "evaluation": row["detail"],
                     "citation_check": result.get("citation_check"),
                     "evidence_check": result.get("evidence_check"),
                     "generation_status": result["status"],
                     "audit": result.get("audit"), "run_provenance": result.get("run_provenance"),
                     "question_type_provenance": "operator supplied; not inferred from guard",
                     "scientific_verification": False, "expert_approved": False}
            cursor.execute("""INSERT INTO halusinasyon_degerlendirme
                (query_id,soru_tipi,claim_text,supporting_chunk_id,citation_correct,
                 claim_supported,hata_turu,evaluation_method,review_status,
                 evaluation_version,kayit_turu,aciklama,ham_cikti)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (row["query_id"], row["soru_tipi"], row["claim_text"], row["supporting_chunk_id"],
                 row["citation_correct"], row["claim_supported"], row["hata_turu"],
                 row["evaluation_method"], row["review_status"], row["evaluation_version"],
                 row["kayit_turu"], "Automated source-grounded check; not clinical/expert approval",
                 json.dumps(audit, ensure_ascii=False, allow_nan=False)))
    return {"query_id": result["query_id"], "status": "committed" if connection is None else "pending_caller_commit",
            "evaluation_rows": len(rows)}
