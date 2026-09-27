"""Additional tests for canvas.daemon — covering edge cases and daemon state."""

from unittest.mock import MagicMock, patch

from canvas.daemon import DaemonState
from canvas.panning import EdgeScrollParams, EdgeScrollState, PanningState


def _make_daemon_state(ipc: MagicMock | None = None) -> DaemonState:
    panning = PanningState(speed=1.0)
    edge_scroll = EdgeScrollState(ramp_distance=50, speed=20.0, enabled=True)
    navigator = MagicMock()
    if ipc is None:
        ipc = MagicMock()
    return DaemonState(panning=panning, edge_scroll=edge_scroll, navigator=navigator, ipc=ipc)


def test_fetch_monitor_rect_focused():
    ipc = MagicMock()
    ipc.send.return_value = '[{"focused":true,"x":0,"y":0,"width":2560,"height":1440}]'
    ds = _make_daemon_state(ipc)
    ds._fetch_monitor_rect()
    assert ds.edge_scroll._monitor_w == 2560
    assert ds.edge_scroll._monitor_h == 1440


def test_fetch_monitor_rect_fallback_first():
    ipc = MagicMock()
    ipc.send.return_value = '[{"focused":false,"x":0,"y":0,"width":1920,"height":1080}]'
    ds = _make_daemon_state(ipc)
    ds._fetch_monitor_rect()
    assert ds.edge_scroll._monitor_w == 1920


def test_fetch_monitor_rect_error():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    ds = _make_daemon_state(ipc)
    ds._fetch_monitor_rect()  # should not raise


def test_handle_ipc_canvas_toggle():
    ds = _make_daemon_state()
    ds.handle_ipc("CANVAS_TOGGLE")
    ds.navigator.canvas_toggle.assert_called_once()


def test_handle_ipc_edge_start_no_cursor():
    ipc = MagicMock()
    ipc.send.return_value = '{"address":"0xabc","at":[100,200],"size":[500,300]}'
    ds = _make_daemon_state(ipc)
    with patch("canvas.daemon.get_cursor_pos", side_effect=ConnectionError("fail")):
        result = ds.handle_ipc("EDGE_START")
    assert result == "EDGE_NO_CURSOR"


def test_edge_scroll_move_ipc_error():
    ipc = MagicMock()
    ipc.eval_lua.side_effect = ConnectionError("fail")
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
    ds.edge_scroll_workspace = 1
    ds.edge_scroll_move(10, -5)  # should not raise


def test_move_windows_to_delta_no_baselines():
    """Without baselines (or workspace) the move must be a no-op."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.move_windows_to_delta(10, 20)
    ipc.eval_lua.assert_not_called()


def test_fetch_baselines_window_with_short_at():
    ipc = MagicMock()
    ipc.send.side_effect = [
        '{"id":1}',
        '[{"address":"0xabc","floating":true,"at":[10]}]',
    ]
    ds = _make_daemon_state(ipc)
    ds.fetch_baselines()
    assert ds.baselines == {}


def test_fetch_monitor_rect_empty_list():
    ipc = MagicMock()
    ipc.send.return_value = "[]"
    ds = _make_daemon_state(ipc)
    ds._fetch_monitor_rect()  # should not raise, defaults stay


# --- idle pan stop drops baselines ---


def test_idle_pan_stop_clears_baselines():
    """Idle timeout must drop baselines so shutdown cannot restore stale positions."""
    import time as time_mod

    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}
    ds.panning._last_move_time = time_mod.monotonic() - 1.0

    assert ds.handle_idle_pan_stop() is True
    assert ds.panning.pan_active is False
    assert ds.baselines == {}


def test_idle_pan_stop_noop_while_active():
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}

    assert ds.handle_idle_pan_stop() is False
    assert ds.panning.pan_active is True
    assert ds.baselines == {"0x1": (10, 20)}


def test_restore_skipped_after_idle_stop():
    """Full chain: idle stop → shutdown restore is a no-op (no window movement)."""
    import time as time_mod

    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.panning.start_pan()
    ds.baselines = {"0xabc": (100, 200)}
    ds.panning._last_move_time = time_mod.monotonic() - 1.0

    ds.handle_idle_pan_stop()
    ds.restore_baselines()

    ipc.eval_lua.assert_not_called()


def test_shutdown_restores_only_when_pan_still_active():
    """SIGTERM during active pan → windows restored; after PAN_STOP → untouched."""
    ipc = MagicMock()
    ds = _make_daemon_state(ipc)
    ds.panning.start_pan()
    ds.baselines = {"0xabc": (100, 200)}
    ds.baseline_workspace = 1

    ds.restore_baselines()  # pan active: baseline restore intended
    ipc.eval_lua.assert_called_once()

    ipc.reset_mock()
    ds.handle_ipc("PAN_STOP")
    ds.restore_baselines()
    ipc.eval_lua.assert_not_called()


# --- monitor rect fail-safe ---


def test_fetch_monitor_rect_returns_bool():
    ipc = MagicMock()
    ipc.send.return_value = '[{"focused":true,"x":0,"y":0,"width":2560,"height":1440}]'
    ds = _make_daemon_state(ipc)
    assert ds._fetch_monitor_rect() is True


def test_fetch_monitor_rect_error_returns_false():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    ds = _make_daemon_state(ipc)
    assert ds._fetch_monitor_rect() is False


def test_fetch_monitor_rect_empty_list_returns_false():
    ipc = MagicMock()
    ipc.send.return_value = "[]"
    ds = _make_daemon_state(ipc)
    assert ds._fetch_monitor_rect() is False


def test_fetch_monitor_rect_rejects_missing_dimensions():
    ipc = MagicMock()
    ipc.send.return_value = '[{"focused":true,"x":0,"y":0}]'
    ds = _make_daemon_state(ipc)
    assert ds._fetch_monitor_rect() is False


# --- mode exclusivity ---


def test_edge_start_cancels_active_pan():
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}

    ipc = MagicMock()
    ipc.send.side_effect = [
        '{"id":1}',
        '[{"address":"0xabc","floating":true,"at":[100,100],"size":[400,300],"workspace":{"id":1}}]',
        '{"address":"0xabc"}',
        '[{"address":"0xabc","floating":true,"at":[100,100],"size":[400,300],"workspace":{"id":1}}]',
        '[{"focused":true,"x":0,"y":0,"width":1920,"height":1080}]',
    ]
    ds.ipc = ipc
    with patch("canvas.daemon.get_cursor_pos", return_value=(300, 250)):
        result = ds.handle_ipc("EDGE_START")

    assert result == "EDGE_ON"
    assert ds.panning.pan_active is False
    assert ds.baselines == {}


def test_pan_start_cancels_active_edge_session():
    ds = _make_daemon_state()
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=0,
            win_y=0,
            win_w=500,
            win_h=300,
            cursor_x=250,
            cursor_y=150,
        )
    )
    assert ds.edge_scroll.active is True

    ipc = MagicMock()
    ipc.send.side_effect = ['{"id":1}', "[]"]
    ds.ipc = ipc
    assert ds.handle_ipc("PAN_START") == "PAN_ON"

    assert ds.edge_scroll.active is False
    assert ds.panning.pan_active is True


def test_pan_start_failure_does_not_activate_pan():
    ipc = MagicMock()
    ipc.send.side_effect = ConnectionError("fail")
    ds = _make_daemon_state(ipc)

    assert ds.handle_ipc("PAN_START") == "PAN_NO_BASELINE"
    assert ds.panning.pan_active is False
    assert ds.baselines == {}


def test_navigation_stops_competing_modes():
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}
    ds.baseline_workspace = 1
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

    assert ds.handle_ipc("NAV_RIGHT") == "OK"

    assert ds.panning.pan_active is False
    assert ds.baselines == {}
    assert ds.baseline_workspace is None
    assert ds.edge_scroll.active is False
    ds.navigator.navigate.assert_called_once_with("right")


def test_navigation_reports_compositor_failure():
    ds = _make_daemon_state()
    ds.navigator.navigate.return_value = False

    assert ds.handle_ipc("NAV_LEFT") == "ERROR:NAV_FAILED"


def test_center_cursor_centers_topmost_window_and_stops_competing_modes():
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}
    ds.baseline_workspace = 1
    ds.edge_scroll.start(
        EdgeScrollParams(
            dragged_addr="0xold",
            win_x=0,
            win_y=0,
            win_w=400,
            win_h=300,
            cursor_x=100,
            cursor_y=100,
        )
    )
    target = {
        "address": "0xabc",
        "class": "kitty",
        "floating": True,
        "at": [300, 200],
        "size": [400, 300],
        "workspace": {"id": 1},
    }
    ds.ipc.get_cursor_pos.return_value = (500, 350)
    ds.navigator.center_window.return_value = True

    with (
        patch.object(ds, "_get_active_workspace_id", return_value=1),
        patch.object(ds, "_find_window_at_cursor", return_value=target),
    ):
        assert ds.handle_ipc("CENTER_CURSOR") == "OK"

    assert ds.panning.is_dragging is False
    assert ds.baselines == {}
    assert ds.edge_scroll.active is False
    ds.navigator.center_window.assert_called_once_with(
        "0xabc", workspace_id=1, cursor_x=500, cursor_y=350
    )


def test_center_cursor_returns_no_window():
    ds = _make_daemon_state()
    ds.ipc.get_cursor_pos.return_value = (500, 350)

    with (
        patch.object(ds, "_get_active_workspace_id", return_value=1),
        patch.object(ds, "_find_window_at_cursor", return_value=None),
    ):
        assert ds.handle_ipc("CENTER_CURSOR") == "ERROR:NO_WINDOW"

    ds.navigator.center_window.assert_not_called()


def test_center_cursor_reports_client_query_failure():
    ds = _make_daemon_state()
    ds.ipc.get_cursor_pos.return_value = (500, 350)
    ds.ipc.send.side_effect = ConnectionError("fail")

    with patch.object(ds, "_get_active_workspace_id", return_value=1):
        assert ds.handle_ipc("CENTER_CURSOR") == "ERROR:QUERY_FAILED"

    ds.navigator.center_window.assert_not_called()


def test_center_cursor_returns_no_workspace():
    ds = _make_daemon_state()
    ds.ipc.get_cursor_pos.return_value = (500, 350)

    with patch.object(ds, "_get_active_workspace_id", return_value=None):
        assert ds.handle_ipc("CENTER_CURSOR") == "ERROR:NO_WORKSPACE"

    ds.navigator.center_window.assert_not_called()


def test_center_cursor_reports_cursor_and_compositor_failures():
    ds = _make_daemon_state()
    ds.ipc.get_cursor_pos.side_effect = ConnectionError("fail")
    assert ds.handle_ipc("CENTER_CURSOR") == "ERROR:NO_CURSOR"

    target = {
        "address": "0xabc",
        "class": "kitty",
        "floating": True,
        "at": [300, 200],
        "size": [400, 300],
        "workspace": {"id": 1},
    }
    ds.ipc.get_cursor_pos.side_effect = None
    ds.ipc.get_cursor_pos.return_value = (500, 350)
    ds.navigator.center_window.return_value = False
    with (
        patch.object(ds, "_get_active_workspace_id", return_value=1),
        patch.object(ds, "_find_window_at_cursor", return_value=target),
    ):
        assert ds.handle_ipc("CENTER_CURSOR") == "ERROR:CENTER_FAILED"


def test_handle_ipc_canvas_toggle_stops_competing_modes():
    """Canvas-toggle during pan/edge stops both so Lua outcome is deterministic."""
    ds = _make_daemon_state()
    ds.panning.start_pan()
    ds.baselines = {"0x1": (10, 20)}
    ds.baseline_workspace = 1
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

    ds.handle_ipc("CANVAS_TOGGLE")

    assert ds.panning.is_dragging is False
    assert ds.baselines == {}
    assert ds.baseline_workspace is None
    assert ds.edge_scroll.active is False
    ds.navigator.canvas_toggle.assert_called_once()


def test_handle_ipc_canvas_toggle_single_stops_pan():
    ds = _make_daemon_state()
    ds.panning.start_pan()

    ds.handle_ipc("CANVAS_TOGGLE_SINGLE")

    assert ds.panning.is_dragging is False
    ds.navigator.canvas_toggle_single.assert_called_once()
