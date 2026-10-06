"""Connection lifecycle checks that do not require a board or GDB."""

import pathlib
import socket

import pytest

from support import target_interface


@pytest.fixture
def fake_gdb(monkeypatch):
    instances = []

    class FakeGdb:
        def __init__(self, command):
            self.exited = False
            instances.append(self)

        def get_gdb_response(self, **kwargs):
            return [{"type": "result", "message": "done"}]

        def write(self, command, **kwargs):
            return []

        def exit(self):
            self.exited = True

    monkeypatch.setattr(target_interface, "find_gdb", lambda *args: pathlib.Path("gdb"))
    monkeypatch.setattr(target_interface.pygdbmi.gdbcontroller, "GdbController", FakeGdb)
    return instances


@pytest.fixture
def semihosting_server():
    with socket.socket() as server:
        server.bind(("localhost", 0))
        server.listen()
        server.settimeout(1.0)
        yield server


def make_interface(port, **kwargs):
    return target_interface.OpenOcdInterface(
        runtime_crate_dir=pathlib.Path("runtime"),
        executable_file=pathlib.Path("test"),
        gdbserver_port=3333,
        terminal_io_port=port,
        **kwargs,
    )


def test_semihosting_stays_connected_between_tests(fake_gdb, semihosting_server):
    connection = target_interface.SemihostingConnection(
        semihosting_server.getsockname()[1]
    )
    assert connection.socket is None  # Skipped tests do not open a connection.
    try:
        with make_interface(connection.port, terminal_connection=connection) as first:
            client, _ = semihosting_server.accept()
            original_socket = first.terminal_socket
        with client:
            client.settimeout(1.0)
            assert fake_gdb[0].exited

            # A failed test must also leave the session's socket open.
            with pytest.raises(RuntimeError, match="test failed"):
                with make_interface(
                    connection.port, terminal_connection=connection
                ) as second:
                    assert second.terminal_socket is original_socket
                    client.sendall(b"second test output")
                    assert second.read_io(timeout=1.0) == b"second test output"
                    raise RuntimeError("test failed")

            assert fake_gdb[1].exited
            # Sending through the original socket confirms no per-test close.
            original_socket.sendall(b"still connected")
            assert client.recv(4096) == b"still connected"
            connection.close()
            assert client.recv(4096) == b""  # Disconnect only at session teardown.
    finally:
        connection.close()


def test_standalone_interface_closes_its_socket(fake_gdb, semihosting_server):
    with make_interface(semihosting_server.getsockname()[1]):
        client, _ = semihosting_server.accept()
    with client:
        client.settimeout(1.0)
        assert client.recv(4096) == b""
    assert fake_gdb[0].exited


def test_unused_connection_can_be_closed(monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("An unused connection must not contact OpenOCD")

    monkeypatch.setattr(socket, "create_connection", unexpected_connection)
    connection = target_interface.SemihostingConnection(4445)
    connection.close()
    connection.close()
    assert connection.socket is None
