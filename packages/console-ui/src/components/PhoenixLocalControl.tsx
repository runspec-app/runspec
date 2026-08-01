import { useCallback, useEffect, useState } from 'react'
import { Button, Tag, Typography, Space } from 'antd'
import { PlayCircleOutlined, PoweroffOutlined, ExportOutlined } from '@ant-design/icons'
import { bridge, type PhoenixStatus } from '../bridge'
import { useIsDark, textSecondary } from '../ThemeContext'

// Settings → Telemetry: a compact control for the console-managed local Arize
// Phoenix server. Starting it launches a private, on-machine OTLP collector +
// trace UI and routes the console's own agent traces to it (auto-wire); its own
// UI opens in the browser (runspec-console does NOT embed it). This is the
// zero-config local alternative to configuring a remote OTLP backend below.

const POLL_MS = 5000

export function PhoenixLocalControl() {
  const isDark = useIsDark()
  const [status, setStatus] = useState<PhoenixStatus | null>(null)
  const [busy, setBusy] = useState<'starting' | 'stopping' | null>(null)

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
    const id = window.setInterval(refresh, POLL_MS)
    return () => {
      window.removeEventListener('runspec:phoenix_changed', onChanged)
      window.clearInterval(id)
    }
  }, [refresh])

  const running = !!status?.running
  const url = status?.url || ''

  const doStart = useCallback(() => {
    setBusy('starting')
    bridge.phoenix_start().then(setStatus).catch(() => {}).finally(() => setBusy(null))
  }, [])

  const doStop = useCallback(() => {
    setBusy('stopping')
    bridge.phoenix_stop().then(setStatus).catch(() => {}).finally(() => setBusy(null))
  }, [])

  const statusTag = () => {
    if (!status) return <Tag>…</Tag>
    if (!status.available) return <Tag color="default">not installed</Tag>
    if (busy === 'starting') return <Tag color="processing">starting…</Tag>
    if (busy === 'stopping') return <Tag color="processing">stopping…</Tag>
    if (running) return <Tag color="success">running</Tag>
    return <Tag color="default">stopped</Tag>
  }

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        <strong>Local Phoenix</strong> — run a private, on-machine{' '}
        <a href="https://phoenix.arize.com/" target="_blank" rel="noreferrer">Arize Phoenix</a>{' '}
        server (an OTLP collector + trace-analysis UI) and route this console's own agent
        traces to it — fully local, no external backend or API keys. Its UI opens in your
        browser. Requires the <code>phoenix</code> extra:{' '}
        <code>pip install runspec-console[phoenix]</code>.
      </Typography.Paragraph>

      <Space size={8} wrap>
        {statusTag()}
        {status?.available && !running && (
          <Button size="small" type="primary" icon={<PlayCircleOutlined />} loading={busy === 'starting'} onClick={doStart}>
            Start
          </Button>
        )}
        {running && (
          <>
            <Button size="small" icon={<ExportOutlined />} onClick={() => bridge.phoenix_open().catch(() => {})}>
              Open in browser
            </Button>
            <Button size="small" danger icon={<PoweroffOutlined />} loading={busy === 'stopping'} onClick={doStop}>
              Stop
            </Button>
          </>
        )}
      </Space>

      {running && url && (
        <Typography.Paragraph style={{ fontSize: 12, margin: '8px 0 0', color: textSecondary(isDark) }}>
          Serving at <Typography.Text copyable style={{ fontSize: 12 }}>{url}</Typography.Text>. Traces from
          new agent turns are routing here; stopping restores your saved trace route below.
        </Typography.Paragraph>
      )}
      {status && !status.available && (
        <Typography.Paragraph style={{ fontSize: 12, margin: '4px 0 0', color: textSecondary(isDark) }}>
          {status.reason}
        </Typography.Paragraph>
      )}
    </div>
  )
}
