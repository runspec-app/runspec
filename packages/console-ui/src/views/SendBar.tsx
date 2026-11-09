/**
 * SendBar — the Send to terminals bar (Windows).
 *
 * Runs in its own small always-on-top window (the console page loaded at
 * `#send-bar`, see main.tsx). Lists the PuTTY windows the console opened, each
 * with a tick box, and types what the person enters into every ticked one —
 * one command run on many hosts at once, while each window stays an ordinary
 * PuTTY session (sudo -iu, prompts and all). Human-only: the bridge methods it
 * calls are not agent or MCP tools.
 *
 * Enter sends the line (then Enter in each terminal), Shift+Enter adds a line,
 * ↑/↓ recall earlier lines (kept in memory only, never saved). The lock button
 * switches to a hidden field for a password: typed the same way, never shown,
 * kept or audited. Sending to more than 10 windows asks first, once per set.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, App as AntApp, Button, Checkbox, ConfigProvider, Input, Modal, Tooltip, theme } from 'antd'
import { AimOutlined, LockOutlined, SendOutlined, UnlockOutlined } from '@ant-design/icons'
import { bridge, type TerminalWindow } from '../bridge'
import {
  DEFAULT_ACCENT, MONO_FONT, RADIUS, ThemeContext, UI_FONT, surfaces, textMuted, useIsDark,
} from '../ThemeContext'
import {
  KEY_BUTTONS, describeResult, historyStep, needsConfirm, pushHistory, signature, tickedIds,
} from './sendBar'

const POLL_MS = 1500

export function SendBar() {
  const isDark = useIsDark()
  const [supported, setSupported] = useState(true)
  const [windows, setWindows] = useState<TerminalWindow[]>([])
  const [unticked, setUnticked] = useState<Set<string>>(new Set())
  const [text, setText] = useState('')
  const [hidden, setHidden] = useState(false)
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<string[]>([])
  const [histIdx, setHistIdx] = useState(0)
  const confirmed = useRef<string | null>(null)
  const [modalApi, modalHolder] = Modal.useModal()
  const draft = useRef('')

  const refresh = useCallback(() => {
    bridge.terminal_windows()
      .then(res => { setSupported(res.supported); setWindows(res.windows) })
      .catch(() => { /* bridge not ready yet — next poll */ })
  }, [])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(t)
  }, [refresh])

  const ticked = useMemo(() => tickedIds(windows, unticked), [windows, unticked])
  const hostOf = useMemo(() => new Map(windows.map(w => [w.id, w.host])), [windows])

  const ask = (ids: string[]): Promise<boolean> => new Promise(resolve => {
    const hosts = ids.map(id => hostOf.get(id) ?? id)
    modalApi.confirm({
      title: `Send to ${ids.length} terminals?`,
      content: (
        <div style={{ fontSize: 12 }}>
          <div style={{ fontFamily: MONO_FONT }}>
            {hosts.slice(0, 12).join(', ')}{hosts.length > 12 ? ` and ${hosts.length - 12} more` : ''}
          </div>
          <div style={{ marginTop: 6, color: textMuted(isDark) }}>
            You won't be asked again until the ticked terminals change.
          </div>
        </div>
      ),
      okText: `Send to ${ids.length}`,
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })

  const send = async (opts: { key?: string; line?: string }) => {
    if (!ticked.length || busy) return
    if (needsConfirm(ticked, confirmed.current)) {
      if (!(await ask(ticked))) return
      confirmed.current = signature(ticked)
    }
    setBusy(true)
    try {
      if (opts.key) {
        const res = await bridge.send_to_terminals(ticked, '', false, opts.key)
        setStatus(describeResult(res, KEY_BUTTONS.find(k => k.key === opts.key)?.label ?? opts.key))
      } else {
        const line = opts.line ?? ''
        const res = await bridge.send_to_terminals(ticked, line, true, '', hidden)
        setStatus(describeResult(res, hidden ? 'Hidden text' : 'Sent'))
        if (!hidden) {
          const next = pushHistory(history, line)
          setHistory(next)
          setHistIdx(next.length)
        }
        setText('')
        draft.current = ''
        if (hidden) setHidden(false)
      }
    } catch (err) {
      setStatus((err as Error)?.message || 'could not send')
    } finally {
      setBusy(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement | HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send({ line: text })
      return
    }
    if (hidden || text.includes('\n')) return
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      if (!history.length) return
      e.preventDefault()
      if (histIdx === history.length) draft.current = text
      const next = historyStep(history.length, histIdx, e.key === 'ArrowUp' ? -1 : 1)
      setHistIdx(next)
      setText(next === history.length ? draft.current : history[next])
    }
  }

  const toggle = (id: string, on: boolean) => setUnticked(prev => {
    const next = new Set(prev)
    if (on) next.delete(id); else next.add(id)
    return next
  })

  if (!supported) {
    return (
      <div style={{ padding: 12 }}>
        <Alert type="info" showIcon message="Sending to terminals works on Windows only." />
      </div>
    )
  }

  const muted = textMuted(isDark)
  return (
    <div style={{ padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 6, height: '100vh', boxSizing: 'border-box' }}>
      {modalHolder}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: muted }}>
        <span>
          {windows.length
            ? `${ticked.length} of ${windows.length} terminal${windows.length === 1 ? '' : 's'} ticked`
            : 'No terminals open'}
        </span>
        <Button size="small" type="link" style={{ padding: 0 }} disabled={!windows.length}
          onClick={() => setUnticked(new Set())}>All</Button>
        <Button size="small" type="link" style={{ padding: 0 }} disabled={!windows.length}
          onClick={() => setUnticked(new Set(windows.map(w => w.id)))}>None</Button>
      </div>
      <div
        aria-label="Terminals"
        style={{ display: 'flex', flexWrap: 'wrap', gap: '2px 12px', maxHeight: 64, overflowY: 'auto', flexShrink: 0 }}
      >
        {windows.length === 0 && (
          <span style={{ fontSize: 12, color: muted }}>
            Open terminals from the console: right-click a host or group, then Open SSH terminals.
          </span>
        )}
        {windows.map(w => (
          <span key={w.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
            <Checkbox checked={!unticked.has(w.id)} onChange={e => toggle(w.id, e.target.checked)}>
              <span style={{ fontFamily: MONO_FONT, fontSize: 12, opacity: w.ready ? 1 : 0.5 }}>{w.host}</span>
            </Checkbox>
            <Tooltip title={w.ready ? 'Bring this window to the front' : 'Window not open yet'}>
              <Button size="small" type="text" icon={<AimOutlined />} disabled={!w.ready}
                aria-label={`Show ${w.host}`}
                onClick={() => { bridge.focus_terminal(w.id).catch(() => {}) }} />
            </Tooltip>
          </span>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
        <Tooltip title={hidden ? 'Back to normal text' : 'Hidden text, for a password: not shown, kept or logged'}>
          <Button
            aria-label={hidden ? 'Show text' : 'Hide text'}
            icon={hidden ? <LockOutlined /> : <UnlockOutlined />}
            type={hidden ? 'primary' : 'default'}
            onClick={() => { setHidden(h => !h); setText('') }}
          />
        </Tooltip>
        {hidden ? (
          <Input.Password
            aria-label="Hidden text"
            autoFocus
            value={text}
            placeholder="Password for the ticked terminals (hidden, not kept)"
            onChange={e => setText(e.target.value)}
            onKeyDown={onKeyDown}
            visibilityToggle={false}
            autoComplete="off"
          />
        ) : (
          <Input.TextArea
            aria-label="Command"
            autoFocus
            value={text}
            placeholder="Command for the ticked terminals (Shift+Enter: new line, ↑: last command)"
            onChange={e => { setText(e.target.value); setHistIdx(history.length) }}
            onKeyDown={onKeyDown}
            autoSize={{ minRows: 1, maxRows: 4 }}
            spellCheck={false}
            style={{ fontFamily: MONO_FONT }}
          />
        )}
        <Button type="primary" icon={<SendOutlined />} loading={busy} disabled={!ticked.length}
          onClick={() => { void send({ line: text }) }}>
          Send
        </Button>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
        {KEY_BUTTONS.map(k => (
          <Tooltip key={k.key} title={`${k.title} in the ticked terminals`}>
            <Button size="small" disabled={!ticked.length || busy}
              onClick={() => { void send({ key: k.key }) }}>{k.label}</Button>
          </Tooltip>
        ))}
        <span role="status" style={{ fontSize: 12, color: muted, marginLeft: 6, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
          {status}
        </span>
      </div>
    </div>
  )
}

/** The bar's window root: theme from the console's saved light/dark choice. */
export function SendBarApp() {
  const isDark = (() => {
    try { return localStorage.getItem('theme') !== 'light' } catch { return true }
  })()
  const sf = surfaces(isDark)
  useEffect(() => {
    document.title = 'Send to terminals'
    document.body.style.background = sf.base
  }, [sf.base])
  return (
    <ThemeContext.Provider value={isDark}>
      <ConfigProvider theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          fontFamily: UI_FONT,
          fontFamilyCode: MONO_FONT,
          borderRadius: RADIUS.md,
          colorPrimary: isDark ? DEFAULT_ACCENT.dark : DEFAULT_ACCENT.light,
          colorBgBase: sf.base,
          colorBgLayout: sf.base,
          colorBgContainer: sf.raised,
          colorBgElevated: sf.overlay,
        },
      }}>
        <AntApp>
          <div style={{ background: sf.base, minHeight: '100vh' }}>
            <SendBar />
          </div>
        </AntApp>
      </ConfigProvider>
    </ThemeContext.Provider>
  )
}
