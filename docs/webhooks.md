# Webhooks (runspec-scheduler)

A webhook lets another system start a runnable on a host: Alertmanager when an
alert fires, GitLab when a pipeline finishes, a ticketing tool when a request is
approved. The sending app makes one HTTP `POST`; the venv's
[runspec-scheduler](shared-schedules.md) daemon checks it, works out what to
run, and runs it straight away (or, if you ask for it, holds it for a person to
approve on runspec-console).

The rules are the same as everywhere else in runspec:

- Only runnables installed in **that venv** can run, as the venv owner, recorded
  in History like any other run (label `hook:<name>`, run by *webhook*).
- **Setting up a hook is the approval.** Like a host schedule or an emitted
  Rundeck job, a hook runs whatever its rules pick without asking each time:
  **autonomous**, **confirm** and **supervised** runnables all run at once.
  What a sender can make it do is limited by the hook's rules, its fixed
  templates and its token.
- **manual** runnables are always refused (an arg that declares `manual`
  counts, as in the console): only a person runs them.
- `require_approval = true` on a hook or a rule makes each of its runs wait for
  a person to approve it on a console. Use it for hooks fed by input from
  outside your team, such as a ticket anyone can raise.
- The denylist applies.
- Values from the body reach the runnable as `RUNSPEC_<RUNNABLE>_ARG_<NAME>`
  environment variables, never on its command line.

## How a delivery goes

1. The app sends `POST https://<host>:<port>/hook/<name>` with a token.
2. The listener checks the sender's address, the token, the rate limits and the
   size, writes the request to the spool (`{venv}/hooks/spool.sqlite`) and
   answers `202 {"id": 17, "status": "queued", "status_url": "/hook/<name>/delivery/17"}`.
   Nothing else happens on the request, so a slow run never makes the sender
   wait or retry. (A hook with `respond = "output"` waits for the run instead:
   see [Sending the output back](#sending-the-output-back).)
3. The daemon picks out the fields it was told to, finds the first rule that
   matches, fills in the args and works out the autonomy:
    - **manual**: refused;
    - **`require_approval`** on the hook or rule: waits for an approval on a
      console (or `hooks approve` on the host). The first approval wins; the
      daemon runs it and records who approved it. A denial settles it for
      everyone;
    - anything else: runs it now (up to `max_runs` at once);
    - a rule with `skip = true`, or no matching rule: recorded, nothing runs;
    - a rule with no `run`: shown on the consoles following the venv, nothing runs.
4. Consoles following the venv see every step as it happens
   (`hooks follow` streams them; runspec-console runs it over SSH).

If nobody approves within `approve_within` (1 hour by default) the run expires.
If nobody is following the venv, or everyone following has muted the hook, and
`[smtp]` is set up, the approval is mailed at once. Otherwise it's mailed when
nobody has picked it up within `expect_claim_within` (15 minutes).

## Turning it on

The listener lives in the scheduler daemon, so the venv needs a keeper even if
its schedules use cron (see [Keepers](shared-schedules.md#keepers-what-keeps-the-daemon-running)).
On a cron venv the daemon fires no schedules; it only serves webhooks.

Webhooks never run as root: a root-owned venv's daemon refuses to listen.

```bash
cd /srv/venvs/ops-tools
bin/runspec-scheduler hooks settings --set enabled=true --set port=8977
bin/runspec-scheduler hooks list
```

Each venv on a host needs its own port. In runspec-console, *Turn on* on a
venv with its listener off (Automation → Webhooks) saves `enabled = true` on
the host in one click, keeping a port already set or picking the first from
8977 up that no other venv on that host uses. The view updates as soon as the
host has saved it. *Settings…* opens these settings to change anything else.

Plain HTTP is only allowed on `127.0.0.1`, behind a reverse proxy that does
TLS. Anywhere else give it a certificate:

```bash
bin/runspec-scheduler hooks settings \
  --set bind=0.0.0.0 \
  --set cert=/etc/pki/tls/certs/ops-01.pem \
  --set key=/etc/pki/tls/private/ops-01.key
```

The daemon picks the change up by itself. `GET /healthz` answers `ok` with no
token, for a load balancer.

## A hook

Hooks are `[[hook]]` tables. This one handles Alertmanager: one request carries
several alerts, so it splits the body and decides per alert.

```toml
[[hook]]
name = "alerts"
description = "Alertmanager: clean up full disks, ask before restarting services"
token_sha256 = "9f2c…"            # from: runspec-scheduler hooks token
split = "alerts"                  # one delivery per alert in the body
fields = { alertname = "labels.alertname", instance = "labels.instance", status = "status", group = "$.groupKey" }
dedup = "{{ group }}:{{ alertname }}:{{ status }}"

[[hook.rule]]
name = "resolved"
match = { status = "^resolved$" }
skip = true

[[hook.rule]]
name = "disk"
match = { alertname = "^DiskFull$" }
run = "cleanup-tmp"
args = { host = "{{ instance | regex('^([^:]+)', 1) }}" }

[[hook.rule]]
name = "service"
match = { alertname = "^(?P<service>[a-z-]+)Down$" }
run = "system-service"
command = "restart"
args = { name = "{{ service }}" }
```

Save it with a new token (printed once; only its SHA-256 is stored):

```bash
bin/runspec-scheduler hooks save --new-token < alerts.toml
```

and give the sending app the address and the token, either as
`Authorization: Bearer <token>` or in the `X-Runspec-Token` header.

A token runspec makes is written to the terminal (or the SSH channel the
console reads) only, never into the run's log, and runspec never prints a
password: you choose it and only its hash is kept.

### Hook keys

| Key | What it does |
|---|---|
| `name` | The address: `/hook/<name>`. Lowercase letters, digits, `.`, `_`, `-` |
| `token_sha256` | SHA-256 of the token the sender uses. Never the token itself |
| `token_header` | The header the token may also come in (default `X-Runspec-Token`) |
| `username` + `password_hash` | Basic auth instead of (or as well as) a token. `password_hash` is a salted PBKDF2 hash (`pbkdf2_sha256$…`). Choose the password yourself: put `password = "…"` in the hook you give `hooks save` (it's hashed before saving), or pipe it to `hooks token --basic` for a hooks.d file, which prints only the hash |
| `allow` | Addresses or networks that may send, e.g. `["10.20.0.0/16"]`. Empty = any |
| `fields` | `name = "path.to.value"`. A path starting `$.` reads the whole body even when it's split |
| `split` | Path to a list in the body: each item is its own delivery |
| `rule` | `[[hook.rule]]` tables, tried in order; the first match wins |
| `run`, `command`, `args` | A rule for when no other matches |
| `dedup` | A template; a delivery with the same text within `dedup_window` (10m) is counted on the first one instead of running again |
| `require_approval` | Wait for a person to approve each run (by default a hook runs without asking) |
| `approve_within`, `expect_claim_within` | Per-hook versions of the `[hooks]` settings |
| `email_to` | Who gets this hook's approval mail (default: `[smtp] to`) |
| `max_running` | Runs of this hook at once (default 1) |
| `skip_if_running` | Skip a delivery that would run at once while the last run is still going |
| `timeout` | Kill a run after this long (default 6h) |
| `rate_per_minute` | This hook's own limit (default `[hooks] rate_per_minute`) |
| `transform` | A runnable that turns an awkward body into fields (see below) |
| `respond` | `"output"`: hold the request until the run finishes and answer with its exit code and output (see [Sending the output back](#sending-the-output-back)) |
| `respond_within` | How long `respond = "output"` holds the request (default `25s`, at most `5m`) |
| `respond_max` | How much of the run's output goes back (default `64k`, at most `4m`; the rest is cut) |
| `enabled` | `false` answers 404 without removing it |

### Rule keys

| Key | What it does |
|---|---|
| `name` | Shown in deliveries and on the consoles |
| `match` | `field = "regex"`. All must match (a search: anchor with `^…$`). Named groups become fields |
| `skip` | Record it and do nothing |
| `run` | The runnable to run. Leave it out to only show the delivery on consoles |
| `command` | The runnable's command, e.g. `"restart"` or `["db", "vacuum"]` |
| `args` | `arg = "template"`. An arg that fills in empty is left out |
| `require_approval` | Wait for a person for this rule |

## Templates

`args` and `dedup` use `{{ field }}` with a short, fixed list of filters, so a
body from another system can never run anything:

| Filter | Example | Result |
|---|---|---|
| `lower`, `upper`, `trim` | `{{ name \| trim \| lower }}` | `diskfull` |
| `replace(old, new)` | `{{ instance \| replace(':9100') }}` | `web-01` |
| `regex(pattern, group)` | `{{ instance \| regex('^([^:]+)', 1) }}` | `web-01` (none when it doesn't match) |
| `default(value)` | `{{ team \| default('ops') }}` | `ops` when empty or missing |
| `split(sep)`, `first`, `last`, `join(sep)` | `{{ hosts \| split(',') \| first }}` | lists for a `multiple` arg |
| `json` | `{{ labels \| json }}` | the value as JSON text |

A template that is one `{{ … }}` and nothing else keeps the value's type, so a
list stays a list for a `multiple` arg. Built-in fields are `hook`, `delivery`,
`received`, `source`, `content_type`, `body` and `headers` (minus anything that
carries a secret), plus `item` when the body is split.

Bodies can be JSON, XML (no `DOCTYPE` or entities), form-encoded or plain text
(plain text only gives `body`). For anything stranger, set `transform` to a
runnable in the venv: it gets the body on stdin and prints a JSON object whose
keys become fields.

## Trying a hook before it's live

`hooks test` shows what a hook would do with a body, without running or storing
anything:

```bash
bin/runspec-scheduler hooks test --hook alerts < sample.json
bin/runspec-scheduler hooks test --hook alerts --last     # the last body it received
bin/runspec-scheduler hooks test --file hooks.d/ops.toml < sample.json   # before deploying
echo '{"hook": {"name": "alerts", "fields": {}}, "body": null}' | bin/runspec-scheduler hooks test --draft   # an unsaved hook (what the console editor sends)
```

```text
alerts with stdin:

Item 1
  outcome:  runs at once
  rule:     disk
  runs:     cleanup-tmp
  autonomy: autonomous
  arg host = "web-01"
```

## Deploying hooks with Ansible

Hooks can also live in `{venv}/hooks.d/*.toml`, one or more `[[hook]]` tables
per file. They win over a console-made hook of the same name and show as
*deployed* (read-only) on the consoles. A file must belong to the venv owner (or
root) and not be writable by group or others, or it's skipped.

`hooks check` validates a file and exits 1 on any problem, so it works as
Ansible's `validate`:

```yaml
- name: Alertmanager webhook
  ansible.builtin.template:
    src: alerts.toml.j2
    dest: /srv/venvs/ops-tools/hooks.d/alerts.toml
    owner: opsbot
    mode: "0644"
    validate: /srv/venvs/ops-tools/bin/runspec-scheduler hooks check %s
```

## Following from runspec-console

runspec-console (0.438.0 and later) can follow a host's webhooks and show the
approvals as cards. Name the hooks to follow on the host's entry in
`runspec_hosts.toml` (or click *Follow* on **Automation → Webhooks**, which
writes the same thing):

```toml
[[host]]
name          = "web-01"
ssh           = "deploy@web-01.internal"
runspec_paths = ["/srv/ops/venv/bin/runspec"]
hooks         = ["*"]          # every hook; or ["alerts", "deploys"]
```

The console then keeps `runspec-scheduler hooks follow` open to each of that
host's venvs, as the venv owner, over the host's SSH connection. It reconnects
by itself and catches up on what arrived while it was away, as far back as
`[webhooks] replay` in its `config.toml` (default `15m`). Venvs on that host
that can't take webhooks (no runspec-scheduler, one older than 0.11.0) are
listed once as "Not taking webhooks" rather than as errors; root-owned venvs
are skipped, since runspec-scheduler never runs as root.

- **Approvals.** A delivery that waits for a person shows as a card at the top
  of the Console tab: the hook, the host and venv, what it wants to run and the
  fields it was sent. Approve or Deny it there, with an optional note. The
  first decision on any console wins. Waiting webhooks also count in the
  status bar's "waiting for you". This works with the console's AI turned off.
- **Automation → Webhooks** (console 0.440.0; before that, the Schedules tab)
  has one strip per followed venv: whether it's connected, what's waiting,
  mutes, recent deliveries, a *Mute* menu and *Hooks…*. Below them, every
  connected host in the profile that you don't follow is listed with each
  venv's state: webhooks on, off (*Turn on*, *Hooks…*, *Settings…*), no
  hooks yet (*Add a hook…*), or needs runspec-scheduler 0.11.0. Followed venvs
  with their listener off are listed under *Followed, not listening*, one row
  per host, with the same buttons per venv and *Stop following* in the host's
  ⋯ menu. *Follow* works
  before any hook exists or the listener is on. The Schedules tab keeps a
  one-line summary.
- **Hooks…** lists the venv's hooks. Hooks made from the console can be edited
  as TOML, tried against a body (or the hook's last one) without running
  anything, saved with a new token or basic-auth password, and removed. The
  console makes the token or password, sends it to the host on stdin, and shows
  it to you once; the host keeps only a hash. Hooks deployed into `hooks.d`
  are read-only there. *Listener & mail* edits `[hooks]` and `[smtp]`.
- **Away means muted.** While your screen is locked, or outside your working
  hours when the rota is on, the console mutes every hook for you and unmutes
  when you're back. If everyone following a hook is away, its approvals are
  mailed straight away.
- **Triggers.** Every delivery is also a `webhook` event for the console's
  triggers (`event` is the hook name, fields are flattened to `a.b` keys). See
  [Webhooks in console triggers](#webhooks-in-console-triggers).

## Webhooks in console triggers

A hook doesn't have to run anything on its host. A hook with no rules and no
`run`, or a rule with no `run`, is a **pass-through**: the host only records
the delivery (state `event`, "consoles only"), and every console following
that venv hands it to its triggers. That's how a request from Jira or Grafana
reaches a console's agent, which can read it, use its tools and runbooks, and
ask before it changes anything.

```toml
# on the host: no run, so nothing runs here
[[hook]]
name   = "incident"
token_sha256 = "…"
fields = { key = "issue.key", summary = "issue.fields.summary" }
```

```toml
# on a console: config/runspec_triggers.toml
[triggers.incident-triage]
source = "webhook"
event  = "incident"        # the hook's name
mode   = "agent"
rules  = "Look at the incident, check the hosts it names, and post what you find."
claim  = true              # one console takes it (see below)
```

- **Fields.** The hook's fields are the event's fields (`{{ key }}`,
  `{{ summary }}` in a rule trigger's `map` or `match`; a field that is itself
  a table is flattened to `a.b` keys). The event also carries `body` (the raw
  request), `hook`, `host`, `venv`, `id` (the delivery) and `state` (what the
  host did with it); a hook field with the same name wins. An agent-mode
  trigger gets the request written out: the hook, where it came from, what the
  host did with it, the fields and the body.
- **Which consoles.** Every console that follows the venv (*Follow* on
  **Automation → Webhooks**, or `hooks = [...]` on its host entry) gets every
  delivery of the hooks it follows. One that wasn't running when a request
  came in catches up on what arrived within its `[webhooks] replay` window
  (default `15m`) when it reconnects; older ones are counted as missed, not
  fired.
- **The Triggers editor** offers `webhook` as a source and lists the hook names
  of the venvs the console follows as events.

### One console takes it: claim

Without `claim`, every console following the venv runs the trigger, so three
consoles mean three agents working on one request. With `claim = true` (the
same switch shared-mailbox triggers use) a matched request parks as **Awaiting
claim** with a **Claim** button on every console instead. Clicking it runs
`runspec-scheduler hooks claim --id <delivery>` on the host as the venv owner:
the first claim wins, the winning console runs the trigger, and the others turn
to **claimed by `<name>`** as soon as the claim reaches their follow stream.
The name is your login on the host (`SUDO_USER`), the same as for approvals.

A claim only decides which console's trigger acts. It doesn't approve, run or
settle anything on the host, and it works for any delivery, including ones
the host ran itself. Claiming needs runspec-scheduler 0.13.0 or later on the
host and runspec-console 0.445.0 or later; an older host says so on the card
and the request stays parked. From the host:

```bash
bin/runspec-scheduler hooks claim --id 17 --note "mine"
```

## Approving from the host

```bash
bin/runspec-scheduler hooks deliveries                  # newest first
bin/runspec-scheduler hooks show --id 17                # one in full, with its body
bin/runspec-scheduler hooks approve --id 17 --note "checked the disk"
bin/runspec-scheduler hooks deny --id 18
```

Run these as the venv owner. Under `sudo -u <owner>`, the approval is recorded
under your own name (`SUDO_USER`).

## Muting

Mute a hook (or every hook on a venv) for a while: you stop counting as
someone watching it, and everyone following the venv sees who muted what until
when. When everyone following has muted a hook, its approvals are mailed
straight away. From the host:

```bash
bin/runspec-scheduler hooks mute --hook alerts --for 2h --reason "change freeze"
bin/runspec-scheduler hooks unmute --hook alerts
bin/runspec-scheduler hooks unmute            # all of your mutes
```

`--hook "*"` mutes (and unmutes) every hook at once; that mute is separate from
a mute of one hook.

## Settings reference

`[hooks]` in `{venv}/runspec_scheduler.toml`, changed with
`hooks settings --set key=value` (an empty value puts the default back):

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turn the listener on |
| `port` | — | The port to listen on |
| `bind` | `127.0.0.1` | The address to listen on. Anything but loopback needs `cert` and `key` |
| `base_path` | — | A prefix for every address, e.g. `/runspec/ops` behind a shared proxy |
| `cert`, `key` | — | PEM certificate and key for HTTPS (TLS 1.2 or later) |
| `max_body` | `256k` | Bigger bodies are refused (413) |
| `keep` | `7d` | How long settled deliveries stay in the spool |
| `max_connections` | `64` | Open connections at once; more get 503 |
| `rate_per_minute` | `120` | Per hook; more get 429 with `Retry-After` |
| `source_rate_per_minute` | `600` | Per sending address |
| `trusted_proxies` | — | Proxies whose `X-Forwarded-For` is believed (for `allow` and the per-sender limit) |
| `approve_within` | `1h` | How long an approval can wait before it expires |
| `expect_claim_within` | `15m` | Mail an approval nobody has picked up after this long (`0` = never) |
| `max_runs` | `4` | Webhook runs at once, across all hooks |
| `max_queue` | `1000` | Deliveries waiting to be worked through before new ones get 503 |
| `min_free_mb` | `100` | Free space the spool's disk must have, or new deliveries get 503 |

`[smtp]`, for mailing approvals (`hooks settings --smtp key=value`):

| Setting | Default | What it does |
|---|---|---|
| `relay` | — | The mail relay's host name |
| `port` | `25` | Its port |
| `from` | — | The sender address |
| `to` | — | Who gets approval mail (a hook's `email_to` wins) |
| `starttls` | `false` | Use STARTTLS |
| `timeout` | `10` | Seconds to wait for the relay |

## Sending the output back

By default a hook answers `202` as soon as the request is stored, and the
runnable reports back on its own if the sender needs to know anything. With
`respond = "output"`, the request is held until the run finishes and the answer
carries the result, so the sending product can use it straight away: Jira
automation's *Send web request* with *Wait for response* (`{{webRequest.body.output}}`),
or a Rundeck HTTP step.

```toml
[[hook]]
name = "disk-cleanup"
token_sha256 = "…"
respond = "output"
respond_within = "25s"   # Jira gives up after 30 s
run = "cleanup-tmp"
args = { host = "{{ host }}" }
```

A run that finishes in time answers `200`:

```json
{
  "id": 17,
  "hook": "disk-cleanup",
  "status": "done",
  "ok": true,
  "state": "ran",
  "exit_code": 0,
  "output": {"freed_mb": 812},
  "output_truncated": false,
  "items": [ … ]
}
```

- `output` is the run's standard output: parsed when the runnable printed JSON
  (so `{{webRequest.body.output.freed_mb}}` works), else the text. Never its
  standard error. It's cut to `respond_max`, and `output_truncated` says so.
- `ok` is true when every item ran with exit code 0 (or was skipped by a rule).
  A failed run still answers `200`: read `ok` / `exit_code`.
- `state`, `exit_code` and `output` sit at the top when the request is one item;
  a [split](#a-hook) body lists every item in `items`.
- A delivery that waits for an approval, or a run still going after
  `respond_within`, answers `202` with `"status": "pending"` or `"running"`
  and a `status_url`. Refused or skipped deliveries answer at once.

The output goes back to whoever holds the hook's token, so only switch it on
for a runnable whose output that sender may see. It's also in the run's log, as
for every run.

### Asking how a request went

Any hook's `202` carries `status_url`, relative to the address it was posted
to. `GET` it with the hook's own token (or basic auth) for the same answer as
above, `200` with `"status": "done"`, `"pending"` or `"running"`. The output is
there only for a `respond = "output"` hook.

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://ops-01:8977/hook/disk-cleanup/delivery/17
```

## What the sender gets back

| Code | Meaning |
|---|---|
| `200` | A `respond = "output"` run finished in time (see [Sending the output back](#sending-the-output-back)), or a `GET` of a `status_url` |
| `202` | Accepted: `{"id": …, "status": "queued", "status_url": …}` (or `"pending"` / `"running"` for a `respond = "output"` hook) |
| `401` | Missing or wrong token / password |
| `403` | The sender's address isn't in `allow` |
| `404` | No such hook, or it's turned off |
| `405` | Not a `POST` (a `status_url` takes only `GET`) |
| `411` | No `Content-Length` (chunked bodies aren't accepted) |
| `413` | The body is over `max_body` |
| `429` | Over a rate limit; see `Retry-After` |
| `503` | Too many connections, too much waiting, or the disk is nearly full; see `Retry-After` |

Rejections are counted in `status` and written to the audit log at most once a
minute per reason.

## Files

| File | What it is |
|---|---|
| `{venv}/runspec_scheduler.toml` | `[hooks]`, `[smtp]` and console-made `[[hook]]` tables |
| `{venv}/hooks.d/*.toml` | Deployed hooks (win over console-made ones) |
| `{venv}/hooks/spool.sqlite` | Every delivery, its events, mutes and followers (owner-only); a `respond = "output"` run's output once it finishes |
| `{venv}/hooks/output/` | A `respond = "output"` run's standard output while it runs (owner-only, moved into the spool when it ends) |
| `{venv}/.runspec_scheduler_env` | Credentials for webhook runs and scheduled fires |
