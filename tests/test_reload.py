"""Tests for hot config reload — `canvas-ctl reload` and the mapping it applies."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from canvas.config import ConfigError, load, resolve_path
from canvas.daemon import DaemonState
from canvas.navigation import Navigator
from canvas.panning import EdgeScrollState, PanningState

FULL_CONFIG = """
speed: 2.5
max_speed: 9.0
invert:
  enabled: false
edge_scroll:
  enabled: false
  ramp_distance: 77
  speed: 33.0
  max_speed: 44.0
  grab_dead_zone: 9
navigation:
  cooldown: 0.75
  protected_apps: ["Foo", "BAR"]
canvas:
  preserve_geometry: false
  auto_float: true
  spawn:
    center: false
    default: "800x600"
"""


@pytest.fixture(autouse=True)
def _isolate_state_file(tmp_path):
    """Keep tests off the real toggle state file.

    `Navigator.__init__` reads the persisted canvas state and
    `rehydrate_spawn_rules` writes it back, so without this a test run against
    a machine with a live daemon would load that daemon's workspaces and then
    persist them — corrupting real state from a unit test.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(run_dir)}):
        yield


def _write_config(root, body: str) -> str:
    """Drop a config where `load` looks for it, and return the path."""
    d = root / "canvas"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.yml"
    path.write_text(body)
    return str(path)


def _state() -> DaemonState:
    """A daemon state with real state objects and a stubbed navigator."""
    return DaemonState(
        panning=PanningState(),
        edge_scroll=EdgeScrollState(),
        navigator=Navigator(ipc=MagicMock()),
        ipc=MagicMock(),
    )


def _load_from(root, body: str) -> dict:
    """Write `body` as the user config, then load it as the daemon would."""
    _write_config(root, body)
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root)}):
        return load()


def test_apply_config_maps_every_key(tmp_path):
    """Every config key lands on the live object, with no key left behind."""
    ds = _state()

    ds.apply_config(_load_from(tmp_path, FULL_CONFIG))

    assert ds.panning.speed == 2.5
    assert ds.panning.max_speed == 9.0
    assert ds.panning.inverted is False
    assert ds.edge_scroll.enabled is False
    assert ds.edge_scroll.ramp_distance == 77
    assert ds.edge_scroll.speed == 33.0
    assert ds.edge_scroll.max_speed == 44.0
    assert ds.edge_scroll.grab_dead_zone == 9
    # protected_apps are lowercased at construction; a reload must not skip that.
    assert ds.navigator._protected_apps == ["foo", "bar"]
    assert ds.navigator._cooldown == 0.75
    assert ds.navigator._preserve_geometry is False
    assert ds.navigator._auto_float is True
    assert ds.navigator._spawn_cfg["default"] == "800x600"
    assert ds.navigator._spawn_cfg["center"] is False


def test_reload_changes_live_state_not_just_response(tmp_path):
    """A reload that answers OK must actually have moved the values.

    Asserting on the response alone would pass against an implementation that
    reported success and applied nothing.
    """
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()
    before = ds.panning.speed
    assert before != 2.5

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        response = ds.handle_ipc("RELOAD")

    assert response.startswith("OK")
    assert ds.panning.speed == 2.5
    assert ds.edge_scroll.ramp_distance == 77
    assert ds.navigator._auto_float is True


def test_reload_rejects_invalid_config_and_keeps_previous(tmp_path):
    """A broken config must not be applied, and must not disturb what is live."""
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        ds.handle_ipc("RELOAD")
    assert ds.panning.speed == 2.5

    _write_config(tmp_path, "speed: not-a-number\n")
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        response = ds.handle_ipc("RELOAD")

    assert response.startswith("ERROR:CONFIG_INVALID")
    # The daemon is still running on the last good config, not a half-applied one.
    assert ds.panning.speed == 2.5
    assert ds.navigator._auto_float is True


def test_reload_rejects_malformed_yaml_and_keeps_previous(tmp_path):
    """Broken YAML is reported, not raised — the client must get a reason."""
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        ds.handle_ipc("RELOAD")

    _write_config(tmp_path, "speed: [unclosed\n")
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        response = ds.handle_ipc("RELOAD")

    assert response.startswith("ERROR:CONFIG_INVALID")
    assert ds.panning.speed == 2.5


def test_reload_rearms_spawn_rules(tmp_path):
    """Spawn rules live in the compositor, so a reload must re-arm them."""
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()

    with (
        patch.object(Navigator, "rehydrate_spawn_rules") as rehydrate,
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
    ):
        ds.handle_ipc("RELOAD")

    rehydrate.assert_called_once()


def test_reload_rearms_rules_when_auto_float_turned_off(tmp_path):
    """Turning auto_float off still re-arms, which is what disables the rules.

    `rehydrate_spawn_rules` is the only thing that retracts rules from the
    compositor, so skipping it on a reload would strand them.
    """
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()
    with (
        patch.object(Navigator, "rehydrate_spawn_rules"),
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
    ):
        ds.handle_ipc("RELOAD")
    assert ds.navigator._auto_float is True

    _write_config(tmp_path, "canvas:\n  auto_float: false\n")
    with (
        patch.object(Navigator, "rehydrate_spawn_rules") as rehydrate,
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
    ):
        ds.handle_ipc("RELOAD")

    assert ds.navigator._auto_float is False
    assert rehydrate.call_count == 1


def test_reload_response_names_the_source_file(tmp_path):
    """The response names the file, so editing the wrong copy is visible."""
    path = _write_config(tmp_path, FULL_CONFIG)
    ds = _state()

    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        response = ds.handle_ipc("RELOAD")

    assert path in response


def test_reload_reads_the_file_it_reports(tmp_path):
    """The path in the response is the path that was read, not a later guess.

    `resolve_path` runs before `load`, and the resolved path is what gets read,
    so a file edited between the two cannot make the two disagree.
    """
    path = _write_config(tmp_path, FULL_CONFIG)
    ds = _state()

    with (
        patch("canvas.daemon.load", wraps=load) as spy,
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
    ):
        response = ds.handle_ipc("RELOAD")

    assert spy.call_args.args[0] == path
    assert path in response


def test_apply_config_rejects_a_partial_config():
    """The mapping requires a merged config, and says so by raising.

    Silently defaulting a missing `edge_scroll` block here would be a second
    copy of DEFAULT_CONFIG, and the copy that drifts is the one nobody edits.
    """
    ds = _state()
    with pytest.raises(KeyError):
        ds.apply_config({"speed": 2.0, "max_speed": None, "invert": {"enabled": False}})


def test_reload_survives_a_double_call(tmp_path):
    """Reloading twice is idempotent, not cumulative."""
    _write_config(tmp_path, FULL_CONFIG)
    ds = _state()
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        first = ds.handle_ipc("RELOAD")
        second = ds.handle_ipc("RELOAD")

    assert first.startswith("OK")
    assert second.startswith("OK")
    assert ds.panning.speed == 2.5
    assert ds.navigator._protected_apps == ["foo", "bar"]


def test_resolve_path_prefers_the_xdg_file(tmp_path):
    path = _write_config(tmp_path, FULL_CONFIG)
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
        assert resolve_path() == path


def test_resolve_path_is_none_without_any_file(tmp_path):
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "absent")}),
        patch("canvas.config._candidates", return_value=[]),
    ):
        assert resolve_path() is None


def test_malformed_yaml_names_the_file(tmp_path):
    path = _write_config(tmp_path, "speed: [unclosed\n")
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        pytest.raises(ConfigError) as exc,
    ):
        load()
    assert path in str(exc.value)


def test_validation_error_names_the_file(tmp_path):
    path = _write_config(tmp_path, "speed: not-a-number\n")
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        pytest.raises(ConfigError) as exc,
    ):
        load()
    assert path in str(exc.value)


def test_ctl_reload_sends_reload():
    from canvas.__main__ import ctl_main

    with (
        patch.object(sys, "argv", ["canvas-ctl", "reload"]),
        patch("canvas.ipc.send_command", return_value="OK: reloaded") as mock_send,
        patch("builtins.print"),
    ):
        ctl_main()
    mock_send.assert_called_with("RELOAD")
