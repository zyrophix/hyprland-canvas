# Architecture

```text
canvasd (daemon)
├── hypr.py         Direct Unix socket IPC to Hyprland
├── panning.py      Cursor polling, pan state, edge-scroll state
├── navigation.py   Window navigation, cursor centering, canvas toggle
├── spawnrules.py   Compositor windowrules for windows opened during canvas
├── ipc.py          Unix socket server for canvas-ctl
├── config.py       YAML config with deep merge
└── daemon.py       Main loop, wires modules together
```

Key design decisions:

- **Cursor polling** — reads cursor position from Hyprland IPC, works on any Wayland setup
- **A canvas drag is 1:1** — the window under the pointer stays under it; more reach comes from pointer acceleration in the compositor or from the `pan-*` step commands
- **`hl.dsp.window.move({window=w})` without focus** — passing a window object bypasses auto-focus, so no cursor warp or feedback loop
- **Direct socket IPC** — one short-lived Unix-socket request per Hyprland command, avoiding subprocess startup on every frame
- **Workspace-scoped** — pan, edge-scroll, navigation and cursor centering only move floating windows on the current workspace; other workspaces are never touched
- **Ground-truth, direction-aware edge pan** — modeled after compositor-level implementations (driftwm, hevel): the camera assists only while a *confirmed* drag (window under cursor + focus match + moved past `grab_dead_zone`) pushes the window *toward* an edge or holds it there; pulling the window away from a boundary stops that side's assist immediately. The dragged window's real geometry is polled from Hyprland every frame — no cursor-derived guessing, so clicks on borders/gaps or holds without movement never move the camera on their own
- **Idle timeout** (`idle_timeout`, default 0.5s) — ends a pan or edge-scroll on its own when the gesture stops producing input (cursor or dragged-window movement) for that long, covering a release event Hyprland never delivered
- **Navigation falls back to the camera** — `nav-*` picks the nearest window in the asked direction (aligned first, then any, then wrapping to the other side); when there is nothing to navigate to that way, as when pressing up in a row, the camera moves one `pan_step` instead of jumping to an arbitrary window
- **Step-pan** (`pan_step`, default 100 px) — `pan-left/right/up/down` move the whole canvas by one step, for keys of your choosing; it moves the view, so `pan-left` shifts the windows right

## Spawn rules (`canvas.auto_float`)

With the flag on, windows opened while a workspace is in canvas mode are
floated, sized and centred by the compositor itself: the daemon registers a
`windowrule` from Lua and Hyprland applies it at map time. Three properties of
the compositor make this cheap and are the reason no event listener is needed:

- A rule only affects windows created **after** it is registered, so "enabled
  while canvas is on" is exactly the desired semantics.
- Matching rules are applied in **registration order, last write wins**: a rule
  the daemon registers at toggle time overrides one declared in the user's
  config, which was loaded at startup.
- The rule engine is **global and outlives the daemon**, and Hyprland exposes no
  way to list windowrules. The registered names are therefore persisted in the
  toggle state and disabled again on OFF; at startup any names left behind by
  a crash are disabled before fresh ones are registered for workspaces that are
  still in canvas mode.
- Re-registering a rule by name is **not** a replacement: Hyprland reuses the
  object and appends every field to its effect list again, with no
  deduplication. The rules therefore carry a marker in the compositor's Lua
  state, and the one-second check only registers when the marker is gone —
  after a compositor reload, or for a fresh ON after OFF. Without the marker
  every check would grow the rule forever.

**One rule per workspace.** It matches every class, but only on the workspace it
was registered for: without that constraint a catch-all claims windows on
ordinary desktops too. The name says `ws<N>` and retraction is by name, so the
workspace has to appear in both.

Percent sizes are resolved by the daemon because `windowrule` only takes pixels:
the workarea of the monitor owning the workspace comes from `j/monitors` minus
`reserved`, matched to the workspace via `monitorID` from `j/workspaces`. That is
the *workarea*, while `monitor_w` in a `windowrule` size expression is the whole
monitor; the two are not interchangeable.

`size` has no effect on a tiled window — tiled placement is layout-owned — so
`float` and `size` always travel in the same rule.

Transaction details that matter:

- On **ON**, rules are registered before the state is persisted and before the
  compositor is touched, so a registration failure needs no rollback. A
  workspace whose monitor cannot be resolved — one on a disabled output — enters
  canvas mode without a rule instead of refusing the toggle: canvas mode is
  about the windows already open, and the rule is for the ones that arrive later.
- On **OFF**, rules are disabled only after tiling succeeded. A failed tile
  rolls the workspace back to floating, where the rules are still correct.

**The rule engine does not survive a config reload.** `ConfigManager::reload()`
calls `ruleEngine()->clearAllRules()`, clears its map of named rules and
recreates the Lua state, so a rule the daemon still believes in is gone and its
name forgotten, with nothing reported. The daemon therefore checks once a second
while any workspace is in canvas mode, and the marker described above tells it
whether the compositor still holds the rules. Confirmed on 0.55.4, 0.56.2 and
0.57.0.

The OFF tile set is the snapshot taken at ON plus every window that is floating
at OFF but was not floating at ON. Addresses already in the snapshot keep their
recorded geometry, because `_toggle_order` reads it to restore the original
row-major layout.

## Geometry on canvas ON

Hyprland's float toggle does not keep a tiled box. In
`DefaultFloatingAlgorithm::movedTarget`, the `wasTiling()` branch takes
`lastFloatingSize()` — the size the window had the last time *it* was floating —
and falls back to the client's requested geometry, or 640x400. It then adds
10x10 on purpose when that would be within 5px of the current size, "to avoid
floating toggles that don't change size, they aren't easily visible to the
user", re-centres on the old centre and clamps into the workarea.

So a 463x493 window can come back as 945x503, or as 1920x1080 at `-2,-2` for a
client that asks for the whole screen. On top of that, floating windows one at a
time re-runs the layout after every step, so the result depends on the order
too.

The daemon therefore does not trust the toggle. After `_set_all_floating` it
applies its own geometry in a single batched call, which fixes both effects at
once. Priority per address:

1. the box the window was last seen in, recorded on every OFF — one real state
   of that window, position and size together, refreshed each time so it cannot
   describe a canvas from an earlier session;
2. the tiled box the window had at ON, taken from the snapshot.

`canvas-ctl canvas-reset` drops the remembered box for the active workspace, so
its next ON takes the tiled box (step 2) instead. It is only meaningful while
that workspace is tiled: the next OFF captures the canvas again, so a reset made
during canvas does not survive it.

`canvas-toggle-single` uses the same record for one window: inside canvas mode,
tiling the focused window stores its box and floating it again applies that box.
Outside canvas mode the compositor's own toggle decides.

The record is not applied to windows it has nothing for: a state file migrated
from before 1.2 keeps window addresses and drops their geometry, and such an
entry falls back to step 2. `preserve_geometry` selects step 1; with it off
nothing is recorded and no geometry is applied at ON, so each window keeps the
box the compositor's float toggle gives it.

Only the windows in the snapshot are recorded, so a window that arrived
floating mid-canvas — the spawn rules make one float at map time — never
enters the record. Its size came from a rule rather than from the layout, and
replaying it is what used to make the layout drift further away on every
toggle.

Addresses that were already floating at ON are absent from the snapshot and so
are never touched.

The daemon builds the match itself — `{ class = ".*", workspace = N }` — so
there is no user pattern to pass through. Per-class patterns are the compositor's
business, through the user's own `windowrule`, and it matches those with
`re2::RE2::FullMatch`: `class = "btop"` matches only the exact class, and a
substring needs `.*btop.*`.
