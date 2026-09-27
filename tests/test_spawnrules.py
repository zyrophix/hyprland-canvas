import json
from unittest.mock import MagicMock

import pytest

from canvas import spawnrules
from canvas.spawnrules import (
    SpawnRuleError,
    build_rules,
    default_rule_name,
    disable,
    override_rule_name,
    parse_size,
    register,
    resolve_workareas,
    rule_names,
    size_spec_error,
)

WORKAREA = (0, 44, 1920, 1036)


def _ipc(responses: dict[str, str]) -> MagicMock:
    ipc = MagicMock()
    ipc.send.side_effect = lambda cmd: responses[cmd]
    ipc.eval_lua.return_value = "ok"
    return ipc


# --- size parsing -----------------------------------------------------------


def test_pixel_size_is_used_as_is():
    assert parse_size("910x930", WORKAREA) == (910, 930)


def test_percent_size_resolves_against_workarea():
    # 30% of 1920 = 576, 40% of 1036 = 414.4 -> 414
    assert parse_size("30%x40%", WORKAREA) == (576, 414)


def test_size_is_clamped_to_workarea():
    """A rule must never ask for a window bigger than the screen."""
    assert parse_size("5000x5000", WORKAREA) == (1920, 1036)


def test_size_has_minimum_of_one_pixel():
    assert parse_size("0.01%x0.01%", WORKAREA) == (1, 1)


@pytest.mark.parametrize(
    "spec",
    ["910x930", "30%x40%", " 910x930 ", "100%x100%", "1x1"],
)
def test_valid_size_specs(spec):
    assert size_spec_error(spec) is None


@pytest.mark.parametrize(
    "spec",
    ["", "   ", None, 910, "910", "910*930", "910x930x2", "30%x40", "0x40", "101%x50%"],
)
def test_invalid_size_specs(spec):
    assert size_spec_error(spec) is not None


def test_mixed_units_cannot_be_written():
    """The separator carries the unit for both dimensions, so 910x40% is malformed."""
    assert size_spec_error("910x40%") is not None


def test_parse_size_raises_on_invalid_spec():
    with pytest.raises(SpawnRuleError):
        parse_size("nonsense", WORKAREA)


# --- rule building ----------------------------------------------------------


def test_catch_all_rule_is_registered_first():
    """The compositor applies matching rules in order, so the default must be first."""
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }
    rules = build_rules(cfg, 5, WORKAREA)

    assert [name for name, _ in rules] == [
        default_rule_name(5),
        override_rule_name(5, 0),
    ]


def test_catch_all_matches_any_class():
    cfg = {"center": True, "default": "600x400", "rules": []}
    _name, lua = build_rules(cfg, 1, WORKAREA)[0]

    assert 'class = ".*"' in lua
    assert "size = { 600, 400 }" in lua
    assert "float = true" in lua
    assert "center = true" in lua


def test_rule_places_a_window_without_restyling_it():
    """The rule says where a window goes. It does not say how it looks.

    It used to add immediate, no_anim, no_dim and no_shadow, which silenced
    animation, dimming and shadows. Registering a windowrule re-evaluates it
    against every mapped window, and the rule matches the whole workspace, so
    that reached windows which were already open and that the sizing never
    concerned itself with — a canvas that flattens them is not what the rule is
    for. no_blur is in this list because the old comment named it while the code
    set no_shadow instead.
    """
    cfg = {"center": True, "default": "600x400", "rules": []}
    _name, lua = build_rules(cfg, 1, WORKAREA)[0]

    for field in ("immediate", "no_anim", "no_dim", "no_shadow", "no_blur"):
        assert field not in lua


def test_center_can_be_disabled():
    cfg = {"center": False, "default": "600x400", "rules": []}
    _name, lua = build_rules(cfg, 1, WORKAREA)[0]

    assert "center = true" not in lua


def test_override_percent_resolved_per_workspace():
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "50%x50%"}],
    }
    _name, lua = build_rules(cfg, 2, (0, 0, 1000, 500))[1]

    assert "size = { 500, 250 }" in lua


def test_override_match_is_passed_through_verbatim():
    """User matchers are handed to the compositor as-is, regex and all."""
    cfg = {
        "center": True,
        "default": "30%x40%",
        "rules": [{"match": {"title": ".*nvim.*"}, "size": "800x600"}],
    }
    _name, lua = build_rules(cfg, 1, WORKAREA)[1]

    assert 'title = ".*nvim.*"' in lua


def test_bool_and_numeric_match_values_render_bare():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"floating": False, "pid": 3}, "size": "600x400"}],
    }
    _name, lua = build_rules(cfg, 1, WORKAREA)[1]

    assert "floating = false" in lua
    assert "pid = 3" in lua


def test_match_string_values_are_escaped():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"class": 'ev"il'}, "size": "600x400"}],
    }
    _name, lua = build_rules(cfg, 1, WORKAREA)[1]

    assert r'class = "ev\"il"' in lua


def test_unknown_match_property_rejected():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"klass": "btop"}, "size": "600x400"}],
    }
    with pytest.raises(SpawnRuleError, match="unknown match property"):
        build_rules(cfg, 1, WORKAREA)


def test_empty_match_rejected():
    cfg = {"center": True, "default": "600x400", "rules": [{"match": {}, "size": "600x400"}]}
    with pytest.raises(SpawnRuleError):
        build_rules(cfg, 1, WORKAREA)


def test_invalid_override_size_rejected():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"class": "btop"}, "size": "big"}],
    }
    with pytest.raises(SpawnRuleError):
        build_rules(cfg, 1, WORKAREA)


def test_rule_names_are_deterministic():
    assert rule_names(5, 2) == [
        "canvas-spawn-ws5-default",
        "canvas-spawn-ws5-override-0",
        "canvas-spawn-ws5-override-1",
    ]
    assert rule_names(5, 2) == rule_names(5, 2)


# --- workarea resolution ----------------------------------------------------


def test_resolve_workareas_maps_workspace_to_its_monitor():
    ipc = _ipc(
        {
            "j/monitors": json.dumps(
                [
                    {
                        "id": 0,
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                        "reserved": [0, 44, 0, 0],
                    },
                    {
                        "id": 1,
                        "x": 1920,
                        "y": 0,
                        "width": 2560,
                        "height": 1440,
                        "reserved": [0, 0, 0, 0],
                    },
                ]
            ),
            "j/workspaces": json.dumps(
                [
                    {"id": 1, "monitorID": 0},
                    {"id": 2, "monitorID": 1},
                ]
            ),
        }
    )

    # 44px bar at the top: the workarea starts below it, and keeps full width.
    assert resolve_workareas(ipc) == {1: (0, 44, 1920, 1036), 2: (1920, 0, 2560, 1440)}


def test_resolve_workareas_subtracts_reserved_on_all_sides():
    ipc = _ipc(
        {
            "j/monitors": json.dumps(
                [
                    {
                        "id": 0,
                        "x": 0,
                        "y": 0,
                        "width": 1000,
                        "height": 800,
                        "reserved": [10, 20, 30, 40],
                    }
                ]
            ),
            "j/workspaces": json.dumps([{"id": 1, "monitorID": 0}]),
        }
    )

    # reserved is serialised left, top, right, bottom
    assert resolve_workareas(ipc) == {1: (10, 20, 960, 740)}


def test_resolve_workareas_skips_workspace_without_monitor():
    """Hyprland reports monitorID as the string "null" for an orphaned workspace."""
    ipc = _ipc(
        {
            "j/monitors": json.dumps(
                [{"id": 0, "x": 0, "y": 0, "width": 1000, "height": 800, "reserved": [0, 0, 0, 0]}]
            ),
            "j/workspaces": json.dumps([{"id": 1, "monitorID": "null"}]),
        }
    )

    assert resolve_workareas(ipc) == {}


def test_resolve_workareas_survives_ipc_failure():
    ipc = MagicMock()
    ipc.send.side_effect = OSError("socket gone")

    assert resolve_workareas(ipc) == {}


# --- registration lifecycle -------------------------------------------------


def test_register_sends_one_batch_and_returns_names():
    ipc = _ipc({})
    rules = build_rules({"center": True, "default": "600x400", "rules": []}, 1, WORKAREA)

    names = register(rules, ipc)

    assert names == ["canvas-spawn-ws1-default"]
    assert ipc.eval_lua.call_count == 1
    code = ipc.eval_lua.call_args[0][0]
    assert "hl.window_rule" in code
    assert "set_enabled(true)" in code


def test_register_of_nothing_does_no_ipc():
    ipc = _ipc({})

    assert register([], ipc) == []
    ipc.eval_lua.assert_not_called()


def test_register_failure_undoes_partial_batch():
    """A batch that aborts halfway must not leave earlier rules live."""
    ipc = _ipc({})
    ipc.eval_lua.side_effect = [RuntimeError("lua exploded"), "ok"]
    rules = build_rules({"center": True, "default": "600x400", "rules": []}, 1, WORKAREA)

    with pytest.raises(SpawnRuleError):
        register(rules, ipc)

    cleanup_code = ipc.eval_lua.call_args[0][0]
    assert "enabled = false" in cleanup_code
    assert "canvas-spawn-ws1-default" in cleanup_code


def test_disable_reports_success():
    ipc = _ipc({})

    assert disable(["canvas-spawn-ws1-default"], ipc) is True
    assert "canvas-spawn-ws1-default" in ipc.eval_lua.call_args[0][0]
    assert "enabled = false" in ipc.eval_lua.call_args[0][0]


def test_disable_of_nothing_does_no_ipc():
    ipc = _ipc({})

    assert disable([], ipc) is True
    ipc.eval_lua.assert_not_called()


def test_disable_failure_is_reported_not_raised():
    """OFF must not blow up; the names stay persisted and are retried at startup."""
    ipc = _ipc({})
    ipc.eval_lua.side_effect = RuntimeError("nope")

    assert disable(["canvas-spawn-ws1-default"], ipc) is False


# --- local matching for already open windows --------------------------------
#
# Hyprland matches windowrule patterns with re2::RE2::FullMatch, so these have
# to agree or an existing window and a new one would end up different sizes.


def test_default_rule_is_scoped_to_the_workspace():
    """The catch-all used to be `class = ".*"` with no workspace condition, so a
    rule registered for one canvas workspace shaped windows in every workspace.
    """
    cfg = {"center": True, "default": "30%x40%"}
    for name, lua in build_rules(cfg, 4, WORKAREA):
        assert "workspace = 4" in lua, lua
        assert name == "canvas-spawn-ws4-default"


def test_override_rules_are_scoped_too():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"class": "btop"}, "size": "910x930"}],
    }
    for _name, lua in build_rules(cfg, 7, WORKAREA):
        assert "workspace = 7" in lua, lua


def test_two_workspaces_get_distinct_workspace_conditions():
    """Otherwise the second registration wins everywhere, sizing windows on the
    first workspace against the second workspace's workarea."""
    cfg = {"center": True, "default": "30%x40%"}
    ws4 = build_rules(cfg, 4, WORKAREA)[0][1]
    ws9 = build_rules(cfg, 9, WORKAREA)[0][1]

    assert "workspace = 4" in ws4
    assert "workspace = 9" in ws9
    assert "workspace = 4" not in ws9


def test_rule_declaring_a_different_workspace_is_rejected():
    """It cannot be satisfied by a rule registered for this workspace, so it is
    an error rather than a match that never fires."""
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"class": "btop", "workspace": 3}, "size": "910x930"}],
    }
    with pytest.raises(SpawnRuleError) as exc:
        build_rules(cfg, 1, WORKAREA)
    assert "workspace 1" in str(exc.value)


def test_rule_repeating_its_own_workspace_is_accepted():
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": {"class": "btop", "workspace": 1}, "size": "910x930"}],
    }
    rules = build_rules(cfg, 1, WORKAREA)
    assert len(rules) == 2
    assert "workspace = 1" in rules[1][1]


def test_match_must_be_a_mapping():
    """build_rules now validates the shape, since it has to add a key to it."""
    cfg = {
        "center": True,
        "default": "600x400",
        "rules": [{"match": "btop", "size": "910x930"}],
    }
    with pytest.raises(SpawnRuleError) as exc:
        build_rules(cfg, 1, WORKAREA)
    assert "must be a non-empty mapping" in str(exc.value)


def test_reserved_order_is_left_top_right_bottom():
    """A top bar must shrink the height and shift y, not narrow the width.

    Hyprland serialises m_reservedArea as left, top, right, bottom
    (src/ipc/s1/Commands.cpp:287-288). Reading it rotated put a top bar into
    the width: a 44px bar yielded a workarea 44px narrower and 44px taller,
    so every percent size and every computed centre was wrong. It only shows
    up with a non-zero reserved area, which is why a barless setup never
    noticed.
    """
    ipc = _ipc(
        {
            "j/monitors": json.dumps(
                [
                    {
                        "id": 0,
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                        "reserved": [0, 44, 0, 0],
                    }
                ]
            ),
            "j/workspaces": json.dumps([{"id": 1, "monitorID": 0}]),
        }
    )
    assert resolve_workareas(ipc) == {1: (0, 44, 1920, 1036)}


def test_percent_size_uses_the_real_workarea():
    """The end-to-end consequence: 30%x40% of a 1920x1036 workarea."""
    assert parse_size("30%x40%", (0, 44, 1920, 1036)) == (576, 414)


@pytest.mark.parametrize("spec", ["9" * 5000 + "x100", "9" * 5000 + "%x50%", "100x" + "9" * 5000])
def test_absurdly_long_size_is_rejected_as_a_bad_spec(spec):
    """A long spec must fail validation, not raise from int().

    CPython refuses to convert a decimal string longer than 4300 digits and
    raises ValueError. That is not a SpawnRuleError, so an unbounded spec that
    reached int() would pass config validation — which calls size_spec_error —
    and then abort the first canvas toggle with an exception nothing catches.
    """
    problem = size_spec_error(spec)
    assert problem is not None
    assert "is not a size" in problem

    with pytest.raises(SpawnRuleError):
        parse_size(spec, WORKAREA)


@pytest.mark.parametrize(
    "spec,expected", [("7680x4320", (7680, 4320)), ("30.5%x40%", (3050, 4000))]
)
def test_realistic_sizes_still_pass_the_digit_limit(spec, expected):
    """The bound must not reject anything a real display needs."""
    assert size_spec_error(spec) is None
    assert parse_size(spec, (0, 0, 10000, 10000)) == expected


def test_fractional_pixels_are_rejected():
    """100.5x50 used to pass validation and then raise out of int()."""
    assert "whole numbers" in str(size_spec_error("100.5x50"))
    with pytest.raises(SpawnRuleError):
        parse_size("100.5x50", WORKAREA)


def test_disable_escapes_rule_names():
    """A name with a quote or a newline must not break the batch.

    The names come from the state file, which is hand-editable, and the whole
    batch is submitted as one eval — so a single unescaped name is a Lua syntax
    error that leaves every real rule in the batch still enabled. This is the
    same escaping every other interpolation on this path uses, and the one place
    that was skipping it.
    """
    ipc = _ipc({})
    hostile = 'canvas-spawn-ws4"\nprint(1)\n--'

    spawnrules.disable([hostile], ipc)

    lua = ipc.eval_lua.call_args[0][0]
    assert "\n" not in lua.split("enabled = false")[0].split("name = ")[1]
    assert '\\"' in lua
    assert "\\n" in lua


def test_disable_escapes_a_plain_name_without_changing_it():
    """Escaping must not corrupt the ordinary case."""
    ipc = _ipc({})
    spawnrules.disable(["canvas-spawn-ws4-default"], ipc)
    assert 'name = "canvas-spawn-ws4-default"' in ipc.eval_lua.call_args[0][0]
