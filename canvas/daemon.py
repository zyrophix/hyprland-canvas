"""Canvas daemon — main loop wiring all modules together."""

import json
import logging
import re
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from canvas import debug
from canvas.config import ConfigError, load, resolve_path
from canvas.hypr import LUA_DISPATCH_HELPER, HyprIPC, get_cursor_pos
from canvas.ipc import IpcServer, acquire_singleton
from canvas.navigation import Navigator
from canvas.panning import EdgeScrollParams, EdgeScrollState, PanningState, cursor_poller

log = logging.getLogger("canvas")

_VALID_ADDR = re.compile(r"^0x[0-9a-fA-F]+$")


def _lua_escape(s: str) -> str:
    """Escape a string for safe interpolation into a Lua double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


class DaemonState:
    """Encapsulates daemon runtime state — replaces closure + nonlocal.

    Makes state testable via dependency injection (mock HyprIPC, etc).
    """

    def __init__(
        self,
        panning: PanningState,
        edge_scroll: EdgeScrollState,
        navigator: Navigator,
        ipc: HyprIPC,
    ) -> None:
        self.panning = panning
        self.edge_scroll = edge_scroll
        self.navigator = navigator
        self.ipc = ipc
        self.baselines: dict[str, tuple[int, int]] = {}
        # Workspace whose windows the baselines belong to; pan and
        # edge-scroll moves are scoped to it so other workspaces'
        # floating layouts stay untouched.
        self.baseline_workspace: int | None = None
        self.edge_scroll_workspace: int | None = None
        # Windows kept out of edge-scroll pan, resolved once at EDGE_START.
        self.edge_scroll_excluded: set[str] = set()
        # Workspace already reported as panned, so a long pan records once
        # instead of on every frame.
        # Serializes compositor mutations from the IPC thread with main-loop
        # pan/edge moves. State objects retain their own fine-grained locks.
        # Addresses to leave alone when panning, from window_pan_excludes.
        self._pan_exclude_apps: list[str] = []
        self._operation_lock = threading.RLock()

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Push config values onto the live state objects.

        Single source of truth for the config-to-state mapping: runs once at
        startup and again on every `canvas-ctl reload`. Two copies of this
        mapping would be the classic way for a reloaded key to quietly mean
        something different from the same key at boot.

        `cfg` must be the merged mapping `config.load` returns, so every key
        below is present. That is why there are no `.get` fallbacks here:
        restating a default here would be a second copy of `DEFAULT_CONFIG`,
        and the copy that drifts is the one nobody edits.

        Every key lands on a plain attribute, so a reload takes effect on the
        next frame without rebuilding anything. Takes the operation lock
        itself: it is an RLock, so calling this while already holding it — as
        the IPC handler does — costs nothing.
        """
        with self._operation_lock:
            self.panning.speed = cfg["speed"]
            self.panning.max_speed = cfg["max_speed"]
            self.panning.inverted = cfg["invert"]["enabled"]

            edge_cfg = cfg["edge_scroll"]
            self.edge_scroll.ramp_distance = edge_cfg["ramp_distance"]
            self.edge_scroll.speed = edge_cfg["speed"]
            self.edge_scroll.max_speed = edge_cfg["max_speed"]
            self.edge_scroll.enabled = edge_cfg["enabled"]
            self.edge_scroll.grab_dead_zone = edge_cfg["grab_dead_zone"]

            nav_cfg = cfg["navigation"]
            self.navigator._protected_apps = [a.lower() for a in nav_cfg["protected_apps"]]
            self.navigator._cooldown = nav_cfg["cooldown"]
            self._pan_exclude_apps = [a.lower() for a in cfg["window_pan_excludes"]]

            canvas_cfg = cfg["canvas"]
            self.navigator._preserve_geometry = canvas_cfg["preserve_geometry"]
            self.navigator._auto_float = canvas_cfg["auto_float"]
            self.navigator._spawn_cfg = canvas_cfg["spawn"]

    def _fetch_monitor_rect(self) -> bool:
        """Fetch focused monitor geometry for edge-scroll. True on success."""
        try:
            resp = self.ipc.send("j/monitors")
            monitors: list[dict[str, Any]] = json.loads(resp)
            first_valid: tuple[int, int, int, int] | None = None
            for monitor in monitors:
                try:
                    x = int(monitor["x"])
                    y = int(monitor["y"])
                    width = int(monitor["width"])
                    height = int(monitor["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                if width <= 0 or height <= 0:
                    continue
                rect = (x, y, width, height)
                if first_valid is None:
                    first_valid = rect
                if monitor.get("focused", False):
                    self.edge_scroll.set_monitor_rect(*rect)
                    return True
            if first_valid is not None:
                self.edge_scroll.set_monitor_rect(*first_valid)
                return True
        except Exception as e:
            log.debug("fetch monitor rect failed: %s", e)
        return False

    def _get_active_workspace_id(self) -> int | None:
        """Active workspace id, or None when the query fails."""
        try:
            resp = self.ipc.send("j/activeworkspace")
            ws: dict[str, Any] = json.loads(resp)
            return int(ws["id"])
        except Exception as e:
            log.debug("get active workspace failed: %s", e)
            return None

    _IPC_DISPATCH: dict[str, str] = {
        "PAN_START": "_handle_pan_start",
        "PAN_STOP": "_handle_pan_stop",
        "NAV_LEFT": "_handle_nav_left",
        "NAV_RIGHT": "_handle_nav_right",
        "NAV_UP": "_handle_nav_up",
        "NAV_DOWN": "_handle_nav_down",
        "CENTER_CURSOR": "_handle_center_cursor",
        "EDGE_START": "_handle_edge_start",
        "EDGE_STOP": "_handle_edge_stop",
        "TOGGLE": "_handle_toggle",
        "CANVAS_TOGGLE": "_handle_canvas_toggle",
        "CANVAS_TOGGLE_ALL": "_handle_canvas_toggle_all",
        "CANVAS_TOGGLE_SINGLE": "_handle_canvas_toggle_single",
        "PING": "_handle_ping",
        "STATUS": "_handle_status",
        "RELOAD": "_handle_reload",
    }

    def handle_ipc(self, cmd: str) -> str:
        """Process an IPC command, return response string."""
        with self._operation_lock:
            handler_name = self._IPC_DISPATCH.get(cmd)
            if handler_name is not None:
                handler: Callable[[], str] = getattr(self, handler_name)
                result = handler()
                debug.dbg("CMD", cmd=cmd, result=result)
                return result
            debug.dbg("CMD", cmd=cmd, result="UNKNOWN")
            return f"UNKNOWN: {cmd}"

    def _handle_pan_start(self) -> str:
        if self.edge_scroll.active:
            self.edge_scroll.stop()
            debug.dbg2("MODE_SWITCH", to="pan", stopped="edge")
        if not self.fetch_baselines():
            self.panning.stop_pan()
            debug.dbg2("PAN_START", baselines=0, result="PAN_NO_BASELINE")
            return "PAN_NO_BASELINE"
        result = self.panning.start_pan()
        debug.dbg2("PAN_START", baselines=len(self.baselines), result=result)
        return result

    def _handle_pan_stop(self) -> str:
        self.panning.stop_pan()
        self.baselines = {}
        debug.dbg2("PAN_STOP")
        return "PAN_OFF"

    def _handle_nav_left(self) -> str:
        self._stop_competing_modes("navigation")
        return "OK" if self.navigator.navigate("left") else "ERROR:NAV_FAILED"

    def _handle_nav_right(self) -> str:
        self._stop_competing_modes("navigation")
        return "OK" if self.navigator.navigate("right") else "ERROR:NAV_FAILED"

    def _handle_nav_up(self) -> str:
        self._stop_competing_modes("navigation")
        return "OK" if self.navigator.navigate("up") else "ERROR:NAV_FAILED"

    def _handle_nav_down(self) -> str:
        self._stop_competing_modes("navigation")
        return "OK" if self.navigator.navigate("down") else "ERROR:NAV_FAILED"

    def _handle_center_cursor(self) -> str:
        self._stop_competing_modes("center-cursor")
        try:
            cursor_x, cursor_y = self.ipc.get_cursor_pos()
        except Exception as e:
            log.debug("center-cursor: cursor query failed: %s", e)
            return "ERROR:NO_CURSOR"

        workspace_id = self._get_active_workspace_id()
        if workspace_id is None:
            return "ERROR:NO_WORKSPACE"

        try:
            target = self._find_window_at_cursor(cursor_x, cursor_y, workspace_id, strict=True)
        except Exception as e:
            log.debug("center-cursor: client query failed: %s", e)
            return "ERROR:QUERY_FAILED"
        if target is None:
            return "ERROR:NO_WINDOW"

        try:
            centered = self.navigator.center_window(
                str(target.get("address", "")),
                workspace_id=workspace_id,
                cursor_x=cursor_x,
                cursor_y=cursor_y,
            )
        except Exception as e:
            log.warning("center-cursor failed: %s", e)
            return "ERROR:CENTER_FAILED"
        if not centered:
            return "ERROR:CENTER_FAILED"
        debug.dbg2(
            "CENTER_CURSOR",
            ws=workspace_id,
            target=target.get("address"),
            cursor=(cursor_x, cursor_y),
        )
        return "OK"

    def _get_focused_window_address(self) -> str:
        """Address of the focused window, empty string on failure."""
        try:
            resp = self.ipc.send("j/activewindow")
            w: dict[str, Any] = json.loads(resp)
            return str(w.get("address", ""))
        except Exception as e:
            log.debug("get focused window address failed: %s", e)
            return ""

    def _find_window_at_cursor(
        self, cx: int, cy: int, workspace_id: int, *, strict: bool = False
    ) -> dict[str, Any] | None:
        """Movable floating window on the workspace whose rect contains the cursor.

        Hyprland's window.drag() moves whatever is under the pointer, not
        the previously focused window — geometry must come from the same
        place. Shared by edge-scroll and center-cursor. On overlap the last
        match wins: Hyprland's clients vector is bottom-to-top.
        """
        try:
            resp = self.ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
        except Exception as e:
            if strict:
                raise
            log.debug("find window at cursor failed: %s", e)
            return None

        found: dict[str, Any] | None = None
        for w in clients:
            if not w.get("floating"):
                continue
            if w.get("hidden") or w.get("fullscreen"):
                continue
            wsw = w.get("workspace")
            if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                continue
            addr = str(w.get("address", ""))
            at = w.get("at", [0, 0])
            size = w.get("size", [0, 0])
            if not addr or len(at) < 2 or len(size) < 2:
                continue
            if at[0] <= cx < at[0] + size[0] and at[1] <= cy < at[1] + size[1]:
                found = w
        return found

    def _handle_edge_start(self) -> str:
        """Activate edge-scroll for the floating window under the cursor."""
        if self.panning.is_dragging:
            # Modes are mutually exclusive: a stale pan session would fight
            # the edge camera (and vice versa below).
            self.panning.stop_pan()
            self.baselines = {}
            debug.dbg2("MODE_SWITCH", to="edge", stopped="pan")

        try:
            cx, cy = get_cursor_pos()
        except Exception as e:
            debug.dbg("EDGE_START_DECISION", verdict="NO_CURSOR", error=str(e))
            return "EDGE_NO_CURSOR"

        ws_id = self._get_active_workspace_id()
        if ws_id is None:
            debug.dbg("EDGE_START_DECISION", cursor=(cx, cy), verdict="NO_WORKSPACE")
            return "EDGE_NO_WORKSPACE"

        win = self._find_window_at_cursor(cx, cy, ws_id)
        if win is None:
            # Nothing draggable under the pointer: empty desktop, a tiled
            # window, or a stale-focused window that sits off-screen.
            # Activating here would derive bogus grab offsets and send the
            # camera chasing an invisible window.
            debug.dbg(
                "EDGE_START_DECISION",
                cursor=(cx, cy),
                ws=ws_id,
                candidate=None,
                verdict="NO_WINDOW_UNDER_CURSOR",
            )
            return "EDGE_NO_WINDOW"

        at = win.get("at", [0, 0])
        size = win.get("size", [0, 0])
        candidate_addr = str(win.get("address", ""))

        # A real grab makes Hyprland focus the pressed window. If focus is
        # elsewhere, this press landed on a border/gap inside the window's
        # bounding rect — no drag will engage. Refuse before arming.
        focused_addr = self._get_focused_window_address()
        if focused_addr != candidate_addr:
            debug.dbg(
                "EDGE_START_DECISION",
                cursor=(cx, cy),
                ws=ws_id,
                candidate=candidate_addr,
                focused=focused_addr,
                verdict="REFUSED_FOCUS_MISMATCH",
            )
            return "EDGE_NO_WINDOW"

        self.edge_scroll_workspace = ws_id
        # One lookup for the whole gesture; edge_scroll_move runs per frame.
        self.edge_scroll_excluded = self._pan_excluded_addresses(ws_id)
        if not self._fetch_monitor_rect():
            # Without real geometry the overflow math would run against a
            # default 1920x1080 rect — on multi-monitor setups that causes
            # phantom scrolling at wrong edges. Refuse instead.
            debug.dbg("EDGE_START_DECISION", candidate=candidate_addr, verdict="NO_MONITOR")
            return "EDGE_NO_MONITOR"

        result = self.edge_scroll.start(
            EdgeScrollParams(
                dragged_addr=candidate_addr,
                win_x=at[0] if len(at) >= 2 else 0,
                win_y=at[1] if len(at) >= 2 else 0,
                win_w=size[0] if len(size) >= 2 else 0,
                win_h=size[1] if len(size) >= 2 else 0,
                cursor_x=cx,
                cursor_y=cy,
            )
        )
        debug.dbg(
            "EDGE_START_DECISION",
            cursor=(cx, cy),
            ws=ws_id,
            candidate=candidate_addr,
            focused=focused_addr,
            verdict=result,
        )
        return result

    def _handle_edge_stop(self) -> str:
        result = self.edge_scroll.stop()
        debug.dbg("EDGE_STOP", verdict=result)
        return result

    def _handle_toggle(self) -> str:
        self.panning.inverted = not self.panning.inverted
        return "INVERTED" if self.panning.inverted else "NORMAL"

    def _stop_competing_modes(self, to: str) -> None:
        """Stop pan/edge sessions before a canvas-toggle.

        A live pan keeps applying baseline+delta moves to floating windows,
        which would fight the toggle Lua dispatched right after (same
        windows, absolute writes, one frame apart). Clearing first makes
        the toggle outcome deterministic.
        """
        if self.panning.is_dragging:
            self.panning.stop_pan()
            self.baselines = {}
            self.baseline_workspace = None
            debug.dbg2("MODE_SWITCH", to=to, stopped="pan")
        if self.edge_scroll.active:
            self.edge_scroll.stop()
            debug.dbg2("MODE_SWITCH", to=to, stopped="edge")

    def _handle_canvas_toggle(self) -> str:
        self._stop_competing_modes("canvas-toggle")
        return self.navigator.canvas_toggle()

    def _handle_canvas_toggle_all(self) -> str:
        self._stop_competing_modes("canvas-toggle")
        return self.navigator.canvas_toggle_all()

    def _handle_canvas_toggle_single(self) -> str:
        self._stop_competing_modes("canvas-toggle")
        return self.navigator.canvas_toggle_single()

    def _handle_ping(self) -> str:
        return "PONG"

    def _handle_status(self) -> str:
        inv = "INVERTED" if self.panning.inverted else "NORMAL"
        pan = "PANNING" if self.panning.is_dragging else "IDLE"
        return f"{inv} {pan}"

    def _handle_reload(self) -> str:
        """Re-read the config file and apply it to the running daemon.

        Validation happens before anything is applied, so a broken config
        leaves the daemon running on the previous one instead of half-way
        between two. The error has to be returned rather than raised: the IPC
        server logs handler exceptions at debug level and sends the client
        nothing, which would surface as a bare "empty response from daemon".
        """
        # Resolving first and reading that exact path means the file named in
        # the response is provably the one that was read. Letting `load` search
        # and asking `resolve_path` afterwards would leave a window where the
        # file changes in between and the two disagree.
        source = resolve_path()
        try:
            cfg = load(source)
        except ConfigError as e:
            log.warning("reload rejected: %s", e)
            return f"ERROR:CONFIG_INVALID: {e}"

        self.apply_config(cfg)
        # Spawn rules live in the compositor, so a changed canvas.spawn or a
        # flipped auto_float only takes effect once they are re-armed. Costs a
        # few compositor round trips, which is why the response below is not
        # sent until it is done.
        self.navigator.rehydrate_spawn_rules()

        log.info("reloaded config from %s", source or "built-in defaults")
        return f"OK: reloaded ({source})" if source else "OK: reloaded (built-in defaults)"

    def handle_idle_pan_stop(self) -> bool:
        """Auto-stop panning after cursor idle; drop baselines.

        Baselines must die together with the pan: keeping them after an
        idle timeout would make a later shutdown restore windows to
        long-stale pre-pan positions, silently destroying layout changes
        the user made in between.
        """
        stopped = self.panning.check_idle_timeout()
        if stopped:
            self.baselines = {}
        return stopped

    def _pan_excluded(self, window: dict[str, Any]) -> bool:
        """True if this window must not be dragged along by the camera.

        Substring match on class, same as Navigator's protected apps. A fullscreen
        window is excluded unconditionally: it owns the output, and panning it
        away would take the user's whole view with it.
        """
        if window.get("fullscreen"):
            return True
        window_class = str(window.get("class", "")).lower()
        return any(app in window_class for app in self._pan_exclude_apps)

    def _pan_excluded_addresses(self, workspace_id: int) -> set[str]:
        """Addresses on a workspace that must not be dragged by the camera.

        One lookup per gesture, not per frame. The edge-scroll path re-queries
        nothing afterwards: the exclusion is baked into the Lua it sends.
        """
        try:
            clients = json.loads(self.ipc.send("j/clients"))
        except Exception as e:
            log.debug("pan exclusion lookup failed: %s", e)
            return set()
        out: set[str] = set()
        for w in clients:
            if not w.get("floating"):
                continue
            wsw = w.get("workspace")
            if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                continue
            if self._pan_excluded(w):
                addr = w.get("address", "")
                if addr:
                    out.add(str(addr))
        return out

    def fetch_baselines(self) -> bool:
        """Snapshot floating windows of the ACTIVE workspace as pan baselines.

        Excluded windows are left out of the snapshot rather than filtered in the
        move loop: both move_windows_to_delta and restore_baselines iterate this
        dict, so omitting a window here makes it stay put and skip the restore,
        which is the whole point of excluding it.
        """
        try:
            ws_resp = self.ipc.send("j/activeworkspace")
            ws: dict[str, Any] = json.loads(ws_resp)
            workspace_id = int(ws["id"])

            resp = self.ipc.send("j/clients")
            clients = json.loads(resp)
            baselines: dict[str, tuple[int, int]] = {}
            for w in clients:
                if not w.get("floating"):
                    continue
                if self._pan_excluded(w):
                    continue
                wsw = w.get("workspace")
                if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                    continue
                addr = w.get("address", "")
                at = w.get("at", [0, 0])
                if addr and len(at) >= 2:
                    baselines[addr] = (at[0], at[1])
            self.baselines = baselines
            self.baseline_workspace = workspace_id
            return True
        except Exception as e:
            log.warning("fetch baselines failed: %s", e)
            self.baselines = {}
            self.baseline_workspace = None
            return False

    def restore_baselines(self) -> None:
        """Move the snapshot's windows back to their pre-pan positions.

        Called on graceful shutdown so windows don't stay displaced.
        """
        if not self.baselines or self.baseline_workspace is None:
            return
        try:
            ws_id = int(self.baseline_workspace)
            lines = [
                LUA_DISPATCH_HELPER,
                f"local ws = hl.get_windows({{ floating = true, workspace = {ws_id} }})",
            ]
            lines.append("local bd = {")
            for addr, (bx, by) in self.baselines.items():
                safe_addr = _lua_escape(addr)
                lines.append(f'  ["{safe_addr}"] = {{{bx}, {by}}},')
            lines.append("}")
            lines.append("for _, w in ipairs(ws) do")
            lines.append("  local b = bd[tostring(w.address)]")
            lines.append("  if b then")
            lines.append(
                "    _canvas_dispatch(hl.dispatch(hl.dsp.window.move({"
                " x = b[1], y = b[2],"
                " relative = false, window = w })))"
            )
            lines.append("  end")
            lines.append("end")
            self.ipc.eval_lua("\n".join(lines))
            log.info("restored %d windows to pre-pan positions", len(self.baselines))
        except Exception as e:
            log.warning("restore baselines failed: %s", e)

    def move_windows_to_delta(self, total_dx: int, total_dy: int) -> None:
        """Move the workspace's floating windows to baseline + total_delta.

        Absolute positioning against the snapshot; scoped to the workspace
        captured at PAN_START.
        """
        if not self.baselines or self.baseline_workspace is None:
            return
        try:
            ws_id = int(self.baseline_workspace)
            lines = [
                LUA_DISPATCH_HELPER,
                f"local ws = hl.get_windows({{ floating = true, workspace = {ws_id} }})",
            ]
            lines.append("local bd = {")
            for addr, (bx, by) in self.baselines.items():
                safe_addr = _lua_escape(addr)
                lines.append(f'  ["{safe_addr}"] = {{{bx}, {by}}},')
            lines.append("}")
            lines.append("for _, w in ipairs(ws) do")
            lines.append("  local b = bd[tostring(w.address)]")
            lines.append("  if b then")
            lines.append(
                f"    _canvas_dispatch(hl.dispatch(hl.dsp.window.move({{"
                f" x = b[1] + {total_dx},"
                f" y = b[2] + {total_dy},"
                f" relative = false, window = w }})))"
            )
            lines.append("  end")
            lines.append("end")
            self.ipc.eval_lua("\n".join(lines))
        except Exception as e:
            log.warning("window move failed: %s", e)

    def edge_scroll_move(self, dx: int, dy: int) -> None:
        """Move all floating windows except the skipped ones by (dx, dy).

        Camera follows the dragged window: other windows move opposite.
        Cursor at right edge → camera right → other windows move left.
        Scoped to the workspace active at EDGE_START.
        """
        if dx == 0 and dy == 0:
            return
        if self.edge_scroll_workspace is None:
            log.warning("edge-scroll move skipped: no workspace captured")
            return
        ws_id = int(self.edge_scroll_workspace)
        # Skipped: the dragged window, plus anything window_pan_excludes or
        # fullscreen ruled out when the gesture was armed.
        skip = {str(self.edge_scroll.dragged_addr), *self.edge_scroll_excluded}
        try:
            lua = [LUA_DISPATCH_HELPER, "local skip = {"]
            for addr in sorted(skip):
                lua.append(f'  ["{_lua_escape(addr)}"] = true,')
            lua.extend(
                [
                    "}",
                    f"local ws = hl.get_windows({{ floating = true, workspace = {ws_id} }})",
                    "for _, w in ipairs(ws) do",
                    "  if not skip[tostring(w.address)] then",
                    "    _canvas_dispatch(hl.dispatch(hl.dsp.window.move({"
                    f" x = {dx}, y = {dy},"
                    " relative = true, window = w })))",
                    "  end",
                    "end",
                ]
            )
            self.ipc.eval_lua("\n".join(lua))
        except Exception as e:
            log.warning("edge-scroll move failed: %s", e)


def run() -> None:
    """Main entry point for the canvas daemon."""
    debug.enable_from_env()
    logging.basicConfig(
        level=logging.DEBUG if debug.enabled() else logging.INFO,
        format="%(name)s: %(message)s",
    )
    if debug.enabled():
        import os

        debug.dbg("BOOT", pid=os.getpid())

    log.info("loading config...")
    cfg = load()

    ipc = HyprIPC.from_env()
    state = PanningState()
    edge_scroll = EdgeScrollState()
    navigator = Navigator(ipc=ipc)

    daemon_state = DaemonState(
        panning=state, edge_scroll=edge_scroll, navigator=navigator, ipc=ipc
    )
    # Same call `canvas-ctl reload` makes, so boot and reload cannot diverge.
    daemon_state.apply_config(cfg)

    ipc_server = IpcServer(handler=daemon_state.handle_ipc)

    acquire_singleton(ipc_server.sock_path)

    navigator.rehydrate_spawn_rules()

    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("received signal %d, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)

    ipc_thread = threading.Thread(target=ipc_server.serve, daemon=True)
    ipc_thread.start()

    cursor_thread = threading.Thread(
        target=cursor_poller, args=(state, edge_scroll, stop_event), daemon=True
    )
    cursor_thread.start()

    log.info("ready — SUPER+SHIFT+LMB to pan, SUPER+LMB to edge-scroll")

    target_interval = 1.0 / 60.0
    prev_time = time.monotonic()

    poller_died = False
    ipc_died = False
    try:
        prev_total = (0, 0)
        while not stop_event.is_set():
            with daemon_state._operation_lock:
                daemon_state.handle_idle_pan_stop()

                if not state.pan_active:
                    prev_total = (0, 0)
                else:
                    total_dx, total_dy = state.get_total_delta()
                    if (
                        total_dx,
                        total_dy,
                    ) != (0, 0) and (total_dx, total_dy) != prev_total:
                        prev_total = (total_dx, total_dy)
                        try:
                            daemon_state.move_windows_to_delta(total_dx, total_dy)
                        except Exception as e:
                            log.warning("window move failed: %s", e)

                edge_scroll.check_idle_timeout()
                if edge_scroll.active:
                    es_dx, es_dy = edge_scroll.consume_delta()
                    if es_dx != 0 or es_dy != 0:
                        daemon_state.edge_scroll_move(es_dx, es_dy)

            if not state.poller_alive:
                poller_died = True
                log.error("cursor poller died — cannot track cursor position")
                break

            # The IPC thread can die before or during the loop: a stale socket
            # path, revoked permissions, or the symlink guard all return or raise
            # outside serve()'s try block. The singleton lock is already held by
            # this point, so a daemon that lingers here holds a lock no second
            # instance can take while answering nothing — the only way out is a
            # SIGKILL. Better to exit and let a supervisor try again.
            if not ipc_thread.is_alive():
                ipc_died = True
                log.error("IPC server died — no control socket, cannot accept commands")
                break

            now = time.monotonic()
            elapsed = now - prev_time
            time.sleep(max(0, target_interval - elapsed))
            prev_time = time.monotonic()

    except KeyboardInterrupt as exc:
        log.info("shutting down: %s", exc)
    finally:
        stop_event.set()
        ipc_server.stop()
        cursor_thread.join(timeout=1)
        ipc_thread.join(timeout=1)
        with daemon_state._operation_lock:
            daemon_state.restore_baselines()
            # Spawn rules match the whole workspace and live in the compositor's
            # engine, which outlives this process. Leaving them behind means
            # every window opened on that workspace afterwards still arrives
            # floating and spawn-sized, with no daemon left to undo it.
            daemon_state.navigator.drop_spawn_rules()

    if poller_died:
        # Exit non-zero so a supervisor (e.g. systemd Restart=on-failure)
        # restarts the daemon instead of leaving a zombie that answers
        # ping but can never pan again.
        raise SystemExit(1)
    if ipc_died:
        # Same reasoning: staying alive here would keep the singleton lock while
        # serving no commands, blocking every later start.
        raise SystemExit(1)
