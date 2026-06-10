"""
Tests for shutdown abort — closing the app (or restarting the engine) must wake
any turn parked at the autonomy gate so a worker isn't stuck in event.wait().

Uses Bridge.__new__ to set just the fields _abort_turns touches — no full
__init__ (which builds the SSH pool etc.).
"""

from __future__ import annotations

import threading

from runspec_console.bridge import Bridge


def _bridge_with_turns():
    b = Bridge.__new__(Bridge)
    b._lock = threading.Lock()
    chat_ev, card_ev = threading.Event(), threading.Event()
    b._chat_cancels = {"chat1": chat_ev, "cardA": card_ev}
    h_chat, h_card = {"cancelled": False}, {"cancelled": False}
    pe_chat, pe_card = threading.Event(), threading.Event()
    b._pending_confirms = {
        "r1": ("chat1", pe_chat, h_chat),
        "r2": ("cardA", pe_card, h_card),
    }
    return b, (chat_ev, h_chat, pe_chat), (card_ev, h_card, pe_card)


def test_abort_turns_scoped_to_ids():
    """Restart aborts only trigger-card turns, leaving a chat turn alone."""
    b, chat, card = _bridge_with_turns()
    chat_ev, h_chat, pe_chat = chat
    card_ev, h_card, pe_card = card

    b._abort_turns({"cardA"})  # only the trigger card

    assert card_ev.is_set() and h_card["cancelled"] and pe_card.is_set()
    assert not chat_ev.is_set() and not h_chat["cancelled"] and not pe_chat.is_set()


def test_abort_all_turns_wakes_everything():
    """Shutdown aborts every turn (chat included) so nothing stays in event.wait()."""
    b, chat, card = _bridge_with_turns()
    chat_ev, h_chat, pe_chat = chat
    card_ev, h_card, pe_card = card

    b._abort_all_turns()  # window-close / atexit path → ids=None

    assert chat_ev.is_set() and h_chat["cancelled"] and pe_chat.is_set()
    assert card_ev.is_set() and h_card["cancelled"] and pe_card.is_set()
