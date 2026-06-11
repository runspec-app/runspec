"""
Tests for the SSH-tunnel chat transport (sources/chat_transport.py).

The NDJSON framing/buffering over a paramiko channel is pure and tested with a
fake channel — line splitting across chunk boundaries, multiple frames per chunk,
blank/malformed/non-object frames, EOF, and a trailing unterminated frame. The
factory is tested with a fake pool, and the whole thing is wired end-to-end into
ChatSource to prove the seam.
"""

from __future__ import annotations

from typing import Any

from runspec_console import config
from runspec_console.events import Event
from runspec_console.sources import chat_transport
from runspec_console.sources.chat import ChatSource
from runspec_console.sources.chat_transport import (
    ChannelChatTransport,
    default_user_from_ssh,
    host_rooms,
    make_host_room_exec_factory,
    make_host_room_factory,
    make_tunnel_transport_factory,
)


class FakeChannel:
    """Serves queued byte chunks to recv() (exhaustion == EOF); records sendall."""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])
        self.sent = b""
        self.closed = False

    def recv(self, n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        self.closed = True


class FakePool:
    """Records open_direct_tcpip calls; hands back a channel + release."""

    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.calls: list[tuple[Any, ...]] = []
        self.exec_calls: list[tuple[Any, ...]] = []
        self.released = 0

    def open_direct_tcpip(self, ssh_target, dest_host, dest_port, **kw):
        self.calls.append((ssh_target, dest_host, dest_port, kw))
        return self._channel, self._release

    def open_exec(self, ssh_target, command, **kw):
        self.exec_calls.append((ssh_target, command, kw))
        return self._channel, self._release

    def _release(self) -> None:
        self.released += 1


# ── send framing ────────────────────────────────────────────────────────────


def test_send_frames_ndjson():
    chan = FakeChannel()
    ChannelChatTransport(chan).send({"type": "message", "room": "ops", "text": "hi"})
    assert chan.sent == b'{"type": "message", "room": "ops", "text": "hi"}\n'


def test_send_after_close_is_noop():
    chan = FakeChannel()
    t = ChannelChatTransport(chan)
    t.close()
    t.send({"a": 1})
    assert chan.sent == b""


# ── recv framing / buffering ──────────────────────────────────────────────────


def test_recv_one_object():
    t = ChannelChatTransport(FakeChannel([b'{"type": "message", "text": "down"}\n']))
    assert t.recv() == {"type": "message", "text": "down"}


def test_recv_splits_across_chunks():
    t = ChannelChatTransport(FakeChannel([b'{"type": "mess', b'age", "text": "hi"}\n']))
    assert t.recv() == {"type": "message", "text": "hi"}


def test_recv_multiple_frames_in_one_chunk():
    t = ChannelChatTransport(FakeChannel([b'{"x": 1}\n{"y": 2}\n']))
    assert t.recv() == {"x": 1}
    assert t.recv() == {"y": 2}
    assert t.recv() is None  # EOF


def test_recv_skips_blank_and_malformed():
    t = ChannelChatTransport(FakeChannel([b'\n   \nnot json\n{"ok": 1}\n']))
    assert t.recv() == {"ok": 1}


def test_recv_drops_non_object_frame():
    t = ChannelChatTransport(FakeChannel([b'[1, 2, 3]\n{"a": 1}\n']))
    assert t.recv() == {"a": 1}


def test_recv_none_at_eof():
    assert ChannelChatTransport(FakeChannel([])).recv() is None


def test_recv_trailing_unterminated_frame():
    t = ChannelChatTransport(FakeChannel([b'{"z": 3}']))  # no trailing newline
    assert t.recv() == {"z": 3}
    assert t.recv() is None


def test_recv_returns_none_on_channel_error():
    class Boom(FakeChannel):
        def recv(self, n):
            raise OSError("channel closed")

    assert ChannelChatTransport(Boom()).recv() is None


# ── close ─────────────────────────────────────────────────────────────────────


def test_close_idempotent_and_releases_once():
    chan = FakeChannel()
    released = []
    t = ChannelChatTransport(chan, release=lambda: released.append(1))
    t.close()
    t.close()
    assert chan.closed is True
    assert released == [1]  # release fired exactly once


def test_close_without_release():
    chan = FakeChannel()
    ChannelChatTransport(chan).close()
    assert chan.closed is True


# ── factory ─────────────────────────────────────────────────────────────────


def test_factory_opens_tunnel_with_expected_args():
    chan = FakeChannel([b'{"type": "message", "text": "x"}\n'])
    pool = FakePool(chan)
    factory = make_tunnel_transport_factory(pool, "jump-1", 8765, identity_file="k")
    transport = factory()
    assert pool.calls == [
        ("jump-1", "127.0.0.1", 8765, {"identity_file": "k", "global_ssh_config": None})
    ]
    assert transport.recv() == {"type": "message", "text": "x"}
    transport.close()
    assert pool.released == 1


# ── host-room specs + per-host factory ────────────────────────────────────────


def test_host_rooms_extracts_specs_from_entries():
    hosts = [
        {"name": "web01", "ssh": "ops@web01:22", "room": "ops", "room_port": 8765},
        {"name": "nochat", "ssh": "x@y"},  # no room → skipped
        {"name": "noport", "ssh": "a@b", "room": "r"},  # no room_port → skipped
        {
            "name": "badport",
            "ssh": "a@b",
            "room": "r",
            "room_port": True,
        },  # bool → skip
    ]
    specs = host_rooms(hosts)
    assert len(specs) == 1
    s = specs[0]
    assert s["host"] == "web01" and s["ssh_target"] == "ops@web01:22"
    assert s["room"] == "ops" and s["port"] == 8765
    assert s["user"] == "ops@web01:22"  # SSH login default (target carries a user)


def test_host_rooms_room_user_override_and_identity():
    hosts = [
        {
            "name": "web01",
            "ssh": "web01",
            "identityFile": "~/.ssh/k",
            "room": "ops",
            "room_port": 8765,
            "room_user": "alice",
        }
    ]
    s = host_rooms(hosts)[0]
    assert s["user"] == "alice"  # explicit override wins
    assert s["identity_file"] == "~/.ssh/k"


def test_make_host_room_factory_uses_host_ssh_and_threads_config():
    ssh = {"proxy_jump": "bastion"}
    spec = {
        "host": "web01",
        "ssh_target": "ops@web01:22",
        "identity_file": "~/.ssh/k",
        "room": "ops",
        "port": 8765,
        "dest_host": "127.0.0.1",
        "user": "ops@web01:22",
    }
    pool = FakePool(FakeChannel([b'{"type": "message", "text": "x"}\n']))
    factory = make_host_room_factory(pool, spec, global_ssh_config=ssh)
    factory()
    target, dest_host, dest_port, kw = pool.calls[0]
    assert target == "ops@web01:22" and dest_host == "127.0.0.1" and dest_port == 8765
    assert kw["identity_file"] == "~/.ssh/k"  # the host's own key
    assert kw["global_ssh_config"] == ssh  # global [ssh] threaded through


def test_host_rooms_default_via_tunnel_with_exec_command():
    hosts = [{"name": "web01", "ssh": "ops@web01", "room": "ops", "room_port": 8765}]
    s = host_rooms(hosts)[0]
    assert s["via"] == "tunnel"  # default
    assert s["exec_command"] == "console-room connect --host 127.0.0.1 --port 8765"


def test_host_rooms_exec_command_full_override():
    # room_exec_command is used verbatim (e.g. socat / nc), bypassing the
    # `{room_command} connect …` builder.
    hosts = [
        {
            "name": "web01",
            "ssh": "ops@web01",
            "room": "ops",
            "room_port": 8765,
            "room_via": "exec",
            "room_exec_command": "socat - TCP:127.0.0.1:8765",
        }
    ]
    s = host_rooms(hosts)[0]
    assert s["via"] == "exec"
    assert s["exec_command"] == "socat - TCP:127.0.0.1:8765"


def test_host_rooms_exec_via_and_custom_command():
    hosts = [
        {
            "name": "web01",
            "ssh": "ops@web01",
            "room": "ops",
            "room_port": 8765,
            "room_via": "exec",
            "room_command": "~/.venv/bin/console-room",
            "room_dest_host": "localhost",
        }
    ]
    s = host_rooms(hosts)[0]
    assert s["via"] == "exec"
    assert (
        s["exec_command"]
        == "~/.venv/bin/console-room connect --host localhost --port 8765"
    )


def test_make_host_room_exec_factory_runs_connect_command():
    spec = {
        "ssh_target": "ops@web01",
        "identity_file": "~/.ssh/k",
        "exec_command": "console-room connect --host 127.0.0.1 --port 8765",
    }
    pool = FakePool(FakeChannel([b'{"type": "message", "text": "x"}\n']))
    factory = make_host_room_exec_factory(pool, spec, global_ssh_config={"user": "ops"})
    transport = factory()
    target, command, kw = pool.exec_calls[0]
    assert target == "ops@web01"
    assert command == "console-room connect --host 127.0.0.1 --port 8765"
    assert kw["identity_file"] == "~/.ssh/k" and kw["global_ssh_config"] == {
        "user": "ops"
    }
    assert transport.recv() == {"type": "message", "text": "x"}  # NDJSON framing reused


# ── end-to-end: factory → ChatSource → events ──────────────────────────────────


def test_chat_source_drains_tunnel(monkeypatch):
    monkeypatch.setattr(config, "read_chat", lambda: {})  # no config bleed-through
    chan = FakeChannel(
        [
            b'{"type": "message", "sender": "alice", "text": "one"}\n',
            b'{"type": "message", "sender": "console-1", "text": "echo"}\n',
            b'{"type": "message", "sender": "bob", "text": "two"}\n',
        ]
    )
    pool = FakePool(chan)
    src = ChatSource(
        room="ops",
        user="console-1",
        transport_factory=make_tunnel_transport_factory(pool, "jump-1", 8765),
    )
    emitted: list[Event] = []
    src.start(emitted.append)
    src._thread.join(timeout=2)  # type: ignore[union-attr]
    texts = [e.payload["text"] for e in emitted if e.type == "message"]
    assert texts == ["one", "two"]  # own echo dropped
    # presence "available" was announced over the same tunnel on start
    assert b'"status": "available"' in chan.sent


# ── SSH-defaulted room identity ────────────────────────────────────────────────


def test_default_user_from_ssh_target_with_user():
    # A user@host target is already a login identity → used as-is.
    assert default_user_from_ssh("alice@web01", "localguy") == "alice@web01"


def test_default_user_from_ssh_target_without_user():
    # Bare host → prefix the local OS user (the SSH login shape).
    assert default_user_from_ssh("web01", "alice") == "alice@web01"


def test_default_user_from_ssh_never_blank():
    assert default_user_from_ssh("", "alice") == "alice"
    assert default_user_from_ssh("", "") == "console"
    assert default_user_from_ssh("web01", "") == "web01"


def test_default_room_user_prefers_ssh_user_over_os_user(monkeypatch):
    from runspec_console.sources.chat_transport import default_room_user

    # getpass would return the *local* (e.g. Windows) user — must NOT win.
    monkeypatch.setattr(
        chat_transport.getpass, "getuser", lambda: "winuser", raising=False
    )
    # A user@host target is used as-is.
    assert default_room_user("bob@web01") == "bob@web01"
    # Bare host + configured [ssh] user → that Linux login, not the OS user.
    assert default_room_user("web01", "linuxuser") == "linuxuser@web01"
    # Bare host + no [ssh] user → falls back to the OS user (last resort).
    assert default_room_user("web01") == "winuser@web01"


def test_host_rooms_uses_ssh_user_for_bare_host(monkeypatch):
    monkeypatch.setattr(
        chat_transport.getpass, "getuser", lambda: "winuser", raising=False
    )
    hosts = [{"name": "web01", "ssh": "web01", "room": "ops", "room_port": 8765}]
    assert host_rooms(hosts, ssh_user="linuxuser")[0]["user"] == "linuxuser@web01"
