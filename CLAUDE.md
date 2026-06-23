# runspec — Claude Code Context

## What this project is

A language-agnostic, TOML-based interface specification for anything runnable.
Runnables (scripts, apps, MCP tools) define their interface in `runspec.toml`
inside the package directory. The library parses this at runtime, validates
arguments, and exposes a rich `RunSpec` object.

Read `DESIGN.md` for the full design history and rationale.
Read `spec/SPEC.md` for the canonical format specification.

---

## Repository Structure

```
runspec/
  CLAUDE.md                        ← you are here
  DESIGN.md                        ← full design document
  spec/SPEC.md                     ← canonical format specification
  packages/
    python/                        ← active development, reference implementation
      runspec/
        __init__.py                ← public API: parse(), load_spec(), register_type()
        finder.py                  ← locates runspec.toml
        loader.py                  ← reads TOML, normalises to internal dict
        inference.py               ← applies inference rules
        types.py                   ← type registry + runspec-python coercers
        validator.py               ← two-pass validation (args then groups)
        parser.py                  ← orchestrates full pipeline, returns RunSpec
        models.py                  ← Arg, Group, RunSpec dataclasses
        errors.py                  ← human-first errors with fuzzy suggestions
        cli.py                     ← discover, check, emit commands
      tests/
        test_inference.py
        test_types.py
        test_validator.py
      pyproject.toml
    node/                          ← active, published to npm as runspec-node
      src/
        cli.ts                     ← init, discover, check, emit, serve commands
        finder.ts                  ← locates runspec.toml
        loader.ts                  ← reads TOML, normalises to internal dict
        inference.ts               ← applies inference rules
        types.ts                   ← type registry + coercers
        validator.ts               ← two-pass validation
        parser.ts                  ← orchestrates pipeline, returns ParsedArgs
        serve.ts                   ← MCP stdio server
        models.ts                  ← TypeScript interfaces
        errors.ts                  ← typed error classes
      tests/
      bin/runspec.js               ← CLI entry point
    go/                            ← stub, not yet implemented
  tests/
    integration/
      fixtures/                    ← shared TOML configs all language packs test against
        simple.toml
        complex.toml
  .github/
    workflows/
      python.yml                   ← CI for Python (active)
      node.yml                     ← CI for Node (active)
      release.yml                  ← Python PyPI release + GitHub Release on v* tag
      node-release.yml             ← Node npm release + GitHub Release on node-v* tag
      go.yml                       ← stub
      integration.yml              ← compliance suite
```

---

## Key Design Decisions

**Format**
- `runspec.toml` is the only supported config format — no `pyproject.toml` support
- `runspec.toml` lives inside the package directory (e.g. `mypkg/runspec.toml`), not at the project root
- Runnables are top-level sections: `[greeter]`
- `[config]` is the only reserved name — everything else is a runnable

**Internally**
- Code uses `runnables` as the dict key (not `scripts`)
- `__script__` on RunSpec refers to the runnable's name — internal naming, not format

**Types**
- Types declared as strings in spec (`"int"`, `"path"`, etc.)
- Coercion handled by language packs — `runspec-python` is bundled with `pip install runspec`
- Custom types registered via `runspec.register_type(name, coercer)`

**Validation**
- Two-pass: individual args first, group constraints second
- Keeps both extensible independently

**Autonomy**
- Defaults to `"confirm"` — safe by default, opt-in to trust
- Resolution order: per-arg → script-level → `[config]` default → library default
- Escalation rule: most restrictive level wins

**Entry points**
- Entry point name must match the runspec runnable name — enforced by convention
- `runspec` binary auto-appears when added as a dependency
- Script discovery: venv bin only

**Discovery**
- `runspec local`, `runspec serve`: importlib.metadata only — installed packages, no filesystem scanning. `pip install -e .` to make a package visible.
- `runspec jump` takes the SSH host string directly on the command line — no local config lookup required.
- `.git` is the only project boundary marker — language agnostic (used by `runspec init --write-project` to locate the parent directory)

---

## Inference Rules (from spec/SPEC.md)

| Condition | Inference |
|---|---|
| `default = 42` | `type = "int"` |
| `default = 3.14` | `type = "float"` |
| `default = "json"` | `type = "str"` |
| `default = false` | `type = "flag"` |
| `options = [...]` | `type = "choice"` |
| No default present | `required = true` |
| `type = "path"` only | `required = true` |

Bool must be checked before int (bool is a subclass of int in Python).

---

## Python Specifics

```bash
# Requirements
Python 3.10+   (tomllib from stdlib on 3.11+; tomli dependency on 3.10)
Zero runtime dependencies on Python 3.11+

# Environment setup
cd packages/python
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
ruff format --check .

# Type check
mypy runspec/
```

**Reading args (0.38.0+):** `parse()` returns a `RunSpec`; `args.<name>` is an
`Arg`. Read `args.<name>.value` for the coerced native value — `Arg` is **not** a
transparent proxy. `if args.x:` and `args.x == y`/`!=` raise a guiding `TypeError`
(those are the silently-wrong-without-`.value` cases); arithmetic / `int()` /
iteration / indexing / `os.fspath` / attribute delegation are absent and raise.
Metadata (`.type`, `.source`, `.meta`, …) and the `runspec_*` properties are
unchanged. Use `name in args` / `args.get(name, default)` (always returns an
`Arg`) for args whose presence depends on the active subcommand — no up-front
guard needed. (Pre-0.38.0 `Arg` proxied ~30 dunders; the leak made `.value`
needed "sometimes but not always".)

`[config.logging]` (0.10.0+): `logger = logging.getLogger(__name__)` just works.
Extra fields (0.11.0): `logger.info('msg', extra={'user_id': '42'})` — standard stdlib
`extra=` API; fields appear nested under `"extra"` in JSON, appended as `{key=value}` on console.
Console routing (0.12.0): INFO → stdout (plain message, reads like `print()`);
WARNING+ → stderr with level prefix. Same in CLI and agent mode — in agent
mode `runspec serve` captures stdout as the MCP tool response, so `logger.info`
lines reach the agent automatically. File handler (JSON) is always on as
the audit trail; its level follows the same `--debug` toggle as stdout
(INFO by default — keeps third-party library DEBUG noise out of the audit
log). `[config.logging]` auto-adds a `--debug` flag (also `RUNSPEC_ARG_DEBUG=1`)
that flips DEBUG on everywhere — stdout, tracebacks, and the audit file —
in one shot. Stderr stays pinned at WARNING regardless. No `level` knob —
silencing INFO would break agent responses.

Run summary (0.33.0): two independently gated parts. `[config.logging] summary`
(default **true**) writes the structured `run_summary` record to the audit log —
the data the console history/analytics tabs and `runspec logs` read.
`summary_console` (default **false**) echoes the one-line human-readable summary
to stderr. The echo is off by default so stdout stays clean for pipes and a
clean run never lands on a stream that stream-classifying log processors (e.g.
Rundeck, which tags *all* stderr as ERROR) would flag — turning it on is the
opt-in for interactive use. Both are **package-level** controls: set `summary =
false` to drop the record. There is no per-invocation override flag — the
auto-injected `--no-summary` / `RUNSPEC_*_ARG_NO_SUMMARY` kill switch was
removed in 0.36.0 (once the echo became opt-in it mostly served to suppress the
audit record the console depends on, so it didn't earn a global flag on every
runnable). (Before 0.33.0 the record and the always-on stderr echo were fused
under `summary`; the abbreviation `errors`→`err` couldn't fix Rundeck because
Rundeck classifies by stream, not text.)

---

## Do Not

- Add runtime dependencies to the core library (stdlib only)
- Use `scripts` as a TOML key in runspec format examples
- Use `[tool.runspec.*]` format — `runspec.toml` only, always
- Put `runspec.toml` at the project root in examples — it belongs inside the package directory
- Change inference rules without updating `spec/SPEC.md` first
- Change the format without updating `spec/SPEC.md` first
- Name internal variables using the format key names (use `runnables`, not `scripts`)
- Use language-specific files (pyproject.toml, package.json) as project boundary markers — use `.git` only

---

## Node Specifics

```bash
# Requirements
Node 18+, TypeScript

# Environment setup
cd packages/node
npm install

# Run tests
npm test

# Build
npm run build

# Type check
npm run typecheck
```

Node mirrors the Python public API: `parse()`, `loadSpec()`, `registerType()`, `getLogger()`.
CLI commands: `init`, `local`, `jump`, `serve` (renamed in 0.8.0 to mirror Python).
`[config.logging]` implemented in 0.9.0 — runnables call `getLogger(name)` from `runspec-node`.
Extra fields (0.10.0): `logger.info('msg', { user_id: '42' })` — `error` key extracts an Error; all other keys appear under `"extra"` in the JSON log.
Console routing (0.11.0) mirrors Python: INFO → stdout; WARNING+ → stderr with level prefix. No `level` knob. `[config.logging]` auto-adds a `--debug` flag (`RUNSPEC_ARG_DEBUG=1`) that flips DEBUG on everywhere — stdout *and* the audit file (the file defaults to INFO, same as stdout, so external library DEBUG noise stays out of the log).
Run summary (0.12.0): same closing-line + audit-record shape as Python 0.12.3. `[config.logging] summary = true` (default on). Log file now lands at `{project_root}/logs/{runnable}.log` — the nearest ancestor `package.json` skipping `node_modules`, falling back to `~/logs/`. `runspec serve` returns a `_meta.runspec` block on every `tools/call` response (`tool`, `duration_ms`, `exit_code`). Record/console split (node-0.29.0): mirrors Python 0.33.0 — `summary` (default true) gates the audit record; `summary_console` (default false) gates the human-readable stderr echo (off by default so stdout stays clean for pipes and the line never hits Rundeck's stderr=ERROR tagging). Per-invocation `--no-summary` flag removed in node-0.32.0 (mirrors Python 0.36.0) — suppression is package-level only (`summary = false`).

---

## Current Status

Both Python and Node packages are active and published.

| Package | Version | PyPI / npm | Notes |
|---|---|---|---|
| `runspec` | 0.44.0 | PyPI | Core library (reference implementation). `runspec stubs` generates `typings/runspec/__init__.pyi` so `parse("name")` is precisely typed (see `docs/design/stubs-handoff.md`). **Denylist (0.43.0):** a venv-scoped kill switch — `parse()` reads `{sys.prefix}/runspec_denylist.toml` (`RUNSPEC_DENYLIST_FILE` override) on every run and refuses runnables matched by name/effective-user/hostname globs (`disabled = true` = all; `mode` enforce/warn/off is authoritative, no per-runnable opt-out). The reserved `runspec` name is exempt so the control plane never self-locks; edit via the core `runspec deny add/all/list/clear/mode` subcommand (not a runnable, so it can't disable itself). See `runspec/denylist.py`, `spec/SPEC.md` → "Denylist File", `docs/denylist.md`. **Serve leaf globals (0.44.0):** when a runnable has subcommands, each leaf MCP tool emitted by `runspec serve` now advertises the *effective* arg set (inherited globals merged with the command's own args, child-wins), so agents can set inherited globals per call — `serve.py` `_expand_tools`. |
| `runspec-node` | 0.33.0 | npm | Node port |
| `runspec-console` | 0.114.0 | PyPI | pywebview desktop console — agentic chat + Forms + SSH jump-host fleet UI (frontend in `packages/console-ui`); desktop toast + taskbar flash on agent confirm-gate events (Windows; `[notifications]` extra); **Rooms** chat-room tab + `chat` trigger source — **always-on, multi-room**: rooms are a property of a fleet host (`room`/`room_port` on the host entry), joined over that host's own SSH tunnel, reconnecting, independent of triggers. `room_via = "exec"` runs `console-room connect` over an SSH exec channel for networks that block port-forwarding. **Helpdesk agent mode** (0.44.0): `room_queue` on a host entry staffs that queue — inbox of private visitor conversations with claim/resolve controls, **claim-gated agent turns** (no model spend on unclaimed threads), and a **self-service catalog publisher** (`discoverable ∋ "self-service"` runnables, optional `room_catalog` allowlist) — see `docs/design/helpdesk-rooms.md`. **Email-verify (room sign-up)**: a helpdesk console advertises `room_queue_email` (+ optional `room_queue_label`) on its host entry so the room offers the queue on `/signup`, and sends the verify mail via Outlook **send-as the queue mailbox** (Windows). See `docs/design/email-queue-signup.md`. **Credentials** (Settings → Credentials; `[credentials]` extra): named credentials whose **secret values live in the OS keychain** (Windows Credential Manager / macOS Keychain / Secret Service) via `keyring` — only metadata is written to `runspec_credentials.toml`. **Two application paths, deliberately split (0.114.0):** (1) **Forms (humans)** never copy a credential into a runnable's args — instead the run dialog's credential section injects **name-derived plain env vars** from the credential's label + kind by a strict convention (label `Jira` + userpass → `JIRA_USERNAME`/`JIRA_PASSWORD`; `Jira Dev` → `JIRA_DEV_USERNAME`/`JIRA_DEV_PASSWORD`; a `token` cred `Deploy` → `DEPLOY_TOKEN`), **multiple credentials per run** — name a credential to match what the runnable reads from the environment. A required arg — **including a `password`-typed one** — must be typed by the operator (credential reuse goes through the env-var channel, which targets runnables that read bare env vars, not required runspec args). The form remembers the operator's credential picks per runnable (localStorage, ids only — never secrets). (2) **Agent** keeps the old **arg-binding** fill: `username_arg`/`secret_arg` bind a credential's values to a runnable's args (username → argv, secret → the `RUNSPEC_*_ARG_*` env channel), in-scope (host/runnable glob) credentials auto-apply, an explicit `credential_id` overrides (the agent's tool gains a `credential_id` enum of ids — never the secret; `password`-typed args are omitted from the schema and filled from the credential, incl. a sole-password fallback without `secret_arg`) — **and** the agent now *also* gets the name-derived env vars, so env-only runnables work for it too. Credentials ride `env(1)` through `sudo`/`su` for `run_as` runnables (most sudo runnables take no credential — passwordless sudo — so are unaffected). Ad-hoc relays can still reference a credential by id instead of a typed password. Derivation lives in `credentials.env_vars_for`/`env_prefix`; the resolver is `bridge._credential_env`; the executor merges `cred_env` alongside the secret channel. **Known gap:** the strict convention emits `JSM_USERNAME`/`JSM_PASSWORD` etc., which don't match `runspec-jsm`'s `JSM_USER`/`JSM_PAT` or `runspec-snow`'s `SNOW_USER`/`SNOW_TOKEN` yet (follow-up). **Config Sync** (0.73.0; Settings → Config Sync; `[git-sync]` extra): pull the config files (`config.toml`, `runspec_hosts.toml`, `runspec_triggers.toml`, `runspec_filters.toml`, `runspec_credentials.toml` metadata) from an external git repo **at a tag** so a fresh install is fully configured from one versioned source. Auth reuses a Credentials-tab entry (HTTPS token / SSH key); git is **dulwich** (pure-Python, no system `git`; it ignores `~/.gitconfig`), SSH over the bundled paramiko; HTTPS verifies against a configurable `ca_bundle` (private-CA / TLS-proxy hosts; with a `tls_verify=false` escape hatch) and routes through the `[ssh] proxy`. A pull validates every fetched TOML before writing and backs up each file first. The merge is **non-destructive** (0.74.0): collection files (hosts/triggers/filters/credentials) merge by identity (`name`/`id`) — adding/updating from the repo, keeping the operator's own entries; `config.toml` deep-overlays (local-only keys survive); a **`preserve`** list (`"<file>:<dotted.path>"`, `*` = any entry; built-in defaults `mailbox.folder` + every host's `room_user`) pins machine-local keys to the local value. The local `[config_sync]` is re-injected + stamped; then hot-reloads with no restart. Merge logic lives in the pure `configmerge` module. See `docs/design/config-git-sync.md`. **Agent memory + runbooks** (0.90.0; `memory.py`): four autonomous tools let the agent accrue cross-run knowledge cheaply — `remember`/`search_memory` over a **machine-local** `runspec_memory.toml` (keyed by an `app` tag; stdlib token-overlap recall, **never synced**), and `save_skill`/`load_skill` over `runspec_skills.toml`, a `[[skill]]` runbook collection that **rides Config Sync** (in `KNOWN_FILES`, merged by `id`). Only the **pinned** memory subset folds into the system prompt (capped, `""` when none) via the per-turn `system_override` path, so the prompt-cached tool/system prefix doesn't churn. Pairs with `runspec-logops` for log-driven triage. **Denylist fleet control** (0.106.0): a **Settings → Denylist** tab (host picker, Disable-all / Re-enable-all danger buttons, add-rule form, enforce/warn/off toggle — `SettingsDrawer.tsx` `DenylistTab`) and a confirm-gated built-in agent tool `manage_denylist`, both running the core `runspec deny` (`list`/`add`/`all`/`clear`/`mode`) across selected fleet hosts over their SSH connections, once per venv — `targets` selects hosts (omit ⇒ all). The public bridge method is `manage_denylist(payload)`. Because it shells out to the core `runspec` binary (exempt from its own denylist), `clear` always re-enables and the console can't be locked out; remote hosts need `runspec >= 0.43.0`. See `bridge.py` `_denylist_tool`/`_run_manage_denylist`. **Outlook Classic agent tools** (0.107.0): when Outlook COM is reachable (Windows + pywin32) the chat agent gets six built-in tools riding the desktop's signed-in Outlook session (works where the Graph device-code flow is blocked) — read-only `outlook_inbox`/`outlook_search`/`outlook_calendar`/`outlook_reminders` (autonomous) and confirm-gated `outlook_draft`/`outlook_send` (send-as a shared mailbox via `from_mailbox`); `_console_context` names the monitored mailbox + tools. Built on `sources/outlook.py` COM (`list_messages`/`search_messages`/`upcoming_events`/`save_new_draft`/`send_new_mail`) + bridge `_outlook_tools`/`_run_outlook_tool`. Graph Outlook/Calendar runnables in `runspec-windows` are now redundant (retirement is a follow-up). **Rota “Take a break”** (0.109.0): a Rota-tab button suspends triggers/schedules for N minutes (presets 15/30/60 + custom; default 60) and **auto-resumes** at the return time, with a best-effort Outlook reminder (`create_reminder`); it **honours the lock screen** (stays paused if still locked at the return time) via a minimal `winlock.is_screen_locked()` (`OpenInputDesktop`). Held in-memory (`_break_until` + `start_break`/`cancel_break`); the rota gate (`_build_clock_ok`) honours it live and self-clears — no timer/restart. Full lock auto-suspend is a follow-up. **Lock auto-suspend** (0.110.0): a presence governance control — reactive triggers don't fire while the operator's screen is **locked** (Rota toggle, **default ON**, live unlocked/locked/unknown indicator). **Fail-closed** when on (uncertain `unknown` probe ⇒ paused); **absolute** (no per-trigger `ignore_lock`/`ignore_rota` bypass); **schedule-sourced jobs are exempt** (pre-authorised, not reactions-while-away); suppressions are logged + audited. Pull-based gate (no listener/thread): engine `lock_ok`, bridge `_build_lock_ok`/`screen_state`, tri-state `winlock.screen_lock_state()`. It's a governance/operational control, **not a security boundary**. Also: `create_reminder` is now profile-robust (appointment→task→flagged-mail, so it works on IMAP/POP). See `docs/design/lock-auto-suspend.md`. **Richer trigger actions + editable confirms** (0.111.0): four new confirm-gated built-in agent action tools — `create_document` (markdown → local file and/or upload to a host via `start_transfer`), `run_skill` (render a saved runbook **inline**, not a spawned sub-agent), `create_schedule`, `emit_event` (`_ACTION_TOOL_NAMES`/`_action_tools`/`_run_action_tool`) — reachable in any agent-mode trigger turn, scoped per trigger by a new **`action_allow`** allowlist (parallel to `runnables`, filtered in `_run_isolated_agent_turn`). Rule triggers gained a **bounded declarative multi-runnable list** (`actions` array-of-tables + `action_mode` sequential/parallel, cap 10, mutually exclusive with `run`): parallel runs behind **one grouped confirm** (`run_group`, most-restrictive autonomy, any `manual` step refuses the group — `_run_rule_group`), plus an optional single **LLM `pre_turn`** extract step (no runnable tools, can't branch) whose output feeds the arg maps via `{{ _llm.* }}`. Stays under the §1 "no workflow engine" cliff (flat list, no branching/result-piping/cross-restart state). **Editable confirmations (global)**: the operator can edit a gated call's args before approving — `resolve_tool_confirmation(request_id, approved, edited_input=None)` → `_finish_confirm` returns `(approved, edited_input)`, used in `_gated_run_tool*` / reply gates / `_run_rule_group` (the `run_runnable` wrapper stays read-only). Triggers UI is `TriggersEditor.tsx` (source-aware reply/once-per-thread, "Actions on results", declarative action list, action allowlist). See `docs/design/console-triggers.md` §5.1 + `docs/design/trigger-actions-ux.md`. |
| `runspec-room` | 0.28.0 | PyPI | The chat-room server (`packages/console-room/`) — a "dumb message pipe with presence". Two front doors, one process: consoles via NDJSON over the SSH tunnel (localhost), people via an email-verify-authenticated browser web app (Starlette + uvicorn, TLS, SQLite transcript; **email login is the only auth door — OIDC removed in 0.20.0**). `console-room connect` is the exec-channel relay (stdio↔local room port) for the no-forwarding transport. **Helpdesk mode**: private per-visitor conversations (`helpdesk/<queue>/<visitor>`, room name derived server-side from the verified identity), namespace-subscribed agent consoles, conversation lifecycle (open→assigned→resolved→closed, claim/resolve/reopen/close), and a per-queue **self-service catalog** published by agent consoles + visitor forms generated from `inputSchema` — the room itself never executes anything. **Email-verify auth mode** (`CONSOLE_ROOM_AUTH=email-verify`): an IdP-free, internal-only self-service door (`/login` → `/verify`, old `/signup` 307-redirects) where an employee verifies a company email to log into a queue; the verify mail is sent **by a connected console as the queue mailbox** (the room never sends mail), backed by a durable SQLite outbox + a periodic expiry sweep, and the per-request operator Accept is still the authorization. Requires `CONSOLE_ROOM_PUBLIC_URL`. **Email *login* with expiring access (0.27.0)**: access is **not permanent** — the signed session cookie carries a `session_ttl` `max_age` (`CONSOLE_ROOM_SESSION_TTL`, default 24h), so it expires and the visitor logs in again the same way; `/verify` sets a non-secret `room_email` cookie so a returning known member gets a one-click "welcome back" login (reusing stored name+team). **TOML config file (0.27.0)** (`--config` / `CONSOLE_ROOM_CONFIG` / `./console_room.toml`): deploy an instance from one file — `[server]` (host/ports/public_url/mailbox/tls/db) + `[auth]` (`verify_ttl`, `session_ttl`, and a **`session_secret_command`** whose stdout is the secret) + authoritative **`[[teams]]`** synced on boot. Precedence **defaults < config < env < CLI** (config values inject into `CONSOLE_ROOM_*` env only where unset). **Configurable login domain allowlist (0.28.0)**: the login door's allowlist is the queue mailbox's own domain (always allowed); `CONSOLE_ROOM_ALLOWED_DOMAINS` / `--allowed-domains` / `[auth] allowed_domains` (TOML array or comma/space string, leading `@` optional) widens it with extra same-company domain variants while the mailbox stays the single monitored connector — an explicit full-domain list, not a wildcard. See `docs/design/console-room-server.md` + `docs/design/helpdesk-rooms.md` + `docs/design/email-queue-signup.md` (§11 = operating/testing, §12 = login + config) |
| `runspec-linux` | 0.5.0 | PyPI | 31 Linux system admin runnables (incl. package management — `list-packages`/`list-upgrades`/`upgrade-packages`/`install-package`/`remove-package` over apt/dnf/yum/zypper/pacman — and `reboot-host`). The privileged ones declare `run_as = "root"` (console escalates via sudo); `enable-passwordless-sudo` is the one-time bootstrap that installs a `visudo`-validated `/etc/sudoers.d` drop-in (`scoped`/`all`) so they run password-free. **Python venv management** (0.4.2): `create-venv` (a specific browsable interpreter, always `--symlinks`, plus a shared `2775` `logs/` dir + optional `--group` chgrp), `install-into-venv` (named packages and/or a local `--project` pyproject/wheel, `--editable`), `configure-pip` (write a `pip.conf` at user/venv scope for private-PyPI). These take a **runtime `--run-as` argument** (+ `--become-method` sudo/su) rather than declarative `run_as` — the service account that owns a shared venv is chosen per-invocation. `pip install` into remote venvs (thin wrappers over `runspec-linux-core`). **Web/TLS/API probes (0.5.0):** 8 cross-platform read-only runnables — `cert-check`/`cert-chain`/`tls-info`/`http-check`/`http-headers`/`redirect-trace`/`dns-lookup`/`api-spec` (an OpenAPI/Swagger reader for writing/updating runnables against internal APIs) — thin wrappers over `runspec-webops-core` (a Python port of the `runspec-webops` Node pack + 4 extras; same wrappers ship in `runspec-windows`). |
| `runspec-linux-core` | 0.3.2 | PyPI | Pure-Python logic core behind `runspec-linux` — importable helpers, **no runspec dep / no runspec.toml / no entry points** (surfaces zero runnables) |
| `runspec-webops-core` | 0.1.0 | PyPI | Pure-Python HTTPS/TLS + web-endpoint logic core behind the cross-platform web runnables in `runspec-linux` **and** `runspec-windows` — stdlib only (`ssl`/`socket`/`http.client`), **no runspec dep / no runspec.toml / no entry points** (surfaces zero runnables). Public API: `connect_peer`/`leaf_report`/`chain_report`, `probe`/`classify`/`fetch_headers`/`trace_redirects`/`fetch_body`, `resolve`, and OpenAPI/Swagger reading (`load_document`/`summarize_spec`/`describe_operation` — bounded spec shape for writing/updating API runnables; JSON stdlib, YAML via the optional `[yaml]` extra). The cert-chain read uses `SSLSocket.get_unverified_chain()` (3.13+) → `_sslobj` fallback (3.10–3.12) → leaf-only. CI `webops-core-quality`/`webops-core-test`; release tag prefix `webops-core-v` (publish before `runspec-linux`/`runspec-windows`). |
| `runspec-windows` | 0.2.0 | PyPI | 35 Windows system admin + Microsoft 365 (Outlook/Teams/Calendar/OneDrive) runnables, **plus the same 8 cross-platform web/TLS/API runnables as `runspec-linux` (0.2.0; `runspec-webops-core` wrappers — not Windows-gated, incl. the `api-spec` OpenAPI reader)** so they surface in the runspec-console (which depends on `runspec-windows`). |
| `runspec-jsm` | 0.1.0 | PyPI | 8 Jira Service Management (Data Center) service-desk runnables — raise/comment/transition/set-property + portal-field discovery |
| `runspec-snow` | 0.1.0 | PyPI | 8 ServiceNow ITSM runnables — create/get/comment/state/set-field over the Table API + Service Catalog discovery |
| `runspec-logops` | 0.1.3 | PyPI | 3 log-condensing + code-mapping runnables (`summarize-log`/`map-trace`/`bundle-digest`) — cheap application-support triage from a log file; thin wrappers over `runspec-logops-core` |
| `runspec-logops-core` | 0.3.1 | PyPI | Pure-Python logic core behind `runspec-logops` — importable helpers, **no runspec dep / no runspec.toml / no entry points** (surfaces zero runnables) |
| `runspec-claude` | 0.1.0 | PyPI | Drives the **Claude Code CLI** (`claude`) headlessly — one runnable `claude-run` with four verbs (subcommands): `ask` (fresh headless turn), `continue` (most-recent session via `claude --continue`), `resume --session <id>` (`claude --resume`), and read-only `sessions` (lists `~/.claude/projects/<encoded-cwd>/*.jsonl`, `CLAUDE_CONFIG_DIR`-aware, best-effort titles). Each shells out to `claude -p … --output-format json` and returns `{result, session_id, num_turns, total_cost_usd, is_error, raw}` so the runspec-console agent (itself Claude) can delegate a bounded sub-task — "Claude driving Claude" — and resume the same worker on a later turn via the returned `session_id`. `runspec serve` flattens it into one leaf MCP tool per verb (`claude-run_ask`/`_continue`/`_resume`/`_sessions`). **Safe-by-default, operator-governed:** action verbs are `autonomy = "confirm"`, `sessions` is `autonomous`; defaults `--permission-mode default` + `--max-turns 12`. The delegate's power (`permission-mode`/`model`/`dir`/`allowed-tools`/`append-system-prompt`/`bare`) lives in inherited **global** args — operator/run-form controlled, not set by the autonomous agent (whose tools take only a prompt + `session`/`limit`); opt up explicitly (`claude-run --permission-mode acceptEdits ask "…"`). Entry point is `claude-run` (not `claude`) so it never shadows the real CLI. Public Python API `run_claude`/`list_sessions`/`encode_project_dir`/`ClaudeError` for corporate-wrapper packages. Requires the `claude` CLI on the host; zero extra runtime deps. CI `claude-quality`/`claude-test`; release tag prefix `claude-v`. |
| `runspec-webops` | 0.1.0 | npm | HTTPS/TLS certificate + web-endpoint runnables (Node; `packages/node-webops`). Now also ported to Python — see `runspec-webops-core` and the web/TLS runnables in `runspec-linux`/`runspec-windows` (for fleets without Node). |
| `runspec-registry` | 0.1.1 | PyPI | Archived — registry removed in favour of SSH+MCP jump-host model |

**Integration packages** live under `packages/python/` alongside the core library. Each is a
self-contained package: own `pyproject.toml`, `runspec.toml`, tests, and CI
workflow. Release tags follow `{name}-v*` (e.g. `linux-v0.1.0`).

**Releasing — read `docs/releasing.md` before cutting a release.** Tagging is
automated, publishing is manual, and they're decoupled: bumping a package's
`version` on `main` makes `auto-tag.yml` write the `{prefix}v{version}` tag, but
that tag is pushed with `GITHUB_TOKEN` so it does **not** trigger the
`*-release.yml` workflow. You publish by manually running the package's release
workflow (`workflow_dispatch`) on that tag. Agents: stop at the version bump +
changelog on the branch; never push a `{prefix}v*` tag yourself.

`runspec-linux` is a thin runnable shell over **`runspec-linux-core`** (0.2.0+):
each module parses args with `runspec`, calls the matching `runspec_linux_core`
helper, and renders the JSON/exit. The core package holds all the pure logic and
has **no `runspec` dependency, no `runspec.toml`, and no entry points**, so
installing it exposes the helpers for import without surfacing any runnables
(invisible to `runspec local` / `runspec serve` discovery, which only sees dists
that require `runspec`). Core helpers return plain data and *raise* on failure
(`ToolNotFoundError` for an absent system tool, `CommandError` for a non-zero
subprocess); the wrappers catch these to reproduce the CLI/agent behaviour.

Import the helpers two ways: `from runspec_linux_core import nc_send, system_info,
…` (zero-surface — depend on `runspec-linux-core` alone), or
`from runspec_linux import nc_send` (re-exported for back-compat; pulls in the
full fleet). So a wrapper runnable for a TCP-interface app that only wants the
helper should depend on `runspec-linux-core`, not `runspec-linux`. `nc_send(host,
port, command, wait, read_timeout)` and the rest are the public Python API.
Release ordering: publish `runspec-linux-core` before `runspec-linux` (see
`docs/releasing.md`).

`runspec-windows` is the Windows counterpart: 35 runnables across system admin
(`subprocess`/`ctypes`/`winreg` — no pywin32) and Microsoft 365 via Graph
(Outlook + Teams + Calendar + OneDrive). OS-specific calls sit behind `_platform.py` helpers; the pure
output parsers and Graph formatters are unit-tested on any platform (CI runs them
on ubuntu plus a `windows-latest` smoke job). Graph auth is MSAL **device-code**
(public client, no secret); the `[graph]` extra carries `msal`/`httpx`, and the
package exports `graph_get()` / `get_token()` as a public API (parallel to
`nc_send`). runspec-console depends on it (`sys_platform == 'win32'` marker) so
the runnables surface in the console UI automatically. CI mirrors the linux jobs
(`windows-quality`/`windows-unit`/`windows-smoke`); release tag prefix is
`windows-v`.

`runspec-jsm` targets **Jira Service Management Data Center** for service-desk
agents and customers (not Jira product admins): 8 runnables for raise / comment
(public or internal) / transition (by name or id) / set-property, plus
request-type + portal-field discovery (`jsm-list-request-types`,
`jsm-list-fields`). Pure HTTP via `httpx` over a **mixed** REST surface
(`/rest/servicedeskapi` for raise/comment/discovery, `/rest/api/2` for
transitions/properties), so it runs unchanged on Windows and Linux. Auth is
**env-var only** — `JSM_BASE_URL` + (`JSM_PAT` Bearer *or* `JSM_USER`/`JSM_PASSWORD`
Basic; PAT wins), with optional `JSM_<ENV>_*` prefixes selected by the
`--env dev|uat|prod` arg on every runnable. **Identity is deploy-determined — no
sudo flag:** run it as yourself locally or as the service account on a server
(`sudo -u <svc> -E` / systemd / login shell); it makes no OS calls. It exports a
public Python API where every operation takes a `JsmConfig` first arg
(`config_from_env`, `create_request`, `add_comment`, `transition_issue`,
`set_issue_property`, `list_request_types`, `get_request_type_fields`,
`resolve_request_fields`, …) so a private (e.g. Nexus-hosted) package can import
them, inject credentials in code, and bake in a default `service_desk_id` /
`project_key` / `env` to build "one runnable per project" on top. CI mirrors the
linux jobs (`jsm-quality`/`jsm-test`); release tag prefix is `jsm-v`.

`runspec-snow` is the ServiceNow counterpart of `runspec-jsm`, for teams
migrating ITSM tasks to ServiceNow: 8 runnables for create / get (by sys_id or
number) / comment / state / set-field, plus read-only Service Catalog discovery
(`snow-list-catalog-items`, `snow-list-variables`). Tuned for `incident`,
`sc_task` and `sc_req_item` but `--table` accepts any table. Pure HTTP via `httpx`
over the **Table API** (`/api/now/table/{table}`) for CRUD and the **Service
Catalog API** (`/api/sn_sc/servicecatalog`) for discovery. ServiceNow's
`comments` (customer) vs `work_notes` (internal) maps onto JSM's public/internal
split (`--internal`); it has no Jira-style transition endpoint, so a "transition"
is a validated `state` choice-field update with label→value resolution via
`sys_choice`. Auth is **env-var only** — `SNOW_BASE_URL` (or `SNOW_INSTANCE`) +
(`SNOW_TOKEN` Bearer/OAuth *or* `SNOW_USER`/`SNOW_PASSWORD` Basic; token wins),
with optional `SNOW_<ENV>_*` prefixes selected by `--env`. Same **deploy-determined
identity — no sudo flag** model as `runspec-jsm`. It exports a public Python API
where every operation takes a `SnowConfig` first arg (`config_from_env`,
`create_record`, `get_record`, `add_comment`, `update_state`, `resolve_state_value`,
`set_fields`, `list_catalog_items`, …) so a private package can inject credentials
and bake in a default `default_table` / `env` to build "one runnable per table" on
top. CI mirrors the linux jobs (`snow-quality`/`snow-test`); release tag prefix is
`snow-v`.

`runspec-logops` lets the console agent triage application issues **from a log
file** cheaply and frequently — the runnables do the heavy lifting on the host
where the log lives and return small structured JSON, so the agent reads
condensed conclusions, not raw logs/code. 3 runnables: `summarize-log`
(autonomous) streams a log and clusters repeated errors by a normalised
*signature*, returning a **bounded** top-N digest (counts, first/last ts, one
sample each) whose size is independent of input size — supports a `--level` floor
and `--since`/`--until` window; `map-trace` (autonomous) resolves a stack trace,
or a `summarize-log` signature like `NullPointerException@com.acme.OrderSvc.price`,
to just the source snippets around each frame in a checkout (`--repo`);
`bundle-digest` (confirm) zips digest + snippets + manifest for transfer via the
console's existing `download_file`. The operator's flow: use the console's
**filtered fetch** to colocate the log with the code (remote or local), then run
these over it. Same **core/wrapper split** as linux: all logic is in
**`runspec-logops-core`** (no `runspec` dep / no `runspec.toml` / no entry points —
invisible to discovery, importable), and `runspec-logops` is thin wrappers
(`rs.parse()` → helper → JSON). Public API: `summarize_log(path, *, level, since,
until, top, max_sample_lines, max_bytes)`, `map_trace_to_sources(repo_root, trace,
*, context, max_files, max_total_lines)`, `build_bundle(digest, code_map, *,
dest_dir, metadata)` — all plain params. **Corporate-wrapping path:** depend on
`runspec-logops-core` only, import the helpers, bake corporate defaults/paths/
redaction as plain params in your own private (e.g. Nexus) package with its own
`runspec.toml`, and only your wrapped runnables surface in the venv — the generic
published code stays clean (same model documented for jsm/snow). CI mirrors the
linux jobs (`logops-quality`/`logops-test`, `logops-core-quality`/
`logops-core-test`); release tag prefixes `logops-v` / `logops-core-v` (publish
core before logops). Note `spec.<arg>` is an `Arg`; read `spec.<arg>.value` for
the coerced value — an unset optional is then a real `None`.

**Docs drift checks.** `scripts/check_docs.py` guards the MkDocs site against
drift and runs in CI (`docs-check` job, gated by a `docs` paths filter) and
optionally via `.pre-commit-config.yaml`. Three checks: (1) **version-sync** —
each package manifest version must have a matching `CHANGELOG.md` entry
(`## [x]` for Python, `## [node-x]` for Node); (2) **toml-examples** — every
runspec ` ```toml ` block in `docs/` validates against
`schema/runspec.schema.json`; (3) **format-reference** — the arg-field and type
tables in `docs/format.md` must match the schema. Practical upshot: when you
add or change a format field/type, update `schema/runspec.schema.json` and the
`docs/format.md` tables in the same change, and add a CHANGELOG entry for any
version bump — or `docs-check` fails. (`KNOWN_CUSTOM_TYPES` in the script lists
the custom `register_type` names demonstrated in the docs.)

**Next:** Config-file value fallback (third tier in value resolution, currently "design for now").

**Queued:**
- `runspec emit --rundeck` — emit a Rundeck job YAML per runnable to
  `jobs/{runnable}.yaml` at the git root. Always uses a Bash script step
  (not exec) so flag args can be conditionally included. Output goes to
  file; user uploads via Rundeck UI. See `docs/design/emit-ansible-rundeck.md`.
- `runspec emit --ansible` — emit an Ansible module Python file per
  runnable to `library/{runnable}.py`. Generates `DOCUMENTATION`,
  `argument_spec`, and subprocess invocation from `runspec.toml`. Full
  AAP survey form and ansible-lint compatibility. See
  `docs/design/emit-ansible-rundeck.md`.
- `runspec push --rundeck` — future: POST job YAMLs to Rundeck REST API
  (`/api/v*/project/{project}/jobs/import`) with an API token. Deferred
  until file emit is in production use.
- `runspec emit --stubs` — **shipped** (0.40.0/0.41.0) as **`runspec stubs`**:
  generates a self-contained `typings/runspec/__init__.pyi` from the installed
  venv so a parsed `RunSpec` types precisely (`args.workers.value` → `int`, typos
  caught, choices as `Literal[...]`) instead of `Any`. Committed artifact,
  regenerated when `runspec.toml` changes; `--check` enforces freshness in CI /
  pre-commit; `runspec init` drops a placeholder + wiring and `runspec local`
  auto-refreshes. See `docs/design/stubs-handoff.md`, `docs/typed-args.md`, and
  `docs/design/emit-stubs.md`. Not yet built: the global strict default ("Option
  C" — ship a `.pyi` so projects that never run `init`/`stubs` get the sentinel
  instead of `Any`).

**Node parity — done (node-0.18.0):** string validation (`pattern`/`min-length`/
`max-length`) and `require-command` are now implemented in the Node pack at parity
with Python 0.22.0/0.23.0. The shared compliance fixtures
(`tests/integration/fixtures/complex.toml`) now exercise both and are verified
identically by the Python and Node compliance suites.

**Node parity — `enforce_run_as` (node-0.33.0):** the runtime identity gate from
Python 0.37.0 is now in the Node pack. New `packages/node/src/become.ts` ports
`resolveRunAs` (string / `$ENV` / per-host / first-match patterns) and
`verifyRunAsIdentity` (uid comparison via `id`) — this is the first `run_as`
support in Node, which previously had none (it builds no escalation command;
`serve` doesn't sudo). On `parse()` the resolved `run_as` user is compared by
uid to the effective user; `enforce_run_as` (`"error"` default / `"warn"` /
`"off"`, per-runnable → `[config]` → library default) drives the reaction.
No-op when `run_as` is empty or `process.geteuid` is undefined (Windows), and
skipped by `loadSpec`.

Build order: `emit --rundeck` first (current platform), `emit --ansible`
second (migration target), `push --rundeck` last (CI/CD automation).

---

## Good Claude Code Prompt Patterns

Be specific and bounded. Examples of good prompts:

```
Read CLAUDE.md. Then look at loader.py and write tests for load_raw
covering the runspec.toml format. Put them in
packages/python/tests/test_loader.py.
```

```
Read CLAUDE.md and inference.py. The infer_arg function needs to handle
the case where both 'type' and 'options' are declared — options should
win and set type to 'choice'. Write the fix and add a test for it.
```

```
Read CLAUDE.md and parser.py. Walk me through what happens step by step
when parse() is called for a script with a subcommand.
```

Avoid open-ended prompts like "build the library" — break work into
one module or one behaviour at a time.
