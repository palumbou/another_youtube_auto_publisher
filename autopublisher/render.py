"""ffmpeg rendering and verification for Shorts and thumbnails, with a
deterministic command log and SHA-256 of every output."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from autopublisher.media import MediaError, MediaInfo, ffprobe
from autopublisher.shorts import SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, ShortPlan

FONT_CANDIDATES = (
    "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
OUT_W, OUT_H = 1080, 1920
TARGET_LUFS = -14.0


def find_font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise MediaError("no caption font found; install Noto Sans or Liberation Sans")


@dataclass
class CommandLog:
    entries: list[dict] = field(default_factory=list)

    def run(self, cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
        rendered = " ".join(shlex.quote(str(c)) for c in cmd)
        try:
            proc = subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            self.entries.append({"cmd": rendered, "ok": False, "stderr": exc.stderr[-800:]})
            raise MediaError(f"{cmd[0]} failed: {exc.stderr[-400:]}") from exc
        self.entries.append({"cmd": rendered, "ok": True})
        return proc

    def to_json(self) -> str:
        return json.dumps(self.entries, indent=2)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%").replace(",", "\\,")


def _wrap(text: str, width: int = 26) -> str:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\n".join(lines[:6])


def crop_filter(info: MediaInfo, crop_region: dict | None) -> str:
    """Crop to the declared region, widened or narrowed to an exact 9:16 box that
    stays inside the frame, so the output never needs black bars."""
    sw, sh = info.width, info.height
    if crop_region:
        x, y = crop_region["x"] * sw, crop_region["y"] * sh
        w, h = crop_region["width"] * sw, crop_region["height"] * sh
    else:
        x, y, w, h = 0.0, 0.0, float(sw), float(sh)
    # Adjust to 9:16 around the region's centre.
    cx, cy = x + w / 2, y + h / 2
    if w / h > 9 / 16:
        w = h * 9 / 16
    else:
        h = w * 16 / 9
    if h > sh:
        h, w = float(sh), sh * 9 / 16
    if w > sw:
        w, h = float(sw), sw * 16 / 9
    x = min(max(0.0, cx - w / 2), sw - w)
    y = min(max(0.0, cy - h / 2), sh - h)
    w, h = int(w) // 2 * 2, int(h) // 2 * 2
    return f"crop={w}:{h}:{int(x)}:{int(y)},scale={OUT_W}:{OUT_H}:flags=lanczos"


def caption_filters(plan: ShortPlan, font: str) -> list[str]:
    filters = []
    box_w = int(OUT_W * (SAFE_RIGHT - SAFE_LEFT))
    for cap in plan.captions:
        text = _escape_drawtext(_wrap(cap.text))
        size = 60 if cap.kind in ("QUESTION", "HOOK") else 52
        colour = {"ANSWER": "0x2E7D32", "QUESTION": "white", "HOOK": "white"}.get(cap.kind, "white")
        y = int(OUT_H * SAFE_TOP) if cap.kind in ("HOOK", "QUESTION") else int(OUT_H * (SAFE_BOTTOM - 0.22))
        start, end = cap.start_ms / 1000, cap.end_ms / 1000
        filters.append(
            f"drawtext=fontfile='{font}':text='{text}':fontsize={size}:fontcolor={colour}:"
            f"line_spacing=8:box=1:boxcolor=black@0.55:boxborderw=24:"
            f"x=(w-text_w)/2:y={y}:enable='between(t,{start:.3f},{end:.3f})'"
        )
        _ = box_w
    return filters


def render_short(source: Path, info: MediaInfo, plan: ShortPlan, out: Path, encoder: str,
                 log: CommandLog, font: str | None = None) -> dict:
    font = font or find_font()
    vf = ",".join([crop_filter(info, plan.crop_region), *caption_filters(plan, font), "format=yuv420p"])
    start, end = plan.start_ms / 1000, plan.end_ms / 1000
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source)]
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{end - start:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if info.has_audio else "1:a:0",
            "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11", "-c:v", encoder]
    cmd += ["-preset", "veryfast", "-crf", "20"] if encoder == "libx264" else ["-b:v", "6M"]
    cmd += ["-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", "-shortest", str(out)]
    log.run(cmd)
    return {"sha256": sha256_of(out), "size_bytes": out.stat().st_size, "filter": vf}


def render_thumbnail(source: Path, at_ms: int, out: Path, log: CommandLog) -> dict:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-ss", f"{at_ms / 1000:.3f}",
           "-i", str(source), "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", str(out)]
    log.run(cmd)
    return {"sha256": sha256_of(out), "size_bytes": out.stat().st_size, "at_ms": at_ms}


def detect_black_bars(path: Path, log: CommandLog) -> dict:
    """Run cropdetect over the clip; if the detected crop is narrower than the frame,
    the clip carries bars."""
    proc = log.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", "cropdetect=24:2:0",
                    "-frames:v", "60", "-f", "null", "-"])
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)
    if not crops:
        return {"checked": False, "has_bars": False}
    w, h, _, _ = (int(v) for v in crops[-1])
    return {"checked": True, "has_bars": w < OUT_W * 0.98 or h < OUT_H * 0.98, "detected": f"{w}x{h}"}


def measure_loudness(path: Path, log: CommandLog) -> dict:
    proc = log.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "a:0", "-af",
                    "ebur128=peak=true", "-f", "null", "-"])
    integrated = re.findall(r"I:\s+(-?[0-9.]+) LUFS", proc.stderr)
    peak = re.findall(r"Peak:\s+(-?[0-9.]+) dBFS", proc.stderr)
    return {"integrated_lufs": float(integrated[-1]) if integrated else None,
            "true_peak_dbfs": float(peak[-1]) if peak else None}


def verify_short(path: Path, plan: ShortPlan, log: CommandLog, tolerance_ms: int = 250) -> dict:
    info = ffprobe(path)
    problems = []
    if (info.width, info.height) != (OUT_W, OUT_H):
        problems.append(f"dimensions {info.width}x{info.height} are not {OUT_W}x{OUT_H}")
    if info.video_codec != "h264":
        problems.append(f"video codec {info.video_codec} is not h264")
    if info.audio_codec != "aac":
        problems.append(f"audio codec {info.audio_codec or 'none'} is not aac")
    if abs(info.duration_ms - plan.duration_ms) > tolerance_ms:
        problems.append(f"duration {info.duration_ms} ms differs from planned {plan.duration_ms} ms")
    bars = detect_black_bars(path, log)
    if bars.get("has_bars"):
        problems.append(f"black bars detected ({bars.get('detected')})")
    loud = measure_loudness(path, log)
    if loud["true_peak_dbfs"] is not None and loud["true_peak_dbfs"] > -0.5:
        problems.append("true peak above -0.5 dBFS (clipping risk)")
    return {"media": info.as_dict(), "black_bars": bars, "loudness": loud, "problems": problems,
            "ok": not problems}
