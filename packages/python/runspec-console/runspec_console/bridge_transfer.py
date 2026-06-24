"""bridge_transfer.py — TransferMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any


from .bridge_const import (
    logger,
    _CANCEL_KEY,
)
from .bridge_utils import (
    _iso_now,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class TransferMixin:
    def _open_sftp(
        self, ssh_target: str, identity_file: str | None, cfg: dict[str, Any] | None
    ) -> tuple[Any, Callable[[], None]]:
        """Open an SFTP channel on ``ssh_target`` — pooled when pooling is on."""
        pool = self._ssh_pool
        if pool is not None:
            return pool.open_sftp(
                ssh_target, identity_file=identity_file, global_ssh_config=cfg
            )
        from .executor import _make_ssh_client

        client = _make_ssh_client(ssh_target, identity_file, cfg)
        sftp = client.open_sftp()

        def release() -> None:
            for closer in (sftp, client):
                try:
                    closer.close()
                except Exception:
                    pass

        return sftp, release

    def start_transfer(
        self,
        source_host: str,
        source_paths: list[str],
        dest_host: str,
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Copy files between the local machine and a remote host over SFTP.

        v1 supports **local↔remote only** (exactly one side is ``"local"``).
        ``filter_name`` selects a saved source-side filter (``filter_arg`` is the
        runtime value for its ``{{arg}}`` placeholder); it applies only to a
        host and only the output is written. For a **download** (pull) the filter
        runs on the remote source; for an **upload** (push) it runs locally over
        the local source (needs a POSIX ``sh`` on PATH). Returns ``{"id": ...}``
        and then streams ``runspec:transfer_progress`` / ``runspec:transfer_end``
        events, or ``{"error": ...}`` on a bad request.
        """

        from .fs_transfer import plan_transfer, render_pipeline

        try:
            plan = plan_transfer(source_host, source_paths, dest_host, dest_dir)
        except ValueError as e:
            return {"error": str(e)}

        entry = self._host_entry(plan.remote_host)
        if entry is None or not entry.get("ssh"):
            return {"error": f"{plan.remote_host} is not a reachable remote host"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            if plan.direction == "push" and shutil.which("sh") is None:
                return {"error": "filtering an upload needs a POSIX shell (sh) on PATH"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"transfer · {plan.direction}",
                "group": "",
                "host": plan.remote_host,
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "direction": plan.direction,
                    "files": len(plan.items),
                    **({"filter": filter_name} if rendered is not None else {}),
                },
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            cfg = self.get_config().get("ssh")
            ok, error, done = True, None, 0
            release: Callable[[], None] | None = None
            count = len(plan.items)

            def session_factory() -> tuple[Any, Callable[[], None]]:
                pool = self._ssh_pool
                if pool is not None:
                    return pool.open_session(
                        entry["ssh"],
                        identity_file=entry.get("identityFile"),
                        global_ssh_config=cfg,
                    )
                from .executor import _make_ssh_client

                client = _make_ssh_client(entry["ssh"], entry.get("identityFile"), cfg)
                transport = client.get_transport()
                if transport is None:
                    client.close()
                    raise OSError("source transport unavailable")
                return transport.open_session(), client.close

            try:
                sftp, release = self._open_sftp(
                    entry["ssh"], entry.get("identityFile"), cfg
                )
                for idx, item in enumerate(plan.items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        if rendered is not None and plan.direction == "pull":
                            self._transfer_one_filtered(
                                session_factory,
                                sftp,
                                item,
                                rendered,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        elif rendered is not None:  # push: filter locally
                            self._transfer_one_filtered_local(
                                sftp,
                                item,
                                rendered,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        else:
                            self._transfer_one(
                                sftp, plan.direction, item, tid, idx, count
                            )
                        done += 1
                    except Exception as e:
                        # One file failing is reported but doesn't abort the rest.
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                if release:
                    release()
                with self._lock:
                    self._in_flight.pop(tid, None)
            logger.info(
                "transfer %s %s: %d/%d ok=%s", tid, plan.direction, done, count, ok
            )
            self._emit_transfer_end(
                tid,
                {"id": tid, "ok": ok, "files": done, "total": count, "error": error},
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def _transfer_one(
        self,
        sftp: Any,
        direction: str,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        host: str | None = None,
    ) -> None:
        """SFTP put/get a single file, emitting throttled progress events.

        ``host`` is set for fan-out (one upload per target host) so the UI can
        show per-host progress; it is omitted for a single transfer.
        """
        last = [0.0]

        def cb(transferred: int, total: int) -> None:
            now = time.monotonic()
            # ``total`` is 0 for a special file (size unknown) — throttle on time
            # alone then; otherwise also flush the final chunk (transferred==total).
            if now - last[0] >= 0.15 or (total and transferred >= total):
                last[0] = now
                detail: dict[str, Any] = {
                    "id": tid,
                    "file": item.name,
                    "index": idx,
                    "count": count,
                    "transferred": transferred,
                    "total": total,
                }
                if host is not None:
                    detail["host"] = host
                self._dispatch("runspec:transfer_progress", detail)

        if direction == "push":
            if os.path.isdir(item.src):
                raise IsADirectoryError(
                    f"{item.name} is a directory — directory transfer isn't supported yet"
                )
            sftp.put(item.src, item.dst, callback=cb)
        else:
            self._sftp_download(sftp, item, cb)

    # Cap for raw reads of special (size-0) files so an unbounded stream
    # (e.g. /proc/kcore) can't fill the disk — large/streaming files should be
    # captured through a filter instead.
    _SPECIAL_FILE_CAP = 256 * 1024 * 1024  # 256 MiB

    def _sftp_download(
        self, sftp: Any, item: Any, cb: Callable[[int, int], None]
    ) -> None:
        """Pull one remote file, tolerant of special (size-0) files.

        ``sftp.get`` can't be used here: it asserts the downloaded size equals
        the remote ``stat()`` size, which is always 0 for procfs/sysfs even
        though the file streams real bytes — so a ``/proc/cpuinfo`` pull always
        fails with "size mismatch in get". We stat for a directory guard, then
        stream via ``getfo`` (same progress callback, no size assertion). A
        special file (stat size 0) takes a bounded raw read instead.
        """
        st = sftp.stat(item.src)
        if stat.S_ISDIR(st.st_mode):
            raise IsADirectoryError(
                f"{item.name} is a directory — directory transfer isn't supported yet"
            )
        if st.st_size == 0:
            self._download_special(sftp, item, cb)
            return
        with open(item.dst, "wb") as fdst:
            sftp.getfo(item.src, fdst, callback=cb)

    def _download_special(
        self, sftp: Any, item: Any, cb: Callable[[int, int], None]
    ) -> None:
        """Stream a size-0 special file to ``item.dst`` until EOF, capped at
        ``_SPECIAL_FILE_CAP`` bytes. The total is unknown, so progress reports
        bytes-so-far with a ``total`` of 0 (the UI treats that as indeterminate)."""
        transferred = 0
        with sftp.open(item.src, "rb") as fsrc, open(item.dst, "wb") as fdst:
            while True:
                chunk = fsrc.read(32768)
                if not chunk:
                    break
                transferred += len(chunk)
                if transferred > self._SPECIAL_FILE_CAP:
                    raise OSError(
                        f"{item.name} exceeds "
                        f"{self._SPECIAL_FILE_CAP // (1024 * 1024)} MiB as a "
                        "streaming/special file — use a filter to capture it"
                    )
                fdst.write(chunk)
                cb(transferred, 0)

    def _emit_transfer_end(self, tid: str, detail: dict[str, Any]) -> None:
        """Dispatch the ``runspec:transfer_end`` UI event and record the result so
        a synchronous waiter (``_await_transfer``) can read the outcome."""
        self._dispatch("runspec:transfer_end", detail)
        with self._lock:
            self._recent_transfer_results[tid] = detail
            # Keep the map bounded — drop the oldest insertions.
            while len(self._recent_transfer_results) > 64:
                oldest = next(iter(self._recent_transfer_results))
                self._recent_transfer_results.pop(oldest, None)

    def _await_transfer(
        self, tid: str, timeout: float = 600.0
    ) -> dict[str, Any] | None:
        """Block until the transfer ``tid`` finishes (or ``timeout`` elapses),
        returning its final ``transfer_end`` detail. Result is recorded by
        ``_emit_transfer_end`` whether or not anyone waits, so there's no race."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                detail = self._recent_transfer_results.pop(tid, None)
            if detail is not None:
                return detail
            time.sleep(0.05)
        return None

    def cancel_transfer(self, transfer_id: str) -> None:
        """Request cancellation of an in-flight transfer (stops before the next file)."""
        with self._lock:
            inv = self._in_flight.get(transfer_id)
        ev = inv.get(_CANCEL_KEY) if inv else None
        if ev is not None:
            ev.set()

    def start_fanout(
        self,
        source_paths: list[str],
        dest_hosts: list[str],
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Upload the same local source file(s) to ``dest_dir`` on **many** remote hosts.

        Push-only (local → N remotes), one SFTP transfer per host in parallel over
        each host's pooled transport. When ``filter_name`` is set, each source is
        filtered **once** locally (needs a POSIX ``sh``) and the filtered output is
        what gets fanned out. Returns ``{"id": ...}`` and streams
        ``runspec:transfer_progress`` events carrying a ``host`` field, then a
        single ``runspec:transfer_end`` whose ``hosts`` lists each host's result —
        or ``{"error": ...}`` on a bad request.
        """

        from .fs_transfer import plan_transfer, render_pipeline

        paths = [p for p in (source_paths or []) if p and p.strip()]
        targets = [h for h in (dest_hosts or []) if h and h != "local"]
        if not paths:
            return {"error": "no source files selected"}
        if not targets:
            return {"error": "no target hosts selected"}
        if not dest_dir or not dest_dir.strip():
            return {"error": "no destination folder selected"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            if shutil.which("sh") is None:
                return {"error": "filtering an upload needs a POSIX shell (sh) on PATH"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        entries: dict[str, dict[str, Any]] = {}
        for h in targets:
            entry = self._host_entry(h)
            if entry is None or not entry.get("ssh"):
                return {"error": f"{h} is not a reachable remote host"}
            entries[h] = entry

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": f"fan-out · {len(targets)} hosts",
                "group": "",
                "host": ", ".join(targets),
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {"hosts": len(targets), "files": len(paths)},
                _CANCEL_KEY: cancel_event,
            }

        cfg = self.get_config().get("ssh")
        # The list each host actually uploads — the raw sources, or (when a filter
        # is set) the locally pre-filtered temp copies, swapped in by run().
        send_paths = list(paths)

        def per_host(h: str) -> dict[str, Any]:
            entry = entries[h]
            ok, error = True, None
            release: Callable[[], None] | None = None
            try:
                sftp, release = self._open_sftp(
                    entry["ssh"], entry.get("identityFile"), cfg
                )
                plan = plan_transfer("local", send_paths, h, dest_dir)
                count = len(plan.items)
                for idx, item in enumerate(plan.items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        self._transfer_one(sftp, "push", item, tid, idx, count, host=h)
                    except Exception as e:
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "host": h,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                if release:
                    release()
            return {"host": h, "ok": ok, "error": error}

        def run() -> None:
            import shutil as _shutil
            import tempfile
            import types
            from concurrent.futures import ThreadPoolExecutor

            results: list[dict[str, Any]] = []
            tmpdir: str | None = None
            try:
                if rendered is not None:
                    # Filter each source once, locally, into a temp dir keyed by the
                    # original basename (so the dest filename is unchanged), then fan
                    # out the small filtered copies instead of the originals.
                    tmpdir = tempfile.mkdtemp(prefix="runspec-filter-")
                    temp_paths: list[str] = []
                    for i, p in enumerate(paths):
                        if cancel_event.is_set():
                            raise RuntimeError("cancelled")
                        base = os.path.basename(p)
                        tmp = os.path.join(tmpdir, base)
                        item = types.SimpleNamespace(name=base, src=p, dst=tmp)
                        self._run_filter_stream_local(
                            rendered,
                            p,
                            dst_open=lambda tmp=tmp: open(tmp, "wb"),
                            dst_remove=lambda tmp=tmp: os.remove(tmp),
                            in_size=os.path.getsize(p) if os.path.exists(p) else 0,
                            item=item,
                            tid=tid,
                            idx=i,
                            count=len(paths),
                            cancel_event=cancel_event,
                        )
                        temp_paths.append(tmp)
                    send_paths[:] = temp_paths
                with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
                    results = list(ex.map(per_host, targets))
            except Exception as e:
                # A failed/cancelled pre-filter means nothing was fanned out.
                results = [{"host": h, "ok": False, "error": str(e)} for h in targets]
            finally:
                if tmpdir is not None:
                    _shutil.rmtree(tmpdir, ignore_errors=True)
                with self._lock:
                    self._in_flight.pop(tid, None)
            overall = bool(results) and all(r["ok"] for r in results)
            logger.info("fan-out %s: %d hosts ok=%s", tid, len(targets), overall)
            self._emit_transfer_end(
                tid,
                {
                    "id": tid,
                    "ok": overall,
                    "files": len(paths),
                    "total": len(paths),
                    "error": None if overall else "one or more hosts failed",
                    "hosts": results,
                },
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def check_dir_exists(
        self, hosts: list[str], path: str
    ) -> dict[str, dict[str, Any]]:
        """Pre-flight: does directory ``path`` exist on each of ``hosts``?

        Used by the fan-out dialog to flag hosts whose layout differs *before* a
        push (the destination is only browsed on the first host). Returns
        ``{host: {"exists": bool, "error": str | None}}``; a connection failure
        is reported as ``exists=False`` with an ``error``.
        """
        import shlex

        result: dict[str, dict[str, Any]] = {}
        if not path or not path.strip():
            return result
        for h in hosts:
            if not h:
                continue
            if h == "local":
                result[h] = {
                    "exists": os.path.isdir(os.path.expanduser(path)),
                    "error": None,
                }
                continue
            entry = self._host_entry(h)
            if entry is None or not entry.get("ssh"):
                result[h] = {"exists": False, "error": "not a reachable remote host"}
                continue
            code, _out, err = self._ssh_run(
                entry["ssh"],
                f"test -d {shlex.quote(path)}",
                identity_file=entry.get("identityFile"),
                timeout=10,
            )
            if code == 0:
                result[h] = {"exists": True, "error": None}
            elif code == 1:
                result[h] = {"exists": False, "error": None}  # test -d: not a directory
            else:
                result[h] = {
                    "exists": False,
                    "error": err.strip() or f"check failed (exit {code})",
                }
        return result

    def _open_source_conn(
        self, source: dict[str, Any], password: str | None, cfg: dict[str, Any] | None
    ) -> tuple[Any, Callable[[], tuple[Any, Callable[[], None]]], Callable[[], None]]:
        """Open a relay source — a configured host, or an ad-hoc host reached via
        a configured jump (key/agent first, then ``password``).

        Returns ``(sftp, session_factory, release)``:

        * ``sftp`` — for ``stat()`` and the unfiltered straight-through path.
        * ``session_factory`` — ``() -> (channel, chan_release)``; opens an exec
          channel on the *same* transport, for the source-side filter path. The
          caller drives ``channel`` and must call ``chan_release()`` when done
          (on the pooled path it frees a session slot; the transport stays warm).
        * ``release`` — tears down ``sftp`` and the transport/tunnel.
        """
        if source.get("kind") == "host":
            entry = self._host_entry(source["host"])
            if entry is None or not entry.get("ssh"):
                raise OSError(f"{source.get('host')} is not a reachable remote host")
            ssh_target = entry["ssh"]
            identity = entry.get("identityFile")
            sftp, sftp_release = self._open_sftp(ssh_target, identity, cfg)

            def session_factory() -> tuple[Any, Callable[[], None]]:
                pool = self._ssh_pool
                if pool is not None:
                    return pool.open_session(
                        ssh_target, identity_file=identity, global_ssh_config=cfg
                    )
                from .executor import _make_ssh_client

                client = _make_ssh_client(ssh_target, identity, cfg)
                transport = client.get_transport()
                if transport is None:
                    client.close()
                    raise OSError("source transport unavailable")
                return transport.open_session(), client.close

            return sftp, session_factory, sftp_release

        # ad-hoc: tunnel a direct-tcpip channel through the configured jump (warm
        # pooled transport), then SSH to the target over it (keys/agent, then password).
        import paramiko

        from .executor import _apply_host_key_policy

        jump_entry = self._host_entry(source["jump"])
        if jump_entry is None or not jump_entry.get("ssh"):
            raise OSError(f"jump host {source.get('jump')} is not reachable")
        target = str(source["host"])
        port = int(source.get("port") or 22)

        chan, chan_release, jump_client = self._jump_channel(
            jump_entry, target, port, cfg
        )

        tclient = paramiko.SSHClient()
        _apply_host_key_policy(tclient, cfg or {})
        try:
            tclient.connect(
                hostname=target,
                port=port,
                username=source.get("user") or None,
                password=password or None,
                sock=chan,
                allow_agent=True,
                look_for_keys=True,
                timeout=float((cfg or {}).get("connect_timeout", 10)),
            )
        except Exception:
            chan_release()
            if jump_client is not None:
                jump_client.close()
            raise
        sftp = tclient.open_sftp()

        def adhoc_session_factory() -> tuple[Any, Callable[[], None]]:
            transport = tclient.get_transport()
            if transport is None:
                raise OSError("source transport closed")
            # The tunneled client has no per-channel slot to free (that's a pool
            # concept), so the release is a no-op — the channel is closed by the
            # filtered loop and the client by ``release`` below.
            return transport.open_session(), (lambda: None)

        def release() -> None:
            for c in (sftp, tclient):
                try:
                    c.close()
                except Exception:
                    pass
            chan_release()
            if jump_client is not None:
                try:
                    jump_client.close()
                except Exception:
                    pass

        return sftp, adhoc_session_factory, release

    def _jump_channel(
        self,
        jump_entry: dict[str, Any],
        target: str,
        port: int,
        cfg: dict[str, Any] | None,
    ) -> tuple[Any, Callable[[], None], Any]:
        """Open a direct-tcpip channel to ``target:port`` through the jump.

        Uses the pooled (warm) jump transport when pooling is on — returns
        ``(channel, release, None)``; otherwise opens a fresh jump client and
        returns it as the third element for the caller to close.
        """
        pool = self._ssh_pool
        if pool is not None:
            chan, release = pool.open_direct_tcpip(
                jump_entry["ssh"],
                target,
                port,
                identity_file=jump_entry.get("identityFile"),
                global_ssh_config=cfg,
            )
            return chan, release, None

        from .executor import _make_ssh_client

        jump_client = _make_ssh_client(
            jump_entry["ssh"], jump_entry.get("identityFile"), cfg
        )
        transport = jump_client.get_transport()
        if transport is None:
            jump_client.close()
            raise OSError("jump transport unavailable")
        chan = transport.open_channel("direct-tcpip", (target, port), ("127.0.0.1", 0))
        return chan, (lambda: None), jump_client

    def _relay_one(
        self,
        src_sftp: Any,
        dest_sftp: Any,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Stream one file source→dest through the console, in chunks."""
        size = int(src_sftp.stat(item.src).st_size)
        transferred = 0
        last = 0.0
        with (
            src_sftp.open(item.src, "rb") as fsrc,
            dest_sftp.open(item.dst, "wb") as fdst,
        ):
            try:
                fsrc.prefetch(size)
            except Exception:
                pass
            while True:
                if cancel_event.is_set():
                    raise RuntimeError("cancelled")
                chunk = fsrc.read(32768)
                if not chunk:
                    break
                fdst.write(chunk)
                transferred += len(chunk)
                now = time.monotonic()
                if now - last >= 0.15 or transferred >= size:
                    last = now
                    self._dispatch(
                        "runspec:transfer_progress",
                        {
                            "id": tid,
                            "file": item.name,
                            "index": idx,
                            "count": count,
                            "transferred": transferred,
                            "total": size,
                        },
                    )

    def _run_filter_stream(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        cmd: str,
        dst_open: Callable[[], Any],
        dst_remove: Callable[[], None],
        in_size: int,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Run ``cmd`` on the source over an exec channel, streaming its stdout
        into the destination ``dst_open()`` writes to. Shared by the relay and
        download filter paths — only the destination sink differs.

        Output size is unknown up front, so progress is reported as bytes written
        with ``indeterminate: True`` (``total`` carries the input size, for
        context only). A pipeline that exits non-zero raises — except exit code 1
        with no stderr, which is grep's "no lines matched" and yields a legitimate
        empty result. On any failure or cancel the partial destination is removed.
        """
        channel, chan_release = session_factory()
        STDERR_CAP = 65536
        try:
            channel.set_combine_stderr(False)
            channel.exec_command(cmd)
            transferred = 0
            last = 0.0
            stderr_buf = b""

            def tick() -> None:
                self._dispatch(
                    "runspec:transfer_progress",
                    {
                        "id": tid,
                        "file": item.name,
                        "index": idx,
                        "count": count,
                        "transferred": transferred,
                        "total": in_size,
                        "indeterminate": True,
                    },
                )

            f = dst_open()
            try:
                while True:
                    if cancel_event.is_set():
                        raise RuntimeError("cancelled")
                    got = False
                    if channel.recv_ready():
                        chunk = channel.recv(32768)
                        if chunk:
                            f.write(chunk)
                            transferred += len(chunk)
                            got = True
                    if channel.recv_stderr_ready():
                        err = channel.recv_stderr(32768)
                        if err and len(stderr_buf) < STDERR_CAP:
                            stderr_buf += err[: STDERR_CAP - len(stderr_buf)]
                        got = got or bool(err)
                    now = time.monotonic()
                    if got and now - last >= 0.15:
                        last = now
                        tick()
                    if channel.exit_status_ready():
                        while True:  # drain any buffered output after exit
                            chunk = channel.recv(32768)
                            if not chunk:
                                break
                            f.write(chunk)
                            transferred += len(chunk)
                        while True:
                            err = channel.recv_stderr(32768)
                            if not err:
                                break
                            if len(stderr_buf) < STDERR_CAP:
                                stderr_buf += err[: STDERR_CAP - len(stderr_buf)]
                        break
                    if not got:
                        time.sleep(0.01)
            finally:
                f.close()

            code = channel.recv_exit_status()
            stderr_text = stderr_buf.decode("utf-8", errors="replace").strip()
            # grep returns 1 for "no lines matched" — a legitimately empty result,
            # not a failure. Distinguish it from a real error by the absence of any
            # diagnostic on stderr.
            if code != 0 and not (code == 1 and not stderr_text):
                raise RuntimeError(stderr_text or f"filter exited {code}")
            tick()  # final count
        except BaseException:
            try:
                dst_remove()
            except Exception:
                pass
            raise
        finally:
            try:
                channel.close()
            except Exception:
                pass
            chan_release()

    def _run_filter_stream_local(
        self,
        rendered_pipeline: str,
        src_path: str,
        dst_open: Callable[[], Any],
        dst_remove: Callable[[], None],
        in_size: int,
        item: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Filter a *local* file through ``rendered_pipeline`` (run via ``sh -c``
        with the file on stdin) and stream its stdout into the destination
        ``dst_open()`` writes to. The local-source counterpart of
        ``_run_filter_stream`` — used for an upload (push), where the source host
        is this machine. Same exit-code / cleanup / indeterminate-progress
        semantics. Requires a POSIX shell on PATH (raises otherwise)."""
        import subprocess

        if shutil.which("sh") is None:
            raise RuntimeError("filtering on upload needs a POSIX shell (sh) on PATH")

        transferred = 0
        last = 0.0

        def tick() -> None:
            self._dispatch(
                "runspec:transfer_progress",
                {
                    "id": tid,
                    "file": item.name,
                    "index": idx,
                    "count": count,
                    "transferred": transferred,
                    "total": in_size,
                    "indeterminate": True,
                },
            )

        src = open(src_path, "rb")
        try:
            proc = subprocess.Popen(
                ["sh", "-c", rendered_pipeline],
                stdin=src,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            src.close()  # the child holds its own copy of the fd

        # Drain stderr on a side thread so a chatty pipeline can't deadlock by
        # filling the stderr pipe while we're busy reading stdout.
        stderr_chunks: list[bytes] = []

        def drain_err() -> None:
            assert proc.stderr is not None
            for block in iter(lambda: proc.stderr.read(32768), b""):
                if sum(len(c) for c in stderr_chunks) < 65536:
                    stderr_chunks.append(block)

        err_thread = threading.Thread(target=drain_err, daemon=True)
        err_thread.start()

        try:
            assert proc.stdout is not None
            f = dst_open()
            try:
                while True:
                    if cancel_event.is_set():
                        proc.kill()
                        raise RuntimeError("cancelled")
                    chunk = proc.stdout.read(32768)
                    if not chunk:
                        break
                    f.write(chunk)
                    transferred += len(chunk)
                    now = time.monotonic()
                    if now - last >= 0.15:
                        last = now
                        tick()
            finally:
                f.close()

            code = proc.wait()
            err_thread.join(timeout=5)
            stderr_text = (
                b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            )
            if code != 0 and not (code == 1 and not stderr_text):
                raise RuntimeError(stderr_text or f"filter exited {code}")
            tick()
        except BaseException:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                dst_remove()
            except Exception:
                pass
            raise
        finally:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass

    def _relay_one_filtered(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        dest_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        src_sftp: Any,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Relay one file through a source-side filter, writing the filtered
        output to the destination host over SFTP."""
        from .fs_transfer import build_filter_command

        try:
            in_size = int(src_sftp.stat(item.src).st_size)
        except Exception:
            in_size = 0
        cmd = build_filter_command(rendered_pipeline, item.src)
        self._run_filter_stream(
            session_factory,
            cmd,
            dst_open=lambda: dest_sftp.open(item.dst, "wb"),
            dst_remove=lambda: dest_sftp.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def _transfer_one_filtered(
        self,
        session_factory: Callable[[], tuple[Any, Callable[[], None]]],
        src_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Download one file through a source-side filter, writing the filtered
        output to the local destination file."""
        from .fs_transfer import build_filter_command

        try:
            in_size = int(src_sftp.stat(item.src).st_size)
        except Exception:
            in_size = 0
        cmd = build_filter_command(rendered_pipeline, item.src)
        self._run_filter_stream(
            session_factory,
            cmd,
            dst_open=lambda: open(item.dst, "wb"),
            dst_remove=lambda: os.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def _transfer_one_filtered_local(
        self,
        dest_sftp: Any,
        item: Any,
        rendered_pipeline: str,
        tid: str,
        idx: int,
        count: int,
        cancel_event: threading.Event,
    ) -> None:
        """Upload one file through a local filter: run the pipeline on this
        machine over the local source and write the filtered output to the remote
        destination over SFTP."""
        try:
            in_size = os.path.getsize(item.src)
        except OSError:
            in_size = 0
        self._run_filter_stream_local(
            rendered_pipeline,
            item.src,
            dst_open=lambda: dest_sftp.open(item.dst, "wb"),
            dst_remove=lambda: dest_sftp.remove(item.dst),
            in_size=in_size,
            item=item,
            tid=tid,
            idx=idx,
            count=count,
            cancel_event=cancel_event,
        )

    def start_relay(
        self,
        source: dict[str, Any],
        source_paths: list[str],
        dest_host: str,
        dest_dir: str,
        filter_name: str = "",
        filter_arg: str | None = None,
    ) -> dict[str, Any]:
        """Relay file(s) from one host to another, streaming through the console.

        ``source`` is ``{"kind": "host", "host": "<configured>"}`` or
        ``{"kind": "adhoc", "host", "port"?, "user"?, "jump": "<configured>",
        "password"?}``. A password (when given) connects to the ad-hoc target only
        and is **never** stored, logged, or pooled. ``filter_name`` selects a saved
        source-side filter (``filter_arg`` is the runtime value for its ``{{arg}}``
        placeholder); when set, the file is filtered on the source host and only
        the output is relayed. Returns ``{"id": ...}`` and streams the usual
        transfer events, or ``{"error": ...}``.
        """
        from .fs_transfer import relay_items, render_pipeline

        try:
            items = relay_items(source_paths, dest_dir)
        except ValueError as e:
            return {"error": str(e)}

        dest_entry = self._host_entry(dest_host)
        if dest_entry is None or not dest_entry.get("ssh"):
            return {"error": f"{dest_host} is not a reachable remote host"}

        kind = source.get("kind")
        if kind not in ("host", "adhoc") or not source.get("host"):
            return {"error": "no source host specified"}
        if kind == "adhoc" and not source.get("jump"):
            return {"error": "no jump host selected"}

        rendered = None
        if filter_name:
            filt = self._filter_entry(filter_name)
            if filt is None:
                return {"error": f"unknown filter: {filter_name}"}
            try:
                rendered = render_pipeline(filt.get("pipeline", ""), filter_arg)
            except ValueError as e:
                return {"error": str(e)}

        # Pull the password / credential id out so neither lands in _in_flight /
        # logs. A credential_id (ad-hoc source) resolves to the stored secret —
        # and its username when none was given — at connect time.
        password = source.pop("password", None)
        credential_id = source.pop("credential_id", None)
        resolved_user, password = self._resolve_adhoc_credential(
            credential_id, source.get("user"), password
        )
        if resolved_user:
            source["user"] = resolved_user
        cfg = self.get_config().get("ssh")

        tid = uuid.uuid4().hex[:12]
        cancel_event = threading.Event()
        with self._lock:
            self._in_flight[tid] = {
                "id": tid,
                "runnable": "relay",
                "group": "",
                "host": f"{source.get('host')} → {dest_host}",
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": {
                    "source": source.get("host"),
                    "via": source.get("jump"),
                    "dest": dest_host,
                    "files": len(items),
                    # The filter name is recorded for the activity panel; the
                    # rendered pipeline / runtime value are not (avoids logging
                    # operator identifiers).
                    **({"filter": filter_name} if filter_name else {}),
                },
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            ok, error, done = True, None, 0
            src_release: Callable[[], None] | None = None
            dest_release: Callable[[], None] | None = None
            count = len(items)
            try:
                src_sftp, session_factory, src_release = self._open_source_conn(
                    source, password, cfg
                )
                dest_sftp, dest_release = self._open_sftp(
                    dest_entry["ssh"], dest_entry.get("identityFile"), cfg
                )
                for idx, item in enumerate(items):
                    if cancel_event.is_set():
                        ok, error = False, "cancelled"
                        break
                    try:
                        if rendered is not None:
                            self._relay_one_filtered(
                                session_factory,
                                dest_sftp,
                                item,
                                rendered,
                                src_sftp,
                                tid,
                                idx,
                                count,
                                cancel_event,
                            )
                        else:
                            self._relay_one(
                                src_sftp, dest_sftp, item, tid, idx, count, cancel_event
                            )
                        done += 1
                    except Exception as e:
                        ok, error = False, str(e)
                        self._dispatch(
                            "runspec:transfer_progress",
                            {
                                "id": tid,
                                "file": item.name,
                                "index": idx,
                                "count": count,
                                "transferred": 0,
                                "total": 0,
                                "error": str(e),
                            },
                        )
            except Exception as e:
                ok, error = False, str(e)
            finally:
                for r in (src_release, dest_release):
                    if r:
                        r()
                with self._lock:
                    self._in_flight.pop(tid, None)
            logger.info("relay %s: %d/%d ok=%s", tid, done, count, ok)
            self._emit_transfer_end(
                tid,
                {"id": tid, "ok": ok, "files": done, "total": count, "error": error},
            )

        threading.Thread(target=run, daemon=True).start()
        return {"id": tid}

    def list_dir_adhoc(
        self,
        jump: str,
        host: str,
        path: str = "",
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        dirs_only: bool = False,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        """List a directory on an ad-hoc host (reached via ``jump``) for the relay
        source browser. Same return shape as :meth:`list_dir`.

        The target connection is cached for the browse session (keyed by jump /
        host / user / port) so navigation on a slow link only handshakes the
        target once. The password (or a ``credential_id``'s stored secret) is used
        only when (re)establishing and is never stored or logged."""
        if not host:
            return {"error": "no source host specified"}
        if not jump:
            return {"error": "no jump host selected"}
        user, password = self._resolve_adhoc_credential(credential_id, user, password)
        key = (jump, host, user or "", int(port or 22))
        cfg = self.get_config().get("ssh")
        for attempt in (1, 2):
            try:
                sftp = self._get_adhoc_sftp(key, jump, host, user, password, port, cfg)
            except Exception as e:
                return {"error": str(e)}
            try:
                from .fs_browse import list_sftp

                return list_sftp(sftp, path, dirs_only=dirs_only)
            except Exception as e:
                # A reused connection may have dropped — drop it and retry once.
                self._close_adhoc_key(key)
                if attempt == 2:
                    return {"error": str(e)}
        return {"error": "unreachable"}

    def _get_adhoc_sftp(
        self,
        key: tuple[Any, ...],
        jump: str,
        host: str,
        user: str | None,
        password: str | None,
        port: int | None,
        cfg: dict[str, Any] | None,
    ) -> Any:
        """Return a live cached ad-hoc SFTP for ``key``, establishing one if needed."""
        with self._lock:
            entry = self._adhoc_conns.get(key)
        if entry is not None and self._adhoc_alive(entry["sftp"]):
            entry["last_used"] = time.monotonic()
            return entry["sftp"]

        source = {
            "kind": "adhoc",
            "host": host,
            "jump": jump,
            "user": user,
            "port": port,
        }
        # Browsing only needs the sftp; the exec session_factory is for filters.
        sftp, _session_factory, release = self._open_source_conn(source, password, cfg)
        with self._lock:
            old = self._adhoc_conns.get(key)
            self._adhoc_conns[key] = {
                "sftp": sftp,
                "release": release,
                "last_used": time.monotonic(),
            }
        if old is not None:
            try:
                old["release"]()
            except Exception:
                pass
        return sftp

    @staticmethod
    def _adhoc_alive(sftp: Any) -> bool:
        try:
            chan = sftp.get_channel()
            transport = chan.get_transport() if chan is not None else None
            return bool(transport is not None and transport.is_active())
        except Exception:
            return False

    def _close_adhoc_key(self, key: tuple[Any, ...]) -> None:
        with self._lock:
            entry = self._adhoc_conns.pop(key, None)
        if entry is not None:
            try:
                entry["release"]()
            except Exception:
                pass

    def close_adhoc(self) -> None:
        """Tear down all cached ad-hoc browse connections (relay dialog close / shutdown)."""
        with self._lock:
            entries = list(self._adhoc_conns.values())
            self._adhoc_conns.clear()
        for e in entries:
            try:
                e["release"]()
            except Exception:
                pass

    def list_dir_unsaved(
        self,
        hostname: str,
        path: str = "",
        user: str | None = None,
        port: int | None = None,
        identity_file: str | None = None,
        dirs_only: bool = False,
    ) -> dict[str, Any]:
        """List a directory on a host that isn't saved yet — the Settings → Jump
        Hosts add/edit form — so the runspec-path picker can browse it directly.

        Same return shape as :meth:`list_dir`. Connects **straight** to the host
        (no jump) from the form's ``hostname``/``user``/``port``/``identity_file``,
        reusing the shared client builder so the browse goes through the same
        key/agent → ``[ssh] proxy`` → ``~/.ssh/config`` → host-key handling as
        every saved host. The connection is cached for the browse session (keyed
        by the target) and torn down by :meth:`close_adhoc` when the form closes.
        """
        if not hostname:
            return {"error": "no hostname specified"}
        target = f"{user}@{hostname}" if user else hostname
        if port:
            target = f"{target}:{int(port)}"
        key = ("__unsaved__", target, identity_file or "")
        cfg = self.get_config().get("ssh")
        for attempt in (1, 2):
            try:
                sftp = self._get_unsaved_sftp(key, target, identity_file, cfg)
            except Exception as e:
                return {"error": str(e)}
            try:
                from .fs_browse import list_sftp

                return list_sftp(sftp, path, dirs_only=dirs_only)
            except Exception as e:
                # A reused connection may have dropped — drop it and retry once.
                self._close_adhoc_key(key)
                if attempt == 2:
                    return {"error": str(e)}
        return {"error": "unreachable"}

    def _get_unsaved_sftp(
        self,
        key: tuple[Any, ...],
        target: str,
        identity_file: str | None,
        cfg: dict[str, Any] | None,
    ) -> Any:
        """Return a live cached direct SFTP for an unsaved host, opening one if needed.

        Owns a dedicated (non-pooled) client — the unsaved host has no pool entry
        yet — cached alongside the relay ad-hoc connections so ``close_adhoc``
        cleans it up too."""
        with self._lock:
            entry = self._adhoc_conns.get(key)
        if entry is not None and self._adhoc_alive(entry["sftp"]):
            entry["last_used"] = time.monotonic()
            return entry["sftp"]

        from .executor import _make_ssh_client

        client = _make_ssh_client(target, identity_file, cfg)
        sftp = client.open_sftp()

        def release() -> None:
            for c in (sftp, client):
                try:
                    c.close()
                except Exception:
                    pass

        with self._lock:
            old = self._adhoc_conns.get(key)
            self._adhoc_conns[key] = {
                "sftp": sftp,
                "release": release,
                "last_used": time.monotonic(),
            }
        if old is not None:
            try:
                old["release"]()
            except Exception:
                pass
        return sftp

    # ── history ───────────────────────────────────────────────────────────────
