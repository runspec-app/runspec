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
    default_chat_user,
    default_user_from_ssh,
    make_tunnel_transport_factory,
    transport_factory_from_config,
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
        self.released = 0

    def open_direct_tcpip(self, ssh_target, dest_host, dest_port, **kw):
        self.calls.append((ssh_target, dest_host, dest_port, kw))
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


def test_transport_factory_from_config(monkeypatch):
    monkeypatch.setattr(
        chat_transport.config, "read_chat", lambda: {"host": "jump-1", "port": 8765}
    )
    factory = transport_factory_from_config(FakePool(FakeChannel()))
    assert factory is not None


def test_transport_factory_from_config_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(chat_transport.config, "read_chat", lambda: {})
    assert transport_factory_from_config(FakePool(FakeChannel())) is None
    # host present but port missing/non-int → still None
    monkeypatch.setattr(chat_transport.config, "read_chat", lambda: {"host": "j"})
    assert transport_factory_from_config(FakePool(FakeChannel())) is None


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


def test_default_chat_user_reads_host(monkeypatch):
    monkeypatch.setattr(
        chat_transport.getpass, "getuser", lambda: "alice", raising=False
    )
    assert default_chat_user({"host": "web01"}) == "alice@web01"
    assert default_chat_user({"host": "bob@web01"}) == "bob@web01"
