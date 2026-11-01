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
    parts = line.split()
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

    assert "cursor=10,20" in mock_log.debug.call_args[0][0]


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


def test_booleans_render_as_words(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", confirmed=True, hidden=False)

    line = mock_log.debug.call_args[0][0]
    assert "confirmed=true" in line
    assert "hidden=false" in line


def test_mappings_and_none_render_compactly(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", counts={"a": 1, "b": 2}, addr=None)

    line = mock_log.debug.call_args[0][0]
    assert "counts={a:1,b:2}" in line
    assert "addr=none" in line


def test_a_value_containing_spaces_is_quoted(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", error="socket went away")

    # Otherwise the pair would split into two fields.
    assert 'error="socket went away"' in mock_log.debug.call_args[0][0]


def test_the_filter_keeps_only_matching_events(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FILTER", "EDGE")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("EDGE_DISARM", reason="idle")
        debug.dbg("PAN_STOP")

    assert mock_log.debug.call_count == 1
    assert "EDGE_DISARM" in mock_log.debug.call_args[0][0]


def test_json_format_is_one_object_per_line(monkeypatch):
    import json as json_module

    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("EDGE_CONFIRMED", s=1, geo=(1, 2, 3, 4))

    payload = json_module.loads(mock_log.debug.call_args[0][0])
    assert payload["event"] == "EDGE_CONFIRMED"
    assert payload["fields"] == {"s": 1, "geo": [1, 2, 3, 4]}
    assert isinstance(payload["ts"], float)
    assert "\n" not in mock_log.debug.call_args[0][0]


def test_configure_formats_without_the_logger_name():
    """run() formats the root logger as "name: message"; a trace line has to
    stay a bare timestamp, which is the documented shape."""
    import logging as logging_module

    debug.configure()
    debug.configure()  # idempotent

    assert debug.log.propagate is False
    assert len(debug.log.handlers) == 1
    record = logging_module.LogRecord(
        "canvas.debug", logging_module.DEBUG, "", 0, "0.1 EVENT", None, None
    )
    assert debug.log.handlers[0].format(record) == "0.1 EVENT"


def test_reset_for_tests_undoes_configure():
    """Otherwise the handler outlives the test that added it, and a later
    caplog test would stop seeing this logger with no visible cause."""
    debug.configure()

    debug.reset_for_tests()

    assert debug._handler is None  # type: ignore[attr-defined]
    assert debug.log.propagate is True
    assert debug.log.handlers == []


def test_the_filter_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FILTER", "edge")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("EDGE_DISARM")
        debug.dbg("PAN_STOP")

    assert mock_log.debug.call_count == 1
    assert "EDGE_DISARM" in mock_log.debug.call_args[0][0]


def test_a_line_starts_with_the_timestamp(monkeypatch):
    """The documented shape is `<seconds> EVENT`, with nothing in front."""
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T")

    line = mock_log.debug.call_args[0][0]
    assert not line.startswith(" ")
    float(line.split()[0])


def test_non_ascii_in_a_field_is_not_escaped(monkeypatch):
    """A window title stays readable instead of turning into \\u escapes."""
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", title="Some page — here")

    assert "—" in mock_log.debug.call_args[0][0]


def test_json_mode_never_writes_non_json_numbers(monkeypatch):
    import json as json_module

    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", speed=float("nan"))

    # NaN would be invalid JSON and break jq on the line.
    payload = json_module.loads(mock_log.debug.call_args[0][0])
    assert payload["fields"]["speed"] == "nan"


def test_json_mode_formats_the_daemon_log_as_json(monkeypatch):
    """The trace is JSON in `_emit`; the root handler has to match, or a
    stream piped into jq breaks on the first warning."""
    import json as json_module
    import logging as logging_module

    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()

    record = logging_module.LogRecord(
        "canvas.navigation", logging_module.WARNING, "", 0, "spawn rules: %s", ("x",), None
    )
    payload = json_module.loads(debug.root_handler().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "canvas.navigation"
    assert payload["message"] == "spawn rules: x"
    assert isinstance(payload["ts"], float)


def test_text_mode_keeps_the_logger_name():
    import logging as logging_module

    record = logging_module.LogRecord(
        "canvas.navigation", logging_module.WARNING, "", 0, "spawn rules: %s", ("x",), None
    )

    assert debug.root_handler().format(record) == "canvas.navigation: spawn rules: x"


def test_json_mode_works_without_tracing(monkeypatch):
    """`CANVAS_DEBUG_FORMAT` asks for machine-readable output on its own; the
    daemon's own records are all it would contain."""
    import json as json_module
    import logging as logging_module

    monkeypatch.delenv("CANVAS_DEBUG", raising=False)
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()

    record = logging_module.LogRecord(
        "canvas", logging_module.INFO, "", 0, "loading config...", None, None
    )
    payload = json_module.loads(debug.root_handler().format(record))

    assert payload["message"] == "loading config..."
    assert payload["ts"] >= 0.0
    assert debug.enabled() is False


def test_an_unknown_object_renders_as_its_str(monkeypatch):
    monkeypatch.setenv("CANVAS_DEBUG", "1")
    debug.enable_from_env()
    marker = object()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", obj=marker)

    assert f"obj={marker}" in mock_log.debug.call_args[0][0]


def test_an_unknown_object_stays_a_string_in_json(monkeypatch):
    import json as json_module

    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()
    marker = object()

    with patch("canvas.debug.log") as mock_log:
        debug.dbg("T", obj=marker)

    payload = json_module.loads(mock_log.debug.call_args[0][0])
    assert payload["fields"]["obj"] == str(marker)


def test_json_mode_carries_an_exception_traceback(monkeypatch):
    """A logged crash must not become a bare line with the traceback dropped."""
    import json as json_module
    import logging as logging_module

    monkeypatch.setenv("CANVAS_DEBUG", "1")
    monkeypatch.setenv("CANVAS_DEBUG_FORMAT", "json")
    debug.enable_from_env()

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = logging_module.LogRecord(
        "canvas", logging_module.ERROR, "", 0, "failed", None, exc_info
    )
    payload = json_module.loads(debug.root_handler().format(record))

    assert "ValueError: boom" in payload["exc"]
