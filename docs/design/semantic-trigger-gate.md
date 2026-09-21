# Semantic trigger gate — a fast decision model between regex and agent mode

**Status: proposal / investigation note. Not built.** Originally parked on
network access to a hosted decision API (Jev); **Laya** (§2.2) — an Apache-2.0,
locally-runnable model of the same shape — removes that blocker, so the design
is buildable now with data never leaving the firm. What it is parked on
instead: a **labelled event set** to calibrate the probabilities and to measure
zero-shot accuracy on our own questions (§9). Written so the design isn't lost;
nothing here changes shipped behaviour.

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

A **System One** decision model — TypeSafe AI's **Jev** prompted this note;
**Laya** is the open, local alternative — is built for exactly that slot: one typed decision per call (a
**choice** from a supplied list, a **score**, or a **yes/no**) returned with a
**calibrated probability**, in the tens-to-hundreds of milliseconds, at a small
fraction of an LLM call. It cannot produce free text, which is the property that
makes it safe to put in a gate: it can only say *whether*, never *what to do*.

## 2. Candidate models

### 2.1 Jev (hosted, TypeSafe AI — unverified)

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
data-retention terms, SDK vs raw HTTP. Hosted-only would also mean mail
subjects/bodies leave the firm on every gated event — a corporate decision that
precedes any build.

### 2.2 Laya (open, local — verified from the repo)

[github.com/NandhaKishorM/laya](https://github.com/NandhaKishorM/laya) — read
directly (README + `BENCHMARKS.md`), so these are the project's own numbers,
not press claims:

- **Apache 2.0**, weights on Hugging Face (`convaiinnovations/laya`),
  `pip install laya` on PyTorch + `transformers`. Python SDK only — no HTTP
  server or CLI ships with it (we'd wrap it, §6). **Runs entirely inside the
  firm.**
- Same three primitives, one forward pass: `choice` (options you supply, as
  key → description), `score` (ordinal), and **`noul`** — a binary
  probability 0..1, i.e. exactly the yes/no the gate needs. **Several questions
  evaluate in the same pass**, so the §8 choice params ride along with the
  gate for free. Every answer carries `confidence` + per-option
  `probabilities`.
- Three checkpoints, auto-picked by a `Router` on script detection:
  `laya` (ModernBERT-large, 421M, English, **512-token** context),
  `laya-multilingual` (mmBERT-base, 322M, 100+ languages, **1,024** tokens,
  ~2× faster), `laya-typed-decisions` (fine-tuned variant). The English model
  collapses outside English; the multilingual one is weaker on English.
- **Latency:** ~33–40 ms per question on a Tesla T4, ~7 ms/question batched;
  **~190–460 ms on CPU**. Fine for a mail-event gate either way, far below an
  LLM call.
- **Choice questions degrade past ~20 options** (a 77-option question fell to
  0.43 from a 0.87 benchmark) — bounds §8 and rules out catalogue pre-selection.

**Two caveats that shape the plan (§9):**

- **Calibration is not usable as shipped.** The repo's own expected-calibration
  error is 0.466 (`laya`) / 0.314 (`laya-multilingual`) raw, and 0.081 / 0.106
  after **temperature scaling fitted per (question type, option count) on a
  held-out labelled set**. So `threshold = 0.85` means nothing on day one — the
  parked-card Run/Dismiss labels are what the temperature gets fitted on.
- **Zero-shot on a new typed question is near chance.** Base checkpoints score
  0.36 on the typed-decisions benchmark against 0.32 random and *below* the
  0.46 majority-class baseline; the 0.77 headline comes only after fine-tuning
  on it, and the ~0.99 spam/phishing results were in the training data. A
  fresh "is this a password-reset request?" question must be assumed weak
  until measured on our own mails, or fine-tuned (the repo ships a notebook:
  ~30k questions, 4 epochs, 4–5 h on 2×T4). That is a different operating
  model from "just ask" — a per-question **evaluation / fine-tuning step** is
  part of adoption, not optional polish.

Young project (54 commits at the time of writing, 5.7k stars).

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
- **`fields`** — an explicit allowlist of payload keys handed to the decider.
  Required when `intent` is set; there is no "send everything" default. It is
  the data-governance control for a hosted provider (a trigger author states
  exactly what leaves the firm) and, for a local model, the **context-budget**
  control: Laya sees 512 tokens (English) / 1,024 (multilingual), so a long
  mail body is truncated — put `subject` and `from` before `body`, and render
  in that order.
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
before the one that costs a model call (off-process, or off-box for a shared
or hosted decider):

```
regex match   →  lock   →  rota   →  once_per_thread dedup  →  [intent]  →  dispatch
  (free)       (free)    (free)         (free)              (model call)
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
provider = "llm"            # llm (default) | mcp:<server> (Laya, §6.1) | jev | <plugin>
timeout = 5                 # seconds; on timeout the gate parks
base_url = ""               # provider endpoint (jev / plugin; unused for mcp:)
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
- **`provider = "mcp:<server>"`** — **the Laya path, and the recommended
  first real provider** (§6.1). Names a `[[mcp.servers]]` entry; the decider
  calls that server's `decide` tool over the console's existing long-lived MCP
  client session. No new dependency in the console.
- **`provider = "jev"`** — stdlib `urllib` HTTP client (no new runtime
  dependency, like `webfetch.py`), yes/no endpoint, returns `Verdict(answer,
  p_yes, latency_ms, raw)`. Written against the real API once its shape is
  known; until then a stub that raises `DeciderUnavailable`.
- **Plugins** — a dotted path / entry point, as adapters allow, for a
  corporate-internal classifier.
- Secrets follow the existing rules: `api_key_command` or a keychain-backed
  `credential`; only the id / command is written to `config.toml`, so `[decider]`
  rides Config Sync and the config seed like `[mcp]` and `[telemetry]`.

### 6.1 Laya as an MCP server — local sidecar or an internal shared server

**Torch does not go in the console venv** — the same reason Phoenix was
decoupled in 0.229.0: a ~GB dependency whose version the console would then
own. And it can't be a runnable either: a runnable cold-starts a process per
call, and loading a 300–400M-parameter model per event defeats the point.
The model needs a **warm, long-lived process**, which is exactly what the
console's MCP *client* already manages: `McpManager` (`mcp_client.py`) holds
an open `ClientSession` per `[[mcp.servers]]` entry on its own event-loop
thread, and `call_tool` marshals a call onto it via
`run_coroutine_threadsafe` — synchronous from the bridge, so `_build_decider`
can call it from the engine's gate with no new plumbing.

So Laya is packaged as a **small MCP server** (its own venv, `laya` + the
`mcp` SDK; proposed `packages/python/runspec-laya-mcp/`, *not* a runnable —
no `runspec.toml`, invisible to discovery) exposing one tool:

```
decide(state: str | object, questions: {name: {type: choice|score|noul, instructions, criteria}})
  → {answers: {name: {choice|score|noul, confidence, probabilities}}, routing: {checkpoint, reason}, latency_ms}
```

— a thin pass-through of Laya's own `Router.predict(state, questions)`
contract, plus **server-side temperature scaling** (§9) applied before the
probabilities are returned, so every console sees calibrated numbers and the
calibration file lives in one place. One server, two deployment shapes, chosen
purely by the `[[mcp.servers]]` entry:

```toml
# (a) per-desktop sidecar — CPU, ~200–450 ms/question, nothing leaves the box
[[mcp.servers]]
name = "laya"
transport = "stdio"
command = "C:/venvs/laya-mcp/Scripts/laya-mcp.exe"
autonomy = "manual"          # keep `decide` out of the chat agent's hands (see below)

# (b) internal shared server — one GPU box inside the firewall, ~33 ms/question
[[mcp.servers]]
name = "laya"
transport = "http"
url = "https://laya.internal.example/mcp"
credential = "laya-token"    # keychain-backed bearer, as any [mcp] server
proxy = ""                   # or the corporate CONNECT proxy if it sits across a zone
autonomy = "manual"

[decider]
provider = "mcp:laya"
```

**(b) is the interesting one for the firm.** A shared internal server means:

- **Data stays within the firm** — the gate sends mail text to a host the firm
  runs, under its own bearer token and TLS (the `[mcp]` client already handles
  `credential`, `header_credentials`, `proxy`, and the console-wide CA bundle).
  The §4 `fields` allowlist stays mandatory, now as minimal-data discipline
  rather than an egress control.
- **More powerful resource** — one T4-class GPU serves every console at ~33 ms
  a question / ~300 questions a second, instead of N desktops each paying CPU
  latency and holding a model in RAM.
- **One place to calibrate and fine-tune.** The temperature file and any
  fine-tuned checkpoint (§9) are server-side; upgrading them upgrades every
  console at once, with no per-desktop model download. And the labelled
  Run/Dismiss data from *every* console can be pooled there (it's internal) —
  which is what makes the ~30k-question fine-tuning budget reachable at all.
- Same MCP contract as (a), so a team can develop against a stdio sidecar and
  point `url` at the shared server for production; the `[[mcp.servers]]` entry
  rides Config Sync / the config seed, so the fleet is repointed from one
  versioned source.

Two things to be explicit about:

- **The decider calls `McpManager.call_tool` directly**, not through the
  agent's autonomy gate — the gate is a console-internal predicate, not an
  agent action. Whether the same `decide` tool is *also* offered to the chat
  agent as `mcp_laya_decide` is governed by the entry's ordinary `autonomy`;
  `manual` keeps it human-only there while the trigger gate still uses it.
  (An explicit `agent_tools = false` on a server entry — "connect, but don't
  fold its tools into the toolset" — would be cleaner; small follow-up.)
- **Server unavailable ⇒ park** (§5) applies unchanged; `mcp_status()` already
  surfaces a down server, so the parked reason and the Settings → MCP status
  agree.

**Governance, unchanged and explicit:**

- The decider **never executes anything**. Its output is one bit and a number;
  the trigger's action still goes through the same autonomy gate
  (`confirm` / `supervised` / `autonomous` / `manual`) it does today.
- **`[console] ai = false`** refuses `intent` on load exactly as it refuses
  `pre_turn` (`Trigger.from_config(ai_enabled=False)`), and `[decider] enabled =
  false` drops the gate fleet-wide (a trigger with `intent` then fails
  validation rather than silently running regex-only — refuse, don't degrade).
- **Where the event text goes is a config decision, audited per event.**
  With Laya (§6.1) it stays on the desktop or on a firm-run server; only the
  hosted `jev` provider sends it outside, which is a corporate decision that
  precedes enabling that provider. Either way `fields` is mandatory and the
  audit record names what was sent. For a mail source the realistic allowlist
  is `subject` + `from` + a `body` the operator has consciously chosen.
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
probability. Settings → a small **Decider** section (provider — with the `mcp:` choice
listing the configured MCP servers —, endpoint/credential for `jev`, timeout)
— or fold it into Model / API under a divider, since it's model-adjacent; the
Laya server itself is edited in the existing Settings → MCP tab. A **Test** button sends a pasted text to the configured
question and shows the verdict, the same way "Send test trace" verifies
telemetry.

## 8. Second step — `choice` params in `pre_turn`

Once the client exists, the LLM pre-turn can hand off the enumerable params:

```toml
[triggers.password-reset.pre_turn_choices]
priority = { question = "How urgent is this request?", options = ["p1", "p2", "p3"] }
queue    = { question = "Which team should handle it?", options = ["identity", "desktop", "network"] }
```

Each entry is one **choice** question — with Laya, *all* of them plus the
§4 intent question go in the **same forward pass** (one `decide` call), so the
extra questions are free. Keep each under ~20 options (§2.2). Results land in
the same `{{ _llm.* }}` namespace the maps already read, so a `map` doesn't care which path filled a
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
4. **Calibrate — mandatory for Laya, a check for Jev.** Bucket audited
   `p_yes` by decile against the operator's Run/Dismiss on parked cards. For
   Laya this labelled set is the *input*: fit one temperature per (question
   type, option count) on it — the repo's own recipe, which takes its ECE from
   0.47 to 0.08 — and load it into the MCP server (§6.1), so thresholds mean
   what they say fleet-wide. For a hosted provider it's the check that the
   calibration claim holds. Until a temperature is fitted, the threshold is a
   guess and the gate stays in shadow.
5. **Measure zero-shot per question, then fine-tune if needed.** Laya's base
   checkpoints are near chance on unfamiliar typed decisions (§2.2). Shadow
   mode (step 1) gives each `intent` question its own accuracy against the
   regex-fired + operator-labelled ground truth; a question that lands near
   the majority-class baseline is not adopted — it goes into a fine-tuning set
   (the repo's notebook, ~30k questions) run *once*, centrally, on the shared
   server's GPU. Pooling every console's Run/Dismiss labels on that server is
   what makes the budget reachable. Re-run the `expect_fire` suite (step 2)
   against the fine-tuned checkpoint before promoting it.

## 10. Open questions (decide before building)

**Laya / shared-server path (§6.1):**

- Who owns and runs the internal server — an ops team's GPU box, or a
  container on existing internal infra? A T4-class card is the benchmarked
  floor; CPU-only is workable for a low-volume pilot.
- Access control: one bearer token per console (keychain `credential`) or one
  shared token? Per-console is the console's model everywhere else.
- Which checkpoint(s) to load: English-only `laya` (512 tokens, stronger on
  English) vs `laya-multilingual` (1,024 tokens) — or both behind the Router,
  at ~2× the RAM.
- Where the temperature file and any fine-tuned checkpoint are versioned
  (server-side artefact store, not Config Sync — they're model files, not
  config).
- Retention of the pooled Run/Dismiss labels on the server — internal, but
  they contain mail text.
- Laya is a young project: pin the version in the sidecar venv and re-run
  `expect_fire` on upgrade.

**Jev / hosted path (§2.1) — only if that provider is ever wanted:**

- Self-hosted or on-prem option? If not, is sending mail subjects/bodies to a
  third-party API acceptable under the company's data rules at all?
- Option-count limit on a choice; input context limit (a long mail body).
- Pricing model, rate limits, data retention.
- Does the API return `p(yes)` directly, or a label plus confidence? The
  `Verdict` shape above assumes the former.

**Either:**

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
| `packages/python/runspec-laya-mcp/` | **New package**: MCP server wrapping Laya's `Router.predict` as a `decide` tool + server-side temperature scaling; own venv (torch); stdio + http transports; not a runnable |
| `runspec_console/decider.py` | `Verdict`, `DeciderUnavailable`, provider registry (`llm` / `mcp:<server>` / `jev` / plugin), pure request/response shaping; stdlib HTTP |
| `runspec_console/mcp_client.py` | optional `agent_tools = false` per server (connect, don't fold tools into the agent toolset) |
| `runspec_console/config.py` | `read_decider()` — mirrors `read_web()` / `read_telemetry()` |
| `runspec_console/triggers.py` | `Intent` dataclass; `intent` in `_TRIGGER_KEYS`; `from_config` validation (rule-only, `fields` required, AI-off refusal); engine `decide` injection + park/drop branch; audit fields |
| `runspec_console/bridge_triggers.py` | `_build_decider()` (the `mcp:` provider resolving to `McpManager.call_tool`); parked-intent Run/Dismiss (reuse `resume_claim` shape); `_trigger_run_pre_turn` choice hand-off (§8) |
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
- `docs/console-mcp.md` — the `[[mcp.servers]]` client the Laya server plugs
  into (transports, `credential`, `proxy`, autonomy)
- `docs/console-phoenix.md` — the precedent for keeping a heavy ML dependency
  out of the console venv
