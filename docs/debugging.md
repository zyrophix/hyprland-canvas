# Debugging

Run the daemon with structured tracing to diagnose input/camera issues:

```bash
CANVAS_DEBUG=1 canvasd 2>&1 | tee /tmp/canvas-debug.log   # summary
CANVAS_DEBUG=2 canvasd 2>&1 | tee /tmp/canvas-debug.log   # per-window details (class/title/at/size)
```

Trace lines are `<seconds> EVENT key=value …`, with no logger prefix; the
daemon's own warnings keep theirs, so the two are easy to tell apart. Values are
written the way the daemon holds them rather than as Python literals —
`confirmed=true`, `geo=1,2,3,4`, `addr=none` — and a string containing a space is
quoted, so it stays one field.

Two more variables shape the output. `CANVAS_DEBUG_FILTER=EDGE` keeps only events
whose name contains the substring, ignoring case, which is usually what you
want: the edge-scroll tick alone is ten lines a second.
`CANVAS_DEBUG_FORMAT=json` turns the whole stream into one JSON object per line,
so it can be piped straight into `jq`: trace lines carry `ts`, `event` and
`fields`, and the daemon's own records `ts`, `level`, `logger` and `message`.
`ts` is seconds since the daemon set tracing up, not a wall clock, so it is for
ordering and correlation, not for timestamps on their own.

`CANVAS_DEBUG=1` emits only these eight:

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
- `CANVAS_RESET` — `ws`, when the saved canvas layout for a workspace is forgotten
- `CANVAS_GEOMETRY` — on canvas ON, which windows took panned / spawn-sized / tiled geometry
- `TILE_START` / `FLOAT_START` — `ws, targets` before Lua dispatch; `TILE_LUA`,
  `TILE_DONE` / `FLOAT_DONE` after it
- `STATE_LOAD` / `STATE_SAVE` — toggle snapshot counts per workspace
- `SPAWN_RULES_REGISTER` / `SPAWN_RULES_DISABLE` / `SPAWN_RULES_DROP` —
  compositor windowrules for `canvas.auto_float`, with the name used; the last
  one is the take-down on daemon exit. `SPAWN_RULES_REGISTER` is re-sent once a
  second while canvas mode is on, so it marks the check, not a newly created
  rule: the compositor only recreates a rule whose marker is gone.
- `PAN_START` / `PAN_STOP` / `MODE_SWITCH` — panning and mode hand-off
- `CENTER_CURSOR`, `FLOATING_SNAPSHOT` — center-cursor and snapshot internals
- `*_DETAIL` — `SNAPSHOT_CREATE_DETAIL`, `TOGGLE_ON_DETAIL`, `TOGGLE_OFF_DETAIL`,
  `STATE_LOAD_DETAIL`, `STATE_SAVE_DETAIL`, `FLOAT_START_DETAIL`, `TILE_START_LIVE`,
  `FLOAT_RESTORE` — live vs saved geometry with `class/title` (truncated to 30-40 chars)
- `*_ERROR` — `TILE_ERROR`, `FLOAT_ERROR`, `TILE_START_LIVE_ERROR`,
  `STATE_LOAD_ERROR`, `STATE_SAVE_ERROR`, `SPAWN_RULES_DISABLE_ERROR`

If spawn sizing seems to do nothing, run with `CANVAS_DEBUG=2` and check that
`SPAWN_RULES_REGISTER` appears; if it does not, `canvas.auto_float` is off or
the workarea could not be resolved. The line is re-sent once a second while
canvas mode is on, so its presence says the check ran, not that a rule was
newly created.
`EDGE_DISARM` with `reason=cursor_left` only disarms before the drag is
confirmed — see [architecture.md](architecture.md#geometry-on-canvas-on).

## Reading a JSON run

`CANVAS_DEBUG_FORMAT=json` makes the whole stream one object per line, so a
recorded run can be queried instead of paged through. These fields are
diagnostic, not an interface: they may change between releases. The recipes
assume every line in the file is JSON — output printed outside logging, or a
crash, would make `jq` stop at that line.

These are the questions that come up most:

```bash
# how many of each event
jq -s 'map(select(has("event"))) | group_by(.event) | map({event: .[0].event, n: length})' run.jsonl

# why edge-scroll sessions ended
jq -r 'select(.event == "EDGE_DISARM") | .fields.reason' run.jsonl | sort | uniq -c

# how often the drag bind's release re-run was caught
jq -r 'select(.event == "EDGE_START_DECISION" and .fields.verdict == "DUPLICATE_AFTER_STOP") | .ts' run.jsonl | wc -l
```

The last one is how the guard's 100 ms window is checked against real gestures
rather than guessed. The daemon's own records have `level`, `logger` and
`message` instead of `event`, which is why the first recipe filters on `event`.
