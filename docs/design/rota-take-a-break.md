# Rota "Take a break" (timed auto-resume + Outlook reminder, lock-aware)

## Why

The working-hours **rota** gates whether automated triggers/schedules fire. Its
only control was a manual enable/disable switch — so an operator stepping away
had to remember to flip it off *and back on*, and a forgotten re-enable silently
skips triggers/schedules. "Take a break" makes stepping away a single click that
**can't be forgotten**: automation resumes by itself.

## Behaviour

- A **"Take a break"** control on the Rota tab: minutes (presets 15/30/60 +
  custom, default 60; today's shift end shown — you may break past it). Disabled
  when the rota is off (nothing to pause).
- Clicking suspends triggers/schedules and **auto-resumes** at the return time.
- An **Outlook reminder** (a calendar appointment with a reminder at the return
  time) is set best-effort, as a nudge to come back.
- **Honours the lock screen:** if the screen is still locked when the break
  elapses, automation stays paused until unlock (the operator is plainly still
  away). If it can't tell (non-Windows / no Outlook), it just auto-resumes.
- While on a break the tab shows "On break until HH:MM" + **End break now**.

## Design (why it's simple)

The rota gate `Bridge._build_clock_ok` evaluates `now` **live** on every event.
So the break is held **in memory** (`Bridge._break_until`) and the gate honours
it live:

```
if break_until:
    if now < break_until:   suspend          # in the window
    if is_screen_locked():  suspend          # window over but still away → hold
    break_until = None                        # back at the desk → resume (self-clears)
```

This means **no timer, no persisted state, no engine restart**, and crucially no
stale-state bug — because the gate self-clears once resumed, a *later* normal
end-of-day lock never re-suspends. A break is transient by nature (lost on app
restart, which is fine).

`is_screen_locked()` (`winlock.py`) is a minimal synchronous probe —
`OpenInputDesktop` returns NULL on the secure/locked desktop — best-effort and
`False` on any error or non-Windows. `create_reminder` (`sources/outlook.py`) is
the file's first *create* calendar item (`CreateItem(1)` appointment with
`ReminderSet`), mirroring the existing draft/send helpers; reading reminders and
draft/send already existed.

## Files
- `runspec_console/winlock.py` (new) — `is_screen_locked()`
- `runspec_console/sources/outlook.py` — `create_reminder` / `_build_reminder_item`
- `runspec_console/bridge.py` — `_break_until`, `start_break`/`cancel_break`,
  the `_build_clock_ok` break clause, `list_triggers` `break_until`, the
  `_console_context` "on break" line
- `console-ui/src/views/RotaView.tsx` — the break control
- `console-ui/src/bridge/index.ts` + `mock.ts` — `start_break`/`cancel_break`, `break_until`

## Follow-up — full lock auto-suspend

An opt-in "Pause automation while my screen is locked" Rota toggle backed by an
**event-driven** lock listener (a `windows_lock` EventSource / background poller
reusing `winlock.is_screen_locked`), a transient `lock_ok` gate in
`TriggerEngine` with a per-trigger `ignore_lock` escape hatch, and presence
integration — with RDP/multi-session caveats. The break's minimal synchronous
check here is the foundation it reuses.
