"""Canvas — an infinite desktop for Hyprland."""

import importlib.metadata

__version__ = "1.7.0"


def version() -> str:
    """The installed package version, or the attribute above.

    The fallback is for a source tree that was never installed, where there is no
    distribution metadata to read: `canvasd --version` would otherwise print
    nothing at all.
    """
    try:
        return importlib.metadata.version("hyprland-canvas")
    except importlib.metadata.PackageNotFoundError:
        return __version__
