"""
test_saved_transfer_bridge.py — the SavedTransfersMixin bridge seam.

A fake SFTP channel backed by a real temp directory stands in for the remote
host, so CRUD, preview, the background run (wildcards + date params + group
fan-in + collisions), the agent tool and its autonomy are exercised end to end
without a server.
"""

from __future__ import annotations

import os
import threading
import types

import pytest

from runspec_console import savedtransfers as st
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _BUILTIN_TOOL_NAMES


# Upload / relay destinations must be POSIX folders, so the tests write to a
# virtual /dst tree that the fake maps under a temp dir (works on Windows too).
DST = "/dst"


class _FakeSftp:
    """The paramiko SFTPClient calls the mixin and the download helper use,
    over the local filesystem (remote paths are real local paths, except
    ``/dst/…``, which lands under ``dest_root``)."""

    dest_root = ""

    def _p(self, path):
        if path == DST or path.startswith(DST + "/"):
            return os.path.join(self.dest_root, path[len(DST) :].lstrip("/"))
        return path

    def listdir_attr(self, d):
        d = self._p(d)
        out = []
        for name in os.listdir(d):
            a = os.lstat(os.path.join(d, name))
            out.append(
                types.SimpleNamespace(
                    filename=name,
                    st_mode=a.st_mode,
                    st_size=a.st_size,
                    st_mtime=a.st_mtime,
                )
            )
        return out

    def stat(self, path):
        return os.stat(self._p(path))

    def put(self, src, dst, callback=None):
        with open(src, "rb") as fh:
            data = fh.read()
        with open(self._p(dst), "wb") as out:
            out.write(data)
        if callback:
            callback(len(data), len(data))

    def mkdir(self, path):
        os.mkdir(self._p(path))

    def open(self, path, mode="rb"):
        return open(self._p(path), mode)

    def getfo(self, src, fdst, callback=None):
        with open(self._p(src), "rb") as fh:
            data = fh.read()
        fdst.write(data)
        if callback:
            callback(len(data), len(data))


_METHODS = (
    "get_saved_transfers",
    "save_saved_transfer",
    "delete_saved_transfer",
    "preview_saved_transfer",
    "run_saved_transfer",
    "_saved_transfer_entry",
    "_saved_transfer_prepare",
    "_saved_transfer_match",
    "_start_saved_transfer",
    "_saved_transfer_tools",
    "_saved_transfer_autonomy",
    "_run_saved_transfer_tool",
    "_workspace_rel",
    "_format_saved_result",
    "_format_saved_preview",
    "_transfer_one",
    "_sftp_download",
    "_download_special",
    "_emit_transfer_end",
    "_await_transfer",
    "_normalise_transfer_side",
    "_invoke_scheduled_transfer",
    "_run_scheduled_transfer",
    "_saved_transfer_sides",
    "_saved_transfer_source_files",
    "_saved_pull_one",
    "_saved_send_one",
    "_relay_one",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.saved_transfers_path",
        lambda: tmp_path / "runspec_saved_transfers.toml",
    )
    remote = tmp_path / "remote"
    ws = tmp_path / "workspace"
    ws.mkdir()
    ns = types.SimpleNamespace()
    for name in _METHODS:
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._format_saved_preview = Bridge._format_saved_preview
    ns._saved_transfer_root = Bridge._saved_transfer_root
    ns._saved_transfer_files = Bridge._saved_transfer_files
    ns._saved_transfer_remote_dests = Bridge._saved_transfer_remote_dests
    ns._saved_transfer_target_text = Bridge._saved_transfer_target_text
    ns._scheduled_transfer_dir = Bridge._scheduled_transfer_dir
    ns.cards = []
    ns._on_schedule_card = lambda card: ns.cards.append(dict(card))
    ns._SPECIAL_FILE_CAP = Bridge._SPECIAL_FILE_CAP
    ns._lock = threading.Lock()
    ns._in_flight = {}
    ns._recent_transfer_results = {}
    ns.dispatched = []
    ns._dispatch = lambda ev, payload: ns.dispatched.append((ev, payload))
    ns.get_config = lambda: {}
    ns._current_user = lambda: "me"
    ns._filters = []
    ns._filter_entry = lambda name: None
    ns._current_workspace_root = lambda chat_id="": ws
    ns._host_entry = lambda h: {"name": h, "ssh": f"u@{h}"}
    ns._group_members = lambda g: {"web": ["web-01", "web-02", "local"]}.get(g, [])
    ns._resolve_adhoc_credential = lambda cid, user, pw: (user, pw)
    ns.opened = []

    def open_conn(desc, pw, cfg):
        ns.opened.append(desc)
        return _FakeSftp(), None, lambda: None

    ns._open_source_conn = open_conn
    ns.remote = remote
    ns.ws = ws
    ns.dst = tmp_path / "dst"
    ns.dst.mkdir()
    monkeypatch.setattr(_FakeSftp, "dest_root", str(ns.dst))
    return ns


def _write(path, text="x", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _save(env, **row):
    res = env.save_saved_transfer(row)
    assert res["ok"], res
    return res


def _wait(env, tid):
    detail = env._await_transfer(tid, timeout=10)
    assert detail is not None
    return detail


def test_crud_round_trip(env):
    _save(env, id="a", host="web-01", paths=["/x"], params={"d": {"default": "today"}})
    rows = env.get_saved_transfers()
    assert rows[0]["source"] == "web-01"
    assert rows[0]["params"][0]["name"] == "d"
    # rename via replace_id keeps one entry
    res = env.save_saved_transfer({**rows[0], "id": "b"}, replace_id="a")
    assert res["ok"], res
    assert [r["id"] for r in st.load_saved_transfers()] == ["b"]
    # duplicate id refused, bad entry refused, nothing written
    _save(env, id="c", host="h", paths=["/y"])
    assert not env.save_saved_transfer({"id": "c", "host": "h", "paths": ["/z"]})["ok"]
    assert (
        "exactly one" in env.save_saved_transfer({"id": "d", "paths": ["/z"]})["error"]
    )
    # a typed password never lands in the file
    _save(env, id="e", adhoc="db7", password="hunter2", paths=["/z"])
    assert "password" not in st.load_saved_transfer("e")
    assert env.delete_saved_transfer("b")["ok"]
    assert not env.delete_saved_transfer("b")["ok"]
    assert ("runspec:saved_transfers_changed", {"id": "e"}) in env.dispatched


def test_preview_and_run_daily_logs(env):
    logs = env.remote / "var/log/app"
    _write(logs / "app-20261028.log", "old", 100)
    _write(logs / "app-20261028.1.log", "older", 50)
    _write(logs / "app-20261029.log", "today", 200)
    _save(
        env,
        id="app",
        host="web-01",
        paths=[f"{logs}/app-{{{{day:%Y%m%d}}}}*.log"],
        subdir="logs/{{day:%Y-%m-%d}}",
        params={"day": {"default": "today"}},
    )
    prev = env.preview_saved_transfer("app", {"day": "2026-10-28"})
    assert prev["total"] == 2
    assert {os.path.basename(f["path"]) for f in prev["sources"][0]["files"]} == {
        "app-20261028.log",
        "app-20261028.1.log",
    }
    assert not any(env.ws.iterdir())  # preview copies nothing

    started = env.run_saved_transfer("app", {"day": "2026-10-28"})
    detail = _wait(env, started["id"])
    assert detail["ok"] and detail["files"] == 2
    got = env.ws / "logs" / "2026-10-28"
    assert sorted(os.listdir(got)) == ["app-20261028.1.log", "app-20261028.log"]
    assert (got / "app-20261028.log").read_text() == "old"
    assert env._in_flight == {}


def test_latest_and_no_match(env):
    logs = env.remote / "l"
    _write(logs / "a-1.log", "1", 100)
    _write(logs / "a-2.log", "2", 300)
    _save(env, id="n", host="h", paths=[f"{logs}/a-*.log", f"{logs}/b-*.log"], latest=1)
    detail = _wait(env, env.run_saved_transfer("n")["id"])
    assert detail["ok"]
    assert os.listdir(env.ws) == ["a-2.log"]
    assert detail["saved"]["sources"][0]["unmatched"] == [f"{logs}/b-*.log"]

    # nothing matches at all → not ok
    _save(env, id="none", host="h", paths=[f"{logs}/zzz*.log"])
    d2 = _wait(env, env.run_saved_transfer("none")["id"])
    assert not d2["ok"] and d2["error"] == "no files matched"


def test_group_fans_in_per_host_and_collisions(env):
    _write(env.remote / "srv/svc1/out.log", "1")
    _write(env.remote / "srv/svc2/out.log", "2")
    _save(env, id="g", group="web", paths=[f"{env.remote}/srv/*/out.log"])
    detail = _wait(env, env.run_saved_transfer("g")["id"])
    assert detail["ok"] and detail["files"] == 4
    assert {h["host"] for h in detail["hosts"]} == {"web-01", "web-02"}  # local skipped
    assert sorted(os.listdir(env.ws / "web-01")) == ["svc1", "svc2"]
    assert (env.ws / "web-02" / "svc2" / "out.log").read_text() == "2"


def test_max_files_refused(env):
    for i in range(4):
        _write(env.remote / f"m/f{i}.log")
    _save(env, id="m", host="h", paths=[f"{env.remote}/m/*.log"], max_files=3)
    prev = env.preview_saved_transfer("m")
    assert "more than max_files" in prev["sources"][0]["error"]


def test_bad_requests(env):
    assert "no saved transfer" in env.run_saved_transfer("nope")["error"]
    _save(
        env, id="p", host="h", paths=["/x/{{svc}}"], params={"svc": {"required": True}}
    )
    assert "error" in env.run_saved_transfer("p", {})
    _save(env, id="f", host="h", paths=["/x"], filter="missing")
    assert "unknown filter" in env.run_saved_transfer("f")["error"]
    _save(env, id="d", host="h", paths=["/x"], dest_dir="relative/dir")
    assert "absolute" in env.run_saved_transfer("d")["error"]


def test_dest_dir_template(env, tmp_path):
    _write(env.remote / "z/a.log")
    out = tmp_path / "out"
    _save(
        env,
        id="z",
        host="h",
        paths=[f"{env.remote}/z/a.log"],
        dest_dir=f"{out}/{{{{host}}}}",
    )
    detail = _wait(env, env.run_saved_transfer("z")["id"])
    assert detail["ok"]
    assert (out / "h" / "a.log").exists()


def test_adhoc_source_descriptor(env):
    _write(env.remote / "q/a.log")
    _save(
        env,
        id="q",
        adhoc="db7",
        jump="bastion",
        user="svc",
        paths=[f"{env.remote}/q/a.log"],
    )
    _wait(env, env.run_saved_transfer("q")["id"])
    assert env.opened[-1] == {
        "kind": "adhoc",
        "host": "db7",
        "jump": "bastion",
        "user": "svc",
    }


def test_agent_tool_schema_autonomy_and_result(env):
    assert env._saved_transfer_tools() == []
    assert "run_saved_transfer" in _BUILTIN_TOOL_NAMES
    _write(env.remote / "t/a.log", "hello")
    _save(
        env,
        id="t",
        title="T logs",
        host="h",
        paths=[f"{env.remote}/t/*.log"],
        params={"day": {"default": "today"}},
    )
    _save(env, id="auto", host="h", paths=["/x"], autonomy="autonomous")
    [tool] = env._saved_transfer_tools()
    assert tool["input_schema"]["properties"]["id"]["enum"] == ["t", "auto"]
    assert "'t' — T logs (from h; params: day)" in tool["description"]

    assert env._saved_transfer_autonomy({"id": "t"}) == "confirm"
    assert env._saved_transfer_autonomy({"id": "t", "preview": True}) == "autonomous"
    assert env._saved_transfer_autonomy({"id": "auto"}) == "autonomous"
    assert env._saved_transfer_autonomy({"id": "nope"}) == "confirm"

    prev = env._run_saved_transfer_tool({"id": "t", "preview": True})
    assert "would fetch 1 file(s)" in prev and "a.log" in prev
    out = env._run_saved_transfer_tool({"id": "t"})
    assert out.startswith("✓ Saved transfer 't' fetched 1 file(s)")
    assert "- a.log (5 B)" in out
    assert "no param(s) named: x" in env._run_saved_transfer_tool(
        {"id": "t", "values": {"x": 1}}
    )


def test_cancel_stops_before_next_file(env):
    for i in range(3):
        _write(env.remote / f"c/f{i}.log")
    _save(env, id="c", host="h", paths=[f"{env.remote}/c/*.log"])
    ev = threading.Event()
    ev.set()
    detail = _wait(env, env._start_saved_transfer("c", None, cancel_event=ev)["id"])
    assert not detail["ok"] and "cancelled" in detail["error"]
    assert not any(env.ws.iterdir())


def _job(**kw):
    from runspec_console.sources.schedule import parse_schedule_entry

    return parse_schedule_entry(
        {"id": "nightly", "action": "saved_transfer", "when": {"every": "1d"}, **kw}
    )


def _wait_card(env, column_in=("done", "failed")):
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if env.cards and env.cards[-1]["column"] in column_in:
            return env.cards[-1]
        time.sleep(0.02)
    raise AssertionError(f"card never finished: {env.cards}")


def test_scheduled_run_lands_in_documents(env, tmp_path, monkeypatch):
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path / "app")
    logs = env.remote / "logs"
    _write(logs / "app-2026-10-28.log", "yday")
    _write(logs / "app-2026-10-29.log", "today")
    _save(
        env,
        id="app-logs",
        host="web-01",
        paths=[f"{logs}/app-{{{{day:%Y-%m-%d}}}}.log"],
        params={"day": {"default": "today"}},
    )
    tid = env._invoke_scheduled_transfer(
        _job(saved_transfer="app-logs", values={"day": "2026-10-28"})
    )
    assert tid
    card = _wait_card(env)
    assert env.cards[0]["column"] == "running"
    assert card["column"] == "done", card
    want = tmp_path / "app" / "documents" / "transfers" / "app-logs"
    assert card["detail"]["dest"] == str(want)
    assert card["detail"]["result"].startswith("1 file(s)")
    assert (want / "app-2026-10-28.log").read_text() == "yday"
    assert not any(env.ws.iterdir())  # never the chat workspace
    assert env.dispatched[-1][1]["saved"]["dest"] == str(want)


def test_scheduled_run_dest_override_and_failures(env, tmp_path):
    logs = env.remote / "logs"
    _write(logs / "a.log", "a")
    _save(env, id="a", host="web-01", paths=[f"{logs}/*.log"])
    out = tmp_path / "out"
    env._invoke_scheduled_transfer(
        _job(saved_transfer="a", transfer_dest=f"{out}/{{{{today:%Y}}}}")
    )
    card = _wait_card(env)
    assert card["column"] == "done"
    year = os.listdir(out)[0]
    assert os.listdir(out / year) == ["a.log"]

    # Nothing matched → the card fails and says so.
    env.cards.clear()
    _save(env, id="none", host="web-01", paths=[f"{logs}/*.nope"])
    env._invoke_scheduled_transfer(_job(saved_transfer="none", transfer_dest=str(out)))
    card = _wait_card(env)
    assert card["column"] == "failed"
    assert card["detail"]["error"] == "no files matched"

    # An unknown id fails at once, without starting a job.
    env.cards.clear()
    assert env._invoke_scheduled_transfer(_job(saved_transfer="gone")) == ""
    assert env.cards[-1]["column"] == "failed"
    assert "no saved transfer with id 'gone'" in env.cards[-1]["detail"]["error"]
    # A relative override is refused.
    env.cards.clear()
    env._invoke_scheduled_transfer(_job(saved_transfer="a", transfer_dest="rel/dir"))
    assert "must be an absolute path" in env.cards[-1]["detail"]["error"]


# ── uploads (push) and relays ──────────────────────────────────────────────────


def test_push_workspace_files_to_group(env):
    _write(env.ws / "out" / "report-2026-10-28.csv", "r1")
    _write(env.ws / "out" / "report-2026-10-27.csv", "r0")
    inbox = env.dst / "in"
    _save(
        env,
        id="reports",
        host="local",
        paths=["out/report-{{day}}.csv"],
        to_group="web",
        dest_dir=f"{DST}/in/{{{{host}}}}",
        subdir="daily",
        params={"day": {"default": "today"}},
    )
    prev = env.preview_saved_transfer("reports", {"day": "2026-10-28"})
    assert prev["direction"] == "push"
    assert prev["targets"] == ["web-01", "web-02"]  # local skipped
    assert prev["total"] == 1
    assert prev["sources"][0]["files"][0]["dest"] == (
        f"{DST}/in/web-01/daily/report-2026-10-28.csv"
    )
    assert not inbox.exists()  # preview copies nothing

    detail = _wait(env, env.run_saved_transfer("reports", {"day": "2026-10-28"})["id"])
    assert detail["ok"] and detail["files"] == 2, detail["saved"]
    assert detail["saved"]["direction"] == "push"
    for host in ("web-01", "web-02"):  # folders made on each destination
        got = inbox / host / "daily"
        assert os.listdir(got) == ["report-2026-10-28.csv"]
        assert (got / "report-2026-10-28.csv").read_text() == "r1"


def test_push_absolute_local_path_to_adhoc(env, tmp_path):
    src = tmp_path / "local-out"
    _write(src / "a.txt", "a")
    drop = env.dst / "drop"
    _save(
        env,
        id="up",
        host="local",
        paths=[f"{src}/*.txt"],
        to_adhoc="10.0.0.9",
        to_user="ops",
        dest_dir=f"{DST}/drop",
    )
    env._current_workspace_root = lambda chat_id="": None  # no chat needed
    detail = _wait(env, env.run_saved_transfer("up")["id"])
    assert detail["ok"], detail
    assert (drop / "a.txt").read_text() == "a"
    assert env.opened[-1]["kind"] == "adhoc"
    assert env.opened[-1]["host"] == "10.0.0.9"


def test_push_relative_path_needs_a_workspace(env):
    _save(env, id="up", host="local", paths=["x.csv"], to_host="web-01", dest_dir="/i")
    env._current_workspace_root = lambda chat_id="": None
    res = env.run_saved_transfer("up")
    assert "relative to the chat workspace" in res["error"]


def test_relay_host_to_host(env):
    logs = env.remote / "src" / "logs"
    _write(logs / "app-2026-10-28.log", "y")
    _write(logs / "app-2026-10-29.log", "t")
    drop = env.dst / "archive"
    _save(
        env,
        id="ship",
        host="web-01",
        paths=[f"{logs}/app-{{{{day:%Y-%m-%d}}}}.log"],
        to_host="backup-01",
        dest_dir=f"{DST}/archive/{{{{host}}}}",
        params={"day": {"default": "today"}},
    )
    detail = _wait(env, env.run_saved_transfer("ship", {"day": "2026-10-28"})["id"])
    assert detail["ok"], detail
    assert detail["saved"]["direction"] == "relay"
    # {{host}} is the source for a relay (the host the scenario is "about").
    assert (drop / "web-01" / "app-2026-10-28.log").read_text() == "y"
    assert detail["saved"]["dest"] == f"backup-01:{DST}/archive/web-01"
    hosts = {d["host"] for d in env.opened}
    assert hosts == {"web-01", "backup-01"}
    text = env._format_saved_result("ship", detail, "")
    assert text.startswith("✓ Saved transfer 'ship' copied 1 file(s) to backup-01:")
    preview = env.preview_saved_transfer("ship", {"day": "2026-10-28"})
    text = env._format_saved_preview(preview)
    assert "would copy 1 file(s) to backup-01:" in text
    assert f"→ {DST}/archive/web-01/app-2026-10-28.log" in text


def test_remote_dest_must_be_absolute_after_render(env):
    _save(env, id="r", host="web-01", paths=["/x"], to_host="b", dest_dir="/i")
    res = env.run_saved_transfer("r")
    assert "id" in res  # starts
    _wait(env, res["id"])
    res = env._start_saved_transfer("r", None, dest="rel/dir")
    assert "absolute path on the remote host" in res["error"]


def test_scheduled_push_reads_from_documents(env, tmp_path, monkeypatch):
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path / "app")
    outbox = tmp_path / "app" / "documents" / "transfers" / "nightly"
    _write(outbox / "export.csv", "e")
    drop = env.dst / "inbox"
    _save(
        env,
        id="nightly",
        host="local",
        paths=["*.csv"],
        to_host="web-01",
        dest_dir=f"{DST}/inbox",
    )
    env._invoke_scheduled_transfer(_job(saved_transfer="nightly"))
    card = _wait_card(env)
    assert card["column"] == "done", card
    assert (drop / "export.csv").read_text() == "e"
