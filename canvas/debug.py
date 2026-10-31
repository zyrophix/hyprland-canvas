"""Structured debug tracing controlled by CANVAS_DEBUG.

`dbg()` emits `<seconds> EVENT k=v k=v` at DEBUG level only while tracing is on;
when it is off the call returns before any formatting, so hot paths stay free.

Three environment variables shape the output:

- `CANVAS_DEBUG=1` — summary events, `2` — also the per-window ones.
- `CANVAS_DEBUG_FILTER` — keep only events whose name contains this substring
  (case-insensitive), which is how the 10 Hz edge-scroll tick stays out of the
  way.
- `CANVAS_DEBUG_FORMAT=json` — one JSON object per line instead of the text form.

Values are rendered rather than repr'd: tuples as `1,2,3,4`, booleans as
`true`/`false`. A line is meant to be read and grepped by a person, not parsed
back with `ast.literal_eval`.
"""

import json
import logging
import math
import os
import time
from typing import Any

log = logging.getLogger("canvas.debug")

_level = 0
_start = 0.0
_filter = ""
_json = False
_configured = False
_handler: logging.Handler | None = None


def enabled() -> bool:
    return _level > 0


def level() -> int:
    return _level


def configure() -> None:
    """Give this logger a bare handler so trace lines carry no logger name.

    `run()` formats the root logger as `"%(name)s: %(message)s"`, which is right
    for warnings and wrong for the trace: every line would begin with
    `canvas.debug: `, while the documented line starts with the timestamp.
    """
    global _configured, _handler
    if _configured:
        return
    _configured = True
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_handler)
    log.propagate = False


def enable_from_env() -> None:
    """Enable tracing: CANVAS_DEBUG=1 (summary) or 2 (per-window)."""
    global _level, _start, _filter, _json
    raw = os.environ.get("CANVAS_DEBUG", "").lower()
    if raw in ("", "0", "false"):
        _level = 0
    elif raw in ("2", "verbose", "debug"):
        _level = 2
    else:
        _level = 1
    _filter = os.environ.get("CANVAS_DEBUG_FILTER", "").upper()
    _json = os.environ.get("CANVAS_DEBUG_FORMAT", "").lower() == "json"
    if _level:
        _start = time.monotonic()


def reset_for_tests() -> None:
    global _level, _start, _filter, _json, _configured, _handler
    _level = 0
    _start = 0.0
    _filter = ""
    _json = False
    if _handler is not None:
        log.removeHandler(_handler)
        _handler = None
    _configured = False
    log.propagate = True


def _render(value: object) -> str:
    """One field value in the text format: compact, and not Python's repr."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # A space would split the key=value pair in two; keep non-ASCII as-is,
        # because a window title is worth reading and not worth escaping.
        return (
            json.dumps(value, ensure_ascii=False)
            if not value or any(c.isspace() for c in value)
            else value
        )
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_render(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple, set)):
        return ",".join(_render(v) for v in value)
    return str(value)


def _jsonable(value: object) -> Any:
    """The same values, but keeping JSON's own types where they exist."""
    if isinstance(value, float) and not math.isfinite(value):
        # `json.dumps` writes NaN/Infinity, which are not valid JSON, so a
        # consumer like jq would choke on the line.
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _emit(event: str, fields: dict[str, object]) -> None:
    if _filter and _filter not in event:
        return
    elapsed = time.monotonic() - _start
    if _json:
        payload = {"ts": round(elapsed, 3), "event": event, "fields": _jsonable(fields)}
        log.debug(json.dumps(payload, sort_keys=False))
        return
    parts = [f"{elapsed:.3f}", event]
    parts.extend(f"{key}={_render(value)}" for key, value in fields.items())
    log.debug(" ".join(parts))


def dbg(event: str, **fields: object) -> None:
    """Emit a structured trace line; no-op when tracing is off."""
    if not _level:
        return
    _emit(event, fields)


def dbg2(event: str, **fields: object) -> None:
    """Verbose per-window trace — only when CANVAS_DEBUG=2."""
    if _level < 2:
        return
    _emit(event, fields)
