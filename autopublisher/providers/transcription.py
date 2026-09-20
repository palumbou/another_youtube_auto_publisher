"""Transcription providers: time-aligned segments with confidence, never silently
"corrected". The manifest provider is the local/mock path; Amazon Transcribe is
the AWS path. Both produce the same `Transcript`."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autopublisher.providers import ProviderError

LOW_CONFIDENCE = 0.75


@dataclass
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float = 1.0


@dataclass
class Transcript:
    language: str
    provider: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    model_version: str = ""

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    @property
    def mean_confidence(self) -> float:
        if not self.segments:
            return 0.0
        return round(sum(s.confidence for s in self.segments) / len(self.segments), 3)

    def low_confidence(self, threshold: float = LOW_CONFIDENCE) -> list[TranscriptSegment]:
        return [s for s in self.segments if s.confidence < threshold]

    def within(self, start_ms: int, end_ms: int) -> list[TranscriptSegment]:
        return [s for s in self.segments if s.end_ms > start_ms and s.start_ms < end_ms]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Transcript:
        return cls(language=data.get("language", ""), provider=data.get("provider", ""),
                   segments=[TranscriptSegment(**s) for s in data.get("segments", [])],
                   model_version=data.get("model_version", ""))


@dataclass
class TranscriptionRequest:
    source_path: Path | None = None
    source_uri: str = ""  # s3://bucket/key for cloud providers
    language_hint: str = ""
    manifest: dict | None = None
    has_speech: bool = True


class TranscriptionProvider(ABC):
    name = "abstract"

    @abstractmethod
    def transcribe(self, request: TranscriptionRequest) -> Transcript: ...


class ManifestTranscriptionProvider(TranscriptionProvider):
    """Local/mock provider: the manifest texts become the transcript, so the
    end-to-end path is exercised without a paid service. Confidence is fixed."""

    name = "manifest-mock"

    def __init__(self, confidence: float = 0.98, low_confidence_segments: tuple[str, ...] = ()):
        self.confidence = confidence
        self.low = low_confidence_segments

    def transcribe(self, request: TranscriptionRequest) -> Transcript:
        if not request.manifest:
            return Transcript(language=request.language_hint or "", provider=self.name, model_version="1")
        segments = []
        for seg in request.manifest["segments"]:
            text = seg.get("text", "")
            if text:
                conf = 0.4 if seg["segment_id"] in self.low else self.confidence
                segments.append(TranscriptSegment(seg["start_ms"], seg["end_ms"], text, conf))
        return Transcript(language=request.manifest["language"], provider=self.name,
                          segments=segments, model_version="1")


class FixtureTranscriptionProvider(TranscriptionProvider):
    name = "fixture"

    def __init__(self, path: Path):
        self.path = Path(path)

    def transcribe(self, request: TranscriptionRequest) -> Transcript:
        return Transcript.from_dict(json.loads(self.path.read_text()))


class FailingTranscriptionProvider(TranscriptionProvider):
    name = "failing"

    def transcribe(self, request: TranscriptionRequest) -> Transcript:
        raise ProviderError("transcription provider unavailable")


class TranscribeProvider(TranscriptionProvider):
    """Amazon Transcribe. The audio must already be in S3 (`source_uri`)."""

    name = "amazon-transcribe"

    def __init__(self, client=None, output_bucket: str = "", poll_seconds: int = 15, max_wait_seconds: int = 3600):
        import boto3

        self.client = client or boto3.client("transcribe")
        self.output_bucket = output_bucket
        self.poll_seconds = poll_seconds
        self.max_wait = max_wait_seconds

    @staticmethod
    def _locale(language: str) -> str:
        return {"it": "it-IT", "en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE"}.get(language, language)

    def transcribe(self, request: TranscriptionRequest) -> Transcript:
        if not request.source_uri:
            raise ProviderError("Amazon Transcribe needs an s3:// source uri")
        job_name = "autopub-" + request.source_uri.rsplit("/", 1)[-1].replace(".", "-")[:180] + f"-{int(time.time())}"
        kwargs = {"TranscriptionJobName": job_name, "Media": {"MediaFileUri": request.source_uri}}
        if request.language_hint:
            kwargs["LanguageCode"] = self._locale(request.language_hint)
        else:
            kwargs["IdentifyLanguage"] = True
        if self.output_bucket:
            kwargs["OutputBucketName"] = self.output_bucket
        self.client.start_transcription_job(**kwargs)
        waited = 0
        while waited < self.max_wait:
            status = self.client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
            state = status["TranscriptionJobStatus"]
            if state == "COMPLETED":
                return self._parse(status)
            if state == "FAILED":
                raise ProviderError(f"transcription failed: {status.get('FailureReason', '')[:200]}")
            time.sleep(self.poll_seconds)
            waited += self.poll_seconds
        raise ProviderError("transcription timed out")

    def _parse(self, status: dict) -> Transcript:
        import urllib.request

        uri = status["Transcript"]["TranscriptFileUri"]
        with urllib.request.urlopen(uri) as response:
            data = json.loads(response.read())
        return parse_transcribe_output(data, status.get("LanguageCode", ""))


def parse_transcribe_output(data: dict, language_code: str = "") -> Transcript:
    """Group Amazon Transcribe items into sentence segments keeping the minimum confidence."""
    results = data.get("results", {})
    items = results.get("items", [])
    segments: list[TranscriptSegment] = []
    words: list[tuple[float, float, str, float]] = []

    def flush():
        if words:
            segments.append(TranscriptSegment(
                start_ms=round(words[0][0] * 1000), end_ms=round(words[-1][1] * 1000),
                text=" ".join(w[2] for w in words), confidence=round(min(w[3] for w in words), 3)))
            words.clear()

    for item in items:
        alt = item.get("alternatives", [{}])[0]
        if item.get("type") == "punctuation":
            if words:
                words[-1] = (words[-1][0], words[-1][1], words[-1][2] + alt.get("content", ""), words[-1][3])
            if alt.get("content") in (".", "?", "!"):
                flush()
            continue
        words.append((float(item.get("start_time", 0)), float(item.get("end_time", 0)),
                      alt.get("content", ""), float(alt.get("confidence", 1.0))))
    flush()
    language = (language_code or results.get("language_code", "")).split("-")[0]
    return Transcript(language=language, provider=TranscribeProvider.name, segments=segments)
