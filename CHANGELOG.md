# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- When several `canvas.spawn` rules match the same window, the last one now
  wins, matching the compositor. Previously an already open window was sized
  with the first matching rule while a newly opened one got the last, so the
  two could end up different sizes. Documentation updated accordingly.

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
