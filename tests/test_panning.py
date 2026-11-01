import logging
import math
import time
from unittest.mock import patch

from canvas.panning import EdgeScrollParams, EdgeScrollState, PanningState, cursor_poller


def test_start_pan_activates():
    state = PanningState(speed=1.0)
    result = state.start_pan()
    assert state.pan_active is True
    assert result == "PAN_ON"


def test_stop_pan_deactivates():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.stop_pan()
    assert state.pan_active is False


def test_cursor_delta_accumulates_when_panning():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx, dy = state.get_total_delta()
    assert dx == -10
    assert dy == -5


def test_cursor_delta_applies_speed():
    state = PanningState(speed=2.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx, dy = state.get_total_delta()
    assert dx == -20
    assert dy == -10


def test_cursor_delta_applies_invert():
    state = PanningState(speed=1.0)
    state.inverted = True
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx, dy = state.get_total_delta()
    assert dx == 10
    assert dy == 5


def test_no_delta_when_not_panning():
    state = PanningState(speed=1.0)
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx, dy = state.get_total_delta()
    assert dx == 0
    assert dy == 0


def test_total_delta_is_running_total():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx1, dy1 = state.get_total_delta()
    assert dx1 == -10
    assert dy1 == -5
    state.update_cursor(120, 110)
    dx2, dy2 = state.get_total_delta()
    assert dx2 == -20
    assert dy2 == -10


def test_get_total_delta_does_not_reset():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    dx1, dy1 = state.get_total_delta()
    dx2, dy2 = state.get_total_delta()
    assert dx1 == dx2
    assert dy1 == dy2


def test_total_delta_accumulates_multiple_moves():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 100)
    state.update_cursor(100, 100)
    dx, dy = state.get_total_delta()
    assert dx == 0
    assert dy == 0


def test_stop_pan_clears_total():
    state = PanningState(speed=1.0)
    state.start_pan()
    state.update_cursor(100, 100)
    state.update_cursor(110, 105)
    state.stop_pan()
    dx, dy = state.get_total_delta()
    assert dx == 0
    assert dy == 0


def test_is_dragging_property():
    state = PanningState(speed=1.0)
    assert state.is_dragging is False
    state.start_pan()
    assert state.is_dragging is True
    state.stop_pan()
    assert state.is_dragging is False


def test_max_speed_clamps_step():
    state = PanningState(speed=1.0, max_speed=10)
    state.start_pan()
    state.update_cursor(0, 0)
    state.update_cursor(100, 100)
    dx, dy = state.get_total_delta()
    assert dx == -10
    assert dy == -10


def test_max_speed_none_no_clamp():
    state = PanningState(speed=1.0, max_speed=None)
    state.start_pan()
    state.update_cursor(0, 0)
    state.update_cursor(100, 100)
    dx, dy = state.get_total_delta()
    assert dx == -100
    assert dy == -100


def test_poller_alive_initially_true():
    state = PanningState(speed=1.0)
    assert state.poller_alive is True


# --- EdgeScrollState: ground-truth geometry model ---
# Monitor 1920x1080 at (0,0). Window 500x300. Dead zone 5px default.


def _start_edge(
    es: EdgeScrollState,
    win_x: int,
    win_y: int,
    cursor_x: int | None = None,
    cursor_y: int | None = None,
) -> str:
    return es.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=win_x,
            win_y=win_y,
            win_w=500,
            win_h=300,
            cursor_x=cursor_x if cursor_x is not None else win_x + 250,
            cursor_y=cursor_y if cursor_y is not None else win_y + 150,
        )
    )


def test_edge_scroll_inactive_by_default():
    es = EdgeScrollState()
    assert es.active is False
    assert es.confirmed_drag is False


def test_edge_scroll_start():
    es = EdgeScrollState(enabled=True)
    assert _start_edge(es, 700, 390) == "EDGE_ON"
    assert es.active is True
    assert es.dragged_addr == "0xabc"
    assert es.confirmed_drag is False


def test_edge_scroll_start_disabled():
    es = EdgeScrollState(enabled=False)
    result = es.start(
        EdgeScrollParams(
            dragged_addr="0xabc",
            win_x=100,
            win_y=100,
            win_w=500,
            win_h=300,
            cursor_x=350,
            cursor_y=250,
        )
    )
    assert result == "EDGE_DISABLED"
    assert es.active is False


def test_edge_scroll_stop():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)
    assert es.stop() == "EDGE_OFF"
    assert es.active is False
    assert es.dragged_addr == ""


def test_no_scroll_until_window_really_moves():
    """Regression for phantom camera: mouse wanders after press but the
    window geometry never changes → strictly zero scroll."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1450, 390)  # right edge exactly at monitor edge

    # any amount of cursor movement is irrelevant now — only real geometry feeds
    es.update_geometry("0xabc", 1450, 390, 500, 300, 1450 + 250, 390 + 150)  # same position
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)


def test_no_scroll_below_dead_zone():
    """Sub-dead-zone jitter of the window itself must not scroll."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1450, 390)

    # moved 2px < 5px dead zone
    es.update_geometry("0xabc", 1452, 391, 500, 300, 1452 + 250, 391 + 150)
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)
    assert es.confirmed_drag is False


def test_confirmed_after_dead_zone_then_proximity_applies():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1400, 390)  # right edge 20px inside

    # drag 30px right: window now crosses edge by 10px, confirmed drag
    es.update_geometry("0xabc", 1430, 390, 500, 300, 1430 + 250, 390 + 150)
    assert es.confirmed_drag is True
    dx, dy = es.consume_delta()
    assert dx < 0  # camera right → other windows left
    assert dy == 0


def test_approach_within_ramp_gives_proportional_speed():
    """Window edge inside ramp zone but not past boundary already assists."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    # grab far from edge so first update only confirms the drag
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 950 + 250, 390 + 150)  # confirm (>5px move)

    # now push window so its right edge is 10px from boundary (dist=10 < ramp=50)
    es.update_geometry("0xabc", 1410, 390, 500, 300, 1410 + 250, 390 + 150)
    dx, _ = es.consume_delta()
    expected = -round(20.0 * ((50 - 10) / 50))  # progress 0.8
    assert dx == expected


def test_past_edge_full_speed():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 950 + 250, 390 + 150)  # confirm

    es.update_geometry(
        "0xabc", 1600, 390, 500, 300, 1600 + 250, 390 + 150
    )  # right edge 180px past
    dx, _ = es.consume_delta()
    assert dx == -20  # clamped to full speed


def test_left_edge_assists_rightward_camera():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 600, 390)
    es.update_geometry("0xabc", 650, 390, 500, 300, 650 + 250, 390 + 150)  # confirm

    es.update_geometry("0xabc", -30, 390, 500, 300, -30 + 250, 390 + 150)  # left edge 30px past
    dx, _ = es.consume_delta()
    assert dx > 0  # camera left → other windows right


def test_top_bottom_edges():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 710, 400)
    es.update_geometry("0xabc", 760, 450, 500, 300, 760 + 250, 450 + 150)  # confirm

    es.update_geometry("0xabc", 760, -40, 500, 300, 760 + 250, -40 + 150)  # top edge 40px past
    _, dy_top = es.consume_delta()

    es.update_geometry("0xabc", 760, 830, 500, 300, 760 + 250, 830 + 150)  # bottom edge 50px past
    _, dy_bot = es.consume_delta()

    assert dy_top > 0  # camera up → windows down
    assert dy_bot < 0  # camera down → windows up


def test_corner_does_not_pan_faster_than_single_edge():
    """A corner adds two full-speed contributions, so the vector must be
    normalized back to `speed`.

    Without this the camera moves √2 faster diagonally, and `max_speed` cannot
    catch it: it clamps each axis on its own and never sees the magnitude.
    """
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 700, 400)
    es.update_geometry("0xabc", 750, 450, 500, 300, 750 + 250, 450 + 150)

    es.update_geometry(
        "0xabc", 1700, 850, 500, 300, 1700 + 250, 850 + 150
    )  # right+bottom far past
    dx, dy = es.consume_delta()

    expected = round(20.0 / (2**0.5))  # 20 spread over two axes
    assert abs(dx) == expected
    assert abs(dy) == expected
    assert math.hypot(dx, dy) <= 20  # magnitude capped, not each axis


def test_single_edge_still_reaches_full_speed():
    """Normalization must not slow down a plain cardinal push."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 700, 400)
    es.update_geometry("0xabc", 750, 450, 500, 300, 750 + 250, 450 + 150)

    es.update_geometry("0xabc", 1900, 450, 500, 300, 1900 + 250, 450)
    dx, dy = es.consume_delta()

    assert abs(dx) == 20
    assert dy == 0


def test_address_mismatch_stops_session():
    """Focus left the grabbed window mid-press → session disarms itself."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1450, 390)

    es.update_geometry("0xother", 1430, 390, 500, 300, 1430 + 250, 390 + 150)

    assert es.active is False
    assert es.dragged_addr == ""
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)


def test_window_inside_screen_no_scroll_when_moved():
    """Confirmed drag but window stays well inside → no assist anywhere."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 600, 300)
    # confirmed, edges far from bounds
    es.update_geometry("0xabc", 640, 340, 500, 300, 640 + 250, 340 + 150)
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)


def test_max_speed_clamps_pending():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, max_speed=5.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 950 + 250, 390 + 150)
    es.update_geometry("0xabc", 1700, 390, 500, 300, 1700 + 250, 390 + 150)
    dx, _ = es.consume_delta()
    assert abs(dx) <= 5


def test_idle_timeout_fires_on_still_grab():
    """Press-and-hold without any movement self-cleans via idle timeout."""
    import time as time_mod

    es = EdgeScrollState(ramp_distance=50, speed=20.0)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1450, 390)
    es._last_move_time = time_mod.monotonic() - 1.0

    assert es.check_idle_timeout() is True
    assert es.active is False


# --- cursor-inside invariant ---


def test_cursor_leaving_window_disarms_session():
    """Before the drag is confirmed, the pointer must stay on the window.

    This is the ghost guard. Once a drag is confirmed the check no longer
    applies: at a workarea edge the compositor clamps the window, so the cursor
    necessarily leaves the window rect while the drag is perfectly real — see
    test_confirmed_drag_survives_cursor_outrunning_a_clamped_window.
    """
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 600, 300)

    # window never moves past the dead zone, cursor walks off it
    es.update_geometry("0xabc", 640, 340, 500, 300, 1800, 900)
    assert es.active is False
    assert es.dragged_addr == ""
    assert es.confirmed_drag is False
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)


def test_cursor_inside_with_margin_keeps_session():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1400, 390)

    # cursor slightly outside the rect but within CURSOR_MARGIN (8px)
    es.update_geometry("0xabc", 1435, 390, 500, 300, 1935 + 4, 540)  # 1939 vs edge 1935+8
    # window right edge 1935 past monitor; cursor at 1939 is inside margin zone → still armed
    assert es.active is True


def test_cursor_far_outside_before_confirmation_disarms():
    """Press armed but the very first geometry tick shows cursor elsewhere."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 600, 300)

    es.update_geometry("0xabc", 600, 300, 500, 300, 50, 50)
    assert es.active is False


# --- direction gating ---


def test_pull_away_from_edge_stops_assist():
    """Regression: a window parked at the right edge, dragged LEFT (inward),
    must not push the camera right."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 1450, 390)  # right edge exactly at boundary

    # drag inward: dx negative → right edge must not assist
    es.update_geometry(
        "0xabc",
        1400,
        390,
        500,
        300,
        1650,
        540,
    )
    dx, dy = es.consume_delta()
    assert (dx, dy) == (0, 0)

    es.update_geometry("0xabc", 1300, 390, 500, 300, 1550, 540)  # still moving left
    assert es.consume_delta() == (0, 0)


def test_hold_at_edge_continues_assist():
    """Holding the window against the edge (no movement) keeps panning."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 1200, 540)  # confirm

    es.update_geometry("0xabc", 1700, 390, 500, 300, 1950, 540)  # push past edge
    first = es.consume_delta()
    assert first[0] != 0

    # hold: identical geometry → win_dx == 0 → assist continues
    second = None
    for _ in range(3):
        es.update_geometry("0xabc", 1700, 390, 500, 300, 1950, 540)
        d = es.consume_delta()
        assert d[0] != 0
        second = d

    assert first == second  # full speed sustained while held


def test_drag_toward_edge_then_back_stops():
    """Toward edge → assist; reverse mid-drag → assist stops that side."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 1200, 540)

    es.update_geometry("0xabc", 1500, 390, 500, 300, 1750, 540)  # toward right
    assert es.consume_delta()[0] != 0

    es.update_geometry("0xabc", 1480, 390, 500, 300, 1730, 540)  # reversed
    assert es.consume_delta() == (0, 0)


def test_direction_reversal_discards_unconsumed_old_sign():
    """A reversal before the main-loop consume must not apply stale motion."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 900, 390)
    es.update_geometry("0xabc", 950, 390, 500, 300, 1200, 540)

    es.update_geometry("0xabc", 1500, 390, 500, 300, 1750, 540)
    assert es.pending_preview[0] > 0

    es.update_geometry("0xabc", 1480, 390, 500, 300, 1730, 540)
    assert es.consume_delta() == (0, 0)


def test_vertical_direction_gating():
    """Bottom-edge assist only while dy >= 0; pulling up stops it."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 710, 400)
    es.update_geometry("0xabc", 760, 450, 500, 300, 1010, 600)  # confirm

    es.update_geometry("0xabc", 760, 840, 500, 300, 1010, 990)  # down toward bottom
    _, dy_down = es.consume_delta()
    assert dy_down < 0

    es.update_geometry("0xabc", 760, 800, 500, 300, 1010, 950)  # pulled up
    assert es.consume_delta() == (0, 0)


def test_first_tick_after_start_treated_as_stationary():
    """First update with unchanged position (dx=dy=0) may still assist —
    matches grab-time snapshot semantics."""
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    # window ALREADY past the right edge at grab time; confirm by moving cursor
    # is impossible without window motion, so this only assists after real motion
    _start_edge(es, 1800, 390)  # right edge 380px past monitor
    es.update_geometry("0xabc", 1800, 390, 500, 300, 2050, 540)  # not confirmed yet
    assert es.consume_delta() == (0, 0)


# --- cursor outruns the window at a clamped edge ---------------------------
#
# The compositor clamps a dragged window to the workarea, so at a boundary the
# cursor necessarily ends up outside the window rect. Disarming there killed
# the camera for the rest of the press, and edge-start only fires on press, so
# the user had to release and grab the window again.


def _edge(**kwargs):
    params = {
        "ramp_distance": 50,
        "speed": 20.0,
        "enabled": True,
        "grab_dead_zone": 5,
    }
    params.update(kwargs)
    return EdgeScrollState(**params)


def _arm(es, x=500, y=300, w=935, h=493, cursor=(600, 400)):
    es.set_monitor_rect(0, 0, 1920, 1080)
    return es.start(
        EdgeScrollParams(
            dragged_addr="0x1",
            win_x=x,
            win_y=y,
            win_w=w,
            win_h=h,
            cursor_x=cursor[0],
            cursor_y=cursor[1],
        )
    )


def test_confirmed_drag_survives_cursor_outrunning_a_clamped_window():
    """The reported bug: pushing past a workarea edge killed the camera."""
    es = _edge()
    _arm(es)
    workarea_right, w, h = 1876, 935, 493
    x0, y0 = 500, 300
    cx, cy = 600, 400

    for step in range(1, 200):
        cx = min(cx + 20, 1920)
        # the compositor clamps the window to the workarea
        nx = min(x0 + step * 20, workarea_right - w)
        es.update_geometry("0x1", nx, y0, w, h, cx, cy)
        assert es.active, f"session died at step {step}: cursor_x={cx} window_x={nx}"

    assert es.confirmed_drag
    assert es.pending_preview[0] > 0, "camera should still be scrolling right"


def test_ghost_press_still_disarms_when_cursor_leaves_before_any_drag():
    """A press that never became a drag must not leave the camera chasing."""
    es = _edge()
    _arm(es)

    # window never moves, cursor walks far away
    es.update_geometry("0x1", 500, 300, 935, 493, 1800, 400)

    assert not es.active
    assert not es.confirmed_drag


def test_ghost_press_across_the_other_axis_still_disarms():
    es = _edge()
    _arm(es)

    es.update_geometry("0x1", 500, 300, 935, 493, 600, 900)

    assert not es.active


def test_cursor_check_still_applies_on_the_confirming_frame():
    """Confirmation must not sneak past the ghost check in the same tick."""
    es = _edge()
    _arm(es)

    # window moved past the dead zone, but the cursor is elsewhere
    es.update_geometry("0x1", 520, 300, 935, 493, 1500, 400)

    assert not es.active
    assert not es.confirmed_drag


def test_focus_mismatch_disarms_even_after_confirmation():
    """The remaining ghost guard stays active for a confirmed drag."""
    es = _edge()
    _arm(es)

    es.update_geometry("0x1", 600, 300, 935, 493, 700, 400)
    assert es.confirmed_drag

    es.update_geometry("0x2", 600, 300, 935, 493, 700, 400)
    assert not es.active


def test_drag_away_from_an_edge_stops_that_side():
    """Direction awareness is unaffected: pulling back must not keep scrolling."""
    es = _edge()
    _arm(es)

    # push right to the edge and confirm
    for i in range(1, 6):
        es.update_geometry("0x1", 500 + i * 20, 300, 935, 493, 600 + i * 20, 400)
    assert es.confirmed_drag

    # now walk the window back left, away from the right edge
    for i in range(6, 30):
        nx = 500 + (30 - i) * 20
        es.update_geometry("0x1", nx, 300, 935, 493, 600, 400)
        assert es.active, f"session died while dragging back at step {i}"

    assert es.pending_preview[0] == 0


# --- cursor_poller ----------------------------------------------------------
#
# The poller has no exit of its own besides the stop event, so the mocked cursor
# query sets it once it has been asked for every position. That keeps the test
# single-threaded: no thread to join, no sleep to race against.


class _StopFlag:
    """A stop event that never sleeps.

    `cursor_poller` calls `wait(0.016)` on every iteration, so a real Event puts
    16 ms of actual sleep into each test. This also counts the calls, which is
    the only way to notice if the throttle is ever dropped.
    """

    def __init__(self) -> None:
        self._set = False
        self.waits = 0

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, _timeout: float) -> bool:
        self.waits += 1
        return self._set


def _run_poller(
    state: PanningState,
    edge_scroll: EdgeScrollState,
    positions: list[tuple[int, int]],
) -> tuple[int, _StopFlag]:
    stop = _StopFlag()
    calls = {"n": 0}

    def cursor() -> tuple[int, int]:
        index = calls["n"]
        calls["n"] += 1
        if calls["n"] >= len(positions):
            stop.set()
        return positions[index]

    with patch("canvas.panning.hypr.get_cursor_pos", side_effect=cursor):
        cursor_poller(state, edge_scroll, stop)
    return calls["n"], stop


def _run_failing(state: PanningState) -> tuple[int, _StopFlag]:
    stop = _StopFlag()
    calls = {"n": 0}

    def boom() -> tuple[int, int]:
        calls["n"] += 1
        raise RuntimeError("nope")

    with patch("canvas.panning.hypr.get_cursor_pos", side_effect=boom):
        cursor_poller(state, EdgeScrollState(), stop)
    return calls["n"], stop


def test_cursor_poller_does_nothing_when_already_stopped():
    stop = _StopFlag()
    stop.set()

    with patch("canvas.panning.hypr.get_cursor_pos") as cursor:
        cursor_poller(PanningState(), EdgeScrollState(), stop)

    cursor.assert_not_called()
    assert stop.waits == 0


def test_cursor_poller_feeds_the_cursor_and_leaves_geometry_alone():
    state = PanningState(speed=1.0)
    state.start_pan()

    with patch("canvas.panning.hypr.get_active_window_geometry") as geometry:
        attempts, stop = _run_poller(state, EdgeScrollState(enabled=True), [(10, 10), (20, 30)])

    assert attempts == 2
    # Edge-scroll is not active, so the extra round trip must not happen.
    geometry.assert_not_called()
    assert state.get_total_delta() == (-10, -20)
    # And the loop paces itself once per iteration.
    assert stop.waits == 2


def test_cursor_poller_feeds_window_geometry_while_edge_scrolling():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)
    geometry = ("0xabc", 700, 390, 500, 300)

    with (
        patch("canvas.panning.debug.enabled", return_value=False),
        patch("canvas.panning.hypr.get_active_window_geometry", return_value=geometry),
    ):
        _run_poller(PanningState(), es, [(900, 500)])

    assert es.last_geometry == (700, 390, 500, 300)


def test_cursor_poller_tolerates_a_missing_window_geometry():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)

    with (
        patch("canvas.panning.debug.enabled", return_value=False),
        patch("canvas.panning.hypr.get_active_window_geometry", return_value=None),
        patch.object(es, "update_geometry") as update,
    ):
        _run_poller(PanningState(), es, [(900, 500)])

    update.assert_not_called()


def test_cursor_poller_logs_a_tick_at_most_every_tenth_of_a_second():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)
    state = PanningState()
    geometry = ("0xabc", 1, 2, 3, 4)
    # One reading per iteration: 0.0 is too soon after the 0.0 start, 0.05 too
    # soon after that, 0.2 and 0.5 are far enough apart to pass the gate.
    readings: list[float] = [0.0, 0.05, 0.2, 0.5]
    asked: list[float] = []

    def monotonic() -> float:
        asked.append(1.0)
        return readings[len(asked) - 1] if len(asked) <= len(readings) else 0.0

    with (
        patch("canvas.panning.debug.enabled", return_value=True),
        patch("canvas.panning.debug.dbg") as dbg,
        patch("canvas.panning.hypr.get_active_window_geometry", return_value=geometry),
        patch("canvas.panning.time.monotonic", side_effect=monotonic),
        patch.object(state, "update_cursor"),
        patch.object(es, "update_geometry"),
    ):
        _run_poller(state, es, [(1, 1), (2, 2), (3, 3), (4, 4)])

    # Exactly one clock reading per iteration, so the list above lines up.
    assert len(asked) == 4
    ticks = [c for c in dbg.call_args_list if c.args and c.args[0] == "EDGE_SESSION_TICK"]
    assert len(ticks) == 2
    assert [c.kwargs["cursor"] for c in ticks] == [(3, 3), (4, 4)]
    assert [c.kwargs["addr"] for c in ticks] == ["0xabc", "0xabc"]
    assert ticks[0].kwargs["geo"] is None
    assert ticks[0].kwargs["confirmed"] is False


def test_cursor_poller_does_not_log_ticks_with_tracing_off():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)

    with (
        patch("canvas.panning.debug.enabled", return_value=False),
        patch("canvas.panning.debug.dbg") as dbg,
        patch("canvas.panning.hypr.get_active_window_geometry", return_value=None),
    ):
        _run_poller(PanningState(), es, [(1, 1), (2, 2)])

    dbg.assert_not_called()


def test_cursor_poller_gives_up_after_ten_consecutive_failures():
    state = PanningState()

    attempts, stop = _run_failing(state)

    assert attempts == 11
    assert state.poller_alive is False
    assert stop.is_set()
    # Ten failures wait between them; the eleventh returns without waiting.
    assert stop.waits == 10


def test_cursor_poller_warns_three_times_and_then_only_errors(caplog):
    with caplog.at_level(logging.WARNING, logger="canvas.panning"):
        _run_failing(PanningState())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 3
    assert len(errors) == 1


def test_cursor_poller_resets_the_error_count_after_a_success():
    """Ten failures give up, but they have to be consecutive."""
    state = PanningState()
    stop = _StopFlag()
    script: list[object] = ["boom"] * 5 + [(0, 0)] + ["boom"] * 6
    calls = {"n": 0}

    def draw() -> tuple[int, int]:
        index = calls["n"]
        calls["n"] += 1
        if calls["n"] >= len(script):
            stop.set()
        if script[index] == "boom":
            raise RuntimeError("nope")
        return (0, 0)

    with patch("canvas.panning.hypr.get_cursor_pos", side_effect=draw):
        cursor_poller(state, EdgeScrollState(), stop)

    # Eleven failures overall, but never ten in a row.
    assert calls["n"] == len(script)
    assert state.poller_alive is True


def test_update_geometry_ignores_an_inactive_session():
    es = EdgeScrollState(enabled=True)

    es.update_geometry("0xabc", 700, 390, 500, 300, 900, 500)

    assert es.last_geometry is None


def test_update_geometry_ignores_a_session_disabled_mid_drag():
    es = EdgeScrollState(enabled=True)
    _start_edge(es, 700, 390)
    es.enabled = False

    es.update_geometry("0xabc", 700, 390, 500, 300, 900, 500)

    assert es.last_geometry is None


def test_edge_scroll_idle_timeout_is_a_no_op_when_inactive():
    assert EdgeScrollState(enabled=True).check_idle_timeout() is False


# --- idle timeout ------------------------------------------------------------


def test_edge_timeout_resets_on_drag_activity_without_camera_motion():
    """A drag that has not reached an edge is still a drag. The camera's pending
    delta is not the activity signal: this window sits in the middle of the
    monitor, nothing scrolls, and the session has to survive anyway."""
    es = EdgeScrollState(enabled=True, idle_timeout=0.5)
    _start_edge(es, 700, 390)
    es.update_geometry("0xabc", 700, 390, 500, 300, 950, 540)
    es._last_move_time = time.monotonic() - 1.0

    es.update_geometry("0xabc", 706, 396, 500, 300, 956, 546)

    assert es.check_idle_timeout() is False


def test_edge_timeout_resets_when_only_the_cursor_moves():
    es = EdgeScrollState(enabled=True, idle_timeout=0.5)
    _start_edge(es, 700, 390)
    es.update_geometry("0xabc", 700, 390, 500, 300, 950, 540)
    es._last_move_time = time.monotonic() - 1.0

    es.update_geometry("0xabc", 700, 390, 500, 300, 951, 541)

    assert es.check_idle_timeout() is False


def test_edge_timeout_fires_when_nothing_moves():
    es = EdgeScrollState(enabled=True, idle_timeout=0.5)
    _start_edge(es, 700, 390)
    es.update_geometry("0xabc", 700, 390, 500, 300, 950, 540)
    es._last_move_time = time.monotonic() - 1.0

    es.update_geometry("0xabc", 700, 390, 500, 300, 950, 540)

    assert es.check_idle_timeout() is True
