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

## Features

| Feature | Keybind | Description |
| --- | --- | --- |
| Pan canvas | SUPER+SHIFT+LMB | Drag to pan all floating windows |
| Edge-scroll | SUPER+LMB | Drag a floating window toward the screen edge — camera follows (engages only for a confirmed drag of the window under the cursor) |
| Navigate | SUPER+SHIFT+Arrows | Spatial jump to nearest window in direction (up/down/left/right), auto-pan to center |
| Center under cursor | SUPER+MMB | Center the canvas on the topmost floating window under the mouse cursor without changing focus |
| Canvas toggle | SUPER+SHIFT+C | Toggle all windows on workspace to/from floating, keeping each window exactly where and how big it was |
| Window sizing | on canvas enable | Give windows a size as they enter the canvas — one default plus per-`class`/`title` rules, opt-in (see [Configuration](#configuration)) |
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
canvas-ctl status    # 3. show pan direction and state
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
end, { mouse = true, release = true })

-- Canvas: edge-scroll (drag window to screen edge → camera follows)
hl.bind("SUPER + mouse:272", function()
    hl.dispatch(hl.dsp.window.drag())
    hl.exec_cmd("canvas-ctl edge-start")
end, { mouse = true })

hl.bind("SUPER + mouse:272", function()
    hl.exec_cmd("canvas-ctl edge-stop")
end, { mouse = true, release = true })

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

### 3. Control commands

The full list lives in the CLI itself — `canvas-ctl --help` is canonical:

```bash
canvas-ctl --help  # all 16 commands with one-line descriptions
canvas-ctl ping    # check if daemon is running
canvas-ctl status  # show pan direction and state
canvas-ctl reload  # re-read the config file and apply it in place
```

### Configuration

All defaults are built into the daemon (`DEFAULT_CONFIG` in `canvas/config.py`) — it runs fine with no config file. The repo's `config.yml` is a ready-to-copy template; installed wheels/pipx/uv-tool packages do not include it. To customize, create `~/.config/canvas/config.yml`:

```yaml
speed: 1.6                    # pan speed multiplier
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
  preserve_geometry: true     # remember where you panned windows to, and restore
                               # them on the next ON; windows you did not move go
                               # back to the exact box they had when tiled
  auto_float: false           # shape windows opened while canvas is ON (see below)
  spawn:
    center: true              # centre new canvas windows on their monitor
    default: 30%x40%          # size for any canvas window without an override
    rules:                    # LAST match wins, like windowrule: list broad
                               # rules first and narrow ones last
      - match: { class: btop }
        size: 910x930
      - match: { title: ".*nvim.*" }
        size: 45%x35%
```

Invalid values (wrong type, zero/negative numbers) are rejected
at daemon startup with the exact offending keys listed on stderr. To pick up
an edited config without restarting the daemon, run `canvas-ctl reload` — it
re-reads the file, prints which one it used, and leaves the running config
untouched if the new one does not validate.

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
while windows that were already floating survive. If you pan while canvas is
on, those positions are remembered and restored on the next enable — geometry
the compositor produced on its own is not treated as your choice and is never
remembered.

### Sizing windows that open during canvas

With `canvas.auto_float: true`, a window opened while a workspace is in canvas
mode arrives already floating, sized and centred — the compositor applies a
windowrule at map time, so there is no visible reflow and no polling in the
daemon. The same sizing is applied to the windows that were already open when
canvas was enabled, so both end up the same size.

Sizes are either pixels (`910x930`) or a percentage of the workarea of the
monitor that owns the workspace (`30%x40%`); they are clamped so a window can
never be asked to be larger than the screen. `match` accepts the same
properties as a Hyprland `windowrule` matcher, and values are treated as
regular expressions, so `.*nvim.*` works. Note that Hyprland matches these
patterns as a *full* match: `class: btop` matches only the exact class `btop`.
When several rules match the same window the **last** one wins, the same as
Hyprland's own `windowrule` — the compositor applies matching rules in
registration order and the last write wins. So list broad rules first and
narrow ones last; anything that matches no rule gets `default`.

Windows that were already floating when canvas was enabled keep their size —
they are not canvas windows, so nothing is done to them. Tiled windows that
were open at enable time get the same size as a newly opened one, unless you
panned them earlier, in which case their remembered position wins.

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

PRs welcome. Run `uv run pytest` and `uv run ruff check` before submitting.

## License

MIT — see [LICENSE](LICENSE) for details.
