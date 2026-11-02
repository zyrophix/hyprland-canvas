"""Tests for `run()`, the daemon main loop.

This is the one place in the project where a test has to stand in for the whole
process: two threads, a signal handler and an endless loop. Everything it talks
to is replaced, and the loop is stopped by an event that reports "set" after a
fixed number of checks, so `run()` returns instead of hanging.

Kept apart from `test_daemon*.py`, which are about `DaemonState` and the IPC
handlers, not about this function.
"""

import copy
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from canvas.config import DEFAULT_CONFIG
from canvas.daemon import DaemonState, run


class _StopAfter:
    """An event that lets the loop run `iterations` passes and then stops it."""

    def __init__(self, iterations: int) -> None:
        self.iterations = iterations
        self.calls = 0
        self._set = False

    def is_set(self) -> bool:
        self.calls += 1
        if self.calls > self.iterations:
            self._set = True
        return self._set

    def set(self) -> None:
        self._set = True


class _FakeThread:
    """Stands in for threading.Thread: nothing runs, liveness is scripted."""

    def __init__(self, alive: bool = True) -> None:
        self.target = None
        self.args = ()
        self.started = False
        self.joined = False
        self.alive = alive

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return self.alive


class _FakePanning:
    """Scripted pan state: one entry of each list per loop pass."""

    def __init__(self, active: list[bool], deltas: list[tuple[int, int]]) -> None:
        self._active = iter(active)
        self._deltas = iter(deltas)
        self.poller_alive = True

    @property
    def pan_active(self) -> bool:
        return next(self._active, False)

    def get_total_delta(self) -> tuple[int, int]:
        return next(self._deltas, (0, 0))

    def check_idle_timeout(self) -> bool:
        return False


class _FakeEdgeScroll:
    def __init__(self, active: list[bool], deltas: list[tuple[int, int]]) -> None:
        self._active = iter(active)
        self._deltas = iter(deltas)

    @property
    def active(self) -> bool:
        return next(self._active, False)

    def consume_delta(self) -> tuple[int, int]:
        return next(self._deltas, (0, 0))

    def check_idle_timeout(self) -> bool:
        return False


def _drive(
    panning: object,
    edge_scroll: object,
    *,
    iterations: int = 1,
    ipc_alive: bool = True,
    idle_stop_raises: bool = False,
    tracing: bool = False,
    move_raises: bool = False,
) -> SimpleNamespace:
    """Run `run()` for `iterations` passes and hand back what it touched."""
    stop = _StopAfter(iterations)
    ipc_thread = _FakeThread(alive=ipc_alive)
    cursor_thread = _FakeThread()
    threads = iter([ipc_thread, cursor_thread])
    ipc_server = MagicMock()
    navigator = MagicMock()

    def make_thread(target=None, args=(), daemon=None):
        thread = next(threads)
        thread.target, thread.args = target, args
        return thread

    if idle_stop_raises:
        idle_stop = MagicMock(side_effect=KeyboardInterrupt)
    else:
        idle_stop = MagicMock(return_value=False)

    # ExitStack, not a parenthesised `with`: twenty patches in one statement
    # trips CPython's statically-nested-block limit.
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("canvas.daemon.threading.Event", return_value=stop))
        enter(patch("canvas.daemon.threading.Thread", side_effect=make_thread))
        set_signal = enter(patch("canvas.daemon.signal.signal"))
        source = "/etc/canvas/config.yml"
        enter(patch("canvas.daemon.resolve_path", return_value=source))
        load = enter(patch("canvas.daemon.load", return_value=copy.deepcopy(DEFAULT_CONFIG)))
        enter(patch("canvas.daemon.HyprIPC.from_env", return_value=MagicMock()))
        enter(patch("canvas.daemon.PanningState", return_value=panning))
        enter(patch("canvas.daemon.EdgeScrollState", return_value=edge_scroll))
        enter(patch("canvas.daemon.Navigator", return_value=navigator))
        enter(patch("canvas.daemon.IpcServer", return_value=ipc_server))
        enter(patch("canvas.daemon.acquire_singleton"))
        enter(patch("canvas.daemon.time.sleep"))
        enter(patch("canvas.daemon.debug.enable_from_env"))
        basic_config = enter(patch("canvas.daemon.logging.basicConfig"))
        root_handler = enter(patch("canvas.daemon.debug.root_handler", return_value=MagicMock()))
        enter(patch("canvas.daemon.debug.configure"))
        enter(patch("canvas.daemon.debug.enabled", return_value=tracing))
        dbg = enter(patch("canvas.daemon.debug.dbg"))
        enter(patch.object(DaemonState, "handle_idle_pan_stop", new=idle_stop))
        enter(patch.object(DaemonState, "rearm_spawn_rules_if_due", return_value=False))
        move = enter(
            patch.object(
                DaemonState,
                "move_windows_to_delta",
                side_effect=(RuntimeError("boom") if move_raises else None),
            )
        )
        edge_move = enter(patch.object(DaemonState, "edge_scroll_move"))
        restore = enter(patch.object(DaemonState, "restore_baselines"))
        apply_config = enter(patch.object(DaemonState, "apply_config"))

        handle = SimpleNamespace(
            move=move,
            edge_move=edge_move,
            restore=restore,
            dbg=dbg,
            load=load,
            apply_config=apply_config,
            basic_config=basic_config,
            root_handler=root_handler,
            source=source,
            stop=stop,
            ipc_server=ipc_server,
            ipc_thread=ipc_thread,
            cursor_thread=cursor_thread,
            navigator=navigator,
            exit=None,
        )
        try:
            run()
        except SystemExit as exc:  # the two give-up paths
            handle.exit = exc
        handle.signal_handler = set_signal.call_args.args[1]

    return handle


def test_run_moves_windows_when_the_pan_delta_changes():
    handle = _drive(_FakePanning([True], [(5, 6)]), _FakeEdgeScroll([False], []))

    handle.move.assert_called_once_with(5, 6)
    assert handle.exit is None


def test_run_moves_once_while_the_delta_is_unchanged():
    """The delta is absolute, so re-sending the same one every frame is waste."""
    handle = _drive(
        _FakePanning([True, True, True], [(5, 6), (5, 6), (5, 6)]),
        _FakeEdgeScroll([False, False, False], []),
        iterations=3,
    )

    handle.move.assert_called_once_with(5, 6)


def test_run_resets_the_delta_after_the_pan_stops():
    """Otherwise the next pan's first frame looks unchanged and is skipped."""
    handle = _drive(
        # One delta per pass with the pan active: the middle pass reads none.
        _FakePanning([True, False, True], [(5, 6), (5, 6)]),
        _FakeEdgeScroll([False, False, False], []),
        iterations=3,
    )

    assert handle.move.call_count == 2
    assert [c.args for c in handle.move.call_args_list] == [(5, 6), (5, 6)]


def test_run_does_not_move_on_a_zero_delta():
    handle = _drive(_FakePanning([True], [(0, 0)]), _FakeEdgeScroll([False], []))

    handle.move.assert_not_called()


def test_run_moves_the_edge_scroll_delta():
    handle = _drive(
        _FakePanning([False, False], []),
        _FakeEdgeScroll([True, True], [(3, 4), (0, 0)]),
        iterations=2,
    )

    handle.edge_move.assert_called_once_with(3, 4)


def test_run_stops_the_server_and_restores_state_on_the_way_out():
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []))

    handle.ipc_server.stop.assert_called_once()
    handle.restore.assert_called_once()
    handle.navigator.drop_spawn_rules.assert_called_once()
    assert handle.ipc_thread.joined is True
    assert handle.cursor_thread.joined is True
    assert handle.stop.is_set() is True


def test_run_exits_non_zero_when_the_poller_dies():
    panning = _FakePanning([False], [])
    panning.poller_alive = False

    handle = _drive(panning, _FakeEdgeScroll([False], []))

    assert handle.exit is not None
    assert handle.exit.code == 1
    # The give-up path still cleans up.
    handle.restore.assert_called_once()


def test_run_exits_non_zero_when_the_ipc_thread_dies():
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []), ipc_alive=False)

    assert handle.exit is not None
    assert handle.exit.code == 1
    handle.restore.assert_called_once()


def test_run_shuts_down_cleanly_on_keyboard_interrupt():
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []), idle_stop_raises=True)

    assert handle.exit is None
    handle.restore.assert_called_once()


def test_run_traces_boot_when_tracing_is_on():
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []), tracing=True)

    events = [c.args[0] for c in handle.dbg.call_args_list]
    assert "BOOT" in events


def test_run_stops_when_sigterm_arrives():
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []))

    handle.signal_handler(15, None)

    assert handle.stop.is_set() is True


def test_run_keeps_going_when_a_move_fails(caplog):
    """A rejected move is one bad frame, not a reason to drop the session."""
    import logging

    with caplog.at_level(logging.WARNING, logger="canvas"):
        handle = _drive(
            _FakePanning([True], [(5, 6)]), _FakeEdgeScroll([False], []), move_raises=True
        )

    assert handle.exit is None
    assert any("window move failed" in r.getMessage() for r in caplog.records)


def test_run_reads_and_records_the_resolved_config_file():
    """`status` names the file the config came from, which means the boot path
    has to hand the resolved path to both `load` and `apply_config`."""
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []))

    handle.load.assert_called_once_with(handle.source)
    assert handle.apply_config.call_args.args[1] == handle.source


def test_run_gives_the_root_logger_the_handler_debug_builds():
    """`run()` has to pass the handler itself, not a format string: the JSON
    formatter is the handler's, and a format string cannot express it."""
    handle = _drive(_FakePanning([False], []), _FakeEdgeScroll([False], []))

    handlers = handle.basic_config.call_args.kwargs["handlers"]
    assert handlers == [handle.root_handler.return_value]
    assert handle.basic_config.call_args.kwargs["force"] is True


def test_run_applies_a_delta_that_comes_back_to_zero():
    """The total is absolute, so zero is the baseline — a real position.
    Skipping it left the canvas short of the pointer until the next move."""
    handle = _drive(
        _FakePanning([True, True, True], [(100, 50), (0, 0)]),
        _FakeEdgeScroll([False, False, False], []),
        iterations=3,
    )

    assert [c.args for c in handle.move.call_args_list] == [(100, 50), (0, 0)]
