import shutil

import pytest

from autopublisher.media import available_encoder, ffprobe
from autopublisher.render import (
    CommandLog,
    crop_filter,
    find_font,
    render_short,
    render_thumbnail,
    verify_short,
)
from autopublisher.shorts import Caption, ShortPlan
from tests import helpers

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def test_crop_filter_stays_inside_frame_and_is_9_16():
    info = ffprobe(helpers.TINY)
    assert crop_filter(info, {"x": 0.2, "y": 0, "width": 0.6, "height": 1}) == "crop=134:240:92:0,scale=1080:1920:flags=lanczos"
    assert crop_filter(info, None).startswith("crop=134:240:92:0")


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
