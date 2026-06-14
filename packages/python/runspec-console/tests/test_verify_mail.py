"""
Tests for the email-verify mail (sources/verify_mail.py + the Outlook send guard).

The composition is pure and tested here; the actual Outlook send is Windows-only
and only its off-platform guard is asserted (the COM path needs a real Outlook).
"""

from __future__ import annotations

import sys

import pytest

from runspec_console.sources.outlook import send_verify_mail
from runspec_console.sources.verify_mail import compose_verify_mail


def test_compose_includes_name_queue_and_link():
    subject, body = compose_verify_mail(
        "Jane Doe",
        "it-support",
        "it-support@corp.com",
        "https://room.test/verify?token=t",
    )
    assert "it-support" in subject
    assert body.startswith("Hi Jane Doe,")
    assert "https://room.test/verify?token=t" in body
    assert "it-support" in body


def test_compose_without_name_uses_generic_greeting():
    _subject, body = compose_verify_mail("", "it-support", "it@corp.com", "https://r/v")
    assert body.startswith("Hi,")


def test_compose_mentions_single_use_and_expiry():
    _subject, body = compose_verify_mail("J", "q", "q@c", "https://r/v")
    assert "once" in body and "expire" in body.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="off-platform guard only")
def test_send_verify_mail_raises_off_windows():
    with pytest.raises(RuntimeError, match="Windows"):
        send_verify_mail(
            to="jane@corp.com", subject="s", body="b", from_mailbox="it@corp.com"
        )
