"""Tests for playbook_repo — sending a Repos-tab repo with a run-playbook run."""

from __future__ import annotations

import base64
import gzip
import json
import shlex
import sys
import time

import pytest

from runspec_console import executor, playbook_repo, repos

# Shared test vector — runspec-playbook's tests assert the same hash, so the two
# sides can't drift apart on the content-hash format.
VECTOR_FILES = {
    "site.yml": "LSBob3N0czogYWxsCg==",
    "roles/web/tasks/main.yml": "LSBwaW5nOgo=",
}
VECTOR_SHA = "48adc37d996b7944c747d03fd5c97d26b03a7baa14ea736027ea75249341cd6f"


def _record(**over):
    rec = {
        "name": "run-playbook",
        "host": "web-01",
        "args": [
            {"name": "playbook", "type": "str"},
            {
                "name": "repo",
                "type": "str",
                "pattern": "^x$",
                "meta": {"console": "playbook-repo"},
            },
        ],
    }
    rec.update(over)
    return rec


def _decode(packed):
    doc = json.loads(gzip.decompress(base64.b64decode(packed.payload)))
    return doc, {p: base64.b64decode(b) for p, b in doc["files"].items()}


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_content_hash_matches_shared_vector():
    assert playbook_repo.content_hash(VECTOR_FILES) == VECTOR_SHA


def test_repo_arg_found_on_top_level_and_command():
    assert playbook_repo.repo_arg(_record(), []) == "repo"
    nested = {
        "name": "ops",
        "args": [],
        "commands": {
            "play": {"args": [{"name": "src", "meta": {"console": "playbook-repo"}}]}
        },
    }
    assert playbook_repo.repo_arg(nested, ["play"]) == "src"
    assert playbook_repo.repo_arg(nested, []) == ""
    assert playbook_repo.repo_arg({"args": [{"name": "x"}]}, []) == ""
    assert playbook_repo.repo_arg(None, []) == ""


def test_repo_value_matches_hyphen_and_underscore():
    assert playbook_repo.repo_value({"repo": "homelab"}, "repo") == "homelab"
    assert playbook_repo.repo_value({"play_repo": "x"}, "play-repo") == "x"
    assert playbook_repo.repo_value({"repo": ""}, "repo") == ""
    assert playbook_repo.repo_value({"repo": "x"}, "") == ""


def test_decorate_records_offers_repos_then_reverts():
    recs = [_record()]
    assert playbook_repo.decorate_records(recs, ["homelab", "work"]) == 1
    arg = recs[0]["args"][1]
    assert arg["type"] == "choice" and arg["options"] == ["homelab", "work"]
    assert "pattern" not in arg
    assert recs[0]["args"][0] == {"name": "playbook", "type": "str"}  # untouched
    playbook_repo.decorate_records(recs, [])
    assert arg["type"] == "str" and "options" not in arg
    assert playbook_repo.decorate_records([{"args": [{"name": "x"}]}], ["a"]) == 0


def test_encode_roundtrip_and_header():
    files = {"site.yml": b"- hosts: all\n", "roles/web/tasks/main.yml": b"- ping:\n"}
    packed = playbook_repo.encode(
        files,
        repo="homelab",
        commit="c" * 40,
        dirty=True,
        host="web-01",
        groups=["web"],
    )
    length, sha = packed.env[playbook_repo.ENV_VAR].split(":")[1:]
    assert int(length) == len(packed.payload) and sha == VECTOR_SHA
    packed.payload.decode("ascii")  # ASCII on the wire
    doc, got = _decode(packed)
    assert got == files
    assert (
        doc["repo"] == "homelab"
        and doc["host"] == "web-01"
        and doc["groups"] == ["web"]
    )
    assert "uncommitted" in packed.describe()


def test_encode_refuses_too_many_files(monkeypatch):
    monkeypatch.setattr(playbook_repo, "MAX_FILES", 2)
    with pytest.raises(playbook_repo.PlaybookRepoError, match="files"):
        playbook_repo.encode({"a": b"", "b": b"", "c": b""}, repo="r")
    with pytest.raises(playbook_repo.PlaybookRepoError, match="no files"):
        playbook_repo.encode({}, repo="r")


def test_runspec_playbook_reads_what_the_console_sends():
    """End to end across the two packages: the console's payload decodes in
    runspec-playbook (skipped when that package isn't installed)."""
    source = pytest.importorskip("runspec_playbook.source")
    import io

    files = {"site.yml": b"- hosts: all\n"}
    packed = playbook_repo.encode(files, repo="homelab", host="web-01", groups=["web"])
    sent = source.receive(packed.env[playbook_repo.ENV_VAR], io.BytesIO(packed.payload))
    assert sent.files == files and sent.repo == "homelab" and sent.groups == ("web",)


# ── a real checkout ───────────────────────────────────────────────────────────


def _make_checkout(tmp_path):
    """A git checkout with one commit (site.yml + .gitignore) — dulwich plumbing,
    no git config / signing involved."""
    pytest.importorskip("dulwich")
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    dest = tmp_path / "homelab"
    dest.mkdir()
    repo = Repo.init(str(dest))
    files = {b"site.yml": b"- hosts: all\n", b".gitignore": b"secret.txt\n"}
    root = Tree()
    blobs = []
    for name, data in files.items():
        blob = Blob.from_string(data)
        blobs.append(blob)
        root.add(name, 0o100644, blob.id)
    commit = Commit()
    commit.tree = root.id
    commit.author = commit.committer = b"T <t@t>"
    commit.author_time = commit.commit_time = 1_700_000_000
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"init"
    for obj in (*blobs, root, commit):
        repo.object_store.add_object(obj)
    repo.refs[b"refs/heads/main"] = commit.id
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    repo.close()
    for name, data in files.items():
        (dest / name.decode()).write_bytes(data)
    return dest, commit.id.decode()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    dest, commit = _make_checkout(tmp_path)
    monkeypatch.setattr(
        repos, "find_repo", lambda n: {"name": "homelab"} if n == "homelab" else None
    )
    monkeypatch.setattr(repos, "read_repos", lambda: [{"name": "homelab"}])
    monkeypatch.setattr(repos, "checkout_dir", lambda n: dest)
    return dest, commit


def test_pack_clean_checkout(checkout):
    dest, commit = checkout
    packed = playbook_repo.pack_repo("homelab", unattended=False)
    doc, files = _decode(packed)
    assert set(files) == {"site.yml", ".gitignore"}
    assert doc["commit"] == commit and doc["dirty"] is False
    assert packed.describe().startswith("▸ Sending repo homelab @ ")


def test_interactive_run_sends_edits_but_not_ignored_files(checkout):
    dest, _ = checkout
    (dest / "site.yml").write_text("- hosts: web\n")
    (dest / "new.yml").write_text("- hosts: all\n")
    (dest / "secret.txt").write_text("vault password")
    packed = playbook_repo.pack_repo("homelab", unattended=False, host="web-01")
    doc, files = _decode(packed)
    assert files["site.yml"] == b"- hosts: web\n"
    assert "new.yml" in files
    assert "secret.txt" not in files  # .gitignore'd
    assert doc["dirty"] is True and doc["host"] == "web-01"


def test_unattended_run_sends_the_committed_tree_only(checkout):
    dest, _ = checkout
    (dest / "site.yml").write_text("- hosts: web\n")
    (dest / "new.yml").write_text("x")
    packed = playbook_repo.pack_repo("homelab", unattended=True)
    doc, files = _decode(packed)
    assert files["site.yml"] == b"- hosts: all\n"
    assert "new.yml" not in files
    assert doc["dirty"] is True
    assert "local edits not sent" in packed.describe()


def test_unknown_or_unsynced_repo_is_refused(checkout, monkeypatch):
    with pytest.raises(playbook_repo.PlaybookRepoError, match="registered: homelab"):
        playbook_repo.pack_repo("nope", unattended=False)
    monkeypatch.setattr(repos, "is_checked_out", lambda n: False)
    with pytest.raises(playbook_repo.PlaybookRepoError, match="Sync it"):
        playbook_repo.pack_repo("homelab", unattended=False)


def test_send_for_uses_host_group_profiles_and_trigger(checkout):
    entry = {"name": "web-01", "group": "web", "profiles": ["home"]}
    rec = _record()
    send = playbook_repo.send_for
    assert send(rec, [], {"repo": ""}, entry, "web-01", "") is None
    assert send({"args": []}, [], {"repo": "homelab"}, entry, "web-01", "") is None
    packed = send(rec, [], {"repo": "homelab"}, entry, "web-01", "")
    doc, _ = _decode(packed)
    assert doc["host"] == "web-01" and doc["groups"] == ["web", "home"]
    assert packed.committed_only is False
    for trig in ("schedule", "email"):
        assert send(rec, [], {"repo": "homelab"}, entry, "web-01", trig).committed_only
    for trig in ("saved_run", "mcp"):
        assert not send(
            rec, [], {"repo": "homelab"}, entry, "web-01", trig
        ).committed_only


# ── executor: the payload follows the env frame on stdin ──────────────────────


def test_run_local_payload_reaches_the_runnable(tmp_path):
    """No escalation: the payload is the child's whole stdin (a real process)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "reader"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "print(os.environ.get('MARK'), len(data), data[:5].decode())\n"
    )
    script.chmod(0o755)
    if sys.platform == "win32":
        pytest.skip("POSIX script runnable")
    lines: list[str] = []
    executor.run_local(
        str(bin_dir / "runspec"),
        "reader",
        {},
        [],
        lambda line, _s: lines.append(line),
        lambda *_: None,
        cred_env={"MARK": "m1"},
        stdin_payload=b"HELLO" + b"x" * 300_000,
    )
    assert lines == ["m1 300005 HELLO"]


def test_run_local_escalated_payload_follows_the_frame(monkeypatch, tmp_path):
    from runspec.become import parse_env_frame

    seen: dict = {}
    monkeypatch.setattr(
        executor, "_stream", lambda cmd, *a, **kw: seen.update(cmd=cmd, **kw)
    )
    executor.run_local(
        str(tmp_path / "runspec"),
        "run-playbook",
        {},
        [],
        lambda *_: None,
        lambda *_: None,
        run_as="deploy",
        cred_env={"RUNSPEC_PLAYBOOK_SOURCE": "stdin:7:x"},
        stdin_payload=b"PAYLOAD",
    )
    data = seen["stdin_data"]
    assert data.endswith(b".\nPAYLOAD") or data.endswith(b"PAYLOAD")
    frame = data[: -len(b"PAYLOAD")]
    assert parse_env_frame(frame)["RUNSPEC_PLAYBOOK_SOURCE"] == "stdin:7:x"


class _Chan:
    def __init__(self) -> None:
        self.command = ""
        self.stdin = b""
        self.eof = False

    def sendall(self, data):
        self.stdin += data

    def shutdown_write(self):
        self.eof = True

    def set_combine_stderr(self, _v):
        pass

    def exec_command(self, command):
        self.command = command

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return True

    def recv(self, _n):
        return b""

    def recv_stderr(self, _n):
        return b""

    def recv_exit_status(self):
        return 0

    def close(self):
        pass


def test_run_remote_payload_follows_the_frame_on_the_channel():
    from runspec.become import parse_env_frame

    chan = _Chan()
    executor.run_remote(
        "user@host",
        "/venv/bin/runspec",
        "run-playbook",
        {"repo": "homelab"},
        [],
        lambda *_: None,
        lambda *_: None,
        session_factory=lambda: (chan, lambda: None),
        cred_env={"RUNSPEC_PLAYBOOK_SOURCE": "stdin:7:x"},
        stdin_payload=b"PAYLOAD",
    )
    deadline = time.monotonic() + 5
    while not chan.eof and time.monotonic() < deadline:
        time.sleep(0.01)
    assert chan.eof and chan.stdin.endswith(b"PAYLOAD")
    assert "PAYLOAD" not in chan.command and "stdin:7" not in chan.command
    assert (
        parse_env_frame(chan.stdin[: -len(b"PAYLOAD")])["RUNSPEC_PLAYBOOK_SOURCE"]
        == "stdin:7:x"
    )
    assert shlex.split(chan.command)[-2:] == ["--repo", "homelab"]
