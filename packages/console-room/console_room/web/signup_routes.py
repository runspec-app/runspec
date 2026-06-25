"""
web/signup_routes.py — the email-verify **login** door (``/login``, ``/verify``).

The browser half of the email-verify auth mode (docs/design/email-queue-signup.md
+ teams-and-email-monitoring.md). Access is **not permanent**: you log in by
verifying a company email (a link sent *via a connected console* as the room
mailbox), the signed session that grants access **expires** (``session_ttl``), and
you renew it the same way. One room = one helpdesk = one queue (its identity is the
room's ``--mailbox``), so the visitor doesn't pick a queue — a first-time visitor
gives their **team** (department) + full name + company email; a returning visitor
is recognised by a remembered-email cookie and logs back in with one click (their
stored name + team are reused). The hub writes a **pending login** for the single
queue and dispatches a verify mail via a connected console (left in the durable
outbox when none is connected). Clicking the signed, single-use link logs them in:
``Hub.verify_token`` burns the nonce + persists the member, and this route sets the
signed session cookie the app gates on, plus the remembered-email cookie.

The page is server-rendered (no build step, no external assets) so it works without
JavaScript and is exercised end-to-end by the TestClient. The actual Outlook mail
send is the console's job.
"""

from __future__ import annotations

import html
import urllib.parse
from typing import Any

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from ..hub import Hub
from ..signup import (
    EmailVerifyConfig,
    Team,
    domain_allowed,
    email_domain,
    is_operator,
    login_domains,
)

__all__ = ["login_routes", "EMAIL_COOKIE"]

#: A long-lived, non-secret cookie remembering the last verified email so a
#: returning visitor doesn't retype it (and is offered the one-click login). It is
#: a convenience pre-fill only — the signed session cookie is the sole credential.
EMAIL_COOKIE = "room_email"
_EMAIL_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # a year


def _domains_phrase(domains: list[str]) -> str:
    """Join allowed domains for the hint/error: ``["a"]`` → ``"a"``; ``["a","b"]`` →
    ``"a or b"``; ``["a","b","c"]`` → ``"a, b, or c"``."""
    if len(domains) <= 1:
        return domains[0] if domains else ""
    if len(domains) == 2:
        return f"{domains[0]} or {domains[1]}"
    return f"{', '.join(domains[:-1])}, or {domains[-1]}"


def _validate(
    email: str,
    name: str,
    team: str,
    mailbox: str,
    allowed_domains: tuple[str, ...],
    team_slugs: set[str],
) -> str:
    """The first human-readable problem with a login, or ``""`` if it's good."""
    if not mailbox:
        return "Login isn't configured for this helpdesk yet."
    if not name:
        return "Enter your full name."
    if team not in team_slugs:
        return "Choose your team."
    if not email_domain(email):
        return "Enter a valid email address."
    if not domain_allowed(email, mailbox, allowed_domains):
        phrase = _domains_phrase(login_domains(mailbox, allowed_domains))
        return f"Use your {phrase} email address to log in."
    return ""


async def _form(request: Any) -> dict[str, str]:
    """Parse the urlencoded login form ourselves — the page posts a plain
    ``application/x-www-form-urlencoded`` body (no file uploads), so stdlib parsing
    avoids pulling in ``python-multipart`` just for ``request.form()``."""
    body = await request.body()
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def login_routes(hub: Hub, email_verify: EmailVerifyConfig) -> list[Route]:
    """Build the email-verify login routes bound to ``hub`` (which owns the team
    list, the room mailbox identity, the outbox, and the verify/enrol logic).
    ``email_verify`` supplies the ``Secure``-cookie flag for the remembered-email
    cookie."""

    def _remembered_member(request: Any) -> Any:
        """The verified member behind the ``room_email`` cookie, when that email is
        still a known member *and* its stored team still exists — the only case the
        one-click "welcome back" path is offered. ``None`` otherwise (show the full
        form). Returns the :class:`~console_room.signup.Member`."""
        email = (request.cookies.get(EMAIL_COOKIE) or "").strip().lower()
        if not email:
            return None
        member = hub.find_member(email)
        if member is None:
            return None
        slugs = {t.slug for t in hub.list_teams()}
        return member if member.team in slugs else None

    async def login(request: Any) -> Any:
        team_list = hub.list_teams()
        domains = login_domains(hub.mailbox, email_verify.allowed_domains)
        switch = request.query_params.get("switch") == "1"
        remembered_email = (request.cookies.get(EMAIL_COOKIE) or "").strip().lower()
        member = None if switch else _remembered_member(request)

        if request.method != "POST":
            if member is not None:
                return HTMLResponse(_render_welcome_back(member.email, member.name))
            # "Use a different email" clears the prefill; otherwise offer it.
            prefill = "" if switch else remembered_email
            return HTMLResponse(_render_login(team_list, domains, email=prefill))

        form = await _form(request)
        # The one-click path: a known email (from the cookie) reuses its name + team.
        if (form.get("mode") or "") == "known" and member is not None:
            email, name, team = member.email, member.name, member.team
        else:
            email = (form.get("email") or "").strip().lower()
            name = (form.get("name") or "").strip()
            team = (form.get("team") or "").strip()
            slugs = {t.slug for t in team_list}
            problem = _validate(
                email, name, team, hub.mailbox, email_verify.allowed_domains, slugs
            )
            if problem:
                return HTMLResponse(
                    _render_login(
                        team_list,
                        domains,
                        error=problem,
                        email=email,
                        name=name,
                        team=team,
                    ),
                    status_code=400,
                )
        # The hub records the pending row + dispatches (or queues) the verify mail.
        await hub.submit_signup(email, name, team)
        return HTMLResponse(_render_sent(email))

    async def teams(_request: Any) -> JSONResponse:
        """The customer teams a visitor may pick when logging in."""
        return JSONResponse(
            {"teams": [{"slug": t.slug, "name": t.name} for t in hub.list_teams()]}
        )

    async def signup_redirect(request: Any) -> RedirectResponse:
        """``/signup`` is the old name for the login door — kept as a redirect so
        existing links / docs / the console smoke test don't 404."""
        return RedirectResponse(url="/login", status_code=307)

    async def verify(request: Any) -> Any:
        token = (request.query_params.get("token") or "").strip()
        member = hub.verify_token(token) if token else None
        if member is None:
            return HTMLResponse(_render_verify_failed(), status_code=400)
        # The verified email is the session identity the WS/catalog gate reads; the
        # name + team ride along for the operator card.
        request.session["user"] = member.email
        request.session["name"] = member.name
        request.session["team"] = member.team
        # Whether this verified email is an operator (allowlisted) — unlocks the full
        # catalogue + runbooks. Stamped once at verify so the gate reads it cheaply.
        request.session["operator"] = is_operator(member.email, email_verify.operators)
        response = RedirectResponse(url=f"/?queue={member.queue}", status_code=303)
        # Remember the proven email so next time they log in with one click.
        response.set_cookie(
            EMAIL_COOKIE,
            member.email,
            max_age=_EMAIL_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=email_verify.secure_cookies,
        )
        return response

    return [
        Route("/teams", teams, name="teams"),
        Route("/login", login, methods=["GET", "POST"], name="login"),
        Route("/signup", signup_redirect, name="signup"),
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
 a{{color:#60a5fa}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _render_login(
    teams: list[Team],
    domains: list[str],
    *,
    error: str = "",
    email: str = "",
    name: str = "",
    team: str = "",
) -> str:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    options = "".join(
        f'<option value="{html.escape(t.slug)}"'
        f"{' selected' if t.slug == team else ''}>"
        f"{html.escape(t.name)}</option>"
        for t in teams
    )
    select = (
        f'<select name="team" required><option value="">Choose your team…</option>'
        f"{options}</select>"
    )
    phrase = _domains_phrase(domains)
    hint = f" with your {html.escape(phrase)} email" if phrase else ""
    body = (
        f"<h1>Log in to the helpdesk</h1>{err}"
        f'<p class="muted">Log in{hint} to request actions a team operator runs '
        "for you. Access is by email link and renews periodically.</p>"
        '<form method="post" action="/login">'
        "<label>Team</label>" + select + "<label>Full name</label>"
        f'<input name="name" value="{html.escape(name)}" '
        'placeholder="Jane Doe" required>'
        "<label>Company email</label>"
        f'<input name="email" type="email" value="{html.escape(email)}" '
        'placeholder="jane@corp.com" required>'
        '<button type="submit">Send login link</button>'
        "</form>"
    )
    return _PAGE.format(title="Log in to the helpdesk", body=body)


def _render_welcome_back(email: str, name: str) -> str:
    """The one-click login for a remembered, known visitor — no form to refill."""
    who = html.escape(name or email)
    body = (
        f"<h1>Welcome back, {who}</h1>"
        f'<p class="muted">We\'ll send a login link to '
        f"<strong>{html.escape(email)}</strong>. Click it to pick up where you "
        "left off.</p>"
        '<form method="post" action="/login">'
        '<input type="hidden" name="mode" value="known">'
        '<button type="submit">Send login link</button>'
        "</form>"
        '<p class="muted" style="margin-top:1rem">'
        '<a href="/login?switch=1">Use a different email</a></p>'
    )
    return _PAGE.format(title="Welcome back", body=body)


def _render_sent(email: str) -> str:
    body = (
        "<h1>Check your email</h1>"
        f"<p>If <strong>{html.escape(email)}</strong> can log in, we've sent a "
        "login link to that address. Click it to finish.</p>"
        '<p class="muted">The link expires shortly and can be used once.</p>'
    )
    return _PAGE.format(title="Check your email", body=body)


def _render_verify_failed() -> str:
    body = (
        "<h1>Link expired or already used</h1>"
        '<p class="muted">Login links can be used once and expire after a '
        "short time.</p>"
        '<p><a href="/login">Log in again</a></p>'
    )
    return _PAGE.format(title="Login failed", body=body)
