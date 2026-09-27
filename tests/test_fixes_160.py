"""Tests for the 1.6.0 behaviour fixes.

Each test here would pass against the pre-1.6.0 implementation if the fix it
covers were reverted, except where noted.
"""

import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from canvas.config import validate
from canvas.daemon import DaemonState
from canvas.ipc import IpcServer, _daemon_alive
from canvas.navigation import Navigator
from canvas.panning import EdgeScrollState, PanningState
from canvas.toggle_state import FORMAT_VERSION, load


def _state(ipc: MagicMock | None = None) -> DaemonState:
    panning = PanningState(speed=1.0)
    edge_scroll = EdgeScrollState(ramp_distance=50, speed=20.0, enabled=True)
    navigator = Navigator(ipc=ipc or MagicMock(), cooldown=0.0)
    return DaemonState(
        panning=panning, edge_scroll=edge_scroll, navigator=navigator, ipc=ipc or MagicMock()
    )


def _client(addr: str, cls: str, **over: object) -> dict:
    w = {
        "address": addr,
        "class": cls,
        "floating": True,
        "at": [100, 100],
        "size": [400, 300],
        "workspace": {"id": 1},
    }
    w.update(over)
    return w


def _pan_state(ipc: MagicMock, clients: list[dict]) -> DaemonState:
    ds = _state(ipc)
    ipc.send.side_effect = [json.dumps({"id": 1}), json.dumps(clients)]
    ds.fetch_baselines()
    return ds


# --- fullscreen windows must not ride the camera ---------------------------


def test_fullscreen_floating_window_is_not_panned():
    """A fullscreen window owns the output; panning it drags the whole view."""
    ipc = MagicMock()
    ds = _pan_state(
        ipc,
        [
            _client("0x1", "kitty"),
            _client("0x2", "zoom", fullscreen=True),
        ],
    )
    assert "0x1" in ds.baselines
    assert "0x2" not in ds.baselines


def test_fullscreen_window_also_skips_the_restore():
    """Excluded at snapshot time, so the restore pass never saw it either."""
    ipc = MagicMock()
    ds = _pan_state(ipc, [_client("0x2", "zoom", fullscreen=True)])
    assert ds.baselines == {}
    # restore_baselines returns immediately with nothing to restore
    ds.restore_baselines()
    ipc.eval_lua.assert_not_called()


# --- window_pan_excludes ----------------------------------------------------


def test_window_pan_excludes_keeps_class_out_of_baselines():
    ipc = MagicMock()
    ds = _state(ipc)
    ds._pan_exclude_apps = ["picture-in-picture"]
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        json.dumps(
            [
                _client("0x1", "kitty"),
                _client("0x2", "picture-in-picture"),
                _client("0x3", "Firefox"),
            ]
        ),
    ]
    ds.fetch_baselines()
    # Matching is case-insensitive substring, so only 0x2 is dropped.
    assert set(ds.baselines) == {"0x1", "0x3"}


def test_window_pan_excludes_defaults_to_empty_nothing_is_excluded():
    """Opt-in: with the default empty list the fullscreen rule is the only one."""
    ipc = MagicMock()
    ds = _pan_state(ipc, [_client("0x1", "kitty"), _client("0x2", "zoom")])
    assert set(ds.baselines) == {"0x1", "0x2"}


def test_edge_scroll_lua_skips_excluded_addresses():
    """The exclusion has to reach the Lua, not just the snapshot."""
    ipc = MagicMock()
    ds = _state(ipc)
    ds.edge_scroll_workspace = 1
    ds.edge_scroll_excluded = {"0x2"}
    ds.edge_scroll._dragged_addr = "0x3"

    ds.edge_scroll_move(10, 20)

    lua = ipc.eval_lua.call_args[0][0]
    skip_table = lua.split("local ws =")[0]
    assert '["0x2"] = true' in skip_table
    assert '["0x3"] = true' in skip_table  # the dragged window
    assert "0x1" not in skip_table  # movable, must not be skipped


# --- validation -------------------------------------------------------------


def test_protected_apps_rejects_empty_string():
    cfg = {
        "navigation": {"cooldown": 0.2, "protected_apps": ["", "mpv"]},
        "invert": {"enabled": True},
    }
    errs = validate(cfg)
    assert any("non-empty" in e for e in errs), errs


def test_protected_apps_accepts_normal_list():
    cfg = {
        "navigation": {"cooldown": 0.2, "protected_apps": ["firefox", "mpv"]},
        "invert": {"enabled": True},
    }
    assert not [e for e in validate(cfg) if "protected_apps" in e]


def test_window_pan_excludes_rejects_empty_string():
    cfg = {
        "navigation": {"cooldown": 0.2, "protected_apps": []},
        "invert": {"enabled": True},
        "window_pan_excludes": [""],
    }
    errs = validate(cfg)
    assert any("window_pan_excludes" in e and "non-empty" in e for e in errs), errs


def test_window_pan_excludes_rejects_wrong_type():
    cfg = {
        "navigation": {"cooldown": 0.2, "protected_apps": []},
        "invert": {"enabled": True},
        "window_pan_excludes": "zoom",
    }
    errs = validate(cfg)
    assert any("window_pan_excludes" in e for e in errs), errs


# --- navigation targets the window's own monitor ----------------------------


def test_navigate_centres_on_the_monitors_the_target_lives_on():
    """Resolving the centre with no coordinates falls back to the focused
    monitor, which would drag a target from a second monitor onto the first."""
    nav = Navigator(ipc=MagicMock(), cooldown=0.0)
    left = _client("0x1", "kitty", at=[100, 100])
    target = _client("0x2", "kitty", at=[3000, 500])
    windows = [left, target]

    monitors = json.dumps(
        [
            {"id": 0, "focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"id": 1, "focused": False, "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ]
    )
    nav._ipc = MagicMock()
    nav._ipc.send.return_value = monitors

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_pan_to_window", return_value=True) as mock_pan,
    ):
        assert nav.navigate("right") is True

    # 1920 + 1920//2 = 2880 — the second monitor's centre, not the focused one
    assert mock_pan.call_args[0][2] == 2880, mock_pan.call_args


def test_navigate_falls_back_to_focused_monitor_when_target_is_off_all_outputs():
    nav = Navigator(ipc=MagicMock(), cooldown=0.0)
    left = _client("0x1", "kitty", at=[100, 100])
    target = _client("0x2", "kitty", at=[9000, 9000])
    windows = [left, target]

    nav._ipc = MagicMock()
    nav._ipc.send.return_value = json.dumps(
        [{"id": 0, "focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080}]
    )

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_pan_to_window", return_value=True) as mock_pan,
    ):
        assert nav.navigate("right") is True

    assert mock_pan.call_args[0][2] == 960, mock_pan.call_args


# --- state file from a newer version ----------------------------------------


def test_newer_state_format_warns_instead_of_failing_silently(tmp_path, caplog):
    """A v5 file is readable as v4, and the next save would overwrite it.
    The warning is the only signal the user gets before losing it."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"_v": FORMAT_VERSION + 1, "1": {"active": True, "tiled": {}}}))
    with caplog.at_level(logging.WARNING, logger="canvas.toggle"):
        load(str(path))
    assert "newer" in caplog.text
    assert str(FORMAT_VERSION) in caplog.text


def test_current_state_format_does_not_warn(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"_v": FORMAT_VERSION, "1": {"active": True, "tiled": {}}}))
    with caplog.at_level(logging.WARNING, logger="canvas.toggle"):
        load(str(path))
    assert "newer" not in caplog.text


# --- IPC thread death -------------------------------------------------------


def test_serve_returns_quietly_on_symlink_without_binding(tmp_path):
    """The trigger: serve() returns instead of raising, so a dead thread
    produces no traceback and the daemon keeps running with no socket."""
    sock = tmp_path / "canvas.sock"
    sock.symlink_to(tmp_path / "nonexistent")
    server = IpcServer(sock_path=str(sock), handler=lambda c: "PONG")

    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert sock.is_symlink(), "the guard must refuse, not clobber the symlink"
    assert not _daemon_alive(str(sock))


@pytest.mark.parametrize("name", ["canvas.sock"])
def test_dead_ipc_thread_would_hold_the_singleton_lock(tmp_path, monkeypatch, name):
    """Documents the wedge the liveness check in run() exists to prevent:
    the lock is taken before the thread starts, so a dead server thread leaves
    a process holding a lock no second instance can take."""
    from canvas.ipc import acquire_singleton

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock = tmp_path / name
    sock.symlink_to(tmp_path / "nonexistent")

    acquire_singleton(str(sock))
    server = IpcServer(sock_path=str(sock), handler=lambda c: "PONG")
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()

    with pytest.raises(SystemExit):
        acquire_singleton(str(sock))


# --- spawn rules must not outlive the daemon ---------------------------------
#
# The compositor keeps windowrules in a global engine that nothing about our
# process lifetime touches. A rule left registered keeps matching the whole
# workspace, so every window opened there afterwards still arrives floating,
# sized to canvas.spawn.default and centred, with no daemon left to turn it off.


def test_drop_spawn_rules_disables_every_registered_name():
    ipc = MagicMock()
    navigator = Navigator(ipc=ipc, cooldown=0.0)
    navigator._spawn_rules = {4: ["canvas-spawn-ws4-default"], 5: ["canvas-spawn-ws5-default"]}

    with patch("canvas.navigation.spawnrules.disable") as disable:
        names = navigator.drop_spawn_rules()

    assert sorted(names) == ["canvas-spawn-ws4-default", "canvas-spawn-ws5-default"]
    disable.assert_called_once()
    assert sorted(disable.call_args[0][0]) == names
    assert navigator._spawn_rules == {}


def test_drop_spawn_rules_with_nothing_registered_does_nothing():
    navigator = Navigator(ipc=MagicMock(), cooldown=0.0)

    with patch("canvas.navigation.spawnrules.disable") as disable:
        assert navigator.drop_spawn_rules() == []

    disable.assert_not_called()


def test_drop_spawn_rules_leaves_the_state_file_names_for_the_next_start():
    """A crash must stay recoverable.

    drop_spawn_rules runs in a finally block, so a daemon killed before it
    reaches there leaves the names registered. The state file is the only record
    of them, and rehydrate_spawn_rules reads it to disable the leftovers — so
    the names must stay written even though the rules are already down.
    """
    ipc = MagicMock()
    navigator = Navigator(ipc=ipc, cooldown=0.0)
    navigator._spawn_rules = {4: ["canvas-spawn-ws4-default"]}

    with (
        patch("canvas.navigation.spawnrules.disable"),
        patch("canvas.navigation.toggle_state.save") as save,
    ):
        navigator.drop_spawn_rules()

    save.assert_not_called()


def test_run_drops_spawn_rules_on_the_way_out():
    from canvas import daemon

    navigator = MagicMock()
    stop = threading.Event()
    stop.set()

    with (
        patch.object(daemon, "load", return_value=MagicMock()),
        patch.object(daemon, "Navigator", return_value=navigator),
        patch.object(daemon, "HyprIPC") as ipc_cls,
        patch.object(daemon, "IpcServer"),
        patch.object(daemon, "acquire_singleton"),
        patch.object(daemon.threading, "Event", return_value=stop),
        patch.object(daemon.threading, "Thread"),
        patch.object(daemon, "signal"),
    ):
        ipc_cls.from_env.return_value = MagicMock()
        daemon.run()

    navigator.drop_spawn_rules.assert_called_once()
