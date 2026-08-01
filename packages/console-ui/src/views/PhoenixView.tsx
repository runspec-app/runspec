import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Alert, Spin, Tag, Typography } from 'antd'
import {
  PlayCircleOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  ExportOutlined,
  NodeIndexOutlined,
} from '@ant-design/icons'
import { bridge, type PhoenixStatus } from '../bridge'
import { useIsDark, textColor, textSecondary, useAccent } from '../ThemeContext'

// The Phoenix tab: launch / stop a private, on-machine Arize Phoenix server
// (OTLP collector + trace-analysis web UI) and embed its own UI in an iframe.
// Starting it also live-routes the console's agent traces to it, so turns show
// up locally with no external backend. If the iframe can't frame the local
// server (framing blocked), the control bar's "Open in browser" is the fallback.

const POLL_MS = 4000

export function PhoenixView({ active }: { active: boolean }) {
  const isDark = useIsDark()
  const accent = useAccent()
  const [status, setStatus] = useState<PhoenixStatus | null>(null)
  const [busy, setBusy] = useState<'starting' | 'stopping' | null>(null)
  const [iframeFailed, setIframeFailed] = useState(false)
  // False until the iframe fires `load` — drives a "Loading Phoenix…" overlay so
  // the brief startup gap (and Phoenix's own first render) reads as a spinner
  // rather than the webview's blank connection-refused page.
  const [frameLoaded, setFrameLoaded] = useState(false)
  // Bump to force the iframe to remount (Reload button / on (re)start).
  const [frameKey, setFrameKey] = useState(0)
  const failTimer = useRef<number | null>(null)

  const refresh = useCallback(() => {
    bridge.phoenix_status().then(setStatus).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const onChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail as PhoenixStatus | undefined
      if (detail) setStatus(detail)
      else refresh()
    }
    window.addEventListener('runspec:phoenix_changed', onChanged)
    return () => window.removeEventListener('runspec:phoenix_changed', onChanged)
  }, [refresh])

  // Light poll while the tab is visible so a crash (or an externally-stopped
  // server) is reflected without a manual refresh.
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(id)
  }, [active, refresh])

  const running = !!status?.running
  const url = status?.url || ''

  const doStart = useCallback(() => {
    setBusy('starting')
    setIframeFailed(false)
    bridge
      .phoenix_start()
      .then(s => {
        setStatus(s)
        setFrameKey(k => k + 1)
      })
      .catch(() => {})
      .finally(() => setBusy(null))
  }, [])

  const doStop = useCallback(() => {
    setBusy('stopping')
    bridge
      .phoenix_stop()
      .then(setStatus)
      .catch(() => {})
      .finally(() => setBusy(null))
  }, [])

  // If the iframe hasn't fired `load` within a grace period after we point it at
  // a running server, assume framing is blocked and offer the browser fallback.
  useEffect(() => {
    if (failTimer.current) window.clearTimeout(failTimer.current)
    if (running && url && !iframeFailed) {
      failTimer.current = window.setTimeout(() => setIframeFailed(true), 8000)
    }
    return () => {
      if (failTimer.current) window.clearTimeout(failTimer.current)
    }
  }, [running, url, frameKey, iframeFailed])

  // A fresh iframe (first mount / Start / Reload) hasn't loaded yet — show the
  // loading overlay until its `load` fires.
  useEffect(() => { setFrameLoaded(false) }, [frameKey])

  const statusTag = () => {
    if (!status) return <Tag>…</Tag>
    if (!status.available) return <Tag color="default">not installed</Tag>
    if (busy === 'starting') return <Tag color="processing">starting…</Tag>
    if (busy === 'stopping') return <Tag color="processing">stopping…</Tag>
    if (running) return <Tag color="success">running</Tag>
    return <Tag color="default">stopped</Tag>
  }

  const border = isDark ? '#303030' : '#e5e5e5'
  const barBg = isDark ? '#161616' : '#fafafa'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      {/* Control bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 14px',
          borderBottom: `1px solid ${border}`,
          background: barBg,
          flexShrink: 0,
        }}
      >
        <NodeIndexOutlined style={{ color: accent }} />
        <span style={{ fontWeight: 600, color: textColor(isDark) }}>Phoenix</span>
        {statusTag()}
        {running && url && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }} copyable={{ text: url }}>
            {url}
          </Typography.Text>
        )}
        <div style={{ flex: 1 }} />
        {status?.available && !running && (
          <Button
            type="primary"
            size="small"
            icon={<PlayCircleOutlined />}
            loading={busy === 'starting'}
            onClick={doStart}
          >
            Start
          </Button>
        )}
        {running && (
          <>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                setIframeFailed(false)
                setFrameKey(k => k + 1)
              }}
            >
              Reload
            </Button>
            <Button size="small" icon={<ExportOutlined />} onClick={() => bridge.phoenix_open().catch(() => {})}>
              Open in browser
            </Button>
            <Button
              danger
              size="small"
              icon={<PoweroffOutlined />}
              loading={busy === 'stopping'}
              onClick={doStop}
            >
              Stop
            </Button>
          </>
        )}
      </div>

      {/* Body */}
      <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        {status && !status.available && (
          <div style={{ padding: 24, maxWidth: 720 }}>
            <Alert
              type="info"
              showIcon
              message="Phoenix isn't installed"
              description={
                <div style={{ color: textSecondary(isDark) }}>
                  <p>
                    The Phoenix tab runs a private, on-machine trace-analysis server (Arize
                    Phoenix) and routes this console's own agent traces to it — nothing leaves
                    the machine. Install the optional extra to enable it:
                  </p>
                  <pre style={{ background: isDark ? '#0d0d0d' : '#f5f5f5', padding: 10, borderRadius: 4 }}>
                    pip install runspec-console[phoenix]
                  </pre>
                  <p style={{ marginBottom: 0 }}>{status.reason}</p>
                </div>
              }
            />
          </div>
        )}

        {status?.available && !running && busy !== 'starting' && (
          <div style={{ padding: 24, maxWidth: 720, color: textSecondary(isDark) }}>
            <p>
              Start a local Phoenix server to view your agent's traces
              (<code>turn → LLM call → tool call</code>) in a private, on-machine explorer.
              Starting it routes this console's traces to it automatically — no external
              backend, no API keys.
            </p>
          </div>
        )}

        {busy === 'starting' && (
          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%', gap: 12 }}>
            <Spin />
            <span style={{ color: textSecondary(isDark) }}>Starting Phoenix…</span>
          </div>
        )}

        {running && url && !iframeFailed && (
          <iframe
            key={frameKey}
            title="Phoenix"
            src={url}
            onLoad={() => {
              if (failTimer.current) window.clearTimeout(failTimer.current)
              setFrameLoaded(true)
            }}
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0, background: '#fff' }}
          />
        )}

        {running && url && !iframeFailed && !frameLoaded && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              justifyContent: 'center',
              alignItems: 'center',
              gap: 12,
              background: isDark ? '#141414' : '#fff',
            }}
          >
            <Spin />
            <span style={{ color: textSecondary(isDark) }}>Loading Phoenix…</span>
          </div>
        )}

        {running && url && iframeFailed && (
          <div style={{ padding: 24, maxWidth: 720 }}>
            <Alert
              type="warning"
              showIcon
              message="Couldn't embed the Phoenix UI"
              description={
                <div style={{ color: textSecondary(isDark) }}>
                  <p>
                    The Phoenix server is running at <code>{url}</code>, but it couldn't be
                    embedded here (it may block being shown in a frame). Open it in your
                    browser instead:
                  </p>
                  <Button type="primary" icon={<ExportOutlined />} onClick={() => bridge.phoenix_open().catch(() => {})}>
                    Open Phoenix in browser
                  </Button>
                </div>
              }
            />
          </div>
        )}
      </div>
    </div>
  )
}
