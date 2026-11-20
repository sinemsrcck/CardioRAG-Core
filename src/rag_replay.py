"""Replay archived generation through current local validators. No API or SQL.

Original source numbers and bytes are never repaired or replaced. An optional
trusted archive SHA-256 receipt verifies the whole archived mapping; without
one, checks establish internal consistency, not cryptographic authenticity.
"""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import uuid

from src.citation_validator import validate_citations
from src.claim_validator import validate_claims, SCOPE

VERSION = "rag-offline-replay-v1"
BINDING_FIELDS = ("number", "filename", "document_id", "chunk_id", "page_number",
                  "processing_version", "text_sha256")


class ReplayIntegrityError(ValueError):
    pass


def _binding(source):
    return {key: source.get(key) for key in BINDING_FIELDS}


def validate_replay_sources(archive):
    sources = archive.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ReplayIntegrityError("missing_sources")
    for position, source in enumerate(sources, 1):
        if not isinstance(source, dict) or type(source.get("number")) is not int or source["number"] != position:
            raise ReplayIntegrityError("source_number_mapping_mismatch")
        if (any(not isinstance(source.get(k), str) or not source[k].strip() for k in
                ("filename", "document_id", "chunk_id", "processing_version", "source_text", "text_sha256"))
                or type(source.get("page_number")) is not int or source["page_number"] < 1):
            raise ReplayIntegrityError("source_metadata_missing")
        digest = hashlib.sha256(source["source_text"].encode("utf-8")).hexdigest()
        if digest != source["text_sha256"]:
            raise ReplayIntegrityError("source_hash_mismatch")
        if "evidence_text" in source and source["evidence_text"] != source["source_text"].strip():
            raise ReplayIntegrityError("evidence_text_mismatch")
    by_number = {s["number"]: s for s in sources}
    # Old archives often retain redundant original snapshots here. Never
    # renumber or borrow evidence from retrieved/reranked candidate pools.
    for key in ("cited_sources", "unused_sources"):
        snapshots = archive.get(key, [])
        if not isinstance(snapshots, list):
            raise ReplayIntegrityError("source_snapshot_invalid")
        for snapshot in snapshots:
            if (not isinstance(snapshot, dict) or snapshot.get("number") not in by_number
                    or _binding(snapshot) != _binding(by_number[snapshot["number"]])):
                raise ReplayIntegrityError("source_number_mapping_mismatch")
    return sources


def replay_archive(path, *, expected_archive_sha256=None):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_archive_sha256 is not None and digest != expected_archive_sha256:
        raise ReplayIntegrityError("archive_hash_mismatch")
    archive = json.loads(raw.decode("utf-8"))
    if not isinstance(archive, dict) or not isinstance(archive.get("audit") or {}, dict):
        raise ReplayIntegrityError("invalid_archive_structure")
    generated = (archive.get("audit") or {}).get("generated_answer")
    # Legacy successful archives can retain a public answer without an audit.
    if generated is None and archive.get("status") == "generated":
        generated = archive.get("answer")
    result = {"replay_version": VERSION, "replay_id": str(uuid.uuid4()),
              "original_query_id": archive.get("query_id"), "original_status": archive.get("status"),
              "archive_sha256": digest, "trusted_receipt_checked": expected_archive_sha256 is not None,
              "original_llm_model": archive.get("llm_model"),
              "generation_repeated": False, "api_calls": 0, "sql_writes": 0,
              "answer": None, "claims_verified": False, "review_status": "pending",
              "scientific_verification": False, "expert_approved": False,
              "prompt_provenance": "original_archive_only_not_current_prompt",
              "validator_scope": SCOPE}
    if not isinstance(generated, str) or not generated.strip():
        return {**result, "replayed_status": "unavailable", "reason": "no_generated_answer"}
    sources = validate_replay_sources(archive)
    bindings = [_binding(s) for s in sources]
    result["source_bindings"] = bindings
    result["source_bindings_sha256"] = hashlib.sha256(
        json.dumps(bindings, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    citation = validate_citations(generated, sources)
    # Even when syntax fails, local diagnostics can be inspected separately;
    # they never override the citation gate or become a production SQL event.
    claims = validate_claims(generated, sources, question=archive.get("question"))
    passed = citation["valid"] and claims["claims_verified"]
    result.update(citation_check=citation, claim_diagnostics=claims,
                  claims_verified=bool(passed),
                  generated_answer_sha256=hashlib.sha256(generated.encode("utf-8")).hexdigest(),
                  claim_diagnostics_role="release_check" if citation["valid"] else "diagnostic_only_citation_gate_failed",
                  replayed_status=("validated_offline" if passed else "claim_validation_failed"
                                   if citation["valid"] else "citation_validation_failed"))
    return result


def write_replay_report(result, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    result = replay_archive(args.archive, expected_archive_sha256=args.expected_sha256)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir / (str(result["original_query_id"]) + "_replay_" + stamp + ".json")
    write_replay_report(result, output)
    print(json.dumps({"report": str(output), "status": result["replayed_status"]}))


if __name__ == "__main__":
    main()
