# AI-use disclaimer

Many AI-enabled products must present a **usage disclaimer** that the operator
acknowledges before using the app. runspec-console has a built-in, configurable
one: a blocking popup shown on first use, with wording you control for a
white-label / corporate install.

## Behaviour

- **Off by default.** A vanilla install shows nothing. It's also **hard-off when
  `[console] ai = false`** — a disclaimer about AI use has no place on a non-AI
  console — regardless of its own config.
- When enabled, the notice pops up **the first time** the console is used and
  **blocks the whole app** until it's acknowledged. The popup itself can't be
  dismissed with an X, the Esc key, or a backdrop click, and offers a single
  **Acknowledge** button — after which the app is usable. To **decline**, close the
  app the normal way (the window's close button), which pops the usual "are you
  sure you want to close?" confirmation; a muted hint under the button points this
  out.
- If you **revise the wording** (or bump `version`), everyone is re-prompted —
  even operators who accepted the previous wording.

## Configuration

`[console.disclaimer]` in `config.toml`:

```toml
[console.disclaimer]
enabled = true
title = "AI use notice"
text = """
This tool uses AI. **Verify important output** before acting on it, and do not
enter sensitive data unless authorised. See the [AI use policy](https://…) for
details.
"""
acknowledge_label = "I acknowledge"
version = "2024-06"
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Off ⇒ no popup. |
| `title` | `Notice` | Heading on the popup. |
| `text` | — | The disclaimer body, rendered as **markdown** (so it can link to a full policy). An empty body disables the popup — a disclaimer with no wording is a no-op. |
| `acknowledge_label` | `I acknowledge` | Label on the accept button. |
| `version` | — | Optional wording version. Bumping it re-prompts everyone even if `text` is unchanged. |

Because it lives in `config.toml`, it distributes through **Config Sync** and the
**config seed** like `[console.theme]` — a corporate seed ships `enabled = true`
plus the wording, and every fresh or air-gapped install is configured. A seeded
value re-asserts on each seed re-apply / sync pull (it isn't preserved).

**There is no Settings toggle** — the disclaimer is read-only from config. That's
deliberate: an operator shouldn't be able to click their own compliance notice off.

## What gets recorded when someone acknowledges

- **Console audit log** — always, on every platform: a `disclaimer_ack` entry with
  the OS user, timestamp, and the wording's version/hash. This is the everyday
  record (visible to the History scanner).
- **Windows Application event log** — on Windows, always, silently: one event is
  written to the Application log (source = your install's brand name). This is the
  native OS record for this kind of thing.

  A note on immutability: the event log is **tamper-evident, not tamper-proof**. A
  local administrator *can* clear the Application log, but Windows records the clear
  itself (a "log cleared" event), so tampering is visible rather than silent.
  Nothing stored locally on a Windows machine is beyond a local admin's reach — a
  truly immutable record has to leave the machine.
- **Telemetry** — *only if you've configured OpenTelemetry* ([Telemetry](console-telemetry.md)).
  When it's on, each agent turn's trace and metric carry whether the current
  disclaimer has been acknowledged (`runspec.disclaimer.state`), and the trace also
  carries the acknowledgement timestamp and version. This is enrichment on data you
  already export — it is **not** the audit record, and it does nothing when
  telemetry is off or unconfigured.

## Performance

Enabling the disclaimer adds nothing to startup: the check is a local read (config
+ a small acknowledgement file), the window opens on its normal timeline, and the
popup renders over the already-loaded UI. The event-log write happens when the
operator clicks Acknowledge, never at launch.
