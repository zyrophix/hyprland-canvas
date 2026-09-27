"""Windows must keep the geometry they had before canvas floated them.

Reproduces the defect where canvas-toggle scrambled the layout: Hyprland
replaces a tiled box with the size the client asked for, re-centres on the old
centre and clamps into the workarea, and because windows are floated one at a
time each step re-runs the layout. Setting the geometry explicitly afterwards
removes both effects.

Most tests assert the target geometry the daemon computed, which is where the
behaviour lives. The last test goes all the way to the Lua it emits.
"""

import json
import re
from unittest.mock import MagicMock, patch

from canvas.navigation import Navigator

SPAWN_CFG = {"center": True, "default": "30%x40%", "rules": []}


def _window(addr, x, y, w, h, floating=False, ws=1, cls="kitty", title="t"):
    return {
        "address": addr,
        "class": cls,
        "title": title,
        "floating": floating,
        "at": [x, y],
        "size": [w, h],
        "workspace": {"id": ws},
    }


def _ipc(clients, retile=()):
    """Fake compositor IPC that models the float toggle faithfully.

    Both _set_all_floating and _tile_windows select windows with
    `hl.get_windows({ floating = X })` and then dispatch
    `hl.dsp.window.float({ action = "toggle" })`, so the Lua itself says which
    windows change state. Parsing it keeps the snapshot seen by the daemon in
    step with reality across repeated toggles.

    `retile` lists box sets the layout hands out, in order, one per tiling —
    that is how dwindle behaves. It re-sorts by focus, so the same windows come
    back in a different arrangement each time it rebuilds. A test that needs the
    layout to be unstable has to ask for it explicitly; without this the fake
    would hand back the same boxes forever and prove nothing about a fix aimed
    at a layout that keeps moving.
    """
    ipc = MagicMock()
    state = {"clients": [dict(w) for w in clients]}
    pending = list(retile)

    def send(cmd):
        return {
            "j/clients": json.dumps(state["clients"]),
            "j/activeworkspace": json.dumps({"id": 1, "name": "1"}),
            "j/monitors": json.dumps(
                [
                    {
                        "id": 0,
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                        "reserved": [0, 0, 0, 0],
                    }
                ]
            ),
            "j/workspaces": json.dumps([{"id": 1, "monitorID": 0}]),
        }.get(cmd, "[]")

    def eval_lua(code):
        if "dsp.window.float" in code:
            # canvas ON un-floats (`floating = false` is selected); canvas OFF
            # tiles the floating ones back (`floating = true`).
            unfloating = "floating = false" in code
            for w in state["clients"]:
                if w.get("floating") is not unfloating:
                    w["floating"] = unfloating
            # Tiling is the point the layout rebuilds, and a rebuild is where
            # dwindle re-sorts by focus.
            if not unfloating and pending:
                for addr, box in pending.pop(0).items():
                    for w in state["clients"]:
                        if w["address"] == addr:
                            w["at"] = list(box["at"])
                            w["size"] = list(box["size"])
        elif "dsp.window.move" in code:
            # The compositor honours the geometry the daemon dispatches. Without
            # this the windows never actually reach the canvas positions, so a
            # later capture would read the layout's boxes instead and every test
            # about remembered geometry would be measuring the fake, not the
            # daemon.
            for addr, ax, ay, sw, sh in re.findall(
                r'\["(0x[0-9a-f]+)"\] = \{at=\{(-?\d+),(-?\d+)\}, '
                r"size=\{(\d+),(\d+)\}\}",
                code,
            ):
                for w in state["clients"]:
                    if w["address"] == addr:
                        w["at"] = [int(ax), int(ay)]
                        w["size"] = [int(sw), int(sh)]
        return "ok"

    ipc.send.side_effect = send
    ipc.eval_lua.side_effect = eval_lua
    return ipc


def _navigator(ipc, **kwargs):
    with patch("canvas.navigation.toggle_state.load", return_value={}):
        return Navigator(ipc=ipc, protected_apps=[], cooldown=0.0, **kwargs)


class _Targets:
    """Records the geometry the daemon decided on, without the compositor."""

    def __init__(self, ipc=None):
        self.calls: list[dict] = []
        self.ipc = ipc

    def __call__(self, _ws, targets):
        self.calls.append(targets)
        if self.ipc is not None:
            # Stand in for the compositor applying what the daemon dispatched,
            # so a later capture reads these positions rather than the layout's.
            self.ipc.eval_lua(_geometry_lua(targets))
        return True

    @property
    def last(self) -> dict:
        return self.calls[-1]


def _geometry_lua(targets):
    lines = [
        f'  ["{a}"] = {{at={{{g["at"][0]},{g["at"][1]}}}, '
        f"size={{{g['size'][0]},{g['size'][1]}}}}},"
        for a, g in sorted(targets.items())
    ]
    return "local geos = {\n" + "\n".join(lines) + "\n}\nhl.dsp.window.move"


def _on(nav, register=True, move=False):
    """Run one canvas ON, recording the geometry targets.

    `move=True` also plays the dispatched geometry back into the fake
    compositor, which a test needs whenever the run after it captures live
    geometry: without the windows actually moving, that capture reads the
    layout's boxes and the test measures the fake instead of the daemon.
    """
    targets = _Targets(getattr(nav, "_ipc", None) if move else None)
    patches = [
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_apply_floating_geos", side_effect=targets),
    ]
    if register:
        patches.append(patch("canvas.navigation.spawnrules.register", return_value=[]))
    with patches[0], patches[1], patches[2]:
        if register:
            with patches[3]:
                return nav.canvas_toggle_all(), targets
        return nav.canvas_toggle_all(), targets


def _off(nav):
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save"),
    ):
        return nav.canvas_toggle_all()


# --- the core defect --------------------------------------------------------


def test_tiled_box_is_reapplied_verbatim():
    """A window that was 463x493 must not come back as 945x503."""
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))

    result, targets = _on(nav, register=False)

    assert result == "CANVAS_ON"
    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_every_window_keeps_its_own_box():
    tiled = [
        _window("0x1", 17, 61, 463, 493),
        _window("0x2", 496, 61, 456, 493),
        _window("0x3", 17, 570, 935, 493),
        _window("0x4", 968, 570, 935, 493),
    ]
    nav = _navigator(_ipc(tiled))

    _result, targets = _on(nav, register=False)

    assert targets.last == {w["address"]: {"at": w["at"], "size": w["size"]} for w in tiled}


def test_already_floating_window_is_not_touched():
    """Not in the snapshot, so its geometry is the user's and stays put."""
    nav = _navigator(_ipc([_window("0x1", 10, 20, 300, 400, floating=True)]))

    result, targets = _on(nav, register=False)

    assert result == "CANVAS_ON"
    assert targets.calls == []


def test_all_floating_workspace_emits_no_geometry():
    nav = _navigator(_ipc([_window("0x1", 10, 20, 300, 400, floating=True)]))

    _result, targets = _on(nav, register=False)

    assert targets.calls == []


def test_geometry_is_one_batch_for_all_windows():
    """Order independence requires a single call, not one per window."""
    tiled = [_window(f"0x{i}", i * 10, i * 10, 100 + i, 100 + i) for i in range(1, 5)]
    ipc = _ipc(tiled)
    nav = _navigator(ipc)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle_all() == "CANVAS_ON"

    # one call to float, one to place
    assert ipc.eval_lua.call_count == 2
    code = ipc.eval_lua.call_args_list[1][0][0]
    for i in range(1, 5):
        assert f'["0x{i}"]' in code


# --- panned geometry wins, noise does not ----------------------------------


def test_a_remembered_window_is_restored_whole():
    """Position and size come from the same remembered state or neither does.

    The entry was captured on the last OFF from where the window actually was,
    so it is one real state of that window. Reading only the position out of it
    and taking the size from the tiled box instead combined two snapshots of
    different moments — and the size then tracked a layout that re-sorts on
    every tile, so a window changed size on each ON.
    """
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)
    _off(nav)

    nav._floating_geos[1] = {"0x1": {"at": [500, 600], "size": [400, 300]}}

    _result, targets = _on(nav, register=False)

    assert targets.last == {"0x1": {"at": [500, 600], "size": [400, 300]}}


def test_toggling_repeatedly_never_changes_a_window():
    """The live failure: a window changed size on every cycle.

    dwindle re-sorts by focus, so the same three windows get different boxes
    each time it rebuilds. Reading the size from that snapshot made each canvas
    ON resize whichever window the previous OFF had promoted to master, which is
    what made the restore look like it depended on which window had focus.

    The layout is given a different arrangement for every tiling on purpose:
    with a stable fake layout this test would pass whether or not the fix is in.
    """
    first_grid = {
        "0x1": {"at": [17, 61], "size": [935, 1002]},
        "0x2": {"at": [968, 61], "size": [935, 493]},
        "0x3": {"at": [968, 570], "size": [935, 493]},
    }
    # same windows, master rotated to a different one each rebuild
    other_grids = [
        {
            "0x3": {"at": [17, 61], "size": [935, 1002]},
            "0x1": {"at": [968, 61], "size": [935, 493]},
            "0x2": {"at": [968, 570], "size": [935, 493]},
        },
        {
            "0x2": {"at": [17, 61], "size": [935, 1002]},
            "0x3": {"at": [968, 61], "size": [935, 493]},
            "0x1": {"at": [968, 570], "size": [935, 493]},
        },
    ]
    windows = [_window(a, b["at"][0], b["at"][1], *b["size"]) for a, b in first_grid.items()]
    nav = _navigator(_ipc(windows, retile=[first_grid, *other_grids, first_grid]))

    _on(nav, register=False, move=True)
    _off(nav)

    _result, first = _on(nav, register=False, move=True)
    assert first.last == first_grid

    for _ in other_grids:
        _off(nav)
        _result, targets = _on(nav, register=False, move=True)
        assert targets.last == first.last


def test_toggle_off_refreshes_remembered_geometry_without_a_pan():
    """A plain toggle must still refresh what OFF remembers.

    Narrowing the capture to panned windows meant a toggle with no pan left
    _floating_geos untouched, and since neither branch cleared it while
    preserve_geometry was on, positions written by some earlier session survived
    every toggle. The next ON applied them, and the workspace came back
    scattered to places nothing on it had to do with.
    """
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)
    nav._floating_geos[1] = {"0x1": {"at": [-550, 804], "size": [935, 1002]}}

    assert _off(nav) == "CANVAS_OFF"

    assert nav._floating_geos[1]["0x1"] == {"at": [17, 61], "size": [463, 493]}


def test_toggle_off_drops_a_stale_entry_when_there_is_nothing_to_capture():
    """The same leak through the other door: no snapshot, nothing to remember."""
    nav = _navigator(_ipc([_window("0x1", 10, 20, 300, 400, floating=True)]))
    nav._floating_geos[1] = {"0x1": {"at": [-550, 804], "size": [935, 1002]}}
    _on(nav, register=False)

    assert _off(nav) == "CANVAS_OFF"

    assert 1 not in nav._floating_geos


def test_a_window_outside_the_snapshot_is_not_remembered():
    """Snapshot membership, not whether a pan moved the window, is the filter.

    A window that arrived as floating during canvas got its geometry from a
    spawn rule, so remembering it would pin a rule-chosen box. It was never in
    the snapshot, so the capture cannot reach it.
    """
    nav = _navigator(
        _ipc(
            [
                _window("0x1", 17, 61, 463, 493),
                _window("0x9", 500, 600, 400, 300, floating=True),
            ]
        )
    )
    _on(nav, register=False)

    assert _off(nav) == "CANVAS_OFF"

    assert "0x9" not in nav._floating_geos[1]


# --- spawn sizing applies to existing windows too ---------------------------


def test_spawn_size_does_not_touch_existing_tiled_windows():
    """A window that was already open keeps the box the layout gave it.

    The spawn size exists so a window *arriving* mid-canvas does not land on
    the others; the compositor applies it at map time. Reshaping the windows
    already on the workspace is not part of that, and did exactly that until
    the branch was removed.
    """
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493)]), auto_float=True, spawn_cfg=SPAWN_CFG
    )

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_spawn_override_does_not_touch_existing_windows():
    """A per-class override is no more entitled to reshape an open window."""
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493, cls="btop")]), auto_float=True, spawn_cfg=cfg
    )

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_every_open_window_keeps_its_own_box_under_auto_float():
    """Four terminals in a 2x2 grid must all survive canvas ON unchanged."""
    windows = [
        _window("0x1", 17, 61, 935, 493),
        _window("0x2", 968, 61, 935, 493),
        _window("0x3", 17, 570, 935, 493),
        _window("0x4", 968, 570, 935, 493),
    ]
    nav = _navigator(_ipc(windows), auto_float=True, spawn_cfg=SPAWN_CFG)

    _result, targets = _on(nav)

    assert targets.last == {w["address"]: {"at": w["at"], "size": w["size"]} for w in windows}


def test_a_remembered_window_is_restored_whole_under_auto_float():
    """auto_float still does not get a say over a window that is already open."""
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493)]),
        auto_float=True,
        spawn_cfg=SPAWN_CFG,
    )
    _on(nav)
    _off(nav)
    nav._floating_geos[1] = {"0x1": {"at": [500, 600], "size": [400, 300]}}

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [500, 600], "size": [400, 300]}}


def test_auto_float_off_leaves_tiled_boxes_alone():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]), auto_float=False)

    _result, targets = _on(nav, register=False)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_geometry_failure_rolls_the_workspace_back():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_apply_floating_geos", return_value=False),
        patch.object(nav, "_set_snapshot_floating", return_value=True) as rollback,
    ):
        assert nav.canvas_toggle_all() == "ERROR:GEOMETRY_RESTORE_FAILED"

    rollback.assert_called_once()
    assert 1 not in nav._canvas_mode_workspaces


# --- the per-frame paths must not add IPC ----------------------------------
#
# edge_scroll_move and move_windows_to_delta both run on every frame of a pan
# or edge-scroll. Hyprland handles IPC synchronously, so an extra j/clients per
# frame there is a real cost, not just wasted work.


def _daemon_state():
    from canvas.daemon import DaemonState
    from canvas.panning import EdgeScrollState, PanningState

    navigator = MagicMock()
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    state = DaemonState(
        panning=PanningState(speed=1.0),
        edge_scroll=EdgeScrollState(ramp_distance=50, speed=20.0, enabled=True, grab_dead_zone=5),
        navigator=navigator,
        ipc=ipc,
    )
    return state, navigator, ipc


def test_a_window_that_arrived_mid_canvas_never_enters_the_remembered_set():
    """The spawn rule's size cannot launder itself into "the user wanted this".

    Restoring a window whole would pin a rule-chosen size for good if such a
    window ever reached the remembered set. The guard is snapshot membership,
    not any judgement about the size: OFF captures only the windows the
    snapshot holds, and a window that arrived after ON is not in it. Its rule
    size is therefore absent, and the next ON falls back to the layout's box.
    """
    nav = _navigator(
        _ipc(
            [
                _window("0x1", 17, 61, 935, 493),
                _window("0x9", 500, 600, 576, 414, floating=True),
            ]
        ),
        auto_float=True,
        spawn_cfg=SPAWN_CFG,
    )
    _on(nav)
    assert "0x9" not in nav._floating_geos.get(1, {})

    _off(nav)

    # tiled back by OFF, but never remembered
    assert "0x9" not in nav._floating_geos.get(1, {})

    # The next ON therefore has to place it from a fresh snapshot. The fake
    # compositor does not re-run a layout when it tiles, so the snapshot's box
    # for 0x9 is the geometry it already had; what matters is the route, not
    # the numbers, so assert it came from the snapshot rather than from memory.
    nav._floating_geos[1] = {"0x1": {"at": [802, 227], "size": [935, 493]}}
    snapshot_box = {"at": [500, 600], "size": [576, 414]}
    _result, targets = _on(nav)

    assert "0x9" in nav._canvas_mode_workspaces[1]
    assert targets.last["0x9"] == snapshot_box


def test_panned_windows_keep_the_size_they_were_captured_at():
    """A pan moves windows without resizing them, so the size is unchanged."""
    windows = [
        _window("0x1", 17, 61, 935, 1002),
        _window("0x2", 968, 61, 935, 493),
        _window("0x3", 968, 570, 935, 493),
    ]
    nav = _navigator(_ipc(windows), auto_float=True, spawn_cfg=SPAWN_CFG)
    _on(nav)
    _off(nav)

    # exactly what an OFF capture leaves behind after the user panned: every
    # position moved, every size the size it already had
    moved = {w["address"]: {"at": [802, 227], "size": list(w["size"])} for w in windows}
    nav._floating_geos[1] = moved

    _result, targets = _on(nav)

    for w in windows:
        assert targets.last[w["address"]] == {
            "at": [802, 227],
            "size": list(w["size"]),
        }


def test_a_remembered_entry_without_geometry_falls_back_to_the_tiled_box():
    """A state file from before 1.1 keeps addresses and drops geometry.

    _parse_snapshot emits {} for such an entry, and applying it verbatim indexed
    a missing "at" — which failed the whole toggle, so canvas could not be
    turned on at all for anyone whose state file had been migrated.
    """
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    nav._floating_geos[1] = {"0x1": {}}

    result, targets = _on(nav, register=False)

    assert result == "CANVAS_ON"
    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_a_remembered_entry_with_only_a_position_keeps_that_position():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    nav._floating_geos[1] = {"0x1": {"at": [500, 600]}}

    _result, targets = _on(nav, register=False)

    # position remembered, size the layout's business
    assert targets.last == {"0x1": {"at": [500, 600], "size": [463, 493]}}
