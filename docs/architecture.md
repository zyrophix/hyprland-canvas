# Architecture

```
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
- **`hl.dsp.window.move({window=w})` without focus** — passing a window object bypasses auto-focus, so no cursor warp or feedback loop
- **Direct socket IPC** — one short-lived Unix-socket request per Hyprland command, avoiding subprocess startup on every frame
- **Workspace-scoped** — pan, edge-scroll, navigation and cursor centering only move floating windows on the current workspace; other workspaces are never touched
- **Ground-truth, direction-aware edge pan** — modeled after compositor-level implementations (driftwm, hevel): the camera assists only while a *confirmed* drag (window under cursor + focus match + moved past `grab_dead_zone`) pushes the window *toward* an edge or holds it there; pulling the window away from a boundary stops that side's assist immediately. The dragged window's real geometry is polled from Hyprland every frame — no cursor-derived guessing, so clicks on borders/gaps or holds without movement never move the camera on their own
- **Idle timeout** (500ms) — auto-stops panning if Hyprland drops a mouse release event during active drag

## Spawn rules (`canvas.auto_float`)

With the flag on, windows opened while a workspace is in canvas mode are
floated, sized and centred by the compositor itself: the daemon registers a
`windowrule` from Lua and Hyprland applies it at map time. Three properties of
the compositor make this cheap and are the reason no event listener is needed:

- A rule only affects windows created **after** it is registered, so "enabled
  while canvas is on" is exactly the desired semantics.
- Matching rules are applied in **registration order, last write wins**, so the
  catch-all default is registered first and user overrides after it. Ordering
  is the whole override mechanism — there is no separate precedence rule.
- The rule engine is **global and outlives the daemon**, and Hyprland exposes no
  way to list windowrules. The registered names are therefore persisted in the
  toggle state and disabled again on OFF; at startup any names left behind by
  a crash are disabled before fresh ones are registered for workspaces that are
  still in canvas mode.

Percent sizes are resolved by the daemon because `windowrule` only takes pixels:
the workarea of the monitor owning the workspace comes from `j/monitors` minus
`reserved`, matched to the workspace via `monitorID` from `j/workspaces`.

`size` has no effect on a tiled window — tiled placement is layout-owned — so
`float` and `size` always travel in the same rule.

Two transaction details matter:

- On **ON**, rules are registered before the state is persisted and before the
  compositor is touched, so a registration failure needs no rollback.
- On **OFF**, rules are disabled only after tiling succeeded. A failed tile
  rolls the workspace back to floating, where the rules are still correct.

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

1. a position the user panned to — tracked in `_panned`, filled by the daemon on
   pan and edge-scroll, because that is a deliberate choice;
2. the configured `spawn` size, when `auto_float` is on;
3. the tiled box the window had at ON, taken from the snapshot.

Step 3 is why the recording is not used as a fallback for everything. Geometry
the compositor invented when a window became floating is not a user choice, and
replaying it is what used to make the layout drift further away on every
toggle. `preserve_geometry` is about the first case only; with it off, panned
positions are forgotten and everything returns to its tiled box.

Addresses that were already floating at ON are absent from the snapshot and so
are never touched.

Because the compositor matches windowrule patterns with `re2::RE2::FullMatch`,
`class: btop` matches only the exact class and a substring needs `.*btop.*`.
`spawnrules.full_match` reproduces that for windows that are already open, where
no rule can be applied retroactively; only `class` and `title` are available
from `j/clients`, so a rule also matching on something else (workspace, pid,
floating) is skipped for existing windows and left to the compositor for new
ones.
