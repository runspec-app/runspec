"""
test_email_bridge.py — the email folder-watch channel's bridge seam.

The claim/queue/fold machinery is gone (see docs/design/email-folder-watch.md):
a matched ``new_mail`` is just an ordinary trigger card + action via the normal
trigger path. The only email-specific bridge seam left is the reply-as identity —
``queue_mailbox_for`` returns the watch's ``mailbox`` tag (reply *as* the shared
mailbox the mail landed in; blank for your own inbox → reply as yourself).

Bound onto a bare stub to dodge Bridge.__init__ side effects.
"""

from __future__ import annotations

import types

from runspec_console.bridge_helpers import _BridgeTriggerOps


def _ops():
    return _BridgeTriggerOps(types.SimpleNamespace())


def test_queue_mailbox_for_returns_the_watch_tag():
    # A shared-mailbox watch → reply as that mailbox (send-as).
    assert (
        _ops().queue_mailbox_for(
            {"mailbox": "it-support@corp.com", "folder": "Tickets"}
        )
        == "it-support@corp.com"
    )


def test_queue_mailbox_for_blank_for_personal_inbox():
    # The default-account inbox has no mailbox tag → reply from your own identity.
    assert _ops().queue_mailbox_for({"mailbox": "", "folder": "Inbox"}) == ""
    assert _ops().queue_mailbox_for({}) == ""
