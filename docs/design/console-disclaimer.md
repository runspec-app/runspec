# First-use AI-use disclaimer

A blocking acknowledgement notice for AI-enabled deployments that require the
operator to accept a usage disclaimer before the app is usable. Common compliance
requirement for AI-enabled products; the wording is configurable so a white-label /
corporate install ships its own.

## Requirements it meets

- **Configurable text** for white-label corporate installs (their own wording).
- **Off by default** for a vanilla `runspec-console` and for `[console] ai = false`
  installs — a disclaimer about AI use has no place on a non-AI console.
- A config seed can turn it **on** where the company requires it.
- Pops up for acknowledgement on **first use**; **not acknowledging blocks the app**
  entirely — the only other option is to **close** the app.

## Where it lives (config layering)

`[console.disclaimer]` in `config.toml`, alongside `[console.theme]` and
`[console] ai`. Living in `config.toml` means it rides **Config Sync** and the
**config seed** with no extra plumbing: a corporate seed ships `enabled = true` +
the wording and every fresh / air-gapped install is compliant. Like `ai` and the
theme, it is **deliberately not in the preserve lists**, so a seeded value
re-asserts on every seed re-apply / sync pull.

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

- `enabled` (default `false`) — master switch.
- `title` — modal heading (default `Notice`).
- `text` — the body, rendered as **markdown** (so it can link to a full policy).
  An empty body forces `enabled` false — a disclaimer with no wording is a no-op.
- `acknowledge_label` — the accept-button label (default `I acknowledge`).
- `version` — optional wording version; bumping it re-prompts everyone even if
  `text` is unchanged.

**Read-only from config — there is no Settings editor.** That's the point: an
operator must not be able to click their own compliance notice off. (They can edit
`config.toml` by hand, but the seed re-asserts, and it is never a one-click UI
toggle.)

## Hard-off when AI is off

`[console] ai = false` forces the disclaimer off regardless of its own config —
it's an AI-use notice, and a non-AI console has nothing to disclaim. The bridge
folds `Bridge._ai_enabled` into the effective `enabled`.

## Acknowledgement state (machine-local, never synced)

`runspec_disclaimer_ack.toml` in the app dir stores `{ hash, version,
acknowledged_at, user }`. It is **never** in Config Sync's `KNOWN_FILES` — the
acknowledgement is this machine's, not shared config (like `runspec_seed_state.toml`
and the active-profile pointer).

**Re-acknowledge on change.** The stored `hash` is a fingerprint of `text` +
`version` (`config.disclaimer_hash`). On launch the gate re-prompts whenever the
current wording's hash differs from the stored one, so a revised disclaimer (or a
bumped `version`) re-consents every operator, including those who accepted the
prior wording.

## The gate

A blocking Ant modal (`DisclaimerGate.tsx`) rendered at the App root when
`bridge.disclaimer().needs_ack` is true. It is non-dismissable — `closable={false}`
(no X), `maskClosable={false}` (backdrop does nothing), `keyboard={false}` (Esc does
nothing) — and has a **single Acknowledge button**:

- **Acknowledge** → `bridge.acknowledge_disclaimer()`; on success the app is usable.
  If the write fails, the gate stays up (the operator is never let in without a
  recorded acknowledgement).
- **To decline**, the operator closes the app the normal way — the **native window
  close button**. The modal covers only the web content, not the native titlebar,
  so the X is always available, and the window is created with `confirm_close =
  True` (`app.py _window_kwargs`), so closing already pops the app's native "are you
  sure?" guard.

There is deliberately **no in-app Close/quit button**. A programmatic
`window.destroy()` **bypasses** the `confirm_close` guard, which would be a second,
inconsistent exit path — so declining goes through the one native close path the
rest of the app already uses. A muted caption under the button ("To decline, close
the window.") points at it so the decline path is discoverable.

**The decision variable is never blank.** The operator's in-session decision is a
separate App state initialised to an explicit `'undecided'` (not `null`/blank) and
flipped to `'acknowledged'` only after a confirmed write — so a crash or bug can't
leave it in an ambiguous state that fails open. Declining isn't a value it holds
(the operator closes the app); an un-acknowledged relaunch reads `'undecided'`
again from the persisted state and the gate re-shows, which is the safe direction.
The backend states are likewise always explicit — `_compute_disclaimer_state`
returns one of `not_required` / `pending` / `acknowledged`, never blank, and an
empty or corrupt ack file resolves to `pending` (re-prompt), never `acknowledged`.

## No startup cost

`bridge.disclaimer()` is a **local** read (config + one small file + a hash) — no
network, SSH, or keyring — and is called once at mount, in parallel with the
existing `ai_enabled` / `brand_theme` reads. The window paints on today's timeline;
the gate renders client-side over the already-mounted UI. The acknowledgement
writes (and the event-log entry) happen on the click, never at launch.

## Recording the acknowledgement

Three layers, chosen for honesty about what "can't be edited after" actually means
on a Windows box:

1. **Console audit log** (always, every platform). A `disclaimer_ack` line in the
   tools audit log the History scanner already reads
   (`bridge_reporting._audit_disclaimer_ack`): the operational record.
2. **Windows Application event log** (always, silent, best-effort, Windows-only).
   Written via the built-in `eventcreate` (no pywin32, no admin, no pre-registered
   source — `runspec_console/eventlog.py`), source = the branded app slug so a
   white-label install shows its own brand. This is the native OS facility for such
   a record. **It is tamper-*evident*, not tamper-*proof*:** a local administrator
   can clear the Application log, but the OS records the clear itself (System log
   event 104), so tampering is visible rather than silent. Nothing local to a
   Windows box is immutable to a local admin — that is an honest limitation, not a
   flaw to hide, so it is documented plainly and never advertised in the operator's
   face. It is not gated behind a config toggle: it's simple, benign, and there's no
   reason to advertise deletability by making it optional.
3. **Telemetry enrichment** (only when telemetry is configured). *Not* the audit
   record — telemetry is opt-in and may be off or unconfigured, so nothing in-app or
   in the docs oversells it as the record. When it *is* on, each agent turn's span
   carries `runspec.disclaimer.state` (`acknowledged` / `pending` / `not_required`)
   plus `runspec.disclaimer.acknowledged_at` + `runspec.disclaimer.version` when
   acknowledged; the turn **metric** carries only the bounded tri-state label — the
   unbounded timestamp rides spans only, never a metric label (it would explode the
   time series). The Bridge injects `_disclaimer_telemetry` as an `observability`
   provider so the module stays decoupled and always-current.

The tri-state is meaningful because headless trigger/schedule turns aren't gated by
a UI modal, so automation *can* run while the wording is un-acknowledged
(`pending`) — that's real signal, not noise.

## Files

- `config.py` — `read_disclaimer`, `disclaimer_hash`, `read/write_disclaimer_ack`,
  `disclaimer_ack_path`.
- `bridge_config_ssh.py` — `disclaimer()`, `acknowledge_disclaimer()`,
  `_compute_disclaimer_state()`, `_disclaimer_telemetry()`.
- `bridge_reporting.py` — `_audit_disclaimer_ack()`.
- `eventlog.py` — Windows Application event-log write.
- `observability.py` — `set_disclaimer_provider` + span/metric enrichment.
- `console-ui/src/components/DisclaimerGate.tsx` + `App.tsx` wiring.
