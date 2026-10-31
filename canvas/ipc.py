"""Unix domain socket IPC for canvas daemon.

Server runs in daemon, listens for commands.
Client (canvas-ctl) sends a command, gets a response, exits.

Security:
- SO_PEERCRED: rejects connections from processes with different UID
- Symlink check: refuses to bind if socket path is a symlink
- Singleton: refuses to start if another canvasd owns the socket
"""

import fcntl
import logging
import os
import socket
import stat
import struct
import threading
from collections.abc import Callable

log = logging.getLogger("canvas.ipc")

_MY_UID = os.getuid()

_CLIENT_TIMEOUT = 2.0
_SERVER_RECV_TIMEOUT = 1.0  # must stay well below _CLIENT_TIMEOUT so queued
# clients are still served within their own budget
_MAX_CONTROL_RESPONSE = 64 * 1024


def _ensure_private_dir(path: str) -> None:
    """Create `path` with mode 0700, then confirm it is a directory we own.

    lstat rather than stat: a symlink dropped in place of the directory has to
    fail the directory test, not be followed into somebody else's tree.
    """
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"could not create runtime directory {path}: {e}") from e

    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"runtime directory {path} is not a directory; refusing to use it")
    if st.st_uid != _MY_UID:
        raise RuntimeError(
            f"runtime directory {path} is owned by uid {st.st_uid}, not {_MY_UID}; "
            "refusing to use it"
        )


def runtime_dir() -> str:
    """Directory holding the control socket and the toggle state.

    `$XDG_RUNTIME_DIR` and `/run/user/<uid>` are created and owned by the
    session manager, so they are taken as they are. `/tmp/user/<uid>` is the
    fallback for a session that has neither, and it is the one path another
    local user can create first: /tmp is world-writable and the name is just
    our uid. So that one is created 0700, and an existing one is used only when
    it is a real directory we own — the alternative is reading window addresses,
    and binding a socket, out of a directory somebody else controls.

    Raises RuntimeError when the fallback cannot be trusted, because every
    caller is about to trust what is inside it.
    """
    run_dir = f"/run/user/{_MY_UID}"
    if os.path.isdir(run_dir):
        return run_dir
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return xdg
    fallback = f"/tmp/user/{_MY_UID}"
    _ensure_private_dir(fallback)
    return fallback


def _default_socket_path() -> str:
    return os.path.join(runtime_dir(), "canvas.sock")


def _get_peer_uid(conn: socket.socket) -> int | None:
    """Get the UID of the process on the other end of a Unix socket.

    Uses SO_PEERCRED (Linux). Returns None on unsupported platforms.
    """
    try:
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iii"))
        _, uid, _ = struct.unpack("iii", cred)
        return int(uid)
    except (AttributeError, OSError):
        return None


def _daemon_alive(sock_path: str) -> bool:
    """True if a process is accepting connections on sock_path."""
    if not os.path.exists(sock_path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


_held_lock = None


def acquire_singleton(sock_path: str | None = None) -> None:
    """Ensure only one canvasd runs for this socket path.

    Raises SystemExit if another daemon is alive (live socket or held lock).
    On success, keeps an exclusive flock on "<sock_path>.lock" for the
    process lifetime, which closes the check-then-bind race.
    """
    global _held_lock
    path = sock_path or _default_socket_path()

    if _daemon_alive(path):
        raise SystemExit(f"canvasd already running on {path}, exiting")

    # Lock fd intentionally stays open for the whole process lifetime.
    lock = open(path + ".lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"canvasd already running (lock {path}.lock), exiting") from None
    _held_lock = lock


class IpcServer:
    """Threaded Unix domain socket server.

    Args:
        sock_path: Path to the socket file.
        handler: Callable that receives a command string and returns a response string.
    """

    def __init__(self, sock_path: str | None = None, handler: Callable[[str], str] | None = None):
        self.sock_path = sock_path or _default_socket_path()
        self._handler = handler or (lambda cmd: "OK")
        self._stop_event = threading.Event()
        self._server_socket: socket.socket | None = None

    def serve(self) -> None:
        """Start listening. Blocks until stop() is called."""
        if os.path.islink(self.sock_path):
            log.error("socket path is a symlink, refusing to bind: %s", self.sock_path)
            return

        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(self.sock_path)
        self._server_socket.listen(5)
        self._server_socket.settimeout(0.5)

        while not self._stop_event.is_set():
            try:
                conn, _ = self._server_socket.accept()
            except TimeoutError:
                continue

            peer_uid = _get_peer_uid(conn)
            if peer_uid is not None and peer_uid != _MY_UID:
                log.warning("rejected IPC connection from uid %d", peer_uid)
                conn.close()
                continue

            try:
                conn.settimeout(_SERVER_RECV_TIMEOUT)
                data = conn.recv(1024)
                if data:
                    cmd = data.decode("utf-8").strip()
                    response = self._handler(cmd)
                    conn.sendall(response.encode("utf-8"))
            except TimeoutError:
                log.warning("IPC client timed out without sending a command")
            except Exception as e:
                log.debug("IPC client handler error: %s", e)
            finally:
                conn.close()

        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

    def stop(self) -> None:
        """Signal the server to stop."""
        self._stop_event.set()


def send_command(cmd: str, sock_path: str | None = None) -> str:
    """Send a command to the canvas daemon and return the response.

    Returns a string starting with "ERROR:" on any failure.
    """
    try:
        path = sock_path or _default_socket_path()
    except RuntimeError as e:
        # The client contract is a string starting with ERROR: on any failure,
        # and an unusable runtime directory is one.
        return f"ERROR: {e}"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_CLIENT_TIMEOUT)
        sock.connect(path)
        sock.sendall(cmd.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)

        response = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
            if len(response) > _MAX_CONTROL_RESPONSE:
                return "ERROR: daemon response too large"

        return response.decode("utf-8")
    except ConnectionRefusedError:
        return "ERROR: daemon not running"
    except FileNotFoundError:
        return "ERROR: socket not found — is canvasd running?"
    except TimeoutError:
        return "ERROR: daemon did not respond in time"
    except OSError as e:
        return f"ERROR: {e}"
    finally:
        sock.close()
