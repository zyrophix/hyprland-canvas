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
from unittest.mock import MagicMock, patch

from canvas.navigation import Navigator
from canvas.spawnrules import SpawnRuleError

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


def _ipc(clients):
    """Fake compositor IPC that models the float toggle faithfully.

    Both _set_all_floating and _tile_windows select windows with
    `hl.get_windows({ floating = X })` and then dispatch
    `hl.dsp.window.float({ action = "toggle" })`, so the Lua itself says which
    windows change state. Parsing it keeps the snapshot seen by the daemon in
    step with reality across repeated toggles.
    """
    ipc = MagicMock()
    state = {"clients": [dict(w) for w in clients]}

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
            selected = "floating = true" in code
            for w in state["clients"]:
                if w.get("floating") is selected:
                    w["floating"] = not selected
        return "ok"

    ipc.send.side_effect = send
    ipc.eval_lua.side_effect = eval_lua
    return ipc


def _navigator(ipc, **kwargs):
    with patch("canvas.navigation.toggle_state.load", return_value={}):
        return Navigator(ipc=ipc, protected_apps=[], cooldown=0.0, **kwargs)


class _Targets:
    """Records the geometry the daemon decided on, without the compositor."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, _ws, targets):
        self.calls.append(targets)
        return True

    @property
    def last(self) -> dict:
        return self.calls[-1]


def _on(nav, register=True):
    """Run one canvas ON, recording the geometry targets."""
    targets = _Targets()
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


def test_panned_position_is_restored():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)
    _off(nav)

    nav._floating_geos[1] = {"0x1": {"at": [500, 600], "size": [400, 300]}}
    nav.note_panned(1, {"0x1"})

    _result, targets = _on(nav, register=False)

    assert targets.last == {"0x1": {"at": [500, 600], "size": [400, 300]}}


def test_geometry_the_user_never_moved_is_not_remembered():
    """Compositor-produced float geometry must not be captured as a user choice."""
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)

    assert _off(nav) == "CANVAS_OFF"

    assert "0x1" not in nav._floating_geos.get(1, {})


def test_panned_geometry_is_remembered_on_off():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)
    nav.note_panned(1, {"0x1"})

    assert _off(nav) == "CANVAS_OFF"

    assert nav._floating_geos[1]["0x1"] == {"at": [17, 61], "size": [463, 493]}


def test_panned_set_is_cleared_after_toggle_off():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]))
    _on(nav, register=False)
    nav.note_panned(1, {"0x1"})

    _off(nav)

    assert nav._panned == {}


def test_note_panned_ignores_empty_input():
    nav = _navigator(_ipc([]))
    nav.note_panned(1, set())
    assert nav._panned == {}


# --- spawn sizing applies to existing windows too ---------------------------


def test_spawn_size_applies_to_existing_tiled_windows():
    """30%x40% of 1920x1080, position unchanged."""
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493)]), auto_float=True, spawn_cfg=SPAWN_CFG
    )

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [576, 432]}}


def test_spawn_override_wins_for_existing_windows():
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493, cls="btop")]), auto_float=True, spawn_cfg=cfg
    )

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [910, 930]}}


def test_spawn_rule_does_not_apply_to_other_classes():
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493, cls="kitty")]), auto_float=True, spawn_cfg=cfg
    )

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [576, 432]}}


def test_panned_geometry_beats_spawn_size():
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493)]),
        auto_float=True,
        spawn_cfg=SPAWN_CFG,
    )
    _on(nav)
    _off(nav)
    nav._floating_geos[1] = {"0x1": {"at": [500, 600], "size": [400, 300]}}
    nav.note_panned(1, {"0x1"})

    _result, targets = _on(nav)

    assert targets.last == {"0x1": {"at": [500, 600], "size": [400, 300]}}


def test_auto_float_off_leaves_tiled_boxes_alone():
    nav = _navigator(_ipc([_window("0x1", 17, 61, 463, 493)]), auto_float=False)

    _result, targets = _on(nav, register=False)

    assert targets.last == {"0x1": {"at": [17, 61], "size": [463, 493]}}


def test_unusable_spawn_size_falls_back_to_tiled_box():
    """A window must get a usable box even if its spawn size cannot be resolved."""
    nav = _navigator(
        _ipc([_window("0x1", 17, 61, 463, 493)]),
        auto_float=True,
        spawn_cfg=SPAWN_CFG,
    )
    _on(nav)
    _off(nav)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save"),
        patch("canvas.navigation.spawnrules.build_rules", return_value=[]),
        patch("canvas.navigation.spawnrules.resolve_spec", return_value="nonsense"),
        patch("canvas.navigation.spawnrules.parse_size", side_effect=SpawnRuleError("bad")),
        patch.object(nav, "_apply_floating_geos", side_effect=_Targets()) as targets,
    ):
        assert nav.canvas_toggle_all() == "CANVAS_ON"

    assert targets.call_args[0][1] == {"0x1": {"at": [17, 61], "size": [463, 493]}}


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
    navigator.floating_addresses.return_value = {"0x1", "0x2"}
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    state = DaemonState(
        panning=PanningState(speed=1.0),
        edge_scroll=EdgeScrollState(ramp_distance=50, speed=20.0, enabled=True, grab_dead_zone=5),
        navigator=navigator,
        ipc=ipc,
    )
    return state, navigator, ipc


def test_edge_scroll_move_issues_no_lookup_per_frame():
    state, navigator, _ipc_obj = _daemon_state()
    state.edge_scroll_workspace = 1
    state.edge_scroll_addresses = {"0x1", "0x2"}
    state.edge_scroll._dragged_addr = "0x1"

    for _ in range(30):
        state.edge_scroll_move(3, 0)

    navigator.floating_addresses.assert_not_called()
    assert navigator.note_panned.call_count == 30
    navigator.note_panned.assert_called_with(1, {"0x1", "0x2"})


def test_edge_scroll_addresses_are_captured_once_at_start():
    state, navigator, _ipc_obj = _daemon_state()
    state.edge_scroll_workspace = 1

    with (
        patch("canvas.daemon.get_cursor_pos", return_value=(10, 10)),
        patch.object(state, "_get_active_workspace_id", return_value=1),
        patch.object(
            state,
            "_find_window_at_cursor",
            return_value={"address": "0x1", "at": [0, 0], "size": [100, 100]},
        ),
        patch.object(state, "_get_focused_window_address", return_value="0x1"),
        patch.object(state, "_fetch_monitor_rect", return_value=True),
    ):
        state._handle_edge_start()

    assert state.edge_scroll_addresses == {"0x1", "0x2"}
    navigator.floating_addresses.assert_called_once_with(1)


def test_pan_records_panned_addresses_once_per_session():
    state, navigator, _ipc_obj = _daemon_state()
    state.baselines = {"0x1": (0, 0), "0x2": (10, 10)}
    state.baseline_workspace = 1

    for _ in range(30):
        state.move_windows_to_delta(5, 0)

    assert navigator.note_panned.call_count == 1
    navigator.note_panned.assert_called_once_with(1, {"0x1", "0x2"})


def test_pan_records_again_in_a_new_session():
    state, navigator, _ipc_obj = _daemon_state()
    state.baselines = {"0x1": (0, 0)}
    state.baseline_workspace = 1
    state.move_windows_to_delta(5, 0)

    state._handle_pan_start = lambda: None
    state._panned_noted_ws = None
    state.baselines = {"0x1": (0, 0), "0x9": (1, 1)}
    state.move_windows_to_delta(5, 0)

    assert navigator.note_panned.call_count == 2
    assert navigator.note_panned.call_args[0][1] == {"0x1", "0x9"}
