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
import stat as stat_mod
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
        for key in ("password", "source", "dest", "error"):
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
        """What a run would fetch, without copying anything: per source host,
        the matching files (path, size, mtime, where each lands) and any
        pattern that matched nothing. Returns ``{error}`` on a bad id / values /
        destination."""
        prep = self._saved_transfer_prepare(str(id or ""), values, chat_id)
        if isinstance(prep, str):
            return {"error": prep}
        entry, vals, dest_root, sources = prep
        cfg = self.get_config().get("ssh")

        def one(src: tuple[str, dict[str, Any], str | None]) -> dict[str, Any]:
            label, desc, pw = src
            res: dict[str, Any] = {"host": label, "files": [], "unmatched": []}
            release: Callable[[], None] | None = None
            try:
                sftp, _sf, release = self._open_source_conn(desc, pw, cfg)
                match = self._saved_transfer_match(
                    sftp, entry, vals, label, dest_root, len(sources) > 1
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

        with ThreadPoolExecutor(max_workers=min(len(sources), _GROUP_WORKERS)) as ex:
            results = list(ex.map(one, sources))
        return {
            "id": entry.id,
            "dest": dest_root,
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
        """Where a scheduled run of a workspace-bound scenario lands: a stable
        per-scenario folder under the console's documents dir (the same root
        ``open_file`` / ``reveal_in_folder`` reach)."""
        return str(
            config_mod.app_dir() / "documents" / "transfers" / st_mod.safe_subdir(sid)
        )

    def _saved_transfer_prepare(
        self,
        sid: str,
        values: dict[str, Any] | None,
        chat_id: str = "",
        *,
        dest: str = "",
        no_chat: bool = False,
    ) -> (
        tuple[
            st_mod.SavedTransfer,
            dict[str, Any],
            str,
            list[tuple[str, dict[str, Any], str | None]],
        ]
        | str
    ):
        """Validate a run request → ``(entry, values, dest_root, sources)``,
        or an error string. ``dest_root`` is the absolute local folder (the
        rendered ``dest_dir``, else the session workspace); each source is the
        ``(label, opener descriptor, password)`` ``_open_source_conn`` takes.

        ``dest`` overrides the entry's destination (a schedule's
        ``transfer_dest``); ``no_chat`` (a scheduled run) sends a
        workspace-bound entry to :meth:`_scheduled_transfer_dir` instead."""
        entry = self._saved_transfer_entry(sid)
        if isinstance(entry, str):
            return entry
        if dest:
            entry = replace(entry, dest_dir=dest)
        elif no_chat and not entry.dest_dir:
            entry = replace(entry, dest_dir=self._scheduled_transfer_dir(entry.id))
        try:
            vals = st_mod.resolve_values(entry, values)
        except ValueError as exc:
            return str(exc)
        if entry.filter and self._filter_entry(entry.filter) is None:
            return f"saved transfer '{entry.id}' uses unknown filter '{entry.filter}'"
        try:
            if entry.dest_dir:
                # The real root is rendered per source (it may use {{host}});
                # this one, for the first source, validates and is displayed.
                first = entry.host or entry.adhoc or "host"
                root = self._saved_transfer_root(entry, vals, first, "")
            else:
                ws = self._current_workspace_root(chat_id)
                if ws is None:
                    return "no chat workspace yet — open a chat or set a destination folder"
                root = str(ws)
            # Render once up front so a bad value fails before any connection.
            for p in entry.paths:
                st_mod.render(p, vals, host="h", escape_glob=True)
            if entry.subdir:
                st_mod.safe_subdir(st_mod.render(entry.subdir, vals, host="h"))
            if entry.filter_arg:
                st_mod.render(entry.filter_arg, vals, host="h")
        except ValueError as exc:
            return str(exc)

        sources: list[tuple[str, dict[str, Any], str | None]] = []
        if entry.host:
            sources.append((entry.host, {"kind": "host", "host": entry.host}, None))
        elif entry.group:
            for name in self._group_members(entry.group):
                he = self._host_entry(name)
                if name != "local" and he is not None and he.get("ssh"):
                    sources.append((name, {"kind": "host", "host": name}, None))
            if not sources:
                return f"host group '{entry.group}' has no remote hosts"
        else:
            side: dict[str, Any] = {
                "kind": "adhoc",
                "host": entry.adhoc,
                "jump": entry.jump,
                "user": entry.user,
                "credential_id": entry.credential_id or None,
            }
            if entry.port:
                side["port"] = entry.port
            try:
                label, desc, pw = self._normalise_transfer_side(side)
            except ValueError as exc:
                return str(exc)
            sources.append((label, desc or {}, pw))
        for label, desc, _pw in sources:
            if desc.get("kind") == "host":
                he = self._host_entry(label)
                if he is None or not he.get("ssh"):
                    return f"{label} is not a reachable remote host"
        return entry, vals, root, sources

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
        """Expand the entry's patterns on one source and decide where each
        match lands → ``{items: [(RemoteFile, local path)], unmatched: [..]}``.
        Raises ``ValueError`` on too many matches / a bad subfolder."""
        listdir, stat = _sftp_io(sftp)
        groups: list[list[st_mod.RemoteFile]] = []
        unmatched: list[str] = []
        for raw in entry.paths:
            pattern = st_mod.render(raw, vals, host=host, now=now, escape_glob=True)
            files = st_mod.expand_pattern(pattern, listdir, stat)
            if not files:
                unmatched.append(pattern)
            groups.append(files)
        chosen = st_mod.select_files(groups, entry.latest, entry.max_files, entry.id)
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
        block (dest, written files, unmatched patterns) for the agent's report;
        ``on_done`` (if given) receives that same detail when the job ends.
        ``dest`` / ``no_chat`` are passed to :meth:`_saved_transfer_prepare`."""
        from .fs_transfer import TransferItem, render_pipeline

        prep = self._saved_transfer_prepare(
            sid, values, chat_id, dest=dest, no_chat=no_chat
        )
        if isinstance(prep, str):
            return {"error": prep}
        entry, vals, dest_root, sources = prep
        filt = self._filter_entry(entry.filter) if entry.filter else None
        group = len(sources) > 1 or bool(entry.group)
        cfg = self.get_config().get("ssh")

        tid = uuid.uuid4().hex[:12]
        cancel = cancel_event or threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"saved transfer · {entry.id}",
                "group": "",
                "host": entry.source_summary(),
                "operator": operator or self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "saved_transfer": entry.id,
                    **({"filter": entry.filter} if filt else {}),
                },
                _CANCEL_KEY: cancel,
            }

        def per_source(src: tuple[str, dict[str, Any], str | None]) -> dict[str, Any]:
            label, desc, pw = src
            res: dict[str, Any] = {
                "host": label,
                "ok": True,
                "error": None,
                "written": [],
                "unmatched": [],
            }
            release: Callable[[], None] | None = None
            try:
                sftp, session_factory, release = self._open_source_conn(desc, pw, cfg)
                match = self._saved_transfer_match(
                    sftp, entry, vals, label, dest_root, group
                )
                res["unmatched"] = match["unmatched"]
                rendered = None
                if filt is not None:
                    arg = (
                        st_mod.render(entry.filter_arg, vals, host=label)
                        if entry.filter_arg
                        else None
                    )
                    rendered = render_pipeline(filt.get("pipeline", ""), arg)
                count = len(match["items"])
                for idx, (f, dst) in enumerate(match["items"]):
                    if cancel.is_set():
                        res.update(ok=False, error="cancelled")
                        break
                    name = os.path.basename(dst)
                    item = TransferItem(src=f.path, dst=dst, name=name)
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if rendered is not None:
                            self._transfer_one_filtered(
                                session_factory,
                                sftp,
                                item,
                                rendered,
                                tid,
                                idx,
                                count,
                                cancel,
                            )
                        else:
                            self._transfer_one(
                                sftp,
                                "pull",
                                item,
                                tid,
                                idx,
                                count,
                                host=label if group else None,
                            )
                        res["written"].append(dst)
                    except Exception as exc:
                        res.update(ok=False, error=str(exc))
                        detail: dict[str, Any] = {
                            "id": tid,
                            "file": item.name,
                            "index": idx,
                            "count": count,
                            "transferred": 0,
                            "total": 0,
                            "error": str(exc),
                        }
                        if group:
                            detail["host"] = label
                        self._dispatch("runspec:transfer_progress", detail)
            except Exception as exc:
                res.update(ok=False, error=str(exc))
            finally:
                if release:
                    release()
            return res

        def run() -> None:
            results: list[dict[str, Any]] = []
            try:
                with ThreadPoolExecutor(
                    max_workers=min(len(sources), _GROUP_WORKERS)
                ) as ex:
                    results = list(ex.map(per_source, sources))
            except Exception as exc:  # pragma: no cover - defensive
                results = [
                    {"host": s[0], "ok": False, "error": str(exc), "written": []}
                    for s in sources
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
                "saved transfer %s (%s): %d file(s) ok=%s", tid, entry.id, written, ok
            )
            detail: dict[str, Any] = {
                "id": tid,
                "ok": ok,
                "files": written,
                "total": written,
                "error": error,
                "saved": {"id": entry.id, "dest": dest_root, "sources": results},
            }
            if group:
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
        return {"id": tid, "dest": dest_root}

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
            if e.params:
                text += f"; params: {', '.join(sorted(e.params))}"
            return text + ")"

        return [
            {
                "name": "run_saved_transfer",
                "description": (
                    "Gather files with a saved transfer — an operator-built "
                    "scenario (source host(s) + path patterns with wildcards and "
                    "dates + optional filter) that pulls the matching files into "
                    "this chat's workspace (or its set folder). Fill open params "
                    "via 'values' (a date param takes today, yesterday, -3d or "
                    "YYYY-MM-DD). Set preview=true first to list what would be "
                    "fetched without copying. Read the fetched files with the "
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
        lines = [f"{head} Saved transfer '{sid}' fetched {n} file(s) into {dest}."]
        for s in sources:
            prefix = f"[{s['host']}] " if len(sources) > 1 else ""
            for p in s.get("written") or []:
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
        lines = [
            f"Saved transfer '{res.get('id')}' would fetch {res.get('total', 0)} "
            f"file(s) into {res.get('dest')}:"
        ]
        for s in sources:
            prefix = f"[{s['host']}] " if len(sources) > 1 else ""
            for f in s.get("files") or []:
                when = datetime.fromtimestamp(int(f.get("mtime") or 0)).strftime(
                    "%Y-%m-%d %H:%M"
                )
                lines.append(
                    f"- {prefix}{f['path']} ({_human(int(f.get('size') or 0))}, "
                    f"modified {when})"
                )
            for pat in s.get("unmatched") or []:
                lines.append(f"- {prefix}no match for {pat}")
            if s.get("error"):
                lines.append(f"- {prefix}error: {s['error']}")
        return "\n".join(lines)
