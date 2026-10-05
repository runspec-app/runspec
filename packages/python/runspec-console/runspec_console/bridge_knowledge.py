"""
bridge_knowledge.py — the Bridge glue for the **team knowledge wiki**.

The pure store/index/capture lives in ``knowledge.py``; this mixin owns the I/O
around it and the three ways in:

* **Agent tools** (``search_wiki`` / ``wiki_page`` / ``wiki_graph`` /
  ``propose_fact`` / ``list_wiki_inbox`` / ``review_wiki_fact`` / ``edit_wiki_fact``
  / ``retire_wiki_fact`` / ``pin_wiki_fact``) — the console's own chat/trigger/schedule agent, and,
  through ``_MCP_SERVER_BUILTIN_NAMES``, an MCP client such as Claude Code when
  ``[mcp_server] expose_builtins`` is on. Same tools, same gate for both.
* **Knowledge tab** (public JS API ``knowledge_*``) — graph, pages, search, the
  review inbox, and operator-authored facts.
* **Capture** — ``knowledge_capture`` mines run history into ``observed`` facts
  (no AI); ``knowledge_distill`` asks the console's utility model to propose facts
  from recent chats into the inbox (AI; refused when ``[console] ai = false``).

**Pinned facts** (up to ``kn.PIN_MAX``, machine-local) fold into every chat's
system prompt via ``_knowledge_header``. The wiki replaced the older ``remember``
memory in 0.332.0: ``_knowledge_store`` moves a leftover ``runspec_memory.toml``
into the review inbox once (``_migrate_memory``), keeping its pins.

Every mutation emits ``runspec:knowledge_updated`` so an open tab refreshes.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config as config_mod
from . import conversations as conv_mod
from . import knowledge as kn

logger = logging.getLogger(__name__)

_MCP_CHAT_ID = "__mcp_server__"  # mirrors bridge_mcp_server._MCP_SERVER_CHAT_ID
# Mechanical capture re-runs at most this often on its own (the tab asks on open).
_CAPTURE_MAX_AGE_S = 60 * 60
_DEFAULT_CAPTURE_DAYS = 30
# Distill reads saved chats in chunks of this many characters, one model call per
# chunk, at most _DISTILL_MAX_CALLS calls per click (the rest wait for the next).
_DISTILL_CHUNK_CHARS = 24000
_DISTILL_MAX_CALLS = 10

_ENTITY_HELP = (
    "Entities are written type:name, type one of "
    + ", ".join(kn.ENTITY_TYPES)
    + " (e.g. host:web-01, runnable:tail-log, file:/var/log/orders/app.log, "
    "term:blue-green)."
)


def _author() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{user}@{socket.gethostname()}"


class KnowledgeMixin:
    # ── store ────────────────────────────────────────────────────────────────
    def _knowledge_store(self) -> kn.KnowledgeStore:
        store = kn.KnowledgeStore(config_mod.knowledge_dir())
        if not getattr(self, "_memory_migration_checked", False):
            self._memory_migration_checked = True
            try:
                self._migrate_memory(store)
            except Exception:  # noqa: BLE001 — never block the wiki on the old store
                logger.warning("memory → wiki migration failed", exc_info=True)
        return store

    def _migrate_memory(self, store: kn.KnowledgeStore) -> dict[str, int] | None:
        """One-time move of the older agent memory (``runspec_memory.toml``, the
        ``remember`` tool's store) into the wiki's review inbox, keeping its pins.
        The old file is renamed ``runspec_memory.toml.migrated`` afterwards (kept
        as a backup), so this runs once. ``None`` when there is nothing to move."""
        path = config_mod.memory_path()
        if not path.exists():
            return None
        from . import memory

        rows = memory.read_memory()
        try:
            catalog = self._knowledge_catalog()
        except Exception:  # noqa: BLE001 — link best-effort
            catalog = {}
        facts, pins, skipped = kn.memory_to_facts(
            rows,
            author=_author(),
            link=lambda about, text: kn.link_about(about, text, catalog),
        )
        for fact in facts:
            store.propose(fact)
        kept = [p for p in store.pinned_ids() if p not in pins]
        room = max(0, kn.PIN_MAX - len(kept))
        store.update_state(
            pinned=kept + pins[:room],
            memory_migrated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        path.replace(path.with_name(path.name + ".migrated"))
        if skipped:
            logger.warning(
                "memory → wiki: skipped %d fact(s) that look like secrets "
                "(still in %s.migrated): %s",
                len(skipped),
                path.name,
                ", ".join(skipped),
            )
        logger.info(
            "memory → wiki: moved %d fact(s) into the review inbox, %d pinned",
            len(facts),
            min(len(pins), room),
        )
        self._knowledge_changed()
        return {
            "facts": len(facts),
            "pinned": min(len(pins), room),
            "skipped": len(skipped),
        }

    def _knowledge_header(self) -> str:
        """The pinned-facts block for the system prompt (``""`` when none)."""
        try:
            return self._knowledge_store().header()
        except Exception:  # noqa: BLE001 — the prompt must still build
            logger.debug("pinned wiki header unavailable", exc_info=True)
            return ""

    def _knowledge_pin_count(self) -> int:
        try:
            return len(self._knowledge_store().pinned_facts())
        except Exception:  # noqa: BLE001
            return 0

    def _knowledge_changed(self) -> None:
        try:
            self._dispatch("runspec:knowledge_updated", {})  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a UI nudge must never fail a write
            logger.debug("knowledge_updated dispatch failed", exc_info=True)

    def _knowledge_query(self, fn: Any) -> Any:
        idx = self._knowledge_store().index()
        try:
            return fn(idx)
        finally:
            idx.close()

    # ── agent tools ──────────────────────────────────────────────────────────
    def _knowledge_tools(self) -> list[dict[str, Any]]:
        """Schemas for the wiki tools. Always offered (local store, no model
        needed), so an MCP client can use them even with the console's AI off."""
        about = {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the fact is about. " + _ENTITY_HELP,
        }
        return [
            {
                "name": "search_wiki",
                "description": (
                    "Search the team knowledge wiki — what the team already knows "
                    "about hosts, groups, profiles, runnables, file locations, jargon, "
                    "which tools go together and knock-on effects. Check it BEFORE "
                    "asking the operator where something lives or what a term means, "
                    "and before investigating something from scratch."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords."},
                        "kind": {"type": "string", "enum": list(kn.FACT_KINDS)},
                        "entity": {
                            "type": "string",
                            "description": "Only facts about this entity (type:name).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Default 10, max 50.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "wiki_page",
                "description": (
                    "Read one wiki page: every fact about an entity (a host, group, "
                    "profile, runnable, file, term, app, service or team), plus the "
                    "entities it links to. " + _ENTITY_HELP
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"entity": {"type": "string"}},
                    "required": ["entity"],
                },
            },
            {
                "name": "wiki_graph",
                "description": (
                    "The wiki's entity graph around an entity: its neighbours out to "
                    "depth hops, with weights — 'what usually goes with X', 'what else "
                    "touches this host'. Omit entity for the most-connected overview."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "depth": {"type": "integer", "description": "1-3, default 1."},
                    },
                },
            },
            {
                "name": "propose_fact",
                "description": (
                    "Propose a durable fact for the team wiki: where a file or log "
                    "lives, what team jargon means, tools that go together, something "
                    "learned about a host/group/profile, a knock-on effect, how the "
                    "team does something. One or two specific sentences that will still "
                    "be true next week and for everyone on the team; never secrets, one-off "
                    "results or per-person details (a username or account). It goes to "
                    "the operator's review inbox (not straight into the wiki), so "
                    "propose freely when you learn something reusable."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(kn.FACT_KINDS)},
                        "about": about,
                        "text": {"type": "string", "description": "The fact."},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional ids it came from (run ids, ticket keys).",
                        },
                    },
                    "required": ["kind", "about", "text"],
                },
            },
            {
                "name": "list_wiki_inbox",
                "description": (
                    "List proposed wiki facts waiting for review (id, kind, about, "
                    "text, who proposed it and from where)."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "review_wiki_fact",
                "description": (
                    "Approve or reject a proposed wiki fact by id (from "
                    "list_wiki_inbox). Optionally fix its kind/about/text while "
                    "approving. The operator confirms each review."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "kind": {"type": "string", "enum": list(kn.FACT_KINDS)},
                        "about": about,
                        "text": {"type": "string"},
                    },
                    "required": ["fact_id", "approve"],
                },
            },
            {
                "name": "edit_wiki_fact",
                "description": (
                    "Correct an approved wiki fact by id (kind, about and/or text). "
                    "The operator confirms each edit."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_id": {"type": "string"},
                        "kind": {"type": "string", "enum": list(kn.FACT_KINDS)},
                        "about": about,
                        "text": {"type": "string"},
                    },
                    "required": ["fact_id"],
                },
            },
            {
                "name": "pin_wiki_fact",
                "description": (
                    f"Pin a wiki fact into every future chat's context (at most "
                    f"{kn.PIN_MAX}), or unpin it with pinned=false. Pin only facts "
                    "that matter on almost every turn; everything else is a "
                    "search_wiki away. Pins are this console's own and are never "
                    "shared. Search results mark pinned facts."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_id": {"type": "string"},
                        "pinned": {
                            "type": "boolean",
                            "description": "false to unpin. Default true.",
                        },
                    },
                    "required": ["fact_id"],
                },
            },
            {
                "name": "retire_wiki_fact",
                "description": (
                    "Retire a wiki fact that is wrong or out of date (it stops showing "
                    "and stops being returned, and loses its pin). The operator "
                    "confirms each retirement."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"fact_id": {"type": "string"}},
                    "required": ["fact_id"],
                },
            },
        ]

    def _knowledge_source(self, chat_id: str) -> str:
        if chat_id == _MCP_CHAT_ID:
            return "mcp"
        try:
            with self._lock:  # type: ignore[attr-defined]
                origin = dict(self._run_origin.get(chat_id, {})) if chat_id else {}  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            origin = {}
        trig = str(origin.get("trigger") or "")
        return f"trigger:{trig}" if trig else "agent"

    def _run_knowledge_tool(
        self, name: str, ti: dict[str, Any], chat_id: str = ""
    ) -> str:
        """Run one wiki tool (the gate has already cleared it). Never raises."""
        try:
            return self._run_knowledge_tool_inner(name, ti, chat_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 — a tool must return
            logger.warning("knowledge tool %s failed", name, exc_info=True)
            return f"Error: {name} failed: {exc}"

    def _run_knowledge_tool_inner(
        self, name: str, ti: dict[str, Any], chat_id: str
    ) -> str:
        store = self._knowledge_store()
        pins = set(store.pinned_ids())
        if name == "search_wiki":
            limit = max(1, min(int(ti.get("limit") or 10), 50))
            hits = self._knowledge_query(
                lambda idx: idx.search(
                    str(ti.get("query") or ""),
                    kind=str(ti.get("kind") or ""),
                    entity=str(ti.get("entity") or ""),
                    limit=limit,
                )
            )
            if not hits:
                return "No wiki facts match. If you learn the answer, propose_fact it."
            return json.dumps({"facts": [_brief(f, pins=pins) for f in hits]})
        if name == "wiki_page":
            entity = str(ti.get("entity") or "").strip()
            if not entity:
                return "Error: 'entity' is required (type:name)."
            page = self._knowledge_query(
                lambda idx: idx.page(entity, include_pending=False)
            )
            if not page["facts"]:
                return f"No wiki page for {page['entity']} yet."
            page["facts"] = [_brief(f, pins=pins) for f in page["facts"]]
            return json.dumps(page)
        if name == "wiki_graph":
            g = self._knowledge_query(
                lambda idx: idx.graph(
                    entity=str(ti.get("entity") or ""),
                    depth=int(ti.get("depth") or 1),
                    max_nodes=60,
                )
            )
            nodes = sorted(g["nodes"], key=lambda n: n["weight"], reverse=True)
            return json.dumps(
                {
                    "nodes": [
                        {"entity": n["id"], "weight": n["weight"], "facts": n["facts"]}
                        for n in nodes
                    ],
                    "edges": sorted(
                        g["edges"], key=lambda e: e["weight"], reverse=True
                    )[:120],
                }
            )
        if name == "propose_fact":
            text = str(ti.get("text") or "")
            fact = kn.make_fact(
                kind=str(ti.get("kind") or "note"),
                about=kn.link_about(
                    list(ti.get("about") or []), text, self._knowledge_catalog()
                ),
                text=text,
                status="pending",
                source=self._knowledge_source(chat_id),
                author=_author(),
                evidence=list(ti.get("evidence") or []),
            )
            fid, outcome = store.propose(fact)
            self._knowledge_changed()
            return {
                "proposed": f"Proposed {fid}; it is in the operator's review inbox.",
                "already-pending": f"{fid} is already waiting for review (noted again).",
                "confirmed": f"{fid} is already in the wiki — confirmation recorded.",
            }[outcome]
        if name == "list_wiki_inbox":
            rows = store.inbox()
            if not rows:
                return "The wiki inbox is empty."
            return json.dumps({"inbox": [_brief(r, full=True) for r in rows]})
        if name == "review_wiki_fact":
            fid = str(ti.get("fact_id") or "").strip()
            edits = {k: ti[k] for k in ("kind", "about", "text") if k in ti}
            res = store.review(fid, bool(ti.get("approve")), edits or None)
            if res is None:
                return f"No pending fact {fid} (see list_wiki_inbox)."
            self._knowledge_changed()
            return f"Approved {res['id']}." if res else f"Rejected {fid}."
        if name == "edit_wiki_fact":
            fid = str(ti.get("fact_id") or "").strip()
            edits = {k: ti[k] for k in ("kind", "about", "text") if k in ti}
            if not edits:
                return "Error: give at least one of kind, about, text."
            res = store.edit(fid, dict(edits, author=_author()))
            if res is None:
                return f"No approved fact {fid}."
            self._knowledge_changed()
            return f"Updated; the fact is now {res['id']}."
        if name == "pin_wiki_fact":
            fid = str(ti.get("fact_id") or "").strip()
            if not fid:
                return "Error: 'fact_id' is required (from search_wiki or wiki_page)."
            ok, msg = store.set_pinned(fid, ti.get("pinned") is not False)
            if ok:
                self._knowledge_changed()
            return msg if ok else f"Error: {msg}"
        if name == "retire_wiki_fact":
            fid = str(ti.get("fact_id") or "").strip()
            ok = store.retire(fid)
            if ok:
                self._knowledge_changed()
            return f"Retired {fid}." if ok else f"No fact {fid}."
        return f"Error: unknown wiki tool {name!r}."

    # ── public JS API (Knowledge tab) ────────────────────────────────────────
    def knowledge_overview(self) -> dict[str, Any]:
        """Everything the tab's first paint needs: counts, the whole graph, the
        inbox, and when usage was last captured. Re-captures usage first when the
        last capture is over an hour old (best-effort)."""
        store = self._knowledge_store()
        try:
            age = time.time() - (store.root / kn.OBSERVED_FILE).stat().st_mtime
        except OSError:
            age = None
        if age is None or age > _CAPTURE_MAX_AGE_S:
            try:
                self.knowledge_capture(notify=False)
            except Exception:  # noqa: BLE001
                logger.debug("knowledge auto-capture failed", exc_info=True)
        idx = store.index()
        try:
            return {
                "stats": idx.stats(),
                "graph": idx.graph(),
                "inbox": store.inbox(),
                "pinned": store.pinned_facts(),
                "pin_max": kn.PIN_MAX,
                "captured_at": _mtime_iso(store.root / kn.OBSERVED_FILE),
                "capture_days": _as_days(
                    store.state().get("capture_days"), _DEFAULT_CAPTURE_DAYS
                ),
                "dir": str(store.root),
                "installed_runnables": self._knowledge_installed(),
                "entity_types": list(kn.ENTITY_TYPES),
                "kinds": list(kn.FACT_KINDS),
            }
        finally:
            idx.close()

    def _knowledge_installed(self) -> list[str]:
        """Names of the runnables discovered right now (local + connected hosts),
        so the tab can dim usage of runnables that are no longer installed. Empty
        until discovery has run — the tab then dims nothing."""
        try:
            with self._lock:  # type: ignore[attr-defined]
                cache = list(getattr(self, "_runnables_cache", []) or [])
        except Exception:  # noqa: BLE001
            return []
        return sorted({str(r.get("name")) for r in cache if r.get("name")})

    def _knowledge_config(self) -> dict[str, list[dict[str, Any]]]:
        """The config the wiki reads (hosts, triggers, schedules, runbooks,
        file filters) — each part best-effort, so one unreadable file never
        stops a capture."""
        out: dict[str, list[dict[str, Any]]] = {}

        def part(key: str, fn: Any) -> None:
            try:
                out[key] = list(fn() or [])
            except Exception:  # noqa: BLE001
                logger.debug("knowledge: reading %s failed", key, exc_info=True)
                out[key] = []

        def hosts() -> list[dict[str, Any]]:
            from . import hosts as hosts_mod

            return [
                {
                    "name": h.get("name"),
                    "group": h.get("group"),
                    "profiles": hosts_mod.host_profiles(h),
                }
                for h in self._all_host_entries()  # type: ignore[attr-defined]
            ]

        part("hosts", hosts)
        part("triggers", config_mod.read_triggers)
        part("schedules", lambda: self._read_schedules())  # type: ignore[attr-defined]
        part("runbooks", lambda: self.get_runbooks())  # type: ignore[attr-defined]
        part("filters", lambda: self.get_filters())  # type: ignore[attr-defined]
        return out

    def _knowledge_catalog(
        self, cfg: dict[str, list[dict[str, Any]]] | None = None
    ) -> dict[str, list[str]]:
        """Every entity the fleet already has — hosts, groups, profiles,
        runnables, triggers, schedules, runbooks — keyed by spelling-insensitive
        name, so new facts link to the existing node instead of a lookalike."""
        cfg = cfg if cfg is not None else self._knowledge_config()
        refs: list[str] = []
        for h in cfg.get("hosts", []):
            refs.append(f"host:{h.get('name')}")
            if h.get("group"):
                refs.append(f"group:{h['group']}")
            refs += [f"profile:{p}" for p in h.get("profiles") or []]
        for name, fn in (("group", "get_groups"), ("profile", "get_profiles")):
            try:
                refs += [f"{name}:{g.get('name')}" for g in getattr(self, fn)() or []]
            except Exception:  # noqa: BLE001
                pass
        refs += [f"runnable:{n}" for n in self._knowledge_installed()]
        refs += [f"trigger:{t.get('name')}" for t in cfg.get("triggers", [])]
        refs += [
            f"schedule:{s.get('id') or s.get('name')}" for s in cfg.get("schedules", [])
        ]
        refs += [f"runbook:{b.get('id')}" for b in cfg.get("runbooks", [])]
        return kn.entity_catalog(r for r in refs if not r.endswith(":None"))

    def knowledge_page(self, entity: str) -> dict[str, Any]:
        return self._knowledge_query(lambda idx: idx.page(str(entity or "")))

    def knowledge_search(self, query: str, kind: str = "") -> list[dict[str, Any]]:
        return self._knowledge_query(
            lambda idx: idx.search(
                str(query or ""), kind=str(kind or ""), include_pending=True, limit=50
            )
        )

    def knowledge_review(
        self, fact_id: str, approve: bool, edits: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            res = self._knowledge_store().review(
                str(fact_id), bool(approve), edits or None
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if res is None:
            return {"ok": False, "error": "That fact is no longer in the inbox."}
        self._knowledge_changed()
        return {"ok": True, "fact": res or None}

    def knowledge_save_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Operator-authored fact (approved straight away), or an edit when
        ``payload.id`` names an existing fact."""
        data = dict(payload or {})
        store = self._knowledge_store()
        try:
            if data.get("id"):
                res = store.edit(str(data["id"]), {**data, "author": _author()})
                if res is None:
                    return {"ok": False, "error": "That fact no longer exists."}
            else:
                res = kn.make_fact(
                    kind=str(data.get("kind") or "note"),
                    about=list(data.get("about") or []),
                    text=str(data.get("text") or ""),
                    status="approved",
                    source="operator",
                    author=_author(),
                )
                store.add_approved(res)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self._knowledge_changed()
        return {"ok": True, "fact": res}

    def knowledge_retire(self, fact_id: str) -> dict[str, Any]:
        ok = self._knowledge_store().retire(str(fact_id))
        if ok:
            self._knowledge_changed()
        return {"ok": ok}

    def knowledge_pin(self, fact_id: str, pinned: bool = True) -> dict[str, Any]:
        """Pin (or unpin) a fact into every chat's system prompt."""
        ok, msg = self._knowledge_store().set_pinned(str(fact_id), bool(pinned))
        if ok:
            self._knowledge_changed()
        return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}

    def knowledge_capture(
        self, since_days: int | None = None, notify: bool = True
    ) -> dict[str, Any]:
        """Mine run history (local + connected hosts) into observed facts,
        replacing the previous observations wholesale. ``since_days`` = 0 reads
        all history; ``None`` reuses the last range picked (default 30 days), so
        the tab's hourly auto-capture never narrows what the operator chose."""
        store = self._knowledge_store()
        if since_days is None:
            days = _as_days(store.state().get("capture_days"), _DEFAULT_CAPTURE_DAYS)
        else:
            days = _as_days(since_days, _DEFAULT_CAPTURE_DAYS)
            store.update_state(capture_days=days)
        try:
            history = self.get_history_all(since_days=days or None)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge capture: history read failed", exc_info=True)
            return {"ok": False, "error": f"could not read run history: {exc}"}
        facts = kn.observe(history)
        cfg = self._knowledge_config()
        catalog = self._knowledge_catalog(cfg)
        facts += kn.observe_config(**cfg, catalog=catalog)
        store.write_observed(facts)
        # Join earlier facts up with entities the fleet has since gained (a new
        # group, a runnable installed after the fact was written).
        relinked = store.relink(lambda about, text: kn.link_about(about, text, catalog))
        if notify:
            self._knowledge_changed()
        return {
            "ok": True,
            "observed": len(facts),
            "runs": len(history),
            "days": days,
            "relinked": relinked,
        }

    def knowledge_distill(
        self, window_days: int = 30, max_calls: int = _DISTILL_MAX_CALLS
    ) -> dict[str, Any]:
        """Ask the utility model to propose facts from saved chats in the last
        ``window_days`` (0 = all). Chats are read in ~24k-character chunks, one
        model call each, up to ``max_calls`` per click; each chat's progress is
        remembered (``state.toml``) so a re-run only reads new turns, and a big
        backlog continues where the last click stopped. Proposals land in the
        inbox (source ``distill``)."""
        if not getattr(self, "_ai_enabled", True):
            from .bridge_const import AI_OFF_MESSAGE

            return {"ok": False, "error": AI_OFF_MESSAGE}
        adapter = self._utility_adapter()  # type: ignore[attr-defined]
        if adapter is None:
            return {
                "ok": False,
                "error": "No model provider is configured (Settings → Model / API).",
            }
        days = _as_days(window_days, 30)
        since = (
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            if days
            else ""
        )
        store = self._knowledge_store()
        done = {
            str(k): int(v)
            for k, v in (store.state().get("distilled") or {}).items()
            if isinstance(v, int)
        }
        blocks, ends = _pending_chat_blocks(since, done)
        if not blocks:
            return {
                "ok": True,
                "proposed": 0,
                "note": "No new chats to read in that range.",
            }
        chunks = kn.chunk_transcripts(blocks, _DISTILL_CHUNK_CHARS)
        budget = max(1, int(max_calls or _DISTILL_MAX_CALLS))
        known = [
            str(f.get("text", ""))
            for f in store.all_facts()
            if f.get("status") != "retired"
        ]
        catalog = self._knowledge_catalog()
        entities = sorted({r for refs in catalog.values() for r in refs})
        proposed = skipped = calls = conversations = 0
        error = ""
        for text, finished in chunks[:budget]:
            if text.strip():
                prompt = kn.build_distill_prompt(
                    known=known, transcript=text, entities=entities
                )
                try:
                    resp = asyncio.run(
                        adapter.chat([{"role": "user", "content": prompt}], [])
                    )
                    reply = (getattr(resp, "text", "") or "").strip()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("knowledge distill failed", exc_info=True)
                    error = f"model call failed: {exc}"
                    break
                calls += 1
                for cand in kn.parse_distill(reply):
                    try:
                        fact = kn.make_fact(
                            kind=cand["kind"],
                            about=kn.link_about(cand["about"], cand["text"], catalog),
                            text=cand["text"],
                            status="pending",
                            source="distill",
                            author=_author(),
                        )
                    except ValueError:
                        skipped += 1
                        continue
                    _fid, outcome = store.propose(fact)
                    if outcome == "proposed":
                        proposed += 1
                        known.append(fact["text"])
            if finished:
                store.update_state(distilled={k: ends[k] for k in finished})
                conversations += len(finished)
        remaining = max(0, len(chunks) - budget) if not error else None
        if calls or conversations:
            self._knowledge_changed()
        out: dict[str, Any] = {
            "ok": not error,
            "proposed": proposed,
            "skipped": skipped,
            "calls": calls,
            "conversations": conversations,
        }
        if error:
            out["error"] = error
        else:
            out["remaining"] = remaining
        return out


# ── helpers ──────────────────────────────────────────────────────────────────


def _brief(
    f: dict[str, Any], full: bool = False, pins: set[str] | None = None
) -> dict[str, Any]:
    out = {
        "id": f.get("id"),
        "kind": f.get("kind"),
        "about": f.get("about", []),
        "text": f.get("text"),
        "confirmations": f.get("confirmations", 1),
        "last_confirmed": f.get("last_confirmed", ""),
    }
    if f.get("status") not in (None, "approved"):
        out["status"] = f.get("status")
    if pins and f.get("id") in pins:
        out["pinned"] = True
    if full:
        out.update(
            source=f.get("source", ""),
            author=f.get("author", ""),
            evidence=f.get("evidence", []),
        )
    return out


def _mtime_iso(path: Any) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
    except OSError:
        return ""


def _as_days(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _pending_chat_blocks(
    since_iso: str, done: dict[str, int]
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Unread chat turns per saved conversation, newest conversation first.

    ``done`` maps a conversation id to how many of its turns were already
    distilled (turns are append-only, so the rest are new). ``since_iso`` picks
    the conversations (last updated on/after it; empty = all) — a picked one is
    read in full from where it left off, so a later wider range loses nothing.
    Returns the ``(id, transcript)`` blocks plus each id's turn count to record."""
    blocks: list[tuple[str, str]] = []
    ends: dict[str, int] = {}
    try:
        directory = config_mod.conversations_dir()
        metas = conv_mod.list_meta(directory)  # newest first
    except Exception:  # noqa: BLE001
        return blocks, ends
    for meta in metas:
        cid = str(meta.get("id") or "")
        if not cid:
            continue
        if (
            since_iso
            and str(meta.get("updated") or "")
            and str(meta["updated"]) < since_iso
        ):
            break
        conv = conv_mod.load(directory, cid)
        turns = list((conv or {}).get("turns") or [])
        start = done.get(cid, 0)
        if start > len(turns):  # rewritten conversation: read it again
            start = 0
        if start >= len(turns):
            continue
        fresh = [
            t for t in turns[start:] if not t.get("kind") or t.get("kind") == "chat"
        ]
        title = str(meta.get("title") or "").strip()
        body = kn.render_turns(fresh)
        header = f"--- conversation: {title} ---" if title else "--- conversation ---"
        blocks.append((cid, f"{header}\n{body}" if body.strip() else ""))
        ends[cid] = len(turns)
    return blocks, ends
