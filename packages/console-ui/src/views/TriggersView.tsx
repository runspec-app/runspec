import { useEffect, useState } from 'react'
import { Alert, Collapse, Tooltip } from 'antd'
import { ReloadOutlined, BellOutlined } from '@ant-design/icons'
import { bridge, type TriggerEvent, type TriggerListResult } from '../bridge'
import { useIsDark, textColor, textMuted } from '../ThemeContext'
import { EventLog } from '../components/EventLog'
import { TriggersEditor } from '../components/TriggersEditor'
import { HelpdeskPanel } from '../components/HelpdeskPanel'

/**
 * TriggersView — the Triggers tab (AI-on; hidden when AI is off).
 *
 * This tab is now the place to *edit* triggers and audit what matched. A
 * trigger's run activity and its Approve/Deny gate live in the Console tab (a
 * fire mirrors into the Console as a run/chat block, so all agent activity has
 * one home and a confirm is always reachable — see ConsoleView / OutputPanel).
 * The only live surface kept here is the helpdesk queue (HelpdeskPanel), which
 * covers queue-specific handling the run stream doesn't: claim, portal requests,
 * drafted replies, and the visitor conversation.
 */
export function TriggersView() {
  const isDark = useIsDark()
  const [config, setConfig] = useState<TriggerListResult | null>(null)
  // Event-audit feed: every event seen + its disposition (newest first).
  const [events, setEvents] = useState<TriggerEvent[]>([])

  const loadConfig = () => bridge.list_triggers().then(setConfig).catch(console.error)

  useEffect(() => {
    loadConfig()
    bridge.get_trigger_events().then(evs => setEvents([...evs].reverse())).catch(console.error)
  }, [])

  useEffect(() => {
    // Config reloads on a status change or an agent-driven edit (manage_triggers /
    // manage_rota) — the view stays mounted behind display:none, so it must refetch.
    const onStatus = () => loadConfig()
    const onEvent = (e: Event) => {
      const rec = (e as CustomEvent).detail as TriggerEvent
      setEvents(prev => [rec, ...prev].slice(0, 300))
    }
    window.addEventListener('runspec:trigger_status', onStatus)
    window.addEventListener('runspec:triggers_updated', onStatus)
    window.addEventListener('runspec:trigger_event', onEvent)
    return () => {
      window.removeEventListener('runspec:trigger_status', onStatus)
      window.removeEventListener('runspec:triggers_updated', onStatus)
      window.removeEventListener('runspec:trigger_event', onEvent)
    }
  }, [])

  const dim = textMuted(isDark)
  const sources = config?.sources ?? []
  const triggers = config?.triggers ?? []
  const errors = config?.errors ?? []

  // Only warn about an unavailable source that a configured trigger actually uses.
  const usedSources = new Set(triggers.map(t => String(t.source)))

  return (
    <div style={{ color: textColor(isDark) }}>
      {/* Source availability (only for sources a configured trigger uses) */}
      {sources.map(s => (
        !s.available && usedSources.has(s.id) ? (
          <Alert
            key={s.id}
            type="warning"
            showIcon
            closable
            style={{ marginBottom: 8 }}
            message={<span>Event source <code>{s.id}</code> is unavailable: {s.reason}</span>}
          />
        ) : null
      ))}
      {errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="Some triggers could not be loaded"
          description={<ul style={{ margin: 0, paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
        />
      )}

      {/* Configured triggers — collapsible editor (create/edit/delete + Trigger now) */}
      <Collapse
        size="small"
        defaultActiveKey={['triggers']}
        style={{ marginBottom: 20 }}
        items={[{
          key: 'triggers',
          label: (
            <span>
              <BellOutlined /> Triggers
              <span style={{ color: dim, fontSize: 13 }}>
                {' · '}{triggers.length} configured{config?.running ? ' · engine running' : ''}
              </span>
            </span>
          ),
          extra: (
            <Tooltip title="Reload trigger config">
              <ReloadOutlined
                onClick={e => { e.stopPropagation(); loadConfig() }}
                style={{ color: dim }}
              />
            </Tooltip>
          ),
          children: <TriggersEditor triggers={triggers} sources={sources} reload={loadConfig} />,
        }]}
      />

      {/* Helpdesk queue (AI-on, self-hiding). Trigger run activity + Approve/Deny
          now live in the Console tab. */}
      <HelpdeskPanel />

      {/* Event audit feed — every event seen + its disposition (shared widget). */}
      <EventLog events={events} />
    </div>
  )
}
