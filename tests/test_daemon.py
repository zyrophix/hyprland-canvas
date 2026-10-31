"""Tests for canvas.daemon — DaemonState and helper functions."""

import json
import time
from unittest.mock import MagicMock, patch

from canvas.daemon import DaemonState, _lua_escape
from canvas.navigation import Navigator
from canvas.panning import EdgeScrollParams, EdgeScrollState, PanningState


def _make_daemon_state(ipc: MagicMock | None = None, navigator=None) -> DaemonState:
    panning = PanningState(speed=1.0)
    edge_scroll = EdgeScrollState(ramp_distance=50, speed=20.0, enabled=True)
    if ipc is None:
        ipc = MagicMock()
    if navigator is None:
        # A real Navigator, not a mock. DaemonState asks it which windows the
        # camera must leave alone, and a mock answers every window with a truthy
        # object — so a mocked navigator silently excludes all of them.
        # Patched load() keeps the test off the real toggle-state file.
        with patch("canvas.navigation.toggle_state.load", return_value={}):
            navigator = Navigator(ipc=ipc, cooldown=0.0)
    return DaemonState(panning=panning, edge_scroll=edge_scroll, navigator=navigator, ipc=ipc)


def _clients(clients_json: str) -> str:
    return clients_json


def test_handle_ipc_pan_start_fetches_baselines():
    """PAN_START fetches baselines and starts panning."""
    ipc = MagicMock()
    ipc.send.side_effect = [json.dumps({"id": 1}), "[]"]
    ds = _make_daemon_state(ipc)

    result = ds.handle_ipc("PAN_START")

    assert result == "PAN_ON"
    assert ds.panning.pan_active is True
    ipc.send.assert_any_call("j/clients")


def test_handle_ipc_pan_stop_clears_baselines():
    """PAN_STOP stops panning and clears baselines."""
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (100, 200)}

    result = ds.handle_ipc("PAN_STOP")

    assert result == "PAN_OFF"
    assert ds.panning.pan_active is False
    assert ds.baselines == {}


def test_handle_ipc_toggle():
    """TOGGLE flips inverted state."""
    ds = _make_daemon_state()
    ds.panning.inverted = True

    result = ds.handle_ipc("TOGGLE")
    assert result == "NORMAL"
    assert ds.panning.inverted is False

    result2 = ds.handle_ipc("TOGGLE")
    assert result2 == "INVERTED"
    assert ds.panning.inverted is True


def test_handle_ipc_nav():
    """NAV_LEFT/NAV_RIGHT delegates to navigator."""
    ds = _make_daemon_state(navigator=MagicMock())

    assert ds.handle_ipc("NAV_LEFT") == "OK"
    ds.navigator.navigate.assert_called_with("left")

    assert ds.handle_ipc("NAV_RIGHT") == "OK"
    ds.navigator.navigate.assert_called_with("right")


def test_handle_ipc_ping():
    assert _make_daemon_state().handle_ipc("PING") == "PONG"


def _status_fields(response: str) -> dict[str, str]:
    """The two-column status report as a mapping, for asserting on one line."""
    return {line.split(None, 1)[0]: line.split(None, 1)[1] for line in response.splitlines()}


def _compositor_ipc(workspace_id: int = 1, clients: list[dict] | None = None) -> MagicMock:
    """An IPC double that answers the two queries `status` makes."""
    payload = json.dumps(clients or [])

    def send(cmd: str) -> str:
        return json.dumps({"id": workspace_id}) if cmd == "j/activeworkspace" else payload

    ipc = MagicMock()
    ipc.send.side_effect = send
    return ipc


def test_handle_ipc_status():
    ds = _make_daemon_state()
    ds.panning.inverted = True

    fields = _status_fields(ds.handle_ipc("STATUS"))
    assert fields["pan"] == "INVERTED IDLE"
    assert fields["edge"] == "IDLE"

    ds.panning.start_pan()
    fields = _status_fields(ds.handle_ipc("STATUS"))
    assert fields["pan"] == "INVERTED PANNING"


def test_handle_ipc_status_names_the_config_file_it_read():
    ds = _make_daemon_state()
    ds.config_source = "/home/u/.config/canvas/config.yml"

    assert _status_fields(ds.handle_ipc("STATUS"))["config"] == ds.config_source


def test_handle_ipc_status_says_unknown_rather_than_failing():
    """A status report that refuses to print because one query timed out is
    worse than one with a gap in it."""
    ipc = MagicMock()
    ipc.send.side_effect = TimeoutError("compositor busy")
    ds = _make_daemon_state(ipc)

    fields = _status_fields(ds.handle_ipc("STATUS"))

    assert fields["config"] == "built-in defaults"
    assert fields["canvas"] == "none"
    assert fields["rules"] == "none"
    assert fields["workspace"] == "unknown"
    assert fields["floating"] == "unknown"


def test_handle_ipc_status_prints_the_workspace_with_an_unknown_window_count():
    """The two queries fail independently: a workspace id is useful on its own,
    so a failed client query must not discard it."""
    ipc = MagicMock()

    def send(cmd: str) -> str:
        if cmd == "j/activeworkspace":
            return json.dumps({"id": 4})
        raise TimeoutError("compositor busy")

    ipc.send.side_effect = send
    ds = _make_daemon_state(ipc)

    fields = _status_fields(ds.handle_ipc("STATUS"))

    assert fields["workspace"] == "4"
    assert fields["floating"] == "unknown"


def test_handle_ipc_status_keeps_a_fixed_field_order():
    """The report is read column-wise, so the lines have a stable order."""
    lines = _make_daemon_state().handle_ipc("STATUS").splitlines()

    assert [line.split()[0] for line in lines] == [
        "version",
        "pid",
        "config",
        "pan",
        "edge",
        "canvas",
        "workspace",
        "floating",
        "rules",
    ]


def test_handle_ipc_status_reads_the_compositor():
    clients = [
        {"floating": True, "workspace": {"id": 2}},
        {"floating": True, "workspace": {"id": 3}},
        {"floating": False, "workspace": {"id": 2}},
    ]
    ds = _make_daemon_state(_compositor_ipc(workspace_id=2, clients=clients))

    fields = _status_fields(ds.handle_ipc("STATUS"))

    assert fields["workspace"] == "2"
    assert fields["floating"] == "1"


def test_handle_ipc_status_lists_canvas_workspaces_and_rules():
    ds = _make_daemon_state()
    ds.navigator._canvas_mode_workspaces = {3: {}, 1: {}}
    ds.navigator._spawn_rules = {
        3: ["canvas-spawn-ws3-default", "canvas-spawn-ws1-default"],
        1: ["canvas-spawn-ws1-default", "canvas-spawn-ws1-fixed"],
    }

    fields = _status_fields(ds.handle_ipc("STATUS"))

    assert fields["canvas"] == "1, 3"
    assert (
        fields["rules"]
        == "canvas-spawn-ws1-default, canvas-spawn-ws1-fixed, canvas-spawn-ws3-default"
    )


def test_handle_ipc_status_json_is_a_single_object():
    import os

    ds = _make_daemon_state()
    ds.panning.inverted = True

    response = ds.handle_ipc("STATUS_JSON")
    payload = json.loads(response)

    assert set(payload) == {
        "version",
        "pid",
        "config",
        "inverted",
        "panning",
        "edge_scroll",
        "canvas_workspaces",
        "workspace",
        "floating",
        "spawn_rules",
    }
    assert payload["inverted"] is True
    assert payload["panning"] is False
    assert payload["canvas_workspaces"] == []
    assert payload["pid"] == os.getpid()
    assert "\n" not in response


def test_handle_ipc_unknown():
    assert _make_daemon_state().handle_ipc("FOOBAR") == "UNKNOWN: FOOBAR"


def test_fetch_baselines_parses_clients():
    """fetch_baselines extracts floating window addresses and positions."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients(
            '[{"address":"0xabc","floating":true,"at":[10,20],"workspace":{"id":1}},'
            '{"address":"0xdef","floating":false,"at":[30,40],"workspace":{"id":1}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {"0xabc": (10, 20)}
    assert ds.baseline_workspace == 1


def test_fetch_baselines_scoped_to_active_workspace():
    """Windows on other workspaces must not enter the baseline snapshot."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients(
            '[{"address":"0xaaa","floating":true,"at":[1,2],"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[3,4],"workspace":{"id":2}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {"0xaaa": (1, 2)}
    assert ds.baseline_workspace == 1


def test_fetch_baselines_empty_response():
    """fetch_baselines handles empty client list."""
    ipc = MagicMock()
    ipc.send.side_effect = [json.dumps({"id": 1}), "[]"]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {}


def test_fetch_baselines_bad_json():
    """fetch_baselines handles invalid JSON gracefully."""
    ipc = MagicMock()
    ipc.send.return_value = "not json"
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {}
    assert ds.baseline_workspace is None


def test_restore_baselines_moves_windows_back():
    """restore_baselines sends Lua to move windows to original positions."""
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    ds = _make_daemon_state(ipc)
    ds.baselines = {"0xabc": (100, 200)}
    ds.baseline_workspace = 4

    ds.restore_baselines()

    ipc.eval_lua.assert_called_once()
    lua_code = ipc.eval_lua.call_args[0][0]
    assert "0xabc" in lua_code
    assert "b[1]" in lua_code
    assert "relative = false" in lua_code
    assert "workspace = 4" in lua_code


def test_restore_baselines_skips_when_empty():
    """restore_baselines does nothing if no baselines recorded."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)

    ds.restore_baselines()

    ipc.eval_lua.assert_not_called()


def test_move_windows_to_delta():
    """move_windows_to_delta generates Lua with correct offsets and workspace scope."""
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    ds = _make_daemon_state(ipc)
    ds.baselines = {"0xabc": (100, 200)}
    ds.baseline_workspace = 2

    ds.move_windows_to_delta(50, -30)

    ipc.eval_lua.assert_called_once()
    lua_code = ipc.eval_lua.call_args[0][0]
    assert "b[1] + 50" in lua_code
    assert "b[2] + -30" in lua_code
    assert "relative = false" in lua_code
    assert "workspace = 2" in lua_code


def test_move_windows_to_delta_without_workspace_is_noop():
    """Without a captured workspace (failed fetch) no Lua must be dispatched."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.baselines = {"0xabc": (100, 200)}
    ds.baseline_workspace = None

    ds.move_windows_to_delta(50, 30)

    ipc.eval_lua.assert_not_called()


def test_lua_escape():
    """_lua_escape handles backslashes, quotes, and newlines."""
    assert _lua_escape("hello") == "hello"
    assert _lua_escape('a"b') == 'a\\"b'
    assert _lua_escape("a\\b") == "a\\\\b"
    assert _lua_escape("a\nb") == "a\\nb"
    assert _lua_escape("0x1a2b") == "0x1a2b"


def test_fetch_baselines_window_without_at():
    """fetch_baselines uses default [0,0] when 'at' field missing."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients('[{"address":"0xabc","floating":true,"workspace":{"id":1}}]'),
    ]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {"0xabc": (0, 0)}


def test_fetch_baselines_window_with_empty_address():
    """fetch_baselines skips windows with empty address."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients('[{"address":"","floating":true,"at":[10,20]}]'),
    ]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {}


def test_fetch_baselines_ipc_error():
    """fetch_baselines handles IPC send failure gracefully."""
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("socket failed")
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {}


def test_restore_baselines_ipc_error():
    """restore_baselines handles IPC eval_lua failure gracefully."""
    ipc = MagicMock()
    ipc.eval_lua.side_effect = ConnectionError("socket failed")
    ds = _make_daemon_state(ipc)
    ds.baselines = {"0xabc": (100, 200)}

    ds.restore_baselines()  # should not raise

    assert ds.baselines == {"0xabc": (100, 200)}


def test_move_windows_to_delta_ipc_error():
    """move_windows_to_delta handles IPC failure gracefully."""
    ipc = MagicMock()
    ipc.eval_lua.side_effect = ConnectionError("socket failed")
    ds = _make_daemon_state(ipc)
    ds.baselines = {"0xabc": (100, 200)}

    ds.move_windows_to_delta(50, 50)  # should not raise


def test_fetch_baselines_multiple_floating():
    """fetch_baselines correctly extracts multiple floating windows."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients(
            '[{"address":"0xaaa","floating":true,"at":[10,20],"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[300,400],"workspace":{"id":1}},'
            '{"address":"0xccc","floating":false,"at":[500,600],"workspace":{"id":1}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    ds.fetch_baselines()

    assert ds.baselines == {"0xaaa": (10, 20), "0xbbb": (300, 400)}


def _clients_json(windows_json: str) -> str:
    """Helper: wrap a raw clients JSON for side_effect sequences."""
    return windows_json


def test_handle_ipc_edge_start():
    """EDGE_START grabs the floating window under the cursor."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 3}),
        _clients_json(
            '[{"address":"0xabc","floating":true,"at":[100,200],"size":[500,300],'
            '"workspace":{"id":3}}]'
        ),
        json.dumps({"address": "0xabc"}),
        _clients_json(
            '[{"address":"0xabc","floating":true,"at":[100,200],"size":[500,300],'
            '"workspace":{"id":3}}]'
        ),
        '[{"focused":true,"x":0,"y":0,"width":1920,"height":1080}]',
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        result = ds.handle_ipc("EDGE_START")
        assert result == "EDGE_ON"
        assert ds.edge_scroll.active is True
        assert ds.edge_scroll.dragged_addr == "0xabc"
        assert ds.edge_scroll_workspace == 3


def test_handle_ipc_edge_start_focus_mismatch_refuses():
    """Press inside bounding rect but Hyprland focused another window →
    the click landed on border/gap and no drag will engage. Must not arm."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients_json(
            '[{"address":"0xabc","floating":true,"at":[100,200],"size":[500,300],'
            '"workspace":{"id":1}}]'
        ),
        json.dumps({"address": "0xfocused_elsewhere"}),
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WINDOW"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_start_no_window():
    """Cursor over empty desktop → no activation, even with a focused window."""
    ipc = MagicMock()
    ipc.send.side_effect = [json.dumps({"id": 1}), "[]"]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(500, 500)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WINDOW"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_start_ignores_offscreen_focused_window():
    """Regression: a stale-focused off-screen window must never drive the camera.

    Old behavior: geometry came from j/activewindow, grab offsets were
    computed against an invisible window, and any drag attempt sent the
    camera chasing it at max speed. Now the window must be under the
    cursor to activate at all.
    """
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients_json(
            # only floating window sits far off-screen; cursor clicks empty space
            '[{"address":"0xdead","floating":true,"at":[2500,390],"size":[500,300],'
            '"workspace":{"id":1}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    with (
        patch("canvas.daemon.get_cursor_pos", return_value=(960, 540)),
        patch("canvas.daemon.log") as _,
    ):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WINDOW"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_start_picks_window_under_cursor():
    """Two floating windows — the one containing the cursor is dragged."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients_json(
            '[{"address":"0xaaa","floating":true,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[600,100],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
        json.dumps({"address": "0xbbb"}),
        _clients_json(
            '[{"address":"0xaaa","floating":true,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[600,100],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
        '[{"focused":true,"x":0,"y":0,"width":1920,"height":1080}]',
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(800, 200)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_ON"
    assert ds.edge_scroll.dragged_addr == "0xbbb"


def test_handle_ipc_edge_start_overlap_last_wins():
    """Overlapping floating windows → last match (topmost guess) is dragged."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients_json(
            '[{"address":"0xaaa","floating":true,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[150,150],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
        json.dumps({"address": "0xbbb"}),
        _clients_json(
            '[{"address":"0xaaa","floating":true,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}},'
            '{"address":"0xbbb","floating":true,"at":[150,150],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
        '[{"focused":true,"x":0,"y":0,"width":1920,"height":1080}]',
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(300, 250)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_ON"
    assert ds.edge_scroll.dragged_addr == "0xbbb"


def test_find_window_at_cursor_skips_hidden_and_fullscreen_overlaps():
    """Pinned windows stay movable; hidden/fullscreen clients are skipped."""
    clients = [
        {
            "address": "0xvisible",
            "floating": True,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
        {
            "address": "0xhidden",
            "floating": True,
            "hidden": True,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
        {
            "address": "0xpinned",
            "floating": True,
            "pinned": True,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
        {
            "address": "0xfullscreen",
            "floating": True,
            "fullscreen": 2,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(clients)
    ds = _make_daemon_state(ipc)

    found = ds._find_window_at_cursor(300, 250, 1)

    assert found is not None
    assert found["address"] == "0xpinned"


def test_find_window_at_cursor_keeps_maximized_clients():
    """`fullscreen` is eFullscreenMode, so 1 is maximized, not fullscreen.

    A maximized floating window is still an ordinary window the user can drag,
    so it stays a candidate; only 2 takes over the output.
    """
    clients = [
        {
            "address": "0xvisible",
            "floating": True,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
        {
            "address": "0xmaximized",
            "floating": True,
            "fullscreen": 1,
            "at": [100, 100],
            "size": [400, 300],
            "workspace": {"id": 1},
        },
    ]
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(clients)
    ds = _make_daemon_state(ipc)

    found = ds._find_window_at_cursor(300, 250, 1)

    assert found is not None
    assert found["address"] == "0xmaximized"


def test_handle_ipc_edge_start_tiled_only_under_cursor():
    """A tiled window under the cursor is not canvas-draggable."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 1}),
        _clients_json(
            '[{"address":"0xtile","floating":false,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(300, 250)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WINDOW"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_start_ignores_other_workspace_window():
    """Floating window containing the cursor point but on another workspace → skip."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 2}),
        _clients_json(
            '[{"address":"0xws1","floating":true,"at":[100,100],"size":[400,300],'
            '"workspace":{"id":1}}]'
        ),
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(300, 250)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WINDOW"


def test_handle_ipc_edge_start_no_workspace():
    """EDGE_START without a resolvable workspace must not activate."""
    ipc = MagicMock()
    ipc.send.side_effect = [Exception("workspace query failed")]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_WORKSPACE"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_start_no_monitor():
    """EDGE_START without monitor geometry must not activate edge-scroll."""
    ipc = MagicMock()
    ipc.send.side_effect = [
        json.dumps({"id": 3}),
        _clients_json(
            '[{"address":"0xabc","floating":true,"at":[100,200],"size":[500,300],'
            '"workspace":{"id":3}}]'
        ),
        json.dumps({"address": "0xabc"}),
        Exception("monitors query failed"),
    ]
    ds = _make_daemon_state(ipc)

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_NO_MONITOR"
    assert ds.edge_scroll.active is False


def test_handle_ipc_edge_stop():
    """EDGE_STOP deactivates edge-scroll."""
    ds = _make_daemon_state()
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=100,
            win_y=200,
            win_w=500,
            win_h=300,
            cursor_x=350,
            cursor_y=350,
        )
    )

    result = ds.handle_ipc("EDGE_STOP")
    assert result == "EDGE_OFF"
    assert ds.edge_scroll.active is False


def test_edge_scroll_move_excludes_dragged():
    """edge_scroll_move generates Lua that excludes dragged window, workspace-scoped."""
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    ds = _make_daemon_state(ipc)
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=100,
            win_y=200,
            win_w=500,
            win_h=300,
            cursor_x=350,
            cursor_y=350,
        )
    )
    ds.edge_scroll_workspace = 5

    ds.edge_scroll_move(10, -5)

    ipc.eval_lua.assert_called_once()
    lua_code = ipc.eval_lua.call_args[0][0]
    assert "0xabc" in lua_code
    assert "~=" in lua_code
    assert "relative = true" in lua_code
    assert "workspace = 5" in lua_code
    assert "_canvas_dispatch" in lua_code


def test_edge_scroll_move_without_workspace_is_noop():
    """Without a captured workspace edge moves must never dispatch."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=100,
            win_y=200,
            win_w=500,
            win_h=300,
            cursor_x=350,
            cursor_y=350,
        )
    )
    ds.edge_scroll_workspace = None

    ds.edge_scroll_move(10, -5)

    ipc.eval_lua.assert_not_called()


def test_edge_scroll_move_zero_delta_is_noop():
    """edge_scroll_move with (0,0) does nothing."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=100,
            win_y=200,
            win_w=500,
            win_h=300,
            cursor_x=350,
            cursor_y=350,
        )
    )

    ds.edge_scroll_move(0, 0)

    ipc.eval_lua.assert_not_called()


# --- the drag bind's release re-run -----------------------------------------


def _armable_ipc() -> MagicMock:
    """An IPC double that answers every edge-start query, however many arrive.

    A fixed list would run out and turn a second start into `EDGE_NO_WORKSPACE`,
    which would make a missing guard look like a refused start rather than an
    armed one.
    """
    clients = _clients_json(
        '[{"address":"0xabc","floating":true,"at":[100,200],"size":[500,300],'
        '"workspace":{"id":3}}]'
    )

    def send(cmd: str) -> str:
        if cmd == "j/activeworkspace":
            return json.dumps({"id": 3})
        if cmd == "j/activewindow":
            return json.dumps({"address": "0xabc"})
        if cmd == "j/monitors":
            return '[{"focused":true,"x":0,"y":0,"width":1920,"height":1080}]'
        return clients

    ipc = MagicMock()
    ipc.send.side_effect = send
    return ipc


def test_edge_start_right_after_a_stop_is_ignored():
    """`window.drag()` makes Hyprland re-run the press bind when the button comes
    up, so a second edge-start lands milliseconds after edge-stop. Left alone it
    arms a session with nothing held."""
    ds = _make_daemon_state(_armable_ipc())

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        assert ds.handle_ipc("EDGE_START") == "EDGE_ON"
        assert ds.handle_ipc("EDGE_STOP") == "EDGE_OFF"
        assert ds.handle_ipc("EDGE_START") == "EDGE_IGNORED"

    assert ds.edge_scroll.active is False


def test_edge_start_after_the_guard_window_is_honored():
    """The guard is a few milliseconds of slop, not a latch: a later press is a
    real gesture and must arm."""
    ds = _make_daemon_state(_armable_ipc())
    ds._last_edge_stop = time.monotonic() - ds._EDGE_RESTART_GUARD - 0.1

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        assert ds.handle_ipc("EDGE_START") == "EDGE_ON"

    assert ds.edge_scroll.active is True


def test_an_ignored_edge_start_still_cancels_a_running_pan():
    """Modes are mutually exclusive, and EDGE_START is what enforces it. The
    re-run guard must not open a hole where both keep running."""
    ds = _make_daemon_state(_armable_ipc())
    ds.panning.start_pan()
    ds._last_edge_stop = time.monotonic()

    with patch("canvas.daemon.get_cursor_pos", return_value=(350, 350)):
        assert ds.handle_ipc("EDGE_START") == "EDGE_IGNORED"

    assert ds.panning.pan_active is False
