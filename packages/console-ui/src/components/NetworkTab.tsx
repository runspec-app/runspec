// Settings → Network: the console-wide [http] proxy that fetch_url, the
// Marketplace, MCP servers over http/sse, the model adapters and telemetry use.
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Divider, Input, Segmented, Select, Space, Typography, message } from 'antd'
import { ApiOutlined } from '@ant-design/icons'
import {
  bridge,
  type HttpProxyConfig,
  type HttpProxyInput,
  type HttpProxyMode,
  type HttpProxyTestResult,
  type McpCredentialOption,
} from '../bridge'
import { MONO_FONT } from '../ThemeContext'
import { SettingsColumns } from './SettingsColumns'

const { Text } = Typography

// Split the no_proxy textbox (commas, spaces or newlines) into entries.
export function parseNoProxy(text: string): string[] {
  return text.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
}

export function NetworkTab() {
  const [mode, setMode] = useState<HttpProxyMode>('system')
  const [proxy, setProxy] = useState('')
  const [noProxy, setNoProxy] = useState('')
  const [credential, setCredential] = useState('')
  const [effective, setEffective] = useState('')
  const [credOptions, setCredOptions] = useState<McpCredentialOption[]>([])
  const [testUrl, setTestUrl] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<HttpProxyTestResult | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const apply = useCallback((cfg: HttpProxyConfig) => {
    setMode(cfg.mode)
    setProxy(cfg.proxy ?? '')
    setNoProxy((cfg.no_proxy ?? []).join(', '))
    setCredential(cfg.credential ?? '')
    setEffective(cfg.effective ?? '')
  }, [])

  useEffect(() => {
    bridge.get_http_proxy_config().then(apply).catch(() => {})
  }, [apply])

  // Refresh the credential picker when a credential is added or removed in the
  // Credentials tab (Settings tabs stay mounted, so a mount-only load goes stale).
  useEffect(() => {
    const loadCreds = () => { bridge.mcp_credential_options().then(setCredOptions).catch(() => setCredOptions([])) }
    loadCreds()
    window.addEventListener('runspec:credentials-changed', loadCreds)
    return () => window.removeEventListener('runspec:credentials-changed', loadCreds)
  }, [])

  const payload = (): HttpProxyInput => ({ mode, proxy: proxy.trim(), no_proxy: parseNoProxy(noProxy), credential })

  const handleSave = async () => {
    setSaving(true)
    try {
      apply(await bridge.save_http_proxy_config(payload()))
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Could not save the proxy settings')
    } finally { setSaving(false) }
  }

  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      setTestResult(await bridge.test_http_proxy(payload(), testUrl.trim() || undefined))
    } catch (e) {
      setTestResult({ ok: false, url: testUrl, route: '', error: e instanceof Error ? e.message : String(e) })
    } finally { setTesting(false) }
  }

  return (
    <div>
      <SettingsColumns>
        <div>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
            The HTTP proxy the console uses for its own web requests: the agent's <code>fetch_url</code>, Marketplace
            index reads, MCP servers over http/sse, the model API and telemetry export. SSH connections use the proxy on
            the SSH tab instead.
          </Typography.Paragraph>

          <Text strong style={{ fontSize: 14 }}>Proxy</Text>
          <div style={{ marginTop: 8 }}>
            <Segmented<HttpProxyMode>
              aria-label="Proxy mode"
              value={mode}
              onChange={setMode}
              options={[
                { label: 'System', value: 'system' },
                { label: 'Manual', value: 'manual' },
                { label: 'No proxy', value: 'direct' },
              ]}
            />
          </div>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 6 }}>
            {mode === 'system' && <>Use the <code>HTTPS_PROXY</code> / <code>HTTP_PROXY</code> / <code>NO_PROXY</code> environment variables, if set.</>}
            {mode === 'manual' && <>Send every request through this proxy. Hosts under "Bypass" and this machine (localhost) go direct.</>}
            {mode === 'direct' && <>Never use a proxy, even if the environment variables set one.</>}
          </Typography.Paragraph>

          {mode === 'manual' && (
            <Space direction="vertical" style={{ width: '100%' }} size={10}>
              <div>
                <Text style={{ fontSize: 13 }}>Proxy URL</Text>
                <Input
                  aria-label="Proxy URL"
                  value={proxy}
                  onChange={e => setProxy(e.target.value)}
                  placeholder="http://proxy.corp:8080"
                  style={{ fontFamily: MONO_FONT, marginTop: 4 }}
                />
              </div>
              <div>
                <Text style={{ fontSize: 13 }}>Bypass</Text>
                <Input.TextArea
                  aria-label="Bypass"
                  value={noProxy}
                  onChange={e => setNoProxy(e.target.value)}
                  placeholder="corp.local, gitlab.internal"
                  autoSize={{ minRows: 1, maxRows: 4 }}
                  style={{ fontFamily: MONO_FONT, marginTop: 4 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>Hosts or domains reached directly, separated by commas. <code>corp.local</code> also covers its subdomains.</Text>
              </div>
              <div>
                <Text style={{ fontSize: 13 }}>Proxy login</Text>
                <Select
                  aria-label="Proxy login"
                  value={credential || undefined}
                  onChange={v => setCredential(v ?? '')}
                  allowClear
                  placeholder="None"
                  style={{ width: '100%', marginTop: 4 }}
                  options={credOptions.map(c => ({ value: c.id, label: c.label + (c.kind ? ` (${c.kind})` : '') }))}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>A username + password credential from the Credentials tab, sent as Basic proxy auth.</Text>
              </div>
            </Space>
          )}
        </div>
        <div>
          <Text strong style={{ fontSize: 14 }}>Test</Text>
          <Space.Compact style={{ width: '100%', marginTop: 8 }}>
            <Input
              aria-label="Test URL"
              value={testUrl}
              onChange={e => setTestUrl(e.target.value)}
              placeholder="https://pypi.org/simple/"
              style={{ fontFamily: MONO_FONT }}
            />
            <Button icon={<ApiOutlined />} loading={testing} onClick={handleTest}>Test</Button>
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 12 }}>Fetches the URL with these settings, before you save them.</Text>
          {testResult && (
            <Alert
              style={{ marginTop: 8 }}
              type={testResult.ok ? 'success' : 'error'}
              showIcon
              message={testResult.ok ? `Reached ${testResult.url} (HTTP ${testResult.status})` : `Could not reach ${testResult.url}`}
              description={<>{testResult.route && <div>Route: {testResult.route}</div>}{testResult.error && <div>{testResult.error}</div>}</>}
            />
          )}
        </div>
      </SettingsColumns>

      <Divider style={{ margin: '16px 0' }} />
      {effective && <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>In use now: <span style={{ fontFamily: MONO_FONT }}>{effective}</span></Typography.Paragraph>}
      <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
    </div>
  )
}
