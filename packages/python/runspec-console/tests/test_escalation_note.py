"""A refused `sudo -n` escalation ends with a plain-English note."""

from __future__ import annotations

from runspec_console.bridge_utils import escalation_refusal_note, watch_escalation

SUDO_NOPASS = [
    "sudo: a terminal is required to read the password; either use ssh's -t option or configure an askpass helper",
    "sudo: a password is required",
]


def test_names_the_accounts_and_the_fix():
    note = escalation_refusal_note(SUDO_NOPASS, "svc-ops", "bob", "deb2-bob")
    assert "Couldn't run as svc-ops on deb2-bob" in note
    assert "bob" in note and "passwordless sudo rule" in note
    assert "askpass" not in note and "-t option" not in note


def test_root_suggests_a_terminal_too():
    note = escalation_refusal_note(
        ["sudo: a password is required"], "root", "jason", "deb3-jason"
    )
    assert "as root" in note and "from a terminal with sudo" in note


def test_not_in_sudoers():
    note = escalation_refusal_note(
        ["bob is not in the sudoers file."], "svc-ops", "bob", "h"
    )
    assert note


def test_su_authentication_failure():
    note = escalation_refusal_note(
        ["su: Authentication failure"], "svc-ops", "bob", "h", "su"
    )
    assert "su needs svc-ops's password" in note


def test_ordinary_failures_get_no_note():
    assert (
        escalation_refusal_note(
            ["Traceback …", "ValueError: nope"], "svc-ops", "bob", "h"
        )
        == ""
    )
    assert escalation_refusal_note(SUDO_NOPASS, "", "bob", "h") == ""


def test_watch_appends_the_note_before_done():
    events = []
    line, done = watch_escalation(
        lambda t, s: events.append(("line", t, s)),
        lambda c, d: events.append(("done", c)),
        "svc-ops",
        "bob",
        "deb2-bob",
    )
    for text in SUDO_NOPASS:
        line(text, "stderr")
    done(1, 5)
    assert events[-1] == ("done", 1)
    assert (
        events[-2][0] == "line"
        and events[-2][1].startswith("✗  Couldn't run as svc-ops")
        and events[-2][2] == "stderr"
    )


def test_watch_is_silent_on_success_and_without_escalation():
    events = []
    line, done = watch_escalation(
        lambda t, s: events.append(t),
        lambda c, d: events.append(c),
        "svc-ops",
        "alice",
        "h",
    )
    line(
        "sudo: a password is required", "stderr"
    )  # even with the text, exit 0 ⇒ no note
    done(0, 1)
    assert events == ["sudo: a password is required", 0]
    on_line, on_done = object(), object()
    assert watch_escalation(on_line, on_done, "", "alice", "h") == (on_line, on_done)
