"""
web/signup_routes.py — the email-verify sign-up door (``/queues``, ``/signup``,
``/verify``).

The browser half of the email-verify auth mode (docs/design/email-queue-signup.md
§3): an internal employee picks an advertised queue, gives their company email +
full name, and the room writes a **pending signup** row (the durable verify
outbox) and dispatches a verify mail *via a connected console* (the
``verify_sender`` seam — wired to the console send in slice 4; left in the outbox
when absent). Clicking the signed, single-use verify link enrolls them: the room
burns the nonce, persists the member, and sets the same signed session cookie the
OIDC door uses — so the existing ``session_user`` gate on ``/ws`` and ``/catalog``
needs no change.

Server-rendered HTML (no build step, no external assets) so the page works without
JavaScript and is exercised end-to-end by the TestClient. The actual mail send is
Outlook-only and lands in slice 4; everything here is platform-agnostic.
"""

from __future__ import annotations

import html
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from ..enrollment_store import EnrollmentStore
from ..hub import Hub
from ..queues import QueueDescriptor
from ..signup import (
    EmailVerifyConfig,
    Member,
    PendingSignup,
    domain_allowed,
    email_domain,
    make_nonce,
    read_verify_token,
    sign_verify_token,
)

logger = logging.getLogger("console_room.web")


@dataclass(frozen=True)
class VerifySend:
    """The data a console needs to send one verify mail *as the queue mailbox*.
    Handed to the :data:`VerifySender` seam (the console send arrives in slice 4)."""

    to: str
    name: str
    queue: str
    queue_email: str
    verify_url: str


#: Dispatch one verify mail; returns whether it was sent now. ``None`` (no sender
#: wired) leaves the signup in the durable outbox for a later flush.
VerifySender = Callable[[VerifySend], Awaitable[bool]]


def _validate(
    email: str, name: str, queue: str, descriptor: QueueDescriptor | None
) -> str:
    """The first human-readable problem with a signup, or ``""`` if it's good."""
    if not name:
        return "Enter your full name."
    if not email_domain(email):
        return "Enter a valid email address."
    if descriptor is None:
        return "That queue isn't available right now — try again shortly."
    if not domain_allowed(email, descriptor.queue_email):
        return f"Use your {descriptor.domain} email address to join this queue."
    return ""


async def _form(request: Any) -> dict[str, str]:
    """Parse the urlencoded sign-up form ourselves — the page posts a plain
    ``application/x-www-form-urlencoded`` body (no file uploads), so stdlib parsing
    avoids pulling in ``python-multipart`` just for ``request.form()``."""
    body = await request.body()
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _verify_url(request: Any, config: EmailVerifyConfig, token: str) -> str:
    """The verify link: the configured public URL (correct behind a TLS proxy),
    falling back to the request's own base URL."""
    base = (config.public_url or str(request.base_url)).rstrip("/")
    return f"{base}/verify?token={token}"


def signup_routes(
    hub: Hub,
    enrollment: EnrollmentStore,
    config: EmailVerifyConfig,
    *,
    verify_sender: VerifySender | None = None,
) -> list[Route]:
    """Build the email-verify routes bound to ``hub`` (the live queue directory),
    ``enrollment`` (members + outbox), and ``config`` (the signing secret + TTL)."""

    async def queues(_request: Any) -> JSONResponse:
        """The live sign-up list — the advertised queues a visitor may join."""
        return JSONResponse(
            {
                "queues": [
                    {"queue": d.queue, "label": d.label, "domain": d.domain}
                    for d in hub.advertised_queues()
                ]
            }
        )

    async def signup(request: Any) -> Any:
        if request.method != "POST":
            return HTMLResponse(_render_signup(hub.advertised_queues()))
        form = await _form(request)
        email = (form.get("email") or "").strip().lower()
        name = (form.get("name") or "").strip()
        queue = (form.get("queue") or "").strip()
        descriptor = hub.queue_descriptor(queue)
        problem = _validate(email, name, queue, descriptor)
        if problem:
            return HTMLResponse(
                _render_signup(
                    hub.advertised_queues(),
                    error=problem,
                    email=email,
                    name=name,
                    queue=queue,
                ),
                status_code=400,
            )
        assert descriptor is not None  # _validate guarantees it
        nonce = make_nonce()
        enrollment.add_pending(
            PendingSignup(
                nonce=nonce,
                email=email,
                name=name,
                queue=queue,
                created_at=time.time(),
            )
        )
        if verify_sender is not None:
            token = sign_verify_token(
                config.session_secret,
                {"nonce": nonce, "email": email, "name": name, "queue": queue},
            )
            send = VerifySend(
                to=email,
                name=name,
                queue=queue,
                queue_email=descriptor.queue_email,
                verify_url=_verify_url(request, config, token),
            )
            try:
                sent = await verify_sender(send)
            except Exception as exc:  # noqa: BLE001 — never 500 a signup on send
                logger.warning("room: verify send failed for %r: %s", email, exc)
                sent = False
            if sent:
                enrollment.mark_sent(nonce)
        else:
            logger.info(
                "room: verify mail for %r queued (no console send wired yet)", email
            )
        return HTMLResponse(_render_sent(email, descriptor.label))

    async def verify(request: Any) -> Any:
        token = (request.query_params.get("token") or "").strip()
        payload = (
            read_verify_token(config.session_secret, token, config.token_ttl)
            if token
            else None
        )
        nonce = payload.get("nonce", "") if isinstance(payload, dict) else ""
        # Burn the nonce: a valid token whose row is already gone (second click,
        # expired-and-swept) fails here — that's what makes the link single-use.
        pending = enrollment.consume_pending(nonce) if nonce else None
        if pending is None:
            return HTMLResponse(_render_verify_failed(), status_code=400)
        enrollment.add_member(
            Member(
                email=pending.email,
                queue=pending.queue,
                name=pending.name,
                verified_at=time.time(),
            )
        )
        # The verified email is the session identity the WS/catalog gate reads;
        # the name rides along for the operator card (slice 5).
        request.session["user"] = pending.email
        request.session["name"] = pending.name
        logger.info("room: %r verified into %r", pending.email, pending.queue)
        return RedirectResponse(url=f"/?queue={pending.queue}", status_code=303)

    return [
        Route("/queues", queues, name="queues"),
        Route("/signup", signup, methods=["GET", "POST"], name="signup"),
        Route("/verify", verify, name="verify"),
    ]


# ── server-rendered pages (self-contained, no external assets) ────────────────

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
   margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
 .card{{background:#1e293b;padding:2rem;border-radius:12px;max-width:26rem;width:90%;
   box-shadow:0 10px 30px rgba(0,0,0,.4)}}
 h1{{font-size:1.3rem;margin:0 0 1rem}}
 label{{display:block;margin:.8rem 0 .25rem;font-size:.9rem;color:#94a3b8}}
 input,select{{width:100%;padding:.55rem;border-radius:8px;border:1px solid #334155;
   background:#0f172a;color:#e2e8f0;box-sizing:border-box;font-size:1rem}}
 button{{margin-top:1.25rem;width:100%;padding:.6rem;border:0;border-radius:8px;
   background:#3b82f6;color:#fff;font-size:1rem;cursor:pointer}}
 button:hover{{background:#2563eb}}
 .err{{background:#7f1d1d;color:#fecaca;padding:.6rem .8rem;border-radius:8px;
   margin-bottom:1rem;font-size:.9rem}}
 .muted{{color:#94a3b8;font-size:.9rem}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _render_signup(
    queues: list[QueueDescriptor],
    *,
    error: str = "",
    email: str = "",
    name: str = "",
    queue: str = "",
) -> str:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    if queues:
        options = "".join(
            f'<option value="{html.escape(d.queue)}"'
            f"{' selected' if d.queue == queue else ''}>"
            f"{html.escape(d.label)} ({html.escape(d.domain)})</option>"
            for d in queues
        )
        select = f'<select name="queue" required><option value="">Choose a queue…</option>{options}</select>'
        disabled = ""
    else:
        # No console is advertising a queue right now — nothing to join.
        select = '<select name="queue" disabled><option>No queues available</option></select>'
        disabled = " disabled"
    body = (
        f"<h1>Join a request queue</h1>{err}"
        '<p class="muted">Sign up with your company email to request actions a '
        "team operator runs for you.</p>"
        '<form method="post" action="/signup">'
        "<label>Queue</label>" + select + "<label>Full name</label>"
        f'<input name="name" value="{html.escape(name)}" '
        'placeholder="Jane Doe" required>'
        "<label>Company email</label>"
        f'<input name="email" type="email" value="{html.escape(email)}" '
        'placeholder="jane@corp.com" required>'
        f'<button type="submit"{disabled}>Send verification link</button>'
        "</form>"
    )
    return _PAGE.format(title="Join a queue", body=body)


def _render_sent(email: str, label: str) -> str:
    body = (
        "<h1>Check your email</h1>"
        f"<p>If <strong>{html.escape(email)}</strong> can join "
        f"<strong>{html.escape(label)}</strong>, we've sent a verification link to "
        "that address. Click it to finish joining.</p>"
        '<p class="muted">The link expires shortly and can be used once.</p>'
    )
    return _PAGE.format(title="Check your email", body=body)


def _render_verify_failed() -> str:
    body = (
        "<h1>Link expired or already used</h1>"
        '<p class="muted">Verification links can be used once and expire after a '
        "short time.</p>"
        '<p><a href="/signup" style="color:#60a5fa">Sign up again</a></p>'
    )
    return _PAGE.format(title="Verification failed", body=body)
