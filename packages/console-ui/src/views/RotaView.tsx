import { useEffect, useState, type ReactNode } from 'react'
import { Switch, Button, Collapse, Segmented, Input, InputNumber, message } from 'antd'
import { ClockCircleOutlined, CoffeeOutlined, LockOutlined, UnlockOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import { bridge, type Rota, type RotaDay, type LockState } from '../bridge'
import { useIsDark, textColor } from '../ThemeContext'

// JS Date.getDay() (0=Sun) → rota day key.
const DOW_KEY = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// Live screen-lock indicator for the lock auto-suspend control — polls the cheap
// screen_state probe while mounted so the operator can see the gate working.
function LockIndicator() {
  const [state, setState] = useState<LockState | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () => bridge.screen_state().then(s => { if (alive) setState(s) }).catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])
  if (state === null) return null
  const meta: Record<LockState, { icon: ReactNode; label: string; color: string }> = {
    unlocked: { icon: <UnlockOutlined />, label: 'screen unlocked — present', color: '#3fb950' },
    locked: { icon: <LockOutlined />, label: 'screen locked — triggers paused', color: '#e8b339' },
    unknown: { icon: <QuestionCircleOutlined />, label: 'lock state unknown — paused (fail-safe)', color: '#e8b339' },
  }
  const m = meta[state]
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: m.color, fontSize: 12 }}>
      {m.icon}{m.label}
    </span>
  )
}

// "Take a break": suspend the rota for N minutes (auto-resumes; held while the
// screen is locked) + a best-effort Outlook reminder at the return time.
function BreakControl({ rota, breakUntil, onChanged }: { rota: Rota; breakUntil: string | null; onChanged: () => void }) {
  const isDark = useIsDark()
  const dim = isDark ? '#888' : '#999'
  const [minutes, setMinutes] = useState<number>(60)
  const [busy, setBusy] = useState(false)

  const onBreak = !!breakUntil && new Date(breakUntil).getTime() > Date.now()

  const today = rota[DOW_KEY[new Date().getDay()]]
  const shiftEnd = today && typeof today === 'object' && (today as RotaDay).on ? (today as RotaDay).end : undefined

  const take = async () => {
    setBusy(true)
    try {
      const res = await bridge.start_break(minutes)
      message.success(
        res.reminder_set
          ? `On break — automation resumes ~${res.break_until ? fmtTime(res.break_until) : `${minutes}m`}; Outlook reminder set.`
          : `On break — automation resumes in ${minutes}m.`,
      )
      onChanged()
    } catch (e) {
      message.error((e as Error)?.message || 'could not start break')
    } finally {
      setBusy(false)
    }
  }

  const end = async () => {
    setBusy(true)
    try {
      await bridge.cancel_break()
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  if (onBreak) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
        <CoffeeOutlined style={{ color: '#e8b339' }} />
        <span>On break until <b>{fmtTime(breakUntil!)}</b> — automation resumes then (held while your screen is locked).</span>
        <Button size="small" onClick={end} loading={busy}>End break now</Button>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14, flexWrap: 'wrap' }}>
      <CoffeeOutlined style={{ color: dim }} />
      <span>Take a break for</span>
      <Segmented
        size="small"
        value={[15, 30, 60].includes(minutes) ? minutes : 'custom'}
        onChange={v => { if (v !== 'custom') setMinutes(Number(v)) }}
        options={[{ label: '15m', value: 15 }, { label: '30m', value: 30 }, { label: '60m', value: 60 }, { label: 'Custom', value: 'custom' }]}
      />
      <InputNumber size="small" min={1} max={1440} value={minutes} onChange={v => setMinutes(Number(v) || 1)} style={{ width: 64 }} />
      <span style={{ color: dim, fontSize: 12 }}>min</span>
      <Button size="small" type="primary" icon={<CoffeeOutlined />} disabled={!rota.enabled} onClick={take} loading={busy}>
        Take a break
      </Button>
      {!rota.enabled
        ? <span style={{ color: dim, fontSize: 12 }}>(enable the rota to take a break)</span>
        : shiftEnd
          ? <span style={{ color: dim, fontSize: 12 }}>your shift ends {shiftEnd} — you can break past it</span>
          : null}
    </div>
  )
}

const DAYS: [string, string][] = [
  ['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'],
  ['fri', 'Friday'], ['sat', 'Saturday'], ['sun', 'Sunday'],
]

// Working-hours rota editor: a master switch + a row per weekday (off / start /
// end). Triggers and schedules only run inside these windows, so a human is
// around for confirms. Off by default — when disabled, automation runs anytime.
function WorkingHoursEditor({ rota, onSaved }: { rota: Rota; onSaved: () => void }) {
  const isDark = useIsDark()
  const dim = isDark ? '#888' : '#999'

  const dayOf = (k: string): RotaDay => {
    const d = rota[k]
    return (d && typeof d === 'object' ? d : {}) as RotaDay
  }
  const [enabled, setEnabled] = useState<boolean>(!!rota.enabled)
  const [source, setSource] = useState<string>(rota.source === 'outlook' ? 'outlook' : 'console')
  const [marker, setMarker] = useState<string>(rota.shift_marker || 'shift')
  const [hold, setHold] = useState<boolean>(!!rota.hold_during_meetings)
  // Lock auto-suspend defaults ON (absent ⇒ true); independent of working hours.
  const [lockSuspend, setLockSuspend] = useState<boolean>(rota.lock_suspend !== false)
  const [days, setDays] = useState<Record<string, RotaDay>>(() =>
    Object.fromEntries(DAYS.map(([k]) => {
      const d = dayOf(k)
      return [k, { on: d.on ?? false, start: d.start ?? '09:00', end: d.end ?? '17:00' }]
    })),
  )
  const [saving, setSaving] = useState(false)

  const setDay = (k: string, patch: Partial<RotaDay>) =>
    setDays(prev => ({ ...prev, [k]: { ...prev[k], ...patch } }))

  const save = async () => {
    setSaving(true)
    try {
      await bridge.save_rota({
        enabled, source, shift_marker: marker, hold_during_meetings: hold,
        lock_suspend: lockSuspend, ...days,
      })
      onSaved()
    } finally {
      setSaving(false)
    }
  }

  const outlook = source === 'outlook'

  return (
    <div style={{ color: textColor(isDark) }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <Switch size="small" checked={enabled} onChange={setEnabled} />
        <span>Only run triggers and schedules during working hours</span>
        <span style={{ color: dim, fontSize: 12 }}>
          {enabled ? '(outside these, events are ignored)' : '(off — automation runs anytime)'}
        </span>
      </div>

      {/* Lock auto-suspend — a presence control, independent of working hours, so
          it sits outside the dimmed block above. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <Switch
          size="small"
          checked={lockSuspend}
          onChange={setLockSuspend}
          aria-label="Pause reactive triggers while my screen is locked"
        />
        <LockOutlined style={{ color: dim }} />
        <span>Pause reactive triggers while my screen is locked</span>
        <LockIndicator />
        <span style={{ color: dim, fontSize: 12 }}>
          {lockSuspend
            ? '(scheduled jobs still run; uncertain reads pause — fail-safe)'
            : '(off — triggers can run while you are away)'}
        </span>
      </div>

      <div style={{ opacity: enabled ? 1 : 0.5, pointerEvents: enabled ? 'auto' : 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
          <span style={{ width: 90 }}>Source</span>
          <Segmented
            size="small"
            value={source}
            onChange={v => setSource(String(v))}
            options={[
              { label: 'Console grid', value: 'console' },
              { label: 'Outlook calendar', value: 'outlook' },
            ]}
          />
        </div>

        {outlook && (
          <div style={{ marginBottom: 10, paddingLeft: 100 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span>On shift when a calendar event's subject contains</span>
              <Input
                size="small" value={marker} onChange={e => setMarker(e.target.value)}
                style={{ width: 120, fontFamily: 'monospace' }} placeholder="shift"
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Switch size="small" checked={hold} onChange={setHold} />
              <span>Hold while I'm in a meeting / OOO</span>
            </div>
            <div style={{ color: dim, fontSize: 12 }}>
              The grid below is the fallback if Outlook can't be read.
            </div>
          </div>
        )}

        {DAYS.map(([k, label]) => (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
            <span style={{ width: 90 }}>{label}</span>
            <Switch size="small" checked={!!days[k].on} onChange={v => setDay(k, { on: v })} />
            <input
              type="time" value={days[k].start} disabled={!days[k].on}
              onChange={e => setDay(k, { start: e.target.value })}
              style={{ fontFamily: 'monospace' }}
            />
            <span style={{ color: dim }}>to</span>
            <input
              type="time" value={days[k].end} disabled={!days[k].on}
              onChange={e => setDay(k, { end: e.target.value })}
              style={{ fontFamily: 'monospace' }}
            />
            {!days[k].on && <span style={{ color: dim, fontSize: 12 }}>not working</span>}
          </div>
        ))}
      </div>
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 10 }}>
        <Button type="primary" size="small" onClick={save} loading={saving}>
          Save working hours
        </Button>
        <span style={{ color: dim, fontSize: 12 }}>Changes take effect when you click Save.</span>
      </div>
    </div>
  )
}

// The Rota tab: the working-hours schedule that gates both triggers and
// schedules, so the console stays a user tool — automation only runs while
// someone's around to handle confirms. Promoted out of the Triggers tab so it
// reads as the single, shared gate it is.
export function RotaView() {
  const isDark = useIsDark()
  const dim = isDark ? '#888' : '#999'
  // null until the first load resolves. The panel is open by default, so the
  // editor would otherwise mount with an empty rota and lock its useState
  // initializers to the defaults before list_triggers() returns — showing
  // defaults even when a rota is saved on disk. Gate the editor on a real load,
  // and key it on the load count so a reload re-seeds from disk.
  const [rota, setRota] = useState<Rota | null>(null)
  const [breakUntil, setBreakUntil] = useState<string | null>(null)
  const [loadSeq, setLoadSeq] = useState(0)

  const loadRota = () =>
    bridge.list_triggers()
      .then(cfg => { setRota((cfg.rota ?? {}) as Rota); setBreakUntil(cfg.break_until ?? null); setLoadSeq(s => s + 1) })
      .catch(console.error)

  useEffect(() => { loadRota() }, [])

  return (
    <div style={{ color: textColor(isDark) }}>
      <Collapse
        size="small"
        defaultActiveKey={['rota']}
        style={{ marginBottom: 20 }}
        items={[{
          key: 'rota',
          label: (
            <span><ClockCircleOutlined /> Working hours{rota?.enabled ? '' : ' (off)'}</span>
          ),
          children: rota === null
            ? <span style={{ color: dim, fontSize: 12 }}>Loading…</span>
            : <>
                <WorkingHoursEditor key={loadSeq} rota={rota} onSaved={loadRota} />
                <BreakControl rota={rota} breakUntil={breakUntil} onChanged={loadRota} />
              </>,
        }]}
      />
      <div style={{ color: dim, fontSize: 12, paddingLeft: 4 }}>
        Triggers and schedules only run inside these hours. Outside them, events are
        ignored — keeping the console a user tool rather than an always-on automation
        system.
      </div>
    </div>
  )
}
