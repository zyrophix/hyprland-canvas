# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] — 2026-09-28

### Added

- `window_pan_excludes` lists window classes that stay in place while the canvas
  pans, for things that should not be dragged off-screen — a video call, a
  picture-in-picture, a persistent dashboard. Matching is a case-insensitive
  substring of the window class, the same as `protected_apps`. Excluded windows
  are left out of the pan snapshot, which also keeps them out of the shutdown
  restore and out of the remembered geometry, since they never moved. Fullscreen
  windows are always excluded.
- `canvas-ctl reload` re-reads the config file and applies it to the running
  daemon, so tuning speeds, protected apps, edge-scroll and `canvas.spawn`
  sizing no longer needs a restart. The new config is validated before anything
  is applied, so a broken file leaves the daemon on the previous one rather
  than half-way between two. The response names the file that was read, which
  matters when an XDG config and a project copy both exist.
- `examples/hypr-canvasd.service` — an example systemd user unit for anyone who
  wants a supervisor. The daemon exits non-zero when the cursor poller dies so
  that something restarts it; until now the repository shipped the mechanism
  without the thing that uses it. It is an example only: the Arch package and
  the wheels still install no service and enable nothing. The unit is bound to
  `graphical-session.target` with `PartOf` and `After`, matching the unit
  Hyprland itself ships for `xdg-desktop-portal`, so it starts and stops with
  the session and never before it.

### Changed

- The default `canvas.spawn` size is now `50%x60%`, up from `30%x40%`. The old
  value gave 576x414 on a 1920x1036 workarea — about 76 columns and 24 rows in
  a terminal — and it overrode the client downward rather than upward: Hyprland
  takes a floating window's size from the application, so any number in the
  rule replaces whatever was asked for. The pixel example in the config and the
  README was `910x930`, more than three times the default's area, which is part
  of why the value read as arbitrary; it is now `960x620`, next to what the
  default actually produces. `canvas.spawn.default` and per-class `rules` still
  override it.

### Fixed

- `canvas.spawn` percent sizes and computed window positions were wrong on any
  setup with a panel. Hyprland serialises a monitor's `reserved` as
  left, top, right, bottom; it was read one position out of order, so a 44px
  top bar produced a workarea 44px narrower and 44px taller. On a 1920x1080
  display with a 44px bar that turned `50%x60%` into 938x648 instead of
  960x621, and put the computed workarea centre at (938, 540) rather than
  (960, 562). The bug is invisible without a reserved area.
- The `canvas.spawn` rule no longer carries `immediate`, `no_anim`, `no_dim`
  and `no_shadow`. A windowrule is re-evaluated against every mapped window
  and this one matches a whole workspace, so those effects reached windows
  that were already open: no animation, no shadow, no dimming for as long as
  canvas was on. The rule now says where a window goes and nothing about how
  it looks.
- `canvas-toggle` no longer resizes windows that were already open. Spawn
  sizing was applied to them as well, so turning canvas on with `auto_float`
  reshaped every window on the workspace to the spawn size — four terminals in
  a 2x2 grid all became the same box. Spawn sizing exists so a window opening
  *during* canvas does not land on the others, and the compositor applies it at
  map time; windows that are already on screen have not earned it. Windows you
  panned earlier still keep their remembered box.
- A window that arrived floating during canvas is now tiled again on
  `canvas-toggle` even when the snapshot was empty. Canvas turned on with
  nothing tiled, a window spawned into it, and toggling off left that window
  floating and centred forever: the arrived-window path was gated on a
  non-empty snapshot, and nothing else in the OFF path tiles it. The marker
  still cleared, so the toggle looked like it had done nothing.
- `canvas.auto_float` no longer shapes windows outside the canvas workspaces it
- Spawn rules are now taken down when the daemon exits. The compositor's rule
  engine outlives the process, so a rule left registered kept shaping every
  window opened on that workspace afterwards, with no daemon left to retract
  it. The state file keeps the names, so a killed daemon is still cleaned up
  on the next start.
  was enabled for. Every rule was registered with a `class = ".*"` catch-all and
- The geometry remembered for the next `canvas-toggle` is now refreshed on
  every toggle off, not only when a pan moved a window. With nothing panned
  there was no capture and the old entry was never cleared, so it survived
  every toggle and the next ON applied positions from an earlier session.
- A state file older than 1.2.0 no longer stops canvas from turning on. Such a
  file keeps window addresses and drops their geometry — 1.2.0 is where
  `canvas-toggle` started recording geometry at all — and applying such an entry
  verbatim failed the whole toggle. It now falls back to the tiled box.
  no workspace condition — the workspace id appeared only in the rule's name,
  which is bookkeeping for retracting it later. So a rule registered for one
  canvas workspace floated and centred every window opened in every workspace,
  and with two canvas workspaces open the second registration's size won
  globally, sizing windows on the first against the second's workarea. Rules
  are now scoped to the workspace they are registered for, and a rule naming a
  different workspace is rejected rather than silently never matching.
- A fullscreen floating window is no longer dragged along by the camera. The
  `fullscreen` field from `j/clients` was read in exactly one place, so a
  fullscreen call or video would travel off-screen with every pan frame.
- Edge-scroll no longer pans up to √2 faster diagonally. The four edge
  contributions are independent, so a corner accumulated two full-speed
  contributions. `edge_scroll.max_speed` could not catch it: it clamps each
  axis independently and never sees the magnitude of the vector.
- `canvas-ctl nav-*` centres on the monitor the target window is actually on.
  Resolving the monitor centre with no cursor coordinates fell back to the
  focused monitor, so navigating to a window on a second monitor dragged it
  onto the first one.
- The daemon now exits non-zero if its IPC thread dies. The bind can fail
  before or during the run — a stale socket path, revoked permissions, or the
  symlink guard — and the singleton lock is already held at that point, so the
  daemon used to keep running, answer nothing, and refuse every later start
  until it was killed with SIGKILL.
- Loading a state file written by a newer format version now logs a warning.
  The parsers accepted any version at or above 2 and read it as the current
  format, so the file was silently misinterpreted and then overwritten.
- The Hyprland IPC socket is now looked up under `$XDG_RUNTIME_DIR` instead of a
  hardcoded `/run/user/<uid>`, with the old path kept as the fallback. Hyprland
  roots its runtime data at that variable and only warns when the value looks
  non-standard, so on a session with an unusual runtime dir the compositor put
  its socket somewhere the daemon never looked — while the daemon's own socket
  and state file, which already read the variable, sat in the right place.
- `navigation.protected_apps` entries must be non-empty. They are matched as a
  substring of the window class, so an empty entry matched every window and
  silently disabled navigation with no diagnostic.
- `docs/debugging.md` no longer claims that per-window trace events appear at
  `CANVAS_DEBUG=1`. Only eight events do; the other thirty-two, including
  `CANVAS_GEOMETRY` and every `SPAWN_RULES_*`, need `CANVAS_DEBUG=2`. The old
  list sent people looking for events the daemon never printed at that level.
- Config errors now name the file that produced them, so a reload that fails
  says which copy to fix.
- `canvas-ctl` no longer exits 0 when the daemon answers `UNKNOWN`, which is
  what a daemon too old to know the command replies. Updating the CLI while an
  older daemon still ran made a script believe a command had been applied when
  the daemon had never heard of it.
- A `canvas.spawn` match pattern that is not a valid regex is now a hard
  failure at registration instead of a logged warning. The daemon used to
  reimplement the match to size windows that were already open, and skipped
  patterns it could not compile; with that gone the pattern reaches the
  compositor verbatim, which rejects the rule, undoes the batch and fails the
  toggle. A typo in a matcher is now loud rather than a rule that quietly
  matches nothing.
- When several `canvas.spawn` rules match the same window, the last one wins,
  the same as Hyprland's own `windowrule`. The 1.5.0 notes said first match
  wins; that was never what the compositor did, and no release corrected it.

## [1.5.0] — 2026-09-27

### Added

- `canvas.auto_float` shapes windows opened while a workspace is in canvas mode:
  they arrive floating, sized and centred, applied by the compositor as a
  windowrule at map time, so there is no reflow and no polling.
- `canvas.spawn` sets the size — pixels (`910x930`) or a percentage of the
  workarea of the monitor owning the workspace (`30%x40%`) — plus a default and
  an ordered list of per-`class`/`title` overrides, first match wins. Matchers
  use the same properties and regular expression semantics as Hyprland
  `windowrule`. The same sizing is applied to windows that were already open
  when canvas was enabled.
- Windows picked up this way are tiled again on `canvas-toggle`; windows that
  were already floating before canvas was enabled are left untouched.

### Fixed

- `canvas-toggle` no longer scrambles the layout. Hyprland deliberately does not
  keep a tiled box when a window becomes floating — it substitutes the size the
  client asked for, re-centres on the old centre and clamps the result into the
  workarea — and because windows were floated one at a time, each step re-ran
  the layout and the result also depended on the order. The daemon now sets the
  geometry itself in a single batch, so every window keeps the exact position
  and size it had.
- Only geometry the user actually panned to is remembered across a toggle.
  Previously whatever the compositor produced when a window became floating was
  captured and then replayed on every following toggle, so one odd moment
  permanently stuck a window at a bogus size such as `-2,-2 1920x1080`.
- Edge-scroll no longer dies when a window is pushed against a screen edge. The
  compositor clamps the dragged window to the workarea, so the pointer
  necessarily ends up outside the window rect; the session treated that as a
  lost grab and stopped, and because a session is armed per mouse press the
  camera stayed dead until the window was released and grabbed again. The
  pointer check now applies only until the drag is confirmed, after which the
  window's own motion is the ground truth.
- Arch packaging installs a single wheel. `makepkg` reuses its checkout, so a
  wheel left by an earlier version could still be in `dist/` and the glob
  installed two copies of the same files. `__pycache__` is no longer packaged.

### Changed

- Toggle state format v4: workspaces additionally record the addresses that
  were floating at ON and the spawn rule names registered for them. Files from
  v2 and v3 load unchanged, with both new sections empty.

### Credits

- The idea of floating newly opened windows into the canvas came from
  [shinishiz/hyprland-canvas](https://github.com/shinishiz/hyprland-canvas).
  The implementation here is independent and uses compositor windowrules
  instead of a socket event listener.

## [1.4.2] — 2026-09-26

### Fixed

- Arch PKGBUILD now includes the post-install message and installs the wheel under `/usr`.

## [1.4.1] — 2026-09-26

### Added

- Arch Linux PKGBUILD for installing `hyprland-canvas` with `makepkg`.

## [1.4.0] — 2026-09-25

### Added

- `canvas-ctl center-cursor` centers the canvas on the topmost floating window under the cursor without changing focus.

## [1.3.1] — 2026-09-24

### Fixed

- Hyprland Lua keybind examples now use the supported `hl.bind` API.
- Floating geometry restore uses Hyprland 0.55/0.56 resize arguments (`x`, `y`).
- Hyprland textual IPC errors now prevent false-success canvas/navigation results.
- Canvas toggle persists an explicit active marker and commits state around compositor actions.
- Navigation and main-loop canvas moves are serialized to avoid competing pan/edge writes.
- Edge-scroll no longer retains a stale movement direction across a fast reversal.
- Invalid non-finite config values and malformed YAML roots fail validation cleanly.

## [1.3.0] — 2026-09-18

### Added

- `canvas-ctl --help` (all 14 commands) and `--version`; `canvasd --help`.
- README T1 structure (badges, Quickstart, Repo overview, Contributing),
  video demo, `docs/` notes, `CHANGELOG.md`, governance files.

### Changed

- CLI misuse exits 2 (argparse standard); error paths unchanged.

## [1.2.0] — 2026-09-04

### Added

- Spatial 4-dir navigation with geometry-preserving toggle.
- Two-level `CANVAS_DEBUG` tracing (summary + per-window details).
- Canvas toggle preserves floating geometry; single-window toggle.

## [1.1.1] — 2026-08-24

### Fixed

- Edge-scroll: camera assist only while dragging toward the edge.

## [1.1.0] — 2026-08-18

### Added

- Structured `CANVAS_DEBUG` tracing for edge-scroll and IPC.
- Ground-truth window geometry with confirmed-drag gating.

### Fixed

- Edge-scroll disarms when the cursor leaves the dragged window; pan/edge exclusivity.
- Pan and navigation scoped to the active workspace.
- Edge-scroll fails safe without monitor geometry.

## [1.0.1] — 2026-05-20

### Fixed

- Handler map, `EdgeScrollParams` dataclass, silent recovery fix.

## [1.0.0] — 2026-05-16

### Added

- Initial release: pan, navigate, toggle, edge-scroll, IPC CLI.
