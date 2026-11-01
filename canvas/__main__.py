"""Canvas — Hyprland infinite desktop.

Entry points (see pyproject.toml [project.scripts]):
    canvasd          Start the panning daemon
    canvas-ctl CMD   Send command to daemon
"""

import argparse
import logging
import sys

from canvas import version

_COMMANDS: tuple[tuple[str, str], ...] = (
    ("ping", "check if daemon is running"),
    ("status", "show daemon state (--json for one object)"),
    ("pan-start", "start panning (called by mouse bind)"),
    ("pan-stop", "stop panning (called by mouse release bind)"),
    ("nav-left", "navigate to nearest window left"),
    ("nav-right", "navigate to nearest window right"),
    ("nav-up", "navigate to nearest window up"),
    ("nav-down", "navigate to nearest window down"),
    ("center-cursor", "center canvas on floating window under cursor"),
    ("canvas-toggle", "toggle floating on current workspace (alias for -all)"),
    ("canvas-toggle-all", "toggle all windows on workspace (explicit)"),
    ("canvas-toggle-single", "toggle focused window only"),
    ("canvas-reset", "forget this workspace's saved canvas layout"),
    ("toggle", "invert pan direction"),
    ("edge-start", "start edge-scroll (called by mouse bind)"),
    ("edge-stop", "stop edge-scroll (called by mouse release bind)"),
    ("reload", "re-read the config file and apply it without restarting"),
)


def daemon_main(argv: list[str] | None = None) -> None:
    """Entry point for `canvasd`."""
    parser = argparse.ArgumentParser(
        prog="canvasd",
        description="Hyprland infinite-canvas panning daemon.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    parser.parse_args(argv)
    from canvas.config import ConfigError
    from canvas.daemon import run

    # `run()` sets logging up before either of these can be raised, so the
    # message goes through the same formatter as everything else — including
    # the JSON one, which a `print` would break.
    log = logging.getLogger("canvas")
    try:
        run()
    except ConfigError as exc:
        log.error("invalid configuration:\n%s", exc)
        sys.exit(1)
    except RuntimeError as exc:
        # An unusable runtime directory, for instance one another local user
        # owns. Refusing to start beats trusting it.
        log.error("%s", exc)
        sys.exit(1)


def _send(cmd: str, as_json: bool = False) -> None:
    from canvas.ipc import send_command

    response = send_command(f"{cmd}_JSON" if as_json else cmd)
    # UNKNOWN means the daemon is too old to know the command. It is a failure
    # like any other: exiting 0 there would tell a script that a newer CLI's
    # command had been accepted by an older daemon that never ran it.
    failed = not response or response.startswith(("ERROR", "UNKNOWN"))
    if failed:
        print(response or "ERROR: empty response from daemon", file=sys.stderr)
        sys.exit(1)
    print(response)


def ctl_main(argv: list[str] | None = None) -> None:
    """Entry point for `canvas-ctl`."""
    parser = argparse.ArgumentParser(
        prog="canvas-ctl",
        description="Send commands to the canvas daemon.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for name, help_text in _COMMANDS:
        child = sub.add_parser(name, help=help_text)
        child.set_defaults(func=_send, cmd=name.upper().replace("-", "_"))
        if name == "status":
            child.add_argument("--json", action="store_true", help="one JSON object")
    args = parser.parse_args(argv)
    args.func(args.cmd, as_json=getattr(args, "json", False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        daemon_main(sys.argv[2:])
    else:
        ctl_main()
