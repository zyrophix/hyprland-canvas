# Debugging

Run the daemon with structured tracing to diagnose input/camera issues:

```bash
CANVAS_DEBUG=1 canvasd 2>&1 | tee /tmp/canvas-debug.log   # summary
CANVAS_DEBUG=2 canvasd 2>&1 | tee /tmp/canvas-debug.log   # per-window details (class/title/at/size)
```

Trace lines are `<seconds> EVENT key=value …`. `CANVAS_DEBUG=1` emits only
these eight:

- `BOOT` — daemon start, with `pid`
- `CMD` — every IPC command with `cmd` + `result`
- `EDGE_START_DECISION`, `EDGE_CONFIRMED`, `EDGE_DISARM`, `EDGE_STOP` — edge-scroll
  arming, drag confirmation, disarm reason, teardown
- `EDGE_SESSION_TICK` (10Hz) — live window geometry during an edge-scroll
- `PAN_IDLE_TIMEOUT` — panning auto-stopped because the cursor went still

Everything else needs `CANVAS_DEBUG=2`, which is a superset — level 1 output is
included:

- `SNAPSHOT_CREATE` — `ws, count, addrs, preserve_geometry`
- `TOGGLE_ON` / `TOGGLE_OFF` — `ws, count, addrs` on canvas-toggle
- `CANVAS_GEOMETRY` — on canvas ON, which windows took panned / spawn-sized / tiled geometry
- `TILE_START` / `FLOAT_START` — `ws, targets` before Lua dispatch; `TILE_LUA`,
  `TILE_DONE` / `FLOAT_DONE` after it
- `STATE_LOAD` / `STATE_SAVE` — toggle snapshot counts per workspace
- `SPAWN_RULES_REGISTER` / `SPAWN_RULES_DISABLE` / `SPAWN_RULES_DROP` —
  compositor windowrules for `canvas.auto_float`, with the names used; the last
  one is the take-down on daemon exit
- `PAN_START` / `PAN_STOP` / `MODE_SWITCH` — panning and mode hand-off
- `CENTER_CURSOR`, `FLOATING_SNAPSHOT` — center-cursor and snapshot internals
- `*_DETAIL` — `SNAPSHOT_CREATE_DETAIL`, `TOGGLE_ON_DETAIL`, `TOGGLE_OFF_DETAIL`,
  `STATE_LOAD_DETAIL`, `STATE_SAVE_DETAIL`, `FLOAT_START_DETAIL`, `TILE_START_LIVE`,
  `FLOAT_RESTORE` — live vs saved geometry with `class/title` (truncated to 30-40 chars)
- `*_ERROR` — `TILE_ERROR`, `FLOAT_ERROR`, `TILE_START_LIVE_ERROR`,
  `STATE_LOAD_ERROR`, `STATE_SAVE_ERROR`, `SPAWN_RULES_DISABLE_ERROR`

If spawn sizing seems to do nothing, run with `CANVAS_DEBUG=2` and check that
`SPAWN_RULES_REGISTER` appears after `canvas-toggle`; if it does not,
`canvas.auto_float` is off or the workarea could not be resolved.
`EDGE_DISARM` with `reason=cursor_left` only disarms before the drag is
confirmed — see [architecture.md](architecture.md#geometry-on-canvas-on).
