"""Canvas spawn rules: registration lifecycle and tiling of arrived windows."""

import json
from unittest.mock import MagicMock, patch

import pytest

from canvas.navigation import Navigator
from canvas.spawnrules import SpawnRuleError

WORKAREA = (0, 0, 1920, 1036)
SPAWN_CFG = {"center": True, "default": "30%x40%", "rules": []}


def _window(addr, cls="kitty", ws=1, floating=False, x=0, y=0, w=100, h=100):
    return {
        "address": addr,
        "class": cls,
        "title": "t",
        "floating": floating,
        "at": [x, y],
        "size": [w, h],
        "workspace": {"id": ws},
    }


def _navigator(ipc, auto_float=True, spawn_cfg=None):
    with patch("canvas.navigation.toggle_state.load", return_value={}):
        return Navigator(
            ipc=ipc,
            protected_apps=[],
            cooldown=0.0,
            auto_float=auto_float,
            spawn_cfg=SPAWN_CFG if spawn_cfg is None else spawn_cfg,
        )


def _ipc_with(clients, monitors=None, workspaces=None):
    ipc = MagicMock()
    monitors = (
        monitors
        if monitors is not None
        else [{"id": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "reserved": [0, 0, 0, 0]}]
    )
    workspaces = workspaces if workspaces is not None else [{"id": 1, "monitorID": 0}]
    ipc.send.side_effect = lambda cmd: {
        "j/clients": json.dumps(clients),
        "j/activeworkspace": json.dumps({"id": 1, "name": "1"}),
        "j/monitors": json.dumps(monitors),
        "j/workspaces": json.dumps(workspaces),
    }.get(cmd, "[]")
    ipc.eval_lua.return_value = "ok"
    return ipc


# --- registration on ON -----------------------------------------------------


def test_on_registers_rules_and_persists_names():
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch(
            "canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]
        ) as reg,
        patch("canvas.navigation.toggle_state.save") as msave,
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"
        reg.assert_called_once()
        assert msave.call_args[0][0][1]["spawn_rules"] == ["canvas-spawn-ws1-default"]


def test_on_registers_rules_before_touching_state():
    """A registration failure must leave state and the compositor untouched."""
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", side_effect=SpawnRuleError("boom")),
        patch("canvas.navigation.toggle_state.save") as msave,
        patch.object(nav, "_set_all_floating") as set_float,
    ):
        assert nav.canvas_toggle() == "ERROR:SPAWN_RULE_FAILED"

    msave.assert_not_called()
    set_float.assert_not_called()


def test_on_without_workarea_fails_cleanly():
    ipc = _ipc_with([_window("0x1")], workspaces=[{"id": 1, "monitorID": "null"}])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.toggle_state.save") as msave,
        patch.object(nav, "_set_all_floating") as set_float,
    ):
        assert nav.canvas_toggle() == "ERROR:SPAWN_RULE_FAILED"

    msave.assert_not_called()
    set_float.assert_not_called()


def test_on_compositor_failure_disables_the_rules():
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_set_all_floating", return_value=False),
    ):
        assert nav.canvas_toggle() == "ERROR:FLOAT_FAILED"

    dis.assert_called_once_with(["canvas-spawn-ws1-default"], ipc)


def test_on_state_save_failure_disables_the_rules():
    from canvas.toggle_state import ToggleStateError

    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.toggle_state.save", side_effect=ToggleStateError("disk")),
        patch.object(nav, "_set_all_floating") as set_float,
    ):
        assert nav.canvas_toggle() == "ERROR:STATE_SAVE_FAILED"

    dis.assert_called_once()
    set_float.assert_not_called()


def test_auto_float_off_never_touches_rules():
    """With the feature off, the whole path must be a no-op."""
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc, auto_float=False)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register") as reg,
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"
        assert nav.canvas_toggle() == "CANVAS_OFF"

    reg.assert_not_called()
    dis.assert_not_called()


# --- arrived windows on OFF -------------------------------------------------


def test_off_tiles_windows_that_arrived_during_canvas():
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    # A window opens during canvas: the rule made it floating, so it is not in
    # the snapshot taken at ON.
    ipc.send.side_effect = lambda cmd: {
        "j/clients": json.dumps([_window("0x1", floating=True), _window("0x9", floating=True)]),
        "j/activeworkspace": json.dumps({"id": 1, "name": "1"}),
        "j/monitors": json.dumps(
            [{"id": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "reserved": [0, 0, 0, 0]}]
        ),
        "j/workspaces": json.dumps([{"id": 1, "monitorID": 0}]),
    }.get(cmd, "[]")

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable"),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=True) as tile,
    ):
        assert nav.canvas_toggle() == "CANVAS_OFF"

    tiled = tile.call_args[0][1]
    assert "0x1" in tiled
    assert "0x9" in tiled


def test_off_tiles_arrived_window_even_with_an_empty_snapshot():
    """Canvas turned on with nothing tiled, then a window spawned floating.

    The arrived-window path used to be gated on a non-empty snapshot, which is
    exactly the case where nothing else would tile it — so the window stayed
    floating and centred after the toggle, and the marker cleared underneath
    it, making the toggle look like it had done nothing.
    """
    ipc = _ipc_with([])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"
    assert nav._canvas_mode_workspaces[1] == {}

    # The spawn rule floats the new window, so it never joins the snapshot.
    ipc.send.side_effect = lambda cmd: {
        "j/clients": json.dumps([_window("0x7", floating=True)]),
        "j/activeworkspace": json.dumps({"id": 1, "name": "1"}),
        "j/monitors": json.dumps(
            [{"id": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "reserved": [0, 0, 0, 0]}]
        ),
        "j/workspaces": json.dumps([{"id": 1, "monitorID": 0}]),
    }.get(cmd, "[]")

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable"),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=True) as tile,
    ):
        assert nav.canvas_toggle() == "CANVAS_OFF"

    tile.assert_called_once()
    assert "0x7" in tile.call_args[0][1]


def test_off_does_not_tile_windows_that_were_already_floating():
    ipc = _ipc_with([_window("0x1"), _window("0x2", floating=True)])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["r"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable"),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=True) as tile,
    ):
        assert nav.canvas_toggle() == "CANVAS_OFF"

    assert "0x2" not in tile.call_args[0][1]


def test_off_keeps_snapshot_geometry_for_arrived_windows():
    """Merging arrived windows must not clobber real snapshot geometry."""
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["r"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    # 0x1 was in the snapshot and got real geometry; 0x9 arrived later.
    nav._canvas_mode_workspaces[1]["0x1"] = {"at": [5, 6], "size": [7, 8]}
    nav._pre_floating[1] = set()
    ipc.send.side_effect = lambda cmd: {
        "j/clients": json.dumps([_window("0x1", floating=True), _window("0x9", floating=True)]),
        "j/activeworkspace": json.dumps({"id": 1, "name": "1"}),
        "j/monitors": "[]",
        "j/workspaces": "[]",
    }.get(cmd, "[]")

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable"),
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=True) as tile,
    ):
        assert nav.canvas_toggle() == "CANVAS_OFF"

    tiled = tile.call_args[0][1]
    assert tiled["0x1"] == {"at": [5, 6], "size": [7, 8]}
    assert tiled["0x9"] == {}


def test_off_disables_rules_only_after_successful_tiling():
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=False),
        patch.object(nav, "_set_snapshot_floating", return_value=True),
    ):
        assert nav.canvas_toggle() == "ERROR:TILE_FAILED"

    # Workspace rolled back to floating, so the rules must stay armed.
    dis.assert_not_called()


def test_off_disables_rules_on_success():
    ipc = _ipc_with([_window("0x1")])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["canvas-spawn-ws1-default"]),
        patch("canvas.navigation.toggle_state.save"),
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.toggle_state.save"),
        patch.object(nav, "_tile_windows", return_value=True),
    ):
        assert nav.canvas_toggle() == "CANVAS_OFF"

    dis.assert_called_once_with(["canvas-spawn-ws1-default"], ipc)
    assert nav._spawn_rules == {}


# --- restart rehydration ----------------------------------------------------


def test_rehydrate_disables_stale_rules_and_re_registers():
    ipc = _ipc_with([_window("0x1", floating=True)])
    with patch(
        "canvas.navigation.toggle_state.load",
        return_value={
            1: {
                "active": True,
                "tiled": {"0x1": {}},
                "floating": {},
                "pre_floating": [],
                "spawn_rules": ["canvas-spawn-ws1-default"],
            }
        },
    ):
        nav = Navigator(
            ipc=ipc, protected_apps=[], cooldown=0.0, auto_float=True, spawn_cfg=SPAWN_CFG
        )

    with (
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.spawnrules.register", return_value=["fresh"]) as reg,
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav.rehydrate_spawn_rules()

    dis.assert_called_once_with(["canvas-spawn-ws1-default"], ipc)
    reg.assert_called_once()
    assert nav._spawn_rules == {1: ["fresh"]}


def test_rehydrate_cleans_up_when_feature_is_off():
    """A crash with rules armed must not leave them live when the flag is off."""
    ipc = _ipc_with([_window("0x1", floating=True)])
    with patch(
        "canvas.navigation.toggle_state.load",
        return_value={
            1: {
                "active": True,
                "tiled": {"0x1": {}},
                "floating": {},
                "pre_floating": [],
                "spawn_rules": ["canvas-spawn-ws1-default"],
            }
        },
    ):
        nav = Navigator(
            ipc=ipc, protected_apps=[], cooldown=0.0, auto_float=False, spawn_cfg=SPAWN_CFG
        )

    with (
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.spawnrules.register") as reg,
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav.rehydrate_spawn_rules()

    dis.assert_called_once_with(["canvas-spawn-ws1-default"], ipc)
    reg.assert_not_called()
    assert nav._spawn_rules == {}


def test_rehydrate_survives_missing_state():
    ipc = _ipc_with([])
    with patch("canvas.navigation.toggle_state.load", return_value={}):
        nav = Navigator(
            ipc=ipc, protected_apps=[], cooldown=0.0, auto_float=True, spawn_cfg=SPAWN_CFG
        )

    with (
        patch("canvas.navigation.spawnrules.disable") as dis,
        patch("canvas.navigation.spawnrules.register") as reg,
        patch("canvas.navigation.toggle_state.save"),
    ):
        nav.rehydrate_spawn_rules()

    dis.assert_not_called()
    reg.assert_not_called()


# --- state round-trip -------------------------------------------------------


def test_pre_floating_survives_a_restart():
    ipc = _ipc_with([_window("0x1"), _window("0x2", floating=True)])
    nav = _navigator(ipc)
    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch("canvas.navigation.spawnrules.register", return_value=["r"]),
        patch("canvas.navigation.toggle_state.save") as msave,
    ):
        assert nav.canvas_toggle() == "CANVAS_ON"

    assert msave.call_args[0][0][1]["pre_floating"] == ["0x2"]


def test_old_state_without_new_sections_loads_as_empty():
    ipc = _ipc_with([])
    with patch(
        "canvas.navigation.toggle_state.load",
        return_value={1: {"active": True, "tiled": {"0x1": {}}, "floating": {}}},
    ):
        nav = Navigator(
            ipc=ipc, protected_apps=[], cooldown=0.0, auto_float=True, spawn_cfg=SPAWN_CFG
        )

    assert nav._pre_floating == {}
    assert nav._spawn_rules == {}


@pytest.mark.parametrize("version", [2, 3])
def test_state_migration_defaults_new_sections(version, tmp_path):
    from canvas.toggle_state import FORMAT_VERSION, load, save

    file = str(tmp_path / "t.json")
    save(
        {
            1: {
                "active": True,
                "tiled": {"0x1": {}},
                "floating": {},
                "pre_floating": [],
                "spawn_rules": [],
            }
        },
        path=file,
    )
    loaded = load(path=file)

    assert loaded[1]["pre_floating"] == []
    assert loaded[1]["spawn_rules"] == []
    assert FORMAT_VERSION == 4
    assert version in (2, 3)
