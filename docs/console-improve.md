# Improve — evaluation & problem reports

The **Improve** tab measures how effective the agent, its runnables, and the app
itself are, and turns the worst cases into concrete recommendations you can act
on. It mines the data the console already keeps — your saved chat conversations
and the run-history audit records — so there is nothing extra to instrument and no
telemetry backend to stand up.

Improve is for **closing the loop**: instead of debugging one bad agent turn at a
time, you get a periodic (or on-demand) read on *where* the agent struggles —
which runnable it keeps mis-calling, which searches come back empty, which turns
stall — and a prioritised list of fixes to hand to whoever maintains the runnables
and the console.

---

## Two ways to use it

Improve is **one engine with two doors**, both on the Improve tab:

- **Evaluation sweep.** Pick a look-back window and click **Run evaluation**. You
  get an effectiveness report over every saved conversation and run in that window.
  Good as a periodic health check (see [Scheduling](#running-it-on-a-schedule)).
- **Report a problem.** Type a short comment describing what went wrong — e.g.
  *"the agent keeps picking the wrong host for restarts"* — and click **Run with my
  comment**. The evaluation runs the same way, but your comment is carried into the
  report so the (optional) recommendation pass can **correlate your complaint
  against the evidence** in the data, rather than guessing.

Either way you end up with the same kind of report; the only difference is whether
a human comment is attached.

---

## What it measures

Improve reconstructs what actually happened in each turn from the saved transcript
and the run-history audit records. The headline signals:

| Signal | What it tells you | Where it comes from |
|---|---|---|
| **Tool-search funnel** | Whether the agent searched for a runnable before running it, how many searches came back **empty** (a discovery miss), and how many `describe_runnable` round-trips it needed. | `search_runnables` / `describe_runnable` / `run_runnable` calls in the transcript |
| **Argument bounces** | How often a run was **rejected for a bad argument** before it even executed — the single best measure of "the agent doesn't understand this runnable's interface". Bucketed by cause (see below). | tool results beginning `Error:` / `✗` |
| **Stops & guidance** | Turns that hit the tool-step limit and had to be told to *continue* (`paused after N tool steps`), and self-recovery attempts after an error. | the assistant text of each turn |
| **Errors** | Turn-level durable errors, plus per-runnable **failure rates** (non-zero exit codes and exceptions) from run history. | assistant text + `run_summary` audit records |
| **Autonomy friction** | Tool calls the operator **denied** or **edited** at the confirm gate — a proxy for "the agent proposed the wrong thing". | `operator_decision` on each recorded tool call |

### Argument-bounce buckets

A bounced run is classified by *what* the agent got wrong, so you can tell a naming
problem from a validation problem:

| Bucket | Typical cause |
|---|---|
| `unknown_arg` | The agent invented an argument name that doesn't exist. |
| `missing_required` | A required argument was omitted. |
| `bad_choice` | A `choice`/`options` value outside the allowed set. |
| `out_of_range` | An `int`/`float` outside its `range`. |
| `bad_command` | A missing or unknown subcommand. |
| `other` | A bounce that didn't match the above (e.g. a `require-command` message). |

A high `unknown_arg` or `bad_choice` rate on one runnable is usually a
**documentation** problem — the arg description or `options` don't tell the model
enough — which is exactly the kind of thing the recommendation pass flags.

### Incident categories

The report lists the worst individual **incidents**, each linking back to a
conversation and turn so you can read the evidence. Categories, most-severe first:

| Category | Meaning |
|---|---|
| `durable_error` | The turn ended in a `⚠ Error:` the agent couldn't recover from. |
| `stop` | The turn hit the tool-step limit and paused for a *continue*. |
| `run_failure` | A runnable run exited non-zero / raised (from run history). |
| `arg_bounce` | A run rejected for a bad argument (carries the bucket + runnable). |
| `recovery` | The agent hit an error mid-turn and tried to recover. |
| `empty_search` | A `search_runnables` call returned no runnables or runbooks. |
| `autonomy_denied` | The operator denied a proposed tool call. |
| `autonomy_edited` | The operator edited a proposed call's arguments before approving. |

Incidents are prioritised by severity and capped (`max_incidents`, default 40) so a
noisy install doesn't bury the important ones.

---

## Anatomy of a report

Each run produces a report with:

- **Headline metric cards** — bounce rate, stops, durable errors, run-history
  failure rate, search counts, and autonomy denies/edits.
- **The reported problem** (problem reports only) — your comment, shown at the top.
- **Recommendations** (after you synthesize — see below).
- **Flagged incidents** — a filterable table of the worst cases with evidence.
- **Portable learnings** — a ready-to-commit `[[learning]]` TOML snippet.

Reports are saved automatically and listed newest-first in the left panel; click
one to reopen it, or delete ones you no longer need. A sidecar Markdown copy of
each report is written next to it so you can attach or paste the full text.

---

## Recommendations (optional LLM pass)

The metrics above are computed deterministically — no model, no cost, always
available. To turn them into *fixes*, click **Synthesize recommendations**: the
console's configured model reads the findings and a sample of the worst transcripts
(and your problem comment, if any) and returns a ranked list. Each recommendation
carries:

- a **category** — one of `agent_prompt`, `runnable_description`, `runnable_args`,
  `tool_visibility`, `tool_mode_config`, `runnable_bug`;
- the **target** (the runnable, argument, or config key affected);
- the **evidence** (why, citing the numbers);
- a **concrete fix** a developer or a coding agent can apply;
- a **confidence** rating.

For a problem report it also returns a short **correlation** — how your comment maps
onto what the data actually shows.

This needs a model provider configured under **Settings → LLM**. Without one, the
deterministic report still stands on its own; the synthesis button just reports
that no provider is configured. The pass is best-effort and never blocks or
corrupts the deterministic report.

---

## Handing a report to developers

A report is **addressed to the developers you configure** (`developer_emails`).
Click **Email developers** to open your default mail client, pre-addressed to them
with the report body as the message; the full report is also saved locally so you
can attach the Markdown copy.

Improve deliberately **never writes back** to any shared repository — [Config
Sync](console-team.md) is pull-only, and the single-user console shouldn't have
write access to the fleet's source of truth. Instead, if a finding is worth keeping
across every install, the report includes a **portable `[[learning]]` snippet** that
you (or your coding agent) drop into your Config Sync repo or [config
seed](white-label.md) on your own terms. That is how a durable learning reaches the
whole fleet: through your normal review-and-commit flow, not an automatic push.

---

## Letting the agent run it

`run_improve_report` is also a **built-in agent tool**, so you can just ask in chat:

> Run an Improve report for the last 14 days and tell me the top three problems.

It's read-only (autonomous — no confirm gate) and returns a compact summary; the
full report lands in the Improve tab like any other. Generating a report never
sends mail — emailing is always an explicit operator action.

---

## Running it on a schedule

Improve reuses the ordinary **prompt schedule** — no special setup. On the
[Schedules](console-automation.md) tab add a schedule whose action is a prompt such
as:

> Run the Improve report for the last 7 days.

At each firing the agent calls `run_improve_report` and the report appears in the
Improve tab's saved-reports list, ready for you to review, synthesize, and hand off.
A weekly sweep is a good default.

---

## Configuration — `[improve]`

Lives in `config.toml`, so it rides Config Sync and the config seed — a white-label
deployment can bundle who its developers are so a fresh install is ready to report.

```toml
[improve]
enabled = true                       # offer the Improve tab + agent tool (default true)
product_name = "Acme Ops Console"    # brand shown in the report + mail subject
developer_emails = ["dev@acme.example", "oncall@acme.example"]
window_days = 30                     # default look-back window
max_conversations = 200              # cap on conversations sampled per run
max_incidents = 40                   # cap on flagged incidents in a report
llm_synthesis = true                 # offer the recommendation pass (needs a provider)
```

`developer_emails` also accepts a single comma/space-separated string
(`"dev@acme.example, oncall@acme.example"`). See the [config
reference](console-reference.md#improve) for the full table.

---

## Privacy & storage

- **Reports are machine-local and never synced.** Each report is derived from this
  install's own chat transcripts and run history, which can contain sensitive
  content, so reports live under the app data directory (`improve/`) and never leave
  the machine on their own — you decide what to send, to whom, via the mailto.
- **Nothing is sent automatically.** Generating or synthesizing a report performs no
  outbound send; only **Email developers** does, and it just opens your mail client.
- **The synthesis pass** sends a *sample* of the worst transcripts to your
  configured model provider (the same one your agent already uses). If that matters
  for your data policy, leave `llm_synthesis` on but simply don't synthesize — the
  deterministic report needs no model — or set `llm_synthesis = false`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Report shows 0 turns | No saved conversations in the window. Chat history must be on (`[chat_history] enabled = true`, the default) and there must be activity in the last *N* days. |
| **Email developers** is disabled | No `developer_emails` configured — set them in `[improve]`. |
| Synthesize says "no provider configured" | Set a model under **Settings → LLM**, or leave synthesis off and use the deterministic report. |
| Run-history failure rate is 0 but runs clearly failed | The failing runs are older than the window, on a host that isn't connected, or didn't write a `run_summary` audit record. |
| The Improve tab isn't there | `[improve] enabled = false`, or the frontend build is stale — rebuild the console UI. |
