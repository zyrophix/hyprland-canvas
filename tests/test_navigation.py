import json
from unittest.mock import MagicMock, patch

from canvas.navigation import Navigator


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
    """Create a fake window dict as hyprctl would return."""
    return {
        "class": class_name,
        "address": address,
        "at": [at_x, at_y],
        "size": [size_w, size_h],
        "floating": floating,
        "workspace": {"id": workspace_id},
    }


def test_navigate_right_cycles():
    """Navigate right cycles through windows, skipping protected."""
    windows = [
        _make_window("terminal", "0x1", 100, 100, 400, 300),
        _make_window("firefox", "0x2", 600, 100, 800, 600),  # protected
        _make_window("editor", "0x3", 100, 500, 400, 300),
    ]

    nav = Navigator(ipc=MagicMock(), protected_apps=["firefox"], cooldown=0.0)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "_pan_to_window") as mock_pan,
    ):
        nav.navigate("right")
        mock_pan.assert_called_once()
        called_addr = mock_pan.call_args[0][1]
        assert called_addr == "0x3"


def test_navigate_left_cycles():
    """Navigate left goes to previous window."""
    windows = [
        _make_window("terminal", "0x1", 100, 100, 400, 300),
        _make_window("editor", "0x3", 100, 500, 400, 300),
    ]

    nav = Navigator(ipc=MagicMock(), protected_apps=[], cooldown=0.0)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[1]),
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "_pan_to_window") as mock_pan,
    ):
        nav.navigate("left")
        called_addr = mock_pan.call_args[0][1]
        assert called_addr == "0x1"


def test_navigate_single_window_does_nothing():
    """With only one floating window, navigation does nothing."""
    windows = [_make_window("terminal", "0x1", 100, 100, 400, 300)]

    nav = Navigator(ipc=MagicMock(), protected_apps=[], cooldown=0.0)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_pan_to_window") as mock_pan,
    ):
        nav.navigate("right")
        mock_pan.assert_not_called()


def test_is_protected():
    """Protected apps are detected by class name."""
    nav = Navigator(ipc=MagicMock(), protected_apps=["firefox", "brave-browser"], cooldown=0.0)
    assert nav._is_protected({"class": "firefox"}) is True
    assert nav._is_protected({"class": "brave-browser"}) is True
    assert nav._is_protected({"class": "kitty"}) is False


def test_navigate_all_protected_does_nothing():
    """When all windows are protected, navigation does nothing."""
    windows = [
        _make_window("firefox", "0x1", 100, 100, 400, 300),
        _make_window("brave-browser", "0x2", 600, 100, 800, 600),
    ]

    nav = Navigator(ipc=MagicMock(), protected_apps=["firefox", "brave-browser"], cooldown=0.0)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_pan_to_window") as mock_pan,
    ):
        nav.navigate("right")
        mock_pan.assert_not_called()


def test_navigate_cooldown_blocks_rapid_calls():
    """Cooldown prevents rapid successive navigation calls."""
    windows = [
        _make_window("a", "0x1", 100, 100, 400, 300),
        _make_window("b", "0x2", 600, 100, 400, 300),
    ]

    nav = Navigator(ipc=MagicMock(), protected_apps=[], cooldown=10.0)  # 10s cooldown

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=windows),
        patch.object(nav, "_get_focused_window", return_value=windows[0]),
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "_pan_to_window") as mock_pan,
    ):
        nav.navigate("right")
        assert mock_pan.call_count == 1
        nav.navigate("right")  # blocked by cooldown
        assert mock_pan.call_count == 1


def test_canvas_toggle_on_records_tiled_snapshot():
    """Canvas ON records which windows were tiled so OFF can restore exactly them."""
    ipc = MagicMock()
    windows = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
        _make_window("kitty", "0x2", 0, 0, 100, 100, floating=True),
    ]
    ipc.send.return_value = json.dumps(windows)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save") as msave,
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"

        # New format: snapshot is dict addr -> {at, size}
        assert "0x1" in nav._canvas_mode_workspaces[1]
        assert nav._canvas_mode_workspaces[1]["0x1"]["at"] == [0, 0]
        assert nav._canvas_mode_workspaces[1]["0x1"]["size"] == [100, 100]
        lua = ipc.eval_lua.call_args[0][0]
        assert "floating = false" in lua
        msave.assert_called_once()


def test_canvas_toggle_off_restores_only_snapshot():
    """Windows that became floating DURING canvas mode must not be tiled on OFF."""
    pre_canvas = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
    ]
    during_canvas = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=True),
        _make_window("kitty", "0x3", 0, 0, 100, 100, floating=True),  # floated by user later
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(pre_canvas)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"

            ipc.send.return_value = json.dumps(during_canvas)
            assert nav.canvas_toggle() == "CANVAS_OFF"

        assert 1 not in nav._canvas_mode_workspaces
        off_lua = ipc.eval_lua.call_args[0][0]
        assert '"0x1"' in off_lua
        assert '"0x3"' not in off_lua
        assert 'action = "toggle"' in off_lua


def test_canvas_toggle_off_with_empty_snapshot_skips_ipc():
    """Enabling canvas on an all-floating workspace → OFF tiles nothing."""
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [_make_window("kitty", "0x2", 0, 0, 100, 100, floating=True)]
    )

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save") as msave,
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"
            assert msave.call_args_list[0].args[0] == {
                1: {
                    "active": True,
                    "tiled": {},
                    "floating": {},
                    "pre_floating": [],
                    "spawn_rules": [],
                }
            }
            ipc.reset_mock()
            assert nav.canvas_toggle() == "CANVAS_OFF"

        ipc.eval_lua.assert_not_called()


def test_canvas_toggle_restores_empty_active_marker():
    """v3 active=true preserves an all-floating canvas session across restart."""
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [_make_window("kitty", "0x2", 0, 0, 100, 100, floating=True)]
    )
    raw = {1: {"active": True, "tiled": {}, "floating": {}}}

    with (
        patch("canvas.navigation.toggle_state.load", return_value=raw),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        assert nav._canvas_mode_workspaces == {1: {}}
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_OFF"


def test_canvas_toggle_distinguishes_inactive_floating_geometry():
    """active=false with stored geometry must restore as the next ON, not OFF."""
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [_make_window("kitty", "0x2", 0, 0, 100, 100, floating=True)]
    )
    raw = {
        1: {
            "active": False,
            "tiled": {},
            "floating": {},
        }
    }

    with (
        patch("canvas.navigation.toggle_state.load", return_value=raw),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        assert 1 not in nav._canvas_mode_workspaces
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"


def test_canvas_toggle_after_restart_is_safe():
    """Restored state from disk → first press acts as OFF and tiles only the snapshot."""
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [
            _make_window("kitty", "0x1", 0, 0, 100, 100, floating=True),
            _make_window("kitty", "0x9", 0, 0, 100, 100, floating=True),
        ]
    )

    with (
        patch("canvas.navigation.toggle_state.load", return_value={1: ["0x1"]}),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            # Old bug: this press returned CANVAS_ON (state lost) and the NEXT
            # press tiled everything. Now the snapshot survives restarts.
            assert nav.canvas_toggle() == "CANVAS_OFF"

        lua = ipc.eval_lua.call_args[0][0]
        assert '"0x1"' in lua
        assert '"0x9"' not in lua


def test_canvas_toggle_off_captures_panned_geos_only():
    """OFF remembers only geometry the user actually panned to."""
    pre_canvas = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
    ]
    during_canvas = [
        _make_window("kitty", "0x1", 500, 600, 400, 300, floating=True),
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(pre_canvas)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save") as msave,
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"

            # Panned by the user, so the position is a deliberate choice.
            nav.note_panned(1, {"0x1"})

            ipc.send.return_value = json.dumps(during_canvas)
            assert nav.canvas_toggle() == "CANVAS_OFF"

        assert nav._floating_geos[1]["0x1"] == {"at": [500, 600], "size": [400, 300]}
        saved = msave.call_args[0][0]
        assert saved[1]["floating"]["0x1"] == {"at": [500, 600], "size": [400, 300]}
        # OFF itself never moves windows — plain toggle only
        off_lua = ipc.eval_lua.call_args[0][0]
        assert "hl.dsp.window.move" not in off_lua
        assert "hl.dsp.window.resize" not in off_lua


def test_canvas_toggle_on_restores_floating_geos():
    """ON moves newly floated windows back to stored floating positions."""
    tiled = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
    ]
    floated = [
        _make_window("kitty", "0x1", 10, 10, 100, 100, floating=True),
    ]
    stored = {
        1: {
            "tiled": {},
            "floating": {"0x1": {"at": [500, 600], "size": [400, 300]}},
        }
    }
    ipc = MagicMock()
    ipc.send.side_effect = [json.dumps(tiled), json.dumps(floated)]

    with (
        patch("canvas.navigation.toggle_state.load", return_value=stored),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"

        assert ipc.eval_lua.call_count == 2
        restore_lua = ipc.eval_lua.call_args_list[1][0][0]
        assert "at={500,600}" in restore_lua
        assert "size={400,300}" in restore_lua
        assert "hl.dsp.window.move" in restore_lua
        assert "hl.dsp.window.resize" in restore_lua
        assert "x = g.size[1], y = g.size[2]" in restore_lua
        assert "width = g.size[1]" not in restore_lua
        assert "height = g.size[2]" not in restore_lua


def test_canvas_toggle_on_without_stored_geos_skips_restore():
    """ON with no stored floating geometry issues only the float toggle."""
    tiled = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(tiled)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"

        assert ipc.eval_lua.call_count == 1


def test_tile_order_is_row_major():
    """Toggle order follows saved (y, x); entries without coords go last."""
    nav = Navigator(ipc=MagicMock(), protected_apps=[], cooldown=0.0)
    snapshot = {
        "0xb": {"at": [0, 500], "size": [100, 100]},
        "0xa": {"at": [500, 0], "size": [100, 100]},
        "0x9": {"at": [0, 0], "size": [100, 100]},
        "0xc": {},
    }
    nav._tile_windows(1, snapshot)

    import re

    lua = nav._ipc.eval_lua.call_args[0][0]
    order = re.findall(r'"(0x[0-9a-fA-F]+)",', lua.split("local order", 1)[1])
    assert order == ["0x9", "0xa", "0xb", "0xc"]


def test_preserve_geometry_false_skips_capture_and_restore():
    """Flag off: snapshots stay address-only, no floating geo traffic."""
    tiled = [
        _make_window("kitty", "0x1", 0, 0, 100, 100, floating=False),
    ]
    during = [
        _make_window("kitty", "0x1", 500, 600, 400, 300, floating=True),
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(tiled)

    with (
        patch("canvas.navigation.toggle_state.load", return_value={}),
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav = Navigator(ipc=ipc, protected_apps=[], cooldown=0.0, preserve_geometry=False)
        with patch.object(nav, "_get_active_workspace_id", return_value=1):
            assert nav.canvas_toggle() == "CANVAS_ON"
            assert nav._canvas_mode_workspaces[1] == {"0x1": {}}

            ipc.send.return_value = json.dumps(during)
            assert nav.canvas_toggle() == "CANVAS_OFF"

        assert nav._floating_geos == {}
