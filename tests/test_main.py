"""Tests for canvas.__main__ — CLI entry points."""

import contextlib
import sys
from unittest.mock import patch

import pytest

from canvas.__main__ import ctl_main, daemon_main


def test_ctl_main_no_args_exits():
    with patch.object(sys, "argv", ["canvas-ctl"]):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 2


def test_ctl_main_unknown_command_exits():
    with patch.object(sys, "argv", ["canvas-ctl", "foobar"]):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 2


def test_ctl_main_valid_command_sends():
    """canvas-ctl with valid command calls send_command and prints response."""
    with (
        patch.object(sys, "argv", ["canvas-ctl", "ping"]),
        patch("canvas.ipc.send_command", return_value="PONG") as mock_send,
        patch("builtins.print") as mock_print,
    ):
        ctl_main()
        mock_send.assert_called_with("PING")
        mock_print.assert_called_with("PONG")


def test_ctl_main_center_cursor_command():
    """canvas-ctl center-cursor normalizes to CENTER_CURSOR."""
    with (
        patch.object(sys, "argv", ["canvas-ctl", "center-cursor"]),
        patch("canvas.ipc.send_command", return_value="OK") as mock_send,
    ):
        ctl_main()
        mock_send.assert_called_with("CENTER_CURSOR")


def test_ctl_main_edge_start_command():
    """canvas-ctl edge-start normalizes to EDGE_START."""
    with (
        patch.object(sys, "argv", ["canvas-ctl", "edge-start"]),
        patch("canvas.ipc.send_command", return_value="EDGE_ON") as mock_send,
    ):
        ctl_main()
        mock_send.assert_called_with("EDGE_START")


def test_ctl_main_no_response_no_print():
    """canvas-ctl exits 1 when send_command returns empty string."""
    with (
        patch.object(sys, "argv", ["canvas-ctl", "ping"]),
        patch("canvas.ipc.send_command", return_value=""),
        patch("builtins.print") as mock_print,
    ):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 1
        mock_print.assert_called_once()


def test_ctl_main_error_response_exits():
    """canvas-ctl exits 1 when the daemon returns an ERROR response."""
    with (
        patch.object(sys, "argv", ["canvas-ctl", "ping"]),
        patch("canvas.ipc.send_command", return_value="ERROR: daemon not running"),
        patch("builtins.print"),
    ):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 1


def test_ctl_main_unknown_command_response_exits():
    """canvas-ctl exits 1 when the daemon does not know the command.

    That is what a daemon too old to have the command answers. Exiting 0 would
    tell a script the command had been accepted by a daemon that never ran it.
    """
    with (
        patch.object(sys, "argv", ["canvas-ctl", "reload"]),
        patch("canvas.ipc.send_command", return_value="UNKNOWN: RELOAD"),
        patch("builtins.print"),
    ):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 1


def test_daemon_main_calls_run():
    """daemon_main delegates to daemon.run()."""
    with patch("canvas.daemon.run") as mock_run:
        daemon_main([])
        mock_run.assert_called_once()


def test_ctl_main_help_lists_commands(capsys):
    """--help exits 0 and names every command (canonical list lives in code)."""
    with patch.object(sys, "argv", ["canvas-ctl", "--help"]):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0
    out = capsys.readouterr().out
    for name, _ in [
        ("ping", ""),
        ("status", ""),
        ("pan-start", ""),
        ("pan-stop", ""),
        ("nav-left", ""),
        ("nav-right", ""),
        ("nav-up", ""),
        ("nav-down", ""),
        ("center-cursor", ""),
        ("canvas-toggle", ""),
        ("canvas-toggle-all", ""),
        ("canvas-toggle-single", ""),
        ("toggle", ""),
        ("edge-start", ""),
        ("edge-stop", ""),
    ]:
        assert name in out


def test_ctl_main_version(capsys):
    """--version exits 0 and prints name + version from a single source."""
    from canvas import __version__

    with patch.object(sys, "argv", ["canvas-ctl", "--version"]):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0
    out = capsys.readouterr().out
    assert "canvas-ctl" in out and __version__ in out


def test_daemon_main_help_and_rejects_positional(capsys):
    """daemon --help exits 0; stray positionals exit 2 instead of starting."""
    with patch.object(sys, "argv", ["canvasd", "--help"]):
        try:
            daemon_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0
    with patch.object(sys, "argv", ["canvasd", "--help"]):
        try:
            daemon_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0
    with patch.object(sys, "argv", ["canvasd", "whatever"]):
        try:
            daemon_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 2
    with (
        patch("canvas.daemon.run") as mock_run,
        patch.object(sys, "argv", ["canvasd", "--version"]),
        contextlib.suppress(SystemExit),
    ):
        daemon_main()
        mock_run.assert_not_called()


def test_version_falls_back_to_the_package_attribute(capsys):
    """Without installed metadata there is nothing to read a version from, and
    --version would print nothing at all."""
    from importlib.metadata import PackageNotFoundError

    from canvas import __version__

    with (
        patch.object(sys, "argv", ["canvas-ctl", "--version"]),
        patch("importlib.metadata.version", side_effect=PackageNotFoundError),
    ):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0

    assert __version__ in capsys.readouterr().out


def test_daemon_main_reports_an_invalid_configuration(caplog):
    from canvas.config import ConfigError

    with patch("canvas.daemon.run", side_effect=ConfigError("speed must be a number")):
        try:
            daemon_main([])
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 1

    assert "speed must be a number" in caplog.text


def test_daemon_main_reports_an_unusable_runtime_directory(caplog):
    """Another local user owning /tmp/user/<uid> must stop the daemon, loudly."""
    with patch("canvas.daemon.run", side_effect=RuntimeError("owned by uid 0")):
        try:
            daemon_main([])
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 1

    assert "owned by uid 0" in caplog.text


def test_ctl_main_status_sends_the_plain_command():
    with (
        patch.object(sys, "argv", ["canvas-ctl", "status"]),
        patch("canvas.ipc.send_command", return_value="version 1.6.0") as mock_send,
    ):
        ctl_main()

    mock_send.assert_called_with("STATUS")


def test_ctl_main_status_json_asks_the_daemon_for_json():
    with (
        patch.object(sys, "argv", ["canvas-ctl", "status", "--json"]),
        patch("canvas.ipc.send_command", return_value="{}") as mock_send,
    ):
        ctl_main()

    mock_send.assert_called_with("STATUS_JSON")


def test_ctl_main_rejects_json_where_it_has_no_meaning():
    with patch.object(sys, "argv", ["canvas-ctl", "ping", "--json"]):
        try:
            ctl_main()
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 2


def test_ctl_main_canvas_reset_sends_the_command():
    with (
        patch.object(sys, "argv", ["canvas-ctl", "canvas-reset"]),
        patch("canvas.ipc.send_command", return_value="CANVAS_RESET") as mock_send,
    ):
        ctl_main()

    mock_send.assert_called_with("CANVAS_RESET")


@pytest.mark.parametrize(
    ("cmd", "sent"),
    [
        ("pan-left", "PAN_LEFT"),
        ("pan-right", "PAN_RIGHT"),
        ("pan-up", "PAN_UP"),
        ("pan-down", "PAN_DOWN"),
    ],
)
def test_ctl_main_pan_step_commands(cmd, sent):
    with (
        patch.object(sys, "argv", ["canvas-ctl", cmd]),
        patch("canvas.ipc.send_command", return_value="PAN_STEP") as mock_send,
    ):
        ctl_main()

    mock_send.assert_called_with(sent)
