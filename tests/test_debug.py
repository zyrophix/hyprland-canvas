"""Tests for canvas.debug — structured tracing helper."""

from unittest.mock import patch

import pytest

from canvas import debug


@pytest.fixture(autouse=True)
def _reset():
    debug.reset_for_tests()
    yield
    debug.reset_for_tests()


def test_disabled_by_default():
    assert debug.enabled() is False


def test_enable_from_env(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()
    assert debug.enabled() is True

    debug.reset_for_tests()
    monkeypatch.setenv("CANVAS_DEBUG", "0")
    debug.enable_from_env()
    assert debug.enabled() is False

    monkeypatch.setenv("CANVAS_DEBUG", "")
    debug.enable_from_env()
    assert debug.enabled() is False


def test_dbg_noop_when_disabled():
    with patch("canvas.debug.log") as mock_log:
        debug.dbg("EVENT", k=1)
        mock_log.debug.assert_not_called()


def test_dbg_formats_fields_when_enabled(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("EDGE_DISARM", s=3, reason="cursor_left")

    line = mock_log.debug.call_args[0][0]
    parts = line.split(" ")
    assert parts[1] == "EDGE_DISARM"
    assert "s=3" in parts
    assert "reason=cursor_left" in parts
    # leading field is a float timestamp (seconds since enable)
    float(parts[0])


def test_dbg_tuple_field_rendering(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", cursor=(10, 20))

    assert "cursor=(10, 20)" in mock_log.debug.call_args[0][0]


def test_level_two_is_verbose(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "2")
    debug.reset_for_tests()

    debug.enable_from_env()

    assert debug.level() == 2
    debug.reset_for_tests()


def test_dbg2_emits_only_at_the_verbose_level(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "2")
    debug.reset_for_tests()
    debug.enable_from_env()

    with patch("canvas.debug.log") as log:
        debug.dbg2("EVENT", a=1)

    assert log.debug.call_count == 1
    debug.reset_for_tests()
