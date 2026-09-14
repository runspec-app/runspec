"""bridge_evals.py — run tool-selection evals against the *live configured agent*.

The Evals feature is the proactive twin of Improve: instead of mining what the
agent already did (``bridge_improve``), it drives the model over a fixed suite of
prompts and checks which runnable it *would* pick, with which args — **without
executing anything** — then scores that against a declared expectation. It tests
the exact surface this console presents: the real toolset (``_runnables_to_tools``
in the operator's ``tool_mode``, honouring ``tool_visibility``), the configured
adapter + system prompt (``_get_adapter``), and — opt-in — the same preflight the
live loop uses (``_preflight_validate``), so a "would this bounce?" failure buckets
the same way an Improve incident does.

The analysis is the pure, unit-tested harness in :mod:`runspec_console.evals`; this
mixin does the I/O the way ``bridge_improve`` does — supply the console-specific
injected callables, run the suite (via ``asyncio.run``, the same pattern
``run_improve_report`` uses), emit one OTLP trace per case
(:mod:`runspec_console.evals_trace` — a no-op when telemetry is off), persist a
dated machine-local report, and expose fixture CRUD for the tab.

Tool identity across ``tool_mode`` (why the injected callables exist):
  * ``dedup``    tool = the runnable ``name`` (+ a ``host`` enum arg) → target =
                 last ``__`` segment (a no-op, but it also normalises a legacy
                 group-qualified id); args = tool input minus ``host``.
  * ``per_host`` tool = ``host__name``                          → same.
  * ``search``   the model calls ``run_runnable`` / ``use_tool`` → target/args
                 live in the tool input (``name`` / ``args``); the read-only
                 discovery tools (``search_runnables`` / ``describe_runnable`` /
                 ``describe_tool``) are run for real so the trajectory is faithful.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import Any

from . import config as config_mod
from . import evals, evals_trace

logger = logging.getLogger(__name__)

# Read-only, local, side-effect-free discovery tools that a search-mode trajectory
# needs real results from (so the model can actually find the runnable). Everything
# else read-only is stubbed — the eval only cares about the terminal runnable choice.
_EVAL_DISCOVERY_TOOLS = frozenset(
    {"search_runnables", "describe_runnable", "describe_tool"}
)


class EvalsMixin:
    # ── injected callables that map the harness onto this console's tool naming ──
    def _eval_injections(self) -> dict[str, Any]:
        mode = self._tool_mode()  # type: ignore[attr-defined]
        # Bare runnable names (sanitised) — a fallback terminal check for a direct
        # tool whose name carries no "__" (an ungrouped runnable in some layouts).
        runnable_names: set[str] = set()
        try:
            for r in self._visible_runnables("agent"):  # type: ignore[attr-defined]
                nm = r.get("name")
                if nm:
                    runnable_names.add(self._sanitize_tool_name(nm))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best-effort; "__"/meta checks still apply
            logger.debug("eval: could not enumerate runnable names", exc_info=True)

        def is_terminal(name: str) -> bool:
            return (
                name in ("run_runnable", "use_tool")
                or "__" in name
                or name in runnable_names
            )

        def resolve_target(inv: evals.ToolInvocation) -> str:
            # search mode: run_runnable carries the runnable in `id` (its schema's
            # required key — bridge_search._search_mode_tools), use_tool the inner
            # tool in `name`. Reading the wrong key made every search-mode case
            # resolve to the wrapper name "run_runnable".
            if inv.name == "run_runnable":
                # The `id` is a search_runnables row id — the dedup tool name,
                # which is now the bare runnable name. A legacy `group__`/`host__`
                # qualifier (e.g. `local__filesystem`) still resolves and can still
                # be spelled in a saved fixture, so keep stripping it, exactly as
                # the direct-tool branch below does (per_host ids stay qualified),
                # so `expect_tool` (always the bare name) matches.
                raw = str(
                    inv.input.get("id")
                    or inv.input.get("runnable")
                    or inv.input.get("name")
                    or inv.name
                )
                return raw.rsplit("__", 1)[-1]
            if inv.name == "use_tool":
                return str(inv.input.get("name") or inv.input.get("tool") or inv.name)
            return inv.name.rsplit("__", 1)[-1]

        def resolve_command(inv: evals.ToolInvocation) -> str:
            # The chosen subcommand rides a top-level `command` field in EVERY
            # mode (a sibling of `args` on run_runnable; a top-level key on a
            # direct dedup/per_host tool) — never inside `args`. String enum for a
            # flat runnable, array for a nested path; space-join the latter so a
            # case can assert e.g. "remote add".
            return evals._command_str(inv.input.get("command"))

        # Cached runnable records by bare name — the dedup host selector's key is
        # derived from the runnable's own declared args, so resolving a host (or
        # dropping the selector from the graded args) needs the spec, not a guess.
        by_name_rec: dict[str, dict[str, Any]] = {}
        try:
            for r in self._visible_runnables("agent"):  # type: ignore[attr-defined]
                nm = r.get("name")
                if nm and nm not in by_name_rec:
                    by_name_rec[nm] = r
        except Exception:  # noqa: BLE001 — falls back to the plain "host" key
            logger.debug("eval: could not cache runnable specs", exc_info=True)

        def _host_key_for(inv: evals.ToolInvocation) -> str:
            """The input key carrying the *fleet host* for a direct tool call.

            Normally ``host`` — but `_host_selector_key` renames the injected
            selector to ``_host``/``__host`` when the runnable declares its OWN
            ``host`` arg (cert-check, dns-lookup, ping-host, … all do), so reading
            ``host`` there would read the runnable's target, not the fleet host."""
            try:
                return str(
                    self._host_selector_key(by_name_rec.get(resolve_target(inv)))  # type: ignore[attr-defined]
                )
            except Exception:  # noqa: BLE001 — fall back to the portable default
                return "host"

        def resolve_host(inv: evals.ToolInvocation) -> str:
            # search mode: run_runnable/use_tool carry a top-level `host`, which
            # may also be a host GROUP name (its schema says so) — graded as-is.
            if inv.name in ("run_runnable", "use_tool"):
                return str(inv.input.get("host") or "")
            # per_host mode: there IS no host in the input (_build_input_schema
            # omits the selector) — the host is the tool-name prefix `host__name`.
            if mode == "per_host":
                return inv.name.split("__", 1)[0] if "__" in inv.name else ""
            # dedup mode: the injected selector, under its real (possibly renamed) key.
            return str(inv.input.get(_host_key_for(inv)) or "")

        def resolve_args(inv: evals.ToolInvocation) -> dict[str, Any]:
            if inv.name in ("run_runnable", "use_tool"):
                args = inv.input.get("args")
                return dict(args) if isinstance(args, dict) else {}
            # Direct tool: the runnable's own args are top-level; the injected host
            # selector and `command` are scored separately — drop both so a subset
            # / preflight check doesn't treat them as unknown args. The selector is
            # dropped under its REAL key: a runnable declaring its own `host` arg
            # gets the selector renamed to `_host`, and dropping a literal "host"
            # there would both leak `_host` (preflight: "unknown argument(s)") and
            # wrongly discard the runnable's own `host` argument from the grading.
            drop = {"command"}
            if mode == "per_host":
                drop.add("host")  # defensive: no selector is injected in per_host
            else:
                drop.add(_host_key_for(inv))
            return {k: v for k, v in inv.input.items() if k not in drop}

        def resolve_autonomy(inv: evals.ToolInvocation) -> str:
            # Prove — without executing — the level this exact call would gate at,
            # via the SAME resolver the live gate uses. `_effective_tool_autonomy`
            # already reads the chosen subcommand leaf's own autonomy and escalates
            # by any per-arg autonomy (most restrictive wins), so a `confirm` here
            # means the operator prompt WOULD be raised and a `manual` means the
            # agent WOULD be refused. Fail-safe: any hiccup → "confirm" (the gate's
            # own unknown-call default), never a crash.
            try:
                return str(
                    self._effective_tool_autonomy(inv.name, dict(inv.input))  # type: ignore[attr-defined]
                )
            except Exception:  # noqa: BLE001
                return "confirm"

        def resolve_readonly(name: str, tool_input: dict[str, Any]) -> str:
            if name in _EVAL_DISCOVERY_TOOLS:
                try:
                    return self._run_tool_sync(name, dict(tool_input))  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    return "(no results)"
            return "(eval: read-only tool not executed)"

        return {
            "is_terminal": is_terminal,
            "resolve_readonly": resolve_readonly,
            "resolve_target": resolve_target,
            "resolve_command": resolve_command,
            "resolve_args": resolve_args,
            "resolve_autonomy": resolve_autonomy,
            "resolve_host": resolve_host,
            "mode": mode,
        }

    def _eval_validator(self):
        """A ``validate(runnable, args) -> str|None`` bound to this console's
        ``_preflight_validate`` — the same structural check the live loop runs, so
        a would-be arg *bounce* is caught proactively. Best-effort: an unknown
        runnable / spec shape returns None (skip), never raises."""
        try:
            runnables = self._visible_runnables("agent")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            runnables = []
        by_name: dict[str, dict[str, Any]] = {}
        for r in runnables:
            nm = r.get("name")
            if nm and nm not in by_name:
                by_name[nm] = r

        def validate(runnable: str, command: str, args: dict[str, Any]) -> str | None:
            cached = by_name.get(runnable)
            if not cached:
                return None  # unknown here — let it pass, nothing to check against
            # The chosen subcommand rides `command` (space-joined for a nested
            # path); split it back to the path list `_preflight_validate` walks,
            # so a require-command runnable and a subcommand's own args validate
            # against the invoked leaf — passing [] here bounced every subcommand
            # runnable with "requires a command".
            command_path = command.split() if command else []
            try:
                return self._preflight_validate(cached, command_path, dict(args))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — preflight is best-effort in evals
                return None

        return validate

    # ── the run (JS-callable) ────────────────────────────────────────────────
    def run_evals(
        self,
        fixture_toml: str = "",
        fixture_id: str = "",
        samples: int = 1,
        validate: bool = False,
        telemetry: bool = False,
    ) -> dict[str, Any]:
        """Run a suite of tool-selection eval cases against the live agent and
        return (and persist) a scored report. Provide the suite inline as
        ``fixture_toml`` (the tab's editor) or by stored ``fixture_id``.

        ``samples`` runs each case N times (a case's ``pass_rate`` is its fraction
        of passing samples — LLMs are stochastic). ``validate`` also asserts the
        model's args wouldn't bounce (``_preflight_validate``). ``telemetry`` emits
        one ``turn → llm → tool`` OTLP trace per case with ``runspec.eval.*``
        attributes (no-op unless ``[telemetry]`` is enabled)."""
        if not getattr(self, "_ai_enabled", True):
            from .bridge_const import AI_OFF_MESSAGE

            return {"ok": False, "reason": AI_OFF_MESSAGE}
        adapter = self._get_adapter()  # type: ignore[attr-defined]
        if adapter is None:
            return {"ok": False, "reason": "no model provider configured ([llm])"}

        if fixture_toml.strip():
            import tomllib

            try:
                cases = evals.cases_from_data(tomllib.loads(fixture_toml))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"invalid fixture TOML: {exc}"}
        elif fixture_id:
            data = self.get_eval_fixture(fixture_id)
            if data is None:
                return {"ok": False, "reason": f"no such fixture: {fixture_id!r}"}
            cases = evals.cases_from_data(data)
        else:
            return {"ok": False, "reason": "provide fixture_toml or fixture_id"}
        if not cases:
            return {"ok": False, "reason": "fixture has no [[eval]] cases"}

        inj = self._eval_injections()
        mode = inj.pop("mode")
        validator = self._eval_validator() if validate else None
        tools = self._runnables_to_tools()  # type: ignore[attr-defined]
        model = str(getattr(adapter, "model", "") or "")

        tcfg: dict[str, Any] = {}
        if telemetry:
            try:
                from . import observability

                tcfg = config_mod.read_telemetry()
                observability.init_tracing(tcfg)
            except Exception:  # noqa: BLE001
                logger.debug("eval: telemetry init failed", exc_info=True)
        # Native Phoenix export: collect (tool_span_id, case, result) to annotate
        # after the traces flush. Only when telemetry + [telemetry] eval_annotations.
        want_annotations = telemetry and bool(tcfg.get("eval_annotations"))
        to_annotate: list[tuple[str, evals.EvalCase, evals.CaseResult]] = []

        async def _go() -> list[evals.CaseResult]:
            out: list[evals.CaseResult] = []
            for case in cases:
                res = await evals.evaluate_case_async(
                    adapter,
                    tools,
                    case,
                    samples=max(1, int(samples)),
                    validate=validator,
                    **inj,
                )
                if telemetry:
                    emitted = evals_trace.emit_case(case, res, model=model)
                    if want_annotations and emitted.tool_span_id:
                        to_annotate.append((emitted.tool_span_id, case, res))
                out.append(res)
            return out

        try:
            results = asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001
            logger.exception("eval run failed")
            return {"ok": False, "reason": f"eval run failed: {exc}"}
        finally:
            if telemetry:
                try:
                    from . import observability

                    # Force-flush so the just-emitted eval spans land immediately
                    # (and Phoenix has them to annotate) — but do NOT shut the
                    # provider down. The console's tracer is a single app-wide
                    # provider created once at startup and shared by chat, triggers
                    # and evals; shutting it down here silently killed tracing for
                    # the rest of the session (enabled() stayed True, so nothing
                    # re-inited and every later span was dropped by the dead batch
                    # processor). The provider's lifecycle belongs to app.py
                    # (started at boot, shut down at exit) — mirrors send_test_trace.
                    observability.flush()
                    if want_annotations and to_annotate:
                        self._export_eval_annotations(tcfg, to_annotate)
                except Exception:  # noqa: BLE001
                    pass

        report = self._build_eval_report(
            results, model=model, mode=mode, samples=samples
        )
        self._save_eval_report(report)
        return {"ok": True, **report}

    def _export_eval_annotations(
        self,
        tcfg: dict[str, Any],
        items: list[tuple[str, Any, Any]],
    ) -> None:
        """POST the eval verdicts to Phoenix as span annotations (best-effort).
        No-op without an endpoint; reuses the ``[telemetry]`` TLS/CA + api-key
        settings. LangSmith is not attemptable here (an OTLP span id doesn't map
        to a LangSmith run id) — the verdict rides the span attributes there."""
        endpoint = str(tcfg.get("endpoint") or "").strip()
        if not endpoint:
            return
        from . import eval_export

        headers: dict[str, str] = dict(tcfg.get("headers") or {})
        cmd = tcfg.get("api_key_command")
        if cmd:
            try:
                from .adapters.base import run_api_key_command

                headers[str(tcfg.get("api_key_header") or "x-api-key")] = (
                    run_api_key_command(str(cmd))
                )
            except Exception:  # noqa: BLE001
                logger.debug("eval export: api_key_command failed", exc_info=True)
        ok, detail = eval_export.post_phoenix_annotations(
            endpoint,
            items,
            headers=headers,
            ca_bundle=tcfg.get("ca_bundle"),
            tls_verify=bool(tcfg.get("tls_verify", True)),
        )
        logger.info("eval export: Phoenix annotations — %s", detail)

    def _build_eval_report(
        self,
        results: list[evals.CaseResult],
        *,
        model: str,
        mode: str,
        samples: int,
    ) -> dict[str, Any]:
        total = len(results)
        passed = sum(1 for r in results if r.pass_rate >= 1.0)
        by_category: dict[str, int] = {}
        for r in results:
            if r.pass_rate < 1.0:
                by_category[r.category] = by_category.get(r.category, 0) + 1
        cfg = self.get_config()  # type: ignore[attr-defined]
        return {
            "id": _dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "provider": str((cfg.get("llm") or {}).get("provider") or ""),
            "model": model,
            "tool_mode": mode,
            "samples": max(1, int(samples)),
            "summary": {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "pass_rate": (passed / total) if total else 1.0,
                "by_category": by_category,
            },
            "cases": [r.to_dict() for r in results],
        }

    # ── report persistence (mirrors bridge_improve) ──────────────────────────
    def _eval_report_path(self, report_id: str):
        safe = "".join(c for c in report_id if c.isalnum() or c in "-_")
        return config_mod.eval_reports_dir() / f"{safe or 'report'}.json"

    def _save_eval_report(self, report: dict[str, Any]) -> None:
        try:
            d = config_mod.eval_reports_dir()
            d.mkdir(parents=True, exist_ok=True)
            self._eval_report_path(report["id"]).write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            logger.warning("eval: could not persist report", exc_info=True)

    def list_eval_reports(self) -> list[dict[str, Any]]:
        """Saved eval reports, newest first — id + summary for the tab list."""
        out: list[dict[str, Any]] = []
        try:
            d = config_mod.eval_reports_dir()
            if not d.exists():
                return out
            for p in sorted(d.glob("*.json"), reverse=True):
                try:
                    rep = json.loads(p.read_text(encoding="utf-8"))
                    out.append(
                        {
                            "id": rep.get("id") or p.stem,
                            "generated_at": rep.get("generated_at"),
                            "model": rep.get("model"),
                            "tool_mode": rep.get("tool_mode"),
                            "summary": rep.get("summary"),
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            logger.debug("eval: list reports failed", exc_info=True)
        return out

    def load_eval_report(self, report_id: str) -> dict[str, Any] | None:
        try:
            p = self._eval_report_path(report_id)
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def delete_eval_report(self, report_id: str) -> bool:
        try:
            p = self._eval_report_path(report_id)
            if p.exists():
                p.unlink()
                return True
        except Exception:  # noqa: BLE001
            logger.debug("eval: delete report failed", exc_info=True)
        return False

    # ── fixture CRUD — one synced runspec_evals.toml of [[suite]] entries ──────
    # A "fixture id" is a suite `name`. The store rides Config Sync (merged by
    # name), so a team shares golden suites from the versioned repo while an
    # operator's own local suites survive the merge. The tab still edits each
    # suite as plain `[[eval]]` TOML — converted to/from a suite entry here.
    def _read_eval_suites(self) -> list[dict[str, Any]]:
        import tomllib

        try:
            p = config_mod.evals_path()
            if not p.exists():
                return []
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            suites = data.get("suite")
            return list(suites) if isinstance(suites, list) else []
        except Exception:  # noqa: BLE001
            logger.debug("eval: could not read suites", exc_info=True)
            return []

    def _write_eval_suites(self, suites: list[dict[str, Any]]) -> None:
        import tomli_w

        p = config_mod.evals_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tomli_w.dumps({"suite": suites}), encoding="utf-8")

    def _find_suite(self, name: str) -> dict[str, Any] | None:
        return next(
            (s for s in self._read_eval_suites() if s.get("name") == name), None
        )

    def list_eval_fixtures(self) -> list[dict[str, Any]]:
        """Stored suites — id (= suite name) + case count, for the tab picker."""
        return [
            {"id": s.get("name", ""), "cases": len(s.get("eval") or [])}
            for s in self._read_eval_suites()
            if s.get("name")
        ]

    def get_eval_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        """The suite's cases as a ``{"eval": [...]}`` dict (what ``run_evals`` /
        ``cases_from_data`` read), or ``None`` if there's no such suite."""
        suite = self._find_suite(fixture_id)
        return {"eval": list(suite.get("eval") or [])} if suite else None

    def get_eval_fixture_text(self, fixture_id: str) -> str:
        """The suite serialised back to plain ``[[eval]]`` TOML, for the editor."""
        import tomli_w

        suite = self._find_suite(fixture_id)
        if not suite:
            return ""
        try:
            return tomli_w.dumps({"eval": list(suite.get("eval") or [])})
        except Exception:  # noqa: BLE001
            return ""

    def save_eval_fixture(self, fixture_id: str, toml_text: str) -> dict[str, Any]:
        """Validate the editor's ``[[eval]]`` TOML and upsert it as a suite named
        ``fixture_id``. Returns ``{ok, reason?, cases?}``."""
        import tomllib

        name = fixture_id.strip()
        if not name:
            return {"ok": False, "reason": "fixture id is required"}
        try:
            data = tomllib.loads(toml_text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"invalid TOML: {exc}"}
        cases = data.get("eval") or []
        try:
            suites = [s for s in self._read_eval_suites() if s.get("name") != name]
            suites.append({"name": name, "eval": cases})
            suites.sort(key=lambda s: s.get("name", ""))
            self._write_eval_suites(suites)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"could not save: {exc}"}
        return {"ok": True, "cases": len(cases)}

    def delete_eval_fixture(self, fixture_id: str) -> bool:
        try:
            suites = self._read_eval_suites()
            kept = [s for s in suites if s.get("name") != fixture_id]
            if len(kept) == len(suites):
                return False
            self._write_eval_suites(kept)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("eval: delete fixture failed", exc_info=True)
            return False
