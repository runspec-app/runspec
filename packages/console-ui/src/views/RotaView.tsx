import { useEffect, useState } from 'react'
import { Switch, Button, Collapse, Segmented, Input } from 'antd'
import { ClockCircleOutlined } from '@ant-design/icons'
import { bridge, type Rota, type RotaDay } from '../bridge'
import { useIsDark, textColor } from '../ThemeContext'

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
        enabled, source, shift_marker: marker, hold_during_meetings: hold, ...days,
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
  const [loadSeq, setLoadSeq] = useState(0)

  const loadRota = () =>
    bridge.list_triggers()
      .then(cfg => { setRota((cfg.rota ?? {}) as Rota); setLoadSeq(s => s + 1) })
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
            : <WorkingHoursEditor key={loadSeq} rota={rota} onSaved={loadRota} />,
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
