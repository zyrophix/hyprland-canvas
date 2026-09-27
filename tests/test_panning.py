from canvas.panning import EdgeScrollParams, EdgeScrollState, PanningState


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
    expected = -int(round(20.0 * ((50 - 10) / 50)))  # progress 0.8
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


def test_corner_full_speed_both_axes():
    es = EdgeScrollState(ramp_distance=50, speed=20.0, grab_dead_zone=5)
    es.set_monitor_rect(0, 0, 1920, 1080)
    _start_edge(es, 700, 400)
    es.update_geometry("0xabc", 750, 450, 500, 300, 750 + 250, 450 + 150)

    es.update_geometry(
        "0xabc", 1700, 850, 500, 300, 1700 + 250, 850 + 150
    )  # right+bottom far past
    dx, dy = es.consume_delta()
    assert dx == -20
    assert dy == -20


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
