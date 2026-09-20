import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from autopublisher import contract

FIXTURES = contract.CONTRACT_DIR / "1.0.0" / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_schema_is_valid_draft_2020_12():
    schema = contract.load_schema("1.0.0")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_valid_fixture_passes():
    assert contract.validate_manifest(load("valid.json"))["job_id"] == "qav-20260919-0001"


@pytest.mark.parametrize("name,code,pointer", [
    ("invalid-hash-format.json", contract.MANIFEST_SCHEMA_INVALID, "/source/sha256"),
    ("invalid-segment-order.json", contract.SEGMENT_ORDER_INVALID, "/segments/3"),
    ("invalid-timestamp-out-of-range.json", contract.TIMESTAMP_OUT_OF_RANGE, "/segments/5"),
    ("unsupported-major-version.json", contract.UNSUPPORTED_SCHEMA_VERSION, "/schema_version"),
    ("invalid-rights-missing.json", contract.RIGHTS_DECLARATION_MISSING, "/rights/source_owner_confirmed"),
    ("invalid-short-cuts-explanation.json", contract.SHORT_INCOMPLETE, "/short_candidates/0"),
    ("invalid-geometry.json", contract.GEOMETRY_INVALID, "/segments/0/region_of_interest"),
])
def test_invalid_fixtures_have_stable_codes(name, code, pointer):
    with pytest.raises(contract.ContractViolation) as info:
        contract.validate_manifest(load(name))
    assert code in info.value.codes
    assert any(e.pointer == pointer for e in info.value.errors), info.value.errors


def test_hash_mismatch_fixture_is_structurally_valid():
    # The mismatch is only detectable against the object: covered by the ingestion tests.
    contract.validate_manifest(load("invalid-hash-mismatch.json"))


def test_future_minor_version_is_rejected_until_supported():
    manifest = load("valid.json")
    manifest["schema_version"] = "1.9.0"
    with pytest.raises(contract.ContractViolation) as info:
        contract.validate_manifest(manifest)
    assert info.value.codes == [contract.UNSUPPORTED_SCHEMA_VERSION]


def test_unknown_fields_are_rejected():
    manifest = load("valid.json")
    manifest["surprise"] = 1
    with pytest.raises(contract.ContractViolation) as info:
        contract.validate_manifest(manifest)
    assert info.value.codes == [contract.MANIFEST_SCHEMA_INVALID]


def test_errors_never_carry_secret_values():
    manifest = load("valid.json")
    manifest["rights"]["notes"] = "token=SHOULD-NOT-LEAK"
    manifest["segments"][0]["end_ms"] = 99999999
    with pytest.raises(contract.ContractViolation) as info:
        contract.validate_manifest(manifest)
    assert "SHOULD-NOT-LEAK" not in str(info.value)


def test_event_fixtures_exist_for_the_ingestion_suite():
    for name in ("duplicate-and-out-of-order-ready-events.json",
                 "pre-cutover-ready-event.json", "missing-ready.json"):
        assert (FIXTURES / "events" / name).is_file()


def test_tiny_fixture_video_is_small_and_redistributable():
    video = Path(__file__).parent / "fixtures" / "tiny.mp4"
    assert video.stat().st_size < 200_000
