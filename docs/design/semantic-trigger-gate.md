# Semantic trigger gate — a fast decision model between regex and agent mode

**Status: proposal / investigation note. Not built.** Parked until a
System-One-style decision API is reachable from the corporate network. Written
so the design isn't lost; nothing here changes shipped behaviour.

## 1. Why

Today a trigger has exactly two ways to decide whether an event is "one of
mine":

- **Rule mode** — `Trigger.matches()` (`triggers.py`) is a pure regex `search`
  over payload fields. Deterministic, free, instant — and blind to meaning. "Does
  this mail actually ask for a password reset?" is not a regex.
- **Agent mode** — a full headless LLM turn with runnable tools
  (`_run_isolated_agent_turn`). It understands the mail, but every matched event
  costs a frontier-model call carrying the whole toolset, and the *decision* to
  act is entangled with *acting*.

There's a gap between them: a trigger that wants **semantic matching with a
deterministic action**. Operators fill it today either by over-matching in rule
mode (regex fires on every mail mentioning "password", the runnable runs, the
operator cleans up) or by promoting the trigger to agent mode just to get intent
detection.

A **System One** decision model — TypeSafe AI's **Jev** is the one that prompted
this note — is built for exactly that slot: one typed decision per call (a
**choice** from a supplied list, a **score**, or a **yes/no**) returned with a
**calibrated probability**, in the tens-to-hundreds of milliseconds, at a small
fraction of an LLM call. It cannot produce free text, which is the property that
makes it safe to put in a gate: it can only say *whether*, never *what to do*.

## 2. What Jev is (as understood — unverified)

From press coverage at the time of writing (September 2026); the vendor pages
were not reachable from the session that wrote this, so **treat every figure as
a claim to verify**:

- Non-autoregressive — the decision is produced in a single forward pass, not
  token by token. Quoted latency 70–500 ms; quoted 40–200× faster and 40–400×
  cheaper than a frontier LLM.
- Three question types: **choice** (pick from options *you* supply), **score**
  (rate on a scale), **yes/no**. Each call returns one decision plus a
  probability.
- Trained with "Reinforcement Learning for Calibrated Decisions" so the returned
  probability is meant to track the real hit rate — the property a threshold
  depends on.

Unknown and must be checked before building: option-count limit for a choice,
input context limit, pricing, whether a self-hosted / on-prem option exists,
data-retention terms, SDK vs raw HTTP.

## 3. Where it fits — and where it does not

| Slot | Fit | Why |
|---|---|---|
| **Trigger intent gate** (this doc) | **Yes** | A yes/no with a probability is exactly the shape the engine's gate chain wants. |
| **`choice`-typed params in `pre_turn`** (§8) | Yes, narrow | A param whose value space is enumerable (a `choice` arg, a flag, a host/group enum, a queue or priority label) is one choice call. |
| Free-text extraction in `pre_turn` (ticket id, hostname, subject) | No | No free text. Stays on the LLM `emit_params` path. |
| Chat tool selection | No (for now) | The agent loop relies on native tool use; the model picks tool *and* args together. At most a pre-selection shortlist in `tool_mode = "search"`, and a wrong shortlist hides the right tool. Needs evidence first. |
| Arg values in general | No | Paths, hosts, names, free strings. |

## 4. Behaviour (trigger contract)

A rule trigger gains an optional `intent` table. The regex `match` stays the
cheap pre-filter; `intent` is a second gate that runs only on events the regex
already passed.

```toml
[triggers.password-reset]
source = "outlook"
event = "new_mail"
mode = "rule"
run = "web-01__reset-password"
map = { user = "{{ _llm.user }}" }

[triggers.password-reset.match]
subject = "(?i)password|login|locked"      # cheap regex pre-filter, unchanged

[triggers.password-reset.intent]
question = "Is the sender asking us to reset their own account password?"
fields = ["subject", "body", "from"]       # payload keys sent to the decider (allowlist)
threshold = 0.85                           # fire at/above this probability
on_low = "park"                            # park | drop   (default park)
```

- **`question`** — the yes/no put to the decider, with the allowlisted payload
  fields rendered as context (`render_event`-style, same text the LLM pre-turn
  sees, restricted to `fields`).
- **`fields`** — an explicit allowlist of payload keys that leave the machine.
  Required when `intent` is set; there is no "send everything" default. This is
  the data-governance control: a trigger author states exactly what an external
  API sees.
- **`threshold`** — fire when `p(yes) >= threshold`. Below it, `on_low` decides.
- **`on_low = "park"`** (default) — the card moves to `AWAITING_HUMAN` with the
  probability and the decider's answer on it; the operator clicks **Run** or
  **Dismiss**. Reuses the parked-card mechanism the claim gate already has
  (`_parked_claims` / `resume_claim`) — a parked intent card is the same shape
  with a different reason. **`"drop"`** skips it like a rota skip, with
  `skip_reasons[name] = "intent below threshold (p=0.41)"` so the Event log
  explains it.
- **Rule mode only.** An agent-mode trigger already reads the event with a full
  model; layering a second gate in front would be redundant spend. `intent` on
  an agent-mode trigger is a validation error, like `pre_turn` on one.

Nothing else in the trigger changes: `map`, `actions`, `pre_turn`, `claim`,
`once_per_thread`, `ignore_rota`, autonomy — all as today.

## 5. Where the gate sits in the engine

`TriggerEngine.feed()` (`triggers.py`) already runs a fixed chain per matched
trigger. The intent gate goes **last**, so every free gate has had its say
before the one that costs money and sends data off-box:

```
regex match   →  lock   →  rota   →  once_per_thread dedup  →  [intent]  →  dispatch
  (free)       (free)    (free)         (free)                (paid, off-box)
```

Concretely, inside the loop after the `once` dedup and before
`matched.append(...)`:

```
if trigger.intent:
    verdict = self._decide(trigger, event)          # injected, like _clock_ok / _lock_ok
    if verdict.error:                               # decider down / timeout
        park(card, reason="intent gate unavailable") ; continue
    if verdict.p_yes < trigger.intent.threshold:
        if trigger.intent.on_low == "park": park(card, verdict) ; continue
        skipped.append(name); skip_reasons[name] = f"intent below threshold (p={verdict.p_yes:.2f})"; continue
    card.log(f"intent: yes p={verdict.p_yes:.2f}")
```

Same injection pattern as `clock_ok` / `lock_ok`: the engine takes a
`decide: Callable[[Trigger, Event], Verdict]` and stays free of HTTP. The
bridge builds it (`_build_decider`) the way `_build_clock_ok` / `_build_lock_ok`
are built today. `fire_test` ("Trigger now") bypasses it like every other gate;
`fire_paste` runs it (a human pasting text is present, but the gate is still the
point of the trigger, and the `{ok, skipped}` return already explains a no-run).

**Decider unavailable ⇒ park, never drop and never fire.** Firing would turn an
outage into over-matching (the exact failure the gate exists to prevent);
dropping would lose events silently. Parking keeps the human in the loop and is
visible.

**Every verdict is audited.** The per-event record (`_record_event`) carries the
decider's answer, probability, threshold, latency, and which fields were sent.
That is what §9's shadow mode reads.

## 6. The decider client — not a `ModelAdapter`

The console's adapters (`adapters/base.py`) implement `stream_with_tools` over
chat messages. A decision model isn't a chat model, so it does not become a
provider under `[llm]`. It is a small, separate client with a **provider
abstraction of its own**, so the gate is testable and usable before any fast
model is reachable:

```toml
[decider]
enabled = true
provider = "llm"            # llm (default — the console's own [llm] adapter) | jev | <plugin>
timeout = 5                 # seconds; on timeout the gate parks
base_url = ""               # provider endpoint (jev / plugin)
api_key_command = ""        # stdout is the key — same pattern as [telemetry] / adapters
credential = ""             # or a keychain Credentials id, as [mcp] / [telemetry] do
ca_bundle = ""              # private CA / TLS-intercepting proxy, mirrors [web]
tls_verify = true
proxy = ""                  # HTTP CONNECT URL, mirrors [ssh] proxy / [[mcp.servers]] proxy
```

- **`provider = "llm"`** — the reference implementation and the fallback: one
  call to the configured `[llm]` adapter with a single forced tool
  `emit_decision(answer: yes|no, confidence: 0..1)`, no runnable tools, exactly
  how `_trigger_run_pre_turn` (`bridge_triggers.py`) constrains the pre-turn.
  Slower and pricier than a System One model, but it exists today, needs no new
  egress, and lets the gate, parking, audit, shadow mode and evals all be built
  and exercised now. Its self-reported confidence is *not* calibrated; the
  threshold is a coarse dial there. When the proper model becomes reachable,
  `provider = "jev"` swaps the implementation with no trigger changes.
- **`provider = "jev"`** — stdlib `urllib` HTTP client (no new runtime
  dependency, like `webfetch.py`), yes/no endpoint, returns `Verdict(answer,
  p_yes, latency_ms, raw)`. Written against the real API once its shape is
  known; until then a stub that raises `DeciderUnavailable`.
- **Plugins** — a dotted path / entry point, as adapters allow, for a
  corporate-internal classifier.
- Secrets follow the existing rules: `api_key_command` or a keychain-backed
  `credential`; only the id / command is written to `config.toml`, so `[decider]`
  rides Config Sync and the config seed like `[mcp]` and `[telemetry]`.

**Governance, unchanged and explicit:**

- The decider **never executes anything**. Its output is one bit and a number;
  the trigger's action still goes through the same autonomy gate
  (`confirm` / `supervised` / `autonomous` / `manual`) it does today.
- **`[console] ai = false`** refuses `intent` on load exactly as it refuses
  `pre_turn` (`Trigger.from_config(ai_enabled=False)`), and `[decider] enabled =
  false` drops the gate fleet-wide (a trigger with `intent` then fails
  validation rather than silently running regex-only — refuse, don't degrade).
- **Data leaves the machine** on every gated event. `fields` is mandatory and
  explicit for that reason, and the audit record names what was sent. For a mail
  source the realistic allowlist is `subject` + `from` + a `body` the operator
  has consciously chosen to send. Whether that is acceptable at all is a
  corporate decision that precedes any build — and is the reason self-hosting is
  the first question to ask the vendor.
- The rota, lock auto-suspend and claim gates are upstream and unaffected.

**Telemetry.** A `decide` span under the trigger's trace (the `tool_span` /
`llm_span` helpers in `observability.py`, `runspec.decider.*` attributes:
provider, p_yes, threshold, outcome, latency) and a bounded counter
`runspec.decider.calls{provider, outcome}` beside the `runspec.tool.*` metrics.
Prompt text is captured only under `capture_content`, like everything else.

## 7. UI

Triggers editor (`TriggersEditor.tsx`), rule mode only: an **Intent** section
with the question, a field-picker (multi-select of the source's known payload
keys), a threshold slider, and the park/drop choice. Parked cards get a
**Run** / **Dismiss** pair in the `AWAITING_HUMAN` column showing the answer and
probability. Settings → a small **Decider** section (provider, endpoint,
credential, timeout, proxy) — or fold it into Model / API under a divider, since
it's model-adjacent. A **Test** button sends a pasted text to the configured
question and shows the verdict, the same way "Send test trace" verifies
telemetry.

## 8. Second step — `choice` params in `pre_turn`

Once the client exists, the LLM pre-turn can hand off the enumerable params:

```toml
[triggers.password-reset.pre_turn_choices]
priority = { question = "How urgent is this request?", options = ["p1", "p2", "p3"] }
queue    = { question = "Which team should handle it?", options = ["identity", "desktop", "network"] }
```

Each entry is one **choice** call; results land in the same `{{ _llm.* }}`
namespace the maps already read, so a `map` doesn't care which path filled a
key. The free-text params stay on `emit_params`. A `choice`-typed runnable arg
can supply its own `options` automatically (the trigger editor already lists a
runnable's declared args). Same cliff rule as the pre-turn: extract only, never
branch — a choice fills a *value*, it never selects an action step.

Not in scope: replacing the LLM pre-turn. The two coexist.

## 9. Evaluate before adopting

The console already has the tooling to say whether this is worth keeping;
build the gate so it feeds that tooling from day one.

1. **Shadow mode first.** `threshold = 0` + `on_low = "drop"` is a no-op gate
   that still calls the decider and audits the verdict. Run existing regex-only
   triggers with a shadow `intent` for a couple of weeks; the Event log then
   holds *"regex fired; decider said no at p=0.12"* rows. That is the
   false-positive rate the gate would have removed — measured, not argued.
2. **An Evals suite for gates.** Today `runspec_evals.toml` grades tool
   selection (`expect_tool` / `expect_args` / `expect_host`). Add a case kind
   `expect_fire = true|false` over a fixture event text + a trigger name, scored
   through the same gate function with the same decider (`samples > 1` →
   `pass_rate`, as now). Runs against `provider = "llm"` today and the fast
   model later — the same fixtures show whether the swap cost accuracy.
3. **Improve report signals.** Count parked-then-dismissed cards (gate fired
   on noise) and parked-then-run cards (gate was too strict) per trigger; both
   are threshold-tuning signals, and the Improve synthesis already ranks such
   findings.
4. **Calibration check.** Bucket audited `p_yes` by decile against the
   operator's Run/Dismiss on parked cards. If the vendor's calibration claim
   holds, the hit rate tracks the bucket; if it doesn't, the threshold is a
   guess and the gate should stay in shadow.

## 10. Open questions (ask the vendor / decide before building)

- Self-hosted or on-prem option? If not, is sending mail subjects/bodies to a
  third-party API acceptable under the company's data rules at all?
- Option-count limit on a choice (matters for §8 and for any future
  catalogue pre-selection); input context limit (a long mail body).
- Pricing model, rate limits, data retention.
- Does the API return `p(yes)` directly, or a label plus confidence? The
  `Verdict` shape above assumes the former.
- Should `intent` be allowed on **schedule**-sourced event triggers? Probably
  not — a schedule tick has no text to judge — but a `paste` source is a
  natural fit.

## 11. Not built (deliberately)

- **Chat tool routing / model routing.** A router that picks a model per prompt
  to cut cost belongs in the gateway in front of the console, not in the
  console: the console already treats the model behind a LangServe/gateway
  adapter as opaque, and `[llm.model_limits]` copes with a gateway that hides
  which model answered. Routing a tool-bearing chat turn mid-conversation also
  breaks prompt caching; the bounded headless paths (trigger/schedule turns,
  `pre_turn`, Improve synthesis, eval sweeps) are where a cheaper tier wins.
  The Evals and Improve tabs are how a routing regression would show up.
- **A per-trigger "trust the decider" autonomy bypass.** A high `p_yes` never
  lowers the action's autonomy level — same reasoning as the lock gate having no
  `ignore_lock`.

## Files (proposed)

| File | Change |
|---|---|
| `runspec_console/decider.py` | `Verdict`, `DeciderUnavailable`, provider registry (`llm` / `jev` / plugin), pure request/response shaping; stdlib HTTP |
| `runspec_console/config.py` | `read_decider()` — mirrors `read_web()` / `read_telemetry()` |
| `runspec_console/triggers.py` | `Intent` dataclass; `intent` in `_TRIGGER_KEYS`; `from_config` validation (rule-only, `fields` required, AI-off refusal); engine `decide` injection + park/drop branch; audit fields |
| `runspec_console/bridge_triggers.py` | `_build_decider()`; parked-intent Run/Dismiss (reuse `resume_claim` shape); `_trigger_run_pre_turn` choice hand-off (§8) |
| `runspec_console/observability.py` | `decide` span + `runspec.decider.calls` counter |
| `runspec_console/evals.py` / `bridge_evals.py` | `expect_fire` case kind |
| `console-ui/src/components/TriggersEditor.tsx` | Intent section; parked-card Run/Dismiss |
| `docs/console-automation.md`, `docs/console-reference.md` | `intent` table, `[decider]` section |
| `scripts/check_docs.py` | add `decider` to `NON_RUNSPEC_SECTIONS` once a `[decider]` example lands in `docs/*.md` |

## See also

- `docs/design/console-triggers.md` — the trigger contract and the "no workflow
  engine" line this stays under (§1, §5.1)
- `docs/design/lock-auto-suspend.md` — the injected-predicate gate pattern
  copied here
- `docs/design/email-claim-move.md` — the parked-card mechanism reused for
  `on_low = "park"`
- `docs/console-evals.md`, `docs/console-improve.md` — the measurement loop §9
  plugs into
