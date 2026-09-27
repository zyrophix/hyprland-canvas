"""Persistence for canvas-toggle snapshots.

Per workspace two sections:
- "tiled": addresses that were tiled before canvas mode was enabled.
  Only these are tiled again on OFF. Geometry here is informational
  (tiled placement is layout-owned) and only drives toggle ordering.
- "floating": last known floating geometry per address. Reapplied on
  the next ON so the canvas comes back where it was.

Written under XDG_RUNTIME_DIR (tmpfs): survives daemon restarts within
a session, resets on reboot — matching the session-scoped nature of
canvas mode itself.

Format v4 on disk: {"_v": 4, "<ws>": {"active": bool, "tiled": {...},
"floating": {...}, "pre_floating": [...], "spawn_rules": [...]}}. The active
bit distinguishes an all-floating canvas workspace from saved geometry for a
workspace whose canvas mode is OFF.
- "pre_floating": addresses that were already floating at ON. Windows floating
  at OFF but absent from this list arrived during canvas and are tiled back.
- "spawn_rules": compositor windowrule names registered for this workspace.
  The compositor keeps them beyond our lifetime and cannot list them, so the
  names are stored here and disabled again on OFF or at the next startup.
Older files are auto-migrated on load; v2 active state is inferred from
whether its tiled snapshot is non-empty, and both new sections default to
empty for every earlier version.
"""

import json
import logging
import os
from contextlib import suppress
from typing import Any, TypedDict

from canvas import debug

log = logging.getLogger("canvas.toggle")

FORMAT_VERSION = 4


class ToggleStateError(RuntimeError):
    """Canvas toggle state could not be persisted."""


Snapshot = dict[str, dict[str, list[int]]]


class WorkspaceState(TypedDict):
    active: bool
    tiled: Snapshot
    floating: Snapshot
    pre_floating: list[str]
    spawn_rules: list[str]


State = dict[int, WorkspaceState]


def _empty_workspace() -> WorkspaceState:
    return {"active": False, "tiled": {}, "floating": {}, "pre_floating": [], "spawn_rules": []}


def _parse_addresses(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return sorted({str(a) for a in raw if isinstance(a, str)})


def default_path() -> str:
    uid = os.getuid()
    run_dir = f"/run/user/{uid}"
    base = (
        run_dir
        if os.path.isdir(run_dir)
        else os.environ.get("XDG_RUNTIME_DIR", f"/tmp/user/{uid}")
    )
    return os.path.join(base, "canvas", "toggle-state.json")


def _parse_snapshot(raw_snap: Any) -> Snapshot:
    if isinstance(raw_snap, list):
        # Old format: list of addresses — geometry unknown
        return {str(a): {} for a in raw_snap if isinstance(a, str)}
    if isinstance(raw_snap, dict):
        out: Snapshot = {}
        for addr, geo in raw_snap.items():
            if not isinstance(addr, str):
                continue
            if isinstance(geo, dict) and "at" in geo and "size" in geo:
                try:
                    at = [int(geo["at"][0]), int(geo["at"][1])]
                    size = [int(geo["size"][0]), int(geo["size"][1])]
                    out[addr] = {"at": at, "size": size}
                except Exception:
                    out[addr] = {}
            elif isinstance(geo, (dict, str)):
                out[addr] = {}
        return out
    return {}


def _parse_workspace(raw_ws: Any) -> WorkspaceState:
    """Parse one workspace entry, migrating old formats.

    v4 adds pre_floating and spawn_rules. v3 adds an explicit active bit. v2
    sections are accepted with active inferred from a non-empty tiled snapshot.
    Older list/bare-dict formats: addresses are
    kept for OFF targeting, geometry is dropped — old geos describe tiled
    slots, which must never be applied as floating positions.

    The version is deliberately not a parameter. This used to branch on the
    file's `_v`, and a v4 file with `_v` missing, unparsable or negative fell
    into the oldest branch: it read the section names as if they were window
    addresses and dropped the geometry, so the canvas came back wrong and the
    next save wrote five junk addresses into the file. Shape says which format
    this is far more reliably than a field we wrote ourselves can be trusted to
    have survived.
    """
    if isinstance(raw_ws, dict) and ("tiled" in raw_ws or "floating" in raw_ws):
        tiled = _parse_snapshot(raw_ws.get("tiled", {}))
        floating = _parse_snapshot(raw_ws.get("floating", {}))
        # v2 had no explicit active bit. Non-empty tiled snapshots were the only
        # states that could represent an active canvas mode. v3 stores it directly.
        active = raw_ws.get("active")
        return {
            "active": active if isinstance(active, bool) else bool(tiled),
            "tiled": tiled,
            "floating": floating,
            "pre_floating": _parse_addresses(raw_ws.get("pre_floating")),
            "spawn_rules": _parse_addresses(raw_ws.get("spawn_rules")),
        }
    addrs: set[str] = set()
    if isinstance(raw_ws, list):
        addrs = {str(a) for a in raw_ws if isinstance(a, str)}
    elif isinstance(raw_ws, dict):
        for addr in raw_ws:
            if isinstance(addr, str):
                addrs.add(addr)
    return {
        "active": bool(addrs),
        "tiled": {a: {} for a in addrs},
        "floating": {},
        "pre_floating": [],
        "spawn_rules": [],
    }


def load(path: str | None = None) -> State:
    """Load saved snapshots. Missing or corrupt file yields an empty state."""
    file = path or default_path()
    try:
        with open(file) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("state must be an object")
        try:
            version = int(raw.get("_v", 0))
        except Exception:
            version = 0
        if version > FORMAT_VERSION:
            # Readable but not ours: the parsers below would silently interpret
            # a newer layout as v4, and the next save would overwrite the file
            # with v4, destroying whatever the newer version added. Say so loudly
            # rather than losing a future release's state without explanation.
            log.warning(
                "state file format v%s is newer than v%s — it will be read as v%s "
                "and rewritten on the next save, losing whatever v%s added",
                version,
                FORMAT_VERSION,
                FORMAT_VERSION,
                version,
            )
        state: State = {}
        for k, v in raw.items():
            if k == "_v":
                continue
            try:
                ws_id = int(k)
            except Exception:
                continue
            state[ws_id] = _parse_workspace(v)
        if debug.enabled():
            debug.dbg2(
                "STATE_LOAD",
                path=file,
                workspaces=sorted(state.keys()),
                counts={
                    ws: {
                        "tiled": len(s.get("tiled", {})),
                        "floating": len(s.get("floating", {})),
                    }
                    for ws, s in state.items()
                },
            )
            if debug.level() >= 2 and state:
                debug.dbg2("STATE_LOAD_DETAIL", state=state)
        return state
    except FileNotFoundError:
        if debug.enabled():
            debug.dbg2("STATE_LOAD", path=file, workspaces=[], counts={})
        return {}
    except Exception as e:
        log.warning("could not read toggle state %s: %s", file, e)
        if debug.enabled():
            debug.dbg2("STATE_LOAD_ERROR", path=file, error=str(e))
        return {}


def save(state: State, path: str | None = None) -> None:
    """Atomically persist snapshots in the current on-disk format."""
    file = path or default_path()
    tmp = file + ".tmp"
    try:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        # Sort for deterministic output
        serializable: dict[str, Any] = {"_v": FORMAT_VERSION}
        for ws, sections in sorted(state.items()):
            tiled = sections.get("tiled", {})
            serializable[str(ws)] = {
                "active": bool(sections.get("active", bool(tiled))),
                "tiled": {addr: geo for addr, geo in sorted(tiled.items())},
                "floating": {
                    addr: geo for addr, geo in sorted(sections.get("floating", {}).items())
                },
                "pre_floating": sorted(set(sections.get("pre_floating", []))),
                "spawn_rules": list(sections.get("spawn_rules", [])),
            }
        with open(tmp, "w") as f:
            json.dump(serializable, f)
        os.replace(tmp, file)
        if debug.enabled():
            debug.dbg2(
                "STATE_SAVE",
                path=file,
                workspaces=sorted(state.keys()),
                counts={
                    ws: {
                        "tiled": len(s.get("tiled", {})),
                        "floating": len(s.get("floating", {})),
                    }
                    for ws, s in state.items()
                },
            )
            if debug.level() >= 2 and state:
                debug.dbg2("STATE_SAVE_DETAIL", state=state)
    except Exception as e:
        with suppress(OSError):
            os.unlink(tmp)
        log.warning("could not write toggle state %s: %s", file, e)
        if debug.enabled():
            debug.dbg2("STATE_SAVE_ERROR", path=file, error=str(e))
        raise ToggleStateError(f"could not persist toggle state to {file}") from e
