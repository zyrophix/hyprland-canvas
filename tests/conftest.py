"""Shared test setup.

Nothing here is a helper for one module; it exists so the suite cannot touch the
session it is running in.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path, monkeypatch):
    """Point the default runtime directory at a per-test temporary one.

    `toggle_state.default_path()` otherwise resolves to the real
    `/run/user/<uid>/canvas/toggle-state.json` — the canvas state of a daemon the
    user may be running right now. Three tests reached `toggle_state.save`
    without patching it and overwrote that file, so a test run wiped whatever
    canvases were open. Redirecting the directory rather than the one call site
    means the next such test cannot reintroduce it.

    The socket path is redirected for the same reason: nothing should be able to
    bind or connect to the live control socket by forgetting an argument.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr("canvas.toggle_state.runtime_dir", lambda: str(runtime))
    monkeypatch.setattr("canvas.ipc.runtime_dir", lambda: str(runtime))
