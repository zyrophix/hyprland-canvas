"""Spawn-time windowrules for canvas mode.

Windows opened while a workspace is in canvas mode are floated and sized by
the compositor itself: rules are registered from Lua and applied at map time.
Rules only affect windows created *after* registration, which is exactly the
semantics we want — no poller, no socket listener, no per-window tracking.

Ordering matters: the compositor applies every matching rule in registration
order and the last write wins, so the catch-all default is registered first
and user overrides after it.

The compositor keeps rules in a global engine that outlives this process and
offers no way to list them, so the names registered here are persisted next
to the toggle state and disabled again on OFF (and at startup, as a net for
rules left behind by a crash).
"""

import json
import logging
import re
from functools import lru_cache
from typing import Any

from canvas import debug
from canvas.hypr import HyprIPC

log = logging.getLogger("canvas.spawnrules")

RULE_PREFIX = "canvas-spawn-"

# Workarea as (x, y, width, height) in layout coordinates.
Workarea = tuple[int, int, int, int]

# Mirror of Hyprland's matchPropFromString table. An unknown key is recorded
# by the compositor as a config error instead of being raised, so it is
# rejected here where we can still fail loudly.
MATCH_KEYS = frozenset(
    {
        "class",
        "content",
        "exec",
        "floating",
        "focus",
        "fullscreen",
        "group",
        "initial_class",
        "initial_title",
        "modal",
        "namespace",
        "pin",
        "pid",
        "title",
        "workspace",
        "xwayland",
        "xdg_tag",
    }
)

# "910x930" is pixels, "30%x40%" is a fraction of the workarea. The unit
# follows each number, and the trailing backreference forces both to agree, so
# a mixed spec like "910x40%" cannot be written.
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)(%?)x(\d+(?:\.\d+)?)(\2)$")

# Wraps each rule so a rejected rule raises instead of failing silently, and
# so enabling is explicit (a rule created without "enabled" stays off).
_REGISTER_HELPER = """
local function _canvas_spawn_reg(t)
  local r = hl.window_rule(t)
  if not r then
    error("canvas: window_rule rejected")
  end
  r:set_enabled(true)
  return r
end
""".strip()


class SpawnRuleError(RuntimeError):
    """Spawn windowrules are invalid or could not be registered."""


def default_rule_name(ws_id: int) -> str:
    return f"{RULE_PREFIX}ws{ws_id}-default"


def override_rule_name(ws_id: int, index: int) -> str:
    return f"{RULE_PREFIX}ws{ws_id}-override-{index}"


def rule_names(ws_id: int, overrides: int) -> list[str]:
    """Deterministic name set for one workspace, in registration order."""
    return [default_rule_name(ws_id)] + [override_rule_name(ws_id, i) for i in range(overrides)]


def size_spec_error(spec: object) -> str | None:
    """Return a problem description for a size spec, or None when it is valid."""
    if not isinstance(spec, str) or not spec.strip():
        return "must be a non-empty size string like 910x930 or 30%x40%"
    m = _SIZE_RE.match(spec.strip())
    if not m:
        return f"{spec!r} is not a size like 910x930 or 30%x40%"
    percent = m.group(2) == "%"
    for raw in (m.group(1), m.group(3)):
        value = float(raw)
        if value <= 0:
            return f"{spec!r} dimensions must be greater than zero"
        if percent and value > 100:
            return f"{spec!r} percent values must not exceed 100"
    return None


def parse_size(spec: str, workarea: Workarea) -> tuple[int, int]:
    """Resolve a size spec to pixel dimensions bounded by the workarea.

    "910x930" is used as-is; "30%x40%" is a fraction of the workarea width
    and height. Results are clamped to the workarea so a rule can never ask
    for a window larger than the screen it lands on.
    """
    problem = size_spec_error(spec)
    if problem:
        raise SpawnRuleError(f"canvas.spawn size: {problem}")
    assert isinstance(spec, str)
    m = _SIZE_RE.match(spec.strip())
    assert m is not None
    _wx, _wy, width, height = workarea
    if m.group(2) == "%":
        w = int(float(m.group(1)) / 100.0 * width)
        h = int(float(m.group(3)) / 100.0 * height)
    else:
        w = int(m.group(1))
        h = int(m.group(3))
    return max(1, min(w, width)), max(1, min(h, height))


def _lua_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    raise SpawnRuleError(f"match value must be string, bool or number: {value!r}")


def _match_table(match: object) -> str:
    if not isinstance(match, dict) or not match:
        raise SpawnRuleError("each spawn rule needs a non-empty 'match' mapping")
    parts: list[str] = []
    for key in sorted(match):
        if key not in MATCH_KEYS:
            raise SpawnRuleError(
                f"unknown match property {key!r}; expected one of {', '.join(sorted(MATCH_KEYS))}"
            )
        parts.append(f"{key} = {_lua_value(match[key])}")
    return "{ " + ", ".join(parts) + " }"


def _rule_lua(name: str, match: object, spec: str, workarea: Workarea, center: bool) -> str:
    width, height = parse_size(spec, workarea)
    fields = [
        f'name = "{name}"',
        f"match = {_match_table(match)}",
        f"size = {{ {width}, {height} }}",
        "float = true",
    ]
    if center:
        fields.append("center = true")
    # immediate + no_anim keep the compositor from animating the placement.
    # no_dim/no_shadow/no_blur stop the window from being treated as special,
    # so a canvas window looks like an ordinary one.
    fields += ["immediate = true", "no_anim = true", "no_dim = true", "no_shadow = true"]
    return f"_canvas_spawn_reg{{ {', '.join(fields)} }}"


def build_rules(
    spawn_cfg: dict[str, Any],
    ws_id: int,
    workarea: Workarea,
) -> list[tuple[str, str]]:
    """Build (name, lua) pairs for one workspace, catch-all first.

    The catch-all matches any class; user overrides follow in config order and
    win because the compositor applies matching rules in registration order.
    """
    center = bool(spawn_cfg.get("center", True))
    rules: list[tuple[str, str]] = []
    name = default_rule_name(ws_id)
    rules.append(
        (name, _rule_lua(name, {"class": ".*"}, spawn_cfg.get("default", ""), workarea, center))
    )

    raw_rules = spawn_cfg.get("rules", [])
    if not isinstance(raw_rules, list):
        raise SpawnRuleError("canvas.spawn.rules must be a list")
    for index, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            raise SpawnRuleError(f"canvas.spawn.rules[{index}] must be a mapping")
        name = override_rule_name(ws_id, index)
        spec = entry.get("size")
        if size_spec_error(spec):
            raise SpawnRuleError(f"canvas.spawn.rules[{index}].size: {size_spec_error(spec)}")
        rules.append((name, _rule_lua(name, entry.get("match"), str(spec), workarea, center)))
    return rules


_NEGATIVE_PREFIX = "negative:"

# Properties we can evaluate locally from j/clients. A rule that also mentions
# something else (workspace, pid, floating, ...) is still handed to the
# compositor for new windows, but it cannot be applied to already open ones.
_LOCAL_MATCH_KEYS = frozenset({"class", "title"})


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def full_match(pattern: str, value: str) -> bool:
    """Match the way Hyprland's regex engine does.

    Hyprland uses re2::RE2::FullMatch, so a rule on class "btop" only matches
    that exact class — a substring needs ".*btop.*". It also supports a
    "negative:" prefix. Python's re is close enough for these patterns; a
    pattern it cannot compile is reported and ignored rather than guessed at.
    """
    negative = pattern.startswith(_NEGATIVE_PREFIX)
    body = pattern[len(_NEGATIVE_PREFIX) :] if negative else pattern
    compiled = _compile(body)
    if compiled is None:
        log.warning("spawn match %r is not a valid regex, ignoring", body)
        return False
    hit = compiled.fullmatch(value) is not None
    return not hit if negative else hit


def resolve_spec(spawn_cfg: dict[str, Any], props: dict[str, str]) -> str:
    """Pick the size spec for one already open window.

    Same first-match-wins order as the registered rules, so an existing window
    and a freshly opened one end up the same size.
    """
    default = str(spawn_cfg.get("default", ""))
    for entry in spawn_cfg.get("rules", []):
        if not isinstance(entry, dict):
            continue
        match = entry.get("match")
        if not isinstance(match, dict) or not match:
            continue
        if not all(key in _LOCAL_MATCH_KEYS for key in match):
            continue
        if all(full_match(str(pattern), props.get(key, "")) for key, pattern in match.items()):
            spec = entry.get("size")
            if size_spec_error(spec) is None:
                return str(spec)
    return default


def resolve_workareas(ipc: HyprIPC) -> dict[int, Workarea]:
    """Map workspace id to the workarea of the monitor that owns it.

    Percent sizes need a concrete pixel budget, and the only correct budget is
    the one belonging to the monitor the window will actually appear on.
    """
    try:
        monitors = json.loads(ipc.send("j/monitors"))
        workspaces = json.loads(ipc.send("j/workspaces"))
    except Exception as e:
        log.warning("spawn workarea lookup failed: %s", e)
        return {}
    by_monitor: dict[int, Workarea] = {}
    for monitor in monitors:
        try:
            mid = int(monitor["id"])
            x = int(monitor["x"])
            y = int(monitor["y"])
            width = int(monitor["width"])
            height = int(monitor["height"])
        except (KeyError, TypeError, ValueError):
            continue
        reserved = monitor.get("reserved") or [0, 0, 0, 0]
        try:
            top, right, bottom, left = (int(v) for v in list(reserved)[:4])
        except (TypeError, ValueError):
            top = right = bottom = left = 0
        by_monitor[mid] = (
            x + left,
            y + top,
            max(1, width - left - right),
            max(1, height - top - bottom),
        )
    out: dict[int, Workarea] = {}
    for workspace in workspaces:
        try:
            ws_id = int(workspace["id"])
            ws_monitor = int(workspace["monitorID"])
        except (KeyError, TypeError, ValueError):
            continue
        workarea = by_monitor.get(ws_monitor)
        if workarea is not None:
            out[ws_id] = workarea
    return out


def register(rules: list[tuple[str, str]], ipc: HyprIPC) -> list[str]:
    """Register rules in one IPC round trip. Returns the registered names."""
    if not rules:
        return []
    lines = [_REGISTER_HELPER]
    lines += [snippet for _name, snippet in rules]
    try:
        ipc.eval_lua("\n".join(lines))
    except Exception as e:
        log.warning("spawn rule registration failed: %s", e)
        # A batch that aborts halfway leaves earlier rules live; undo them so a
        # failed toggle cannot leak canvas sizing into a normal workspace.
        disable([name for name, _ in rules], ipc)
        raise SpawnRuleError(f"could not register spawn rules: {e}") from e
    names = [name for name, _ in rules]
    if debug.enabled():
        debug.dbg2("SPAWN_RULES_REGISTER", count=len(names), names=names)
    return names


def disable(names: list[str], ipc: HyprIPC) -> bool:
    """Disable previously registered rules. Best effort; True when all ran."""
    if not names:
        return True
    lines = [f'hl.window_rule{{ name = "{name}", enabled = false }}' for name in names]
    try:
        ipc.eval_lua("\n".join(lines))
    except Exception as e:
        log.warning("spawn rule disable failed: %s", e)
        if debug.enabled():
            debug.dbg2("SPAWN_RULES_DISABLE_ERROR", names=names, error=str(e))
        return False
    if debug.enabled():
        debug.dbg2("SPAWN_RULES_DISABLE", count=len(names), names=names)
    return True
