import os
import tempfile
from unittest.mock import patch

import pytest

from canvas.config import DEFAULT_CONFIG, ConfigError, load, validate


def test_load_default_config_when_no_file():
    """Config returns hardcoded defaults when no YAML files exist at all."""
    # skip_user=True + path that doesn't exist → falls through to bundled config.yml
    # which is merged over DEFAULT_CONFIG. The merge is what this checks, not
    # any single default value.
    cfg = load("/nonexistent/path/config.yml", skip_user=True)
    # Bundled config.yml values match DEFAULT_CONFIG
    assert cfg["idle_timeout"] == DEFAULT_CONFIG["idle_timeout"]
    # Navigation defaults from DEFAULT_CONFIG still apply
    assert cfg["navigation"]["cooldown"] == 0.2
    assert "firefox" in cfg["navigation"]["protected_apps"]
    assert cfg["invert"]["enabled"] is True


def test_load_partial_config_merges_defaults():
    """Partial YAML file merges with defaults."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("pan_step: 250\ninvert:\n  enabled: true\n")
        f.flush()
        cfg = load(f.name)
    os.unlink(f.name)

    assert cfg["pan_step"] == 250
    assert cfg["invert"]["enabled"] is True
    # defaults still present
    assert cfg["navigation"]["cooldown"] == DEFAULT_CONFIG["navigation"]["cooldown"]


def test_load_full_config():
    """Full YAML file overrides all defaults."""
    yaml_content = """pan_step: 200
navigation:
  cooldown: 0.5
  protected_apps:
    - my-browser
invert:
  enabled: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load(f.name)
    os.unlink(f.name)

    assert cfg["pan_step"] == 200
    assert cfg["navigation"]["cooldown"] == 0.5
    assert cfg["navigation"]["protected_apps"] == ["my-browser"]
    assert cfg["invert"]["enabled"] is True


def test_deep_merge_does_not_mutate_defaults():
    """Loading config must not mutate DEFAULT_CONFIG."""
    from canvas.config import DEFAULT_CONFIG as dc

    original_step = dc["pan_step"]
    original_protected = list(dc["navigation"]["protected_apps"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("pan_step: 99\nnavigation:\n  protected_apps:\n    - hacked\n")
        f.flush()
        cfg = load(f.name)
    os.unlink(f.name)

    # The returned config has overrides
    assert cfg["pan_step"] == 99
    assert cfg["navigation"]["protected_apps"] == ["hacked"]
    # But DEFAULT_CONFIG is untouched
    assert dc["pan_step"] == original_step
    assert dc["navigation"]["protected_apps"] == original_protected


# --- validation ---


def _load_yaml(content: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(content)
        f.flush()
        name = f.name
    try:
        return load(name)
    finally:
        os.unlink(name)


def test_validate_default_config_has_no_problems():
    import copy

    assert validate(copy.deepcopy(DEFAULT_CONFIG)) == []


def test_validate_reports_the_removed_pan_keys():
    """Both were removed rather than defaulted, so a config still carrying
    them is told to delete them instead of silently changing behaviour."""
    problems = validate({"speed": 1.6, "max_speed": 50})

    assert any("speed was removed" in p for p in problems)
    assert any("max_speed was removed" in p for p in problems)


def test_validate_rejects_negative_cooldown():
    problems = validate({"navigation": {"cooldown": -1}})
    assert any("cooldown" in p for p in problems)


def test_validate_rejects_non_list_protected_apps():
    problems = validate({"navigation": {"protected_apps": "firefox"}})
    assert any("protected_apps" in p for p in problems)


def test_validate_rejects_non_dict_section():
    problems = validate({"invert": None})
    assert any("invert" in p for p in problems)


def test_validate_rejects_zero_ramp_distance():
    problems = validate({"edge_scroll": {"ramp_distance": 0, "speed": 20.0, "enabled": True}})
    assert any("ramp_distance" in p for p in problems)


def test_validate_rejects_float_ramp_distance():
    problems = validate({"edge_scroll": {"ramp_distance": 50.5, "speed": 20.0, "enabled": True}})
    assert any("ramp_distance" in p for p in problems)


def test_load_raises_config_error_listing_problems():
    with pytest.raises(ConfigError) as exc_info:
        _load_yaml("idle_timeout: -1\ninvert:\n  enabled: maybe\n")
    msg = str(exc_info.value)
    assert "idle_timeout" in msg
    assert "invert.enabled" in msg


def test_load_valid_partial_config_passes():
    cfg = _load_yaml("pan_step: 25\n")
    assert cfg["pan_step"] == 25


# --- toggle_state persistence ---


def test_toggle_state_roundtrip(tmp_path):
    from canvas.toggle_state import FORMAT_VERSION
    from canvas.toggle_state import load as ts_load
    from canvas.toggle_state import save as ts_save

    file = str(tmp_path / "toggle.json")
    state = {
        1: {
            "active": True,
            "tiled": {"0xabc": {"at": [10, 20], "size": [500, 300]}, "0x2": {}},
            "floating": {"0xabc": {"at": [11, 21], "size": [500, 300]}},
            "pre_floating": ["0x2", "0x9"],
            "spawn_rules": ["canvas-spawn-ws1-default"],
        },
        7: {
            "active": True,
            "tiled": {"0xfff": {"at": [0, 0], "size": [100, 100]}},
            "floating": {},
            "pre_floating": [],
            "spawn_rules": [],
        },
    }
    ts_save(state, path=file)
    loaded = ts_load(path=file)
    assert loaded == state

    import json

    with open(file) as f:
        raw = json.load(f)
    assert raw["_v"] == FORMAT_VERSION
    assert raw["1"]["active"] is True


def test_toggle_state_v2_infers_active_from_tiled_snapshot(tmp_path):
    import json

    from canvas.toggle_state import load as ts_load

    file = tmp_path / "toggle-v2.json"
    file.write_text(
        json.dumps(
            {
                "_v": 2,
                "1": {"tiled": {"0xabc": {}}, "floating": {}},
                "2": {"tiled": {}, "floating": {}},
            }
        )
    )

    loaded = ts_load(path=str(file))
    assert loaded[1]["active"] is True
    assert loaded[2]["active"] is False


def test_toggle_state_v3_preserves_empty_active_marker(tmp_path):
    import json

    from canvas.toggle_state import load as ts_load

    file = tmp_path / "toggle-v3.json"
    file.write_text(json.dumps({"_v": 3, "1": {"active": True, "tiled": {}, "floating": {}}}))

    loaded = ts_load(path=str(file))
    assert loaded[1]["active"] is True


def test_toggle_state_roundtrip_legacy_list(tmp_path):
    """Old list format migrates to tiled-only, floating stays empty."""
    from canvas.toggle_state import load as ts_load

    file = str(tmp_path / "toggle.json")
    # Simulate old file on disk directly
    import json

    with open(file, "w") as f:
        json.dump({"1": ["0xabc", "0x2"], "7": ["0xfff"]}, f)
    loaded = ts_load(path=file)
    assert loaded[1]["active"] is True
    assert set(loaded[1]["tiled"].keys()) == {"0xabc", "0x2"}
    assert loaded[1]["floating"] == {}
    assert loaded[7]["active"] is True
    assert set(loaded[7]["tiled"].keys()) == {"0xfff"}


def test_toggle_state_migrates_v1_dict_dropping_geos(tmp_path):
    """v1 files (addr->tiled geo, no _v) keep addresses, drop stale geos."""
    from canvas.toggle_state import load as ts_load

    file = str(tmp_path / "toggle.json")
    import json

    with open(file, "w") as f:
        json.dump({"1": {"0xabc": {"at": [10, 20], "size": [500, 300]}}}, f)
    loaded = ts_load(path=file)
    # Address kept for OFF targeting, geometry dropped (was tiled slots)
    assert loaded[1]["active"] is True
    assert set(loaded[1]["tiled"].keys()) == {"0xabc"}
    assert loaded[1]["tiled"]["0xabc"] == {}
    assert loaded[1]["floating"] == {}


def test_toggle_state_load_missing_file(tmp_path):
    from canvas.toggle_state import load as ts_load

    assert ts_load(path=str(tmp_path / "absent.json")) == {}


def test_toggle_state_load_corrupt_file(tmp_path):
    import logging

    logging.disable(logging.CRITICAL)
    from canvas.toggle_state import load as ts_load

    file = tmp_path / "bad.json"
    file.write_text("{not json")
    try:
        assert ts_load(path=str(file)) == {}
    finally:
        logging.disable(logging.NOTSET)


def test_validate_rejects_bad_grab_dead_zone():
    problems = validate(
        {
            "edge_scroll": {
                "ramp_distance": 50,
                "speed": 20.0,
                "enabled": True,
                "grab_dead_zone": 0,
            },
        }
    )
    assert any("grab_dead_zone" in p for p in problems)


@pytest.mark.parametrize("content", ["idle_timeout: .nan\n", "idle_timeout: .inf\n"])
def test_load_rejects_non_finite_numbers(tmp_path, content):
    path = tmp_path / "config.yml"
    path.write_text(content)

    with pytest.raises(ConfigError, match="idle_timeout must be a number"):
        load(str(path))


@pytest.mark.parametrize("content", ["[]\n", "hello\n"])
def test_load_rejects_non_mapping_root(tmp_path, content):
    path = tmp_path / "config.yml"
    path.write_text(content)

    with pytest.raises(ConfigError, match="root must be a mapping"):
        load(str(path))


def test_toggle_state_save_raises_on_write_failure(tmp_path):
    from unittest.mock import patch

    from canvas.toggle_state import ToggleStateError
    from canvas.toggle_state import save as ts_save

    path = str(tmp_path / "toggle.json")
    with (
        patch("canvas.toggle_state.os.replace", side_effect=OSError("disk full")),
        pytest.raises(ToggleStateError, match="could not persist"),
    ):
        ts_save({}, path=path)


# --- canvas.spawn validation ------------------------------------------------


def _cfg(**canvas):
    return {"idle_timeout": 0.5, "canvas": {"preserve_geometry": True, **canvas}}


def test_validate_accepts_minimal_spawn():
    problems = validate(_cfg(auto_float=True, spawn={"center": True, "default": "30%x40%"}))
    assert not any("canvas" in p for p in problems)


def test_validate_rejects_non_bool_auto_float():
    problems = validate(_cfg(auto_float="yes"))
    assert any("auto_float" in p for p in problems)


def test_validate_rejects_non_mapping_spawn():
    problems = validate(_cfg(spawn="30%x40%"))
    assert any("canvas.spawn section must be a mapping" in p for p in problems)


def test_validate_rejects_bad_default_size():
    problems = validate(_cfg(spawn={"center": True, "default": "big"}))
    assert any("canvas.spawn.default" in p for p in problems)


def test_bundled_config_template_is_valid():
    """config.yml ships as a copy-paste template, so it must pass validation."""
    cfg = load("/nonexistent/path/config.yml", skip_user=True)
    assert validate(cfg) == []
    # The exact key set, so a silently added setting is caught here.
    assert cfg["canvas"]["spawn"] == {"center": True, "default": "50%x60%"}


def test_validate_rejects_bad_edge_scroll_values():
    problems = validate({"edge_scroll": {"speed": 0, "max_speed": -1, "enabled": "yes"}})

    assert any("edge_scroll.speed" in p for p in problems)
    assert any("edge_scroll.max_speed" in p for p in problems)
    assert any("edge_scroll.enabled" in p for p in problems)


def test_validate_rejects_non_bool_preserve_geometry_and_spawn_center():
    problems = validate(
        _cfg(preserve_geometry="yes", spawn={"center": "yes", "default": "50%x60%"})
    )

    assert any("preserve_geometry" in p for p in problems)
    assert any("canvas.spawn.center" in p for p in problems)


def test_load_treats_an_empty_file_as_defaults(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("")

    cfg = load(str(path), skip_user=True)

    assert cfg["canvas"]["spawn"]["default"] == "50%x60%"


def test_load_without_any_candidate_uses_the_built_ins():
    """No explicit path, no user file, no bundled template: the defaults."""
    with patch("canvas.config._candidates", return_value=[]):
        cfg = load(skip_user=True)

    assert cfg["canvas"]["spawn"]["default"] == "50%x60%"


def test_validate_rejects_a_negative_idle_timeout():
    problems = validate({"idle_timeout": -1})

    assert any("idle_timeout" in p for p in problems)


def test_validate_rejects_a_zero_idle_timeout():
    """Zero would disarm on the first check, so it is a mistake, not an opt-out."""
    problems = validate({"idle_timeout": 0})

    assert any("idle_timeout" in p for p in problems)


def test_validate_rejects_a_bad_pan_step():
    for bad in (0, -5, "big", True):
        problems = validate({"pan_step": bad})
        assert any("pan_step" in p for p in problems), bad


def test_load_rejects_the_removed_pan_keys():
    """End to end, not only through `validate`: the daemon has to refuse to
    start on a config that still names them."""
    with pytest.raises(ConfigError, match="speed was removed"):
        _load_yaml("speed: 1.6\n")

    with pytest.raises(ConfigError, match="max_speed was removed"):
        _load_yaml("max_speed: 50\n")
