import shutil
from pathlib import Path

import pytest

from autopublisher.media import MediaInfo, available_encoder, ffprobe
from autopublisher.render import (
    OUT_H,
    CommandLog,
    caption_plan,
    crop_filter,
    find_font,
    layout,
    render_short,
    render_thumbnail,
    verify_short,
)
from autopublisher.shorts import SAFE_TOP, Caption, ShortPlan
from tests import helpers

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def test_layout_crops_exactly_the_declared_region_and_never_zooms_beyond_it():
    master = MediaInfo(duration_ms=1000, width=1920, height=1080, frame_rate=30, video_codec="h264",
                       audio_codec="aac", has_audio=True, rotation=0, bit_rate=0, size_bytes=0)
    # The central 1080 px of a 1920x1080 master: pixel-exact, scale 1.0, centred vertically.
    lay = layout(master, {"x": 0.21875, "y": 0, "width": 0.5625, "height": 1})
    assert (lay.crop_w, lay.crop_h, lay.crop_x, lay.crop_y) == (1080, 1080, 420, 0)
    assert (lay.fg_w, lay.fg_h, lay.fg_x, lay.fg_y, lay.scale) == (1080, 1080, 0, 420, 1.0)
    assert lay.map_x(700) == 280 and lay.map_y(0) == 420
    assert "crop=1080:1080:420:0" in crop_filter(master, {"x": 0.21875, "y": 0, "width": 0.5625, "height": 1})
    # A region narrower than 9:16 is scaled up to fit the canvas, never cropped tighter.
    lay = layout(master, {"x": 0.34, "y": 0, "width": 0.316, "height": 1})
    assert (lay.crop_w, lay.crop_h, lay.crop_x) == (606, 1080, 652)
    assert (lay.fg_w, lay.fg_h) == (1076, 1920) and abs(lay.scale - 1.776) < 0.01
    # No region: the whole frame, letterboxed on a blurred copy of itself.
    lay = layout(master, None)
    assert (lay.crop_w, lay.crop_h, lay.fg_w, lay.fg_h, lay.fg_y) == (1920, 1080, 1080, 606, 657)
    tiny = ffprobe(helpers.TINY)
    assert crop_filter(tiny, {"x": 0.2, "y": 0, "width": 0.6, "height": 1}).startswith("[0:v]crop=192:240:64:0,")


def _synthetic_master(path: Path, marker_x: int = 700, marker_w: int = 20) -> None:
    """1920x1080 grey master with one white vertical marker at a known column."""
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=0x606060:s=1920x1080:r=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "2", "-vf", f"drawbox=x={marker_x}:y=0:w={marker_w}:h=1080:color=white:t=fill",
                    "-c:v", available_encoder(), "-b:v", "4M", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def _row(path: Path, y: int, frame: int = 15) -> bytes:
    import subprocess
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf",
                          f"select='eq(n,{frame})',format=gray,crop=1080:2:0:{y}", "-frames:v", "1",
                          "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
    assert len(out) == 2160
    return out[:1080]


def _bright_columns(row: bytes, threshold: int = 200) -> list[int]:
    return [i for i, v in enumerate(row) if v >= threshold]


@pytest.mark.parametrize("region,marker_x,expected", [
    ({"x": 0.21875, "y": 0, "width": 0.5625, "height": 1}, 700, (280, 300)),   # 1:1 central crop
    ({"x": 0.34, "y": 0, "width": 0.316, "height": 1}, 700, (85, 121)),        # scaled-up narrow region
])
def test_marker_lands_where_the_layout_says(tmp_path, region, marker_x, expected):
    master = tmp_path / "master.mp4"
    _synthetic_master(master, marker_x)
    info = ffprobe(master)
    plan = ShortPlan("s", 0, 1900, crop_region=region)
    out = tmp_path / "short.mp4"
    result = render_short(master, info, plan, out, available_encoder(), CommandLog(), find_font())
    lay = layout(info, region)
    assert result["layout"]["scale"] == lay.scale
    row = _row(out, lay.fg_y + lay.fg_h // 2)
    cols = _bright_columns(row)
    assert cols, "marker not found in the picture row"
    lo, hi = expected
    assert lo - 4 <= min(cols) <= lo + 4 and hi - 4 <= max(cols) <= hi + 4, (min(cols), max(cols))
    # Outside the picture the blurred background is present, not black, and carries no hard marker.
    if lay.fg_y > 0:
        top = _row(out, lay.fg_y // 2)
        assert max(top) < 200 and sum(top) / len(top) > 20
    # A tighter crop would have zoomed the marker: its width must equal the source width times the scale.
    assert abs((max(cols) - min(cols) + 1) - 20 * lay.scale) <= 6


def test_captions_never_cover_master_text_and_decisions_are_logged():
    master = MediaInfo(duration_ms=1000, width=1920, height=1080, frame_rate=30, video_codec="h264",
                       audio_codec="aac", has_audio=True, rotation=0, bit_rate=0, size_bytes=0)
    central = {"x": 0.21875, "y": 0, "width": 0.5625, "height": 1}
    plan = ShortPlan("s", 0, 5000, crop_region=central, captions=[
        Caption(0, 1000, "Quale Grande Slam si disputa a Parigi?", "QUESTION", source_has_text=True),
        Caption(1000, 2000, "3, 2, 1", "TEXT", source_has_text=False)])
    decisions = caption_plan(plan, layout(master, central))
    assert decisions[0]["burn"] is False and "already shows" in decisions[0]["reason"]
    assert decisions[1]["burn"] is True and decisions[1]["where"] == "over-picture"
    # A short picture leaves a free band inside the safe zone: captions go there, even for master text.
    lay = layout(master, {"x": 0.21875, "y": 0.25, "width": 0.5625, "height": 0.5})
    assert lay.fg_h == 540
    decisions = caption_plan(plan, lay)
    assert all(d["burn"] for d in decisions) and decisions[0]["where"] == "above-picture"
    assert int(OUT_H * SAFE_TOP) <= decisions[0]["y"] < lay.fg_y


def test_render_short_from_fixture(tmp_path):
    info = ffprobe(helpers.TINY)
    plan = ShortPlan("short-01", 0, 1900, crop_region={"x": 0.2, "y": 0, "width": 0.6, "height": 1},
                     captions=[Caption(400, 900, "Qual è il fiume più lungo d'Italia?", "QUESTION"),
                               Caption(1200, 1500, "Il Po", "ANSWER")])
    log = CommandLog()
    out = tmp_path / "short-01.mp4"
    result = render_short(helpers.TINY, info, plan, out, available_encoder(), log, find_font())
    assert len(result["sha256"]) == 64
    report = verify_short(out, plan, log)
    assert report["ok"], report["problems"]
    assert report["media"]["width"] == 1080 and report["media"]["height"] == 1920
    assert report["media"]["video_codec"] == "h264" and report["media"]["audio_codec"] == "aac"
    assert report["black_bars"]["has_bars"] is False
    assert all(e["ok"] for e in log.entries)
    thumb = render_thumbnail(helpers.TINY, 700, tmp_path / "t.jpg", log)
    assert thumb["size_bytes"] > 0
