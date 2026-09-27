"""Additional tests for canvas.navigation — covering IPC-dependent methods."""

import json
from unittest.mock import MagicMock, patch

from canvas.navigation import Navigator, _safe_int
from canvas.toggle_state import ToggleStateError


def _make_nav(ipc: MagicMock | None = None) -> Navigator:
    if ipc is None:
        ipc = MagicMock()
    return Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)


def _make_window(
    class_name: str,
    address: str,
    at_x: int,
    at_y: int,
    size_w: int,
    size_h: int,
    floating: bool = True,
    workspace_id: int = 1,
) -> dict:
    return {
        "class": class_name,
        "address": address,
        "at": [at_x, at_y],
        "size": [size_w, size_h],
        "floating": floating,
        "workspace": {"id": workspace_id},
    }


def test_get_active_workspace_id():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps({"id": 3})
    nav = _make_nav(ipc)
    assert nav._get_active_workspace_id() == 3


def test_get_active_workspace_id_error():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    assert nav._get_active_workspace_id() is None


def test_get_floating_windows():
    ipc = MagicMock()
    w1 = _make_window("a", "0x1", 10, 20, 400, 300, floating=True, workspace_id=1)
    w2 = _make_window("b", "0x2", 50, 60, 400, 300, floating=False, workspace_id=1)
    w3 = _make_window("c", "0x3", 90, 100, 400, 300, floating=True, workspace_id=2)
    ipc.send.return_value = json.dumps([w1, w2, w3])
    nav = _make_nav(ipc)
    result = nav._get_floating_windows(1)
    assert len(result) == 1
    assert result[0]["address"] == "0x1"


def test_get_floating_windows_error():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    assert nav._get_floating_windows(1) is None


def test_get_focused_window():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps({"class": "kitty", "address": "0x1"})
    nav = _make_nav(ipc)
    result = nav._get_focused_window()
    assert result["class"] == "kitty"


def test_get_focused_window_error():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    assert nav._get_focused_window() is None


def test_get_monitor_center_focused():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [
            {"focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"focused": False, "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ]
    )
    nav = _make_nav(ipc)
    cx, cy = nav._get_monitor_center()
    assert cx == 960
    assert cy == 540


def test_get_monitor_center_fallback_first():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [
            {"focused": False, "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ]
    )
    nav = _make_nav(ipc)
    cx, cy = nav._get_monitor_center()
    assert cx == 2880


def test_get_monitor_center_uses_point_under_cursor():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [
            {"focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"focused": False, "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ]
    )
    nav = _make_nav(ipc)

    assert nav._get_monitor_center(2500, 100) == (2880, 540)


def test_get_monitor_center_rejects_point_outside_all_monitors():
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [{"focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080}]
    )
    nav = _make_nav(ipc)

    assert nav._get_monitor_center(5000, 5000) is None


def test_get_monitor_center_error():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    assert nav._get_monitor_center() is None


def test_pan_to_window():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)
    windows = [_make_window("kitty", "0x1", 100, 200, 400, 300)]
    nav._pan_to_window(windows, "0x1", 960, 540)
    ipc.eval_lua.assert_called_once()
    lua = ipc.eval_lua.call_args[0][0]
    assert "0x1" in lua
    assert "relative = true" in lua
    assert "_canvas_dispatch" in lua


def test_center_window_resolves_live_target_in_single_lua_eval():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)

    with patch.object(nav, "_get_monitor_center", return_value=(960, 540)):
        assert nav.center_window("0x1", workspace_id=1, cursor_x=300, cursor_y=350) is True

    lua = ipc.eval_lua.call_args.args[0]
    assert 'tostring(w.address) == "0x1"' in lua
    assert 'error("center target no longer exists")' in lua
    assert "target.at.x + math.floor(target.size.x / 2)" in lua
    assert "dsp.focus" not in lua


def test_center_window_fails_without_monitor():
    nav = _make_nav(MagicMock())

    with patch.object(nav, "_get_monitor_center", return_value=None):
        assert nav.center_window("0x1", workspace_id=1, cursor_x=300, cursor_y=350) is False


def test_center_window_reports_disappeared_target():
    ipc = MagicMock()
    ipc.eval_lua.side_effect = RuntimeError("error: center target no longer exists")
    nav = _make_nav(ipc)

    with patch.object(nav, "_get_monitor_center", return_value=(960, 540)):
        assert nav.center_window("0x1", workspace_id=1, cursor_x=300, cursor_y=350) is False


def test_pan_to_window_target_not_found():
    ipc = MagicMock()
    nav = _make_nav(ipc)
    windows = [_make_window("kitty", "0x1", 100, 200, 400, 300)]
    nav._pan_to_window(windows, "0x999", 960, 540)
    ipc.eval_lua.assert_not_called()


def test_pan_to_window_reports_lua_failure():
    ipc = MagicMock()
    ipc.eval_lua.side_effect = RuntimeError("error: forced Lua failure")
    nav = _make_nav(ipc)
    windows = [_make_window("kitty", "0x1", 100, 200, 400, 300)]

    assert nav._pan_to_window(windows, "0x1", 960, 540) is False


def test_set_snapshot_floating_uses_explicit_action():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)
    snapshot = {"0x1": {"at": [10, 20], "size": [400, 300]}}

    assert nav._set_snapshot_floating(1, snapshot, floating=True) is True

    lua = ipc.eval_lua.call_args[0][0]
    assert 'action = "enable"' in lua
    assert "window = w" in lua
    assert "hl.get_windows({ workspace = 1 })" in lua


def test_set_all_floating_make_float():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)
    nav._set_all_floating(1, floating=True)
    ipc.eval_lua.assert_called_once()
    lua = ipc.eval_lua.call_args[0][0]
    assert "floating = false" in lua
    assert "window.float" in lua
    assert "window = w" in lua
    assert "_canvas_dispatch" in lua
    assert "dsp.focus" not in lua


def test_set_all_floating_make_tiled():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)
    nav._set_all_floating(1, floating=False)
    lua = ipc.eval_lua.call_args[0][0]
    assert "floating = true" in lua


def test_set_all_floating_error():
    ipc = MagicMock()
    ipc.eval_lua.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    nav._set_all_floating(1, floating=True)  # should not raise


def test_canvas_toggle_on_does_not_commit_after_lua_failure():
    """A compositor failure must not leave canvas mode marked active."""
    tiled = [_make_window("kitty", "0x1", 0, 0, 400, 300, floating=False)]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(tiled)
    ipc.eval_lua.side_effect = [RuntimeError("error: forced Lua failure"), "ok"]

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save") as save,
    ):
        nav = Navigator(ipc, [], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle_all() == "ERROR:FLOAT_FAILED"

    assert 1 not in nav._canvas_mode_workspaces
    assert save.call_count == 2
    assert save.call_args_list[1].args[0] == {}


def test_canvas_toggle_off_keeps_mode_after_tiling_failure():
    """OFF state remains active when compositor tiling fails."""
    stored = {1: {"tiled": {"0x1": {}}, "floating": {}}}
    floating = [_make_window("kitty", "0x1", 10, 20, 400, 300, floating=True)]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(floating)
    ipc.eval_lua.side_effect = [
        RuntimeError("error: forced Lua failure"),
        "ok",
        "ok",
    ]

    with (
        patch("canvas.navigation.toggle_state.load", return_value=stored),
        patch("canvas.navigation.toggle_state.save") as save,
    ):
        nav = Navigator(ipc, [], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle_all() == "ERROR:TILE_FAILED"

    assert 1 in nav._canvas_mode_workspaces
    assert save.call_count == 2
    assert save.call_args_list[1].args[0][1]["active"] is True


def test_canvas_toggle_on_rollback_restores_preexisting_floating_window():
    stored = {
        1: {
            "active": False,
            "tiled": {},
            "floating": {"0x2": {"at": [900, 700], "size": [400, 300]}},
        }
    }
    pre_geometry = {"0x2": {"at": [100, 200], "size": [400, 300]}}
    ipc = MagicMock()

    with (
        patch("canvas.navigation.toggle_state.load", return_value=stored),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc, [], cooldown=0.0)
        with (
            patch.object(nav, "_get_active_workspace_id", return_value=1),
            patch.object(
                nav,
                "_snapshot_tiled_windows",
                return_value={"0x1": {"at": [0, 0], "size": [400, 300]}},
            ),
            patch.object(nav, "_snapshot_floating_geos", return_value=pre_geometry),
            patch.object(nav, "_set_all_floating", return_value=True),
            patch.object(nav, "_apply_canvas_geometry", return_value=False),
            patch.object(nav, "_set_snapshot_floating", return_value=True) as rollback,
            patch.object(nav, "_apply_floating_geos", return_value=True) as apply_geo,
        ):
            assert nav.canvas_toggle_all() == "ERROR:GEOMETRY_RESTORE_FAILED"

    rollback.assert_called_once()
    apply_geo.assert_called_once_with(1, pre_geometry)


def test_canvas_toggle_on_state_save_failure_skips_compositor_action():
    """State must persist before any compositor mutation."""
    tiled = [_make_window("kitty", "0x1", 0, 0, 400, 300, floating=False)]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(tiled)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch(
            "canvas.navigation.toggle_state.save",
            side_effect=ToggleStateError("disk full"),
        ),
    ):
        nav = Navigator(ipc, [], cooldown=0.0)
        with (
            patch.object(nav, "_get_active_workspace_id", return_value=1),
            patch.object(nav, "_set_all_floating") as float_all,
        ):
            assert nav.canvas_toggle_all() == "ERROR:STATE_SAVE_FAILED"
            float_all.assert_not_called()

    assert 1 not in nav._canvas_mode_workspaces


def test_canvas_toggle_off_state_save_failure_skips_tiling():
    stored = {1: {"active": True, "tiled": {"0x1": {}}, "floating": {}}}
    ipc = MagicMock()

    with (
        patch("canvas.navigation.toggle_state.load", return_value=stored),
        patch(
            "canvas.navigation.toggle_state.save",
            side_effect=ToggleStateError("disk full"),
        ),
    ):
        nav = Navigator(ipc, [], cooldown=0.0)
        with (
            patch.object(nav, "_get_active_workspace_id", return_value=1),
            patch.object(nav, "_snapshot_floating_geos", return_value={}),
            patch.object(nav, "_tile_windows") as tile,
        ):
            assert nav.canvas_toggle_all() == "ERROR:STATE_SAVE_FAILED"
            tile.assert_not_called()

    assert nav._canvas_mode_workspaces == {1: {"0x1": {}}}


def test_canvas_toggle_no_workspace():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    assert nav.canvas_toggle() == "ERROR:NO_WORKSPACE"


def test_safe_int_valid():
    assert _safe_int(42, "x") == 42
    assert _safe_int("100", "x") == 100


def test_safe_int_invalid():
    import pytest

    with pytest.raises(ValueError, match="unsafe Lua value"):
        _safe_int("abc", "x")


def test_safe_int_none():
    import pytest

    with pytest.raises(ValueError, match="unsafe Lua value"):
        _safe_int(None, "x")


def test_navigate_reports_floating_query_failure():
    nav = _make_nav(MagicMock())
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=None),
    ):
        assert nav.navigate("right") is False


def test_navigate_no_workspace():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    nav = _make_nav(ipc)
    nav.navigate("right")  # should not raise


def test_navigate_no_focused():
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        json.dumps([]),
        Exception("no focused"),
    ]
    nav = _make_nav(ipc)
    nav.navigate("right")  # should not raise


def test_navigate_focused_not_in_list():
    ipc = MagicMock()
    w = _make_window("a", "0x1", 100, 100, 400, 300)
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        json.dumps([w]),
        json.dumps({"address": "0x999"}),
    ]
    nav = _make_nav(ipc)
    nav.navigate("right")  # should not raise


def test_pan_to_window_scopes_lua_to_workspace():
    ipc = MagicMock()
    nav = _make_nav(ipc)
    windows = [_make_window("kitty", "0x1", 100, 200, 400, 300)]
    nav._pan_to_window(windows, "0x1", 960, 540, workspace_id=7)
    lua = ipc.eval_lua.call_args[0][0]
    assert lua.count("workspace = 7") == 2  # move loop + focus loop


def test_navigate_cooldown_uses_monotonic_clock():
    """Cooldown must be monotonic-based so wall-clock jumps cannot break it."""
    import canvas.navigation as nav_mod

    windows = [
        _make_window("a", "0x1", 0, 0, 100, 100),
        _make_window("b", "0x2", 500, 0, 100, 100),
    ]
    ipc = MagicMock()

    def fake_send(cmd):
        if "activeworkspace" in cmd:
            return json.dumps({"id": 1})
        if "clients" in cmd:
            return json.dumps(windows)
        if "activewindow" in cmd:
            return json.dumps({"class": "a", "address": "0x1"})
        return json.dumps([{"focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080}])

    ipc.send.side_effect = fake_send
    nav = Navigator(ipc=ipc, protected_apps=[], cooldown=10.0)

    with patch.object(nav_mod.time, "monotonic", side_effect=[1000.0, 1000.05]):
        nav.navigate("right")  # stamps t=1000.0
        nav.navigate("right")  # dt=0.05 < cooldown → blocked

    assert ipc.eval_lua.call_count == 1


def test_navigate_passes_workspace_to_pan():
    """navigate() must scope the pan Lua to the active workspace."""
    windows = [
        _make_window("a", "0x1", 0, 0, 100, 100, workspace_id=9),
        _make_window("b", "0x2", 500, 0, 100, 100, workspace_id=9),
    ]
    ipc = MagicMock()

    def fake_send(cmd):
        if "activeworkspace" in cmd:
            return json.dumps({"id": 9})
        if "clients" in cmd:
            return json.dumps(windows)
        if "activewindow" in cmd:
            return json.dumps({"class": "a", "address": "0x1"})
        return json.dumps([{"focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080}])

    ipc.send.side_effect = fake_send
    nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)

    nav.navigate("right")

    assert ipc.eval_lua.call_count == 1
    assert "workspace = 9" in ipc.eval_lua.call_args[0][0]
