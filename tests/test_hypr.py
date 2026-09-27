"""Tests for canvas.hypr module — Hyprland IPC abstraction."""

import os
from unittest.mock import MagicMock, patch

import pytest

from canvas.hypr import (
    HyprIPC,
    HyprIPCError,
    _hypr_socket_path,
    eval_lua,
    get_cursor_pos,
    send,
)


def _fake_socket(root: str, signature: str = "abc123") -> str:
    """Create a socket-shaped tree under root and return its path."""
    d = os.path.join(root, "hypr", signature)
    os.makedirs(d)
    sock = os.path.join(d, ".socket.sock")
    with open(sock, "w"):
        pass
    return sock


def _make_ipc_with_mock(mock_sock: MagicMock) -> HyprIPC:
    """Create HyprIPC with a mock _connect that returns mock_sock."""
    ipc = HyprIPC("/tmp/test.sock")
    ipc._connect = lambda: mock_sock  # type: ignore[assignment]
    return ipc


def test_send_calls_socket():
    """send() connects, sends command, reads response."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"ok", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    result = ipc.send("cursorpos")

    assert result == "ok"
    mock_sock.sendall.assert_called_once_with(b"cursorpos")


def test_eval_lua_prefixes_eval():
    """eval_lua prepends 'eval ' to lua code."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"ok", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    result = ipc.eval_lua("hl.dispatch(hl.dsp.no_op())")

    assert result == "ok"
    mock_sock.sendall.assert_called_once_with(b"eval hl.dispatch(hl.dsp.no_op())")


def test_get_cursor_pos_parses_response():
    """get_cursor_pos parses 'X, Y' response."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"100, 200", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    x, y = ipc.get_cursor_pos()

    assert x == 100
    assert y == 200


def test_send_closes_socket_after_response():
    """send() always closes the connection (server closes after each response)."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"ok", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    result = ipc.send("cursorpos")

    assert result == "ok"
    mock_sock.close.assert_called_once()


def test_send_response_size_limit():
    """An oversized response aborts instead of consuming unbounded memory."""
    import canvas.hypr as hypr_mod

    mock_sock = MagicMock()
    chunk = b"x" * 4096
    mock_sock.recv.side_effect = lambda *_: chunk  # never returns EOF
    ipc = _make_ipc_with_mock(mock_sock)

    original = hypr_mod._MAX_RESPONSE
    hypr_mod._MAX_RESPONSE = 8192
    try:
        try:
            ipc.send("j/clients")
            raise AssertionError("should have raised ConnectionError")
        except ConnectionError as e:
            assert "size limit" in str(e)
    finally:
        hypr_mod._MAX_RESPONSE = original


def test_module_level_send_delegates_to_default():
    """Module-level send() delegates to default HyprIPC instance."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"module_ok", b""]

    with patch("canvas.hypr._get_default") as mock_get:
        ipc = _make_ipc_with_mock(mock_sock)
        mock_get.return_value = ipc
        result = send("cursorpos")

    assert result == "module_ok"


def test_module_level_eval_lua_delegates():
    """Module-level eval_lua() delegates to default HyprIPC instance."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"lua_ok", b""]

    with patch("canvas.hypr._get_default") as mock_get:
        ipc = _make_ipc_with_mock(mock_sock)
        mock_get.return_value = ipc
        result = eval_lua("print('hi')")

    assert result == "lua_ok"


def test_module_level_get_cursor_pos_delegates():
    """Module-level get_cursor_pos() delegates to default HyprIPC instance."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"42, 84", b""]

    with patch("canvas.hypr._get_default") as mock_get:
        ipc = _make_ipc_with_mock(mock_sock)
        mock_get.return_value = ipc
        x, y = get_cursor_pos()

    assert x == 42
    assert y == 84


def test_send_empty_response():
    """send() handles empty response from Hyprland."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b""]
    ipc = _make_ipc_with_mock(mock_sock)

    result = ipc.send("cursorpos")

    assert result == ""


def test_send_raises_on_hyprland_error_response():
    """Hyprland's textual error response must not look like success."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"error: eval failed", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    with pytest.raises(HyprIPCError, match="eval failed"):
        ipc.send("eval broken")


def test_send_connection_error():
    """send() raises when both persistent and fresh connections fail."""
    ipc = HyprIPC("/tmp/test.sock")
    ipc._connect = MagicMock(side_effect=ConnectionError("refused"))  # type: ignore[assignment]

    try:
        ipc.send("cursorpos")
        raise AssertionError("should have raised ConnectionError")
    except ConnectionError:
        pass


def test_get_cursor_pos_single_digit():
    """get_cursor_pos handles single-digit coordinates."""
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b"0, 0", b""]
    ipc = _make_ipc_with_mock(mock_sock)

    x, y = ipc.get_cursor_pos()

    assert x == 0
    assert y == 0


def test_from_env_uses_hyprland_socket():
    """from_env() resolves socket path from HYPRLAND_INSTANCE_SIGNATURE."""
    with (
        patch("canvas.hypr.os.environ.get", return_value="test_sig"),
        patch("canvas.hypr.os.getuid", return_value=1000),
        patch("canvas.hypr.os.path.exists", return_value=True),
    ):
        ipc = HyprIPC.from_env()

    assert "test_sig" in ipc._socket_path


def test_get_active_window_geometry_parses():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [
        b'{"address":"0xabc","at":[10,20],"size":[500,300]}',
        b"",
    ]
    ipc = _make_ipc_with_mock(mock_sock)

    geo = ipc.get_active_window_geometry()

    assert geo == ("0xabc", 10, 20, 500, 300)


def test_get_active_window_geometry_none_without_address():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"at":[10,20],"size":[500,300]}', b""]
    ipc = _make_ipc_with_mock(mock_sock)

    assert ipc.get_active_window_geometry() is None


def test_get_active_window_geometry_none_on_malformed_fields():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [
        b'{"address":"0xabc","at":[10],"size":[500,300]}',
        b"",
    ]
    ipc = _make_ipc_with_mock(mock_sock)

    assert ipc.get_active_window_geometry() is None


def test_module_level_get_active_window_geometry_delegates():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [
        b'{"address":"0x9","at":[0,0],"size":[1,2]}',
        b"",
    ]

    from canvas.hypr import get_active_window_geometry

    with patch("canvas.hypr._get_default") as mock_get:
        ipc = _make_ipc_with_mock(mock_sock)
        mock_get.return_value = ipc
        geo = get_active_window_geometry()

    assert geo == ("0x9", 0, 0, 1, 2)


# --- socket path resolution -------------------------------------------------


def test_socket_path_honours_xdg_runtime_dir(tmp_path, monkeypatch):
    """Hyprland roots its runtime data at $XDG_RUNTIME_DIR/hypr. A non-standard
    value must be followed, not overridden by a hardcoded /run/user/<uid>."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
    expected = _fake_socket(str(tmp_path))

    assert _hypr_socket_path() == expected


def test_socket_path_prefers_the_instance_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
    expected = _fake_socket(str(tmp_path), "abc123")
    _fake_socket(str(tmp_path), "zzz999")

    assert _hypr_socket_path() == expected


def test_socket_path_falls_back_to_the_only_instance(tmp_path, monkeypatch):
    """One Hyprland instance still resolves without the signature set."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    expected = _fake_socket(str(tmp_path), "only")

    assert _hypr_socket_path() == expected


def test_socket_path_falls_back_to_run_user_when_env_unset(monkeypatch):
    """With no XDG_RUNTIME_DIR the search is rooted at /run/user/<uid>/hypr.

    Asserted as a property rather than a fixed result: on a machine with
    Hyprland running the fallback finds the real socket, and on a machine
    without it the call raises. Both outcomes confirm the same thing — the
    path it looks under is the legacy one, not the variable's default.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    uid = os.getuid()

    try:
        resolved = _hypr_socket_path()
    except FileNotFoundError as e:
        assert f"/run/user/{uid}/hypr" in str(e)
    else:
        assert resolved.startswith(f"/run/user/{uid}/hypr/")


def test_socket_path_error_names_the_directory_it_searched(tmp_path, monkeypatch):
    """The poller reports this exception three times before giving up, so the
    message has to say which directory was wrong."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)

    with pytest.raises(FileNotFoundError) as exc:
        _hypr_socket_path()
    assert str(tmp_path / "hypr") in str(exc.value)
