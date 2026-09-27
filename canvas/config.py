"""Load YAML configuration with sensible defaults."""

import copy
import math
import os
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from canvas.spawnrules import MATCH_KEYS, size_spec_error


class ConfigError(Exception):
    """Raised when the configuration contains invalid values."""


DEFAULT_CONFIG: dict[str, Any] = {
    "speed": 1.6,
    "max_speed": None,
    "navigation": {
        "cooldown": 0.2,
        "protected_apps": [
            "brave-browser",
            "chromium",
            "chromium-browser",
            "google-chrome",
            "firefox",
            "firefoxdeveloperedition",
            "librewolf",
            "vivaldi",
            "opera",
            "microsoft-edge",
        ],
    },
    "invert": {
        "enabled": True,
    },
    "edge_scroll": {
        "enabled": True,
        "ramp_distance": 50,
        "speed": 20.0,
        "max_speed": None,
        "grab_dead_zone": 5,
    },
    "canvas": {
        "preserve_geometry": True,
        "auto_float": False,
        "spawn": {
            "center": True,
            "default": "30%x40%",
            "rules": [],
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflicts."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_num(value: object) -> TypeGuard[int | float]:
    """True for finite int/float, excluding bool."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def validate(cfg: dict[str, Any]) -> list[str]:
    """Validate configuration values. Returns a list of human-readable problems.

    An empty list means the config is safe to run with.
    """
    errors: list[str] = []

    speed = cfg.get("speed")
    if not _is_num(speed) or speed <= 0:
        errors.append(f"speed must be a number > 0, got {speed!r}")

    max_speed = cfg.get("max_speed")
    if max_speed is not None and (not _is_num(max_speed) or max_speed <= 0):
        errors.append(f"max_speed must be a number > 0 or null, got {max_speed!r}")

    nav = cfg.get("navigation")
    if not isinstance(nav, dict):
        errors.append("navigation section must be a mapping")
    else:
        cooldown = nav.get("cooldown")
        if not _is_num(cooldown) or cooldown < 0:
            errors.append(f"navigation.cooldown must be a number >= 0, got {cooldown!r}")
        apps = nav.get("protected_apps")
        if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
            errors.append("navigation.protected_apps must be a list of strings")

    invert = cfg.get("invert")
    if not isinstance(invert, dict):
        errors.append("invert section must be a mapping")
    elif not isinstance(invert.get("enabled"), bool):
        errors.append(f"invert.enabled must be a boolean, got {invert.get('enabled')!r}")

    es = cfg.get("edge_scroll")
    if not isinstance(es, dict):
        errors.append("edge_scroll section must be a mapping")
    else:
        rd = es.get("ramp_distance")
        if not isinstance(rd, int) or isinstance(rd, bool) or rd <= 0:
            errors.append(f"edge_scroll.ramp_distance must be an integer > 0, got {rd!r}")
        es_speed = es.get("speed")
        if not _is_num(es_speed) or es_speed <= 0:
            errors.append(f"edge_scroll.speed must be a number > 0, got {es_speed!r}")
        es_max = es.get("max_speed")
        if es_max is not None and (not _is_num(es_max) or es_max <= 0):
            errors.append(f"edge_scroll.max_speed must be a number > 0 or null, got {es_max!r}")
        if not isinstance(es.get("enabled"), bool):
            errors.append(f"edge_scroll.enabled must be a boolean, got {es.get('enabled')!r}")
        gdz = es.get("grab_dead_zone")
        if not isinstance(gdz, int) or isinstance(gdz, bool) or gdz <= 0:
            errors.append(f"edge_scroll.grab_dead_zone must be an integer > 0, got {gdz!r}")

    canvas_cfg = cfg.get("canvas")
    if not isinstance(canvas_cfg, dict):
        errors.append("canvas section must be a mapping")
        return errors

    if not isinstance(canvas_cfg.get("preserve_geometry"), bool):
        errors.append(
            "canvas.preserve_geometry must be a boolean, "
            f"got {canvas_cfg.get('preserve_geometry')!r}"
        )
    if not isinstance(canvas_cfg.get("auto_float"), bool):
        errors.append(f"canvas.auto_float must be a boolean, got {canvas_cfg.get('auto_float')!r}")
    errors.extend(_validate_spawn(canvas_cfg.get("spawn")))

    return errors


def _validate_spawn(spawn: object) -> list[str]:
    """Validate the canvas.spawn section.

    Sizes are checked against the workarea at registration time; here we only
    verify the shape so a typo fails at startup instead of on first toggle.
    """
    if not isinstance(spawn, dict):
        return ["canvas.spawn section must be a mapping"]

    errors: list[str] = []
    if not isinstance(spawn.get("center"), bool):
        errors.append(f"canvas.spawn.center must be a boolean, got {spawn.get('center')!r}")

    default = spawn.get("default")
    problem = size_spec_error(default)
    if problem:
        errors.append(f"canvas.spawn.default {problem}")

    rules = spawn.get("rules")
    if not isinstance(rules, list):
        errors.append(f"canvas.spawn.rules must be a list, got {rules!r}")
        return errors

    for index, entry in enumerate(rules):
        if not isinstance(entry, dict):
            errors.append(f"canvas.spawn.rules[{index}] must be a mapping")
            continue
        match = entry.get("match")
        if not isinstance(match, dict) or not match:
            errors.append(f"canvas.spawn.rules[{index}].match must be a non-empty mapping")
        else:
            for key in match:
                if key not in MATCH_KEYS:
                    errors.append(
                        f"canvas.spawn.rules[{index}].match has unknown property {key!r}; "
                        f"expected one of {', '.join(sorted(MATCH_KEYS))}"
                    )
                elif not isinstance(match[key], (str, bool, int, float)):
                    errors.append(
                        f"canvas.spawn.rules[{index}].match.{key} must be a string, "
                        f"bool or number, got {match[key]!r}"
                    )
        spec_problem = size_spec_error(entry.get("size"))
        if spec_problem:
            errors.append(f"canvas.spawn.rules[{index}].size {spec_problem}")

    return errors


def load(path: str | None = None, skip_user: bool = False) -> dict[str, Any]:
    """Load config from YAML file, merging with defaults.

    Search order:
        1. Explicit path
        2. ~/.config/canvas/config.yml (unless skip_user=True)
        3. <project_dir>/config.yml (bundled default)
        4. Hardcoded DEFAULT_CONFIG
    """
    candidates: list[str] = []

    if path:
        candidates.append(path)

    if not skip_user:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        candidates.append(os.path.join(xdg, "canvas", "config.yml"))

    candidates.append(str(Path(__file__).resolve().parent.parent / "config.yml"))

    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                user_cfg = yaml.safe_load(f)
            if user_cfg is None:
                user_cfg = {}
            if not isinstance(user_cfg, dict):
                raise ConfigError("config root must be a mapping")
            cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
            problems = validate(cfg)
            if problems:
                raise ConfigError("\n".join(problems))
            return cfg

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    problems = validate(cfg)
    if problems:
        raise ConfigError("\n".join(problems))
    return cfg
