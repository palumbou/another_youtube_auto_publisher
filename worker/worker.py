"""Fargate worker: heavy ffmpeg work, driven by environment variables.

Modes (env MODE):
  probe  — extract signals from the source video: metadata, scene changes, loudness,
           speech detection (VAD), timestamped frame mosaics for visual analysis,
           audio track for optional transcription. Writes work/<job>/probe.json.
  cut    — render each planned segment as a 9:16 Short (blurred background, full
           original frame centered, audio untouched). Writes work/<job>/cut_result.json.

Local development (no S3): worker.py <probe|cut> --local <video> --workdir <dir>
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Mosaic layout: 4x6 tiles of 480x270 -> 1920x1620 images the vision model reads well
TILE_W, TILE_H = 480, 270
GRID_COLS, GRID_ROWS = 4, 6
TILES_PER_MOSAIC = GRID_COLS * GRID_ROWS
MAX_MOSAICS = 18  # stay under the model's per-request image limit
SCENE_THRESHOLD = 0.30
SPEECH_RATIO_THRESHOLD = 0.05  # >5% voiced frames -> worth transcribing


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def ffprobe_info(video: Path) -> dict:
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", video],
        capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    video_stream = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio_streams = [s for s in data["streams"] if s["codec_type"] == "audio"]
    num, _, den = (video_stream.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if float(den or 1) else 0.0
    return {
        "duration": float(data["format"]["duration"]),
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": round(fps, 3),
        "has_audio": bool(audio_streams),
    }


def detect_scenes(video: Path, workdir: Path) -> list[float]:
    """Timestamps (s) where ffmpeg sees a hard visual change."""
    meta = workdir / "scenes.txt"
    run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", video,
         "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print:file={meta}",
         "-an", "-f", "null", "-"],
    )
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", meta.read_text())]
    return [round(t, 2) for t in times]


def loudness_curve(video: Path, workdir: Path) -> list[float]:
    """Per-second RMS level (dB); silence shows up as very negative values."""
    meta = workdir / "loudness.txt"
    run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-map", "a:0?",
         "-af", ("aresample=48000,asetnsamples=n=48000,astats=metadata=1:reset=1,"
                 f"ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file={meta}"),
         "-f", "null", "-"],
    )
    if not meta.exists():
        return []
    values = re.findall(r"RMS_level=(-?[0-9.]+|-inf)", meta.read_text())
    return [round(float(v), 1) if v != "-inf" else -99.0 for v in values]


def extract_audio(video: Path, workdir: Path) -> Path | None:
    """16 kHz mono FLAC: small, and what both the VAD and Transcribe want."""
    audio = workdir / "audio.flac"
    try:
        run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", video,
             "-map", "a:0", "-ac", "1", "-ar", "16000", audio])
    except subprocess.CalledProcessError:
        return None  # no audio track
    return audio


def speech_ratio(audio: Path, workdir: Path) -> float:
    """Fraction of 30 ms frames that WebRTC VAD marks as voiced."""
    import webrtcvad

    pcm = workdir / "audio.pcm"
    run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", audio,
         "-f", "s16le", "-ac", "1", "-ar", "16000", pcm])
    vad = webrtcvad.Vad(2)
    frame_bytes = int(16000 * 0.03) * 2
    data = pcm.read_bytes()
    frames = len(data) // frame_bytes
    if not frames:
        return 0.0
    voiced = sum(
        vad.is_speech(data[i * frame_bytes:(i + 1) * frame_bytes], 16000)
        for i in range(frames)
    )
    pcm.unlink()
    return round(voiced / frames, 3)


def build_mosaics(video: Path, workdir: Path, duration: float) -> tuple[list[Path], float]:
    """Sample frames uniformly, burn the timestamp into each, tile into mosaics."""
    max_tiles = MAX_MOSAICS * TILES_PER_MOSAIC
    interval = max(1.0, duration / max_tiles)
    out_pattern = workdir / "mosaic_%03d.jpg"
    drawtext = (
        "drawtext=text='%{pts\\:hms}':fontcolor=white:fontsize=28:box=1:"
        "boxcolor=black@0.6:boxborderw=6:x=8:y=h-th-8"
    )
    run(
        ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", video,
         "-vf", (f"fps=1/{interval:.4f},scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
                 f"pad={TILE_W}:{TILE_H}:(ow-iw)/2:(oh-ih)/2,{drawtext},"
                 f"tile={GRID_COLS}x{GRID_ROWS}"),
         "-q:v", "4", "-an", out_pattern],
    )
    return sorted(workdir.glob("mosaic_*.jpg")), interval


def render_short(video: Path, start: float, end: float, out: Path) -> None:
    """9:16 1080x1920: blurred zoomed copy as background, full original frame centered.

    Content is untouched (no editing): only the framing changes. Audio re-encoded to
    AAC as-is. -ss before -i for fast seek, then accurate trim on the output.
    """
    filter_complex = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=30[bgb];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2:format=auto[v]"
    )
    run(
        ["ffmpeg", "-hide_banner", "-nostats", "-y",
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video,
         "-filter_complex", filter_complex, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out],
    )


def downsample(values: list[float], max_points: int = 300) -> list[float]:
    if len(values) <= max_points:
        return values
    step = len(values) / max_points
    return [values[int(i * step)] for i in range(max_points)]


def probe(video: Path, workdir: Path) -> dict:
    info = ffprobe_info(video)
    scenes = detect_scenes(video, workdir)
    loudness = loudness_curve(video, workdir) if info["has_audio"] else []
    audio = extract_audio(video, workdir) if info["has_audio"] else None
    ratio = speech_ratio(audio, workdir) if audio else 0.0
    mosaics, interval = build_mosaics(video, workdir, info["duration"])
    return {
        **info,
        "scene_changes": scenes[:500],
        "loudness_db_per_s": downsample(loudness),
        "loudness_step_s": max(1, math.ceil(len(loudness) / 300)) if loudness else 1,
        "speech_ratio": ratio,
        "has_speech": ratio >= SPEECH_RATIO_THRESHOLD,
        "mosaic_files": [m.name for m in mosaics],
        "mosaic_interval_s": round(interval, 3),
        "mosaic_grid": f"{GRID_COLS}x{GRID_ROWS}",
        "audio_file": audio.name if audio else "",
    }


def cut(video: Path, plan: dict, workdir: Path) -> dict:
    results = []
    for short in plan["shorts"]:
        out = workdir / f"{short['short_id']}.mp4"
        render_short(video, float(short["start"]), float(short["end"]), out)
        results.append({"short_id": short["short_id"], "file": out.name})
    return {"shorts": results}


# ---------------------------------------------------------------------------
# S3-driven entrypoint (Fargate) and local CLI


def s3_main(mode: str) -> None:
    import boto3

    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    job_id = os.environ["JOB_ID"]
    video_key = os.environ["VIDEO_KEY"]
    prefix = f"work/{job_id}"

    with tempfile.TemporaryDirectory(dir="/work" if Path("/work").is_dir() else None) as tmp:
        workdir = Path(tmp)
        video = workdir / Path(video_key).name
        print(f"downloading s3://{bucket}/{video_key}", flush=True)
        s3.download_file(bucket, video_key, str(video))

        if mode == "probe":
            result = probe(video, workdir)
            for name in result["mosaic_files"]:
                s3.upload_file(str(workdir / name), bucket, f"{prefix}/mosaics/{name}")
            if result["audio_file"]:
                s3.upload_file(str(workdir / result["audio_file"]), bucket,
                               f"{prefix}/{result['audio_file']}")
            s3.put_object(Bucket=bucket, Key=f"{prefix}/probe.json",
                          Body=json.dumps(result).encode(), ContentType="application/json")
        elif mode == "cut":
            plan_key = os.environ.get("PLAN_KEY", f"{prefix}/plan.json")
            plan = json.loads(s3.get_object(Bucket=bucket, Key=plan_key)["Body"].read())
            result = cut(video, plan, workdir)
            for item in result["shorts"]:
                dest = f"ready/{job_id}/{item['file']}"
                s3.upload_file(str(workdir / item["file"]), bucket, dest,
                               ExtraArgs={"ContentType": "video/mp4"})
                item["s3_key"] = dest
            s3.put_object(Bucket=bucket, Key=f"{prefix}/cut_result.json",
                          Body=json.dumps(result).encode(), ContentType="application/json")
        else:
            raise SystemExit(f"unknown mode: {mode}")
    print("done", flush=True)


def local_main(mode: str, video: Path, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    if mode == "probe":
        result = probe(video, workdir)
        (workdir / "probe.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("scene_changes", "loudness_db_per_s")}, indent=2))
    elif mode == "cut":
        plan = json.loads((workdir / "plan.json").read_text())
        result = cut(video, plan, workdir)
        print(json.dumps(result, indent=2))
    else:
        raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else os.environ.get("MODE", "")
    if "--local" in args:
        video = Path(args[args.index("--local") + 1])
        workdir = Path(args[args.index("--workdir") + 1]) if "--workdir" in args \
            else video.parent / f"{video.stem}.work"
        local_main(mode, video, workdir)
    else:
        s3_main(mode)
