"""Canvas — Hyprland infinite desktop.

Entry points (see pyproject.toml [project.scripts]):
    canvasd          Start the panning daemon
    canvas-ctl CMD   Send command to daemon
"""

import argparse
import sys


def _version() -> str:
    """Single source of truth: installed metadata, fallback to package attr."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("hyprland-canvas")
    except PackageNotFoundError:
        from canvas import __version__

        return __version__


_COMMANDS: tuple[tuple[str, str], ...] = (
    ("ping", "check if daemon is running"),
    ("status", "show pan direction and state"),
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.parse_args(argv)
    from canvas.config import ConfigError
    from canvas.daemon import run

    try:
        run()
    except ConfigError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        sys.exit(1)


def _send(cmd: str) -> None:
    from canvas.ipc import send_command

    response = send_command(cmd)
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for name, help_text in _COMMANDS:
        sub.add_parser(name, help=help_text).set_defaults(
            func=_send, cmd=name.upper().replace("-", "_")
        )
    args = parser.parse_args(argv)
    args.func(args.cmd)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        daemon_main(sys.argv[2:])
    else:
        ctl_main()
