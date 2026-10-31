"""The suite must not touch the session it runs in.

This is not a test of the daemon. It guards `tests/conftest.py`, which redirects
the runtime directory: before it existed, three tests reached
`toggle_state.save` without patching it and overwrote
`/run/user/<uid>/canvas/toggle-state.json` — the canvas state of a daemon the
user may have running. A test run wiped their open canvases.
"""

import os

from canvas import toggle_state


def test_the_default_state_path_is_a_temporary_one(tmp_path):
    """The file a test would write to is under pytest's temporary directory."""
    assert str(tmp_path) in toggle_state.default_path()


def test_the_default_state_path_is_not_the_live_one(tmp_path):
    """And specifically not the one a running daemon owns."""
    live = f"/run/user/{os.getuid()}/canvas/toggle-state.json"

    assert toggle_state.default_path() != live
