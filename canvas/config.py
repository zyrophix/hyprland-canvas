"""Load YAML configuration with sensible defaults."""

import copy
import math
import os
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from canvas.spawnrules import size_spec_error


class ConfigError(Exception):
    """Raised when the configuration contains invalid values."""


DEFAULT_CONFIG: dict[str, Any] = {
    "speed": 1.6,
    "max_speed": None,
    # Seconds without gesture input before pan or edge-scroll ends on its own.
    "idle_timeout": 0.5,
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
    "window_pan_excludes": [],
    "edge_scroll": {
        "enabled": True,
        "ramp_distance": 50,
        "speed": 20.0,
        "max_speed": None,
        "grab_dead_zone": 5,
    },
    "canvas": {
        "preserve_geometry": True,
        "auto_float": True,
        "spawn": {
            "center": True,
            "default": "50%x60%",
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


# Settings a previous version read and this one does not. Without a message of
# its own the generic one below fires, which is technically true and practically
# useless: the reader needs to know what to do with the key, not that it is
# unknown.
_REMOVED_KEYS: dict[str, str] = {
    "canvas.spawn.rules": (
        "canvas.spawn.rules was removed in 1.7.0 — per-class spawn sizing belongs "
        'in your Hyprland config as a windowrule now; see README, "Sizing windows '
        'that open during canvas". Delete this key.'
    ),
}


def _unknown_keys(cfg: Any, shape: Any, prefix: str = "") -> list[str]:
    """Dotted names of keys `cfg` has and `shape` (the defaults) does not.

    The defaults are the schema: `apply_config` reads nothing that is not in
    them. `_deep_merge` copies every user key through unchecked, so a typo or a
    setting from an older version reaches the daemon looking exactly like one it
    understood, and the edit appears to do nothing.

    Only key presence is compared, never types: `max_speed` defaults to None
    while a valid value is a number, so a type test here would reject it.
    """
    if not isinstance(cfg, dict) or not isinstance(shape, dict):
        return []
    out: list[str] = []
    for key, value in cfg.items():
        name = f"{prefix}{key}"
        if key not in shape:
            out.append(name)
            continue
        out.extend(_unknown_keys(value, shape[key], f"{name}."))
    return out


def validate(cfg: dict[str, Any]) -> list[str]:
    """Validate configuration values. Returns a list of human-readable problems.

    An empty list means the config is safe to run with.
    """
    errors: list[str] = []
    errors.extend(
        _REMOVED_KEYS.get(name, f"{name} is not a setting this version reads")
        for name in _unknown_keys(cfg, DEFAULT_CONFIG)
    )

    speed = cfg.get("speed")
    if not _is_num(speed) or speed <= 0:
        errors.append(f"speed must be a number > 0, got {speed!r}")

    max_speed = cfg.get("max_speed")
    if max_speed is not None and (not _is_num(max_speed) or max_speed <= 0):
        errors.append(f"max_speed must be a number > 0 or null, got {max_speed!r}")

    idle_timeout = cfg.get("idle_timeout")
    if not _is_num(idle_timeout) or idle_timeout <= 0:
        errors.append(f"idle_timeout must be a number > 0, got {idle_timeout!r}")

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
        elif any(not a.strip() for a in apps):
            # Matched as a substring against the window class, so an empty entry
            # matches every window and silently disables navigation.
            errors.append("navigation.protected_apps entries must be non-empty")

    invert = cfg.get("invert")
    if not isinstance(invert, dict):
        errors.append("invert section must be a mapping")
    elif not isinstance(invert.get("enabled"), bool):
        errors.append(f"invert.enabled must be a boolean, got {invert.get('enabled')!r}")

    excludes = cfg.get("window_pan_excludes")
    if not isinstance(excludes, list) or not all(isinstance(a, str) for a in excludes):
        errors.append("window_pan_excludes must be a list of strings")
    elif any(not a.strip() for a in excludes):
        errors.append("window_pan_excludes entries must be non-empty")

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

    return errors


def _candidates(path: str | None = None, skip_user: bool = False) -> list[str]:
    """Config search order: explicit path, user XDG, bundled project file."""
    candidates: list[str] = []

    if path:
        candidates.append(path)

    if not skip_user:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        candidates.append(os.path.join(xdg, "canvas", "config.yml"))

    candidates.append(str(Path(__file__).resolve().parent.parent / "config.yml"))

    return candidates


def resolve_path() -> str | None:
    """Path `load` would read, or None when only built-in defaults apply.

    Lets `canvas-ctl reload` name the file it picked, so a user editing the
    wrong copy is not left guessing why nothing changed.
    """
    for candidate in _candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def load(path: str | None = None, skip_user: bool = False) -> dict[str, Any]:
    """Load config from YAML file, merging with defaults.

    Search order:
        1. Explicit path
        2. ~/.config/canvas/config.yml (unless skip_user=True)
        3. <project_dir>/config.yml (bundled default)
        4. Hardcoded DEFAULT_CONFIG

    Every failure names the file, because a reload that reports "invalid
    config" without saying which file is invalid is not actionable.
    """
    for candidate in _candidates(path, skip_user):
        if os.path.isfile(candidate):
            with open(candidate) as f:
                try:
                    user_cfg = yaml.safe_load(f)
                except yaml.YAMLError as e:
                    raise ConfigError(f"{candidate}: invalid YAML: {e}") from e
            if user_cfg is None:
                user_cfg = {}
            if not isinstance(user_cfg, dict):
                raise ConfigError(f"{candidate}: config root must be a mapping")
            cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
            problems = validate(cfg)
            if problems:
                raise ConfigError("\n".join(f"{candidate}: {p}" for p in problems))
            return cfg

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    problems = validate(cfg)
    if problems:
        raise ConfigError("\n".join(problems))
    return cfg
