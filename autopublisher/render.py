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
from autopublisher.shorts import SAFE_BOTTOM, SAFE_TOP, ShortPlan

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


@dataclass(frozen=True)
class Layout:
    """Pixel-exact geometry of one Short: the crop taken from the source, the size the
    crop is drawn at inside the 1080x1920 canvas, and its offset."""

    crop_w: int
    crop_h: int
    crop_x: int
    crop_y: int
    fg_w: int
    fg_h: int
    fg_x: int
    fg_y: int
    scale: float

    def map_x(self, source_x: float) -> float:
        """Where a source column lands in the Short (for tests and reports)."""
        return (source_x - self.crop_x) * self.scale + self.fg_x

    def map_y(self, source_y: float) -> float:
        return (source_y - self.crop_y) * self.scale + self.fg_y

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def layout(info: MediaInfo, crop_region: dict | None) -> Layout:
    """Crop exactly the declared region (never tighter), fit it into the canvas without
    exceeding it, centre it, and let a blurred copy fill the rest so no bar is black.

    Rounding: x and y are floored, width and height are floored to even pixels
    (a libx264/yuv420p requirement); the drawn size is floored to even pixels too.
    A 1080x1080 region on a 1920x1080 master therefore maps 1:1 (scale 1.0)."""
    sw, sh = info.width, info.height
    if crop_region:
        x = int(crop_region["x"] * sw)
        y = int(crop_region["y"] * sh)
        w = int(crop_region["width"] * sw) // 2 * 2
        h = int(crop_region["height"] * sh) // 2 * 2
        w, h = max(2, min(w, sw - x)), max(2, min(h, sh - y))
    else:
        x, y, w, h = 0, 0, sw // 2 * 2, sh // 2 * 2
    scale = min(OUT_W / w, OUT_H / h)
    fg_w, fg_h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    fg_x, fg_y = (OUT_W - fg_w) // 2, (OUT_H - fg_h) // 2
    return Layout(w, h, x, y, fg_w, fg_h, fg_x, fg_y, fg_w / w)


def crop_filter(info: MediaInfo, crop_region: dict | None) -> str:
    """filter_complex producing [v]: exact crop, blurred cover background, centred fit."""
    lay = layout(info, crop_region)
    return (f"[0:v]crop={lay.crop_w}:{lay.crop_h}:{lay.crop_x}:{lay.crop_y},split=2[bg][fg];"
            f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            f"gblur=sigma=40,eq=brightness=-0.08[bgb];"
            f"[fg]scale={lay.fg_w}:{lay.fg_h}:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay={lay.fg_x}:{lay.fg_y}:format=auto[v]")


MIN_CAPTION_BAND = 160  # px of free canvas needed to burn a caption outside the picture


def caption_plan(plan: ShortPlan, lay: Layout) -> list[dict]:
    """Decide, per caption, whether and where to burn it. Captions must stay inside the
    Shorts-safe band and must not cover text the master already shows (the segment
    declared a text_safe_area / region_of_interest for it)."""
    safe_top, safe_bottom = int(OUT_H * SAFE_TOP), int(OUT_H * SAFE_BOTTOM)
    fg_top, fg_bottom = lay.fg_y, lay.fg_y + lay.fg_h
    free_top = max(0, min(fg_top, safe_bottom) - safe_top)          # rows [safe_top, fg_top)
    free_bottom = max(0, safe_bottom - max(fg_bottom, safe_top))     # rows (fg_bottom, safe_bottom]
    decisions = []
    for cap in plan.captions:
        if free_bottom >= MIN_CAPTION_BAND:
            y, where = safe_bottom - MIN_CAPTION_BAND + 16, "below-picture"
        elif free_top >= MIN_CAPTION_BAND:
            y, where = safe_top + 16, "above-picture"
        elif cap.source_has_text:
            decisions.append({"caption": cap.text[:80], "kind": cap.kind, "burn": False, "reason":
                              "master already shows this text in the cut region and no free band exists"})
            continue
        else:
            y, where = safe_bottom - 220, "over-picture"
        decisions.append({"caption": cap.text[:80], "kind": cap.kind, "burn": True, "y": y, "where": where})
    return decisions


def caption_filters(plan: ShortPlan, font: str, lay: Layout | None = None) -> tuple[list[str], list[dict]]:
    lay = lay or Layout(0, 0, 0, 0, OUT_W, OUT_H, 0, 0, 1.0)
    decisions = caption_plan(plan, lay)
    filters = []
    for cap in plan.captions:
        decision = next((d for d in decisions if d["caption"] == cap.text[:80] and d["kind"] == cap.kind), None)
        if not decision or not decision["burn"]:
            continue
        text = _escape_drawtext(_wrap(cap.text))
        size = 60 if cap.kind in ("QUESTION", "HOOK") else 52
        colour = {"ANSWER": "0x2E7D32", "QUESTION": "white", "HOOK": "white"}.get(cap.kind, "white")
        start, end = cap.start_ms / 1000, cap.end_ms / 1000
        filters.append(
            f"drawtext=fontfile='{font}':text='{text}':fontsize={size}:fontcolor={colour}:"
            f"line_spacing=8:box=1:boxcolor=black@0.55:boxborderw=24:"
            f"x=(w-text_w)/2:y={decision['y']}:enable='between(t,{start:.3f},{end:.3f})'"
        )
    return filters, decisions


def render_short(source: Path, info: MediaInfo, plan: ShortPlan, out: Path, encoder: str,
                 log: CommandLog, font: str | None = None) -> dict:
    font = font or find_font()
    lay = layout(info, plan.crop_region)
    text_filters, decisions = caption_filters(plan, font, lay)
    graph = crop_filter(info, plan.crop_region)
    tail = ",".join([*text_filters, "format=yuv420p"])
    graph = graph.replace("[v]", "[v0]") + f";[v0]{tail}[v]"
    start, end = plan.start_ms / 1000, plan.end_ms / 1000
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source)]
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{end - start:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0" if info.has_audio else "1:a:0",
            "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11", "-c:v", encoder]
    cmd += ["-preset", "veryfast", "-crf", "20"] if encoder == "libx264" else ["-b:v", "6M"]
    cmd += ["-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", "-shortest", str(out)]
    log.run(cmd)
    return {"sha256": sha256_of(out), "size_bytes": out.stat().st_size, "filter": graph,
            "layout": lay.as_dict(), "captions": decisions}


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
