"""
sources/verify_mail.py — compose + send the email-verify mail (console side).

The console's half of email-verified queue signup (docs/design/email-queue-signup.md
§7): when a console advertises a queue, the room dispatches it a ``send_verify``
frame asking it to mail a verify link **as the queue mailbox**. The console composes
the mail here and sends it via Outlook send-as (``SentOnBehalfOfName``), then acks
``verify_sent``.

The composition (:func:`compose_verify_mail`) is pure and unit-tested on any
platform; only the actual send (:func:`outlook_verify_sender`) needs Windows +
Outlook — the same split the email folder-watch source uses. ``ChatSource`` calls
the composer, then a pluggable sender (this module's default, or a fake in tests).
"""

from __future__ import annotations


def compose_verify_mail(
    name: str, queue: str, queue_email: str, verify_url: str
) -> tuple[str, str]:
    """The ``(subject, body)`` of the verify mail. Addressed to a person who *asked*
    to join ``queue``; the link is the single action. Plain text — it's sent as the
    queue mailbox and must read as a routine internal verification."""
    greeting = f"Hi {name}," if name else "Hi,"
    subject = f"Verify your email to join the {queue} queue"
    body = (
        f"{greeting}\n\n"
        f"You asked to join the {queue} request queue. Confirm this email address "
        f"by opening the link below:\n\n"
        f"    {verify_url}\n\n"
        f"The link can be used once and expires shortly. If you didn't request "
        f"this, you can ignore this message.\n"
    )
    return subject, body


def outlook_verify_sender(
    *, to: str, subject: str, body: str, from_mailbox: str
) -> None:
    """The default sender: mail via Outlook send-as ``from_mailbox``. Raises on any
    failure (so ``ChatSource`` acks ``verify_sent`` with ``ok=false`` and the room
    retries elsewhere). Windows-only — the win32 import lives in ``outlook``."""
    from .outlook import send_verify_mail

    send_verify_mail(to=to, subject=subject, body=body, from_mailbox=from_mailbox)
