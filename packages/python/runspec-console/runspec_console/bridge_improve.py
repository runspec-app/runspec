"""
bridge_improve.py — the Bridge glue for the **Improve** feature.

Improve mines this install's own saved conversations + run history into a
structured effectiveness report (see ``improve.py`` for the pure analysis), lets
the operator optionally run an LLM-judge synthesis pass over the worst cases for
ranked "why + fix" recommendations, and hands the result off to the configured
developers via a mailto (the console never sends mail itself). One engine, two
doors: an evaluation sweep and a "report a problem" comment.

This mixin does the I/O the pure core deliberately avoids:
  * gathering the inputs (``conversations`` on disk + ``get_history_all``),
  * persisting a dated report to the machine-local store (``improve_reports_dir``),
  * running the optional synthesis via the console's own adapter (``_get_adapter``),
  * building + opening the ``mailto:`` for the developer handoff.

Reports are machine-local and never synced — a report is derived from this box's
own transcripts. Durable learnings ride out via the report itself: the developer
(or their coding agent) folds them into the Config Sync repo / seed on their terms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config as config_mod
from . import conversations as conv_mod
from . import improve as improve_mod

logger = logging.getLogger(__name__)


class ImproveMixin:
    # ── agent tool (schema + handler) ────────────────────────────────────────
    def _improve_tools(self) -> list[dict[str, Any]]:
        """Schema for the ``run_improve_report`` built-in agent tool — generate an
        effectiveness / problem report over this install's saved chats + run
        history. Read-only analysis, offered only when ``[improve] enabled``."""
        if not config_mod.read_improve().get("enabled", True):
            return []
        return [
            {
                "name": "run_improve_report",
                "description": (
                    "Generate an Improve report: analyse the console's own saved "
                    "chat conversations and run history to measure how effective "
                    "the agent, its runnables, and the app are — tool-search "
                    "behaviour, argument-validation bounces, turns that had to be "
                    "stopped and given guidance, errors, and operator denials. "
                    "Returns a JSON summary of the metrics and the worst incidents. "
                    "Pass comment to file a problem report (your description is "
                    "carried into the report for correlation against the evidence). "
                    "Read-only. The full report is saved so the operator can review "
                    "it, synthesize recommendations, and email the developers."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "window_days": {
                            "type": "integer",
                            "description": (
                                "How many days back to analyse (default from "
                                "[improve] config, usually 30)."
                            ),
                        },
                        "comment": {
                            "type": "string",
                            "description": (
                                "Optional problem description — makes this a "
                                "problem report, correlated against the data."
                            ),
                        },
                    },
                },
            }
        ]

    def _run_improve_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``run_improve_report`` for the agent → a compact JSON summary."""
        cfg = config_mod.read_improve()
        if not cfg.get("enabled", True):
            return "Error: the Improve feature is disabled ([improve] enabled = false)."
        try:
            window = int(ti.get("window_days") or cfg["window_days"])
        except (TypeError, ValueError):
            window = cfg["window_days"]
        comment = str(ti.get("comment") or "").strip()
        try:
            report = self._build_improve_report(window, comment)
        except Exception as exc:  # noqa: BLE001 — a tool must return, never raise
            logger.warning("run_improve_report failed", exc_info=True)
            return f"Error: could not build the Improve report: {exc}"
        m = report.get("metrics", {})
        s = report.get("scores", {})
        summary = {
            "report_id": report.get("id"),
            "kind": report.get("kind"),
            "window_days": window,
            "turns_analysed": m.get("turns", 0),
            "arg_bounce_rate_pct": s.get("arg_bounce_rate_pct", 0),
            "stops": m.get("stops", 0),
            "durable_errors": m.get("durable_errors", 0),
            "history_failure_rate_pct": s.get("history_failure_rate_pct", 0),
            "top_incidents": [
                {
                    "category": i.get("category"),
                    "runnable": i.get("runnable", ""),
                    "evidence": i.get("evidence", ""),
                }
                for i in (report.get("incidents") or [])[:8]
            ],
            "note": (
                "Saved. The operator can open the Improve tab to synthesize "
                "recommendations and email the developers."
            ),
        }
        return json.dumps(summary, default=str)

    # ── public JS API ────────────────────────────────────────────────────────
    def run_improve_report(
        self, window_days: int = 0, comment: str = ""
    ) -> dict[str, Any]:
        """Build + persist an Improve report, returning the full report dict.
        ``comment`` set ⇒ a problem report. ``window_days`` 0 ⇒ the config default."""
        cfg = config_mod.read_improve()
        try:
            window = int(window_days) or cfg["window_days"]
        except (TypeError, ValueError):
            window = cfg["window_days"]
        return self._build_improve_report(window, str(comment or "").strip())

    def list_improve_reports(self) -> list[dict[str, Any]]:
        """Saved reports, newest first — ``{id, ts, kind, comment, product_name}``."""
        out: list[dict[str, Any]] = []
        try:
            d = config_mod.improve_reports_dir()
            if not d.exists():
                return []
            for p in sorted(d.glob("*.json"), reverse=True):
                try:
                    rep = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                out.append(
                    {
                        "id": rep.get("id") or p.stem,
                        "ts": rep.get("ts") or p.stem,
                        "kind": rep.get("kind", "evaluation"),
                        "comment": rep.get("comment", ""),
                        "product_name": rep.get("product_name", ""),
                        "has_synthesis": bool(rep.get("synthesis")),
                    }
                )
        except Exception:  # noqa: BLE001 — listing is best-effort
            logger.warning("list_improve_reports failed", exc_info=True)
        return out

    def load_improve_report(self, report_id: str) -> dict[str, Any] | None:
        """The full saved report dict, or None."""
        return self._load_improve(report_id)

    def delete_improve_report(self, report_id: str) -> bool:
        p = self._improve_path(report_id)
        try:
            p.unlink(missing_ok=True)
            (p.with_suffix(".md")).unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def synthesize_improve_report(self, report_id: str) -> dict[str, Any]:
        """Run the LLM-judge pass over a saved report: ranked recommendations +
        (for a problem report) a correlation of the comment against the evidence.
        Best-effort — returns the report with a ``synthesis_error`` note when no
        provider is configured or the call fails, never raising."""
        report = self._load_improve(report_id)
        if report is None:
            return {"error": f"unknown report id {report_id!r}"}
        cfg = config_mod.read_improve()
        if not cfg.get("llm_synthesis", True):
            report["synthesis_error"] = "LLM synthesis is disabled ([improve])."
            return report
        adapter = self._get_adapter()
        if adapter is None:
            report["synthesis_error"] = (
                "No model provider is configured — set one in Settings → LLM to "
                "synthesize recommendations."
            )
            return report
        prompt = improve_mod.build_synthesis_prompt(report)
        try:
            resp = asyncio.run(adapter.chat([{"role": "user", "content": prompt}], []))
            text = (getattr(resp, "text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001 — synthesis is best-effort
            logger.warning("synthesize_improve_report failed", exc_info=True)
            report["synthesis_error"] = f"synthesis call failed: {exc}"
            return report
        synthesis = improve_mod.parse_synthesis(text)
        if not synthesis:
            report["synthesis_error"] = "the model did not return a parseable report."
            return report
        report["synthesis"] = synthesis
        report["learnings_toml"] = improve_mod.build_learnings_snippet(
            report, synthesis
        )
        report["markdown"] = improve_mod.render_markdown(report, synthesis)
        self._save_improve(report)
        return report

    def improve_mailto(self, report_id: str) -> str:
        """Build a ``mailto:`` to the configured developers with the report body,
        open it in the operator's default mail client, and return the URL (so the
        frontend can also render it as a link). Empty string when no report."""
        report = self._load_improve(report_id)
        if report is None:
            return ""
        body = report.get("markdown") or improve_mod.render_markdown(
            report, report.get("synthesis")
        )
        url = improve_mod.mailto_url(report, body)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — opening a client is best-effort
            logger.warning(
                "could not open mail client for Improve report", exc_info=True
            )
        return url

    # ── internals ────────────────────────────────────────────────────────────
    def _build_improve_report(self, window_days: int, comment: str) -> dict[str, Any]:
        cfg = config_mod.read_improve()
        since = datetime.now(timezone.utc) - timedelta(days=max(1, window_days))
        since_iso = since.isoformat()
        conversations = self._gather_improve_conversations(
            since_iso, cfg["max_conversations"]
        )
        try:
            history = self.get_history_all()
        except Exception:  # noqa: BLE001 — history read is best-effort
            logger.warning("Improve: get_history_all failed", exc_info=True)
            history = []
        product = cfg["product_name"] or self._improve_product_name()
        report = improve_mod.build_report(
            conversations,
            history,
            comment=comment,
            since_iso=since_iso,
            window_days=window_days,
            max_incidents=cfg["max_incidents"],
            product_name=product,
            developer_emails=cfg["developer_emails"],
        )
        now = datetime.now(timezone.utc)
        report["id"] = now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond // 1000:03d}"
        report["ts"] = now.isoformat()
        report["learnings_toml"] = improve_mod.build_learnings_snippet(report)
        report["markdown"] = improve_mod.render_markdown(report)
        self._save_improve(report)
        return report

    def _gather_improve_conversations(
        self, since_iso: str, cap: int
    ) -> list[dict[str, Any]]:
        """Load recent saved conversations (newest first, capped). Filtering by
        ``since_iso`` happens in the core per-turn; here we just bound how many
        conversation files we read."""
        out: list[dict[str, Any]] = []
        try:
            directory = config_mod.conversations_dir()
            metas = conv_mod.list_meta(directory)  # newest first
        except Exception:  # noqa: BLE001
            logger.warning("Improve: could not list conversations", exc_info=True)
            return out
        for meta in metas:
            if str(meta.get("updated") or "") and str(meta["updated"]) < since_iso:
                # Whole conversation predates the window — stop (list is newest-first).
                break
            conv = conv_mod.load(directory, str(meta.get("id") or ""))
            if conv:
                out.append(conv)
            if len(out) >= cap:
                break
        return out

    def _improve_product_name(self) -> str:
        try:
            title = self.app_title()
            return str(title or "").strip() or "runspec console"
        except Exception:  # noqa: BLE001
            return "runspec console"

    def _improve_path(self, report_id: str):
        safe = "".join(c for c in str(report_id) if c.isalnum() or c in "-_")
        return config_mod.improve_reports_dir() / f"{safe or 'report'}.json"

    def _load_improve(self, report_id: str) -> dict[str, Any] | None:
        p = self._improve_path(report_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def _save_improve(self, report: dict[str, Any]) -> None:
        try:
            d = config_mod.improve_reports_dir()
            d.mkdir(parents=True, exist_ok=True)
            rid = str(report.get("id") or "report")
            p = self._improve_path(rid)
            p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            # Sidecar markdown — the attachable report body (mailto is length-capped).
            md = report.get("markdown") or improve_mod.render_markdown(
                report, report.get("synthesis")
            )
            p.with_suffix(".md").write_text(md, encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning("could not save Improve report", exc_info=True)
