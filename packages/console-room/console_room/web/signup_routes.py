"""
web/signup_routes.py — the email-verify sign-up door (``/queues``, ``/signup``,
``/verify``).

The browser half of the email-verify auth mode (docs/design/email-queue-signup.md
§3): an internal employee picks an advertised queue, gives their company email +
full name, and the hub writes a **pending signup** row and dispatches a verify mail
*via a connected console* (`Hub.submit_signup`; left in the durable outbox when no
console for the queue is connected). Clicking the signed, single-use verify link
enrolls them: `Hub.verify_token` burns the nonce + persists the member, and this
route sets the signed session cookie the app gates on — so the existing
``session_user`` gate on ``/ws`` and ``/catalog`` needs no change.

The routes are thin: identity + the outbox state machine live on the hub (so the
flush-on-connect and the console ack share them); this module is just the HTTP
surface + the server-rendered HTML. Server-rendered (no build step, no external
assets) so the page works without JavaScript and is exercised end-to-end by the
TestClient. The actual Outlook mail send is the console's job (slice 4).
"""

from __future__ import annotations

import html
import urllib.parse
from typing import Any

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from ..hub import Hub
from ..queues import QueueDescriptor
from ..signup import domain_allowed, email_domain


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


def signup_routes(hub: Hub) -> list[Route]:
    """Build the email-verify routes bound to ``hub`` (which owns the queue
    directory, the outbox, and the verify/enrol logic)."""

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
        # The hub records the pending row + dispatches (or queues) the verify mail.
        await hub.submit_signup(email, name, queue)
        return HTMLResponse(_render_sent(email, descriptor.label))

    async def verify(request: Any) -> Any:
        token = (request.query_params.get("token") or "").strip()
        member = hub.verify_token(token) if token else None
        if member is None:
            return HTMLResponse(_render_verify_failed(), status_code=400)
        # The verified email is the session identity the WS/catalog gate reads;
        # the name rides along for the operator card (slice 5).
        request.session["user"] = member.email
        request.session["name"] = member.name
        return RedirectResponse(url=f"/?queue={member.queue}", status_code=303)

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
