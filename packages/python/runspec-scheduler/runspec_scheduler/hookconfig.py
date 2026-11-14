"""hookconfig.py — webhook settings and hooks for one venv.

Two places hold hooks, read together:

* **console-made** — ``[[hook]]`` tables in ``runspec_scheduler.toml`` (written
  by ``runspec-scheduler hooks save``, which runspec-console calls);
* **deployed** — ``{config dir}/hooks.d/*.toml`` (Ansible from GitLab, say).
  Read-only to the console, and a deployed hook wins when both define a name
  (the console copy is listed as ``overridden``). A file there must belong to
  the venv owner (or root) and not be group/other-writable, or it is skipped.

``[hooks]`` holds the listener settings and ``[smtp]`` the mail relay for
approvals nobody picked up. Hooks never hold a secret: only the SHA-256 of a
token (``token_sha256`` — tokens are long and random, so a fast hash is
enough) and a salted, slow PBKDF2 hash of a basic-auth password
(``password_hash`` — a person may choose that one).

Stdlib only; the TOML writer is hand-rolled for these shapes (see
:mod:`runspec_scheduler.config`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hooktemplate as tpl
from .config import parse_duration, read_config, rewrite_sections, toml_value

#: A hook name: it's in the URL path and the run label ``hook:<name>``.
HOOK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: The label prefix a hook's runs carry in the runs index.
HOOK_LABEL_PREFIX = "hook:"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_WEBHOOK_APPROVED = "webhook:approved"

DEFAULT_TOKEN_HEADER = "X-Runspec-Token"
HOOKS_DIR_NAME = "hooks.d"

_SIZE_UNITS = {"k": 1024, "m": 1024 * 1024}


def parse_size(value: Any, what: str) -> int:
    """Bytes from ``262144`` / ``"256k"`` / ``"1m"``."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a size like 256k (got {value!r})")
    if isinstance(value, int):
        n = value
    else:
        text = str(value).strip().lower()
        mult = 1
        if text and text[-1] in _SIZE_UNITS:
            mult, text = _SIZE_UNITS[text[-1]], text[:-1]
        try:
            n = int(text) * mult
        except ValueError:
            raise ValueError(f"{what} must be a size like 256k or 1m (got {value!r})") from None
    if n <= 0:
        raise ValueError(f"{what} must be more than 0 (got {value!r})")
    return n


def _int(table: dict[str, Any], key: str, default: int, what: str, *, low: int = 0, high: int | None = None) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be a whole number (got {value!r})")
    if value < low or (high is not None and value > high):
        raise ValueError(f"{what} must be between {low} and {high} (got {value})" if high is not None else f"{what} must be at least {low} (got {value})")
    return value


def _bool(table: dict[str, Any], key: str, default: bool, what: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false (got {value!r})")
    return value


def _str_list(value: Any, what: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(p for p in re.split(r"[,\s]+", value) if p)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(v.strip() for v in value if v.strip())
    raise ValueError(f"{what} must be a list of strings (got {value!r})")


def _networks(value: Any, what: str) -> tuple[Any, ...]:
    out = []
    for item in _str_list(value, what):
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"{what}: {item!r} isn't an address or network like 10.1.2.3 or 10.0.0.0/8") from None
    return tuple(out)


# ── [hooks] and [smtp] ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HooksSettings:
    """The ``[hooks]`` table: the listener and its limits."""

    enabled: bool = False
    port: int = 0
    bind: str = "127.0.0.1"
    base_path: str = ""
    cert: str = ""
    key: str = ""
    max_body: int = 256 * 1024
    keep: int = 7 * 86400
    max_connections: int = 64
    rate_per_minute: int = 120
    source_rate_per_minute: int = 600
    trusted_proxies: tuple[Any, ...] = ()
    approve_within: int = 3600
    expect_claim_within: int = 15 * 60
    max_runs: int = 4
    max_queue: int = 1000
    min_free_mb: int = 100

    @property
    def tls(self) -> bool:
        return bool(self.cert)

    def listen_problem(self) -> str:
        """Why the listener can't start with these settings, or ""."""
        if not self.enabled:
            return "webhooks are off ([hooks] enabled = false)"
        if not self.port:
            return "[hooks] port isn't set"
        if not self.tls and not _is_loopback(self.bind):
            return f"[hooks] bind = {self.bind!r} needs a certificate (cert and key): plain HTTP is only allowed on 127.0.0.1, behind a proxy that does TLS"
        return ""

    def url(self, host: str, name: str) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{host}:{self.port}{self.base_path}/hook/{name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "port": self.port,
            "bind": self.bind,
            "base_path": self.base_path,
            "cert": self.cert,
            "key": self.key,
            "tls": self.tls,
            "max_body": self.max_body,
            "keep": self.keep,
            "max_connections": self.max_connections,
            "rate_per_minute": self.rate_per_minute,
            "source_rate_per_minute": self.source_rate_per_minute,
            "trusted_proxies": [str(n) for n in self.trusted_proxies],
            "approve_within": self.approve_within,
            "expect_claim_within": self.expect_claim_within,
            "max_runs": self.max_runs,
            "max_queue": self.max_queue,
            "min_free_mb": self.min_free_mb,
        }


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _base_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "/":
        return ""
    if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+/?", text if text.startswith("/") else "/" + text):
        raise ValueError(f"[hooks] base_path must be a plain path like /runspec/ops (got {value!r})")
    return ("/" + text.strip("/")) if text else ""


def parse_hooks_settings(table: Any) -> HooksSettings:
    """Validate + parse ``[hooks]`` (absent = defaults, webhooks off)."""
    if table is None:
        table = {}
    if not isinstance(table, dict):
        raise ValueError("[hooks] must be a table")
    cert = str(table.get("cert") or "").strip()
    key = str(table.get("key") or "").strip()
    if bool(cert) != bool(key):
        raise ValueError("[hooks] cert and key go together: set both, or neither")
    return HooksSettings(
        enabled=_bool(table, "enabled", False, "[hooks] enabled"),
        port=_int(table, "port", 0, "[hooks] port", high=65535),
        bind=str(table.get("bind") or "127.0.0.1").strip(),
        base_path=_base_path(table.get("base_path")),
        cert=cert,
        key=key,
        max_body=parse_size(table.get("max_body", 256 * 1024), "[hooks] max_body"),
        keep=max(3600, parse_duration(table.get("keep", "7d"), "[hooks] keep")),
        max_connections=_int(table, "max_connections", 64, "[hooks] max_connections", low=1, high=4096),
        rate_per_minute=_int(table, "rate_per_minute", 120, "[hooks] rate_per_minute", low=1),
        source_rate_per_minute=_int(table, "source_rate_per_minute", 600, "[hooks] source_rate_per_minute", low=1),
        trusted_proxies=_networks(table.get("trusted_proxies"), "[hooks] trusted_proxies"),
        approve_within=max(60, parse_duration(table.get("approve_within", "1h"), "[hooks] approve_within")),
        expect_claim_within=parse_duration(table.get("expect_claim_within", "15m"), "[hooks] expect_claim_within"),
        max_runs=_int(table, "max_runs", 4, "[hooks] max_runs", low=1, high=64),
        max_queue=_int(table, "max_queue", 1000, "[hooks] max_queue", low=10),
        min_free_mb=_int(table, "min_free_mb", 100, "[hooks] min_free_mb"),
    )


@dataclass(frozen=True)
class SmtpSettings:
    """``[smtp]``: the relay that mails an approval nobody picked up."""

    relay: str = ""
    port: int = 25
    sender: str = ""
    to: tuple[str, ...] = ()
    starttls: bool = False
    timeout: int = 10

    @property
    def configured(self) -> bool:
        return bool(self.relay and self.sender)

    def to_dict(self) -> dict[str, Any]:
        return {"relay": self.relay, "port": self.port, "from": self.sender, "to": list(self.to), "starttls": self.starttls, "timeout": self.timeout}


def parse_smtp(table: Any) -> SmtpSettings:
    if table is None:
        table = {}
    if not isinstance(table, dict):
        raise ValueError("[smtp] must be a table")
    return SmtpSettings(
        relay=str(table.get("relay") or "").strip(),
        port=_int(table, "port", 25, "[smtp] port", low=1, high=65535),
        sender=str(table.get("from") or "").strip(),
        to=_str_list(table.get("to"), "[smtp] to"),
        starttls=_bool(table, "starttls", False, "[smtp] starttls"),
        timeout=_int(table, "timeout", 10, "[smtp] timeout", low=1, high=120),
    )


# ── [[hook]] ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """One ``[[hook.rule]]``: when its patterns match, what happens."""

    name: str
    match: dict[str, re.Pattern[str]] = field(default_factory=dict)
    run: str = ""
    command: tuple[str, ...] = ()
    args: dict[str, Any] = field(default_factory=dict)
    skip: bool = False
    require_approval: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def action(self) -> str:
        return "skip" if self.skip else ("run" if self.run else "event")


@dataclass(frozen=True)
class Hook:
    """One webhook: where it's checked, how a body becomes a run."""

    name: str
    enabled: bool = True
    description: str = ""
    token_sha256: str = ""
    token_header: str = DEFAULT_TOKEN_HEADER
    username: str = ""
    password_hash: str = ""
    allow: tuple[Any, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)
    split: str = ""
    rules: tuple[Rule, ...] = ()
    dedup: str = ""
    dedup_window: int = 600
    require_approval: bool = False
    approve_within: int | None = None
    expect_claim_within: int | None = None
    email_to: tuple[str, ...] = ()
    max_running: int = 1
    skip_if_running: bool = False
    timeout: int | None = None
    rate_per_minute: int | None = None
    transform: str = ""
    managed_by: str = ""
    source: str = "console"  # console | deployed
    file: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def check_token(self, token: str) -> bool:
        return bool(self.token_sha256) and bool(token) and hmac.compare_digest(sha256(token), self.token_sha256)

    def check_basic(self, username: str, password: str) -> bool:
        if not (self.username and self.password_hash and password) or not hmac.compare_digest(username, self.username):
            return False
        return _password_matches(password, self.password_hash)

    def allows(self, address: str) -> bool:
        if not self.allow:
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in net for net in self.allow)

    def summary(self) -> dict[str, Any]:
        """What a listing shows (never a hash)."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "description": self.description,
            "source": self.source,
            "file": self.file,
            "managed_by": self.managed_by,
            "auth": [a for a, on in (("token", bool(self.token_sha256)), ("basic", bool(self.username))) if on],
            "token_header": self.token_header,
            "allow": [str(n) for n in self.allow],
            "fields": dict(self.fields),
            "split": self.split,
            "rules": [{"name": r.name, "action": r.action, "run": r.run, "command": list(r.command), "match": {k: p.pattern for k, p in r.match.items()}} for r in self.rules],
            "dedup": self.dedup,
            "require_approval": self.require_approval,
            "transform": self.transform,
        }


def sha256(text: str) -> str:
    """SHA-256 hex of a token. Only for long random tokens; a password goes
    through :func:`hash_password`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PBKDF2_ITERATIONS = 600_000
_PW_HASH_RE = re.compile(r"^pbkdf2_sha256\$(\d{1,9})\$([A-Za-z0-9+/=]{8,})\$([A-Za-z0-9+/=]{40,})$")


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    """``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (base64) for a basic-auth
    password: salted and slow, so a copy of the config doesn't give the
    password away to a guessing run."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    m = _PW_HASH_RE.match(encoded)
    if not m:
        return False
    try:
        salt = base64.b64decode(m.group(2), validate=True)
        want = base64.b64decode(m.group(3), validate=True)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(m.group(1)))
    return hmac.compare_digest(got, want)


# A sender sends the same password with every delivery; checking it costs
# ~0.3 s, so the last password that matched each hash is kept in memory (never
# written anywhere) and compared directly next time.
_matched: dict[str, str] = {}
_matched_lock = threading.Lock()


def _password_matches(password: str, encoded: str) -> bool:
    with _matched_lock:
        known = _matched.get(encoded)
    if known is not None and hmac.compare_digest(known.encode("utf-8"), password.encode("utf-8")):
        return True
    if not verify_password(password, encoded):
        return False
    with _matched_lock:
        if len(_matched) >= 256:
            _matched.clear()
        _matched[encoded] = password
    return True


def new_token() -> str:
    """A fresh random token (shown once; only its hash is kept)."""
    return secrets.token_urlsafe(32)


_HOOK_KEYS = {
    "name", "enabled", "description", "token_sha256", "token_header", "username", "password_hash", "allow",
    "fields", "split", "rule", "run", "command", "args", "dedup", "dedup_window", "require_approval",
    "approve_within", "expect_claim_within", "email_to", "max_running", "skip_if_running", "timeout",
    "rate_per_minute", "transform", "managed_by",
}  # fmt: skip
_RULE_KEYS = {"name", "match", "run", "command", "args", "skip", "require_approval"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _command(raw: Any, what: str) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, list) and all(isinstance(p, str) for p in raw):
        return tuple(p.strip() for p in raw if p.strip())
    raise ValueError(f"{what} must be a string or a list of strings")


def _parse_rule(raw: Any, hook: str, index: int) -> Rule:
    what = f"hook {hook!r} rule {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be a table")
    unknown = sorted(set(raw) - _RULE_KEYS)
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {', '.join(unknown)}")
    name = str(raw.get("name") or f"rule-{index + 1}").strip()
    match_raw = raw.get("match") or {}
    if not isinstance(match_raw, dict):
        raise ValueError(f"{what}: match must be a table of field = regex")
    patterns: dict[str, re.Pattern[str]] = {}
    for fname, pattern in match_raw.items():
        try:
            patterns[str(fname)] = re.compile(str(pattern))
        except re.error as exc:
            raise ValueError(f"{what}: match.{fname}: {exc}") from None
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError(f"{what}: args must be a table")
    problem = tpl.check_template(args)
    if problem:
        raise ValueError(f"{what}: {problem}")
    run = str(raw.get("run") or "").strip()
    skip = bool(raw.get("skip", False))
    if skip and run:
        raise ValueError(f"{what}: a rule either skips or runs, not both")
    if (args or raw.get("command")) and not run:
        raise ValueError(f'{what}: command/args need run = "<runnable>"')
    return Rule(
        name=name,
        match=patterns,
        run=run,
        command=_command(raw.get("command"), f"{what} command"),
        args=dict(args),
        skip=skip,
        require_approval=bool(raw.get("require_approval", False)),
        raw=dict(raw),
    )


def _opt_duration(raw: dict[str, Any], key: str, what: str) -> int | None:
    if key not in raw or raw[key] in (None, ""):
        return None
    return parse_duration(raw[key], what)


def parse_hook(raw: Any, *, source: str = "console", file: str = "") -> Hook:
    """Validate + parse one ``[[hook]]``. Raises ``ValueError`` saying why not."""
    if not isinstance(raw, dict):
        raise ValueError("a [[hook]] must be a table")
    name = str(raw.get("name") or "").strip()
    if not HOOK_NAME_RE.match(name):
        raise ValueError(f"hook name {name!r} must be lowercase letters, digits, '.', '_' or '-', start with a letter or digit, at most 64 long")
    what = f"hook {name!r}"
    unknown = sorted(set(raw) - _HOOK_KEYS)
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {', '.join(unknown)}")
    token_sha = str(raw.get("token_sha256") or "").strip().lower()
    pw_hash = str(raw.get("password_hash") or "").strip()
    username = str(raw.get("username") or "").strip()
    if token_sha and not _HEX64.match(token_sha):
        raise ValueError(f"{what}: token_sha256 must be the 64-character SHA-256 hex of the token, never the token itself")
    if pw_hash and not _PW_HASH_RE.match(pw_hash):
        raise ValueError(f"{what}: password_hash must be a pbkdf2_sha256$… hash, never the password itself; make one with: runspec-scheduler hooks token --basic")
    if bool(username) != bool(pw_hash):
        raise ValueError(f"{what}: basic auth needs both username and password_hash")
    if not token_sha and not username:
        raise ValueError(f"{what}: needs a token (token_sha256) or basic auth (username + password_hash); make one with: runspec-scheduler hooks token")
    fields_raw = raw.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise ValueError(f'{what}: fields must be a table of name = "path.to.value"')
    fields_map: dict[str, str] = {}
    for fname, path in fields_raw.items():
        problem = tpl.check_path(str(path))
        if problem:
            raise ValueError(f"{what}: fields.{fname}: {problem}")
        fields_map[str(fname)] = str(path)
    rules_raw = raw.get("rule") or []
    if not isinstance(rules_raw, list):
        raise ValueError(f"{what}: rule must be an array of tables ([[hook.rule]])")
    rules = [_parse_rule(r, name, i) for i, r in enumerate(rules_raw)]
    if raw.get("run") or raw.get("args") or raw.get("command"):
        # A hook-level run is the rule that applies when no other one does.
        rules.append(_parse_rule({"name": "default", "run": raw.get("run"), "command": raw.get("command"), "args": raw.get("args")}, name, len(rules)))
    dedup = str(raw.get("dedup") or "")
    problem = tpl.check_template(dedup)
    if problem:
        raise ValueError(f"{what}: dedup: {problem}")
    split = str(raw.get("split") or "").strip()
    if split and tpl.check_path(split):
        raise ValueError(f"{what}: split: {tpl.check_path(split)}")
    header = str(raw.get("token_header") or DEFAULT_TOKEN_HEADER).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise ValueError(f"{what}: token_header must be a header name like X-Runspec-Token")
    rate = raw.get("rate_per_minute")
    if rate is not None and (isinstance(rate, bool) or not isinstance(rate, int) or rate < 1):
        raise ValueError(f"{what}: rate_per_minute must be a whole number above 0")
    transform = str(raw.get("transform") or "").strip()
    return Hook(
        name=name,
        enabled=_bool(raw, "enabled", True, f"{what} enabled"),
        description=str(raw.get("description") or ""),
        token_sha256=token_sha,
        token_header=header,
        username=username,
        password_hash=pw_hash,
        allow=_networks(raw.get("allow"), f"{what} allow"),
        fields=fields_map,
        split=split,
        rules=tuple(rules),
        dedup=dedup,
        dedup_window=parse_duration(raw.get("dedup_window", "10m"), f"{what} dedup_window"),
        require_approval=_bool(raw, "require_approval", False, f"{what} require_approval"),
        approve_within=_opt_duration(raw, "approve_within", f"{what} approve_within"),
        expect_claim_within=_opt_duration(raw, "expect_claim_within", f"{what} expect_claim_within"),
        email_to=_str_list(raw.get("email_to"), f"{what} email_to"),
        max_running=_int(raw, "max_running", 1, f"{what} max_running", low=1, high=32),
        skip_if_running=_bool(raw, "skip_if_running", False, f"{what} skip_if_running"),
        timeout=_opt_duration(raw, "timeout", f"{what} timeout"),
        rate_per_minute=rate,
        transform=transform,
        managed_by=str(raw.get("managed_by") or ""),
        source=source,
        file=file,
        raw=dict(raw),
    )


# ── reading both layers ───────────────────────────────────────────────────────


@dataclass
class HookConfig:
    """Everything about this venv's webhooks, from one read."""

    settings: HooksSettings
    smtp: SmtpSettings
    hooks: dict[str, Hook]  # the active one per name (deployed wins)
    listed: list[Hook]  # every hook found, active or overridden
    overridden: set[tuple[str, str]] = field(default_factory=set)  # (source, name)
    errors: list[dict[str, str]] = field(default_factory=list)  # {file, hook, error}

    def listing(self) -> list[dict[str, Any]]:
        out = []
        for h in self.listed:
            row = h.summary()
            row["overridden"] = (h.source, h.name) in self.overridden
            # The table as written (hashes only, never a secret) for an editor
            # and for "Copy as TOML".
            row["raw"] = dict(h.raw)
            row["toml"] = render_hook(dict(h.raw)) if h.raw else ""
            out.append(row)
        return out


def hooks_dir(config_path: Path) -> Path:
    return config_path.parent / HOOKS_DIR_NAME


def deployed_file_problem(path: Path) -> str:
    """Why a hooks.d file can't be trusted, or "" (owner = us or root, not
    group/other-writable, a regular file)."""
    try:
        st = path.lstat()
    except OSError as exc:
        return f"can't read it: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_mode & 0o022:
        return "group or others can write it (chmod go-w)"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and st.st_uid not in (0, geteuid()):
        return "it belongs to another account (it must belong to the venv owner or root)"
    return ""


def read_hook_file(path: Path) -> list[dict[str, Any]]:
    """The raw ``[[hook]]`` tables of a TOML file."""
    data = read_config(path)
    hooks = data.get("hook") or []
    if not isinstance(hooks, list):
        raise ValueError("hook must be an array of tables ([[hook]])")
    return [dict(h) for h in hooks]


def check_file(path: Path) -> list[str]:
    """Every problem with a hook file (what ``hooks check`` prints; [] = good).
    Checks the content only, so Ansible's ``validate:`` can point it at a temp
    copy."""
    try:
        raws = read_hook_file(path)
    except Exception as exc:  # noqa: BLE001 — a TOML error is the answer
        return [f"{path}: {exc}"]
    problems: list[str] = []
    seen: set[str] = set()
    data = read_config(path)
    for key in ("hooks", "smtp"):
        if key in data:
            try:
                (parse_hooks_settings if key == "hooks" else parse_smtp)(data[key])
            except ValueError as exc:
                problems.append(str(exc))
    for raw in raws:
        try:
            hook = parse_hook(raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if hook.name in seen:
            problems.append(f"hook {hook.name!r} appears more than once")
        seen.add(hook.name)
    return problems


def load_hook_config(config_path: Path) -> HookConfig:
    """Settings + every hook (console layer, then hooks.d). A broken hook is
    reported in ``errors`` and left out; a broken ``[hooks]``/``[smtp]``
    raises."""
    data = read_config(config_path)
    cfg = HookConfig(settings=parse_hooks_settings(data.get("hooks")), smtp=parse_smtp(data.get("smtp")), hooks={}, listed=[])
    console: dict[str, Hook] = {}
    for raw in data.get("hook") or []:
        try:
            hook = parse_hook(raw, source="console", file=str(config_path))
        except ValueError as exc:
            cfg.errors.append({"file": str(config_path), "hook": str((raw or {}).get("name") or ""), "error": str(exc)})
            continue
        if hook.name in console:
            cfg.errors.append({"file": str(config_path), "hook": hook.name, "error": "defined more than once; the first is used"})
            continue
        console[hook.name] = hook
    deployed: dict[str, Hook] = {}
    folder = hooks_dir(config_path)
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        problem = deployed_file_problem(path)
        if problem:
            cfg.errors.append({"file": str(path), "hook": "", "error": f"skipped: {problem}"})
            continue
        try:
            raws = read_hook_file(path)
        except Exception as exc:  # noqa: BLE001 — one bad file doesn't stop the rest
            cfg.errors.append({"file": str(path), "hook": "", "error": str(exc)})
            continue
        for raw in raws:
            try:
                hook = parse_hook(raw, source="deployed", file=str(path))
            except ValueError as exc:
                cfg.errors.append({"file": str(path), "hook": str((raw or {}).get("name") or ""), "error": str(exc)})
                continue
            if hook.name in deployed:
                cfg.errors.append({"file": str(path), "hook": hook.name, "error": f"also in {deployed[hook.name].file}; the first is used"})
                continue
            deployed[hook.name] = hook
    for name, hook in console.items():
        cfg.listed.append(hook)
        if name in deployed:
            cfg.overridden.add(("console", name))
        else:
            cfg.hooks[name] = hook
    for name, hook in deployed.items():
        cfg.listed.append(hook)
        cfg.hooks[name] = hook
    return cfg


def signature(config_path: Path) -> tuple[Any, ...]:
    """Changes when the config or any hooks.d file does (for a reload)."""
    parts: list[Any] = []
    for path in [config_path, *(sorted(hooks_dir(config_path).glob("*.toml")) if hooks_dir(config_path).is_dir() else [])]:
        try:
            st = path.stat()
            parts.append((str(path), st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            parts.append((str(path), None))
    return tuple(parts)


# ── writing the console layer ─────────────────────────────────────────────────

_HOOK_SCALARS = (
    "name", "enabled", "description", "managed_by", "token_header", "token_sha256", "username", "password_hash",
    "split", "dedup", "dedup_window", "require_approval", "approve_within", "expect_claim_within", "max_running",
    "skip_if_running", "timeout", "rate_per_minute", "transform", "allow", "email_to",
)  # fmt: skip


def render_hook(raw: dict[str, Any]) -> str:
    """One ``[[hook]]`` (with its ``[[hook.rule]]`` tables) as TOML text."""
    lines = ["[[hook]]"]
    for key in _HOOK_SCALARS:
        if key in raw and raw[key] not in (None, ""):
            lines.append(f"{key} = {toml_value(raw[key])}")
    for key in ("fields",):
        if raw.get(key):
            lines.append(f"{key} = {toml_value(raw[key])}")
    if raw.get("run"):
        lines.append(f"run = {toml_value(raw['run'])}")
        if raw.get("command"):
            lines.append(f"command = {toml_value(list(_command(raw['command'], 'command')))}")
        if raw.get("args"):
            lines.append(f"args = {toml_value(raw['args'])}")
    for rule in raw.get("rule") or []:
        lines.append("")
        lines.append("[[hook.rule]]")
        for key in ("name", "skip", "require_approval", "run"):
            if key in rule and rule[key] not in (None, ""):
                lines.append(f"{key} = {toml_value(rule[key])}")
        if rule.get("command"):
            lines.append(f"command = {toml_value(list(_command(rule['command'], 'command')))}")
        for key in ("match", "args"):
            if rule.get(key):
                lines.append(f"{key} = {toml_value(rule[key])}")
    return "\n".join(lines)


def _write_console_hooks(config_path: Path, raws: list[dict[str, Any]]) -> None:
    blocks = [render_hook(r) for r in raws]
    rewrite_sections(config_path, drop={"hook"}, add=blocks)


def save_hook(config_path: Path, raw: dict[str, Any]) -> Hook:
    """Add or replace a console-made hook (validated first). A hook deployed in
    hooks.d with the same name can't be saved over."""
    raw = _clean_raw(raw)
    hook = parse_hook(raw, source="console", file=str(config_path))
    current = load_hook_config(config_path)
    deployed = next((h for h in current.listed if h.source == "deployed" and h.name == hook.name), None)
    if deployed is not None:
        raise ValueError(f"hook {hook.name!r} is deployed from {deployed.file}; change it there (it wins over a console copy)")
    raws = [r for r in (read_config(config_path).get("hook") or []) if str((r or {}).get("name") or "").strip() != hook.name]
    raws.append(raw)
    _write_console_hooks(config_path, raws)
    return hook


def _clean_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """A hook table to write: no secrets (a plain ``token``/``password`` is
    hashed), no empty values."""
    out = {k: v for k, v in raw.items() if v not in (None, "", [], {})}
    if out.get("token"):
        out["token_sha256"] = sha256(str(out["token"]))
    if out.get("password"):
        out["password_hash"] = hash_password(str(out["password"]))
    out.pop("token", None)
    out.pop("password", None)
    return out


def remove_hook(config_path: Path, name: str) -> bool:
    """Remove a console-made hook. False when there's none by that name."""
    raws = [dict(r) for r in (read_config(config_path).get("hook") or [])]
    kept = [r for r in raws if str(r.get("name") or "").strip() != name]
    if len(kept) == len(raws):
        return False
    _write_console_hooks(config_path, kept)
    return True


#: The ``[hooks]`` keys ``hooks settings`` may change.
SETTING_KEYS = (
    "enabled", "port", "bind", "base_path", "cert", "key", "max_body", "keep", "max_connections",
    "rate_per_minute", "source_rate_per_minute", "trusted_proxies", "approve_within", "expect_claim_within",
    "max_runs", "max_queue", "min_free_mb",
)  # fmt: skip
SMTP_KEYS = ("relay", "port", "from", "to", "starttls", "timeout")


def set_settings(config_path: Path, table_name: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Change ``[hooks]`` or ``[smtp]`` keys (``None`` = back to the default),
    validated before anything is written. Returns the table as written."""
    allowed = SETTING_KEYS if table_name == "hooks" else SMTP_KEYS
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise ValueError(f"not a [{table_name}] setting: {', '.join(unknown)}")
    table = dict(read_config(config_path).get(table_name) or {})
    for key, value in changes.items():
        if value is None or value == "":
            table.pop(key, None)
        else:
            table[key] = value
    (parse_hooks_settings if table_name == "hooks" else parse_smtp)(table)
    body = [f"[{table_name}]", *(f"{k} = {toml_value(v)}" for k, v in table.items())]
    rewrite_sections(config_path, drop={table_name}, add=["\n".join(body)] if table else [])
    return table


def with_secret(raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``raw`` with a freshly made token's hash in it, and the token (show it
    once; it's never stored)."""
    token = new_token()
    out = dict(raw)
    out["token_sha256"] = sha256(token)
    return out, token
