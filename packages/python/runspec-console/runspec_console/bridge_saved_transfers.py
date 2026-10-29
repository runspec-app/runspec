"""bridge_saved_transfers.py — SavedTransfersMixin: the saved-transfer seam.

A saved transfer is a reusable "gather these files" scenario (remote source +
path patterns + open params + local destination) stored in
``runspec_saved_transfers.toml`` (synced — see savedtransfers.py). This mixin is
the bridge surface over it:

* CRUD for the Transfer dialog (``get_saved_transfers`` /
  ``save_saved_transfer`` / ``delete_saved_transfer``);
* ``preview_saved_transfer`` — resolve params, expand the wildcards on each
  source host and list what would be fetched, copying nothing;
* ``run_saved_transfer`` — the operator path: one background job that expands
  and pulls every match into the destination, streaming the usual
  ``runspec:transfer_progress`` / ``runspec:transfer_end`` events;
* the ``run_saved_transfer`` agent tool (schema + handler) — the same job, run
  into the calling session's workspace and awaited, gated by the entry's own
  ``autonomy`` (``preview`` is always autonomous — it only lists);
* ``_invoke_scheduled_transfer`` — the Schedules ``saved_transfer`` action:
  the same job with no chat behind it, so a workspace-bound scenario lands in
  ``{app_dir}/documents/transfers/<id>/`` (or the schedule's ``transfer_dest``),
  reported on a Schedules run card.

Copying reuses the Transfer dialog's per-file helpers (``_transfer_one`` /
``_transfer_one_filtered``) and connection opener (``_open_source_conn``), so a
saved transfer behaves exactly like the dialog it was built in.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import stat as stat_mod
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from . import config as config_mod
from . import savedtransfers as st_mod
from .bridge_const import _CANCEL_KEY, logger
from .bridge_utils import _iso_now

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# Hosts of a group source fetched at once.
_GROUP_WORKERS = 4


def _sftp_io(
    sftp: Any,
) -> tuple[st_mod.ListDir, st_mod.Stat]:
    """``listdir`` / ``stat`` callables for :func:`savedtransfers.expand_pattern`
    over one SFTP channel. Symlinks are resolved so a linked log or folder
    behaves like the real thing."""

    def child(d: str, name: str) -> str:
        if d in ("", "."):
            return name
        return d + name if d.endswith("/") else f"{d}/{name}"

    def listdir(d: str) -> list[tuple[str, bool, int, int]]:
        out: list[tuple[str, bool, int, int]] = []
        for a in sftp.listdir_attr(d):
            mode = a.st_mode or 0
            is_dir = stat_mod.S_ISDIR(mode)
            size, mtime = int(a.st_size or 0), int(a.st_mtime or 0)
            if stat_mod.S_ISLNK(mode):
                try:
                    t = sftp.stat(child(d, a.filename))
                except OSError:
                    continue
                is_dir = stat_mod.S_ISDIR(t.st_mode or 0)
                size, mtime = int(t.st_size or 0), int(t.st_mtime or 0)
            out.append((a.filename, is_dir, size, mtime))
        return out

    def stat(path: str) -> tuple[bool, int, int] | None:
        try:
            t = sftp.stat(path)
        except OSError:
            return None
        return (
            stat_mod.S_ISDIR(t.st_mode or 0),
            int(t.st_size or 0),
            int(t.st_mtime or 0),
        )

    return listdir, stat


def _local_io() -> tuple[st_mod.ListDir, st_mod.Stat]:
    """``listdir`` / ``stat`` callables over this machine's filesystem, for a
    push's patterns (symlinks followed, like the SFTP side)."""

    def listdir(d: str) -> list[tuple[str, bool, int, int]]:
        out: list[tuple[str, bool, int, int]] = []
        with os.scandir(d) as it:
            for e in it:
                try:
                    t = e.stat()
                except OSError:
                    continue
                out.append(
                    (
                        e.name,
                        stat_mod.S_ISDIR(t.st_mode),
                        int(t.st_size),
                        int(t.st_mtime),
                    )
                )
        return out

    def stat(path: str) -> tuple[bool, int, int] | None:
        try:
            t = os.stat(path)
        except OSError:
            return None
        return stat_mod.S_ISDIR(t.st_mode), int(t.st_size), int(t.st_mtime)

    return listdir, stat


def _sftp_makedirs(sftp: Any, path: str) -> None:
    """``mkdir -p`` over SFTP for an absolute POSIX folder."""
    parts = [p for p in path.split("/") if p]
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}"
        try:
            st = sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
                continue
            except OSError:
                st = sftp.stat(cur)  # made meanwhile — fine if it's a folder
        if not stat_mod.S_ISDIR(st.st_mode or 0):
            raise NotADirectoryError(
                f"{cur} exists on the destination and isn't a folder"
            )


@dataclass
class _Plan:
    """A validated saved-transfer run (see ``_saved_transfer_prepare``)."""

    entry: st_mod.SavedTransfer
    vals: dict[str, Any]
    # pull: the local folder files land under (before subdir); push: the local
    # folder relative source paths read from ("" = none); relay: "".
    root: str
    # (label, opener descriptor, password) — the remote source host(s) of a
    # pull / relay, or [("local", …)] for a push.
    sources: list[tuple[str, dict[str, Any], str | None]]
    # The remote destination host(s) of a push / relay; [] for a pull.
    targets: list[tuple[str, dict[str, Any], str | None]]

    @property
    def direction(self) -> str:
        return self.entry.direction


# direction → (past verb, preposition, infinitive) for the agent's report.
_VERBS = {
    "pull": ("fetched", "into", "fetch"),
    "push": ("uploaded", "to", "upload"),
    "relay": ("copied", "to", "copy"),
}


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class SavedTransfersMixin:
    # ── registry CRUD (JS API) ────────────────────────────────────────────────

    def get_saved_transfers(self) -> list[dict[str, Any]]:
        """Every saved transfer, with its params parsed to the ``ArgDef`` shape
        runnable forms use (``params``) plus the stored table (``paramsRaw``)
        for a faithful round-trip. A malformed entry is listed with an
        ``error`` rather than breaking the list."""
        from . import runbookargs

        out: list[dict[str, Any]] = []
        for row in st_mod.load_saved_transfers():
            if not row.get("id"):
                continue
            raw = row.get("params") if isinstance(row.get("params"), dict) else {}
            item: dict[str, Any] = {**row, "paramsRaw": raw}
            try:
                parsed = st_mod.parse_saved_transfer(row)
                item["source"] = parsed.source_summary()
                item["dest"] = parsed.dest_summary()
                item["direction"] = parsed.direction
                item["autonomy"] = parsed.autonomy
                item["params"] = runbookargs.parse_runbook_args(raw)
            except Exception as exc:
                item["params"] = []
                item["error"] = str(exc)
            out.append(item)
        return out

    def save_saved_transfer(
        self, row: dict[str, Any], replace_id: str = ""
    ) -> dict[str, Any]:
        """Add or update one saved transfer (the Transfer dialog's *Save as
        scenario*). ``replace_id`` names the entry being edited, so a rename
        replaces it instead of leaving the old id behind. Validated before
        anything is written. Returns ``{ok, id}`` or ``{ok: False, error}``."""
        data = dict(row or {})
        raw_params = data.pop("paramsRaw", None)
        if isinstance(raw_params, dict):
            data["params"] = raw_params
        elif not isinstance(data.get("params"), dict):
            data.pop("params", None)
        for key in ("password", "to_password", "source", "dest", "direction", "error"):
            data.pop(key, None)  # never stored / display-only
        try:
            parsed = st_mod.parse_saved_transfer(data)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        rows = st_mod.load_saved_transfers()
        old = str(replace_id or "").strip()
        if parsed.id != old and any(str(r.get("id")) == parsed.id for r in rows):
            return {
                "ok": False,
                "error": f"a saved transfer with id '{parsed.id}' already exists",
            }
        new_row = st_mod.entry_to_row(parsed)
        out: list[dict[str, Any]] = []
        placed = False
        for r in rows:
            rid = str(r.get("id") or "")
            if rid in (old, parsed.id):
                if not placed:
                    out.append(new_row)
                    placed = True
                continue
            out.append(r)
        if not placed:
            out.append(new_row)
        st_mod.save_saved_transfers(out)
        self._dispatch("runspec:saved_transfers_changed", {"id": parsed.id})
        return {"ok": True, "id": parsed.id}

    def delete_saved_transfer(self, id: str) -> dict[str, Any]:
        rows = st_mod.load_saved_transfers()
        keep = [r for r in rows if str(r.get("id") or "") != str(id)]
        if len(keep) == len(rows):
            return {"ok": False, "error": f"no saved transfer with id '{id}'"}
        st_mod.save_saved_transfers(keep)
        self._dispatch("runspec:saved_transfers_changed", {"id": id})
        return {"ok": True}

    # ── preview + run (JS API) ────────────────────────────────────────────────

    def preview_saved_transfer(
        self, id: str, values: dict[str, Any] | None = None, chat_id: str = ""
    ) -> dict[str, Any]:
        """What a run would copy, without copying anything: per source host
        (a pull) or for the one source (a push / relay), the matching files
        (path, size, mtime, where each lands) and any pattern that matched
        nothing. Returns ``{error}`` on a bad id / values / destination."""
        plan = self._saved_transfer_prepare(str(id or ""), values, chat_id)
        if isinstance(plan, str):
            return {"error": plan}
        entry, vals = plan.entry, plan.vals
        cfg = self.get_config().get("ssh")
        if plan.direction != "pull":
            res: dict[str, Any] = {"host": plan.sources[0][0], "files": []}
            first = plan.targets[0][0]
            try:
                files, res["unmatched"] = self._saved_transfer_source_files(plan, cfg)
                dests = self._saved_transfer_remote_dests(entry, vals, first, files)
                res["files"] = [
                    {"path": f.path, "size": f.size, "mtime": f.mtime, "dest": d}
                    for f, d in zip(files, dests)
                ]
            except Exception as exc:
                res.update(unmatched=[], error=str(exc))
            return {
                "id": entry.id,
                "direction": plan.direction,
                "dest": self._saved_transfer_target_text(plan),
                "targets": [t[0] for t in plan.targets],
                "sources": [res],
                "total": len(res["files"]),
            }

        def one(src: tuple[str, dict[str, Any], str | None]) -> dict[str, Any]:
            label, desc, pw = src
            res: dict[str, Any] = {"host": label, "files": [], "unmatched": []}
            release: Callable[[], None] | None = None
            try:
                sftp, _sf, release = self._open_source_conn(desc, pw, cfg)
                match = self._saved_transfer_match(
                    sftp, entry, vals, label, plan.root, len(plan.sources) > 1
                )
                res["unmatched"] = match["unmatched"]
                res["files"] = [
                    {"path": f.path, "size": f.size, "mtime": f.mtime, "dest": d}
                    for f, d in match["items"]
                ]
            except Exception as exc:
                res["error"] = str(exc)
            finally:
                if release:
                    release()
            return res

        workers = min(len(plan.sources), _GROUP_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, plan.sources))
        return {
            "id": entry.id,
            "direction": "pull",
            "dest": plan.root,
            "sources": results,
            "total": sum(len(r["files"]) for r in results),
        }

    def run_saved_transfer(
        self, id: str, values: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a saved transfer now (the operator path). Starts one background
        job and returns ``{id, dest}`` at once — progress and the outcome
        arrive as ``runspec:transfer_progress`` / ``runspec:transfer_end`` — or
        ``{error}`` on a bad id / values / destination."""
        return self._start_saved_transfer(str(id or ""), values)

    # ── internals ─────────────────────────────────────────────────────────────

    def _saved_transfer_entry(self, sid: str) -> st_mod.SavedTransfer | str:
        raw = st_mod.load_saved_transfer(sid)
        if raw is None:
            known = [str(r.get("id") or "") for r in st_mod.load_saved_transfers()]
            hint = f" Known ids: {', '.join(known)}." if known else ""
            return f"no saved transfer with id '{sid}'.{hint}"
        try:
            return st_mod.parse_saved_transfer(raw)
        except ValueError as exc:
            return f"saved transfer '{sid}' is invalid — {exc}"

    @staticmethod
    def _scheduled_transfer_dir(sid: str) -> str:
        """Where a scheduled run of a workspace-bound scenario lands (and, for
        a push, where its relative paths read from): a stable per-scenario
        folder under the console's documents dir (the same root
        ``open_file`` / ``reveal_in_folder`` reach)."""
        return str(
            config_mod.app_dir() / "documents" / "transfers" / st_mod.safe_subdir(sid)
        )

    def _saved_transfer_sides(
        self,
        host: str,
        group: str,
        adhoc: str,
        jump: str,
        user: str,
        port: int,
        credential_id: str,
    ) -> list[tuple[str, dict[str, Any], str | None]] | str:
        """One end of a transfer (a fleet host, every remote member of a
        group, or an ad-hoc host) as ``(label, opener descriptor, password)``
        triples for ``_open_source_conn``, or an error string."""
        sides: list[tuple[str, dict[str, Any], str | None]] = []
        if host:
            sides.append((host, {"kind": "host", "host": host}, None))
        elif group:
            for name in self._group_members(group):
                he = self._host_entry(name)
                if name != "local" and he is not None and he.get("ssh"):
                    sides.append((name, {"kind": "host", "host": name}, None))
            if not sides:
                return f"host group '{group}' has no remote hosts"
        else:
            side: dict[str, Any] = {
                "kind": "adhoc",
                "host": adhoc,
                "jump": jump,
                "user": user,
                "credential_id": credential_id or None,
            }
            if port:
                side["port"] = port
            try:
                label, desc, pw = self._normalise_transfer_side(side)
            except ValueError as exc:
                return str(exc)
            sides.append((label, desc or {}, pw))
        for label, desc, _pw in sides:
            if desc.get("kind") == "host":
                he = self._host_entry(label)
                if he is None or not he.get("ssh"):
                    return f"{label} is not a reachable remote host"
        return sides

    def _saved_transfer_prepare(
        self,
        sid: str,
        values: dict[str, Any] | None,
        chat_id: str = "",
        *,
        dest: str = "",
        no_chat: bool = False,
    ) -> _Plan | str:
        """Validate a run request → a :class:`_Plan`, or an error string.

        ``dest`` overrides the entry's ``dest_dir`` (a schedule's
        ``transfer_dest``); ``no_chat`` (a scheduled run) uses
        :meth:`_scheduled_transfer_dir` where a chat workspace would be."""
        entry = self._saved_transfer_entry(sid)
        if isinstance(entry, str):
            return entry
        direction = entry.direction
        if dest:
            entry = replace(entry, dest_dir=dest)
        elif no_chat and not entry.dest_dir and direction == "pull":
            entry = replace(entry, dest_dir=self._scheduled_transfer_dir(entry.id))
        try:
            vals = st_mod.resolve_values(entry, values)
        except ValueError as exc:
            return str(exc)
        if entry.filter and self._filter_entry(entry.filter) is None:
            return f"saved transfer '{entry.id}' uses unknown filter '{entry.filter}'"
        if entry.filter and direction == "push" and shutil.which("sh") is None:
            return "filtering an upload needs a POSIX shell (sh) on PATH"
        root = ""
        try:
            if direction == "pull":
                if entry.dest_dir:
                    # The real root is rendered per source (it may use {{host}});
                    # this one, for the first source, validates and is displayed.
                    first = entry.host or entry.adhoc or "host"
                    root = self._saved_transfer_root(entry, vals, first, "")
                else:
                    ws = self._current_workspace_root(chat_id)
                    if ws is None:
                        return (
                            "no chat workspace yet — open a chat or set a "
                            "destination folder"
                        )
                    root = str(ws)
            else:
                first = entry.to_host or entry.to_adhoc or "host"
                remote = st_mod.render(entry.dest_dir, vals, host=first)
                if not remote.startswith("/"):
                    return (
                        f"destination folder '{remote}' must be an absolute path "
                        "on the remote host"
                    )
                if direction == "push":
                    if no_chat:
                        root = self._scheduled_transfer_dir(entry.id)
                    else:
                        ws = self._current_workspace_root(chat_id)
                        root = str(ws) if ws is not None else ""
            # Render once up front so a bad value fails before any connection.
            for p in entry.paths:
                rendered = st_mod.render(p, vals, host="h", escape_glob=True)
                if (
                    direction == "push"
                    and not root
                    and not st_mod.is_absolute(rendered)
                ):
                    return (
                        f"'{p}' is relative to the chat workspace — open a chat "
                        "or use an absolute path"
                    )
            if entry.subdir:
                st_mod.safe_subdir(st_mod.render(entry.subdir, vals, host="h"))
            if entry.filter_arg:
                st_mod.render(entry.filter_arg, vals, host="h")
        except ValueError as exc:
            return str(exc)

        targets: list[tuple[str, dict[str, Any], str | None]] = []
        if direction == "push":
            sources: list[tuple[str, dict[str, Any], str | None]] = [
                ("local", {"kind": "local"}, None)
            ]
        else:
            got = self._saved_transfer_sides(
                entry.host,
                entry.group,
                entry.adhoc,
                entry.jump,
                entry.user,
                entry.port,
                entry.credential_id,
            )
            if isinstance(got, str):
                return got
            sources = got
        if direction != "pull":
            got = self._saved_transfer_sides(
                entry.to_host,
                entry.to_group,
                entry.to_adhoc,
                entry.to_jump,
                entry.to_user,
                entry.to_port,
                entry.to_credential_id,
            )
            if isinstance(got, str):
                return got
            targets = got
        return _Plan(entry, vals, root, sources, targets)

    @staticmethod
    def _saved_transfer_root(
        entry: st_mod.SavedTransfer, vals: dict[str, Any], host: str, ws_root: str
    ) -> str:
        """The local folder one source's files land under (before ``subdir``):
        the rendered ``dest_dir``, else the session workspace ``ws_root``."""
        if not entry.dest_dir:
            return ws_root
        root = os.path.expanduser(st_mod.render(entry.dest_dir, vals, host=host))
        if not os.path.isabs(root):
            raise ValueError(f"destination folder '{root}' must be an absolute path")
        return root

    @staticmethod
    def _saved_transfer_files(
        entry: st_mod.SavedTransfer,
        vals: dict[str, Any],
        host: str,
        listdir: st_mod.ListDir,
        stat: st_mod.Stat,
        base: str = "",
        now: datetime | None = None,
    ) -> tuple[list[st_mod.RemoteFile], list[str]]:
        """Expand the entry's patterns on one source → ``(files, unmatched)``.
        A relative pattern is read under ``base`` (a push's workspace).
        Raises ``ValueError`` on too many matches."""
        groups: list[list[st_mod.RemoteFile]] = []
        unmatched: list[str] = []
        prefix = st_mod.glob_escape(base.replace("\\", "/")).rstrip("/")
        for raw in entry.paths:
            pattern = st_mod.render(raw, vals, host=host, now=now, escape_glob=True)
            if prefix and not st_mod.is_absolute(pattern):
                pattern = f"{prefix}/{pattern}"
            files = st_mod.expand_pattern(pattern, listdir, stat)
            if not files:
                unmatched.append(pattern)
            groups.append(files)
        chosen = st_mod.select_files(groups, entry.latest, entry.max_files, entry.id)
        return chosen, unmatched

    def _saved_transfer_match(
        self,
        sftp: Any,
        entry: st_mod.SavedTransfer,
        vals: dict[str, Any],
        host: str,
        dest_root: str,
        per_host_folder: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """A pull from one source: expand the patterns there and decide where
        each match lands → ``{items: [(RemoteFile, local path)], unmatched}``.
        Raises ``ValueError`` on too many matches / a bad subfolder."""
        listdir, stat = _sftp_io(sftp)
        chosen, unmatched = self._saved_transfer_files(
            entry, vals, host, listdir, stat, now=now
        )
        sub = ""
        if entry.subdir:
            sub = st_mod.safe_subdir(
                st_mod.render(entry.subdir, vals, host=host, now=now)
            )
        uses_host = "{{host" in (entry.subdir + entry.dest_dir).replace(" ", "")
        if per_host_folder and not uses_host:
            sub = (
                f"{sub}/{st_mod.safe_subdir(host)}" if sub else st_mod.safe_subdir(host)
            )
        root = self._saved_transfer_root(entry, vals, host, dest_root)
        base = st_mod.local_join(root, sub) if sub else root
        items = [
            (f, st_mod.local_join(base, rel))
            for f, rel in zip(chosen, st_mod.dest_names(chosen))
        ]
        return {"items": items, "unmatched": unmatched}

    def _saved_transfer_source_files(
        self, plan: _Plan, cfg: Any
    ) -> tuple[list[st_mod.RemoteFile], list[str]]:
        """A push / relay's files, matched once on its single source (this
        machine, or the remote source over SFTP)."""
        entry, vals = plan.entry, plan.vals
        if plan.direction == "push":
            listdir, stat = _local_io()
            return self._saved_transfer_files(
                entry, vals, "local", listdir, stat, base=plan.root
            )
        label, desc, pw = plan.sources[0]
        release: Callable[[], None] | None = None
        try:
            sftp, _sf, release = self._open_source_conn(desc, pw, cfg)
            listdir, stat = _sftp_io(sftp)
            return self._saved_transfer_files(entry, vals, label, listdir, stat)
        finally:
            if release:
                release()

    @staticmethod
    def _saved_transfer_remote_dests(
        entry: st_mod.SavedTransfer,
        vals: dict[str, Any],
        target: str,
        files: list[st_mod.RemoteFile],
    ) -> list[str]:
        """Where each file lands on one remote destination host. ``{{host}}``
        is the destination for a push and the source for a relay (the remote
        host the scenario is "about")."""
        host = target if entry.direction == "push" else (entry.host or entry.adhoc)
        base = posixpath.normpath(st_mod.render(entry.dest_dir, vals, host=host))
        if entry.subdir:
            sub = st_mod.safe_subdir(st_mod.render(entry.subdir, vals, host=host))
            if sub:
                base = posixpath.join(base, sub)
        return [posixpath.join(base, rel) for rel in st_mod.dest_names(files)]

    @staticmethod
    def _saved_transfer_target_text(plan: _Plan) -> str:
        """``web-01, web-02:/srv/in`` — where a push / relay lands."""
        names = ", ".join(t[0] for t in plan.targets)
        entry = plan.entry
        host = (
            plan.targets[0][0]
            if entry.direction == "push"
            else (entry.host or entry.adhoc)
        )
        folder = st_mod.render(entry.dest_dir, plan.vals, host=host)
        if entry.subdir:
            folder = posixpath.join(
                folder, st_mod.render(entry.subdir, plan.vals, host=host)
            )
        return f"{names}:{folder}"

    def _start_saved_transfer(
        self,
        sid: str,
        values: dict[str, Any] | None,
        chat_id: str = "",
        cancel_event: threading.Event | None = None,
        *,
        dest: str = "",
        no_chat: bool = False,
        operator: str = "",
        on_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Validate, then start the background job. Returns ``{id, dest}`` or
        ``{error}``. The job's ``transfer_end`` detail carries a ``saved``
        block (direction, dest, per-host written files, unmatched patterns)
        for the agent's report; ``on_done`` (if given) receives that same
        detail when the job ends. ``dest`` / ``no_chat`` are passed to
        :meth:`_saved_transfer_prepare`."""
        from .fs_transfer import render_pipeline

        plan = self._saved_transfer_prepare(
            sid, values, chat_id, dest=dest, no_chat=no_chat
        )
        if isinstance(plan, str):
            return {"error": plan}
        entry = plan.entry
        filt = self._filter_entry(entry.filter) if entry.filter else None
        cfg = self.get_config().get("ssh")
        if plan.direction == "pull":
            fan = plan.sources
            multi = len(fan) > 1 or bool(entry.group)
            dest_text = plan.root
            where = entry.source_summary()
        else:
            fan = plan.targets
            multi = len(fan) > 1 or bool(entry.to_group)
            dest_text = self._saved_transfer_target_text(plan)
            where = f"{entry.source_summary()} → {entry.target_summary()}"

        tid = uuid.uuid4().hex[:12]
        cancel = cancel_event or threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"saved transfer · {entry.id}",
                "group": "",
                "host": where,
                "operator": operator or self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "saved_transfer": entry.id,
                    "direction": plan.direction,
                    **({"filter": entry.filter} if filt else {}),
                },
                _CANCEL_KEY: cancel,
            }

        def pipeline_for(host: str) -> str | None:
            if filt is None:
                return None
            arg = (
                st_mod.render(entry.filter_arg, plan.vals, host=host)
                if entry.filter_arg
                else None
            )
            return render_pipeline(filt.get("pipeline", ""), arg)

        def report_error(
            label: str, item: Any, idx: int, count: int, exc: Exception
        ) -> None:
            detail: dict[str, Any] = {
                "id": tid,
                "file": item.name,
                "index": idx,
                "count": count,
                "transferred": 0,
                "total": 0,
                "error": str(exc),
            }
            if multi:
                detail["host"] = label
            self._dispatch("runspec:transfer_progress", detail)

        files: list[st_mod.RemoteFile] = []
        src_pipeline: str | None = None

        def per_host(side: tuple[str, dict[str, Any], str | None]) -> dict[str, Any]:
            label = side[0]
            res: dict[str, Any] = {
                "host": label,
                "ok": True,
                "error": None,
                "written": [],
                "unmatched": [],
            }
            releases: list[Callable[[], None]] = []
            try:
                if plan.direction == "pull":
                    self._saved_pull_one(
                        side,
                        plan,
                        res,
                        releases,
                        cfg,
                        tid,
                        cancel,
                        multi,
                        pipeline_for(label),
                        report_error,
                    )
                else:
                    self._saved_send_one(
                        side,
                        plan,
                        files,
                        res,
                        releases,
                        cfg,
                        tid,
                        cancel,
                        multi,
                        src_pipeline,
                        report_error,
                    )
            except Exception as exc:
                res.update(ok=False, error=str(exc))
            finally:
                for release in releases:
                    release()
            return res

        def run() -> None:
            nonlocal files, src_pipeline
            results: list[dict[str, Any]] = []
            unmatched: list[str] = []
            try:
                if plan.direction != "pull":
                    files, unmatched = self._saved_transfer_source_files(plan, cfg)
                    src_pipeline = pipeline_for(
                        plan.sources[0][0] if plan.direction == "relay" else "local"
                    )
                with ThreadPoolExecutor(
                    max_workers=min(len(fan), _GROUP_WORKERS)
                ) as ex:
                    results = list(ex.map(per_host, fan))
            except Exception as exc:
                results = [
                    {"host": s[0], "ok": False, "error": str(exc), "written": []}
                    for s in fan
                ]
            finally:
                with self._lock:
                    self._in_flight.pop(tid, None)
            written = sum(len(r.get("written") or []) for r in results)
            ok = bool(results) and all(r["ok"] for r in results) and written > 0
            error = None
            if not ok:
                errs = [f"{r['host']}: {r['error']}" for r in results if r.get("error")]
                error = "; ".join(errs) if errs else "no files matched"
            logger.info(
                "saved transfer %s (%s, %s): %d file(s) ok=%s",
                tid,
                entry.id,
                plan.direction,
                written,
                ok,
            )
            detail: dict[str, Any] = {
                "id": tid,
                "ok": ok,
                "files": written,
                "total": written,
                "error": error,
                "saved": {
                    "id": entry.id,
                    "direction": plan.direction,
                    "dest": dest_text,
                    "sources": results,
                    "unmatched": unmatched,
                },
            }
            if multi:
                detail["hosts"] = [
                    {"host": r["host"], "ok": r["ok"], "error": r.get("error")}
                    for r in results
                ]
            self._emit_transfer_end(tid, detail)
            if on_done is not None:
                try:
                    on_done(detail)
                except Exception:
                    logger.exception("saved transfer %s: on_done failed", tid)

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid, "dest": dest_text}

    def _saved_pull_one(
        self,
        side: tuple[str, dict[str, Any], str | None],
        plan: _Plan,
        res: dict[str, Any],
        releases: list[Callable[[], None]],
        cfg: Any,
        tid: str,
        cancel: threading.Event,
        multi: bool,
        rendered: str | None,
        report_error: Callable[..., None],
    ) -> None:
        """Pull one source's matches onto this machine (fills ``res``)."""
        from .fs_transfer import TransferItem

        label, desc, pw = side
        sftp, session_factory, release = self._open_source_conn(desc, pw, cfg)
        releases.append(release)
        match = self._saved_transfer_match(
            sftp, plan.entry, plan.vals, label, plan.root, multi
        )
        res["unmatched"] = match["unmatched"]
        count = len(match["items"])
        for idx, (f, dst) in enumerate(match["items"]):
            if cancel.is_set():
                res.update(ok=False, error="cancelled")
                break
            item = TransferItem(src=f.path, dst=dst, name=os.path.basename(dst))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if rendered is not None:
                    self._transfer_one_filtered(
                        session_factory, sftp, item, rendered, tid, idx, count, cancel
                    )
                else:
                    self._transfer_one(
                        sftp,
                        "pull",
                        item,
                        tid,
                        idx,
                        count,
                        host=label if multi else None,
                    )
                res["written"].append(dst)
            except Exception as exc:
                res.update(ok=False, error=str(exc))
                report_error(label, item, idx, count, exc)

    def _saved_send_one(
        self,
        side: tuple[str, dict[str, Any], str | None],
        plan: _Plan,
        files: list[st_mod.RemoteFile],
        res: dict[str, Any],
        releases: list[Callable[[], None]],
        cfg: Any,
        tid: str,
        cancel: threading.Event,
        multi: bool,
        rendered: str | None,
        report_error: Callable[..., None],
    ) -> None:
        """Copy the matched files to one remote destination host — from this
        machine (push) or from the remote source (relay). Folders are created
        on the destination as needed. Fills ``res``."""
        from .fs_transfer import TransferItem

        label, desc, pw = side
        dst_sftp, _sf, release = self._open_source_conn(desc, pw, cfg)
        releases.append(release)
        src_sftp: Any = None
        src_factory: Any = None
        if plan.direction == "relay":
            _l, src_desc, src_pw = plan.sources[0]
            src_sftp, src_factory, src_release = self._open_source_conn(
                src_desc, src_pw, cfg
            )
            releases.append(src_release)
        dests = self._saved_transfer_remote_dests(plan.entry, plan.vals, label, files)
        made: set[str] = set()
        count = len(files)
        for idx, (f, dst) in enumerate(zip(files, dests)):
            if cancel.is_set():
                res.update(ok=False, error="cancelled")
                break
            item = TransferItem(src=f.path, dst=dst, name=posixpath.basename(dst))
            try:
                parent = posixpath.dirname(dst)
                if parent not in made:
                    _sftp_makedirs(dst_sftp, parent)
                    made.add(parent)
                if plan.direction == "relay":
                    if rendered is not None:
                        self._relay_one_filtered(
                            src_factory,
                            dst_sftp,
                            item,
                            rendered,
                            src_sftp,
                            tid,
                            idx,
                            count,
                            cancel,
                        )
                    else:
                        self._relay_one(
                            src_sftp, dst_sftp, item, tid, idx, count, cancel
                        )
                elif rendered is not None:
                    self._transfer_one_filtered_local(
                        dst_sftp, item, rendered, tid, idx, count, cancel
                    )
                else:
                    self._transfer_one(
                        dst_sftp,
                        "push",
                        item,
                        tid,
                        idx,
                        count,
                        host=label if multi else None,
                    )
                res["written"].append(dst)
            except Exception as exc:
                res.update(ok=False, error=str(exc))
                report_error(label, item, idx, count, exc)

    # ── schedules ─────────────────────────────────────────────────────────────

    def _run_scheduled_transfer(self, job: Any) -> None:
        """ScheduleSource's ``run_transfer`` callback (the timer path)."""
        self._invoke_scheduled_transfer(job)

    def _invoke_scheduled_transfer(self, job: Any) -> str:
        """Run a ``saved_transfer`` schedule: start the saved transfer with the
        schedule's ``values`` (and ``transfer_dest``, if set) and track it on a
        Schedules run card that ends ``done`` / ``failed`` with what was
        written. Returns the transfer id (``""`` when it couldn't start — the
        card then says why). Runs without asking: scheduling it is the
        authorization, as for a saved run."""
        from .sources.schedule import schedule_label

        sid = str(getattr(job, "saved_transfer", "") or "")
        card_id = uuid.uuid4().hex[:12]
        created = time.time()
        card: dict[str, Any] = {
            "card_id": card_id,
            "trigger": schedule_label(job),
            "source": "schedule",
            "event_type": "saved_transfer",
            "summary": f"saved transfer: {sid}",
            "column": "running",
            "detail": {
                "action": "saved_transfer",
                "saved_transfer": sid,
                "values": dict(getattr(job, "values", {}) or {}),
                "schedule_id": job.id,
            },
            "created_at": created,
            "timeline": [{"ts": created, "text": "running"}],
        }
        self._on_schedule_card(card)

        def finish(column: str, **detail: Any) -> None:
            now = time.time()
            card["column"] = column
            card["detail"] = {**card["detail"], **detail}
            card["timeline"] = [*card["timeline"], {"ts": now, "text": column}]
            self._on_schedule_card(card)

        def done(detail: dict[str, Any]) -> None:
            saved = detail.get("saved") or {}
            n = int(detail.get("files") or 0)
            dest = str(saved.get("dest") or "")
            result = f"{n} file(s) → {dest}"
            if detail.get("ok"):
                finish("done", result=result, dest=dest)
            else:
                finish(
                    "failed",
                    result=result if n else "",
                    dest=dest,
                    error=str(detail.get("error") or "transfer failed"),
                )

        started = self._start_saved_transfer(
            sid,
            dict(getattr(job, "values", {}) or {}),
            dest=str(getattr(job, "transfer_dest", "") or ""),
            no_chat=True,
            operator="Scheduled Task",
            on_done=done,
        )
        if started.get("error"):
            logger.warning("schedule '%s': %s", job.id, started["error"])
            finish("failed", error=str(started["error"]))
            return ""
        return str(started.get("id") or "")

    # ── agent tool ────────────────────────────────────────────────────────────

    def _saved_transfer_tools(self) -> list[dict[str, Any]]:
        """The ``run_saved_transfer`` schema, offered only when at least one
        saved transfer exists. Its description is the menu (id, title, source,
        params) so the agent needs no lookup to pick one."""
        rows: list[st_mod.SavedTransfer] = []
        for raw in st_mod.load_saved_transfers():
            try:
                rows.append(st_mod.parse_saved_transfer(raw))
            except ValueError:
                continue
        if not rows:
            return []

        def line(e: st_mod.SavedTransfer) -> str:
            text = f"'{e.id}'"
            if e.title:
                text += f" — {e.title}"
            text += f" (from {e.source_summary()}"
            if e.direction != "pull":
                text += f" to {e.target_summary()}"
            if e.params:
                text += f"; params: {', '.join(sorted(e.params))}"
            return text + ")"

        return [
            {
                "name": "run_saved_transfer",
                "description": (
                    "Copy files with a saved transfer — an operator-built "
                    "scenario (source + path patterns with wildcards and dates + "
                    "optional filter). Most pull the matching files from remote "
                    "host(s) into this chat's workspace (or a set folder); one "
                    "'from this computer to …' uploads files (paths relative to "
                    "the workspace) to remote host(s); one 'from <host> to …' "
                    "copies between two remote hosts. Fill open params via "
                    "'values' (a date param takes today, yesterday, -3d or "
                    "YYYY-MM-DD). Set preview=true first to list what would be "
                    "copied without copying. Read pulled files with the "
                    "workspace_* tools. Available: "
                    + "; ".join(line(e) for e in rows)
                    + "."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "enum": [e.id for e in rows],
                            "description": "Which saved transfer to run.",
                        },
                        "values": {
                            "type": "object",
                            "description": (
                                "Values for the saved transfer's params "
                                "(required ones must be supplied)."
                            ),
                        },
                        "preview": {
                            "type": "boolean",
                            "description": (
                                "List the matching files and where they would "
                                "land, without copying anything."
                            ),
                        },
                    },
                    "required": ["id"],
                },
            }
        ]

    def _saved_transfer_autonomy(self, tool_input: dict[str, Any]) -> str:
        """Gate level for a ``run_saved_transfer`` call: a preview only lists,
        so it never asks; a run takes the entry's own ``autonomy`` (confirm
        unless the operator opted the scenario down); unknown id → confirm."""
        ti = dict(tool_input or {})
        if ti.get("preview"):
            return "autonomous"
        entry = self._saved_transfer_entry(str(ti.get("id") or ""))
        return "confirm" if isinstance(entry, str) else entry.autonomy

    def _run_saved_transfer_tool(
        self,
        ti: dict[str, Any],
        chat_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Agent handler — preview, or run into the calling session's
        workspace and wait for the result."""
        sid = str(ti.get("id") or "")
        values = ti.get("values") if isinstance(ti.get("values"), dict) else None
        if ti.get("preview"):
            res = self.preview_saved_transfer(sid, values, chat_id=chat_id)
            if res.get("error"):
                return f"Error: {res['error']}"
            return self._format_saved_preview(res)
        started = self._start_saved_transfer(sid, values, chat_id, cancel_event)
        if started.get("error"):
            return f"✗ Could not start saved transfer '{sid}': {started['error']}"
        detail = self._await_transfer(started["id"])
        if detail is None:
            return (
                f"Started saved transfer '{sid}' (id {started['id']}); still "
                "running — check the transfer panel for the result."
            )
        return self._format_saved_result(sid, detail, chat_id)

    def _workspace_rel(self, path: str, dest_root: str, chat_id: str) -> str:
        """A written path as the agent should name it: relative to the session
        workspace when it's inside it, else absolute."""
        ws = self._current_workspace_root(chat_id)
        if ws is not None:
            try:
                rel = os.path.relpath(path, str(ws))
                if not rel.startswith(".."):
                    return rel.replace(os.sep, "/")
            except ValueError:
                pass
        return path

    def _format_saved_result(
        self, sid: str, detail: dict[str, Any], chat_id: str
    ) -> str:
        saved = detail.get("saved") or {}
        dest = str(saved.get("dest") or "")
        sources = saved.get("sources") or []
        n = int(detail.get("files") or 0)
        head = "✓" if detail.get("ok") else ("⚠" if n else "✗")
        pull = saved.get("direction", "pull") == "pull"
        verb = _VERBS.get(str(saved.get("direction") or "pull"), _VERBS["pull"])
        lines = [
            f"{head} Saved transfer '{sid}' {verb[0]} {n} file(s) {verb[1]} {dest}."
        ]
        for unmatched in saved.get("unmatched") or []:
            lines.append(f"- no match for {unmatched}")
        for s in sources:
            prefix = f"[{s['host']}] " if len(sources) > 1 else ""
            for p in s.get("written") or []:
                if not pull:
                    lines.append(f"- {prefix}{p}")
                    continue
                try:
                    size = f" ({_human(os.path.getsize(p))})"
                except OSError:
                    size = ""
                lines.append(f"- {prefix}{self._workspace_rel(p, dest, chat_id)}{size}")
            for pat in s.get("unmatched") or []:
                lines.append(f"- {prefix}no match for {pat}")
            if s.get("error"):
                lines.append(f"- {prefix}error: {s['error']}")
        return "\n".join(lines)

    @staticmethod
    def _format_saved_preview(res: dict[str, Any]) -> str:
        sources = res.get("sources") or []
        pull = res.get("direction", "pull") == "pull"
        verb = _VERBS.get(str(res.get("direction") or "pull"), _VERBS["pull"])
        lines = [
            f"Saved transfer '{res.get('id')}' would {verb[2]} {res.get('total', 0)} "
            f"file(s) {verb[1]} {res.get('dest')}:"
        ]
        for s in sources:
            prefix = f"[{s['host']}] " if len(sources) > 1 else ""
            for f in s.get("files") or []:
                when = datetime.fromtimestamp(int(f.get("mtime") or 0)).strftime(
                    "%Y-%m-%d %H:%M"
                )
                to = "" if pull else f" → {f.get('dest')}"
                lines.append(
                    f"- {prefix}{f['path']} ({_human(int(f.get('size') or 0))}, "
                    f"modified {when}){to}"
                )
            for pat in s.get("unmatched") or []:
                lines.append(f"- {prefix}no match for {pat}")
            if s.get("error"):
                lines.append(f"- {prefix}error: {s['error']}")
        return "\n".join(lines)
