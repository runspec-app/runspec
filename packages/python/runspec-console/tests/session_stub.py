"""Shared helpers for tests that drive Bridge internals on a bare stub.

The chat state that used to be five singletons on the Bridge
(``_chat_history`` / ``_history_turn_ids`` / ``_excluded_turns`` /
``_chat_lock`` / ``_conversation_id``) lives on a per-conversation
``ChatSession`` now (``runspec_console.session``), with the registry +
lifecycle helpers on ``TurnMixin`` (``runspec_console.bridge_turn``). These
helpers give a ``SimpleNamespace`` / plain-class stub exactly that, so a test
can keep binding the real Bridge methods it exercises.
"""

from __future__ import annotations

import threading
import types
from typing import Any

from runspec_console.bridge import Bridge
from runspec_console.bridge_turn import TurnSpec
from runspec_console.session import ChatSession, new_interactive

_REGISTRY_METHODS = (
    "_emit_session",
    "_chat_session",
    "_session_for_turn",
    "_resolve_chat_session",
    "_open_automation_session",
    "_close_automation_session",
    "_announce_evicted",
    "_begin_turn",
    "_end_turn_session",
    "new_chat_session",
    "list_chat_sessions",
    "select_chat_session",
    "close_chat_session",
)

_ENGINE_METHODS = (
    "_run_agent_turn",
    "_finish_turn",
    "_emit_context_estimate",
)


def install_sessions(stub: Any, conversation_id: str = "") -> ChatSession | None:
    """Give ``stub`` the session registry state + the real registry helpers.
    With ``conversation_id`` an interactive session of that id is opened and
    made current (so its workspace dir is ``workspace_dir(conversation_id)``);
    otherwise the first ``_chat_session()`` call mints one. Returns the opened
    session (or ``None``)."""
    if not hasattr(stub, "_lock"):
        stub._lock = threading.Lock()
    stub._sessions = {}
    stub._chat_session_id = ""
    stub._turn_sessions = {}
    stub._mcp_client_sessions = {}
    stub._conv_store_lock = threading.Lock()
    if not hasattr(stub, "_dispatch"):
        stub._dispatch = lambda *a, **k: None
    for name in _REGISTRY_METHODS:
        setattr(stub, name, types.MethodType(getattr(Bridge, name), stub))
    if conversation_id:
        session = new_interactive(conversation_id)
        stub._sessions[session.id] = session
        stub._chat_session_id = session.id
        return session
    return None


def install_engine(stub: Any) -> None:
    """Bind the real agent-turn engine (+ its turn-end reporting) onto a stub
    that already has ``install_sessions`` applied."""
    for name in _ENGINE_METHODS:
        setattr(stub, name, types.MethodType(getattr(Bridge, name), stub))
    # The context gauge counts pinned wiki facts; a stub has no wiki.
    if not hasattr(stub, "_knowledge_pin_count"):
        stub._knowledge_pin_count = lambda: 0


def agentic_shim(stub: Any):
    """The old ``_agentic_chat_turn(chat_id, message, adapter, system_parts,
    index_text)`` entry point, rebuilt over the engine: builds the ``TurnSpec``
    the chat builder (``_spawn_chat_turn``) would and runs it against the
    stub's current chat session. Lets loop tests keep their call shape."""
    install_engine(stub)

    async def _agentic_chat_turn(
        chat_id: str,
        message: str,
        adapter: Any,
        system_parts: dict[str, int] | None = None,
        index_text: str = "",
    ) -> None:
        cancels = getattr(stub, "_chat_cancels", {})
        spec = TurnSpec(
            chat_id=chat_id,
            session=stub._chat_session(),
            message=message,
            adapter=adapter,
            tools=stub._runnables_to_tools(),
            kind="chat",
            interactive=True,
            cancel=cancels.get(chat_id),
            system_parts=dict(system_parts or {}),
            index_text=index_text,
        )
        await stub._run_agent_turn(spec)

    return _agentic_chat_turn


def current(stub: Any) -> ChatSession:
    """The stub's current interactive chat session (``_S`` in the tests)."""
    return stub._chat_session()
