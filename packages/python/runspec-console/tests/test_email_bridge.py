"""
test_email_bridge.py — the email channel of a help-desk queue.

The email analog of test_chat_bridge's queue-card coverage: a new_mail event
addressed to a mapped shared mailbox folds onto one trigger card per email
conversation, surfacing in the same Triggers inbox as a chat queue card. Mail
that isn't addressed to a mapped mailbox (personal/other mail) never becomes a
card. Claim + lifecycle ride the mailbox itself — an ack email carries a marker
and the card's state/owner come from folding the thread (mailbox_claim).

Pure: message payloads are built from the platform-independent ``_mail_payload``
fakes (no win32), marker payloads are plain dicts, and the bridge methods are
bound onto a bare stub to dodge Bridge.__init__ side effects (mirroring
test_chat_bridge). The COM ack send is monkeypatched.

See docs/design/helpdesk-channels.md.
"""

from __future__ import annotations

import threading
import types
from typing import Any

from runspec_console.bridge import Bridge
from runspec_console.events import Event
from runspec_console.sources.outlook import _mail_payload


def _mail(
    to: str,
    *,
    sender: str = "alice@big.com",
    subject: str = "Outage on web01",
    body: str = "the site is down",
    conversation_id: str = "CONV1",
    entry_id: str = "ENT1",
    received_ts: float = 0.0,
) -> dict[str, Any]:
    """A requester-message payload via the pure extractor — what OnNewMailEx emits
    for ordinary mail (no PropertyAccessor → no transport headers → no marker).
    ``received_ts`` overrides the fold order key (the fakes carry no ReceivedTime)."""
    payload = _mail_payload(
        types.SimpleNamespace(
            To=to,
            SenderEmailAddress=sender,
            Subject=subject,
            Body=body,
            ConversationID=conversation_id,
            EntryID=entry_id,
        )
    )
    if received_ts:
        payload["received_ts"] = received_ts
    return payload


def _marker_mail(
    kind: str,
    agent: str,
    *,
    queue: str = "it-support",
    to: str = "it-support@corp.com",
    conversation_id: str = "CONV1",
    received_ts: float = 100.0,
    message_id: str = "<ack@corp>",
) -> dict[str, Any]:
    """A claim/lifecycle ack payload — the new_mail the marker email becomes when
    it lands back in the shared mailbox (X-Runspec-Claim header → claim_marker)."""
    return {
        "to": to,
        "from": "it-support@corp.com",
        "subject": "RE: Outage on web01",
        "body": "runspec claim marker (automated)",
        "conversation_id": conversation_id,
        "recipients": [to],
        "received_ts": received_ts,
        "message_id": message_id,
        "claim_marker": f"{queue}/{conversation_id}/{agent}/{kind}",
        "entry_id": "ACK1",
    }


_BRIDGE_METHODS = (
    "_email_queue_for",
    "_email_mailbox_for",
    "_email_conversation_key",
    "_ingest_email",
    "_evict_trigger_cards",
    "_on_engine_event",
    "_email_feed_gate",
    "_feed_email_event",
    "_email_control",
    "claim_email_thread",
    "resolve_email_thread",
    "reopen_email_thread",
    "close_email_thread",
    "reply_email_thread",
)


class _FakeEngine:
    """Records the synthetic lifecycle events the bridge feeds (e.g. claimed)."""

    def __init__(self) -> None:
        self.fed: list[Event] = []

    def feed(self, event: Event) -> None:
        self.fed.append(event)


def _email_stub(mailboxes: dict[str, str]):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._trigger_cards = {}
    s._email_threads = {}
    s._email_seen = set()
    s._trigger_engine = _FakeEngine()  # captures fed claimed events
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._mailbox_queues = lambda: dict(mailboxes)  # the [mailboxes] map, lowercased
    s._agent_identity = lambda: "me@console1"  # deterministic console identity
    # A ticket opens only for *triggered* mail; default the gate open so the
    # card-synthesis tests exercise the fold. The "not triggered" test overrides it.
    s._email_is_triggered = lambda payload: True
    for name in _BRIDGE_METHODS:
        setattr(s, name, types.MethodType(getattr(Bridge, name), s))
    return s


_MAP = {"it-support@corp.com": "it-support"}


# ── recipient → queue mapping ─────────────────────────────────────────────────


def test_email_queue_for_matches_case_insensitive_and_multi_recipient():
    s = _email_stub(_MAP)
    # A display-name recipient with the mailbox SMTP address still matches.
    assert s._email_queue_for({"to": "Helpdesk <IT-Support@Corp.com>"}) == "it-support"
    # The mailbox can be one of several recipients.
    assert (
        s._email_queue_for({"to": "someone@x.com; it-support@corp.com"}) == "it-support"
    )
    assert s._email_queue_for({"to": "other@x.com"}) is None
    assert s._email_queue_for({"to": ""}) is None


def test_email_queue_for_no_mapping_returns_none():
    assert _email_stub({})._email_queue_for({"to": "it-support@corp.com"}) is None


def test_email_queue_for_prefers_header_recipients_over_display_to():
    """The authoritative path: real SMTP recipients (from the transport headers)
    name the mailbox even when an alias hides it from the visible To."""
    s = _email_stub(_MAP)
    payload = {"to": "Help Desk <help@corp.com>", "recipients": ["it-support@corp.com"]}
    assert s._email_queue_for(payload) == "it-support"


def test_email_queue_for_header_match_is_exact_not_substring():
    """A lookalike address in the headers must NOT match — exact membership,
    unlike the To-string fallback's substring test."""
    s = _email_stub(_MAP)
    assert (
        s._email_queue_for({"recipients": ["it-support@corp.com.evil.example"]}) is None
    )


def test_email_queue_for_falls_back_to_to_when_no_header_recipients():
    """Internal MAPI mail with no transport-header block → fall back to the
    display To string."""
    s = _email_stub(_MAP)
    out = s._email_queue_for({"to": "it-support@corp.com", "recipients": []})
    assert out == "it-support"


def test_email_mailbox_for_reverse_lookup():
    s = _email_stub(_MAP)
    assert s._email_mailbox_for("it-support") == "it-support@corp.com"
    assert s._email_mailbox_for("unknown") == ""


# ── card synthesis (the email analog of _touch_queue_card) ────────────────────


def test_email_to_mapped_mailbox_creates_card():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    cards = [d for ev, d in s.dispatched if ev == "runspec:trigger_card"]
    assert len(cards) == 1  # the email surfaced exactly one card
    card = s._trigger_cards["queue:email:CONV1"]
    assert card["source"] == "email" and card["event_type"] == "new_mail"
    assert card["trigger"] == "queue:it-support"
    assert card["summary"] == "Outage on web01"  # subject is the summary
    assert card["column"] == "triggered"
    assert card["detail"]["visitor"] == "alice@big.com"  # visitor = From
    assert card["detail"]["queue"] == "it-support"
    assert card["detail"]["mailbox"] == "it-support@corp.com"  # for the ack send
    assert card["detail"]["state"] == "open"  # unclaimed
    assert card["detail"]["owner"] is None
    assert card["detail"]["entry_id"] == "ENT1"


def test_repeated_emails_same_conversation_update_one_card():
    """An email and its reply (same ConversationID) update one card, not two."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", subject="Outage", entry_id="E1"))
    s._ingest_email(
        _mail(to="it-support@corp.com", subject="Re: Outage", entry_id="E2")
    )
    assert list(s._trigger_cards) == ["queue:email:CONV1"]  # one card, keyed by convo
    card = s._trigger_cards["queue:email:CONV1"]
    assert card["summary"] == "Re: Outage"  # latest message
    assert card["detail"]["entry_id"] == "E2"  # newest mail stays addressable
    cards = [d for ev, d in s.dispatched if ev == "runspec:trigger_card"]
    assert len({c["card_id"] for c in cards}) == 1


def test_distinct_conversations_create_distinct_cards():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", conversation_id="A", entry_id="A1"))
    s._ingest_email(_mail(to="it-support@corp.com", conversation_id="B", entry_id="B1"))
    assert set(s._trigger_cards) == {"queue:email:A", "queue:email:B"}


def test_personal_mail_creates_no_card():
    """Mail not addressed to a mapped mailbox never becomes a queue card — the
    email analog of a plain (non-helpdesk) chat room."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="me@corp.com"))
    assert s._trigger_cards == {}
    assert s.dispatched == []
    assert s._email_threads == {}  # not even tracked for the fold


def test_no_mailboxes_configured_creates_no_card():
    s = _email_stub({})
    s._ingest_email(_mail(to="it-support@corp.com"))
    assert s._trigger_cards == {}


def test_card_summary_falls_back_when_subject_and_body_empty():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", subject="", body=""))
    card = s._trigger_cards["queue:email:CONV1"]
    assert card["summary"] == "alice@big.com · it-support"


# ── claim + lifecycle (the validated fold on the card) ────────────────────────


def test_claim_ack_assigns_owner_and_is_not_a_requester_message():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))  # requester asks
    s._ingest_email(_marker_mail("claim", "ana@web01"))  # ana's claim ack lands
    card = s._trigger_cards["queue:email:CONV1"]
    assert card["detail"]["state"] == "assigned"
    assert card["detail"]["owner"] == "ana@web01"
    assert card["column"] == "triage"
    # The ack is internal plumbing: it neither bumps the summary nor adds a
    # requester line to the timeline.
    assert card["summary"] == "Outage on web01"
    assert not any("runspec claim marker" in e["text"] for e in card["timeline"])


def test_first_ack_wins_on_the_card():
    """Two consoles race Claim; both acks land. The earlier server order owns it,
    regardless of which ack this console ingests first."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(
        _marker_mail("claim", "bob@web02", received_ts=200, message_id="<b>")
    )
    s._ingest_email(
        _marker_mail("claim", "ana@web01", received_ts=100, message_id="<a>")
    )
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["owner"] == "ana@web01"


def test_resolve_by_owner_then_reopen():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(
        _marker_mail("claim", "ana@web01", received_ts=100, message_id="<1>")
    )
    s._ingest_email(
        _marker_mail("resolve", "ana@web01", received_ts=200, message_id="<2>")
    )
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["state"] == "resolved"
    # A requester reply after resolve reopens (later server order folds to assigned).
    s._ingest_email(
        _mail(
            to="it-support@corp.com",
            subject="still broken",
            received_ts=300,
            entry_id="ENT2",
        )
    )
    card = s._trigger_cards["queue:email:CONV1"]
    assert (
        card["detail"]["state"] == "assigned" and card["detail"]["owner"] == "ana@web01"
    )


def test_lifecycle_marker_from_non_owner_is_ignored():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(
        _marker_mail("claim", "ana@web01", received_ts=100, message_id="<1>")
    )
    s._ingest_email(
        _marker_mail("resolve", "bob@web02", received_ts=200, message_id="<2>")
    )
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["state"] == "assigned"


# ── sending the ack (claim_email_thread → send_claim_marker, monkeypatched) ────


def test_claim_email_thread_sends_self_addressed_marker(monkeypatch):
    from runspec_console.sources import outlook

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        outlook,
        "send_claim_marker",
        lambda entry_id, marker, mailbox: sent.append((entry_id, marker, mailbox)),
    )
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", entry_id="ENT9"))
    out = s.claim_email_thread("queue:email:CONV1")
    assert out == {"ok": True}
    assert len(sent) == 1
    entry_id, marker, mailbox = sent[0]
    assert entry_id == "ENT9"  # threads on the latest mail
    assert mailbox == "it-support@corp.com"  # addressed to the mailbox, not requester
    assert marker == "it-support/CONV1/me@console1/claim"  # this console's identity


def test_lifecycle_methods_send_their_kind(monkeypatch):
    from runspec_console.sources import outlook

    kinds: list[str] = []
    monkeypatch.setattr(
        outlook,
        "send_claim_marker",
        lambda entry_id, marker, mailbox: kinds.append(marker.rsplit("/", 1)[-1]),
    )
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    card_id = "queue:email:CONV1"
    s.resolve_email_thread(card_id)
    s.reopen_email_thread(card_id)
    s.close_email_thread(card_id)
    assert kinds == ["resolve", "reopen", "close"]


def test_email_control_unknown_card():
    out = _email_stub(_MAP)._email_control("claim", "queue:email:nope")
    assert out["ok"] is False and "unknown card" in out["error"]


def test_claim_by_this_console_marks_owned_by_me():
    s = _email_stub(_MAP)  # stub identity is "me@console1"
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "me@console1"))
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["owned_by_me"] is True


def test_claim_by_teammate_is_not_owned_by_me():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "ana@web01"))
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["owned_by_me"] is False


# ── reply (ownership-gated; draft + send to the requester) ─────────────────────


def _patch_outlook_reply(monkeypatch):
    from runspec_console.sources import outlook

    drafted: list[tuple[str, str, str | None]] = []
    sent: list[str] = []
    monkeypatch.setattr(
        outlook,
        "save_reply_draft",
        lambda eid, body, from_mailbox=None: (
            drafted.append((eid, body, from_mailbox)),
            {"entry_id": "DRAFT1"},
        )[1],
    )
    monkeypatch.setattr(outlook, "send_draft", lambda eid: (sent.append(eid), {})[1])
    return drafted, sent


def test_reply_requires_ownership():
    """Rule 3: a console that doesn't own the conversation can't message the
    requester — even if it raced Claim and lost."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "ana@web01"))  # a teammate owns it
    out = s.reply_email_thread("queue:email:CONV1", "on it")
    assert out["ok"] is False and "claim" in out["error"]


def test_reply_by_owner_drafts_and_sends(monkeypatch):
    drafted, sent = _patch_outlook_reply(monkeypatch)
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", entry_id="ENT9"))
    s._ingest_email(_marker_mail("claim", "me@console1"))  # we own it
    out = s.reply_email_thread("queue:email:CONV1", "rebooted — try now")
    assert out == {"ok": True}
    # Replies to the requester's mail, sent *as the shared mailbox* (Q5).
    assert drafted == [("ENT9", "rebooted — try now", "it-support@corp.com")]
    assert sent == ["DRAFT1"]
    card = s._trigger_cards["queue:email:CONV1"]
    assert any("↩ replied" in e["text"] for e in card["timeline"])


def test_reply_rejects_empty_and_unknown():
    s = _email_stub(_MAP)
    assert s.reply_email_thread("queue:email:CONV1", "   ")["ok"] is False
    assert s.reply_email_thread("queue:email:nope", "hi")["ok"] is False


# ── ticket gate (only triggered mail opens a card) + dedup ────────────────────


def test_untriggered_email_opens_no_ticket():
    """An email that matches no trigger stays in the inbox — no card, and no
    per-conversation state kept."""
    s = _email_stub(_MAP)
    s._email_is_triggered = lambda payload: False
    s._ingest_email(_mail(to="it-support@corp.com"))
    assert s._trigger_cards == {}  # no ticket
    assert s._email_threads == {}  # not even folded (no memory for inbox noise)
    assert [ev for ev, _ in s.dispatched] == []  # nothing pushed to the UI


def test_marker_for_unsurfaced_thread_is_ignored():
    """A claim/lifecycle ack for a conversation that never became a ticket (no
    matching trigger) is ignored — a marker never *opens* a ticket."""
    s = _email_stub(_MAP)
    s._email_is_triggered = lambda payload: (
        False
    )  # would-be requester mail isn't a ticket
    s._ingest_email(_marker_mail("claim", "me@console1"))
    assert s._trigger_cards == {}


def test_triggered_email_opens_a_ticket():
    """A triggered email opens exactly one ticket (the gate default in the stub)."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    assert "queue:email:CONV1" in s._trigger_cards


def test_ingest_dedupes_an_overlapping_item():
    """The live source re-delivering an item folds it only once."""
    s = _email_stub(_MAP)
    item = _mail(to="it-support@corp.com", entry_id="M1")
    item["message_id"] = "<dup>"
    s._ingest_email(item)
    s._ingest_email(item)  # a re-delivery of the same item
    card = s._trigger_cards["queue:email:CONV1"]
    requester_lines = [
        e for e in card["timeline"] if e["text"].startswith("alice@big.com:")
    ]
    assert len(requester_lines) == 1  # folded once, not twice


# ── the claim gate for email agent turns (mirrors chat's _feed_engine_for) ─────


def _new_mail_event(payload: dict) -> Event:
    return Event.new("outlook", "new_mail", payload)


def test_feed_gate_blocks_unclaimed_queue_mail():
    # A help-desk new_mail on an unowned thread surfaces the card but does NOT
    # route — no model spend until someone here claims it.
    s = _email_stub(_MAP)
    mail = _mail(to="it-support@corp.com")
    s._ingest_email(mail)  # surfaces the card (unowned)
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["owned_by_me"] is False
    assert s._email_feed_gate(_new_mail_event(mail)) is False


def test_feed_gate_passes_once_owned_by_this_console():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "me@console1"))  # our claim folds
    assert s._trigger_cards["queue:email:CONV1"]["detail"]["owned_by_me"] is True
    follow_up = _mail(to="it-support@corp.com", entry_id="ENT2")
    assert s._email_feed_gate(_new_mail_event(follow_up)) is True


def test_feed_gate_blocks_when_a_teammate_owns_it():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "ana@web01"))  # a teammate claims
    follow_up = _mail(to="it-support@corp.com", entry_id="ENT2")
    assert s._email_feed_gate(_new_mail_event(follow_up)) is False


def test_feed_gate_blocks_a_marker_ack():
    # A claim/lifecycle ack is internal plumbing — never an agent turn, even on a
    # thread this console owns.
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "me@console1"))  # we own it
    ack = _marker_mail("resolve", "me@console1", message_id="<ack2>")
    assert s._email_feed_gate(_new_mail_event(ack)) is False


def test_feed_gate_passes_non_queue_mail_ungated():
    # Personal / other mail has no claim concept — a user's own Outlook trigger
    # still fires.
    s = _email_stub(_MAP)
    assert s._email_feed_gate(_new_mail_event(_mail(to="me@corp.com"))) is True


def test_feed_gate_passes_non_new_mail_events():
    s = _email_stub(_MAP)
    assert (
        s._email_feed_gate(Event.new("outlook", "reminder", {"subject": "x"})) is True
    )
    assert s._email_feed_gate(Event.new("chat", "message", {"text": "hi"})) is True


# ── draft on claim: a synthetic email/claimed trigger event ───────────────────


def _claimed_events(stub):
    return [e for e in stub._trigger_engine.fed if e.type == "claimed"]


def test_claim_by_this_console_feeds_a_claimed_event():
    """Taking ownership feeds one email/claimed event carrying the latest
    requester message (so a configured trigger can draft on claim)."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", subject="VPN down", body="help"))
    s._ingest_email(_marker_mail("claim", "me@console1"))  # we win the claim
    events = _claimed_events(s)
    assert len(events) == 1
    p = events[0].payload
    assert events[0].source == "email"
    assert p["queue"] == "it-support"
    assert p["from"] == "alice@big.com"
    assert p["subject"] == "VPN down"
    assert p["body"] == "help"
    assert p["entry_id"] == "ENT1"
    # recipients = the mailbox, so queue_mailbox_for resolves the send-as identity.
    assert p["recipients"] == ["it-support@corp.com"]


def test_claim_by_teammate_feeds_no_claimed_event():
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com"))
    s._ingest_email(_marker_mail("claim", "ana@web01"))  # a teammate wins
    assert _claimed_events(s) == []


def test_ownership_flip_fires_once_not_on_later_messages():
    """The claimed event fires on the flip only — a follow-up message on an
    already-owned thread doesn't re-fire it."""
    s = _email_stub(_MAP)
    s._ingest_email(_mail(to="it-support@corp.com", entry_id="E1"))
    s._ingest_email(_marker_mail("claim", "me@console1"))  # flip → fires
    s._ingest_email(
        _mail(to="it-support@corp.com", subject="Re: more", entry_id="E2")
    )  # still owned → no new fire
    assert len(_claimed_events(s)) == 1


# ── send-as-mailbox resolution for the agent draft (_BridgeTriggerOps) ─────────


def test_queue_mailbox_for_resolves_helpdesk_and_skips_personal():
    """The trigger-ops adapter resolves a help-desk email event to its shared
    mailbox (so the agent draft replies as the queue), and "" for other mail."""
    from runspec_console.bridge import _BridgeTriggerOps

    ops = _BridgeTriggerOps(_email_stub(_MAP))
    assert (
        ops.queue_mailbox_for({"recipients": ["it-support@corp.com"]})
        == "it-support@corp.com"
    )
    assert ops.queue_mailbox_for({"to": "someone-else@corp.com"}) == ""


# ── conversation key (ConversationID, with a normalised-subject fallback) ──────


def test_conversation_key_prefers_conversation_id():
    assert (
        Bridge._email_conversation_key({"conversation_id": "C9", "subject": "x"})
        == "C9"
    )


def test_conversation_key_falls_back_to_normalised_subject():
    # Leading reply/forward prefixes are stripped so a Re:/Fwd: still threads.
    assert Bridge._email_conversation_key({"subject": "Re: Help me"}) == "Help me"
    assert (
        Bridge._email_conversation_key({"subject": "FWD: Re: Ticket 42"}) == "Ticket 42"
    )
    assert Bridge._email_conversation_key({}) == "(no subject)"


# ── engine raw-event observer (_on_engine_event) ──────────────────────────────


def test_on_engine_event_folds_new_mail_into_a_card():
    s = _email_stub(_MAP)
    s._on_engine_event(
        Event.new("outlook", "new_mail", _mail(to="it-support@corp.com"))
    )
    assert "queue:email:CONV1" in s._trigger_cards


def test_on_engine_event_ignores_reminders_and_chat():
    """Only outlook:new_mail folds onto an email card; reminders and chat feeds
    (which already route through _on_chat_event) are left to the normal path."""
    s = _email_stub(_MAP)
    s._on_engine_event(Event.new("outlook", "reminder", {"subject": "standup"}))
    s._on_engine_event(
        Event.new("chat", "message", {"room": "helpdesk/it/x@corp", "text": "hi"})
    )
    assert s._trigger_cards == {}
    assert s.dispatched == []
