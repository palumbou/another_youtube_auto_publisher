"""Producer-to-publisher contract: the video job manifest, version 1.x.

The JSON Schema in `contracts/video-job-manifest/<version>/schema.json` is the
structural authority. This module adds the semantic rules of the contract
(ordering, ranges, geometry, rights, Short completeness) and maps every failure
to a stable machine code with a JSON pointer, never a secret value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contracts" / "video-job-manifest"
SUPPORTED_MAJOR = 1
SUPPORTED_VERSIONS = ("1.0.0",)

# Stable error codes (contract section "Error contract").
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
MISSING_READY_DEPENDENCY = "MISSING_READY_DEPENDENCY"
SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
SOURCE_VERSION_CHANGED = "SOURCE_VERSION_CHANGED"
INVALID_MEDIA = "INVALID_MEDIA"
TIMESTAMP_OUT_OF_RANGE = "TIMESTAMP_OUT_OF_RANGE"
SEGMENT_ORDER_INVALID = "SEGMENT_ORDER_INVALID"
RIGHTS_DECLARATION_MISSING = "RIGHTS_DECLARATION_MISSING"
PRE_CUTOVER_JOB = "PRE_CUTOVER_JOB"
DUPLICATE_JOB = "DUPLICATE_JOB"
UNSUPPORTED_PROJECT = "UNSUPPORTED_PROJECT"
# Codes added by this implementation, same stability rules.
MANIFEST_SCHEMA_INVALID = "MANIFEST_SCHEMA_INVALID"
GEOMETRY_INVALID = "GEOMETRY_INVALID"
SHORT_INCOMPLETE = "SHORT_INCOMPLETE"
UNSAFE_OBJECT_KEY = "UNSAFE_OBJECT_KEY"

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ContractError:
    code: str
    message: str
    pointer: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "pointer": self.pointer}


class ContractViolation(ValueError):
    """Raised when a manifest or a job fails the contract. Carries every error found."""

    def __init__(self, errors: list[ContractError]):
        self.errors = list(errors)
        super().__init__("; ".join(f"{e.code} at '{e.pointer}': {e.message}" for e in self.errors))

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.errors]


@lru_cache(maxsize=8)
def load_schema(version: str = "1.0.0") -> dict:
    path = CONTRACT_DIR / version / "schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def validator(version: str = "1.0.0") -> Draft202012Validator:
    schema = load_schema(version)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def parse_version(value) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = SEMVER.match(value)
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def version_errors(manifest: dict) -> list[ContractError]:
    version = parse_version(manifest.get("schema_version"))
    if version is None:
        return [ContractError(UNSUPPORTED_SCHEMA_VERSION,
                              "schema_version must be a semantic version string", "/schema_version")]
    if version[0] != SUPPORTED_MAJOR:
        return [ContractError(
            UNSUPPORTED_SCHEMA_VERSION,
            f"major version {version[0]} is not supported; this publisher accepts {SUPPORTED_MAJOR}.x",
            "/schema_version")]
    if manifest.get("schema_version") not in SUPPORTED_VERSIONS:
        return [ContractError(
            UNSUPPORTED_SCHEMA_VERSION,
            f"version {manifest.get('schema_version')} is newer than the supported "
            f"{', '.join(SUPPORTED_VERSIONS)}; deploy consumer compatibility first",
            "/schema_version")]
    return []


def schema_errors(manifest: dict, version: str = "1.0.0") -> list[ContractError]:
    errors = []
    for err in sorted(validator(version).iter_errors(manifest), key=lambda e: list(e.absolute_path)):
        pointer = "/" + "/".join(str(p) for p in err.absolute_path)
        errors.append(ContractError(MANIFEST_SCHEMA_INVALID, err.message[:300], pointer))
    return errors


def _rect_ok(rect: dict) -> bool:
    return rect["x"] + rect["width"] <= 1.0 + 1e-9 and rect["y"] + rect["height"] <= 1.0 + 1e-9


def semantic_errors(manifest: dict) -> list[ContractError]:
    """Rules the schema cannot express. Assumes the schema already passed."""
    errors: list[ContractError] = []
    duration = manifest["source"]["duration_ms"]
    segments = manifest["segments"]
    by_id: dict[str, dict] = {}

    previous_end = 0
    for i, seg in enumerate(segments):
        ptr = f"/segments/{i}"
        if seg["segment_id"] in by_id:
            errors.append(ContractError(SEGMENT_ORDER_INVALID, "duplicate segment_id", ptr))
        by_id[seg["segment_id"]] = seg
        if seg["start_ms"] >= seg["end_ms"]:
            errors.append(ContractError(TIMESTAMP_OUT_OF_RANGE, "start_ms must be below end_ms", ptr))
        if seg["end_ms"] > duration:
            errors.append(ContractError(TIMESTAMP_OUT_OF_RANGE,
                                        f"end_ms {seg['end_ms']} exceeds source duration {duration}", ptr))
        if seg["start_ms"] < previous_end:
            errors.append(ContractError(SEGMENT_ORDER_INVALID,
                                        "segments must be ordered and non-overlapping", ptr))
        previous_end = max(previous_end, seg["end_ms"])
        for key in ("region_of_interest", "text_safe_area"):
            if key in seg and not _rect_ok(seg[key]):
                errors.append(ContractError(GEOMETRY_INVALID,
                                            "x + width and y + height must not exceed 1", f"{ptr}/{key}"))

    # The reveal must never be cut from the question: within one question the
    # semantic order is QUESTION < COUNTDOWN < ANSWER < EXPLANATION.
    rank = {"QUESTION": 0, "COUNTDOWN": 1, "ANSWER": 2, "EXPLANATION": 3}
    last_rank: dict[str, int] = {}
    for i, seg in enumerate(segments):
        qid = seg.get("question_id")
        if not qid or seg["type"] not in rank:
            continue
        if last_rank.get(qid, -1) > rank[seg["type"]]:
            errors.append(ContractError(SEGMENT_ORDER_INVALID,
                                        f"{seg['type']} of {qid} appears after a later phase",
                                        f"/segments/{i}"))
        last_rank[qid] = max(last_rank.get(qid, -1), rank[seg["type"]])

    for i, cand in enumerate(manifest["short_candidates"]):
        ptr = f"/short_candidates/{i}"
        if cand["start_ms"] >= cand["end_ms"]:
            errors.append(ContractError(TIMESTAMP_OUT_OF_RANGE, "start_ms must be below end_ms", ptr))
        if cand["end_ms"] > duration:
            errors.append(ContractError(TIMESTAMP_OUT_OF_RANGE, "end_ms exceeds source duration", ptr))
        if "crop_region" in cand and not _rect_ok(cand["crop_region"]):
            errors.append(ContractError(GEOMETRY_INVALID,
                                        "x + width and y + height must not exceed 1", f"{ptr}/crop_region"))
        members = []
        for sid in cand["segment_ids"]:
            if sid not in by_id:
                errors.append(ContractError(SEGMENT_ORDER_INVALID, f"unknown segment_id {sid}", ptr))
            else:
                members.append(by_id[sid])
        if not members:
            continue
        lo = min(s["start_ms"] for s in members)
        hi = max(s["end_ms"] for s in members)
        if cand["start_ms"] > lo or cand["end_ms"] < hi:
            errors.append(ContractError(SHORT_INCOMPLETE,
                                        "the candidate window must cover every declared segment", ptr))
        types = {s["type"] for s in members}
        questions = {s.get("question_id") for s in members if s.get("question_id")}
        if "QUESTION" in types and "ANSWER" not in types:
            errors.append(ContractError(SHORT_INCOMPLETE, "a Short with a question must include its answer", ptr))
        if cand.get("must_include_explanation", True):
            for qid in questions:
                declared = [s for s in segments if s.get("question_id") == qid and s["type"] == "EXPLANATION"]
                if declared and not any(s["type"] == "EXPLANATION" and s.get("question_id") == qid
                                        for s in members):
                    errors.append(ContractError(SHORT_INCOMPLETE,
                                                f"the declared explanation of {qid} is not included", ptr))

    if manifest["rights"]["source_owner_confirmed"] is not True:
        errors.append(ContractError(RIGHTS_DECLARATION_MISSING,
                                    "source ownership is not confirmed", "/rights/source_owner_confirmed"))
    for i, asset in enumerate(manifest["rights"]["assets"]):
        if not asset.get("allowed_for_youtube"):
            errors.append(ContractError(RIGHTS_DECLARATION_MISSING,
                                        f"asset {asset['asset_id']} is not allowed for YouTube",
                                        f"/rights/assets/{i}/allowed_for_youtube"))
    return errors


def validate_manifest(manifest) -> dict:
    """Return the manifest when it is valid; raise ContractViolation otherwise."""
    if not isinstance(manifest, dict):
        raise ContractViolation([ContractError(MANIFEST_SCHEMA_INVALID, "manifest must be a JSON object", "")])
    errors = version_errors(manifest)
    if errors:
        raise ContractViolation(errors)
    errors = schema_errors(manifest, manifest["schema_version"])
    if errors:
        raise ContractViolation(errors)
    errors = semantic_errors(manifest)
    if errors:
        raise ContractViolation(errors)
    return manifest


def load_and_validate(path: Path) -> dict:
    return validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
