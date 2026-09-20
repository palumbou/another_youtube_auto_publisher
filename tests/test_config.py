import pytest

from autopublisher.config import ConfigurationError, Settings, parse_utc


def test_local_mode_fails_closed_outside_local():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"ENVIRONMENT": "prod", "LOCAL_MODE": "1"})
    Settings.from_env({"ENVIRONMENT": "local", "LOCAL_MODE": "1"})


def test_real_youtube_mode_never_combines_with_local_mode():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"ENVIRONMENT": "local", "LOCAL_MODE": "1", "YOUTUBE_MODE": "real"})


def test_defaults_are_safe():
    s = Settings.from_env({})
    assert s.publish_kill_switch is True
    assert s.youtube_mode == "fake"
    assert s.api_project_audited is False
    assert s.cutover_at.year == 2099  # nothing is eligible until an owner sets the cutover


def test_cutover_must_be_rfc3339_with_zone():
    s = Settings.from_env({"CUTOVER_AT": "2026-09-20T00:00:00Z"})
    assert s.cutover_at.isoformat() == "2026-09-20T00:00:00+00:00"
    with pytest.raises(ConfigurationError):
        parse_utc("2026-09-20T00:00:00")
    with pytest.raises(ConfigurationError):
        Settings.from_env({"CUTOVER_AT": "yesterday"})
