"""Human classification preflight. No approvals, API calls, SQL or file writes.

reviewed_fields_by_candidate remains an ID -> record dictionary. When omitted,
CLINICAL_REVIEW_MAP may contain the same dictionary as JSON (not a file path).
Legacy ordinal IDs and enum-only values are deliberately not trusted. Provenance
is explicitly declared; authenticating the reviewer is the caller's responsibility.
"""
import json
import os
from copy import deepcopy
from datetime import datetime

from src.clinical_candidates import CANDIDATE_ID_VERSION, candidate_binding
from src.clinical_validation import MUDAHALE_TURU, ETKI_TURU, YON, OUTCOME_TYPE


REVIEW_SCHEMA_VERSION = "clinical-review-v1"
CLASSIFICATION_FIELDS = {"mudahale_turu": MUDAHALE_TURU, "etki_turu": ETKI_TURU,
                         "yon": YON, "outcome_type": OUTCOME_TYPE}
PROVENANCE_FIELDS = {"schema_version", "approval_status", "review_source", "reviewer_id", "reviewed_at"}


class ClinicalReviewError(ValueError):
    """Safe, explicit batch preflight failure; never contains candidate text."""

    def __init__(self, code, candidate_index=None):
        self.code = code
        self.candidate_index = candidate_index
        super().__init__(f"Clinical review preflight failed: {code}; candidate_index={candidate_index}")


def _source_binding(candidate, index):
    try:
        binding = candidate_binding(candidate)
    except (ValueError, TypeError):
        raise ClinicalReviewError("invalid_candidate_source", index) from None
    filename = candidate.get("filename") or candidate.get("dosya")
    if not isinstance(filename, str) or not filename.strip() or not isinstance(candidate.get("makale_id"), str) or not candidate["makale_id"].strip():
        raise ClinicalReviewError("missing_candidate_metadata", index)
    if candidate.get("filename") and candidate.get("dosya") and candidate["filename"] != candidate["dosya"]:
        raise ClinicalReviewError("conflicting_candidate_filename", index)
    if not isinstance(candidate.get("kaynak_turu"), str) or not candidate["kaynak_turu"]:
        raise ClinicalReviewError("missing_candidate_source_type", index)
    if candidate.get("aday_id") != binding["aday_id"]:
        raise ClinicalReviewError("legacy_or_mismatched_candidate_id", index)
    if candidate.get("source_text_sha256", binding["source_text_sha256"]) != binding["source_text_sha256"]:
        raise ClinicalReviewError("candidate_source_hash_mismatch", index)
    if candidate.get("candidate_id_version", CANDIDATE_ID_VERSION) != CANDIDATE_ID_VERSION:
        raise ClinicalReviewError("candidate_id_version_mismatch", index)
    return {**binding, "makale_id": candidate["makale_id"]}


def build_review_template(candidates):
    """Return an UNAPPROVED template for humans to fill; never infer a class."""
    result = {}
    for index, candidate in enumerate(candidates):
        binding = _source_binding(candidate, index)
        result[binding["aday_id"]] = {
            **binding, "schema_version": REVIEW_SCHEMA_VERSION, "mudahale_turu": None,
            "approval_status": "pending_review", "review_source": None,
            "reviewer_id": None, "reviewed_at": None,
        }
    return result


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_review_keys")
        result[key] = value
    return result


def validate_clinical_reviews(candidates, reviewed_fields_by_candidate=None):
    """Validate ALL selected candidates before any client/API/SQL initialization.

    Catalog entries for unselected candidates are not used. Every selected entry
    must bind exact source text/document/page and explicitly declare human
    approval, reviewer and timezone-aware timestamp. No legacy auto-migration.
    """
    candidates = list(candidates)
    bindings = [_source_binding(candidate, index) for index, candidate in enumerate(candidates)]
    reviews = reviewed_fields_by_candidate
    if reviews is None:
        if not candidates:
            return {}
        raw = os.getenv("CLINICAL_REVIEW_MAP")
        if not raw:
            raise ClinicalReviewError("missing_review_map")
        try:
            reviews = json.loads(raw, object_pairs_hook=_unique_json_object)
        except (ValueError, TypeError):
            raise ClinicalReviewError("invalid_review_map_json") from None
    if not isinstance(reviews, dict):
        raise ClinicalReviewError("review_map_must_be_object")
    approved = {}
    for index, binding in enumerate(bindings):
        review = reviews.get(binding["aday_id"])
        if not isinstance(review, dict):
            raise ClinicalReviewError("missing_candidate_approval", index)
        required = set(binding) | PROVENANCE_FIELDS | {"mudahale_turu"}
        if not required.issubset(review) or set(review) - (required | set(CLASSIFICATION_FIELDS) | {"review_notes"}):
            raise ClinicalReviewError("invalid_review_schema", index)
        if review["schema_version"] != REVIEW_SCHEMA_VERSION:
            raise ClinicalReviewError("review_schema_version_mismatch", index)
        for field, expected in binding.items():
            if type(review[field]) is not type(expected) or review[field] != expected:
                raise ClinicalReviewError("review_source_binding_mismatch", index)
        if review["approval_status"] != "approved" or review["review_source"] != "human":
            raise ClinicalReviewError("explicit_human_approval_required", index)
        if not isinstance(review["reviewer_id"], str) or not review["reviewer_id"].strip():
            raise ClinicalReviewError("missing_reviewer_identity", index)
        try:
            timestamp = datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ClinicalReviewError("invalid_review_timestamp", index) from None
        for field, allowed in CLASSIFICATION_FIELDS.items():
            value = review.get(field)
            if field != "mudahale_turu" and value is None:
                continue
            if not isinstance(value, str) or value not in allowed:
                raise ClinicalReviewError("invalid_clinical_classification", index)
        if "review_notes" in review and not isinstance(review["review_notes"], str):
            raise ClinicalReviewError("invalid_review_notes", index)
        approved[binding["aday_id"]] = deepcopy(review)
    return approved
