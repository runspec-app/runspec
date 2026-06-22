# Lock auto-suspend (presence governance control)

## Why

The working-hours rota answers *when* automation may run; this answers *whether a
human is present* when it does. The goal, in the operator's words: "we all walk
away from our desks and forget to turn off the rota — our PCs auto-lock, and while
they're locked the console must not act on our behalf." So while the workstation
is **locked**, reactive triggers don't fire. This is the presence half of a small
control set — denylist (*what* may run), rota (*when*), lock-suspend (*present*),
confirm-gates (*approval*) — and it produces an audit trail that the control fired.

It is a **governance / operational** control, **not a security boundary**: the
control plane runs as the operator and the config is editable, so it prevents
*accidental* unattended autonomous action and gives audit evidence — it does not
stop a determined insider. State that honestly; don't overclaim.

## Behaviour

- A Rota-tab toggle, **default ON** (absent `lock_suspend` ⇒ enabled), independent
  of the working-hours grid, with a **live indicator** (unlocked / locked /
  unknown) polled every 5s.
- While locked, **reactive** triggers (Outlook mail, chat, toasts, …) are skipped,
  logged (`trigger '…' skipped: session locked`) and recorded in the per-event
  audit feed.
- **Absolute:** unlike the rota, `ignore_rota` does **not** bypass it. There is no
  per-trigger override — an exception list would erode the exact guarantee that
  gives the control its value.
- **Scheduled jobs are exempt:** schedule-sourced events (a 2am batch) are
  pre-authorised, not reactions-while-away, so they still run while locked. (Only
  *event-action* schedules flow through the engine; *runnable-action* schedules
  never did and are unaffected.)
- **Fail-closed when on:** if the probe can't determine the state (`unknown`), the
  gate treats it as locked. An unreadable lock state must not let automation run
  unattended.

## Design (why it's light)

No event listener, no background thread, no new EventSource. The trigger gate is
**pull-based** — `clock_ok()`/`lock_ok()` are evaluated when an event arrives — so
lock-suspend is just one more injected predicate on `TriggerEngine`:

```
lock_ok = self._lock_ok() or event.source == "schedule"   # schedules exempt
...
if not lock_ok:                 # absolute — no ignore_rota bypass
    skip "session locked"
```

`Bridge._build_lock_ok()` reads the `lock_suspend` flag at build (toggling goes
through `save_rota` → restart) and the lock **state** live:

```
enabled = read_rota().get("lock_suspend", True)   # default ON
def lock_ok(): return True if not enabled else screen_lock_state() == "unlocked"
```

`winlock.screen_lock_state() -> "locked" | "unlocked" | "unknown"` is the
tri-state the fail-closed posture needs (`OpenInputDesktop`; `unknown` only when
the Windows probe raises; non-Windows is `unlocked`). `is_screen_locked()` is now
a fail-open bool wrapper (used by the lenient "Take a break" path).

## Files
- `runspec_console/winlock.py` — `screen_lock_state()` tri-state (+ `is_screen_locked` wrapper)
- `runspec_console/triggers.py` — `TriggerEngine(lock_ok=…)` + the dispatch gate (schedule-exempt, absolute)
- `runspec_console/bridge.py` — `_build_lock_ok()`, `screen_state()`, engine wiring, `list_triggers.screen_state`, `_console_context` note
- `console-ui/src/views/RotaView.tsx` — toggle + `LockIndicator`
- `console-ui/src/bridge/index.ts` + `mock.ts` — `screen_state`, `LockState`, `Rota.lock_suspend`

## Distribution / governance

The control's value grows when it's enforced centrally: ship it via **Config Sync**
(the rota file rides config-at-a-tag), so an org enforces `lock_suspend = true`
across the fleet from one versioned, reviewed source — change-management evidence
for free. Default-on for the individual; Config-Sync-enforced for the fleet.

## Known environment note (RDP / VDI)

`OpenInputDesktop` semantics differ on remote-desktop / virtual-desktop sessions
vs a physical console — a *disconnected* session reads as no input desktop
(correctly "away"), but *locked-while-connected* over RDP/Citrix should be
verified on the actual target before relying on the claim there. Citrix is out of
scope for the current deployment (no agent access; being decommissioned); RDP is
to be validated.

## Not built (deliberately)

- No per-trigger `ignore_lock` — see "Absolute" above.
- No event-driven lock listener / `windows_lock` source — pull-based gate suffices.
- No chat-presence integration — v1 governs autonomous trigger/schedule execution,
  not room presence.
