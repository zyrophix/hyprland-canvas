# hyprland-canvas

Pan floating windows like an infinite desktop on Hyprland.

[![CI](https://img.shields.io/github/actions/workflow/status/zyrophix/hyprland-canvas/ci.yml)](https://github.com/zyrophix/hyprland-canvas/actions)
[![Release](https://img.shields.io/github/v/release/zyrophix/hyprland-canvas)](https://github.com/zyrophix/hyprland-canvas/releases)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Drag the canvas with **SUPER+SHIFT+LMB**, navigate between windows, toggle canvas mode per workspace. Runs as an unprivileged user daemon — communicates directly with Hyprland via its IPC socket and Lua API.

<video src="https://github.com/user-attachments/assets/6bb06c3e-c553-481d-b726-15033ed8ac37" autoplay loop muted playsinline width="900">Demo: panning floating windows as an infinite desktop</video>

## Why

Hyprland has no built-in infinite desktop. This daemon provides one by communicating with Hyprland the right way:

- **Direct Unix socket IPC** to Hyprland — no per-request subprocess startup
- **Hyprland Lua API** (`hl.dsp.window.move`) moves windows without focusing them — no cursor warp or flicker
- Runs as an unprivileged user daemon — no special permissions needed
- Has a **Unix socket IPC** for keybind-driven commands (navigate, center, toggle, invert)

Honest limits: no render-level zoom (windows move, nothing scales), no touchpad gestures, no resize/move of tiled windows — pan, navigate, center, toggle and size, nothing else.

Features here get added when they look useful and removed when they turn out
not to, so expect breaking changes.

## Features

| Feature | Keybind | Description |
| --- | --- | --- |
| Pan canvas | SUPER+SHIFT+LMB | Drag to pan all floating windows |
| Edge-scroll | SUPER+LMB | Drag a floating window toward the screen edge — camera follows (engages only for a confirmed drag of the window under the cursor) |
| Navigate | SUPER+SHIFT+Arrows | Spatial jump to nearest window in direction (up/down/left/right), auto-pan to center |
| Center under cursor | SUPER+MMB | Center the canvas on the topmost floating window under the mouse cursor without changing focus |
| Canvas toggle | SUPER+SHIFT+C | Toggle all windows on workspace to/from floating, keeping each window exactly where and how big it was |
| Window sizing | on canvas enable | Give windows that open while canvas is on a size — one default, on by default (see [Sizing windows that open during canvas](#sizing-windows-that-open-during-canvas)) |
| Toggle single | SUPER+SHIFT+V | Toggle focused window floating ↔ tiled |
| Invert | SUPER+SHIFT+G | Invert pan direction |

## Install

Requires: Hyprland 0.55+ (Lua config with `hl.*` API), Python 3.12+, `uv`, `pipx`, or Arch `makepkg`.

**uv (recommended):**

```bash
git clone https://github.com/zyrophix/hyprland-canvas.git
cd hyprland-canvas
uv tool install .
```

**pipx:**

```bash
git clone https://github.com/zyrophix/hyprland-canvas.git
cd hyprland-canvas
pipx install .
```

**Arch Linux (makepkg):**

```bash
git clone https://github.com/zyrophix/hyprland-canvas.git
cd hyprland-canvas/packaging/arch
makepkg -si
```

**Run from source (no install):**

```bash
git clone https://github.com/zyrophix/hyprland-canvas.git
cd hyprland-canvas
uv run canvasd
```

After pulling new code, reinstall and restart the daemon — an old installed copy keeps running until you do:

```bash
git pull
uv tool install . --force --reinstall   # or: pipx install . --force
```

## Quickstart

```bash
canvasd &            # 1. start the daemon
canvas-ctl ping      # 2. check it answers
```

Expected output:

```text
PONG
```

```bash
canvas-ctl status    # 3. show daemon state
```

Then add the Hyprland keybinds from [Usage](#usage) and drag with SUPER+SHIFT+LMB.

## Usage

### 1. Start the daemon

```bash
canvasd
```

The daemon exits non-zero if its cursor poller dies, so that a supervisor
restarts it instead of leaving a process that still answers `ping` but can never
pan again. `examples/hypr-canvasd.service` is a ready-made systemd user unit
for that. It is an example only — no package installs or enables a service:

```bash
install -Dm644 examples/hypr-canvasd.service ~/.config/systemd/user/hypr-canvasd.service
systemctl --user daemon-reload
systemctl --user enable hypr-canvasd.service
```

Enabling wires the unit into `graphical-session.target`, so it starts and stops
with your session. If nothing in your setup activates that target, start the
unit from your Hyprland config instead (e.g. under `hl.on("hyprland.start", ...)`
with `hl.exec_cmd("systemctl --user start hypr-canvasd.service")`).

### 2. Add Hyprland keybinds

Hyprland 0.55+ uses Lua for config. Add these binds:

```lua
-- Canvas: pan (mouse binds)
hl.bind("SUPER + SHIFT + mouse:272", function()
    hl.exec_cmd("canvas-ctl pan-start")
end, { mouse = true })

hl.bind("SUPER + SHIFT + mouse:272", function()
    hl.exec_cmd("canvas-ctl pan-stop")
end, { release = true, transparent = true })

-- Canvas: edge-scroll (drag window to screen edge → camera follows)
hl.bind("SUPER + mouse:272", function()
    hl.dispatch(hl.dsp.window.drag())
    hl.exec_cmd("canvas-ctl edge-start")
end, { mouse = true })

hl.bind("SUPER + mouse:272", function()
    hl.exec_cmd("canvas-ctl edge-stop")
end, { release = true, transparent = true })

-- Canvas: center view on the floating window under the cursor
hl.bind("SUPER + mouse:274", hl.dsp.exec_cmd("canvas-ctl center-cursor"), { mouse = true })

-- Canvas: navigation (4-dir spatial)
hl.bind("SUPER + SHIFT + left", hl.dsp.exec_cmd("canvas-ctl nav-left"))
hl.bind("SUPER + SHIFT + right", hl.dsp.exec_cmd("canvas-ctl nav-right"))
hl.bind("SUPER + SHIFT + up", hl.dsp.exec_cmd("canvas-ctl nav-up"))
hl.bind("SUPER + SHIFT + down", hl.dsp.exec_cmd("canvas-ctl nav-down"))

-- Canvas: toggle & invert
hl.bind("SUPER + SHIFT + C", hl.dsp.exec_cmd("canvas-ctl canvas-toggle"))
hl.bind("SUPER + SHIFT + V", hl.dsp.exec_cmd("canvas-ctl canvas-toggle-single"))
hl.bind("SUPER + SHIFT + G", hl.dsp.exec_cmd("canvas-ctl toggle"))
```

Two things about the options in the pairs above. `mouse = true` marks a bind as a
mouse bind and conflicts with `release`, so the release halves omit it.
`transparent = true` keeps a release half from being shadowed by its own press
half: while the button is held, the press half has already consumed the event,
and without the flag the release half never runs.

### 3. Control commands

The full list lives in the CLI itself — `canvas-ctl --help` is canonical:

```bash
canvas-ctl --help  # all 16 commands with one-line descriptions
canvas-ctl ping    # check if daemon is running
canvas-ctl status  # show daemon state
canvas-ctl reload  # re-read the config file and apply it in place
```

`status` prints the version and pid, which config file the daemon read, the pan
and edge-scroll state, the workspaces in canvas mode with the number of floating
windows on the active one, and the spawn rules it believes are registered. A
value the compositor cannot answer reads `unknown` rather than failing the whole
report:

```text
version   1.7.0
pid       4711
config    /home/u/.config/canvas/config.yml
pan       INVERTED IDLE
edge      IDLE
canvas    3
workspace 3
floating  6
rules     canvas-spawn-ws3-default
```

`canvas-ctl status --json` returns the same as one JSON object, for a status bar
or a script. An older daemon replies `UNKNOWN`, which the command reports as a
failure rather than an empty success.

### Configuration

All defaults are built into the daemon (`DEFAULT_CONFIG` in `canvas/config.py`) — it runs fine with no config file. The repo's `config.yml` is a ready-to-copy template; installed wheels/pipx/uv-tool packages do not include it. To customize, create `~/.config/canvas/config.yml`:

```yaml
speed: 1.6                    # pan speed multiplier
idle_timeout: 0.5             # seconds of gesture silence before pan/edge-scroll stops
invert:
  enabled: true               # true = grab canvas (intuitive), false = follow cursor
edge_scroll:
  enabled: true               # auto-pan when dragging window past screen edge
  ramp_distance: 50            # px of overflow to reach full speed
  speed: 20.0                  # max px/frame at full overflow (~1200 px/s at 60fps)
  grab_dead_zone: 5            # px of real movement before camera engages
  # max_speed: 30             # optional: cap per-frame edge-scroll delta (pixels)
navigation:
  cooldown: 0.2               # seconds between nav commands
  protected_apps:             # these windows are skipped during navigation
    - brave-browser
    - chromium
    - firefox
window_pan_excludes: []        # windows left in place while the canvas pans,
                               # matched as a substring of the window class.
                               # Fullscreen windows are always excluded.
canvas:
  preserve_geometry: true     # remember where each window was when canvas was
                               # last switched off, and restore that whole box on
                               # the next ON. Recorded on every toggle off, so it
                               # cannot go stale. Windows you did not move come
                               # back to the exact box they had when tiled
  auto_float: true            # shape windows opened while canvas is ON (see below)
  spawn:
    center: true              # centre new canvas windows on their monitor
    default: 50%x60%          # size: pixels (960x620) or % of the monitor workarea
```

Invalid values (wrong type, zero/negative numbers, a size that is not a size)
are rejected at daemon startup with the exact offending keys listed on stderr.
So is a key this version does not read — a typo, or a setting an earlier version
had — because unknown keys would otherwise be merged in without complaint, and a
half-applied config is worse than one that refuses to start. To pick up an edited
config without restarting the daemon, run `canvas-ctl reload` — it re-reads the
file, prints which one it used, and leaves the running config untouched if the
new one does not validate.

### What canvas-toggle does to window geometry

Enabling canvas makes the workspace's tiled windows float **in place, at exactly
the size they had**. This is deliberate, because Hyprland's own float toggle
does not preserve a tiled box: it substitutes the size the application asked
for, re-centres the window on its old centre and clamps the result into the
workarea, so a 463x493 window can come back as 945x503 or even 1920x1080. The
daemon therefore sets the geometry itself, in one batch, which also makes the
result independent of the order the windows were floated in.

Windows that were already floating when canvas was enabled are not touched at
all. On `canvas-toggle` the windows recorded at enable time are tiled again,
while windows that were already floating survive.

Where the windows end up afterwards is up to your layout, not to the order they
were toggled in: dwindle puts each window back as a split at the node nearest
the cursor. Switching canvas off can therefore reshuffle the grid —
particularly windows that are the same size as each other, since nothing
distinguishes them. Your canvas arrangement is unaffected; only the tiled grid
moves.

### Sizing windows that open during canvas

With `canvas.auto_float: true` — the default — a window opened while a workspace
is in canvas mode arrives already floating, sized and centred. The compositor
applies a windowrule at map time, so there is no visible reflow and no polling
in the daemon.

`canvas.spawn.default` is the size, either pixels (`960x620`) or a percentage of
the workarea of the monitor that owns the workspace (`50%x60%`), clamped so a
window is never asked to be larger than the screen. `canvas.spawn.center` puts it
in the middle of that monitor. That is the whole of what the daemon configures.

**Per-class sizes belong in your Hyprland config**, as an ordinary `windowrule`:

```lua
hl.window_rule({ match = { class = "btop" }, size = { 960, 620 }, float = true })
```

#### What your windowrule can and cannot override

While a workspace is in canvas mode the daemon has registered its own rule for
that workspace. The compositor applies matching windowrules in registration
order and the last write wins; yours is declared at config load and the daemon's
is registered at toggle time, so **the daemon's `float`, `size` and `center` win
on canvas workspaces.** Everything else on the same rule — `border_size`,
`rounding`, `opacity`, `no_anim`, `suppress_event`, `tag` — applies normally,
because the daemon does not touch those.

For a size the canvas rule would override, use an event instead. Events run after
the rules have been applied:

```lua
hl.on("window.title", function(w)
    if w ~= nil and w.class == "btop" then
        hl.dispatch(hl.dsp.window.resize({ x = 960, y = 620 }))
    end
end)
```

Hyprland recommends the same technique for anything a static rule cannot express.
To drop canvas sizing entirely, set `canvas: auto_float: false`.

Windows that were already open when canvas was enabled keep the box the layout
gave them — spawn sizing only applies to windows that open *while* canvas is on,
which is what keeps them from landing on top of each other. The one exception: if
you panned a window earlier, its remembered position wins, since that was a
deliberate choice rather than something the layout picked.

### Moving and resizing one window

The canvas pans and navigates; resizing an individual window is Hyprland's own
job, and the dispatchers are enough:

```lua
hl.bind("SUPER + SHIFT + mouse:273", function()
    hl.dispatch(hl.dsp.window.resize({ x = 960, y = 620, relative = false }))
end)
```

`hl.dsp.window.move({ x = ..., y = ..., window = w })` and its `resize` likewise
take a `window` field; passing one acts on that window without changing focus,
which is the same call the daemon makes when it pans. `relative = true` moves by
a delta rather than to a coordinate.

### Reporting what a toggle did

`canvas-ctl canvas-toggle` prints `CANVAS_ON` or `CANVAS_OFF` and exits non-zero
on failure, so a bind can say what happened:

```lua
hl.bind("SUPER + SHIFT + C", function()
    os.execute("canvas-ctl canvas-toggle && notify-send 'Canvas on' || notify-send 'Canvas off'")
end)
```

### Status bar

Waybar wants its own `{text, tooltip, class}` shape, so the object
`canvas-ctl status --json` returns needs one small script. The trace JSON is
diagnostic and may change freely; these fields are the more deliberate surface,
though the project as a whole still expects breaking changes between releases.

Make `~/.config/waybar/canvas-status.sh` executable:

```sh
#!/bin/sh
out=$(canvas-ctl status --json 2>/dev/null) || {
    printf '%s\n' '{"text":"canvas ?","class":"off","tooltip":"status unavailable"}'
    exit 0
}
printf '%s' "$out" | jq -c '{
    text: (if (.canvas_workspaces | length) > 0 then "canvas " + (.canvas_workspaces | map(tostring) | join(",")) else "canvas off" end),
    tooltip: ("ws " + ((.workspace // "?") | tostring) + " · " + ((.floating // "?") | tostring) + " floating"),
    class: (if (.canvas_workspaces | length) > 0 then "on" else "off" end)
}'
```

```bash
chmod +x ~/.config/waybar/canvas-status.sh
```

Then list `custom/canvas` in `modules-right` (or `modules-left` /
`modules-center`) and define it:

```json
"custom/canvas": {
    "exec": "~/.config/waybar/canvas-status.sh",
    "return-type": "json",
    "interval": 2,
    "format": "{}",
    "tooltip": true,
    "on-click": "canvas-ctl canvas-toggle"
}
```

It renders `canvas 3` when workspace 3 is in canvas mode and `canvas off`
otherwise; the `on` and `off` classes exist to be styled. Two seconds is
plenty, because one `status` is two IPC queries. `jq` has to be installed: if it
is missing the script prints nothing and Waybar hides the module rather than
showing an error.

## Repo overview

- `canvas/` — daemon source: `hypr.py` (IPC), `panning.py` (cursor polling, pan
  and edge-scroll state), `navigation.py`, `spawnrules.py` (canvas spawn
  sizing), `toggle_state.py`, `ipc.py` (ctl server), `config.py`,
  `debug.py` (tracing), `daemon.py`
- `tests/` — mocked pytest suite, no live compositor needed (`uv run pytest`)
- `docs/` — [architecture.md](docs/architecture.md): process model, IPC,
  geometry on toggle, spawn rules; [debugging.md](docs/debugging.md): logs,
  tracing, common failures
- `config.yml` — ready-to-copy config template
- `pyproject.toml` — package metadata, pytest/ruff/mypy config

## Contributing

I'd be happy to receive bug reports, suggestions, discussions, and ideas for the
project's development.

CI is the gate: ruff, ruff-format, mypy, pytest and markdownlint, on Python
3.12, 3.13 and 3.14.

## License

MIT — see [LICENSE](LICENSE) for details.
