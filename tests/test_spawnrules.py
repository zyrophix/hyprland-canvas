import json
from unittest.mock import MagicMock

import pytest

from canvas.spawnrules import (
    SpawnRuleError,
    build_rules,
    default_rule_name,
    disable,
    full_match,
    override_rule_name,
    parse_size,
    register,
    resolve_spec,
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
    assert "immediate = true" in lua
    assert "no_anim = true" in lua


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
        "rules": [{"match": {"floating": False, "workspace": 3}, "size": "600x400"}],
    }
    _name, lua = build_rules(cfg, 1, WORKAREA)[1]

    assert "floating = false" in lua
    assert "workspace = 3" in lua


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
                        "reserved": [0, 0, 44, 0],
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

    assert resolve_workareas(ipc) == {1: (0, 0, 1920, 1036), 2: (1920, 0, 2560, 1440)}


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

    assert resolve_workareas(ipc) == {1: (40, 10, 940, 760)}


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


def test_match_is_a_full_match_not_a_search():
    assert full_match("btop", "btop")
    assert not full_match("btop", "btop-extra")
    assert full_match(".*btop.*", "btop-extra")


def test_negative_prefix_inverts_the_match():
    assert full_match("negative:btop", "kitty")
    assert not full_match("negative:btop", "btop")


def test_uncompilable_pattern_is_ignored_not_fatal():
    assert full_match("(unclosed", "anything") is False
    assert full_match("negative:(unclosed", "anything") is False


def test_resolve_spec_uses_the_default_without_rules():
    cfg = {"default": "30%x40%", "rules": []}
    assert resolve_spec(cfg, {"class": "kitty"}) == "30%x40%"


def test_resolve_spec_first_match_wins():
    cfg = {
        "default": "30%x40%",
        "rules": [
            {"match": {"class": "btop"}, "size": "910x930"},
            {"match": {"class": "kitty"}, "size": "800x600"},
        ],
    }
    assert resolve_spec(cfg, {"class": "btop"}) == "910x930"
    assert resolve_spec(cfg, {"class": "kitty"}) == "800x600"
    assert resolve_spec(cfg, {"class": "zen"}) == "30%x40%"


def test_resolve_spec_requires_every_matcher_to_match():
    cfg = {
        "default": "30%x40%",
        "rules": [{"match": {"class": "kitty", "title": ".*nvim.*"}, "size": "800x600"}],
    }
    assert resolve_spec(cfg, {"class": "kitty", "title": "nvim notes"}) == "800x600"
    assert resolve_spec(cfg, {"class": "kitty", "title": "zsh"}) == "30%x40%"


def test_resolve_spec_skips_rules_it_cannot_evaluate_locally():
    """workspace/pid matchers only exist in the compositor, so we cannot apply them."""
    cfg = {
        "default": "30%x40%",
        "rules": [{"match": {"workspace": 3}, "size": "100x100"}],
    }
    assert resolve_spec(cfg, {"class": "kitty"}) == "30%x40%"


def test_resolve_spec_skips_malformed_entries():
    cfg = {
        "default": "30%x40%",
        "rules": ["nonsense", {"match": {}, "size": "1x1"}, {"match": {"class": "btop"}}],
    }
    assert resolve_spec(cfg, {"class": "btop"}) == "30%x40%"


def test_resolve_spec_skips_a_rule_with_an_unusable_size():
    cfg = {
        "default": "30%x40%",
        "rules": [{"match": {"class": "btop"}, "size": "huge"}],
    }
    assert resolve_spec(cfg, {"class": "btop"}) == "30%x40%"


def test_resolve_spec_matches_the_compositor_result():
    """The rule handed to the compositor and the local pick must agree.

    Otherwise a window opened while canvas is on would come out a different
    size from the same window that was already open when canvas was enabled.
    """
    rules = [
        {"match": {"class": "btop"}, "size": "910x930"},
        {"match": {"class": ".*term.*", "title": ".*log.*"}, "size": "50%x40%"},
    ]
    cfg = {"default": "30%x40%", "rules": rules}
    workarea = (0, 0, 1920, 1080)
    built = {name: lua for name, lua in build_rules(cfg, 1, workarea)}

    for index, (props, rule) in enumerate(
        [
            ({"class": "btop", "title": "x"}, rules[0]),
            ({"class": "myterm", "title": "syslog"}, rules[1]),
        ]
    ):
        spec = resolve_spec(cfg, props)
        assert spec == rule["size"]
        width, height = parse_size(spec, workarea)
        lua = built[override_rule_name(1, index)]
        assert f"size = {{ {width}, {height} }}" in lua
