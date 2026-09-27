"""Panning via Hyprland cursor position polling.

Architecture:
- Poll cursor position via Hyprland IPC (canvas.hypr module)
- When pan_active, accumulate total delta from cursor movement
- Daemon uses absolute positioning (baseline + total_delta) to avoid drift
- PAN_START/PAN_STOP controlled by Hyprland keybinds
- Edge-scroll: while a CONFIRMED window drag is in progress, the daemon
  polls the dragged window's real geometry and the camera assists as the
  window enters the monitor edge zone. No cursor-derived guessing: if the
  window does not actually move, nothing scrolls.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

from canvas import debug, hypr

log = logging.getLogger("canvas.panning")


@dataclass
class EdgeScrollParams:
    dragged_addr: str
    win_x: int
    win_y: int
    win_w: int
    win_h: int
    cursor_x: int
    cursor_y: int


class PanningState:
    """Thread-safe panning state with total delta tracking.

    Instead of consuming incremental deltas (which accumulate rounding
    errors), we track the total offset from the start position. The
    daemon uses this with baseline window positions for absolute moves,
    eliminating drift.
    """

    _IDLE_TIMEOUT = 0.5  # auto-stop after 500ms without cursor movement

    def __init__(self, speed: float = 1.0, max_speed: float | None = None):
        self.speed = speed
        self.max_speed = max_speed
        self._pan_active = False
        self._inverted = False
        self.poller_alive = True

        self._prev_x: int | None = None
        self._prev_y: int | None = None
        self._total_dx = 0.0
        self._total_dy = 0.0
        self._last_move_time: float = 0.0

        self._lock = threading.Lock()

    @property
    def pan_active(self) -> bool:
        with self._lock:
            return self._pan_active

    @property
    def inverted(self) -> bool:
        with self._lock:
            return self._inverted

    @inverted.setter
    def inverted(self, value: bool) -> None:
        with self._lock:
            self._inverted = value

    @property
    def is_dragging(self) -> bool:
        with self._lock:
            return self._pan_active

    def start_pan(self) -> str:
        """Activate panning mode."""
        with self._lock:
            self._pan_active = True
            self._prev_x = None
            self._prev_y = None
            self._total_dx = 0.0
            self._total_dy = 0.0
            self._last_move_time = time.monotonic()
            return "PAN_ON"

    def stop_pan(self) -> None:
        """Force-stop panning mode."""
        with self._lock:
            self._pan_active = False
            self._prev_x = None
            self._prev_y = None
            self._total_dx = 0.0
            self._total_dy = 0.0

    def update_cursor(self, x: int, y: int) -> None:
        """Feed cursor position. If panning, accumulate total delta."""
        with self._lock:
            if not self._pan_active:
                self._prev_x = x
                self._prev_y = y
                return

            if self._prev_x is not None and self._prev_y is not None:
                dx = x - self._prev_x
                dy = y - self._prev_y
                if dx != 0 or dy != 0:
                    sign = 1 if self._inverted else -1
                    step_x = dx * self.speed * sign
                    step_y = dy * self.speed * sign
                    if self.max_speed is not None:
                        step_x = max(-self.max_speed, min(self.max_speed, step_x))
                        step_y = max(-self.max_speed, min(self.max_speed, step_y))
                    self._total_dx += step_x
                    self._total_dy += step_y
                    self._last_move_time = time.monotonic()

            self._prev_x = x
            self._prev_y = y

    def check_idle_timeout(self) -> bool:
        """Auto-stop if cursor hasn't moved for _IDLE_TIMEOUT (release bind fallback)."""
        with self._lock:
            if self._pan_active and (time.monotonic() - self._last_move_time) > self._IDLE_TIMEOUT:
                self._pan_active = False
                self._prev_x = None
                self._prev_y = None
                self._total_dx = 0.0
                self._total_dy = 0.0
                debug.dbg("PAN_IDLE_TIMEOUT")
                return True
            return False

    def get_total_delta(self) -> tuple[int, int]:
        """Return total (dx, dy) offset from pan start. Does NOT reset.

        Each call returns the same value until more cursor movement
        happens. The daemon uses this with baseline positions for
        absolute moves, so drift cannot accumulate.
        """
        with self._lock:
            return int(round(self._total_dx)), int(round(self._total_dy))


class EdgeScrollState:
    """Thread-safe edge-scroll driven by REAL window geometry.

    The daemon polls the dragged window's actual position from Hyprland
    (j/activewindow — during an interactive drag the dragged window is
    focused) and feeds it via update_geometry(). Nothing is derived from
    cursor math: if the window does not really move, the camera never
    moves. This mirrors compositor-level implementations (driftwm, hevel)
    where edge pan engages only inside a confirmed move grab.

    Guards, in order:
    - address mismatch → drag lost → auto-stop
    - window displacement below grab_dead_zone → click, not a drag → no scroll
    - cursor outside the dragged window's rect → grab gone → auto-stop

    Camera assist is DIRECTION-AWARE: each monitor edge contributes only
    while the window moves toward it (or is held against it). Pulling the
    window away from an edge stops that side's assist immediately, so a
    window parked near a boundary never drags the camera behind your back.

    A session is armed per mouse press. It ends on release, on a lost focus
    change, and on an idle timeout. The pointer must stay on the dragged
    window only until the drag is confirmed: at a workarea edge the
    compositor clamps the window, so the pointer necessarily leaves the
    window rect while the drag is still entirely real, and treating that as
    a lost grab killed the camera for the rest of the press.
    """

    _IDLE_TIMEOUT = 0.5
    CURSOR_MARGIN = 8  # px of slack around the dragged window for the cursor check

    def __init__(
        self,
        ramp_distance: int = 50,
        speed: float = 20.0,
        max_speed: float | None = None,
        enabled: bool = True,
        grab_dead_zone: int = 5,
    ) -> None:
        self.ramp_distance = ramp_distance
        self.speed = speed
        self.max_speed = max_speed
        self.enabled = enabled
        self.grab_dead_zone = grab_dead_zone
        self._active = False
        self._dragged_addr: str = ""
        self._session = 0
        self._last_geo: tuple[int, int, int, int] | None = None
        self._monitor_x = 0
        self._monitor_y = 0
        self._monitor_w = 1920
        self._monitor_h = 1080
        self._grab_x = 0
        self._grab_y = 0
        self._prev_x: int | None = None
        self._prev_y: int | None = None
        self._confirmed_drag = False
        self._last_move_time: float = 0.0
        self._pending_dx = 0.0
        self._pending_dy = 0.0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def dragged_addr(self) -> str:
        with self._lock:
            return self._dragged_addr

    @property
    def session(self) -> int:
        with self._lock:
            return self._session

    @property
    def pending_preview(self) -> tuple[int, int]:
        """Rounded pending delta without consuming (for diagnostics)."""
        with self._lock:
            return int(round(self._pending_dx)), int(round(self._pending_dy))

    @property
    def last_geometry(self) -> tuple[int, int, int, int] | None:
        """Last real geometry fed via update_geometry (for diagnostics)."""
        with self._lock:
            return self._last_geo

    @property
    def confirmed_drag(self) -> bool:
        with self._lock:
            return self._confirmed_drag

    def set_monitor_rect(self, x: int, y: int, w: int, h: int) -> None:
        with self._lock:
            self._monitor_x = x
            self._monitor_y = y
            self._monitor_w = w
            self._monitor_h = h

    def start(self, params: EdgeScrollParams) -> str:
        """Activate edge-scroll, remembering the grab-time window position."""
        with self._lock:
            if not self.enabled:
                return "EDGE_DISABLED"
            self._active = True
            self._dragged_addr = params.dragged_addr
            self._session += 1
            self._grab_x = params.win_x
            self._grab_y = params.win_y
            self._prev_x = params.win_x
            self._prev_y = params.win_y
            self._confirmed_drag = False
            self._last_geo = None
            self._pending_dx = 0.0
            self._pending_dy = 0.0
            self._last_move_time = time.monotonic()
            return "EDGE_ON"

    def stop(self) -> str:
        """Deactivate edge-scroll."""
        with self._lock:
            self._active = False
            self._dragged_addr = ""
            self._confirmed_drag = False
            self._prev_x = None
            self._prev_y = None
            self._pending_dx = 0.0
            self._pending_dy = 0.0
            return "EDGE_OFF"

    def update_geometry(
        self, addr: str, x: int, y: int, w: int, h: int, cursor_x: int, cursor_y: int
    ) -> None:
        """Feed the dragged window's real geometry plus cursor position."""
        with self._lock:
            if not self._active or not self.enabled:
                return

            if addr != self._dragged_addr:
                # Focus left the grabbed window mid-press: no real drag.
                debug.dbg(
                    "EDGE_DISARM",
                    s=self._session,
                    reason="focus_mismatch",
                    grabbed=self._dragged_addr,
                    now=addr,
                )
                log.debug("edge-scroll lost dragged window %s (now %s)", self._dragged_addr, addr)
                self._active = False
                self._dragged_addr = ""
                self._confirmed_drag = False
                self._prev_x = None
                self._prev_y = None
                self._pending_dx = 0.0
                self._pending_dy = 0.0
                return

            # Until the drag is confirmed, the session lives only while the
            # pointer stays on the dragged window (with slack for borders):
            # cursor elsewhere means the press never became a drag, and the
            # camera must never chase a ghost.
            #
            # After confirmation the window's own motion is the ground truth.
            # The cursor check has to stop there, because at a workarea edge
            # the compositor clamps the window and the cursor necessarily
            # ends up outside it — and that is exactly the "held against the
            # edge" case the camera is meant to follow. The window following
            # the cursor is only impossible at a boundary, so nothing else is
            # lost by dropping the check. The focus check above stays.
            if not self._confirmed_drag:
                margin = self.CURSOR_MARGIN
                inside = (
                    x - margin <= cursor_x < x + w + margin
                    and y - margin <= cursor_y < y + h + margin
                )
                if not inside:
                    debug.dbg(
                        "EDGE_DISARM",
                        s=self._session,
                        reason="cursor_left",
                        geo=(x, y, w, h),
                        cursor=(cursor_x, cursor_y),
                    )
                    self._active = False
                    self._dragged_addr = ""
                    self._confirmed_drag = False
                    self._prev_x = None
                    self._prev_y = None
                    self._pending_dx = 0.0
                    self._pending_dy = 0.0
                    return

            self._last_geo = (x, y, w, h)

            # Click-vs-drag threshold (driftwm uses 5px): until the window
            # itself moved beyond it, this is still just a press.
            if not self._confirmed_drag:
                moved = max(abs(x - self._grab_x), abs(y - self._grab_y))
                if moved < self.grab_dead_zone:
                    return
                self._confirmed_drag = True
                debug.dbg(
                    "EDGE_CONFIRMED",
                    s=self._session,
                    moved=moved,
                    geo=(x, y, w, h),
                )

            mx, my = self._monitor_x, self._monitor_y
            mw, mh = self._monitor_w, self._monitor_h
            rd = self.ramp_distance

            # Window velocity since the previous tick: an edge only assists
            # while the window moves TOWARD it (or is held against it).
            win_dx = 0 if self._prev_x is None else x - self._prev_x
            win_dy = 0 if self._prev_y is None else y - self._prev_y

            dist_left = x - mx
            dist_right = (mx + mw) - (x + w)
            dist_top = y - my
            dist_bottom = (my + mh) - (y + h)

            before_x, before_y = self._pending_dx, self._pending_dy
            tick_dx = 0.0
            tick_dy = 0.0

            if dist_left < rd and win_dx <= 0:
                progress = min((rd - dist_left) / rd, 1.0)
                tick_dx -= self.speed * progress

            if dist_right < rd and win_dx >= 0:
                progress = min((rd - dist_right) / rd, 1.0)
                tick_dx += self.speed * progress

            if dist_top < rd and win_dy <= 0:
                progress = min((rd - dist_top) / rd, 1.0)
                tick_dy -= self.speed * progress

            if dist_bottom < rd and win_dy >= 0:
                progress = min((rd - dist_bottom) / rd, 1.0)
                tick_dy += self.speed * progress

            # Normalize the diagonal. The four branches above are independent, so
            # a corner accumulates two full-speed contributions and the camera
            # would pan √2 faster than a single edge. max_speed cannot catch it:
            # it clamps each axis separately and cannot see the magnitude. The
            # same fix driftwm applies in edge_pan_velocity.
            magnitude = math.hypot(tick_dx, tick_dy)
            if magnitude > self.speed > 0:
                scale = self.speed / magnitude
                tick_dx *= scale
                tick_dy *= scale

            # Replace, rather than accumulate, the pending camera delta. If the
            # drag reverses before the main loop consumes it, stale motion from
            # the previous direction must not survive.
            self._pending_dx = tick_dx
            self._pending_dy = tick_dy
            self._prev_x = x
            self._prev_y = y

            if self._pending_dx != before_x or self._pending_dy != before_y:
                self._last_move_time = time.monotonic()

    def consume_delta(self) -> tuple[int, int]:
        """Return and clear pending (dx, dy) for moving other windows.

        Returns inverted values: camera right → other windows move LEFT.
        Clamps by max_speed if set.
        """
        with self._lock:
            if not self._active:
                return 0, 0
            dx = self._pending_dx
            dy = self._pending_dy
            self._pending_dx = 0.0
            self._pending_dy = 0.0
            if self.max_speed is not None:
                dx = max(-self.max_speed, min(self.max_speed, dx))
                dy = max(-self.max_speed, min(self.max_speed, dy))
            return -int(round(dx)), -int(round(dy))

    def check_idle_timeout(self) -> bool:
        """Auto-stop if no real scroll activity for _IDLE_TIMEOUT."""
        with self._lock:
            if self._active and (time.monotonic() - self._last_move_time) > self._IDLE_TIMEOUT:
                debug.dbg("EDGE_DISARM", s=self._session, reason="idle_timeout")
                self._active = False
                self._dragged_addr = ""
                self._confirmed_drag = False
                self._prev_x = None
                self._prev_y = None
                self._pending_dx = 0.0
                self._pending_dy = 0.0
                return True
            return False


def cursor_poller(
    state: PanningState,
    edge_scroll: EdgeScrollState,
    stop_event: threading.Event,
) -> None:
    """Poll cursor at ~60Hz; while edge-scrolling also poll real window geometry."""
    err_count = 0
    last_tick_log = 0.0
    while not stop_event.is_set():
        try:
            x, y = hypr.get_cursor_pos()
            state.update_cursor(x, y)
            if edge_scroll.active:
                geo = hypr.get_active_window_geometry()
                if geo is not None:
                    edge_scroll.update_geometry(*geo, x, y)
                now = time.monotonic()
                if debug.enabled() and now - last_tick_log >= 0.1:
                    last_tick_log = now
                    debug.dbg(
                        "EDGE_SESSION_TICK",
                        s=edge_scroll.session,
                        addr=geo[0] if geo else None,
                        geo=edge_scroll.last_geometry,
                        cursor=(x, y),
                        confirmed=edge_scroll.confirmed_drag,
                        pending=edge_scroll.pending_preview,
                    )
            err_count = 0
        except Exception as e:
            err_count += 1
            if err_count <= 3:
                log.warning("cursor poll error #%d: %s", err_count, e)
            if err_count > 10:
                log.error("too many cursor poll failures, stopping poller")
                state.poller_alive = False
                stop_event.set()
                return
        stop_event.wait(0.016)
