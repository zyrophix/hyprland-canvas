import os
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from canvas.ipc import IpcServer, acquire_singleton, send_command


def test_ipc_ping_pong(tmp_path):
    """Server responds to PING with PONG."""
    sock_path = str(tmp_path / "test.sock")
    received = []
    server = IpcServer(
        sock_path,
        lambda cmd: received.append(cmd) or "PONG" if cmd == "PING" else "OK",
    )

    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    response = send_command("PING", sock_path=sock_path)
    assert response == "PONG"
    assert "PING" in received

    server.stop()
    server_thread.join(timeout=2)


def test_ipc_nav_command(tmp_path):
    """Server receives NAV_LEFT and returns OK."""
    sock_path = str(tmp_path / "test2.sock")
    received = []
    server = IpcServer(sock_path, lambda cmd: (received.append(cmd), "OK")[1])

    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    response = send_command("NAV_LEFT", sock_path=sock_path)
    assert response == "OK"
    assert received == ["NAV_LEFT"]

    server.stop()
    server_thread.join(timeout=2)


def test_ipc_toggle_returns_state(tmp_path):
    """Server receives TOGGLE and returns the new state."""
    sock_path = str(tmp_path / "test3.sock")
    inverted = [False]

    def handler(cmd):
        if cmd == "TOGGLE":
            inverted[0] = not inverted[0]
            return "INVERTED" if inverted[0] else "NORMAL"
        elif cmd == "STATUS":
            return "INVERTED" if inverted[0] else "NORMAL"
        return "OK"

    server = IpcServer(sock_path, handler)
    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    r1 = send_command("TOGGLE", sock_path=sock_path)
    assert r1 == "INVERTED"
    r2 = send_command("STATUS", sock_path=sock_path)
    assert r2 == "INVERTED"
    r3 = send_command("TOGGLE", sock_path=sock_path)
    assert r3 == "NORMAL"

    server.stop()
    server_thread.join(timeout=2)


# --- singleton guard ---


def test_acquire_singleton_stale_socket_ok(tmp_path):
    """A leftover socket file with no listener is not a running daemon."""
    sock_path = str(tmp_path / "stale.sock")
    open(sock_path, "w").close()  # stale file, nobody listening
    acquire_singleton(sock_path)  # should not raise


def test_acquire_singleton_rejects_live_daemon(tmp_path):
    sock_path = str(tmp_path / "live.sock")
    server = IpcServer(sock_path, lambda cmd: "PONG")
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.1)

    try:
        with pytest.raises(SystemExit, match="already running"):
            acquire_singleton(sock_path)
    finally:
        server.stop()
        thread.join(timeout=2)


def test_acquire_singleton_lock_conflict(tmp_path):
    """Holding the flock simulates a daemon that just bound between checks."""
    import fcntl

    sock_path = str(tmp_path / "x.sock")
    holder = open(sock_path + ".lock", "w")  # noqa: SIM115
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit, match="already running"):
            acquire_singleton(sock_path)
    finally:
        holder.close()


def test_acquire_singleton_released_after_process_death(tmp_path):
    """After the lock holder closes the fd, acquisition succeeds again."""
    sock_path = str(tmp_path / "y.sock")
    acquire_singleton(sock_path)
    from canvas.ipc import _held_lock

    assert _held_lock is not None
    _held_lock.close()
    # reset module state for other tests
    import canvas.ipc as ipc_mod

    ipc_mod._held_lock = None
    acquire_singleton(sock_path)  # should not raise


# --- timeouts ---


def test_server_survives_silent_client(tmp_path):
    """A connected client that never sends must not wedge the server forever."""
    sock_path = str(tmp_path / "wedge.sock")
    received = []
    server = IpcServer(sock_path, lambda cmd: (received.append(cmd), "OK")[1])
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.1)

    wedge = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    wedge.connect(sock_path)  # connects, sends nothing

    start = time.monotonic()
    response = send_command("PING", sock_path=sock_path)
    elapsed = time.monotonic() - start

    assert response == "OK"
    assert elapsed < 4.0  # unblocks after client timeout instead of hanging forever
    assert "PING" in received

    wedge.close()
    server.stop()
    thread.join(timeout=2)


def test_send_command_timeout_returns_error(tmp_path):
    """send_command reports an error instead of hanging on a dead peer."""
    # bind-and-listen but never accept/respond
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = str(tmp_path / "deaf.sock")
    srv.bind(path)
    srv.listen(1)
    try:
        result = send_command("PING", sock_path=path)
        assert result.startswith("ERROR")
    finally:
        srv.close()


def test_send_command_connection_reset_returns_error(tmp_path):
    """Handler exception must not hang or crash the client — empty reply comes back."""
    sock_path = str(tmp_path / "reset.sock")

    def hostile_handler(cmd):
        raise ConnectionResetError("boom")

    server = IpcServer(sock_path, hostile_handler)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.1)
    result = send_command("PING", sock_path=sock_path)
    server.stop()
    thread.join(timeout=2)
    assert result == ""


def test_serve_unlinks_a_leftover_socket_file(tmp_path):
    sock_path = tmp_path / "stale.sock"
    sock_path.write_text("not a socket")
    server = IpcServer(str(sock_path))
    server.stop()

    server.serve()

    assert not sock_path.exists()


def test_get_peer_uid_returns_none_when_unsupported():
    from canvas.ipc import _get_peer_uid

    conn = MagicMock()
    conn.getsockopt.side_effect = OSError("no SO_PEERCRED here")

    assert _get_peer_uid(conn) is None


def test_server_rejects_a_peer_from_another_uid(tmp_path):
    """SO_PEERCRED is what keeps another user off the control socket."""
    sock_path = str(tmp_path / "peer.sock")
    handled = []
    server = IpcServer(sock_path, lambda cmd: handled.append(cmd) or "OK")

    server_thread = threading.Thread(target=server.serve, daemon=True)
    with patch("canvas.ipc._get_peer_uid", return_value=os.getuid() + 1):
        server_thread.start()
        time.sleep(0.1)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        client.sendall(b"PING")
        try:
            answered = client.recv(64)
        except ConnectionResetError:
            # Closing with unread data resets the connection instead of EOF;
            # either way the point is that no reply arrives.
            answered = b""
        assert answered == b""
        client.close()

        server.stop()
        server_thread.join(timeout=2)

    assert handled == []


def test_send_command_reports_a_refused_connection():
    with patch("canvas.ipc.socket.socket") as make:
        make.return_value.connect.side_effect = ConnectionRefusedError
        assert send_command("PING", sock_path="/tmp/x") == "ERROR: daemon not running"


def test_send_command_reports_a_missing_socket_file():
    with patch("canvas.ipc.socket.socket") as make:
        make.return_value.connect.side_effect = FileNotFoundError
        assert send_command("PING", sock_path="/tmp/x").startswith("ERROR: socket not found")


def test_send_command_reports_an_oversized_response():
    with patch("canvas.ipc.socket.socket") as make:
        make.return_value.recv.return_value = b"x" * (70 * 1024)
        assert send_command("PING", sock_path="/tmp/x") == "ERROR: daemon response too large"


def test_send_command_reports_an_os_error():
    with patch("canvas.ipc.socket.socket") as make:
        make.return_value.connect.side_effect = OSError("boom")
        assert send_command("PING", sock_path="/tmp/x") == "ERROR: boom"
