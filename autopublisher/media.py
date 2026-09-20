"""ffprobe/ffmpeg helpers shared by ingestion and the media worker."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    width: int
    height: int
    frame_rate: float
    video_codec: str
    audio_codec: str
    has_audio: bool
    rotation: int
    bit_rate: int
    size_bytes: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"{cmd[0]} failed: {exc.stderr[-500:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} timed out after {timeout}s") from exc


def ffprobe(path: Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError(f"{path.name}: missing or empty file")
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned no JSON") from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None or "duration" not in data.get("format", {}):
        raise MediaError(f"{path.name}: no decodable video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if float(den or 1) else 0.0
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(side["rotation"])
    return MediaInfo(
        duration_ms=round(float(data["format"]["duration"]) * 1000),
        width=int(video["width"]), height=int(video["height"]), frame_rate=round(fps, 3),
        video_codec=video.get("codec_name", ""), audio_codec=audio.get("codec_name", "") if audio else "",
        has_audio=audio is not None, rotation=rotation,
        bit_rate=int(data["format"].get("bit_rate", 0) or 0), size_bytes=path.stat().st_size,
    )


def available_encoder(preferred: str = "libx264") -> str:
    """Prefer libx264; fall back to libopenh264 on distributions that ship ffmpeg-free."""
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg is not installed")
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                              check=False).stdout
    for candidate in (preferred, "libx264", "libopenh264"):
        if f" {candidate} " in encoders:
            return candidate
    raise MediaError("no H.264 encoder available (libx264 or libopenh264 required)")
