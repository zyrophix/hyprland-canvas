"""Navigate between floating windows on the infinite canvas."""

import json
import logging
import re
import time
from typing import Any

from canvas import debug, spawnrules, toggle_state
from canvas.hypr import LUA_DISPATCH_HELPER, HyprIPC
from canvas.spawnrules import Workarea

log = logging.getLogger("canvas.navigation")

_VALID_ADDR = re.compile(r"^0x[0-9a-fA-F]+$")


def _safe_int(value: object, name: str) -> int:
    """Coerce to int, raising ValueError if impossible.

    Prevents accidental string interpolation into Lua — all values
    inserted into Lua f-strings MUST pass through this or _VALID_ADDR.
    """
    try:
        result: int = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsafe Lua value: {name}={value!r}") from exc
    return result


class Navigator:
    """Handles navigation between floating windows with auto-pan."""

    def __init__(
        self,
        ipc: HyprIPC,
        protected_apps: list[str] | None = None,
        cooldown: float = 0.2,
        preserve_geometry: bool = True,
        auto_float: bool = False,
        spawn_cfg: dict[str, Any] | None = None,
    ) -> None:
        self._ipc = ipc
        self._protected_apps = [a.lower() for a in protected_apps or []]
        self._cooldown = cooldown
        self._preserve_geometry = preserve_geometry
        self._auto_float = auto_float
        self._spawn_cfg = spawn_cfg or {}
        self._last_nav_time = 0.0
        # workspace id -> snapshot of TILED windows before canvas ON.
        # Only these are tiled again on OFF, so windows that were already
        # floating before canvas mode survive.
        # workspace id -> last known FLOATING geometry per address.
        # Reapplied on the next ON so the canvas comes back where it was.
        # workspace id -> addresses already floating at ON. Windows floating at
        # OFF but absent from here arrived during canvas and belong to it.
        # workspace id -> addresses the user actually moved while canvas was
        # on. Only these are worth remembering: geometry Hyprland produced by
        # itself when a window became floating is not a user choice.
        # workspace id -> spawn rule names registered in the compositor, kept
        # so rules left behind by a crash can be disabled again at startup.
        self._canvas_mode_workspaces: dict[int, dict[str, dict[str, list[int]]]] = {}
        self._floating_geos: dict[int, dict[str, dict[str, list[int]]]] = {}
        self._pre_floating: dict[int, set[str]] = {}
        self._panned: dict[int, set[str]] = {}
        self._spawn_rules: dict[int, list[str]] = {}
        raw = toggle_state.load()
        for ws, sections in raw.items():
            if isinstance(sections, list):
                # Legacy mocked load in tests: bare address list = tiled set
                self._canvas_mode_workspaces[ws] = {
                    str(a): {} for a in sections if isinstance(a, str)
                }
            elif isinstance(sections, dict) and (
                "active" in sections or "tiled" in sections or "floating" in sections
            ):
                tiled = sections.get("tiled", {})
                floating = sections.get("floating", {})
                if not isinstance(tiled, dict):
                    tiled = {}
                if not isinstance(floating, dict):
                    floating = {}
                active = sections.get("active", bool(tiled))
                if active is True:
                    self._canvas_mode_workspaces[ws] = dict(tiled)
                if floating:
                    self._floating_geos[ws] = dict(floating)
                pre = sections.get("pre_floating")
                if isinstance(pre, list):
                    self._pre_floating[ws] = {str(a) for a in pre if isinstance(a, str)}
                rules = sections.get("spawn_rules")
                if isinstance(rules, list):
                    self._spawn_rules[ws] = [str(a) for a in rules if isinstance(a, str)]
            elif isinstance(sections, dict):
                # Legacy v1 dict (addr->geo of tiled slots): keep addresses
                # for targeting, drop geometry (tiled slots are layout-owned).
                addrs: dict[str, dict[str, list[int]]] = {
                    a: {} for a in sections if isinstance(a, str)
                }
                if addrs:
                    self._canvas_mode_workspaces[ws] = addrs
            else:
                self._canvas_mode_workspaces[ws] = {}
        if debug.enabled():
            debug.dbg2(
                "STATE_LOAD",
                workspaces=sorted(raw.keys()),
                tiled={ws: len(s) for ws, s in self._canvas_mode_workspaces.items()},
                floating={ws: len(s) for ws, s in self._floating_geos.items()},
                preserve_geometry=self._preserve_geometry,
            )

    @staticmethod
    def _window_center(w: dict[str, Any]) -> tuple[int, int]:
        return w["at"][0] + w["size"][0] // 2, w["at"][1] + w["size"][1] // 2

    @staticmethod
    def _window_bounds(w: dict[str, Any]) -> dict[str, int]:
        x, y = w["at"][0], w["at"][1]
        ww, wh = w["size"][0], w["size"][1]
        return {
            "left": x,
            "right": x + ww,
            "top": y,
            "bottom": y + wh,
            "center_x": x + ww // 2,
            "center_y": y + wh // 2,
        }

    @staticmethod
    def _overlap_h(b1: dict[str, int], b2: dict[str, int]) -> bool:
        return not (b1["right"] <= b2["left"] or b1["left"] >= b2["right"])

    @staticmethod
    def _overlap_v(b1: dict[str, int], b2: dict[str, int]) -> bool:
        return not (b1["bottom"] <= b2["top"] or b1["top"] >= b2["bottom"])

    def _find_spatial_target(
        self,
        floating: list[dict[str, Any]],
        current_bounds: dict[str, int],
        current_center: tuple[int, int],
        direction: str,
    ) -> dict[str, Any] | None:
        cx, cy = current_center
        candidates = [w for w in floating if not self._is_protected(w)]
        if not candidates:
            return None

        # Tier 1: overlapping band + direction
        aligned: list[tuple[dict[str, Any], int]] = []
        for w in candidates:
            b = self._window_bounds(w)
            wx, wy = b["center_x"], b["center_y"]
            if direction == "left" and self._overlap_v(current_bounds, b) and wx < cx:
                aligned.append((w, cx - wx))
            elif direction == "right" and self._overlap_v(current_bounds, b) and wx > cx:
                aligned.append((w, wx - cx))
            elif direction == "up" and self._overlap_h(current_bounds, b) and wy < cy:
                aligned.append((w, cy - wy))
            elif direction == "down" and self._overlap_h(current_bounds, b) and wy > cy:
                aligned.append((w, wy - cy))
        if aligned:
            return sorted(aligned, key=lambda x: x[1])[0][0]

        # Tier 2: any window in direction
        same_dir: list[tuple[dict[str, Any], int]] = []
        for w in candidates:
            b = self._window_bounds(w)
            wx, wy = b["center_x"], b["center_y"]
            if direction == "left" and wx < cx:
                same_dir.append((w, cx - wx))
            elif direction == "right" and wx > cx:
                same_dir.append((w, wx - cx))
            elif direction == "up" and wy < cy:
                same_dir.append((w, cy - wy))
            elif direction == "down" and wy > cy:
                same_dir.append((w, wy - cy))
        if same_dir:
            return sorted(same_dir, key=lambda x: x[1])[0][0]

        # Tier 3: wrap — farthest in opposite direction
        opp = {"left": "right", "right": "left", "up": "down", "down": "up"}[direction]
        wrap: list[tuple[dict[str, Any], int]] = []
        for w in candidates:
            b = self._window_bounds(w)
            wx, wy = b["center_x"], b["center_y"]
            if opp == "left" and wx < cx:
                wrap.append((w, cx - wx))
            elif opp == "right" and wx > cx:
                wrap.append((w, wx - cx))
            elif opp == "up" and wy < cy:
                wrap.append((w, cy - wy))
            elif opp == "down" and wy > cy:
                wrap.append((w, wy - cy))
        if wrap:
            return sorted(wrap, key=lambda x: x[1])[0][0]
        return None

    def navigate(self, direction: str) -> bool:
        """Navigate to the nearest floating window. False only on IPC failure."""
        current_time = time.monotonic()
        if current_time - self._last_nav_time < self._cooldown:
            return True
        self._last_nav_time = current_time

        workspace_id = self._get_active_workspace_id()
        if workspace_id is None:
            return False

        floating = self._get_floating_windows(workspace_id)
        if floating is None:
            return False
        if len(floating) <= 1:
            return True

        focused = self._get_focused_window()
        if focused is None:
            return False
        if "address" not in focused:
            return True

        current_addr = focused["address"]
        current_win = next((w for w in floating if w["address"] == current_addr), None)
        if current_win is None:
            return True

        current_bounds = self._window_bounds(current_win)
        current_center = self._window_center(current_win)

        target = self._find_spatial_target(floating, current_bounds, current_center, direction)
        # Fallback: circular index order (legacy behavior) when no spatial candidate
        target_addr: str | None = None
        if target is not None:
            target_addr = target["address"]
        else:
            # No window in that direction spatially — cycle by index (protected-aware)
            current_index = next(
                (i for i, w in enumerate(floating) if w["address"] == current_addr), -1
            )
            if current_index != -1:
                idx = current_index
                for _ in range(len(floating)):
                    if direction in ("right", "down"):
                        idx = (idx + 1) % len(floating)
                    else:
                        idx = (idx - 1) % len(floating)
                    if not self._is_protected(floating[idx]):
                        target_addr = floating[idx]["address"]
                        break

        if target_addr is None:
            return True

        floating_updated = self._get_floating_windows(workspace_id)
        if floating_updated is None:
            return False
        # Pan the monitor the target actually lives on. Resolving the centre
        # without coordinates falls back to the focused monitor, so a target on
        # a second monitor was dragged onto the first one to satisfy a
        # navigation command. Falls back to the focused monitor when the target
        # sits outside every output.
        target_at = next(
            (w.get("at") for w in floating_updated if w.get("address") == target_addr),
            None,
        )
        center = None
        if isinstance(target_at, list) and len(target_at) >= 2:
            center = self._get_monitor_center(int(target_at[0]), int(target_at[1]))
        if center is None:
            center = self._get_monitor_center()
        if center is None:
            return False
        center_x, center_y = center
        return self._pan_to_window(floating_updated, target_addr, center_x, center_y, workspace_id)

    def _persist_canvas_state(
        self,
        canvas_modes: dict[int, dict[str, dict[str, list[int]]]],
        floating_geos: dict[int, dict[str, dict[str, list[int]]]],
        pre_floating: dict[int, set[str]] | None = None,
        spawn_rules: dict[int, list[str]] | None = None,
    ) -> None:
        pre = self._pre_floating if pre_floating is None else pre_floating
        rules = self._spawn_rules if spawn_rules is None else spawn_rules
        workspaces = set(canvas_modes) | set(floating_geos) | set(pre) | set(rules)
        state: toggle_state.State = {}
        for ws in workspaces:
            state[ws] = {
                "active": ws in canvas_modes,
                "tiled": dict(canvas_modes.get(ws, {})),
                "floating": dict(floating_geos.get(ws, {})),
                "pre_floating": sorted(pre.get(ws, set())),
                "spawn_rules": list(rules.get(ws, [])),
            }
        toggle_state.save(state)

    def canvas_toggle(self) -> str:
        # Backward-compat alias — single word `canvas-toggle` still means "all"
        return self.canvas_toggle_all()

    def canvas_toggle_all(self) -> str:
        workspace_id = self._get_active_workspace_id()
        if workspace_id is None:
            return "ERROR:NO_WORKSPACE"

        if workspace_id in self._canvas_mode_workspaces:
            snapshot = self._canvas_mode_workspaces[workspace_id]
            captured: dict[str, dict[str, list[int]]] = {}
            # Remember only where the user put windows by panning. Everything
            # else is restored from the tiled snapshot on the next ON, which is
            # the box the window actually had.
            panned = set(self._panned.get(workspace_id, set())) & set(snapshot)
            if self._preserve_geometry and panned:
                snapshot_result = self._snapshot_floating_geos(workspace_id, panned)
                if snapshot_result is None:
                    return "ERROR:SNAPSHOT_FAILED"
                captured = snapshot_result

            # Windows that arrived as floating during canvas (spawn rules make
            # them float at map time) are canvas members too, so they are tiled
            # back. Addresses already in the snapshot keep their recorded
            # geometry — _toggle_order reads it to restore the original layout.
            #
            # Deliberately not gated on a non-empty snapshot: an empty
            # snapshot is exactly the case where this matters most — canvas
            # turned on with nothing tiled, then a window spawned floating into
            # it. Gating here left that window floating and centred forever,
            # because nothing else in the OFF path tiles it.
            tile_target = snapshot
            if self._auto_float:
                arrived = self._arrived_addresses(workspace_id)
                extra: dict[str, dict[str, list[int]]] = {
                    a: {} for a in sorted(arrived) if a not in snapshot
                }
                if extra:
                    tile_target = {**snapshot, **extra}

            next_modes = dict(self._canvas_mode_workspaces)
            next_modes.pop(workspace_id, None)
            next_floating = dict(self._floating_geos)
            if self._preserve_geometry and captured:
                next_floating[workspace_id] = captured
            elif not self._preserve_geometry:
                next_floating.pop(workspace_id, None)
            next_pre = dict(self._pre_floating)
            next_pre.pop(workspace_id, None)
            next_panned = dict(self._panned)
            next_panned.pop(workspace_id, None)
            stale_rules = self._spawn_rules.get(workspace_id, [])
            next_rules = dict(self._spawn_rules)
            next_rules.pop(workspace_id, None)

            # Persist the target state first. A persistence failure therefore
            # cannot leave the compositor tiled while memory still says ON.
            try:
                self._persist_canvas_state(next_modes, next_floating, next_pre, next_rules)
            except toggle_state.ToggleStateError as e:
                log.warning("canvas OFF state save failed: %s", e)
                return "ERROR:STATE_SAVE_FAILED"

            if tile_target and not self._tile_windows(workspace_id, tile_target):
                compositor_rollback = self._set_snapshot_floating(
                    workspace_id, snapshot, floating=True
                )
                if captured:
                    compositor_rollback = (
                        self._apply_floating_geos(workspace_id, captured) and compositor_rollback
                    )
                state_rollback = True
                try:
                    self._persist_canvas_state(self._canvas_mode_workspaces, self._floating_geos)
                except toggle_state.ToggleStateError as e:
                    log.error("canvas OFF state rollback failed: %s", e)
                    state_rollback = False
                if not state_rollback and not compositor_rollback:
                    return "ERROR:STATE_AND_COMPOSITOR_ROLLBACK_FAILED"
                if not state_rollback:
                    return "ERROR:STATE_ROLLBACK_FAILED"
                if not compositor_rollback:
                    return "ERROR:COMPOSITOR_ROLLBACK_FAILED"
                return "ERROR:TILE_FAILED"

            # Only now that the tiling succeeded may the spawn rules go away:
            # a failed tile rolls the workspace back to floating, where the
            # rules are still the right behaviour.
            if stale_rules:
                spawnrules.disable(stale_rules, self._ipc)

            self._canvas_mode_workspaces = next_modes
            self._floating_geos = next_floating
            self._pre_floating = next_pre
            self._panned = next_panned
            self._spawn_rules = next_rules
            if debug.enabled():
                debug.dbg2(
                    "TOGGLE_OFF",
                    ws=workspace_id,
                    count=len(snapshot),
                    addrs=sorted(snapshot.keys()),
                )
                if debug.level() >= 2:
                    geos = {a: snapshot[a] for a in sorted(snapshot.keys())}
                    debug.dbg2("TOGGLE_OFF_DETAIL", ws=workspace_id, geos=geos)
            return "CANVAS_OFF"

        tiled_snapshot = self._snapshot_tiled_windows(workspace_id)
        if tiled_snapshot is None:
            return "ERROR:SNAPSHOT_FAILED"
        next_modes = dict(self._canvas_mode_workspaces)
        next_modes[workspace_id] = tiled_snapshot
        next_floating = dict(self._floating_geos)

        # Restore-on-ON can also target windows that were already floating and
        # therefore are absent from tiled_snapshot. Capture those separately so
        # a partial geometry failure can roll them back too.
        rollback_addresses = set(self._floating_geos.get(workspace_id, {})) - set(tiled_snapshot)
        rollback_geos: dict[str, dict[str, list[int]]] = {}
        if rollback_addresses:
            rollback_result = self._snapshot_floating_geos(workspace_id, rollback_addresses)
            if rollback_result is None:
                return "ERROR:SNAPSHOT_FAILED"
            rollback_geos = rollback_result

        # Windows already floating at ON must not be tiled back on OFF, so
        # record them. Only needed when spawn rules can add floating windows.
        pre_floating: set[str] = set()
        rule_names: list[str] = []
        workarea: Workarea | None = None
        if self._auto_float:
            captured_floating = self._snapshot_floating_addresses(workspace_id)
            if captured_floating is None:
                return "ERROR:SNAPSHOT_FAILED"
            pre_floating = captured_floating

            # Register before touching state or the compositor: a failure here
            # leaves both untouched, so no rollback is needed.
            workarea = spawnrules.resolve_workareas(self._ipc).get(workspace_id)
            if workarea is None:
                log.warning("canvas ON: no workarea for workspace %s", workspace_id)
                return "ERROR:SPAWN_RULE_FAILED"
            try:
                built = spawnrules.build_rules(self._spawn_cfg, workspace_id, workarea)
                rule_names = spawnrules.register(built, self._ipc)
            except spawnrules.SpawnRuleError as e:
                log.warning("canvas ON: spawn rules failed: %s", e)
                return "ERROR:SPAWN_RULE_FAILED"

        next_pre = dict(self._pre_floating)
        next_pre[workspace_id] = pre_floating
        next_rules = dict(self._spawn_rules)
        next_rules[workspace_id] = rule_names

        try:
            self._persist_canvas_state(next_modes, next_floating, next_pre, next_rules)
        except toggle_state.ToggleStateError as e:
            log.warning("canvas ON state save failed: %s", e)
            spawnrules.disable(rule_names, self._ipc)
            return "ERROR:STATE_SAVE_FAILED"

        if not self._set_all_floating(workspace_id, floating=True):
            failure = "ERROR:FLOAT_FAILED"
        elif not self._apply_canvas_geometry(workspace_id, tiled_snapshot, workarea):
            failure = "ERROR:GEOMETRY_RESTORE_FAILED"
        else:
            failure = ""
        if failure:
            # Canvas never came up, so its spawn rules must not stay behind.
            spawnrules.disable(rule_names, self._ipc)
            compositor_rollback = self._set_snapshot_floating(
                workspace_id, tiled_snapshot, floating=False
            )
            if rollback_geos:
                compositor_rollback = (
                    self._apply_floating_geos(workspace_id, rollback_geos) and compositor_rollback
                )
            state_rollback = True
            try:
                self._persist_canvas_state(self._canvas_mode_workspaces, self._floating_geos)
            except toggle_state.ToggleStateError as e:
                log.error("canvas ON state rollback failed: %s", e)
                state_rollback = False
            if not state_rollback and not compositor_rollback:
                return "ERROR:STATE_AND_COMPOSITOR_ROLLBACK_FAILED"
            if not state_rollback:
                return "ERROR:STATE_ROLLBACK_FAILED"
            if not compositor_rollback:
                return "ERROR:COMPOSITOR_ROLLBACK_FAILED"
            return failure

        self._canvas_mode_workspaces = next_modes
        self._floating_geos = next_floating
        self._pre_floating = next_pre
        self._spawn_rules = next_rules
        if debug.enabled():
            debug.dbg2(
                "TOGGLE_ON",
                ws=workspace_id,
                count=len(tiled_snapshot),
                addrs=sorted(tiled_snapshot.keys()),
                preserve_geometry=self._preserve_geometry,
            )
            if debug.level() >= 2 and tiled_snapshot:
                debug.dbg2("TOGGLE_ON_DETAIL", ws=workspace_id, geos=tiled_snapshot)
        return "CANVAS_ON"

    def canvas_toggle_single(self) -> str:
        focused = self._get_focused_window()
        if focused is None or "address" not in focused:
            return "ERROR:NO_FOCUS"
        addr = str(focused["address"])
        if not _VALID_ADDR.match(addr):
            return "ERROR:BAD_ADDRESS"
        was_floating = bool(focused.get("floating"))
        try:
            lua = (
                f"{LUA_DISPATCH_HELPER}\n"
                f"local w = nil\n"
                f"for _, win in ipairs(hl.get_windows({{}})) do\n"
                f'  if tostring(win.address) == "{addr}" then w = win; break end\n'
                f"end\n"
                f"if w then _canvas_dispatch(hl.dispatch(hl.dsp.window.float({{"
                f' action = "toggle", window = w }}))) end'
            )
            self._ipc.eval_lua(lua)
        except Exception as e:
            log.warning("toggle single failed: %s", e)
            return "ERROR:TOGGLE_FAILED"
        return "TILED" if was_floating else "FLOATED"

    def _snapshot_tiled_windows(self, workspace_id: int) -> dict[str, dict[str, list[int]]] | None:
        """Snapshot of currently tiled windows on the workspace (pre-canvas state).

        When preserve_geometry is true, each entry stores at/size. Tiled
        coordinates are informational only (placement is layout-owned) and
        feed the row-major toggle order in _tile_windows.
        """
        try:
            resp = self._ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
            snap: dict[str, dict[str, list[int]]] = {}
            debug_details: dict[str, dict[str, Any]] = {} if debug.level() >= 2 else {}  # type: ignore[assignment]
            for w in clients:
                if w.get("floating"):
                    continue
                addr = w.get("address")
                if not addr or not isinstance(addr, str):
                    continue
                wsw = w.get("workspace")
                if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                    continue
                if self._preserve_geometry:
                    at = w.get("at", [0, 0])
                    size = w.get("size", [0, 0])
                    try:
                        snap[addr] = {
                            "at": [int(at[0]), int(at[1])],
                            "size": [int(size[0]), int(size[1])],
                        }
                    except Exception:
                        snap[addr] = {}
                else:
                    snap[addr] = {}
                if debug.level() >= 2:
                    at = w.get("at", [0, 0])
                    size = w.get("size", [0, 0])
                    debug_details[addr] = {
                        "at": at,
                        "size": size,
                        "class": str(w.get("class", ""))[:40],
                        "title": str(w.get("title", ""))[:40],
                    }
            if debug.enabled():
                debug.dbg2(
                    "SNAPSHOT_CREATE",
                    ws=workspace_id,
                    count=len(snap),
                    addrs=sorted(snap.keys()),
                    preserve_geometry=self._preserve_geometry,
                )
                if debug.level() >= 2 and debug_details:
                    debug.dbg2("SNAPSHOT_CREATE_DETAIL", ws=workspace_id, details=debug_details)
            return snap
        except Exception as e:
            log.warning("snapshot tiled windows failed: %s", e)
            return None

    def _snapshot_floating_geos(
        self, workspace_id: int, addresses: set[str] | None = None
    ) -> dict[str, dict[str, list[int]]] | None:
        """Capture current FLOATING geometry for the given addresses.

        Unlike tiled slots (layout-owned), floating positions are
        authoritative — move/resize applies them exactly. These are the
        coordinates the next canvas ON restores. If addresses is None, capture
        every floating window on the workspace.
        """
        try:
            resp = self._ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
            geos: dict[str, dict[str, list[int]]] = {}
            for w in clients:
                if not w.get("floating"):
                    continue
                addr = w.get("address")
                if not isinstance(addr, str) or (addresses is not None and addr not in addresses):
                    continue
                wsw = w.get("workspace")
                if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                    continue
                at = w.get("at", [0, 0])
                size = w.get("size", [0, 0])
                try:
                    geos[addr] = {
                        "at": [int(at[0]), int(at[1])],
                        "size": [int(size[0]), int(size[1])],
                    }
                except Exception:
                    continue
            if debug.enabled():
                debug.dbg2(
                    "FLOATING_SNAPSHOT",
                    ws=workspace_id,
                    count=len(geos),
                    addrs=sorted(geos.keys()),
                    geos=geos,
                )
            return geos
        except Exception as e:
            log.warning("snapshot floating geos failed: %s", e)
            return None

    def floating_addresses(self, workspace_id: int) -> set[str]:
        """Addresses of the floating windows on a workspace, empty on failure."""
        return self._snapshot_floating_addresses(workspace_id) or set()

    def note_panned(self, workspace_id: int, addresses: set[str]) -> None:
        """Record that the user moved these windows while canvas was on.

        Only such geometry is worth restoring later. Geometry that Hyprland
        produced on its own when a window became floating is not a choice the
        user made, and replaying it is what used to make canvas-toggle scramble
        the layout.
        """
        if not addresses:
            return
        self._panned.setdefault(workspace_id, set()).update(addresses)

    def rehydrate_spawn_rules(self) -> None:
        """Re-arm spawn rules after a daemon restart.

        Rules live in the compositor's engine and outlive this process, so any
        left behind by a previous run are disabled first — otherwise a crash
        would leave canvas sizing active on a workspace that is not in canvas
        mode. Every workspace that is still in canvas mode then gets a fresh
        set, because the compositor cannot replay the old ones for us.
        """
        stale = [name for names in self._spawn_rules.values() for name in names]
        if stale:
            spawnrules.disable(stale, self._ipc)
        self._spawn_rules = {}

        if self._auto_float:
            workareas = spawnrules.resolve_workareas(self._ipc)
            for ws_id in sorted(self._canvas_mode_workspaces):
                workarea = workareas.get(ws_id)
                if workarea is None:
                    log.warning("spawn rules: no workarea for workspace %s", ws_id)
                    continue
                try:
                    built = spawnrules.build_rules(self._spawn_cfg, ws_id, workarea)
                    self._spawn_rules[ws_id] = spawnrules.register(built, self._ipc)
                except spawnrules.SpawnRuleError as e:
                    log.warning("spawn rules for workspace %s failed: %s", ws_id, e)

        try:
            self._persist_canvas_state(self._canvas_mode_workspaces, self._floating_geos)
        except toggle_state.ToggleStateError as e:
            log.warning("spawn rule state save failed: %s", e)

    def _snapshot_floating_addresses(self, workspace_id: int) -> set[str] | None:
        """Addresses that are floating on the workspace right now.

        Recorded at canvas ON so that OFF can tell windows that were already
        floating (which survive) from windows the spawn rules made floating
        during canvas (which are canvas members and get tiled back).
        """
        floating = self._get_floating_windows(workspace_id)
        if floating is None:
            return None
        return {
            str(w["address"])
            for w in floating
            if isinstance(w.get("address"), str) and _VALID_ADDR.match(str(w["address"]))
        }

    def _arrived_addresses(self, workspace_id: int) -> set[str]:
        """Floating windows on the workspace that were not floating at ON."""
        before = self._pre_floating.get(workspace_id, set())
        floating = self._get_floating_windows(workspace_id)
        if floating is None:
            return set()
        return {
            str(w["address"])
            for w in floating
            if isinstance(w.get("address"), str)
            and _VALID_ADDR.match(str(w["address"]))
            and str(w["address"]) not in before
        }

    def _apply_canvas_geometry(
        self,
        workspace_id: int,
        snapshot: dict[str, dict[str, list[int]]],
        workarea: Workarea | None,
    ) -> bool:
        """Give every freshly floated window a geometry we chose.

        Hyprland does not keep a tiled box when a window becomes floating: it
        substitutes the size the client asked for, re-centres on the old centre
        and clamps the result into the workarea, all on purpose. Worse, windows
        are floated one at a time and each one re-runs the layout, so the result
        also depends on the order. Setting the geometry here instead makes the
        outcome deterministic and independent of both.

        Priority per address: a position the user panned to, then the
        configured spawn size, then the tiled box the window just had.
        """
        if not snapshot:
            return True

        stored = self._floating_geos.get(workspace_id, {}) if self._preserve_geometry else {}
        props: dict[str, dict[str, str]] = {}
        sizes: dict[str, tuple[int, int]] = {}
        if self._auto_float and workarea is not None:
            need_size = [a for a in snapshot if a not in stored]
            if need_size:
                props = self._client_class_title(workspace_id, set(need_size))
                for addr in need_size:
                    try:
                        spec = spawnrules.resolve_spec(self._spawn_cfg, props.get(addr, {}))
                        sizes[addr] = spawnrules.parse_size(spec, workarea)
                    except spawnrules.SpawnRuleError as e:
                        log.warning("spawn size for %s skipped: %s", addr, e)

        targets: dict[str, dict[str, list[int]]] = {}
        for addr in sorted(snapshot):
            if not _VALID_ADDR.match(addr):
                continue
            if addr in stored:
                targets[addr] = stored[addr]
                continue
            if addr in sizes:
                _w, _h = sizes[addr]
                box = snapshot[addr]
                targets[addr] = {
                    "at": list(box.get("at", [0, 0])),
                    "size": [_w, _h],
                }
                continue
            box = snapshot[addr]
            at = list(box.get("at", [0, 0]))
            size = list(box.get("size", [0, 0]))
            if at == [0, 0] and size == [0, 0]:
                continue
            targets[addr] = {"at": at, "size": size}

        if not targets:
            return True
        if debug.enabled():
            debug.dbg2(
                "CANVAS_GEOMETRY",
                ws=workspace_id,
                count=len(targets),
                from_stored=sorted(a for a in targets if a in stored),
                spawn_sized=sorted(a for a in targets if a in sizes),
            )
        return self._apply_floating_geos(workspace_id, targets)

    def _client_class_title(
        self, workspace_id: int, addresses: set[str]
    ) -> dict[str, dict[str, str]]:
        """Class and title per address, so spawn sizes can be matched locally."""
        try:
            resp = self._ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
        except Exception as e:
            log.warning("spawn props lookup failed: %s", e)
            return {}
        out: dict[str, dict[str, str]] = {}
        for w in clients:
            addr = w.get("address")
            if not isinstance(addr, str) or addr not in addresses:
                continue
            wsw = w.get("workspace")
            if not isinstance(wsw, dict) or wsw.get("id") != workspace_id:
                continue
            out[addr] = {
                "class": str(w.get("class", "")),
                "title": str(w.get("title", "")),
            }
        return out

    def _restore_floating_geos(self, workspace_id: int) -> bool:
        """Move newly floated snapshot windows to stored floating geometry."""
        if not self._preserve_geometry:
            return True
        stored = self._floating_geos.get(workspace_id, {})
        return self._apply_floating_geos(workspace_id, stored)

    def _apply_floating_geos(
        self, workspace_id: int, stored: dict[str, dict[str, list[int]]]
    ) -> bool:
        """Apply known floating geometry to live windows on one workspace."""
        if not stored:
            return True
        try:
            resp = self._ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
            live = {
                str(w.get("address"))
                for w in clients
                if w.get("floating")
                and isinstance(w.get("workspace"), dict)
                and w.get("workspace", {}).get("id") == workspace_id
            }
        except Exception as e:
            log.warning("restore floating geos failed: %s", e)
            return False
        targets: dict[str, dict[str, list[int]]] = {}
        for addr in sorted(stored):
            if addr not in live or not _VALID_ADDR.match(addr):
                continue
            geo = stored[addr]
            if not isinstance(geo, dict):
                continue
            try:
                at = [int(geo.get("at", [0, 0])[0]), int(geo.get("at", [0, 0])[1])]
                size = [int(geo.get("size", [0, 0])[0]), int(geo.get("size", [0, 0])[1])]
            except Exception:
                continue
            if at == [0, 0] and size == [0, 0]:
                continue
            targets[addr] = {"at": at, "size": size}
        if not targets:
            return True
        lines = [LUA_DISPATCH_HELPER, "local geos = {"]
        for addr, geo in targets.items():
            ax, ay = geo["at"]
            sw, sh = geo["size"]
            lines.append(f'  ["{addr}"] = {{at={{{ax},{ay}}}, size={{{sw},{sh}}}}},')
        lines.append("}")
        lines.append(
            f"local ws = hl.get_windows({{ floating = true, "
            f"workspace = {_safe_int(workspace_id, 'workspace_id')} }})"
        )
        lines.append("for _, w in ipairs(ws) do")
        lines.append("  local g = geos[tostring(w.address)]")
        lines.append("  if g then")
        lines.append(
            "    _canvas_dispatch(hl.dispatch(hl.dsp.window.resize({"
            " x = g.size[1], y = g.size[2], relative = false, window = w })))"
        )
        lines.append(
            "    _canvas_dispatch(hl.dispatch(hl.dsp.window.move({"
            " x = g.at[1], y = g.at[2], relative = false, window = w })))"
        )
        lines.append("  end")
        lines.append("end")
        if debug.enabled():
            debug.dbg2(
                "FLOAT_RESTORE",
                ws=workspace_id,
                count=len(targets),
                applied={a: targets[a] for a in sorted(targets)},
            )
        try:
            self._ipc.eval_lua("\n".join(lines))
            return True
        except Exception as e:
            log.warning("restore floating geos failed: %s", e)
            return False

    @staticmethod
    def _toggle_order(snapshot: dict[str, dict[str, list[int]]]) -> list[str]:
        """Order snapshot addresses for tiling: row-major by saved position.

        The compositor rebuilds the tiled layout in toggle order, so feeding
        top-to-bottom, left-to-right approximates the original grid. Entries
        without usable coordinates fall back to plain address order.
        """
        positioned: list[tuple[int, int, str]] = []
        plain: list[str] = []
        for addr in snapshot:
            if not _VALID_ADDR.match(addr):
                continue
            geo = snapshot.get(addr, {})
            at = geo.get("at") if isinstance(geo, dict) else None
            try:
                y, x = int(at[1]), int(at[0])  # type: ignore[index]
                positioned.append((y, x, addr))
            except Exception:
                plain.append(addr)
        positioned.sort()
        return [a for _, _, a in positioned] + sorted(plain)

    def _set_snapshot_floating(
        self,
        workspace_id: int,
        snapshot: dict[str, dict[str, list[int]]],
        floating: bool,
    ) -> bool:
        """Set an exact snapshot to floating/tiled without toggling other windows."""
        ordered = self._toggle_order(snapshot)
        if not ordered:
            return True
        ws_id = _safe_int(workspace_id, "workspace_id")
        action = "enable" if floating else "disable"
        lines = [LUA_DISPATCH_HELPER, "local order = {"]
        lines.extend(f'  "{addr}",' for addr in ordered)
        lines.append("}")
        lines.append(f"local ws = hl.get_windows({{ workspace = {ws_id} }})")
        lines.append("for _, addr in ipairs(order) do")
        lines.append("  for _, w in ipairs(ws) do")
        lines.append("    if tostring(w.address) == addr then")
        lines.append(
            "      _canvas_dispatch(hl.dispatch(hl.dsp.window.float({ "
            f'action = "{action}", window = w }})))'
        )
        lines.append("      break")
        lines.append("    end")
        lines.append("  end")
        lines.append("end")
        try:
            self._ipc.eval_lua("\n".join(lines))
            return True
        except Exception as e:
            log.warning("snapshot floating rollback failed: %s", e)
            return False

    def _tile_windows(self, workspace_id: int, snapshot: dict[str, dict[str, list[int]]]) -> bool:
        """Tile exactly the windows recorded in the snapshot, leaving others floating.

        Plain per-window toggle: tiled placement is layout-owned, so no
        move/resize is attempted here (it would be discarded by the layout
        anyway). Floating geometry is preserved separately and reapplied on
        the next ON.
        """
        ordered = self._toggle_order(snapshot)
        if not ordered:
            if debug.enabled():
                debug.dbg2("TILE_START", ws=workspace_id, targets=0, addrs=[])
            return True
        ws_id = _safe_int(workspace_id, "workspace_id")
        if debug.enabled():
            debug.dbg2(
                "TILE_START",
                ws=workspace_id,
                targets=len(ordered),
                order=ordered,
            )
        if debug.level() >= 2:
            try:
                resp = self._ipc.send("j/clients")
                clients: list[dict[str, Any]] = json.loads(resp)
                live = {
                    w["address"]: {
                        "at": w.get("at"),
                        "size": w.get("size"),
                        "class": str(w.get("class", ""))[:30],
                        "title": str(w.get("title", ""))[:30],
                    }
                    for w in clients
                    if w.get("address") in snapshot
                }
                debug.dbg2("TILE_START_LIVE", ws=workspace_id, live=live)
            except Exception as e:
                debug.dbg2("TILE_START_LIVE_ERROR", ws=workspace_id, error=str(e))
        lines = [LUA_DISPATCH_HELPER, "local order = {"]
        lines.extend(f'  "{a}",' for a in ordered)
        lines.append("}")
        lines.append(f"local ws = hl.get_windows({{ floating = true, workspace = {ws_id} }})")
        lines.append("for _, addr in ipairs(order) do")
        lines.append("  for _, w in ipairs(ws) do")
        lines.append("    if tostring(w.address) == addr then")
        lines.append(
            "      _canvas_dispatch(hl.dispatch(hl.dsp.window.float({ "
            'action = "toggle", window = w })))'
        )
        lines.append("      break")
        lines.append("    end")
        lines.append("  end")
        lines.append("end")
        if debug.enabled():
            debug.dbg2("TILE_LUA", ws=workspace_id, lines=len(lines), preview="; ".join(lines[:2]))
        try:
            self._ipc.eval_lua("\n".join(lines))
            if debug.enabled():
                debug.dbg2("TILE_DONE", ws=workspace_id, targets=len(ordered))
            return True
        except Exception as e:
            log.warning("tile windows failed: %s", e)
            if debug.enabled():
                debug.dbg2("TILE_ERROR", ws=workspace_id, error=str(e))
            return False

    def _set_all_floating(self, workspace_id: int, floating: bool) -> bool:
        """Make every currently-tiled window on the workspace floating (canvas ON).

        The inverse is intentionally NOT done here: turning canvas off must
        tile only the windows recorded in the snapshot (_tile_windows), so
        windows that were already floating before canvas mode survive.
        """
        try:
            ws_id = _safe_int(workspace_id, "workspace_id")
            fl = "false" if floating else "true"
            if debug.enabled():
                try:
                    resp = self._ipc.send("j/clients")
                    clients: list[dict[str, Any]] = json.loads(resp)
                    tiled = [
                        w
                        for w in clients
                        if not w.get("floating")
                        and w.get("workspace", {}).get("id") == workspace_id
                    ]
                    debug.dbg2(
                        "FLOAT_START",
                        ws=workspace_id,
                        count=len(tiled),
                        addrs=sorted([str(w.get("address", "")) for w in tiled]),
                    )
                    if debug.level() >= 2 and tiled:
                        details = {
                            str(w["address"]): {
                                "at": w.get("at"),
                                "size": w.get("size"),
                                "class": str(w.get("class", ""))[:30],
                            }
                            for w in tiled
                            if w.get("address")
                        }
                        debug.dbg2("FLOAT_START_DETAIL", ws=workspace_id, details=details)
                except Exception as e:
                    log.debug("float debug snapshot unavailable: %s", e)
            lua = (
                f"{LUA_DISPATCH_HELPER}\n"
                f"local ws = hl.get_windows({{ floating = {fl}, workspace = {ws_id} }})\n"
                f"for _, w in ipairs(ws) do\n"
                f"  _canvas_dispatch(hl.dispatch(hl.dsp.window.float({{ "
                f'action = "toggle", window = w }})))\n'
                f"end"
            )
            self._ipc.eval_lua(lua)
            if debug.enabled():
                debug.dbg2("FLOAT_DONE", ws=workspace_id, floating=floating)
            return True
        except Exception as e:
            log.warning("set_all_floating failed: %s", e)
            if debug.enabled():
                debug.dbg2("FLOAT_ERROR", ws=workspace_id, error=str(e))
            return False

    def center_window(
        self,
        target_addr: str,
        workspace_id: int,
        cursor_x: int,
        cursor_y: int,
    ) -> bool:
        """Center a live target atomically without changing focus."""
        if not _VALID_ADDR.match(target_addr):
            return False
        center = self._get_monitor_center(cursor_x, cursor_y)
        if center is None:
            return False
        center_x, center_y = center
        safe_x = _safe_int(center_x, "center_x")
        safe_y = _safe_int(center_y, "center_y")
        ws_id = _safe_int(workspace_id, "workspace_id")
        lua = (
            f"{LUA_DISPATCH_HELPER}\n"
            f"local ws = hl.get_windows({{ floating = true, workspace = {ws_id} }})\n"
            f"local target = nil\n"
            f"for _, w in ipairs(ws) do\n"
            f'  if tostring(w.address) == "{target_addr}" then target = w; break end\n'
            f"end\n"
            f'if not target then error("center target no longer exists") end\n'
            f"local target_cx = target.at.x + math.floor(target.size.x / 2)\n"
            f"local target_cy = target.at.y + math.floor(target.size.y / 2)\n"
            f"local dx = {safe_x} - target_cx\n"
            f"local dy = {safe_y} - target_cy\n"
            f"for _, w in ipairs(ws) do\n"
            f"  _canvas_dispatch(hl.dispatch(hl.dsp.window.move({{"
            f" x = dx, y = dy, relative = true, window = w }})))\n"
            f"end"
        )
        try:
            self._ipc.eval_lua(lua)
            return True
        except Exception as e:
            log.warning("center window failed: %s", e)
            return False

    def _is_protected(self, window: dict[str, Any]) -> bool:
        """Check if window class matches a protected app."""
        window_class = window.get("class", "").lower()
        return any(app in window_class for app in self._protected_apps)

    def _pan_to_window(
        self,
        floating_windows: list[dict[str, Any]],
        target_addr: Any,
        center_x: int,
        center_y: int,
        workspace_id: int | None = None,
    ) -> bool:
        """Pan the workspace's floating windows so the target centers on monitor."""
        target = None
        for w in floating_windows:
            if w["address"] == target_addr:
                target = w
                break

        if target is None:
            return False

        target_cx = target["at"][0] + target["size"][0] // 2
        target_cy = target["at"][1] + target["size"][1] // 2

        dx = center_x - target_cx
        dy = center_y - target_cy

        safe_dx = _safe_int(dx, "dx")
        safe_dy = _safe_int(dy, "dy")

        ws_filter = ""
        if workspace_id is not None:
            ws_filter = f", workspace = {_safe_int(workspace_id, 'workspace_id')}"

        lua = (
            f"{LUA_DISPATCH_HELPER}\n"
            f"local ws = hl.get_windows({{ floating = true{ws_filter} }})\n"
            f"for _, w in ipairs(ws) do\n"
            f"  _canvas_dispatch(hl.dispatch(hl.dsp.window.move({{"
            f" x = {safe_dx}, y = {safe_dy},"
            f" relative = true, window = w }})))\n"
            f"end\n"
        )

        # Focus target by iterating floating windows and matching address
        # (avoids Lua injection from class names)
        if not self._is_protected(target):
            addr = target.get("address", "")
            if _VALID_ADDR.match(addr):
                lua += (
                    f"local _t = hl.get_windows({{ floating = true{ws_filter} }})\n"
                    f"for _, w in ipairs(_t) do\n"
                    f'  if tostring(w.address) == "{addr}" then\n'
                    f"    _canvas_dispatch(hl.dispatch(hl.dsp.focus({{ window = w }})))\n"
                    f"    break\n"
                    f"  end\n"
                    f"end\n"
                )

        try:
            self._ipc.eval_lua(lua)
            return True
        except Exception as e:
            log.warning("navigation pan failed: %s", e)
            return False

    def _get_active_workspace_id(self) -> int | None:
        try:
            resp = self._ipc.send("j/activeworkspace")
            ws: dict[str, Any] = json.loads(resp)
            return int(ws["id"])
        except Exception as e:
            log.debug("get_active_workspace_id failed: %s", e)
            return None

    def _get_floating_windows(self, workspace_id: int) -> list[dict[str, Any]] | None:
        try:
            resp = self._ipc.send("j/clients")
            clients: list[dict[str, Any]] = json.loads(resp)
            return [
                w
                for w in clients
                if w.get("floating")
                and (ws := w.get("workspace")) is not None
                and ws.get("id") == workspace_id
            ]
        except Exception as e:
            log.debug("get_floating_windows failed: %s", e)
            return None

    def _get_focused_window(self) -> dict[str, Any] | None:
        try:
            resp = self._ipc.send("j/activewindow")
            result: dict[str, Any] = json.loads(resp)
            return result
        except Exception as e:
            log.debug("get_focused_window failed: %s", e)
            return None

    def _get_monitor_center(
        self, cursor_x: int | None = None, cursor_y: int | None = None
    ) -> tuple[int, int] | None:
        try:
            resp = self._ipc.send("j/monitors")
            monitors: list[dict[str, Any]] = json.loads(resp)
            parsed: list[tuple[dict[str, Any], int, int, int, int]] = []
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
                parsed.append((monitor, x, y, width, height))

            if cursor_x is not None and cursor_y is not None:
                for _monitor, x, y, width, height in parsed:
                    if x <= cursor_x < x + width and y <= cursor_y < y + height:
                        return x + width // 2, y + height // 2
                return None

            for monitor, x, y, width, height in parsed:
                if monitor.get("focused", False):
                    return x + width // 2, y + height // 2
            if parsed:
                _monitor, x, y, width, height = parsed[0]
                return x + width // 2, y + height // 2
        except Exception as e:
            log.debug("get_monitor_center failed: %s", e)
        return None
