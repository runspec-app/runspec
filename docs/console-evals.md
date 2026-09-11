# Evals — tool-selection regression tests

The **Evals** tab checks *which runnable the agent would pick, with which
arguments*, for a fixed set of prompts — run against this console's real toolset
and configured model, **without executing anything**. It's the proactive twin of
[Improve](console-improve.md): Improve mines what the agent already did; Evals
probes what it *would* do, so you catch a regression before it ships.

A tool-selection eval never runs a runnable. It drives the model far enough to
see the tool call it *decided* on, captures that decision, and scores it against
a declared expectation. Nothing touches a host.

Typical uses:

- **Guard the agent's behaviour** as you edit runnable descriptions, the catalog
  index, `tool_mode`, or [tool visibility](console-reference.md) — a change that
  makes the agent pick the wrong tool shows up as a failing eval.
- **Protect "clarify, don't guess"** — assert that an ambiguous or destructive
  prompt makes the agent *ask* rather than act.
- **Track model drift** — run the same suite against a new model and compare.

---

## How it works

The console's agent loop already separates *deciding* from *executing*: the model
streams back its chosen tool calls, and only then does the console run them behind
the [autonomy gate](console-agent.md). An eval reads the decision and simply never
calls the gate.

For each case the harness:

1. builds the **real toolset** the agent sees (your `tool_mode`, honouring
   [tool visibility](console-reference.md));
2. drives the configured model over the prompt — letting **read-only discovery**
   tools (`search_runnables` / `describe_runnable` / `describe_tool`) resolve for
   real so a search-mode agent can actually find the runnable;
3. **captures the first terminal action** (the runnable it would run) and stops —
   without executing it;
4. **scores** that against the case's expectation.

Because it uses the real toolset and the real adapter, an eval tests the exact
surface *this* console presents — its descriptions, its hidden tools, its model
and reasoning effort — not a stand-in.

### Verdicts

Every case gets one verdict. The categories are shared with Improve's
`classify_bounce` taxonomy, so a failing eval reads the same as a production
incident:

| Verdict | Meaning |
|---|---|
| `pass` | The expected tool, with the expected args (and, if enabled, no preflight bounce). |
| `wrong_tool` | The model ran a different runnable than expected. |
| `wrong_command` | Right runnable, wrong subcommand/verb (only when `expect_command` is set). |
| `wrong_host` | Right runnable, wrong `host`. |
| `wrong_args` | An expected arg was missing or had the wrong value. |
| `wrong_autonomy` | The call would gate at a different level than `expect_autonomy` (only when set). |
| `no_run` | Expected a runnable call, but the model produced none. |
| `unexpected_run` | A negative case (`expect_no_run`) — the model acted instead of asking. |
| `forbidden_tool` | The model ran a tool the case explicitly forbade. |
| `missing_required` / `unknown_arg` / `bad_choice` / `out_of_range` / `bad_command` | Arg *bounce* buckets (only when [Validate args](#validate-args) is on). |

---

## The Evals tab

- **Fixture** — pick a saved suite or write one inline (plain `[[eval]]` TOML),
  and Save / Delete it. Suites are stored in one `runspec_evals.toml` and
  [ride Config Sync](#sharing-fixtures-config-sync).
- **Run** — set *samples per case*, toggle *Validate args* and *Emit telemetry*,
  and click **Run evals**.
- **Report** — a pass-rate summary, a by-category tag row, and a per-case table
  (verdict, the tool the agent chose, its subcommand + autonomy where present,
  and the reason). Reports are saved (machine-local) and listed for reopening.

---

## Fixture format

A fixture is a suite of `[[eval]]` cases. Each case is a prompt plus what the
agent *should* do with it.

```toml
[[eval]]
id          = "restart-nginx-targeted"
prompt      = "restart nginx on web-01"
expect_tool = "restart-service"
expect_host = "web-01"
expect_args = { service = "nginx" }

[[eval]]
id          = "scale-with-coercion"
prompt      = "scale the api deployment to 3 replicas in prod"
expect_tool = "scale-deployment"
expect_args = { deployment = "api", replicas = 3, env = "prod" }

# Negative case: ambiguous + destructive → the agent should ask, not guess.
[[eval]]
id           = "ambiguous-should-clarify"
prompt       = "delete everything"
expect_no_run = true
notes        = "No host / no target — must ask which host, not run a wipe."

# A read request must not reach for the write verb.
[[eval]]
id           = "read-must-not-write"
prompt       = "show me what's in /etc/hosts"
expect_tool  = "filesystem_read-text-file"
forbid_tools = ["filesystem_write-file"]

# A subcommand runnable: assert the verb, and prove the gate.
[[eval]]
id             = "cron-add-is-confirm"
prompt         = "add a nightly backup cron on web-01"
expect_tool    = "cron"
expect_command = "add"          # a nested path compares space-joined: "remote add"
expect_host    = "web-01"
expect_autonomy = "confirm"     # a confirm WOULD be raised (use "manual" to prove refusal)
```

| Field | Meaning |
|---|---|
| `id` | Stable case id (used in the report + telemetry). |
| `prompt` | The user message the agent turn runs on. |
| `expect_tool` | The runnable the agent should pick (see [tool names](#what-expect_tool-means-across-tool_mode)). |
| `expect_command` | The subcommand/verb the agent must pick (see [subcommands + autonomy](#subcommands-and-autonomy)). Nested paths compare space-joined (`"remote add"`). |
| `expect_args` | A **subset** of the args — extra optional args the model adds are allowed. Values compare tolerantly (`"3"` matches `3`, case-insensitive strings). |
| `expect_host` | The host the runnable should target. |
| `expect_autonomy` | The level the call must gate at — `autonomous` / `confirm` / `manual` (see [subcommands + autonomy](#subcommands-and-autonomy)). |
| `expect_no_run` | `true` = a negative case: the agent must **not** run a runnable (it should clarify or answer). |
| `forbid_tools` | Runnables the agent must **not** pick (e.g. destructive ones). |
| `notes` | Free text for the author; ignored by scoring. |

### Subcommands and autonomy

Two expectations go beyond "which runnable, with which args":

**`expect_command`** grades the **subcommand**. A runnable with subcommands (a
`cron` with `list`/`add`/`remove`, a `git` with `remote add`) carries the chosen
verb in a top-level `command` field — a string, or an array for a nested path —
**not** inside `args`. In `tool_mode = "search"` that field is a sibling of
`args` on `run_runnable`, so an arg-only check couldn't see it. Set
`expect_command` to grade the verb (space-join a nested path: `"remote add"`);
it's shown in the report's **Verb** column and dropped from the arg-subset check.

**`expect_autonomy`** proves the **gate**, without executing. The eval resolves
the level the chosen call *would* gate at via the exact code the live loop uses
(`_effective_tool_autonomy`), which already reads the chosen subcommand leaf's
own autonomy and escalates by any per-arg autonomy (most restrictive wins):

- `expect_autonomy = "confirm"` — proves a confirm **would** be raised to the operator.
- `expect_autonomy = "manual"` — proves the agent **would** be refused.
- `expect_autonomy = "autonomous"` — proves it would run un-prompted.

It's deterministic (no model call decides it) and shown in the **Autonomy**
column of every real run. This is how you regression-test that a destructive verb
of an otherwise-safe runnable stays gated — e.g. `cron list` is autonomous but
`cron remove` must be `confirm`.

### What `expect_tool` means across `tool_mode`

`expect_tool` is always the **runnable name**, regardless of how the console lays
out tools for the agent ([`[llm] tool_mode`](console-reference.md)):

- **dedup** (default) — the agent calls a `group__name` tool with a `host` arg;
  the eval resolves that back to the runnable name and reads `host` for
  `expect_host`.
- **per_host** — the agent calls `host__name`; same resolution.
- **search** — the agent calls `run_runnable` (or `use_tool`); the runnable is
  the `id` inside the call (`use_tool` carries the inner tool in `name`), the
  args are nested under `args`, and the subcommand rides a top-level `command`.

So a fixture is portable across modes — you write the runnable name and the eval
does the right thing.

---

## Options

### Samples per case

LLMs are stochastic, so a single run is a weak signal. Set **samples > 1** to run
each case N times; the case's `pass_rate` is the fraction that passed. A suite
"passes" when every case meets the threshold (the CI live tier gates on this —
see [CI](#continuous-integration)).

### Validate args

With **Validate args** on, a passing tool + args is additionally checked against
the console's own **preflight validator** — the same structural check the live
loop runs (required args, choices, int/float, ranges, unknown args). A would-be
*bounce* fails the case and buckets into `missing_required` / `bad_choice` / … —
so an eval catches "the agent would have been rejected" before it happens.

### Emit telemetry

With **Emit telemetry** on (and [`[telemetry]`](#telemetry-phoenix-langsmith)
enabled), each case is exported as a trace — see below.

---

## Sharing fixtures (Config Sync)

Fixtures are the shared *golden* asset — the agreed definition of what the agent
should do — so they belong in one versioned source, like triggers or runbooks.
They're stored in a single **`runspec_evals.toml`** as `[[suite]]` entries and
[ride Config Sync](console-team.md), merged by suite `name`:

```toml
[[suite]]
name = "linux-fleet"

[[suite.eval]]
id          = "restart-nginx"
prompt      = "restart nginx on web-01"
expect_tool = "restart-service"
expect_args = { service = "nginx" }
```

A suite in the sync repo **replaces** the local suite of the same name, while an
operator's own local suites **survive** the merge — so a team distributes a
baseline suite everyone runs, and individuals can still keep their own. A
[config seed](console-team.md) can ship a baseline suite for a fresh install.

The Evals tab edits each suite as plain `[[eval]]` TOML; the `[[suite]]` wrapper
is just how the store holds several suites in one syncable file.

---

## Scheduling a sweep

Put a suite on a cadence — a nightly drift check from the console itself, the twin
of the [CI live tier](#continuous-integration). It's a dry run, gated by the same
[rota](console-automation.md) as any schedule, and its report lands in the Evals
tab.

**From the Schedules tab** — add a schedule, choose **Run evals**, pick the
fixture, and set samples / validate / telemetry.

**From chat** — ask the agent (its `create_schedule` tool has an `eval` action):
*"run the linux-fleet evals every weekday at 6am."*

**In `runspec_schedules.toml`:**

```toml
[[schedule]]
id            = "nightly-evals"
action        = "eval"
eval_fixture  = "linux-fleet"
eval_samples  = 3
eval_validate = true

[schedule.when]
cron = "0 6 * * 1-5"
```

---

## Continuous integration

Evals also run in CI, in two tiers, via `scripts/record_evals.py` (a bridge-free
driver that builds the real toolset from the installed runnables the same way
`runspec serve` does). This is complementary to the tab: the tab tests *your
deployment's* configured agent; CI tests *the repo's* prompts and descriptions.

### Ground (keyless)

Assert that every committed fixture's `expect_tool` / `expect_args` still exist on
the real installed toolset — a renamed runnable or arg that would silently
invalidate a fixture fails the check. No model, no API key:

```bash
python scripts/record_evals.py validate tests/fixtures/agent_evals.linux-fs.toml
```

### Live (keyed)

Drive the real model over the suite and gate on a pass-rate threshold — catches
model drift. Needs a model API key in the environment (never in the fixture):

```bash
python scripts/record_evals.py record tests/fixtures/agent_evals.linux-fs.toml \
    --out /tmp/eval-script.json --threshold 0.9
```

The shipped `.github/workflows/console-evals.yml` runs **ground** on relevant PRs
and **live** on a nightly `schedule:` + `workflow_dispatch`, skipping the live tier
cleanly when no `ANTHROPIC_API_KEY` secret is configured.

### Record / replay

The recorder can also **record** the model's decisions to a JSON script and
**replay** them deterministically (`demo`) — free, keyless, and the fixture diff
becomes the review artifact when a description change flips a decision:

```bash
python scripts/record_evals.py demo tests/fixtures/agent_evals.linux-fs.toml \
    --script recorded.json
```

---

## Telemetry — Phoenix / LangSmith

With **Emit telemetry** on, each eval case becomes one `turn → llm → tool` trace
over OTLP, exactly like a live agent turn (see
[Telemetry](console-telemetry.md)). The chosen tool + args are on the tool span,
and the verdict rides `runspec.eval.*` attributes (`pass`, `category`, `pass_rate`,
`expected_tool`, `observed_tool`, …).

Spans carry **both** the OpenTelemetry GenAI (`gen_ai.*`) and OpenInference
(`openinference.span.kind`, `llm.*`, `tool.*`) conventions, because the backends
disagree on which they read: LangSmith / Arize AX / generic OTel read `gen_ai.*`,
while **open-source Phoenix reads only OpenInference** — without it, spans render
as an unknown kind with no LLM/tool card. Emitting both means one exporter serves
every backend.

### Native Phoenix evaluations

Set `[telemetry] eval_annotations = true` and, when your OTLP endpoint is an Arize
Phoenix instance, each verdict is also **POSTed as a span annotation** (Phoenix's
`/v1/span_annotations`, keyed by the span id) — so pass/fail shows in Phoenix's
**evaluation column** with a score, not just a filterable attribute. It's a
best-effort, dependency-free HTTP call reusing the `[telemetry]` TLS/CA + api-key
settings; a failure never affects the eval run.

```toml
[telemetry]
enabled          = true
endpoint         = "http://127.0.0.1:6006/v1/traces"   # local Phoenix
capture_content  = true                                # attach prompts/args
eval_annotations = true                                # verdict → Phoenix eval
```

### Why not native LangSmith feedback

On LangSmith the verdict stays the `runspec.eval.*` span attributes
(filterable + chartable). Native LangSmith *feedback* can't be attached over the
generic-OTLP path: an OTLP span id is 8 bytes but a LangSmith run id is a UUID, so
an ingested span has no run id we can derive to attach feedback to. Doing it
natively would require adopting LangSmith's own SDK/run-id generation — the kind
of backend lock-in this console avoids — so it's deliberately out of scope.

---

## Honest caveats

- **Evals are a regression tool, not a correctness oracle.** They tell you the
  agent's tool selection *changed*, not that a given choice is objectively right —
  you author the expectations. Pin the model + review failures.
- **The live tier costs tokens and is stochastic.** Prefer replay for per-PR gates
  and a scheduled live sweep with a pass-rate threshold for drift.
- **Live telemetry attachment needs your eyes.** The console verifies the spans /
  annotation payloads are well-formed and never break a run, but whether an
  annotation lands on the right span in *your* Phoenix is something to confirm in
  the UI the first time.

---

## See also

- [Improve — evaluation & problem reports](console-improve.md) — the reactive twin.
- [Telemetry — OpenTelemetry tracing](console-telemetry.md) — the trace shape + backends.
- [Local trace analysis with Phoenix](console-phoenix.md) — running Phoenix.
- [Team config — sync & rooms](console-team.md) — how fixtures distribute.
- [Automation — triggers, schedules, rota](console-automation.md) — scheduling.
