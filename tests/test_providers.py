import pytest

from autopublisher.providers import ProviderError
from autopublisher.providers.analysis import (
    AnalysisInput,
    AnalysisValidationError,
    FailingAnalysisProvider,
    GroundingError,
    ManifestAnalysisProvider,
    UngroundedAnalysisProvider,
    ground,
    validate_analysis,
)
from autopublisher.providers.transcription import (
    FailingTranscriptionProvider,
    ManifestTranscriptionProvider,
    Transcript,
    TranscriptionRequest,
    parse_transcribe_output,
)
from tests import helpers


def test_manifest_transcription_keeps_timing_and_confidence():
    manifest = helpers.tiny_manifest()
    provider = ManifestTranscriptionProvider(low_confidence_segments=("answer-01",))
    transcript = provider.transcribe(TranscriptionRequest(manifest=manifest))
    assert transcript.language == "it"
    assert [s.start_ms for s in transcript.segments] == [0, 400, 1200, 1500]
    assert [s.text for s in transcript.low_confidence()] == ["Il Po"]
    assert transcript.within(1000, 1300)[0].text == "Il Po"
    assert Transcript.from_dict(transcript.to_dict()) == transcript


def test_failing_providers_raise_provider_error():
    with pytest.raises(ProviderError):
        FailingTranscriptionProvider().transcribe(TranscriptionRequest())
    with pytest.raises(ProviderError):
        FailingAnalysisProvider().analyze(AnalysisInput(manifest=None, transcript=None))


def test_transcribe_output_parsing_groups_sentences_with_min_confidence():
    data = {"results": {"language_code": "it-IT", "items": [
        {"type": "pronunciation", "start_time": "0.1", "end_time": "0.4",
         "alternatives": [{"content": "Ciao", "confidence": "0.99"}]},
        {"type": "pronunciation", "start_time": "0.5", "end_time": "0.9",
         "alternatives": [{"content": "mondo", "confidence": "0.60"}]},
        {"type": "punctuation", "alternatives": [{"content": "."}]},
        {"type": "pronunciation", "start_time": "1.0", "end_time": "1.4",
         "alternatives": [{"content": "Fine", "confidence": "0.95"}]},
    ]}}
    transcript = parse_transcribe_output(data)
    assert transcript.language == "it"
    assert [(s.text, s.confidence) for s in transcript.segments] == [("Ciao mondo.", 0.6), ("Fine", 0.95)]


def test_mock_analysis_is_schema_valid_and_grounded():
    manifest = helpers.tiny_manifest()
    transcript = ManifestTranscriptionProvider().transcribe(TranscriptionRequest(manifest=manifest))
    analysis = validate_analysis(ManifestAnalysisProvider().analyze(AnalysisInput(manifest, transcript)))
    assert len(analysis["title_candidates"]) == 3
    assert ground(analysis, manifest, transcript) == []
    assert analysis["clip_candidates"][0]["candidate_id"] == "short-01"


def test_ungrounded_claims_are_rejected():
    manifest = helpers.tiny_manifest()
    analysis = validate_analysis(UngroundedAnalysisProvider().analyze(AnalysisInput(manifest, None)))
    with pytest.raises(GroundingError):
        ground(analysis, manifest, None)


def test_invalid_structured_output_is_rejected():
    manifest = helpers.tiny_manifest()
    analysis = ManifestAnalysisProvider().analyze(AnalysisInput(manifest, None))
    analysis["hashtags"] = ["#a", "#b", "#c", "#d"]
    with pytest.raises(AnalysisValidationError):
        validate_analysis(analysis)
    del analysis["hashtags"]
    with pytest.raises(AnalysisValidationError):
        validate_analysis(analysis)


def test_grounding_needs_a_corpus():
    manifest = helpers.tiny_manifest()
    analysis = ManifestAnalysisProvider().analyze(AnalysisInput(manifest, None))
    with pytest.raises(GroundingError):
        ground(analysis, None, None)
