"""Tests for the 1.7.0 behaviour fixes.

Each test here would pass against the pre-1.7.0 implementation if the fix it
covers were reverted, except where noted.
"""

import json
import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from canvas import toggle_state
from canvas.config import DEFAULT_CONFIG, ConfigError, _deep_merge, load, validate
from canvas.ipc import _MY_UID, _ensure_private_dir, runtime_dir, send_command
from canvas.navigation import FSMODE_FULLSCREEN, Navigator, fullscreen_state
from canvas.spawnrules import build_rules


def _make_nav(ipc: MagicMock | None = None, **kwargs: object) -> Navigator:
    """A Navigator with no on-disk state, so tests do not read the real file."""
    with patch("canvas.navigation.toggle_state.load", return_value={}):
        return Navigator(ipc=ipc or MagicMock(), cooldown=0.0, **kwargs)  # type: ignore[arg-type]


def _win(addr: str, cls: str = "kitty", **over: object) -> dict:
    w: dict = {
        "address": addr,
        "class": cls,
        "floating": True,
        "at": [0, 0],
        "size": [100, 100],
        "workspace": {"id": 1},
    }
    w.update(over)
    return w


# --- fullscreen is eFullscreenMode, not a bool -----------------------------


def test_fullscreen_state_reads_the_enum():
    """0 none, 1 maximized, 2 fullscreen; anything unusable reads as 0."""
    assert fullscreen_state({"fullscreen": 2}) == FSMODE_FULLSCREEN
    assert fullscreen_state({"fullscreen": 1}) == 1
    assert fullscreen_state({"fullscreen": 0}) == 0
    assert fullscreen_state({}) == 0
    assert fullscreen_state({"fullscreen": None}) == 0
    assert fullscreen_state({"fullscreen": "2"}) == 2
    assert fullscreen_state({"fullscreen": "junk"}) == 0


def test_excluded_covers_fullscreen_hidden_pinned_and_class():
    """A maximized window is not fullscreen, so it stays on the canvas."""
    nav = _make_nav()
    nav._pan_exclude_apps = ["picture-in-picture"]

    assert nav.excluded(_win("0x1", fullscreen=2)) is True
    assert nav.excluded(_win("0x2", hidden=True)) is True
    assert nav.excluded(_win("0x3", pinned=True)) is True
    assert nav.excluded(_win("0x4", "Picture-in-Picture")) is True

    assert nav.excluded(_win("0x5", fullscreen=1)) is False
    assert nav.excluded(_win("0x6")) is False


def test_excluded_addresses_is_one_query_and_workspace_scoped():
    """Floating, on this workspace, and excluded — nothing else qualifies."""
    ipc = MagicMock()
    ipc.send.return_value = json.dumps(
        [
            _win("0x1"),
            _win("0x2", fullscreen=2),
            _win("0x3", fullscreen=2, workspace={"id": 2}),
            _win("0x4", fullscreen=2, floating=False),
        ]
    )
    nav = _make_nav(ipc)

    assert nav.excluded_addresses(1) == {"0x2"}
    assert ipc.send.call_count == 1


# --- navigation must not move or target an excluded window -----------------


def test_navigate_ignores_an_excluded_window_as_a_target():
    """The window to the right is fullscreen, so it is not a destination."""
    nav = _make_nav()
    current = _win("0x1", at=[0, 0])
    fullscreen = _win("0x2", at=[400, 0], fullscreen=2)
    other = _win("0x3", at=[0, 400])

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=[current, fullscreen, other]),
        patch.object(nav, "_get_focused_window", return_value=current),
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "_pan_to_window", return_value=True) as pan,
    ):
        assert nav.navigate("right") is True

    pan.assert_called_once()
    args = pan.call_args.args
    assert args[1] == "0x3"  # the fullscreen window was not chosen
    assert args[5] == {"0x2"}  # and it is carried through as the skip set


def test_navigate_stops_when_filtering_leaves_one_window():
    """Two floating windows, one excluded: nothing left to navigate between.

    The risk this guards is the filter turning into a silent pan of the one
    survivor instead of a no-op.
    """
    nav = _make_nav()
    current = _win("0x1")
    fullscreen = _win("0x2", at=[400, 0], fullscreen=2)

    with (
        patch.object(nav, "_get_active_workspace_id", return_value=1),
        patch.object(nav, "_get_floating_windows", return_value=[current, fullscreen]),
        patch.object(nav, "_get_focused_window", return_value=current),
        patch.object(nav, "_pan_to_window", return_value=True) as pan,
    ):
        assert nav.navigate("right") is True

    pan.assert_not_called()


def test_navigate_pan_lua_skips_excluded_addresses():
    """The exclusion has to reach the Lua, not just the Python-side choice."""
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)

    assert nav._pan_to_window([_win("0x1"), _win("0x2")], "0x1", 960, 540, 1, {"0x2"}) is True

    lua = ipc.eval_lua.call_args.args[0]
    assert '["0x2"] = true' in lua
    assert "if not skip[tostring(w.address)] then" in lua


def test_navigate_pan_lua_without_exclusions_still_defines_skip():
    """An empty skip set must still exist, or the guard fails to compile."""
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)

    assert nav._pan_to_window([_win("0x1")], "0x1", 960, 540, 1, None) is True

    lua = ipc.eval_lua.call_args.args[0]
    assert "local skip = {}" in lua


# --- centering cannot move a window that is excluded from moving -----------


def test_center_window_refuses_an_excluded_target():
    """Centering shifts every other window, so an excluded one cannot be it."""
    ipc = MagicMock()
    nav = _make_nav(ipc)

    with (
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "excluded_addresses", return_value={"0x1"}),
    ):
        assert nav.center_window("0x1", workspace_id=1, cursor_x=1, cursor_y=1) is False

    ipc.eval_lua.assert_not_called()


def test_center_window_lua_skips_excluded_addresses():
    ipc = MagicMock()
    ipc.eval_lua.return_value = "ok"
    nav = _make_nav(ipc)

    with (
        patch.object(nav, "_get_monitor_center", return_value=(960, 540)),
        patch.object(nav, "excluded_addresses", return_value={"0x9"}),
    ):
        assert nav.center_window("0x1", workspace_id=1, cursor_x=1, cursor_y=1) is True

    lua = ipc.eval_lua.call_args.args[0]
    assert '["0x9"] = true' in lua
    assert "if not skip[tostring(w.address)] then" in lua


# --- the /tmp fallback runtime directory ----------------------------------
#
# $XDG_RUNTIME_DIR and /run/user/<uid> are made by the session manager. The
# fallback is the one path another local user can create first, so it is the
# one that has to be created private and checked.


def test_ensure_private_dir_creates_it_0700(tmp_path):
    target = tmp_path / "user" / "1000"

    _ensure_private_dir(str(target))

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_private_dir_rejects_a_symlink(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "user" / "1000"
    link.parent.mkdir()
    link.symlink_to(real)

    with pytest.raises(RuntimeError, match="is not a directory"):
        _ensure_private_dir(str(link))


def test_ensure_private_dir_rejects_a_foreign_owner(tmp_path):
    target = tmp_path / "user" / "1000"
    target.mkdir(parents=True)

    with (
        patch("canvas.ipc._MY_UID", _MY_UID + 1),
        pytest.raises(RuntimeError, match="owned by uid"),
    ):
        _ensure_private_dir(str(target))


def test_ensure_private_dir_rejects_a_regular_file(tmp_path):
    target = tmp_path / "user" / "1000"
    target.parent.mkdir()
    target.write_text("not a directory")

    with pytest.raises(RuntimeError, match="could not create"):
        _ensure_private_dir(str(target))


def test_runtime_dir_hardens_the_tmp_fallback():
    with (
        patch("canvas.ipc.os.path.isdir", return_value=False),
        patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}),
        patch("canvas.ipc._ensure_private_dir") as ensure,
    ):
        assert runtime_dir() == f"/tmp/user/{_MY_UID}"

    ensure.assert_called_once_with(f"/tmp/user/{_MY_UID}")


def test_runtime_dir_takes_xdg_at_face_value():
    """It is set by the session manager, so it needs no check of its own."""
    with (
        patch("canvas.ipc.os.path.isdir", return_value=False),
        patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}),
    ):
        assert runtime_dir() == "/run/user/4242"


def test_toggle_state_path_lives_under_the_runtime_dir():
    with patch("canvas.toggle_state.runtime_dir", return_value="/run/user/4242"):
        assert toggle_state.default_path() == "/run/user/4242/canvas/toggle-state.json"


def test_send_command_reports_an_unusable_runtime_dir():
    """The client contract is a string starting with ERROR:, not a traceback."""
    with patch("canvas.ipc._default_socket_path", side_effect=RuntimeError("owned by uid 0")):
        result = send_command("PING")

    assert result.startswith("ERROR: owned by uid 0")


# --- config keys this version does not read -------------------------------


def _validate(**over: object) -> list[str]:
    return validate(_deep_merge(DEFAULT_CONFIG, over))


def test_validate_rejects_an_unknown_top_level_key():
    problems = _validate(totally_unknown=1)

    assert any("totally_unknown" in p and "not a setting" in p for p in problems), problems


def test_validate_rejects_a_typo_inside_a_section():
    problems = _validate(navigation={"protcted_apps": ["x"]})

    assert any("navigation.protcted_apps" in p for p in problems), problems


def test_validate_accepts_a_setting_whose_default_is_none():
    """max_speed defaults to None while a number is valid, so only the key is
    compared — a type test against the default would reject every real value."""
    assert _validate(max_speed=5) == []
    assert _validate(edge_scroll={"max_speed": 30}) == []


def test_validate_does_not_walk_into_lists():
    """protected_apps and window_pan_excludes hold strings, not mappings."""
    assert _validate(window_pan_excludes=["zoom", "slack"]) == []
    assert _validate(navigation={"protected_apps": ["firefox"]}) == []


# --- canvas.spawn.rules was removed ---------------------------------------
#
# The generic "not a setting" message is technically true and practically
# useless here: the reader is mid-upgrade with a config that stopped working,
# and needs to be told what to do with the key.


def test_validate_explains_the_removed_spawn_rules_key():
    problems = _validate(canvas={"spawn": {"center": True, "default": "50%x60%", "rules": []}})

    assert len(problems) == 1, problems
    assert "canvas.spawn.rules" in problems[0]
    assert "removed in 1.7.0" in problems[0]
    assert "windowrule" in problems[0]


def test_load_refuses_a_config_with_the_removed_key(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("canvas:\n  spawn:\n    rules: []\n")

    with pytest.raises(ConfigError) as exc:
        load(str(path), skip_user=True)

    assert "canvas.spawn.rules" in str(exc.value)
    assert str(path) in str(exc.value)


def test_build_rules_no_longer_reads_a_rules_key():
    """A leftover rules list must not add a second rule."""
    cfg = {
        "center": True,
        "default": "50%x60%",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }

    assert [name for name, _ in build_rules(cfg, 1, (0, 0, 1920, 1036))] == [
        "canvas-spawn-ws1-default"
    ]


# --- auto_float is on by default ------------------------------------------


def test_auto_float_is_on_by_default():
    """Windows arriving on a canvas workspace used to land tiled and on top of
    each other unless the user found the setting. Shaping them is the point of
    the feature, so the default is on."""
    assert DEFAULT_CONFIG["canvas"]["auto_float"] is True


def test_the_bundled_template_ships_auto_float_on():
    """config.yml is the copy-paste template; it has to agree with the default."""
    cfg = load("/nonexistent/path/config.yml", skip_user=True)

    assert cfg["canvas"]["auto_float"] is True
