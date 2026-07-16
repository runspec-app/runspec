import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Drawer, Modal, Form, Input, InputNumber, Button, Divider, Typography, Space, Tabs, Popconfirm, message, Tag, Tooltip, Select, Switch, Collapse, Alert, Segmented, AutoComplete, Empty, Table } from 'antd'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined, UploadOutlined, DownloadOutlined, UpOutlined, DownOutlined, ApiOutlined, LoadingOutlined, KeyOutlined, DesktopOutlined, MailOutlined, ThunderboltOutlined, TeamOutlined, StopOutlined, ReloadOutlined, WarningOutlined, SearchOutlined } from '@ant-design/icons'
import { bridge, type JumpHost, type HostGroup, type HostProfile, type Repo, type TestResult, type RotateHostResult, type Filter, type Credential, type Mailbox, type Watch, type TriggerListResult, type Team, type TeamsEvent, type SyncConfig, type SyncPullResult, type SeedStatus, type SeedApplyResult, type Runbook, type Host, type DenylistPayload, type DenylistHostStatus, type SelfServiceEntry, type Runnable } from '../bridge'
import { CopyKeyModal } from '../views/CopyKeyModal'
import { CopyKeyAllModal } from '../views/CopyKeyAllModal'
import { PathInput } from '../views/PathInput'
import { credEnvNames } from '../views/FormsView'

const { Text, Link } = Typography

interface SettingsDrawerProps {
  open: boolean
  onClose: () => void
  connectedHosts?: string[]
  allRemoteHosts?: string[]
  onHostsChanged?: () => void
  onKeyChanged?: () => void
}

const PROVIDER_MODELS: Record<string, string> = {
  anthropic: 'claude-sonnet-4-6',
  openai: 'gpt-4o',
  bedrock: 'anthropic.claude-sonnet-4-6',
  langserve: '',  // the published LangServe chain usually pins its own model
}

const BUILTIN_PROVIDERS = new Set(['anthropic', 'openai', 'bedrock', 'langserve'])

const BUILTIN_PROVIDER_OPTIONS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'bedrock', label: 'AWS Bedrock' },
  { value: 'langserve', label: 'LangServe gateway' },
]

// String keys the LLM form owns: written when non-empty, removed when blank, so a
// hand-edited value is only cleared by explicitly emptying its field. Non-string
// keys (api_key_ttl_ms number, tools_in_config bool) are handled separately in
// handleSave. Anything not listed here still survives a Save untouched.
const MANAGED_LLM_STR_KEYS = [
  'provider', 'api_key', 'model', 'base_url', 'aws_region', 'system', 'tool_mode',
  'api_key_command', 'auth_header', 'auth_scheme', 'input_messages_key',
  'input_tools_key', 'aws_access_key', 'aws_secret_key', 'aws_session_token',
] as const

const TOOL_MODE_OPTIONS = [
  { value: 'dedup', label: 'Dedup — one tool per runnable, host as an argument (default)' },
  { value: 'per_host', label: 'Per-host — one tool per host × runnable' },
  { value: 'search', label: 'Search — discovery meta-tools (best for large, multi-package fleets)' },
]

// ── Sounds & Notifications tab ───────────────────────────────────────────────

// Category → display label + default synthesized token (mirrors
// config.DEFAULT_SOUND_MAP in the backend).
const SOUND_CATEGORIES: { key: string; label: string; def: string }[] = [
  { key: 'confirm', label: 'Confirmation waiting (urgent)', def: 'synth:confirm' },
  { key: 'trigger', label: 'Trigger fired', def: 'synth:trigger' },
  { key: 'completion_success', label: 'Run succeeded', def: 'synth:success' },
  { key: 'completion_error', label: 'Run failed', def: 'synth:error' },
  { key: 'completion_stopped', label: 'Run stopped', def: 'synth:stopped' },
  { key: 'reply', label: 'Agent reply ready (unfocused)', def: 'synth:reply' },
  { key: 'message', label: 'Chat / helpdesk message', def: 'synth:message' },
]
// Valid winsound SND_ALIAS names offered as alternatives to the synth set.
const SYSTEM_SOUND_ALIASES = ['SystemAsterisk', 'SystemExclamation', 'SystemHand', 'SystemQuestion', 'SystemDefault']
const soundOptions = (def: string) => [
  { value: def, label: 'Default (synthesized)' },
  ...SYSTEM_SOUND_ALIASES.map(a => ({ value: `system:${a}`, label: `Windows: ${a}` })),
  { value: 'off', label: 'Off' },
]

export function SoundsTab() {
  const [enabled, setEnabled] = useState(true)
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [confirmNotif, setConfirmNotif] = useState(true)
  const [roomNotif, setRoomNotif] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<{ toast_available: boolean; toast_reason: string; sound_available: boolean; window_found: boolean } | null>(null)

  useEffect(() => {
    bridge.get_config().then(cfg => {
      const con = (cfg.console ?? {}) as Record<string, unknown>
      const sounds = (con.sounds ?? {}) as Record<string, unknown>
      setEnabled(sounds.enabled !== false)
      const ch: Record<string, string> = {}
      for (const c of SOUND_CATEGORIES) {
        const v = sounds[c.key]
        ch[c.key] = typeof v === 'string' && v ? v : c.def
      }
      setChoices(ch)
      setConfirmNotif(con.confirm_notifications !== false)
      setRoomNotif(con.room_notifications !== false)
    })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      const con: Record<string, unknown> = { ...((cfg.console as Record<string, unknown>) ?? {}) }
      con.sounds = { enabled, ...choices }
      con.confirm_notifications = confirmNotif
      con.room_notifications = roomNotif
      await bridge.save_config({ ...cfg, console: con })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const runTest = async () => {
    setTesting(true)
    try { setTest(await bridge.test_notification()) }
    finally { setTesting(false) }
  }

  const row = (label: string, control: ReactNode) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
      <span style={{ fontSize: 13 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        Audio cues + desktop alerts so you stay aware while the console is minimized or in the background.
        Sounds play from the app, so they work even when minimized. The desktop <em>toast</em> needs the
        optional notifications extra (<Typography.Text code>pip install runspec-console[notifications]</Typography.Text>),
        but the <em>taskbar flash</em> and <em>sounds</em> do not.
      </Typography.Paragraph>

      {row('Enable sounds', <Switch checked={enabled} onChange={setEnabled} />)}
      <Divider style={{ margin: '8px 0' }} />
      {SOUND_CATEGORIES.map(c => row(
        c.label,
        <Select
          size="small"
          disabled={!enabled}
          value={choices[c.key] ?? c.def}
          onChange={v => setChoices(prev => ({ ...prev, [c.key]: v }))}
          options={soundOptions(c.def)}
          style={{ width: 230 }}
        />,
      ))}

      <Divider style={{ margin: '12px 0 8px' }} />
      <Typography.Text strong style={{ fontSize: 13 }}>Desktop notifications</Typography.Text>
      <div style={{ marginTop: 8 }}>
        {row('Toast + flash when a confirmation is waiting', <Switch checked={confirmNotif} onChange={setConfirmNotif} />)}
        {row('Toast + flash on a chat / helpdesk message', <Switch checked={roomNotif} onChange={setRoomNotif} />)}
      </div>

      <Space style={{ marginTop: 8 }}>
        <Button type="primary" onClick={handleSave} loading={saving}>Save</Button>
        {saved && <Typography.Text type="success"><CheckOutlined /> Saved</Typography.Text>}
        <Button onClick={runTest} loading={testing}>Test notification &amp; sound</Button>
      </Space>

      {test && (
        <pre style={{ marginTop: 12, fontSize: 12, background: 'rgba(127,127,127,0.12)', padding: 10, borderRadius: 6, whiteSpace: 'pre-wrap' }}>
{`Sound:  ${test.sound_available ? 'available' : 'unavailable (not Windows?)'}
Toast:  ${test.toast_available ? 'available' : `unavailable — ${test.toast_reason}`}
Flash:  ${test.window_found ? 'window found' : 'window NOT found — title mismatch'}`}
        </pre>
      )}
    </div>
  )
}

// ── LLM tab ───────────────────────────────────────────────────────────────────

export function LlmTab() {
  const [form] = Form.useForm()
  const [provider, setProvider] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [configDir, setConfigDir] = useState<string>('')
  const [providerOptions, setProviderOptions] = useState(BUILTIN_PROVIDER_OPTIONS)
  // The operator-defined list of selectable models — the source for the strict
  // Model dropdown, edited via the "Available models" tag field.
  const [models, setModels] = useState<string[]>([])

  useEffect(() => { bridge.config_dir().then(setConfigDir) }, [])
  // Built-ins plus any discovered plugins (entry points / [plugins] modules).
  useEffect(() => {
    bridge.list_providers?.()
      .then(p => { if (p?.length) setProviderOptions(p) })
      .catch(() => { /* keep built-in fallback */ })
  }, [])

  useEffect(() => {
    bridge.get_config().then(cfg => {
      const llm = (cfg.llm ?? {}) as Record<string, unknown>
      const p = (llm.provider as string) ?? ''
      setProvider(p)
      const modelVal = (llm.model as string) ?? ''
      let list = Array.isArray(llm.models)
        ? (llm.models as unknown[]).map(String).filter(Boolean)
        : []
      // Keep a hand-edited active model selectable even if it's not in the list.
      if (modelVal && !list.includes(modelVal)) list = [modelVal, ...list]
      // Seed an empty list with the provider default so the dropdown isn't empty.
      if (list.length === 0 && PROVIDER_MODELS[p]) list = [PROVIDER_MODELS[p]]
      setModels(list)
      form.setFieldsValue({
        provider: p,
        api_key: (llm.api_key as string) ?? '',
        model: modelVal,
        base_url: (llm.base_url as string) ?? '',
        aws_region: (llm.aws_region as string) ?? '',
        system: (llm.system as string) ?? '',
        tool_mode: (llm.tool_mode as string) ?? '',
        api_key_command: (llm.api_key_command as string) ?? '',
        api_key_ttl_ms: llm.api_key_ttl_ms ?? undefined,
        auth_header: (llm.auth_header as string) ?? '',
        auth_scheme: (llm.auth_scheme as string) ?? '',
        input_messages_key: (llm.input_messages_key as string) ?? '',
        input_tools_key: (llm.input_tools_key as string) ?? '',
        tools_in_config: !!llm.tools_in_config,
        aws_access_key: (llm.aws_access_key as string) ?? '',
        aws_secret_key: (llm.aws_secret_key as string) ?? '',
        aws_session_token: (llm.aws_session_token as string) ?? '',
      })
    })
  }, [form])

  const handleProviderChange = (val: string) => {
    setProvider(val)
    const def = PROVIDER_MODELS[val]
    if (def) {
      // Ensure the provider default is in the list so the strict dropdown can show it.
      setModels(prev => (prev.includes(def) ? prev : [...prev, def]))
      if (!form.getFieldValue('model')) form.setFieldValue('model', def)
    }
  }

  const handleSave = async () => {
    const v = form.getFieldsValue()
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      // Start from the existing [llm] section so any key the form doesn't manage
      // survives a Save; then set-or-delete only the keys we own.
      const llm: Record<string, unknown> = { ...((cfg.llm as Record<string, unknown>) ?? {}) }
      const next: Record<string, string> = {
        provider: v.provider ?? '',
        api_key: v.api_key ?? '',
        model: v.model ?? '',
        base_url: v.base_url ?? '',
        aws_region: v.aws_region ?? '',
        system: v.system?.trim() ?? '',
        tool_mode: v.tool_mode ?? '',
        api_key_command: v.api_key_command?.trim() ?? '',
        auth_header: v.auth_header?.trim() ?? '',
        auth_scheme: v.auth_scheme?.trim() ?? '',
        input_messages_key: v.input_messages_key?.trim() ?? '',
        input_tools_key: v.input_tools_key?.trim() ?? '',
        aws_access_key: v.aws_access_key ?? '',
        aws_secret_key: v.aws_secret_key ?? '',
        aws_session_token: v.aws_session_token ?? '',
      }
      for (const k of MANAGED_LLM_STR_KEYS) {
        if (next[k]) llm[k] = next[k]
        else delete llm[k]
      }
      // Numeric TTL for the rotating-token command (blank → use the key forever).
      if (v.api_key_ttl_ms !== undefined && v.api_key_ttl_ms !== null && v.api_key_ttl_ms !== '') {
        llm.api_key_ttl_ms = Number(v.api_key_ttl_ms)
      } else {
        delete llm.api_key_ttl_ms
      }
      // LangServe boolean — only persist when on (keeps the section tidy).
      if (v.tools_in_config) llm.tools_in_config = true
      else delete llm.tools_in_config
      // Operator-defined model list (strict Model dropdown source). Written when
      // non-empty, removed when blank — mirrors the string set-or-delete above.
      if (models.length) llm.models = models
      else delete llm.models
      await bridge.save_config({ ...cfg, llm })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Form form={form} layout="vertical" size="small" style={{ marginTop: 4 }}>
      <Form.Item name="provider" label="Provider">
        <Select placeholder="None — chat disabled" allowClear onChange={handleProviderChange}>
          {providerOptions.map(o => (
            <Select.Option key={o.value} value={o.value}>{o.label}</Select.Option>
          ))}
        </Select>
      </Form.Item>
      {!!provider && (
        <>
          {provider !== 'bedrock' && (
            <Form.Item
              name="api_key"
              label={provider === 'langserve' ? 'API token' : 'API key'}
              help={provider === 'langserve' ? 'Bearer token. For rotating corporate tokens, use the token command under Advanced.' : undefined}
            >
              <Input.Password placeholder={provider === 'anthropic' ? 'sk-ant-...' : provider === 'openai' ? 'sk-...' : 'token'} />
            </Form.Item>
          )}
          {provider !== 'langserve' && (
            <>
              <Form.Item
                label="Available models"
                help="The models this console offers. Add each model id you use — the Model dropdown below selects the active one from this list."
              >
                <Select
                  mode="tags"
                  value={models}
                  onChange={next => {
                    setModels(next)
                    // If the active model was removed from the list, reset it so the
                    // strict dropdown never shows a value that isn't an option.
                    const active = form.getFieldValue('model')
                    if (active && !next.includes(active)) form.setFieldValue('model', next[0] ?? '')
                  }}
                  notFoundContent={null}
                  tokenSeparators={[',', ' ']}
                  placeholder={PROVIDER_MODELS[provider] ? `e.g. ${PROVIDER_MODELS[provider]}` : 'Add model ids…'}
                  style={{ fontFamily: 'monospace' }}
                />
              </Form.Item>
              <Form.Item name="model" label="Model">
                <Select
                  showSearch
                  allowClear
                  placeholder={models.length ? 'Select a model' : 'Add a model above first'}
                  options={models.map(m => ({ value: m, label: m }))}
                  style={{ fontFamily: 'monospace' }}
                />
              </Form.Item>
            </>
          )}
          <Form.Item
            name="system"
            label="System prompt"
            help="Optional standing instructions applied to every chat turn — site policy, host context, tone. For rules you must enforce, use a runnable's autonomy level; this is guidance, not a guard."
          >
            <Input.TextArea
              autoSize={{ minRows: 3, maxRows: 10 }}
              placeholder={'You are a helpful assistant with access to runspec tools on local and remote hosts.\nUse tools when they help; briefly explain each call before running it.'}
            />
          </Form.Item>
          <Form.Item
            name="tool_mode"
            label="Tool list mode"
            help="How fleet runnables are exposed to the agent each turn. Dedup (default) collapses identical per-host copies; Search lists only discovery meta-tools — far fewer tokens on large, multi-package fleets."
          >
            <Select options={TOOL_MODE_OPTIONS} allowClear placeholder="dedup (default)" />
          </Form.Item>
          {(provider === 'openai' || provider === 'bedrock' || provider === 'langserve' || !BUILTIN_PROVIDERS.has(provider)) && (
            <Form.Item
              name="base_url"
              label={provider === 'langserve' ? 'Endpoint URL' : 'Base URL'}
              help={
                provider === 'bedrock' ? 'Corporate proxy URL (optional)'
                : provider === 'langserve' ? 'Required — the LangServe route (/invoke is appended automatically)'
                : !BUILTIN_PROVIDERS.has(provider) ? 'Endpoint URL for this provider plugin (if it needs one)'
                : 'Optional — for OpenAI-compatible endpoints'
              }
            >
              <Input placeholder={provider === 'langserve' ? 'https://gateway.corp/my-chain' : 'https://...'} style={{ fontFamily: 'monospace' }} />
            </Form.Item>
          )}
          {provider === 'bedrock' && (
            <>
              <Form.Item name="api_key" label="Proxy API key" help="Only needed if using a corporate Bedrock proxy">
                <Input.Password placeholder="token" />
              </Form.Item>
              <Form.Item name="aws_region" label="AWS region">
                <Input placeholder="us-east-1" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </>
          )}

          <Collapse
            size="small"
            ghost
            style={{ marginTop: 4 }}
            items={[{
              key: 'advanced',
              // Force-render the panel so its Form.Items are always mounted. Without
              // this, antd Collapse leaves an unexpanded panel's children unmounted,
              // so getFieldsValue() omits them at Save time and the set-or-delete loop
              // drops hand-edited keys (api_key_command, api_key_ttl_ms, …) whenever
              // the user saves without first opening Advanced.
              forceRender: true,
              label: <Text style={{ fontSize: 12 }}>Advanced</Text>,
              children: (
                <>
                  <Form.Item
                    name="api_key_command"
                    label="Token command"
                    help="Run this command to vend a short-lived token instead of a static key (rotating corporate creds). Its stdout is used as the token."
                  >
                    <Input placeholder="aws-vend-token --print" style={{ fontFamily: 'monospace' }} />
                  </Form.Item>
                  <Form.Item
                    name="api_key_ttl_ms"
                    label="Token cache (ms)"
                    help="How long to reuse the token command's output. 0 or blank re-runs it every request."
                  >
                    <InputNumber min={0} step={1000} style={{ width: '100%' }} placeholder="0" />
                  </Form.Item>

                  {provider === 'bedrock' && (
                    <>
                      <Form.Item
                        name="aws_access_key"
                        label="AWS access key ID"
                        help="Optional — leave blank to use the standard AWS credential chain (env vars / shared profile / STS). Stored in plaintext in config.toml."
                      >
                        <Input.Password placeholder="AKIA… (optional)" />
                      </Form.Item>
                      <Form.Item name="aws_secret_key" label="AWS secret access key">
                        <Input.Password placeholder="(optional)" />
                      </Form.Item>
                      <Form.Item name="aws_session_token" label="AWS session token">
                        <Input.Password placeholder="(optional — for temporary STS creds)" />
                      </Form.Item>
                    </>
                  )}

                  {provider === 'langserve' && (
                    <>
                      <Divider style={{ margin: '4px 0 10px' }}>
                        <Text type="secondary" style={{ fontSize: 11 }}>LangServe wiring</Text>
                      </Divider>
                      <Form.Item name="auth_header" label="Auth header" help="HTTP header the token is sent in.">
                        <Input placeholder="Authorization" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <Form.Item name="auth_scheme" label="Auth scheme" help="Scheme prefix; blank sends the raw token.">
                        <Input placeholder="Bearer" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <Form.Item name="input_messages_key" label="Messages key" help="Key the chat history is sent under, inside input.">
                        <Input placeholder="messages" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <Form.Item name="input_tools_key" label="Tools key" help="Key the tool schemas are sent under.">
                        <Input placeholder="tools" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <Form.Item
                        name="tools_in_config"
                        label="Tools in config"
                        valuePropName="checked"
                        help="When on, tools move to config.configurable instead of input."
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </>
                  )}
                </>
              ),
            }]}
          />
        </>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <Button type="primary" size="small" loading={saving} onClick={handleSave}>Save</Button>
        {saved && <Text type="success" style={{ fontSize: 12 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
        Settings are saved to <code>{configDir || '%APPDATA%/runspec-console'}</code>
      </Text>
    </Form>
  )
}

// ── SSH tab ───────────────────────────────────────────────────────────────────

function keyAgeDays(createdAt: string | null): number | null {
  if (!createdAt) return null
  return Math.floor((Date.now() - new Date(createdAt).getTime()) / (1000 * 60 * 60 * 24))
}

function SshTab({ onKeyChanged, connectedHosts = [], allRemoteHosts = [] }: { onKeyChanged?: () => void; connectedHosts?: string[]; allRemoteHosts?: string[] }) {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [selectedHost, setSelectedHost] = useState<string>('')
  const [generating, setGenerating] = useState(false)
  const [publicKey, setPublicKey] = useState<string | null>(null)
  const [storedPublicKey, setStoredPublicKey] = useState<string | null>(null)
  const [keyPath, setKeyPath] = useState<string | null>(null)
  const [keyCreatedAt, setKeyCreatedAt] = useState<string | null>(null)
  const [configDir, setConfigDir] = useState<string>('')
  const [rotationResult, setRotationResult] = useState<{ committed: boolean; perHost: RotateHostResult[]; publicKey: string } | null>(null)
  const [copyAllOpen, setCopyAllOpen] = useState(false)

  const loadConfig = () => {
    bridge.get_config().then(cfg => {
      const ssh = (cfg.ssh ?? {}) as Record<string, unknown>
      const refresh = (cfg.refresh ?? {}) as Record<string, unknown>
      setKeyCreatedAt((ssh.key_created_at as string) ?? null)
      setKeyPath((ssh.identityFile as string) ?? null)
      // Normalise the host-key mode to the two options the picker offers: any
      // strict alias ("yes"/"true"/"strict"/"on"/"1") shows as "yes".
      const hk = String(ssh.host_key_checking ?? '').toLowerCase()
      const hkVal = ['yes', 'true', 'strict', 'on', '1'].includes(hk)
        ? 'yes'
        : hk === 'accept-new' ? 'accept-new' : ''
      form.setFieldsValue({
        ssh_user: (ssh.user as string) ?? '',
        ssh_proxy: (ssh.proxy as string) ?? '',
        ssh_use_ssh_config: !!ssh.use_ssh_config,
        ssh_host_key_checking: hkVal,
        ssh_connect_timeout: ssh.connect_timeout ?? undefined,
        ssh_banner_timeout: ssh.banner_timeout ?? undefined,
        ssh_auth_timeout: ssh.auth_timeout ?? undefined,
        // TCP keepalive is on unless explicitly disabled in config.
        ssh_tcp_keepalive: ssh.tcp_keepalive !== false,
        ssh_tcp_keepidle: ssh.tcp_keepidle ?? undefined,
        ssh_tcp_keepintvl: ssh.tcp_keepintvl ?? undefined,
        ssh_tcp_keepcnt: ssh.tcp_keepcnt ?? undefined,
        refresh_interval: refresh.interval ?? undefined,
        refresh_jitter: refresh.jitter ?? undefined,
      })
    })
    bridge.get_public_key()
      .then(r => setStoredPublicKey(r.ok ? r.public_key : null))
      .catch(() => setStoredPublicKey(null))
  }

  useEffect(() => { loadConfig() }, [form])
  useEffect(() => { bridge.config_dir().then(setConfigDir) }, [])
  useEffect(() => {
    if (!selectedHost && connectedHosts.length > 0) setSelectedHost(connectedHosts[0])
  }, [connectedHosts, selectedHost])

  const handleSave = async () => {
    const v = form.getFieldsValue()
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      const sshOut: Record<string, unknown> = { ...((cfg.ssh as Record<string, unknown>) ?? {}) }
      if (v.ssh_user) sshOut.user = v.ssh_user
      if (v.ssh_proxy) sshOut.proxy = v.ssh_proxy
      else delete sshOut.proxy
      sshOut.use_ssh_config = !!v.ssh_use_ssh_config
      // Advanced: set-or-delete so a blank field falls back to the safe default.
      if (v.ssh_host_key_checking) sshOut.host_key_checking = v.ssh_host_key_checking
      else delete sshOut.host_key_checking
      for (const [field, key] of [
        ['ssh_connect_timeout', 'connect_timeout'],
        ['ssh_banner_timeout', 'banner_timeout'],
        ['ssh_auth_timeout', 'auth_timeout'],
        ['ssh_tcp_keepidle', 'tcp_keepidle'],
        ['ssh_tcp_keepintvl', 'tcp_keepintvl'],
        ['ssh_tcp_keepcnt', 'tcp_keepcnt'],
      ] as const) {
        const n = v[field]
        if (n !== undefined && n !== null && n !== '') sshOut[key] = Number(n)
        else delete sshOut[key]
      }
      // TCP keepalive is on by default — only persist an explicit opt-out so a
      // default install keeps a clean config (mirrors the backend default).
      if (v.ssh_tcp_keepalive === false) sshOut.tcp_keepalive = false
      else delete sshOut.tcp_keepalive
      const refreshOut: Record<string, unknown> = { ...((cfg.refresh as Record<string, unknown>) ?? {}) }
      for (const [field, key] of [
        ['refresh_interval', 'interval'],
        ['refresh_jitter', 'jitter'],
      ] as const) {
        const n = v[field]
        if (n !== undefined && n !== null && n !== '') refreshOut[key] = Number(n)
        else delete refreshOut[key]
      }
      await bridge.save_config({ ...cfg, ssh: sshOut, refresh: refreshOut })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const handleGenerate = async () => {
    setGenerating(true)
    setPublicKey(null)
    setRotationResult(null)
    try {
      const result = await bridge.generate_ssh_key()
      if (result.ok) {
        setPublicKey(result.public_key)
        setKeyPath(result.key_path)
        loadConfig()
        onKeyChanged?.()
        message.success('Key generated')
      } else {
        message.error(result.message)
      }
    } finally {
      setGenerating(false)
    }
  }

  const handleRotate = async () => {
    setGenerating(true)
    setPublicKey(null)
    setRotationResult(null)
    try {
      const result = await bridge.rotate_ssh_key()
      setRotationResult({ committed: result.committed, perHost: result.per_host, publicKey: result.public_key })
      const anySkipped = result.per_host.some(h => h.skipped)
      if (result.committed) {
        setPublicKey(null) // committed — manual snippet still shown below if any host was skipped
        setKeyPath(result.key_path)
        loadConfig()
        onKeyChanged?.()
        // Use the backend message — it reports how many hosts verified vs. skipped
        if (anySkipped) message.warning(result.message)
        else message.success(result.message)
      } else if (!result.ok) {
        message.error(result.message)
      } else {
        // partial failure — show per-host results, old key still active
        message.warning(result.message)
      }
    } finally {
      setGenerating(false)
    }
  }

  const ageDays = keyAgeDays(keyCreatedAt)
  const authorizedKeysLine = publicKey ? `echo "${publicKey}" >> ~/.ssh/authorized_keys` : ''
  // Configured remotes that aren't currently connected — they won't receive the new key.
  const disconnected = allRemoteHosts.filter(h => !connectedHosts.includes(h))

  return (
    <Form form={form} layout="vertical" size="small" style={{ marginTop: 4 }}>
      <Form.Item name="ssh_user" label="Default username">
        <Input placeholder="your-username" />
      </Form.Item>

      <Form.Item
        name="ssh_proxy"
        label="HTTP proxy"
        help="Routes SSH — running runnables and Launch terminal — through an HTTP CONNECT proxy. Leave blank for none."
      >
        <Input placeholder="http://proxy.corp:8080" style={{ fontFamily: 'monospace' }} />
      </Form.Item>

      <Form.Item
        name="ssh_use_ssh_config"
        label="Honor ~/.ssh/config"
        valuePropName="checked"
        help="Also read ~/.ssh/config (HostName, User, Port, IdentityFile, ProxyCommand) when running runnables. PuTTY uses the HTTP proxy above, not ~/.ssh/config."
      >
        <Switch size="small" />
      </Form.Item>

      <Collapse
        size="small"
        ghost
        items={[{
          key: 'advanced',
          label: <Text style={{ fontSize: 12 }}>Advanced</Text>,
          children: (
            <>
              <Form.Item
                name="ssh_host_key_checking"
                label="Host-key checking"
                help="accept-new (default) trusts a host the first time and rejects a changed key (TOFU). Strict rejects any host not already in known_hosts."
              >
                <Select
                  allowClear
                  placeholder="accept-new (default)"
                  options={[
                    { value: 'accept-new', label: 'Accept new (TOFU — default)' },
                    { value: 'yes', label: 'Strict (known hosts only)' },
                  ]}
                />
              </Form.Item>
              <Form.Item
                name="ssh_connect_timeout"
                label="Connect timeout (s)"
                help="TCP connect timeout. Default 10."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="10" />
              </Form.Item>
              <Form.Item
                name="ssh_banner_timeout"
                label="Banner timeout (s)"
                help="Wait for the SSH banner — raise it for busy servers (sshd MaxStartups). Default 30."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="30" />
              </Form.Item>
              <Form.Item
                name="ssh_auth_timeout"
                label="Auth timeout (s)"
                help="Wait for the authentication exchange. Default 30."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="30" />
              </Form.Item>

              <Divider style={{ margin: '4px 0 10px' }}>
                <Text type="secondary" style={{ fontSize: 11 }}>TCP keepalive</Text>
              </Divider>
              <Form.Item
                name="ssh_tcp_keepalive"
                label="Socket keepalive"
                valuePropName="checked"
                help="OS-level SO_KEEPALIVE on the SSH socket — a backstop to the SSH keepalive that lets the kernel drop a silently-dead connection (NAT/VPN idle-drop, peer reboot). On by default; the probe schedule below applies where the OS supports it."
              >
                <Switch size="small" />
              </Form.Item>
              <Form.Item
                name="ssh_tcp_keepidle"
                label="Idle before first probe (s)"
                help="Seconds a connection may sit idle before the first keepalive probe. Default 60."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="60" />
              </Form.Item>
              <Form.Item
                name="ssh_tcp_keepintvl"
                label="Probe interval (s)"
                help="Seconds between keepalive probes once they start. Default 15."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="15" />
              </Form.Item>
              <Form.Item
                name="ssh_tcp_keepcnt"
                label="Probes before drop"
                help="Unanswered probes before the socket is declared dead. Default 3. (Probe count is fixed by the OS on Windows.)"
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="3" />
              </Form.Item>

              <Divider style={{ margin: '4px 0 10px' }}>
                <Text type="secondary" style={{ fontSize: 11 }}>Discovery</Text>
              </Divider>
              <Form.Item
                name="refresh_interval"
                label="Refresh interval (s)"
                help="How often the console re-scans hosts for runnables. Default 30."
              >
                <InputNumber min={1} style={{ width: '100%' }} placeholder="30" />
              </Form.Item>
              <Form.Item
                name="refresh_jitter"
                label="Refresh jitter (± s)"
                help="Random spread on each host's scan start, to avoid a thundering herd. Default 3."
              >
                <InputNumber min={0} style={{ width: '100%' }} placeholder="3" />
              </Form.Item>
            </>
          ),
        }]}
      />

      <Divider />

      {/* SSH key section */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <Text strong style={{ fontSize: 13 }}>SSH key</Text>
        {ageDays !== null && (
          <Tag
            color={ageDays >= 90 ? 'orange' : ageDays >= 75 ? 'gold' : 'success'}
            icon={<KeyOutlined />}
            style={{ margin: 0, fontSize: 11 }}
          >
            {ageDays === 0 ? 'created today' : `${ageDays}d old`}
          </Tag>
        )}
      </div>

      {ageDays !== null && ageDays >= 75 && (
        <div style={{
          padding: '6px 10px', borderRadius: 6, marginBottom: 10,
          background: ageDays >= 90 ? 'rgba(250,140,22,0.1)' : 'rgba(250,219,20,0.08)',
          border: `1px solid ${ageDays >= 90 ? 'rgba(250,140,22,0.3)' : 'rgba(250,219,20,0.3)'}`,
          fontSize: 12, color: ageDays >= 90 ? '#fa8c16' : '#d4b106',
        }}>
          {ageDays >= 90 ? `Key is ${ageDays} days old — rotation recommended.` : `Key is ${ageDays} days old — consider rotating soon.`}
        </div>
      )}

      {keyPath && (
        <div style={{ marginBottom: 10 }}>
          <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>
            {keyPath}
          </Text>
        </div>
      )}

      <Space wrap style={{ marginBottom: 8 }}>
        {!keyPath ? (
          <Button
            type="primary"
            size="small"
            icon={generating ? <LoadingOutlined /> : <KeyOutlined />}
            disabled={generating}
            onClick={handleGenerate}
          >
            Generate key
          </Button>
        ) : (
          <Popconfirm
            title="Generate new key?"
            description={
              <span style={{ fontSize: 12 }}>
                The current key will be backed up and replaced immediately.<br />
                You'll need to add the new public key to each host manually.<br />
                Use <strong>Rotate key</strong> instead if your remote hosts are reachable.
              </span>
            }
            onConfirm={handleGenerate}
            okText="Generate" cancelText="Cancel"
            placement="bottom"
          >
            <Button
              size="small"
              icon={generating ? <LoadingOutlined /> : <KeyOutlined />}
              disabled={generating}
            >
              Generate new key
            </Button>
          </Popconfirm>
        )}
        {keyPath && (
          <Popconfirm
            title="Rotate SSH key?"
            icon={disconnected.length > 0 ? <KeyOutlined style={{ color: '#fa8c16' }} /> : undefined}
            description={
              disconnected.length === 0 ? (
                <span style={{ fontSize: 12 }}>
                  A new key will be pushed to every connected host using the current key,<br />
                  then verified before swapping. Active sessions stay alive throughout.
                </span>
              ) : (
                <span style={{ fontSize: 12, maxWidth: 320, display: 'inline-block' }}>
                  <strong style={{ color: '#fa8c16' }}>
                    {disconnected.length} host{disconnected.length !== 1 ? 's' : ''} disconnected:
                  </strong>{' '}
                  <span style={{ fontFamily: 'monospace' }}>{disconnected.join(', ')}</span>.<br />
                  {disconnected.length !== 1 ? 'They' : 'It'} will <strong>not</strong> receive the new key —
                  you may be locked out until you update {disconnected.length !== 1 ? 'them' : 'it'} manually.<br />
                  Connected hosts rotate now. Continue?
                </span>
              )
            }
            onConfirm={handleRotate}
            okText={disconnected.length > 0 ? 'Rotate anyway' : 'Rotate'}
            okButtonProps={disconnected.length > 0 ? { danger: true } : undefined}
            cancelText="Cancel"
            placement="bottom"
          >
            <Button
              size="small"
              icon={generating ? <LoadingOutlined /> : <KeyOutlined />}
              disabled={generating}
              loading={generating}
            >
              {generating ? 'Rotating…' : 'Rotate key'}
            </Button>
          </Popconfirm>
        )}
        {keyPath && allRemoteHosts.length > 0 && (
          <Tooltip title="Authorize this machine on every host at once over a one-off password — for bootstrapping a freshly synced fleet">
            <Button
              size="small"
              icon={<KeyOutlined />}
              disabled={generating}
              onClick={() => setCopyAllOpen(true)}
            >
              Copy key to all hosts…
            </Button>
          </Tooltip>
        )}
      </Space>

      {/* Per-host rotation results */}
      {rotationResult && rotationResult.perHost.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          {rotationResult.perHost.map(h => {
            const icon = h.skipped ? '⚠' : h.verified ? '✓' : '✗'
            const color = h.skipped ? '#d4b106' : h.verified ? '#52c41a' : '#ff4d4f'
            return (
              <div key={h.host} style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 3 }}>
                <span style={{ color, fontFamily: 'monospace', fontSize: 11, flexShrink: 0 }}>{icon} {h.host}</span>
                {h.error && <Text type="secondary" style={{ fontSize: 10 }}>{h.error}</Text>}
              </div>
            )
          })}
          {!rotationResult.committed && (
            <Text type="warning" style={{ fontSize: 11, display: 'block', marginTop: 4 }}>
              Old key still active — fix the failures above and retry.
            </Text>
          )}
        </div>
      )}

      {/* Manual snippet — shown on partial/total failure, or when a committed rotation
          skipped some hosts (disconnected / per-host key) that still need the new key */}
      {rotationResult && rotationResult.publicKey &&
        (!rotationResult.committed || rotationResult.perHost.some(h => h.skipped)) && (
        <div style={{ marginBottom: 10 }}>
          <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
            Run on each skipped/failing host to authorise the new key manually:
          </Text>
          <Typography.Paragraph
            copyable={{ tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: 'monospace', fontSize: 10, padding: '6px 10px',
              background: 'rgba(255,255,255,0.04)', border: '1px solid #333',
              borderRadius: 4, wordBreak: 'break-all', margin: 0, color: '#52c41a',
            }}
          >
            {`echo "${rotationResult.publicKey}" >> ~/.ssh/authorized_keys`}
          </Typography.Paragraph>
        </div>
      )}

      {/* First-time generate — show manual snippet since there are no sessions to push via */}
      {publicKey && !rotationResult && (
        <div style={{ marginBottom: 10 }}>
          <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
            Run on each host to authorise this key:
          </Text>
          <Typography.Paragraph
            copyable={{ tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: 'monospace', fontSize: 10, padding: '6px 10px',
              background: 'rgba(255,255,255,0.04)', border: '1px solid #333',
              borderRadius: 4, wordBreak: 'break-all', margin: 0, color: '#52c41a',
            }}
          >
            {authorizedKeysLine}
          </Typography.Paragraph>
        </div>
      )}

      {/* Persistent public key — always available to copy, derived if no .pub */}
      {keyPath && storedPublicKey && !publicKey && !rotationResult && (
        <div style={{ marginBottom: 10 }}>
          <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
            Public key — add to each host's authorized_keys:
          </Text>
          <Typography.Paragraph
            copyable={{ text: storedPublicKey, tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: 'monospace', fontSize: 10, padding: '6px 10px',
              background: 'rgba(255,255,255,0.04)', border: '1px solid #333',
              borderRadius: 4, wordBreak: 'break-all', margin: 0, color: '#52c41a',
            }}
          >
            {`echo "${storedPublicKey}" >> ~/.ssh/authorized_keys`}
          </Typography.Paragraph>
        </div>
      )}

      <Divider />

      <Text strong style={{ fontSize: 13 }}>Quick connect</Text>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10 }}>
        <Select
          size="small"
          value={connectedHosts.length === 0 ? undefined : selectedHost}
          placeholder={connectedHosts.length === 0 ? 'No connected hosts' : 'Select host'}
          disabled={connectedHosts.length === 0}
          onChange={setSelectedHost}
          options={connectedHosts.map(h => ({ value: h, label: h }))}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Button
          size="small"
          icon={<DesktopOutlined />}
          disabled={connectedHosts.length === 0 || !selectedHost}
          onClick={() => bridge.launch_terminal(selectedHost).catch(e => message.error(String(e)))}
        >
          Open PuTTY
        </Button>
      </div>

      <Divider />

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Button type="primary" size="small" loading={saving} onClick={handleSave}>Save</Button>
        {saved && <Text type="success" style={{ fontSize: 12 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
        Settings are saved to <code>{configDir || '%APPDATA%/runspec-console'}</code>
      </Text>

      <CopyKeyAllModal
        open={copyAllOpen}
        hosts={allRemoteHosts.map(name => ({ name, connected: connectedHosts.includes(name) }))}
        onClose={() => setCopyAllOpen(false)}
        onDone={() => onKeyChanged?.()}
      />
    </Form>
  )
}

// ── Jump Hosts tab ────────────────────────────────────────────────────────────

interface HostFormValues {
  name: string
  hostname: string
  runspec_paths?: string[]
  user?: string
  port?: number
  identityFile?: string
  group?: string
  profiles?: string[]
  room?: string
  room_port?: number
  room_user?: string
  room_via?: string
  room_exec_command?: string
  room_queue?: string
  room_catalog?: string[]
}

function toToml(hosts: JumpHost[]): string {
  return hosts.map(h => {
    const lines = [`[${h.name}]`, `hostname = "${h.hostname}"`]
    if (h.user) lines.push(`user = "${h.user}"`)
    if (h.port) lines.push(`port = ${h.port}`)
    if (h.identityFile) lines.push(`identity_file = "${h.identityFile}"`)
    if (h.group) lines.push(`group = "${h.group}"`)
    if (h.room) lines.push(`room = "${h.room}"`)
    if (h.room_port) lines.push(`room_port = ${h.room_port}`)
    if (h.room_user) lines.push(`room_user = "${h.room_user}"`)
    if (h.room_via === 'exec') lines.push(`room_via = "exec"`)
    if (h.room_exec_command) lines.push(`room_exec_command = "${h.room_exec_command}"`)
    if (h.room_queue) lines.push(`room_queue = "${h.room_queue}"`)
    if (h.room_catalog?.length) {
      const names = h.room_catalog.map(n => `"${n}"`).join(', ')
      lines.push(`room_catalog = [${names}]`)
    }
    if (h.runspec_paths?.length) {
      const paths = h.runspec_paths.map(p => `"${p}"`).join(', ')
      lines.push(`runspec_paths = [${paths}]`)
    }
    return lines.join('\n')
  }).join('\n\n')
}

function downloadToml(content: string, filename = 'jump_hosts.toml') {
  const blob = new Blob([content], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// Manage the canonical host-group registry (runspec_groups.toml). Names are the
// controlled vocabulary the host editor picks from; a rename re-points member
// hosts, and a delete resets affected hosts to Ungrouped (handled in the bridge).
function GroupsManager({
  open,
  groups,
  onClose,
  onChanged,
}: {
  open: boolean
  groups: HostGroup[]
  onClose: () => void
  onChanged: () => void
}) {
  const [rows, setRows] = useState<HostGroup[]>([])
  const [newName, setNewName] = useState('')

  useEffect(() => {
    if (open) { setRows(groups.map(g => ({ ...g }))); setNewName('') }
  }, [open, groups])

  const addGroup = async () => {
    const n = newName.trim()
    if (!n) return
    if (rows.some(r => r.name.toLowerCase() === n.toLowerCase())) {
      message.warning('A group with that name already exists'); return
    }
    await bridge.save_groups([...rows, { name: n, description: '' }])
    setNewName(''); onChanged()
  }

  const removeGroup = async (name: string) => {
    await bridge.save_groups(rows.filter(r => r.name !== name))
    onChanged()
  }

  const commitRename = async (oldName: string, nextName: string) => {
    const nn = nextName.trim()
    if (!nn || nn === oldName) { setRows(groups.map(g => ({ ...g }))); return }
    if (rows.some(r => r.name.toLowerCase() === nn.toLowerCase() && r.name !== oldName)) {
      message.warning('A group with that name already exists')
      setRows(groups.map(g => ({ ...g }))); return
    }
    await bridge.rename_group(oldName, nn)
    onChanged()
  }

  const commitDescription = async () => {
    await bridge.save_groups(rows)
    onChanged()
  }

  return (
    <Modal open={open} title="Host groups" onCancel={onClose} footer={null} width={520}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        Groups are shared across consoles (Config Sync) so a runbook targeting a
        group behaves the same everywhere. Hosts pick their group from this list.
      </Text>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((g, i) => (
          <Space key={g.name} align="baseline" style={{ display: 'flex' }}>
            <Input
              size="small"
              style={{ width: 150, fontFamily: 'monospace' }}
              value={g.name}
              onChange={e => setRows(r => r.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
              onBlur={e => commitRename(groups[i]?.name ?? g.name, e.target.value)}
              onPressEnter={e => (e.target as HTMLInputElement).blur()}
            />
            <Input
              size="small"
              style={{ width: 220 }}
              placeholder="description (optional)"
              value={g.description ?? ''}
              onChange={e => setRows(r => r.map((x, j) => j === i ? { ...x, description: e.target.value } : x))}
              onBlur={commitDescription}
            />
            <Popconfirm
              title={`Delete group "${g.name}"?`}
              description="Hosts in it become Ungrouped."
              onConfirm={() => removeGroup(g.name)}
            >
              <MinusCircleOutlined style={{ color: '#666', cursor: 'pointer' }} />
            </Popconfirm>
          </Space>
        ))}
        {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No groups yet" />}
        <Space.Compact style={{ marginTop: 4 }}>
          <Input
            size="small"
            style={{ width: 200, fontFamily: 'monospace' }}
            placeholder="New group name"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onPressEnter={addGroup}
          />
          <Button size="small" icon={<PlusOutlined />} onClick={addGroup}>Add group</Button>
        </Space.Compact>
      </div>
    </Modal>
  )
}

// Manage the host-profile registry (runspec_profiles.toml) — the controlled
// vocabulary of working-set names. Mirrors GroupsManager; profiles differ in
// being many-per-host and switchable (the active one is machine-local).
function ProfilesManager({
  open,
  profiles,
  onClose,
  onChanged,
}: {
  open: boolean
  profiles: HostProfile[]
  onClose: () => void
  onChanged: () => void
}) {
  const [rows, setRows] = useState<HostProfile[]>([])
  const [newName, setNewName] = useState('')

  useEffect(() => {
    if (open) { setRows(profiles.map(p => ({ ...p }))); setNewName('') }
  }, [open, profiles])

  const addProfile = async () => {
    const n = newName.trim()
    if (!n) return
    if (rows.some(r => r.name.toLowerCase() === n.toLowerCase())) {
      message.warning('A profile with that name already exists'); return
    }
    await bridge.save_profiles([...rows, { name: n, description: '' }])
    setNewName(''); onChanged()
  }

  const removeProfile = async (name: string) => {
    await bridge.save_profiles(rows.filter(r => r.name !== name))
    onChanged()
  }

  const commitRename = async (oldName: string, nextName: string) => {
    const nn = nextName.trim()
    if (!nn || nn === oldName) { setRows(profiles.map(p => ({ ...p }))); return }
    if (rows.some(r => r.name.toLowerCase() === nn.toLowerCase() && r.name !== oldName)) {
      message.warning('A profile with that name already exists')
      setRows(profiles.map(p => ({ ...p }))); return
    }
    await bridge.rename_profile(oldName, nn)
    onChanged()
  }

  const commitDescription = async () => {
    await bridge.save_profiles(rows)
    onChanged()
  }

  return (
    <Modal open={open} title="Host profiles" onCancel={onClose} footer={null} width={520}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        A profile is a named working-set of hosts. You load one at a time — only its
        hosts stay connected. Definitions are shared across consoles (Config Sync);
        which one you have loaded is local to this machine.
      </Text>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((p, i) => (
          <Space key={p.name} align="baseline" style={{ display: 'flex' }}>
            <Input
              size="small"
              style={{ width: 150, fontFamily: 'monospace' }}
              value={p.name}
              onChange={e => setRows(r => r.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
              onBlur={e => commitRename(profiles[i]?.name ?? p.name, e.target.value)}
              onPressEnter={e => (e.target as HTMLInputElement).blur()}
            />
            <Input
              size="small"
              style={{ width: 220 }}
              placeholder="description (optional)"
              value={p.description ?? ''}
              onChange={e => setRows(r => r.map((x, j) => j === i ? { ...x, description: e.target.value } : x))}
              onBlur={commitDescription}
            />
            <Popconfirm
              title={`Delete profile "${p.name}"?`}
              description="Hosts lose this tag; if it was loaded, you drop to All hosts."
              onConfirm={() => removeProfile(p.name)}
            >
              <MinusCircleOutlined style={{ color: '#666', cursor: 'pointer' }} />
            </Popconfirm>
          </Space>
        ))}
        {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No profiles yet" />}
        <Space.Compact style={{ marginTop: 4 }}>
          <Input
            size="small"
            style={{ width: 200, fontFamily: 'monospace' }}
            placeholder="New profile name"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onPressEnter={addProfile}
          />
          <Button size="small" icon={<PlusOutlined />} onClick={addProfile}>Add profile</Button>
        </Space.Compact>
      </div>
    </Modal>
  )
}

function JumpHostsTab({ onHostsChanged }: { onHostsChanged?: () => void }) {
  const [hosts, setHosts] = useState<JumpHost[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [form] = Form.useForm<HostFormValues>()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [testingHost, setTestingHost] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({})
  const [copyKeyHost, setCopyKeyHost] = useState<string | null>(null)
  const [groups, setGroups] = useState<HostGroup[]>([])
  const [groupsOpen, setGroupsOpen] = useState(false)
  const [profiles, setProfiles] = useState<HostProfile[]>([])
  const [profilesOpen, setProfilesOpen] = useState(false)

  const loadGroups = useCallback(() => { bridge.get_groups().then(setGroups) }, [])
  const loadProfiles = useCallback(() => { bridge.get_profiles().then(setProfiles) }, [])

  // Live connection details of the host being added/edited — drive the runspec-
  // path browser so it lists the host straight from the form (before it's saved).
  const editHostname = Form.useWatch('hostname', form)
  const editUser = Form.useWatch('user', form)
  const editPort = Form.useWatch('port', form)
  const editIdentity = Form.useWatch('identityFile', form)
  const hostReady = !!(editHostname ?? '').trim()

  const pathLister = useCallback(
    (path: string, dirsOnly: boolean) =>
      bridge.list_dir_unsaved(
        (editHostname ?? '').trim(),
        path,
        (editUser ?? '').trim() || undefined,
        editPort ? Number(editPort) : undefined,
        (editIdentity ?? '').trim() || undefined,
        dirsOnly,
      ),
    [editHostname, editUser, editPort, editIdentity],
  )

  useEffect(() => {
    bridge.get_jump_hosts().then(setHosts)
    loadGroups()
    loadProfiles()
  }, [loadGroups, loadProfiles])

  // Tear down any browse connection opened from the add/edit form on unmount.
  useEffect(() => () => { bridge.close_adhoc().catch(() => {}) }, [])

  const persist = async (next: JumpHost[]) => {
    setHosts(next)
    await bridge.save_jump_hosts(next)
    onHostsChanged?.()
  }

  const startAdd = () => { form.resetFields(); setEditingKey('') }

  const startEdit = (h: JumpHost) => {
    // group defaults to "" (Ungrouped) so the required Select has a valid value
    // for a host that predates the registry.
    form.setFieldsValue({ ...h, runspec_paths: h.runspec_paths ?? [], group: h.group ?? '' })
    setEditingKey(h.name)
  }

  const cancel = () => { setEditingKey(null); form.resetFields(); bridge.close_adhoc().catch(() => {}) }

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!fileInputRef.current) return
    fileInputRef.current.value = ''
    if (!file) return
    const content = await file.text()
    const imported = await bridge.import_jump_hosts(content)
    if (imported.length === 0) { message.warning('No hosts found in file'); return }
    const updated = await bridge.get_jump_hosts()
    setHosts(updated)
    message.success(`Imported ${imported.length} host${imported.length !== 1 ? 's' : ''}`)
  }

  const save = async () => {
    const values = await form.validateFields()
    // <Input type="number"> yields a string — coerce the room port, and drop the
    // room fields when no room is set (the backend wants an int port or nothing).
    const v: HostFormValues = { ...values }
    v.profiles = Array.isArray(v.profiles)
      ? v.profiles.map(s => s.toString().trim()).filter(Boolean)
      : undefined
    if (!v.profiles?.length) delete v.profiles
    v.room = (v.room ?? '').toString().trim() || undefined
    v.room_queue = (v.room_queue ?? '').toString().trim() || undefined
    v.room_user = (v.room_user ?? '').toString().trim() || undefined
    v.room_exec_command = (v.room_exec_command ?? '').toString().trim() || undefined
    v.room_via = v.room_via === 'exec' ? 'exec' : undefined
    v.room_catalog = Array.isArray(v.room_catalog)
      ? v.room_catalog.map(s => s.toString().trim()).filter(Boolean)
      : undefined
    // A host with a room_port runs the room — that alone staffs the helpdesk (the
    // queue name defaults server-side; no room_queue needed). A group-chat room is
    // optional. room_port alone is enough.
    if (v.room_port != null && `${v.room_port}` !== '') {
      v.room_port = Number(v.room_port)
    } else {
      delete v.room
      delete v.room_queue
      delete v.room_port
      delete v.room_user
      delete v.room_via
      delete v.room_exec_command
    }
    // The catalog allowlist only means anything on a host that runs the room.
    if (!v.room_port || !v.room_catalog?.length) delete v.room_catalog
    if (editingKey === '') {
      await persist([...hosts, v as JumpHost])
    } else {
      await persist(hosts.map(h => h.name === editingKey ? { ...h, ...v } : h))
    }
    setEditingKey(null)
    form.resetFields()
    bridge.close_adhoc().catch(() => {})
  }

  const remove = async (name: string) => {
    await persist(hosts.filter(h => h.name !== name))
    if (editingKey === name) setEditingKey(null)
  }

  const buildGroups = (hostList: JumpHost[]) => {
    const map = new Map<string, JumpHost[]>()
    const order: string[] = []
    for (const h of hostList) {
      const g = h.group ?? ''
      if (!map.has(g)) { map.set(g, []); order.push(g) }
      map.get(g)!.push(h)
    }
    return order.map(g => ({ name: g, hosts: map.get(g)! }))
  }

  const moveGroupUp = async (groupIdx: number) => {
    const groups = buildGroups(hosts)
    if (groupIdx === 0) return
    const next = [...groups];
    [next[groupIdx - 1], next[groupIdx]] = [next[groupIdx], next[groupIdx - 1]]
    await persist(next.flatMap(g => g.hosts))
  }

  const moveGroupDown = async (groupIdx: number) => {
    const groups = buildGroups(hosts)
    if (groupIdx === groups.length - 1) return
    const next = [...groups];
    [next[groupIdx], next[groupIdx + 1]] = [next[groupIdx + 1], next[groupIdx]]
    await persist(next.flatMap(g => g.hosts))
  }

  const moveHostUp = async (h: JumpHost) => {
    const groups = buildGroups(hosts)
    const group = groups.find(g => g.hosts.some(x => x.name === h.name))!
    const idx = group.hosts.findIndex(x => x.name === h.name)
    if (idx === 0) return
    const next = [...group.hosts];
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]]
    await persist(groups.flatMap(g => g.name === group.name ? next : g.hosts))
  }

  const moveHostDown = async (h: JumpHost) => {
    const groups = buildGroups(hosts)
    const group = groups.find(g => g.hosts.some(x => x.name === h.name))!
    const idx = group.hosts.findIndex(x => x.name === h.name)
    if (idx === group.hosts.length - 1) return
    const next = [...group.hosts];
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]]
    await persist(groups.flatMap(g => g.name === group.name ? next : g.hosts))
  }

  const handleTest = async (name: string) => {
    setTestingHost(name)
    setTestResults(r => { const n = { ...r }; delete n[name]; return n })
    try {
      const result = await bridge.test_host(name)
      setTestResults(r => ({ ...r, [name]: result }))
    } finally {
      setTestingHost(null)
    }
  }

  const isEditing = editingKey !== null

  return (
    <div>
      <input ref={fileInputRef} type="file" accept=".toml" style={{ display: 'none' }} onChange={handleImport} />
      <GroupsManager
        open={groupsOpen}
        groups={groups}
        onClose={() => setGroupsOpen(false)}
        onChanged={() => { loadGroups(); bridge.get_jump_hosts().then(setHosts); onHostsChanged?.() }}
      />
      <ProfilesManager
        open={profilesOpen}
        profiles={profiles}
        onClose={() => setProfilesOpen(false)}
        onChanged={() => { loadProfiles(); bridge.get_jump_hosts().then(setHosts); onHostsChanged?.() }}
      />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Saved to <code>jump_hosts.toml</code>. Hosts are probed on startup.
        </Text>
        <Space size={6}>
          <Button size="small" icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()} disabled={isEditing}>Import</Button>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => { downloadToml(toToml(hosts)); message.success('Exported') }} disabled={isEditing || hosts.length === 0}>Export</Button>
          <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add host</Button>
        </Space>
      </div>

      {/* The add/edit form lives in a modal so editing a host deep in a long list
          never requires scrolling back to the top — it opens centred in view. */}
      <Modal
        open={editingKey !== null}
        title={editingKey === '' ? 'New jump host' : `Edit — ${editingKey}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
              <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="prod-eu-1" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
              </Form.Item>
              <Form.Item name="hostname" label="Hostname" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="hostname.company.com" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Form.Item name="user" label="SSH user">
                <Input placeholder="inherits default" />
              </Form.Item>
              <Form.Item name="port" label="Port">
                <Input placeholder="22" type="number" />
              </Form.Item>
            </div>
            <Form.Item
              label="runspec path(s) on host"
              help={hostReady ? undefined : 'Enter a hostname above to browse the host for where runspec lives.'}
            >
              <Form.List name="runspec_paths">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...restField }) => (
                      <Space key={key} align="baseline" style={{ display: 'flex', marginBottom: 4 }}>
                        <Form.Item {...restField} name={name} style={{ marginBottom: 0, width: 312 }}>
                          <PathInput
                            host={(editHostname ?? '').trim() || 'host'}
                            lister={pathLister}
                            browseDisabled={!hostReady}
                            placeholder="/home/user/.venv/bin/runspec"
                          />
                        </Form.Item>
                        <MinusCircleOutlined onClick={() => remove(name)} style={{ color: '#666', cursor: 'pointer' }} />
                      </Space>
                    ))}
                    <Button type="dashed" size="small" onClick={() => add()} icon={<PlusOutlined />} style={{ marginTop: 2 }}>Add path</Button>
                  </>
                )}
              </Form.List>
            </Form.Item>
            <Form.Item
              name="group"
              label={
                <Space size={6}>
                  Group
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0, height: 'auto', fontSize: 12 }}
                    onClick={() => setGroupsOpen(true)}
                  >
                    Manage groups…
                  </Button>
                </Space>
              }
              // Required *selection* — pick from the shared registry rather than
              // typing a free-form name (which would diverge across consoles and
              // break shared runbooks). "Ungrouped" is the explicit no-group pick.
              rules={[{ required: true, message: 'Pick a group (or Ungrouped)' }]}
              initialValue=""
            >
              <Select
                placeholder="Select a group"
                options={[
                  { value: '', label: 'Ungrouped' },
                  ...groups.map(g => ({ value: g.name, label: g.name })),
                ]}
              />
            </Form.Item>
            <Form.Item
              name="profiles"
              label={
                <Space size={6}>
                  Profiles
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0, height: 'auto', fontSize: 12 }}
                    onClick={() => setProfilesOpen(true)}
                  >
                    Manage profiles…
                  </Button>
                </Space>
              }
              help="Working-sets this host belongs to. You load one profile at a time — only its hosts stay connected. Optional; a host in no profile shows only under All hosts."
            >
              <Select
                mode="multiple"
                allowClear
                placeholder="No profiles (All hosts only)"
                options={profiles.map(p => ({ value: p.name, label: p.name }))}
              />
            </Form.Item>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
              <Form.Item
                name="room_port"
                label="Room port"
                help="The room server's localhost port on this host. Setting it staffs the helpdesk over this host's SSH tunnel — no queue name needed."
              >
                <Input placeholder="8765" type="number" />
              </Form.Item>
              <Form.Item name="room_user" label="Display name" help="Your name in the helpdesk. Defaults to the SSH login.">
                <Input placeholder="defaults to SSH login" />
              </Form.Item>
              <Form.Item name="room_via" label="Connect via" help="Tunnel forwards a port; exec runs a relay command (for networks that block forwarding).">
                <Select
                  options={[
                    { value: 'tunnel', label: 'Tunnel (port-forward)' },
                    { value: 'exec', label: 'Exec (relay command)' },
                  ]}
                  allowClear
                  placeholder="tunnel (default)"
                />
              </Form.Item>
              <Form.Item name="room_exec_command" label="Relay command" help="Optional override run on the host when via=exec, e.g. socat - TCP:127.0.0.1:8765. Defaults to console-room connect.">
                <Input placeholder="defaults to console-room connect" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              {/* The group-chat `room` stays hidden in the helpdesk-focused build
                  (kept so an existing config still round-trips; remove `hidden` to
                  re-expose). The queue name IS exposed: it defaults to "helpdesk",
                  so two hosts that both run the room collide on the namespace
                  `helpdesk/helpdesk` and only the first connects — give each a
                  distinct name here. */}
              <Form.Item name="room" hidden>
                <Input />
              </Form.Item>
              <Form.Item
                name="room_queue"
                label="Queue name"
                help='The helpdesk queue this host staffs — becomes the room "helpdesk/<name>". Defaults to "helpdesk"; set a distinct name on each host so two rooms do not collide (a same-named room is skipped).'
              >
                <Input placeholder="helpdesk" />
              </Form.Item>
              <Form.Item name="room_catalog" label="Catalogue allowlist" help="Optional: limit the helpdesk's self-service catalogue to these runnables. Empty = every self-service-marked runnable.">
                <Select
                  mode="tags"
                  tokenSeparators={[',', ' ']}
                  allowClear
                  placeholder="all self-service runnables"
                  open={false}
                  suffixIcon={null}
                />
              </Form.Item>
            </div>
        </Form>
      </Modal>

      {hosts.length === 0 && editingKey === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No jump hosts configured. Add one to get started.</Text>
      )}

      {(() => {
        const groups = buildGroups(hosts)
        return groups.map(({ name: groupName, hosts: groupHosts }, groupIdx) => (
          <div key={groupName || '__ungrouped__'} style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 6 }}>
              <Text type="secondary" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', flex: 1 }}>
                {groupName || 'Hosts'}
              </Text>
              <Button type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />} onClick={() => moveGroupUp(groupIdx)} disabled={isEditing || groupIdx === 0} style={{ padding: '0 4px', color: '#555', height: 18 }} />
              <Button type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />} onClick={() => moveGroupDown(groupIdx)} disabled={isEditing || groupIdx === groups.length - 1} style={{ padding: '0 4px', color: '#555', height: 18 }} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {groupHosts.map((h, hostIdx) => (
                <div key={h.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a', background: editingKey === h.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
                    <Button type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />} onClick={() => moveHostUp(h)} disabled={isEditing || hostIdx === 0} style={{ padding: '0 3px', color: '#555', height: 14, lineHeight: 1 }} />
                    <Button type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />} onClick={() => moveHostDown(h)} disabled={isEditing || hostIdx === groupHosts.length - 1} style={{ padding: '0 3px', color: '#555', height: 14, lineHeight: 1 }} />
                  </div>
                  <Text style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 110, flexShrink: 0 }}>{h.name}</Text>
                  <Text type="secondary" title={`${h.user ? h.user + '@' : ''}${h.hostname}${h.port ? ':' + h.port : ''}`} style={{ fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {h.user ? `${h.user}@` : ''}{h.hostname}{h.port ? `:${h.port}` : ''}
                  </Text>
                  {(() => {
                    const r = testResults[h.name]
                    if (!r) return null
                    const ok = r.runspec_ok
                    const sshOnly = r.connected && !r.runspec_ok
                    const color = ok ? 'success' : sshOnly ? 'warning' : 'error'
                    const label = ok ? `✓ ${r.runnable_count} runnable${r.runnable_count !== 1 ? 's' : ''}` : sshOnly ? '✓ SSH · runspec failed' : '✗ failed'
                    const detail = r.stderr || r.stdout || `exit ${r.exit_code}`
                    return (
                      <Tooltip title={<pre style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', maxWidth: 340 }}>{detail}</pre>} placement="left">
                        <Tag color={color} style={{ margin: 0, cursor: 'default', fontSize: 11 }}>{label}</Tag>
                      </Tooltip>
                    )
                  })()}
                  <Tooltip title="Test connection">
                    <Button type="text" size="small" icon={testingHost === h.name ? <LoadingOutlined /> : <ApiOutlined />} onClick={() => handleTest(h.name)} disabled={isEditing || testingHost !== null} style={{ padding: '0 4px', color: '#666' }} />
                  </Tooltip>
                  <Tooltip title="Copy SSH public key (authorize this machine)">
                    <Button type="text" size="small" icon={<KeyOutlined />} onClick={() => setCopyKeyHost(h.name)} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} />
                  </Tooltip>
                  <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(h)} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} />
                  <Popconfirm title={`Remove ${h.name}?`} onConfirm={() => remove(h.name)} okText="Remove" cancelText="Cancel" placement="left">
                    <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} danger />
                  </Popconfirm>
                </div>
              ))}
            </div>
          </div>
        ))
      })()}

      <CopyKeyModal
        open={copyKeyHost !== null}
        host={copyKeyHost ?? ''}
        onClose={() => setCopyKeyHost(null)}
        onDone={() => onHostsChanged?.()}
      />
    </div>
  )
}

// ── Filters tab ─────────────────────────────────────────────────────────────

interface FilterFormValues {
  name: string
  pipeline: string
  description?: string
  param_label?: string
}

// A filter is "parameterized" when its pipeline carries the {{arg}} placeholder —
// the Relay/Download dialogs then prompt for a value to substitute at run time.
export const FILTER_ARG_TOKEN = '{{arg}}'
export const isParameterized = (pipeline: string) => pipeline.includes(FILTER_ARG_TOKEN)

function filterToToml(filters: Filter[]): string {
  const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  return filters.map(f => {
    const lines = ['[[filter]]', `name = "${esc(f.name)}"`, `pipeline = "${esc(f.pipeline)}"`]
    if (f.description) lines.push(`description = "${esc(f.description)}"`)
    if (f.param_label) lines.push(`param_label = "${esc(f.param_label)}"`)
    return lines.join('\n')
  }).join('\n\n')
}

function FiltersTab() {
  const [filters, setFilters] = useState<Filter[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [form] = Form.useForm<FilterFormValues>()
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { bridge.get_filters().then(setFilters) }, [])

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!fileInputRef.current) return
    fileInputRef.current.value = ''
    if (!file) return
    const imported = await bridge.import_filters(await file.text())
    if (imported.length === 0) { message.warning('No new filters found in file'); return }
    setFilters(await bridge.get_filters())
    message.success(`Imported ${imported.length} filter${imported.length !== 1 ? 's' : ''}`)
  }

  const persist = async (next: Filter[]) => { setFilters(next); await bridge.save_filters(next) }
  const startAdd = () => { form.resetFields(); setEditingKey('') }
  const startEdit = (f: Filter) => { form.setFieldsValue({ ...f }); setEditingKey(f.name) }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  const save = async () => {
    const values = await form.validateFields()
    const next: Filter = {
      name: values.name.trim(),
      pipeline: values.pipeline.trim(),
      ...(values.description?.trim() ? { description: values.description.trim() } : {}),
      ...(values.param_label?.trim() ? { param_label: values.param_label.trim() } : {}),
    }
    if (editingKey === '') {
      if (filters.some(f => f.name === next.name)) { message.error('A filter with that name already exists'); return }
      await persist([...filters, next])
    } else {
      await persist(filters.map(f => f.name === editingKey ? next : f))
    }
    setEditingKey(null)
    form.resetFields()
  }

  const remove = async (name: string) => {
    await persist(filters.filter(f => f.name !== name))
    if (editingKey === name) setEditingKey(null)
  }

  const isEditing = editingKey !== null

  return (
    <div>
      <input ref={fileInputRef} type="file" accept=".toml" style={{ display: 'none' }} onChange={handleImport} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Saved to <code>runspec_filters.toml</code>. Pick one in the Relay or Transfer dialog to filter on the source host.
        </Text>
        <Space size={6}>
          <Button size="small" icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()} disabled={isEditing}>Import</Button>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => { downloadToml(filterToToml(filters), 'runspec_filters.toml'); message.success('Exported') }} disabled={isEditing || filters.length === 0}>Export</Button>
          <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add filter</Button>
        </Space>
      </div>

      <Modal
        open={editingKey !== null}
        title={editingKey === '' ? 'New filter' : `Edit — ${editingKey}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
            <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Required' }]}>
              <Input placeholder="id-lines" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
            </Form.Item>
            <Form.Item
              name="pipeline"
              label="Pipeline"
              rules={[{ required: true, message: 'Required' }]}
              help="Runs on the source host; the file arrives on stdin. Use {{arg}} for a value you'll enter at transfer time. e.g. grep -F -- {{arg}} | cut -d, -f1,4"
            >
              <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="grep -F -- {{arg}}" style={{ fontFamily: 'monospace' }} />
            </Form.Item>
            <Form.Item name="param_label" label="Value label" tooltip="Labels the input shown when this filter is selected (only used when the pipeline has {{arg}}).">
              <Input placeholder="e.g. identifier (optional)" />
            </Form.Item>
            <Form.Item name="description" label="Description">
              <Input placeholder="What this filter keeps (optional — shown in the picker / used by the agent)" />
            </Form.Item>
        </Form>
      </Modal>

      {filters.length === 0 && editingKey === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No filters yet. Add one to shrink large transfers at the source.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {filters.map(f => (
          <div key={f.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a', background: editingKey === f.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <Text style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 90, flexShrink: 0 }}>{f.name}</Text>
            {isParameterized(f.pipeline) && <Tag style={{ margin: 0, fontSize: 10 }}>arg</Tag>}
            <Text type="secondary" title={f.pipeline} style={{ fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {f.pipeline}
            </Text>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(f)} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} />
            <Popconfirm title={`Remove ${f.name}?`} onConfirm={() => remove(f.name)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} danger />
            </Popconfirm>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Self-Service tab (which runnables visitors may request in the room) ─────

// Designations whose runnable isn't currently discovered anywhere on the fleet are
// kept (a host being offline shouldn't silently drop them) but collected under this
// synthetic group key so they stay visible and removable.
const SS_ORPHAN_KEY = '__not_discovered__'

interface SsItem { name: string; description?: string; missing: boolean }

function SelfServiceTab() {
  const [entries, setEntries] = useState<SelfServiceEntry[]>([])
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [saving, setSaving] = useState(false)
  const [query, setQuery] = useState('')
  // null = not yet touched → show every group open. Once the operator collapses a
  // panel we honour their choice (search overrides both, see activeKeys below).
  const [openKeys, setOpenKeys] = useState<string[] | null>(null)

  const load = useCallback(() => {
    bridge.get_self_service().then(setEntries)
    bridge.get_runnables('all').then(setRunnables)
  }, [])
  useEffect(() => { load() }, [load])

  const persist = async (next: SelfServiceEntry[]) => {
    setEntries(next)
    setSaving(true)
    try { await bridge.save_self_service(next) } finally { setSaving(false) }
  }

  const isOn = (name: string) => entries.some(e => e.name === name)
  const toggle = async (name: string, on: boolean) => {
    if (on) { if (!isOn(name)) await persist([...entries, { name }]) }
    else { await persist(entries.filter(e => e.name !== name)) }
  }
  const toggleMany = async (names: string[], on: boolean) => {
    if (on) {
      const add = names.filter(n => !isOn(n)).map(n => ({ name: n }))
      if (add.length) await persist([...entries, ...add])
    } else {
      const drop = new Set(names)
      await persist(entries.filter(e => !drop.has(e.name)))
    }
  }

  // Dedupe discovered runnables by name (the same name can surface on several
  // hosts), keeping the first group/description seen — the self-service entry is
  // keyed by name only, so one toggle covers every host carrying it.
  const byName = new Map<string, { group: string; description?: string }>()
  for (const r of runnables) {
    if (!byName.has(r.name)) byName.set(r.name, { group: r.group || 'ungrouped', description: r.description })
  }

  // group key → items (the runnable's venv `group` is the "category" chip)
  const groups = new Map<string, SsItem[]>()
  for (const [name, info] of byName) {
    const arr = groups.get(info.group) ?? []
    arr.push({ name, description: info.description, missing: false })
    groups.set(info.group, arr)
  }
  const orphaned = entries.filter(e => !byName.has(e.name))
  if (orphaned.length) groups.set(SS_ORPHAN_KEY, orphaned.map(e => ({ name: e.name, missing: true })))

  // Real groups alphabetically, the "not discovered" bucket pinned last.
  const groupKeys = [...groups.keys()].filter(k => k !== SS_ORPHAN_KEY).sort((a, b) => a.localeCompare(b))
  if (groups.has(SS_ORPHAN_KEY)) groupKeys.push(SS_ORPHAN_KEY)

  const q = query.trim().toLowerCase()
  const hit = (it: SsItem, gk: string) =>
    !q || it.name.toLowerCase().includes(q) || gk.toLowerCase().includes(q) || (it.description?.toLowerCase().includes(q) ?? false)

  const totalOn = entries.length
  const totalDiscovered = byName.size

  const card = (it: SsItem) => {
    const on = isOn(it.name)
    return (
      <div key={it.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 9px', borderRadius: 6, border: `1px solid ${on ? '#3b5161' : '#2a2a2a'}`, background: on ? 'rgba(64, 130, 180, 0.10)' : 'transparent', minWidth: 0 }}>
        <Switch size="small" checked={on} disabled={saving} onChange={v => toggle(it.name, v)} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div title={it.name} style={{ fontFamily: 'monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.name}</div>
          {it.description && <div title={it.description} style={{ fontSize: 10, color: '#888', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.description}</div>}
        </div>
        {it.missing && <Tag color="orange" style={{ margin: 0, fontSize: 10 }}>missing</Tag>}
      </div>
    )
  }

  const panels = groupKeys.map(gk => {
    const all = (groups.get(gk) ?? []).slice().sort((a, b) => a.name.localeCompare(b.name))
    const shown = all.filter(it => hit(it, gk))
    const onCount = all.filter(it => isOn(it.name)).length
    const isOrphan = gk === SS_ORPHAN_KEY
    return {
      key: gk,
      label: (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Tag color={isOrphan ? 'orange' : 'geekblue'} style={{ margin: 0, fontFamily: 'monospace', fontSize: 11 }}>
            {isOrphan ? 'not discovered' : gk}
          </Tag>
          <Text type="secondary" style={{ fontSize: 11 }}>{onCount}/{all.length} on</Text>
          <span style={{ flex: 1 }} />
          {!isOrphan && (
            <span onClick={e => e.stopPropagation()}>
              <Tooltip title={onCount === all.length ? 'Disable all in this category' : 'Enable all in this category'}>
                <Switch size="small" checked={all.length > 0 && onCount === all.length} disabled={saving}
                  onChange={v => toggleMany(all.map(i => i.name), v)} />
              </Tooltip>
            </span>
          )}
        </div>
      ),
      children: (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 6 }}>
          {shown.map(card)}
        </div>
      ),
      _hasMatch: shown.length > 0,
    }
  }).filter(p => p._hasMatch)

  // When searching, force every matching panel open so results aren't hidden in a
  // collapsed category; otherwise honour the operator's manual open/closed state.
  const matchedKeys = panels.map(p => p.key)
  const activeKeys = q ? matchedKeys : (openKeys ?? groupKeys)

  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
        Saved to <code>runspec_self_service.toml</code> (synced). The enabled runnables are offered to visitors in the helpdesk room's self-service catalogue — everything else stays operator-only. A queue's optional <code>room_catalog</code> allowlist narrows this set per queue.
      </Text>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <Input
          allowClear
          size="small"
          prefix={<SearchOutlined />}
          placeholder="Search runnables or categories…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          style={{ maxWidth: 300 }}
        />
        <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{totalOn} enabled · {totalDiscovered} discovered</Text>
      </div>

      {totalDiscovered === 0 && orphaned.length === 0 && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No runnables discovered yet" />
      )}

      {panels.length === 0 && q && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`No runnables match "${query}"`} />
      )}

      {panels.length > 0 && (
        <Collapse
          size="small"
          activeKey={activeKeys}
          onChange={keys => { if (!q) setOpenKeys(Array.isArray(keys) ? keys : [keys]) }}
          items={panels.map(({ _hasMatch, ...rest }) => rest)}
        />
      )}
    </div>
  )
}

// ── Repos tab (git repos the agent may read + locally edit) ─────────────────

interface RepoFormValues { name: string; url: string; ref?: string; credential_id?: string; description?: string; path?: string }

function ReposTab() {
  const [repos, setRepos] = useState<Repo[]>([])
  const [creds, setCreds] = useState<Credential[]>([])
  const [engineMissing, setEngineMissing] = useState(false)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [syncing, setSyncing] = useState<string | null>(null)
  const [form] = Form.useForm<RepoFormValues>()

  const reload = useCallback(() => bridge.get_repos().then(setRepos), [])
  useEffect(() => {
    reload()
    // Refresh the credential dropdown live when the Credentials tab adds/edits one
    // (it dispatches this event) — otherwise a newly-added credential only appears
    // after a restart.
    const loadCreds = () => bridge.list_credentials().then(setCreds)
    loadCreds()
    bridge.get_sync_config().then(c => setEngineMissing(!c.engine))
    window.addEventListener('runspec:credentials-changed', loadCreds)
    return () => window.removeEventListener('runspec:credentials-changed', loadCreds)
  }, [reload])

  // Status fields (checked_out/commit) are read-only; the backend ignores them on
  // save, so we can send the list as-is.
  const persist = async (next: Repo[]) => { setRepos(next); await bridge.save_repos(next); await reload() }
  const startAdd = () => { form.resetFields(); setEditingKey('') }
  const startEdit = (r: Repo) => { form.setFieldsValue({ name: r.name, url: r.url, ref: r.ref, credential_id: r.credential_id, description: r.description, path: r.path }); setEditingKey(r.name) }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  const browsePath = async () => {
    const dir = await bridge.browse_folder()
    if (dir) form.setFieldValue('path', dir)
  }

  const save = async () => {
    const v = await form.validateFields()
    const next: Repo = {
      name: v.name.trim(),
      url: v.url.trim(),
      ref: (v.ref || '').trim() || 'main',
      ...(v.credential_id ? { credential_id: v.credential_id } : {}),
      ...(v.description?.trim() ? { description: v.description.trim() } : {}),
      ...(v.path?.trim() ? { path: v.path.trim() } : {}),
    }
    if (editingKey === '') {
      if (repos.some(r => r.name === next.name)) { message.error('A repo with that name already exists'); return }
      await persist([...repos, next])
    } else {
      await persist(repos.map(r => r.name === editingKey ? next : r))
    }
    setEditingKey(null); form.resetFields()
  }

  const remove = async (name: string) => {
    await bridge.delete_repo(name)
    await reload()
    if (editingKey === name) setEditingKey(null)
  }

  const sync = async (name: string) => {
    setSyncing(name)
    try {
      const res = await bridge.sync_repo(name)
      if (res.ok) message.success(`${res.action === 'updated' ? 'Updated' : 'Cloned'} ${name}${res.commit ? ` @ ${res.commit}` : ''}`)
      else message.error(`Sync failed: ${res.error}`)
    } finally { setSyncing(null); await reload() }
  }

  const isEditing = editingKey !== null
  const credLabel = (id?: string) => creds.find(c => c.id === id)?.label

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Repositories the agent may read and <strong>locally</strong> edit (the <code>repo_*</code> tools). This list is the allowlist — a repo not here is unreachable. Saved to <code>runspec_repos.toml</code>; rides Config Sync. Edits are local only (no commit/push).
        </Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add repo</Button>
      </div>

      {engineMissing && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="Git engine not installed"
          description={<span style={{ fontSize: 12 }}>Install the <code>git-sync</code> extra (<code>pip install runspec-console[git-sync]</code>) to clone/read repos.</span>} />
      )}

      <Modal
        open={editingKey !== null}
        title={editingKey === '' ? 'New repo' : `Edit — ${editingKey}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
          <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Required' }]}
            tooltip="A short identifier the agent uses to name this repo (e.g. 'payments').">
            <Input placeholder="payments" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
          </Form.Item>
          <Form.Item name="url" label="Repository URL" rules={[{ required: true, message: 'Required' }]}
            tooltip="HTTPS (https://…) or SSH (git@host:org/repo.git).">
            <Input placeholder="https://git.example.com/team/payments.git" autoComplete="off" />
          </Form.Item>
          <Form.Item name="ref" label="Branch / tag" tooltip="Branch or tag to check out. Defaults to main.">
            <Input placeholder="main" />
          </Form.Item>
          <Form.Item name="credential_id" label="Credential"
            tooltip="A Credentials-tab entry. HTTPS uses a token/userpass credential; SSH uses an ssh-key credential (else your console SSH key). Leave blank for a public repo.">
            <Select allowClear placeholder="None (public / anonymous)"
              options={creds.map(c => ({ value: c.id, label: `${c.label}${c.kind === 'ssh-key' ? ' (ssh-key)' : c.username ? ` (${c.username})` : ''}` }))} />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input placeholder="What this repo is (optional — shown to the agent)" />
          </Form.Item>
          <Form.Item name="path" label="Checkout location"
            tooltip="Where the repo is checked out on disk. Leave blank to use the default app-data location (recommended for read-only repos). Set it to your own dev folder for a repo you actively develop — that path is machine-local and never synced. Deleting the repo never removes a custom dev-folder checkout.">
            <Input placeholder="(default app-data location)" autoComplete="off"
              addonAfter={<a onClick={browsePath}>Browse…</a>} />
          </Form.Item>
        </Form>
      </Modal>

      {repos.length === 0 && editingKey === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No repos yet. Add one so the agent can read and edit its code.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {repos.map(r => (
          <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a', background: editingKey === r.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <Text style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 90, flexShrink: 0 }}>{r.name}</Text>
            <Tag style={{ margin: 0, fontSize: 10 }}>{r.ref}</Tag>
            {r.checked_out
              ? <Tag color="green" style={{ margin: 0, fontSize: 10 }} title={`Checked out at ${r.commit}${r.checkout_dir ? ` — ${r.checkout_dir}` : ''}`}>synced {r.commit}</Tag>
              : <Tag style={{ margin: 0, fontSize: 10 }}>not synced</Tag>}
            {r.path && <Tag color="blue" style={{ margin: 0, fontSize: 10 }} title={`Custom checkout: ${r.path}`}>custom</Tag>}
            <Text type="secondary" title={`${r.url}${credLabel(r.credential_id) ? ` — auth: ${credLabel(r.credential_id)}` : ''}`} style={{ fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.url}
            </Text>
            <Tooltip title="Sync now (clone / fetch)">
              <Button type="text" size="small" icon={syncing === r.name ? <LoadingOutlined /> : <ReloadOutlined />} onClick={() => sync(r.name)} disabled={isEditing || engineMissing || syncing !== null} style={{ padding: '0 4px', color: '#666' }} />
            </Tooltip>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(r)} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} />
            <Popconfirm title={`Remove ${r.name}?`} description={r.path ? 'Your custom dev-folder checkout is left untouched.' : 'The app-data checkout on disk is also deleted.'} onConfirm={() => remove(r.name)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} danger />
            </Popconfirm>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Credentials tab ─────────────────────────────────────────────────────────

const KIND_OPTIONS = [
  { value: 'userpass', label: 'Username + password' },
  { value: 'password', label: 'Password / passphrase' },
  { value: 'token', label: 'Token / API key' },
  { value: 'ssh-key', label: 'SSH key (private + public key files)' },
]

interface CredFormValues {
  label: string
  kind: Credential['kind']
  username?: string
  username_arg?: string
  secret_arg?: string
  private_key?: string
  public_key?: string
  scope_hosts?: string[]
  scope_runnables?: string[]
  secret?: string
}

function CredentialsTab() {
  const [creds, setCreds] = useState<Credential[]>([])
  const [backend, setBackend] = useState<{ available: boolean; backend: string } | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)  // '' = new, id = edit
  const [form] = Form.useForm<CredFormValues>()

  const reload = useCallback(() => { bridge.list_credentials().then(setCreds) }, [])
  useEffect(() => { reload() }, [reload])
  useEffect(() => { bridge.credentials_backend().then(setBackend) }, [])

  const startAdd = () => { form.resetFields(); form.setFieldValue('kind', 'userpass'); setEditingId('') }
  const startEdit = (c: Credential) => {
    form.setFieldsValue({
      label: c.label, kind: c.kind, username: c.username,
      username_arg: c.username_arg, secret_arg: c.secret_arg,
      private_key: c.private_key, public_key: c.public_key,
      scope_hosts: c.scope_hosts ?? [], scope_runnables: c.scope_runnables ?? [],
      secret: '',  // write-only: blank means "keep the stored value"
    })
    setEditingId(c.id)
  }
  const cancel = () => { setEditingId(null); form.resetFields() }

  const pickKey = async (field: 'private_key' | 'public_key') => {
    const path = await bridge.browse_key_file()
    if (path) form.setFieldValue(field, path)
  }

  const save = async () => {
    const v = await form.validateFields()
    const entry: Credential = {
      id: editingId || '',
      label: v.label.trim(),
      kind: v.kind,
      username: v.username?.trim() || undefined,
      username_arg: v.username_arg?.trim() || undefined,
      secret_arg: v.secret_arg?.trim() || undefined,
      private_key: v.private_key?.trim() || undefined,
      public_key: v.public_key?.trim() || undefined,
      scope_hosts: v.scope_hosts ?? [],
      scope_runnables: v.scope_runnables ?? [],
      ...(v.secret ? { secret: v.secret } : {}),  // only send when set
    }
    const res = await bridge.save_credential(entry)
    if (res.error) { message.error(res.error); return }
    setEditingId(null); form.resetFields(); reload()
    // Let other tabs (e.g. Config Sync's credential dropdown) refresh live.
    window.dispatchEvent(new CustomEvent('runspec:credentials-changed'))
    message.success('Credential saved')
  }

  const remove = async (id: string) => {
    await bridge.delete_credential(id)
    if (editingId === id) setEditingId(null)
    reload()
    window.dispatchEvent(new CustomEvent('runspec:credentials-changed'))
  }

  const isEditing = editingId !== null
  const editingCred = editingId ? creds.find(c => c.id === editingId) : undefined
  const kind = Form.useWatch('kind', form)
  const label = Form.useWatch('label', form)
  const isSshKey = kind === 'ssh-key'
  // The plain env vars this credential will set when applied to a run (Forms'
  // credential section / agent). Derived from the label by the same strict
  // convention the run path uses.
  const envNames = !isSshKey ? credEnvNames(label ?? '', kind ?? 'userpass') : []

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Secrets are stored in the OS keychain{backend?.backend ? <> (<code>{backend.backend}</code>)</> : null}; only metadata is saved to <code>runspec_credentials.toml</code>.
        </Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add</Button>
      </div>

      {backend && !backend.available && (
        <Text type="warning" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
          No OS keychain is available, so secret values can't be stored. Install the <code>credentials</code> extra (<code>pip install runspec-console[credentials]</code>) on a desktop session. Metadata-only entries still save.
        </Text>
      )}

      <Modal
        open={editingId !== null}
        title={editingId === '' ? 'New credential' : `Edit — ${editingCred?.label ?? editingId}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
            <Form.Item name="label" label="Label" rules={[{ required: true, message: 'Required' }]}>
              <Input placeholder="Service account (prod)" />
            </Form.Item>
            <Form.Item name="kind" label="Kind">
              <Select options={KIND_OPTIONS} />
            </Form.Item>
            {isSshKey ? (
              <>
                <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>
                  An SSH-key credential authenticates a Config Sync SSH repo with its own deploy key (use it in Settings → Config Sync). The key files stay on disk — only the paths are saved.
                </Text>
                <Form.Item name="private_key" label="Private key path" rules={[{ required: true, message: 'Required' }]} tooltip="Path to the private key file (e.g. an ed25519 deploy key). Passed to the SSH client as the identity file.">
                  <Input placeholder="~/.ssh/runspec_ed25519" style={{ fontFamily: 'monospace' }} autoComplete="off"
                    addonAfter={<a onClick={() => pickKey('private_key')}>Browse</a>} />
                </Form.Item>
                <Form.Item name="public_key" label="Public key path" tooltip="Optional — the matching .pub file. Informational; not required to connect.">
                  <Input placeholder="~/.ssh/runspec_ed25519.pub" style={{ fontFamily: 'monospace' }} autoComplete="off"
                    addonAfter={<a onClick={() => pickKey('public_key')}>Browse</a>} />
                </Form.Item>
                <Form.Item name="username" label="SSH user" tooltip="The SSH login user — defaults to git for most hosts (GitHub, GitLab).">
                  <Input placeholder="git" autoComplete="off" />
                </Form.Item>
                <Form.Item name="secret" label="Key passphrase" tooltip={editingId !== '' ? 'Leave blank to keep the stored passphrase. Leave empty for an unencrypted key.' : 'Only if the private key is encrypted; stored in the OS keychain.'}>
                  <Input.Password placeholder={editingId !== '' && editingCred?.has_secret ? '•••••••• (unchanged)' : 'optional — only for an encrypted key'} autoComplete="new-password" disabled={!backend?.available} />
                </Form.Item>
              </>
            ) : (
              <>
                <Form.Item name="username" label="Username" tooltip="The non-secret identity (for username + password kinds).">
                  <Input placeholder="svc_runner" autoComplete="off" />
                </Form.Item>
                <Form.Item name="secret" label="Secret" tooltip={editingId !== '' ? 'Leave blank to keep the stored secret.' : undefined}>
                  <Input.Password placeholder={editingId !== '' && editingCred?.has_secret ? '•••••••• (unchanged)' : 'password / token'} autoComplete="new-password" disabled={!backend?.available} />
                </Form.Item>
                <div style={{ marginBottom: 12, padding: '8px 10px', borderRadius: 6, background: 'rgba(128,128,128,0.08)' }}>
                  <Text type="secondary" style={{ fontSize: 11, display: 'block' }}>
                    The <b>label</b> drives the env var prefix. When this credential is passed to a run it sets:
                  </Text>
                  <div style={{ marginTop: 4 }}>
                    {envNames.length
                      ? envNames.map((n, i) => <span key={n}>{i > 0 && ' '}<code style={{ fontSize: 11 }}>{n}</code></span>)
                      : <Text type="secondary" style={{ fontSize: 11 }}>— (give the credential a label)</Text>}
                  </div>
                  <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 4 }}>
                    Name it to match what the runnable reads from the environment.
                  </Text>
                </div>
                <Divider style={{ margin: '4px 0 12px' }} />
                <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>
                  Binding (agent only) — which runnable arguments these values fill for agent-driven runs, and where the credential may be applied (glob patterns; blank = any). The Forms path uses the env vars above, not these.
                </Text>
                <Form.Item name="username_arg" label="Username → argument" tooltip="Runnable arg name that receives the username (passed normally).">
                  <Input placeholder="service-username" style={{ fontFamily: 'monospace' }} autoComplete="off" />
                </Form.Item>
                <Form.Item name="secret_arg" label="Secret → argument" tooltip="Runnable arg name that receives the secret (passed via env, never argv).">
                  <Input placeholder="user-password" style={{ fontFamily: 'monospace' }} autoComplete="off" />
                </Form.Item>
                <Form.Item name="scope_hosts" label="Host scope">
                  <Select mode="tags" placeholder="prod-*  (Enter to add)" tokenSeparators={[',', ' ']} open={false} suffixIcon={null} />
                </Form.Item>
                <Form.Item name="scope_runnables" label="Runnable scope">
                  <Select mode="tags" placeholder="deploy, db-*  (Enter to add)" tokenSeparators={[',', ' ']} open={false} suffixIcon={null} />
                </Form.Item>
              </>
            )}
        </Form>
      </Modal>

      {creds.length === 0 && editingId === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No credentials yet. Add one to let a runnable or relay use a stored identity.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {creds.map(c => (
          <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a', background: editingId === c.id ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <KeyOutlined style={{ color: '#666', fontSize: 12, flexShrink: 0 }} />
            <Text style={{ fontSize: 12, minWidth: 110, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.label}</Text>
            <Tag style={{ margin: 0, fontSize: 10 }}>{c.kind}</Tag>
            {c.has_secret
              ? <Tag color="green" style={{ margin: 0, fontSize: 10 }}>secret set</Tag>
              : <Tag style={{ margin: 0, fontSize: 10 }}>no secret</Tag>}
            <Text type="secondary" title={c.username} style={{ fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {c.username}
            </Text>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(c)} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} />
            <Popconfirm title={`Remove ${c.label}?`} onConfirm={() => remove(c.id)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: '#666' }} danger />
            </Popconfirm>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Mailbox tab (the monitored mailbox; its own tab so Triggers has room) ────

function MailboxesTab() {
  const [form] = Form.useForm<Mailbox>()
  // Whether a [mailbox] is configured at all (the master switch). Off → save null.
  const [enabled, setEnabled] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [configDir, setConfigDir] = useState<string>('')
  // Additional watch-only mailboxes (the [[watch]] extras) — e.g. the operator's
  // own Inbox alongside the shared mailbox above. No room/verify-from role.
  const [watches, setWatches] = useState<Watch[]>([])

  // Live address — drives the shared-mailbox warning (a non-default address).
  const address = Form.useWatch('address', form)

  useEffect(() => {
    bridge.get_mailbox().then(mb => {
      setEnabled(mb !== null)
      form.setFieldsValue({
        address: mb?.address ?? '',
        folder: mb?.folder ?? '',
        label: mb?.label ?? '',
        once_per_thread: !!mb?.once_per_thread,
      })
    }).catch(console.error)
  }, [form])
  useEffect(() => { bridge.get_watches().then(setWatches).catch(console.error) }, [])
  useEffect(() => { bridge.config_dir().then(setConfigDir) }, [])

  const setWatch = (i: number, patch: Partial<Watch>) =>
    setWatches(ws => ws.map((w, j) => (j === i ? { ...w, ...patch } : w)))
  const addWatch = () => setWatches(ws => [...ws, { address: '', folder: '', label: '' }])
  const removeWatch = (i: number) => setWatches(ws => ws.filter((_, j) => j !== i))

  const save = async () => {
    setSaving(true)
    try {
      if (!enabled) {
        await bridge.save_mailbox(null)
      } else {
        const v = form.getFieldsValue()
        await bridge.save_mailbox({
          address: (v.address ?? '').trim(),
          folder: (v.folder ?? '').trim() || 'Inbox',
          label: (v.label ?? '').trim(),
          once_per_thread: !!v.once_per_thread,
        })
      }
      await bridge.save_watches(watches.map(w => ({
        address: (w.address ?? '').trim(),
        folder: (w.folder ?? '').trim() || 'Inbox',
        label: (w.label ?? '').trim(),
      })))
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
        Monitor one Outlook inbox for the <code>new_mail</code> trigger source. Trigger
        rules &mdash; what to do with matched mail &mdash; live in <code>runspec_triggers.toml</code>.
      </Text>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <Switch size="small" checked={enabled} onChange={setEnabled} />
        <MailOutlined style={{ color: '#666' }} />
        <span style={{ fontSize: 13 }}>Monitor a mailbox</span>
      </div>

      {enabled && (
        <Form form={form} layout="vertical" size="small" style={{ marginTop: 4 }}>
          <Form.Item
            name="address"
            label="Mailbox email address"
            help="The account or shared mailbox to monitor. Leave blank for your own default account."
          >
            <Input placeholder="helpdesk@corp.com (blank = my mailbox)" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item
            name="folder"
            label="Folder (optional)"
            help={'A folder path under that mailbox, /-separated. Blank or "Inbox" monitors the Inbox.'}
          >
            <Input placeholder="Inbox, or e.g. Tickets/Open" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
          <Form.Item name="label" label="Label (optional)" help="A display name for this inbox.">
            <Input placeholder="e.g. Helpdesk" />
          </Form.Item>
          <Form.Item name="once_per_thread" valuePropName="checked"
            label="Suppress replies by default (once per thread)"
            help="Default for this mailbox: triggers fire on the first message in a conversation and ignore later replies. Each trigger can override this.">
            {/* Switch must be the SINGLE child of Form.Item — Ant only injects the
                checked/onChange binding into a lone child, so a sibling <span> here
                left the switch unbound and its value was never saved or restored. */}
            <Switch size="small" />
          </Form.Item>
          {!!(address ?? '').trim() && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 10 }}
              message="Shared mailbox: no cross-console coordination"
              description={
                <span style={{ fontSize: 12 }}>
                  If another console monitors this same mailbox + folder, both react to
                  every new mail (duplicate replies / runs). Monitor one folder from one
                  console, or let Outlook rules sort each operator's mail into a personal
                  folder.
                </span>
              }
            />
          )}
        </Form>
      )}

      <Divider style={{ margin: '16px 0 12px' }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <MailOutlined style={{ color: '#666' }} />
        <span style={{ fontSize: 13 }}>Also watch (optional)</span>
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        Extra mailboxes/folders to monitor for <code>new_mail</code> alongside the one
        above &mdash; e.g. your own Inbox next to a shared helpdesk mailbox. These feed
        triggers only; the mailbox above stays the room / sign-up identity.
      </Text>
      {watches.map((w, i) => (
        <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'center' }}>
          <Input
            size="small"
            placeholder="address (blank = my default account)"
            value={w.address}
            onChange={e => setWatch(i, { address: e.target.value })}
            style={{ fontFamily: 'monospace', flex: 2 }}
          />
          <Input
            size="small"
            placeholder='folder (blank = "Inbox")'
            value={w.folder}
            onChange={e => setWatch(i, { folder: e.target.value })}
            style={{ fontFamily: 'monospace', flex: 2 }}
          />
          <Input
            size="small"
            placeholder="label"
            value={w.label}
            onChange={e => setWatch(i, { label: e.target.value })}
            style={{ flex: 1 }}
          />
          <Button size="small" type="text" danger onClick={() => removeWatch(i)}>Remove</Button>
        </div>
      ))}
      <Button size="small" onClick={addWatch} style={{ marginTop: 2 }}>+ Add mailbox</Button>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <Button type="primary" size="small" loading={saving} onClick={save}>Save</Button>
        {saved && <Text type="success" style={{ fontSize: 12 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
        Saved to <code>config.toml</code> in <code>{configDir || '%APPDATA%/runspec-console'}</code>. Changes restart the trigger engine.
      </Text>
    </div>
  )
}

// ── Teams tab (customer departments, managed in the helpdesk room) ──────────

function TeamsTab() {
  const [teams, setTeams] = useState<Team[]>([])
  const [name, setName] = useState('')
  const [err, setErr] = useState('')
  const [notConnected, setNotConnected] = useState(false)
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    const onTeams = (e: Event) => { setTeams(((e as CustomEvent).detail as TeamsEvent).teams ?? []); setNotConnected(false) }
    window.addEventListener('runspec:teams', onTeams)
    bridge.list_teams().then(r => { if (!r.ok) { setNotConnected(true); setErr(r.error || '') } })
    return () => window.removeEventListener('runspec:teams', onTeams)
  }, [])

  const add = async () => {
    const n = name.trim()
    if (!n) return
    setAdding(true)
    try {
      const r = await bridge.add_team(n)
      if (!r.ok) setErr(r.error || 'Could not add the team.')
      else { setName(''); setErr('') }
    } finally { setAdding(false) }
  }
  const remove = async (slug: string) => {
    const r = await bridge.remove_team(slug)
    if (!r.ok && r.error) setErr(r.error)
  }

  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
        The customer teams a visitor picks at sign-up — stored in the helpdesk room and shared across operators. <code>Default</code> is always present.
      </Text>

      {notConnected && (
        <Alert type="warning" showIcon style={{ marginBottom: 10 }}
          message="Not connected to a helpdesk room"
          description={<span style={{ fontSize: 12 }}>Connect a host with a room port + a matching <code>[mailbox]</code> (Settings → Jump Hosts + Triggers) to manage teams.</span>} />
      )}

      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <Input size="small" value={name} onChange={e => setName(e.target.value)} onPressEnter={add}
          placeholder="New team name (e.g. HR)" disabled={notConnected} />
        <Button size="small" type="primary" icon={<PlusOutlined />} onClick={add} loading={adding} disabled={notConnected || !name.trim()}>Add</Button>
      </div>

      {err && <Alert type="error" showIcon style={{ marginBottom: 10 }} message={err} />}

      {teams.length === 0 && !notConnected ? (
        <Text type="secondary" style={{ fontSize: 12 }}>No teams yet — add one above.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {teams.map(t => (
            <div key={t.slug} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
              <TeamOutlined style={{ color: '#666', flexShrink: 0 }} />
              <Text style={{ fontSize: 13, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</Text>
              <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11, flexShrink: 0 }}>{t.slug}</Text>
              {t.slug === 'default' ? (
                <Tag style={{ margin: 0, fontSize: 10 }}>protected</Tag>
              ) : (
                <Popconfirm title={`Remove team ${t.name}?`} onConfirm={() => remove(t.slug)} okText="Remove" cancelText="Cancel" placement="left">
                  <Button type="text" size="small" icon={<DeleteOutlined />} danger style={{ padding: '0 4px', color: '#666' }} />
                </Popconfirm>
              )}
            </div>
          ))}
        </div>
      )}
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
        Teams live in the helpdesk room database — not a local file. Changes are shared with every operator on the same room.
      </Text>
    </div>
  )
}

// ── Config Sync tab (pull config files from a git repo, by tag) ─────────────────

interface SyncFormValues {
  url: string
  ref: string
  credential_id?: string
  layers?: string[]
  ca_bundle?: string
  tls_verify?: boolean
  files: string[]
  preserve?: string[]
  backups_keep?: number
}

function ConfigSyncTab() {
  const [form] = Form.useForm<SyncFormValues>()
  const [cfg, setCfg] = useState<SyncConfig | null>(null)
  const [creds, setCreds] = useState<Credential[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [pulling, setPulling] = useState(false)
  const [result, setResult] = useState<SyncPullResult | null>(null)
  const [docsDir, setDocsDir] = useState('')
  const [docsDefault, setDocsDefault] = useState('')
  const [docsBusy, setDocsBusy] = useState(false)
  const [docsResult, setDocsResult] = useState<{ ok: boolean; dir?: string; written?: string[]; error?: string } | null>(null)
  const [seed, setSeed] = useState<SeedStatus | null>(null)
  const [seedBusy, setSeedBusy] = useState(false)
  const [seedResult, setSeedResult] = useState<SeedApplyResult | null>(null)

  const load = useCallback(() => {
    bridge.get_sync_config().then(c => {
      setCfg(c)
      form.setFieldsValue({
        url: c.url, ref: c.ref, credential_id: c.credential_id || undefined,
        // One unified "folders" field: prefer layers; migrate an old single subpath.
        layers: c.layers?.length ? c.layers : (c.subpath ? [c.subpath] : []),
        ca_bundle: c.ca_bundle, tls_verify: c.tls_verify !== false,
        files: c.files?.length ? c.files : c.files_default,
        preserve: c.preserve ?? [],
        backups_keep: c.backups_keep ?? 5,
      })
    })
  }, [form])
  useEffect(() => { load() }, [load])
  // Load the credential dropdown, and refresh it when a credential is added or
  // removed on the Credentials tab — so a new deploy token shows up without a
  // console restart (the drawer keeps tab panes mounted).
  useEffect(() => {
    const reloadCreds = () => { bridge.list_credentials().then(setCreds) }
    reloadCreds()
    window.addEventListener('runspec:credentials-changed', reloadCreds)
    return () => window.removeEventListener('runspec:credentials-changed', reloadCreds)
  }, [])

  const persist = async (): Promise<boolean> => {
    const v = await form.validateFields()
    await bridge.save_sync_config({
      url: v.url.trim(), ref: v.ref.trim(),
      credential_id: v.credential_id || '',
      // The unified "folders" field is the single source of truth; clear the
      // legacy subpath so there's no stale second value behind it.
      subpath: '',
      layers: (v.layers ?? []).map(s => s.trim()).filter(Boolean),
      ca_bundle: v.ca_bundle?.trim() || '', tls_verify: v.tls_verify !== false,
      files: v.files ?? [], preserve: v.preserve ?? [],
      backups_keep: v.backups_keep ?? 5,
    })
    return true
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      if (!(await persist())) return
      setSaved(true); setTimeout(() => setSaved(false), 2000)
      load()
    } catch { /* validation surfaced inline */ } finally { setSaving(false) }
  }

  const loadTags = async () => {
    try {
      if (!(await persist())) return  // tags need the saved URL + credential
    } catch { return }
    const r = await bridge.list_sync_tags()
    if (r.error) { message.error(r.error); setTags([]) }
    else { setTags(r.tags ?? []); if (!r.tags?.length) message.info('No tags found in the repo.') }
  }

  const pull = async () => {
    setPulling(true); setResult(null)
    try {
      if (!(await persist())) return
      const r = await bridge.sync_pull()
      setResult(r)
      if (r.ok) message.success(`Pulled ${r.written?.length ?? 0} file(s) at ${r.ref}`)
      else message.error(r.error || 'Pull failed')
      load()
    } finally { setPulling(false) }
  }

  // Package config seed: read-only status of what the deployment package bundles.
  const loadSeed = useCallback(() => { bridge.seed_status().then(setSeed) }, [])
  useEffect(() => { loadSeed() }, [loadSeed])

  const reapplySeed = async () => {
    setSeedBusy(true); setSeedResult(null)
    try {
      const r = await bridge.seed_apply(true)
      setSeedResult(r)
      if (!r.ok) message.error(r.error || 'Seed apply failed')
      else if (r.applied) message.success(`Applied seed (${r.written?.length ?? 0} file(s))`)
      else message.info('Seed already up to date')
      loadSeed()
    } finally { setSeedBusy(false) }
  }

  // Runbook docs: load the configured output dir + the default it falls back to.
  useEffect(() => {
    bridge.get_docs_config().then(d => { setDocsDir(d.dir || ''); setDocsDefault(d.default) })
  }, [])

  const generateDocs = async () => {
    setDocsBusy(true); setDocsResult(null)
    try {
      await bridge.save_docs_config(docsDir.trim())
      const r = await bridge.generate_docs(docsDir.trim())
      setDocsResult(r)
      if (r.ok) message.success(`Wrote ${r.written?.length ?? 0} doc file(s)`)
      else message.error(r.error || 'Doc generation failed')
    } finally { setDocsBusy(false) }
  }

  const engineMissing = cfg !== null && !cfg.engine
  const fileOptions = (cfg?.files_default ?? []).map(f => ({ value: f, label: f }))

  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
        Pull this console's config files from a git repository at a tag, so a fresh install can be fully configured from one source. Authentication uses a saved credential (Credentials tab). A pull is <strong>non-destructive</strong>: it adds and updates from the repo but keeps your own hosts/triggers/filters/credentials and the keys you protect below (each file is backed up first).
      </Text>

      {engineMissing && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="Git engine not installed"
          description={<span style={{ fontSize: 12 }}>Install the <code>git-sync</code> extra (<code>pip install runspec-console[git-sync]</code>) to enable pulling.</span>} />
      )}

      <Form form={form} layout="vertical" size="small">
        <Form.Item name="url" label="Repository URL" rules={[{ required: true, message: 'Required' }]}
          tooltip="HTTPS (https://…) or SSH (git@host:org/repo.git). HTTPS uses the credential's token; SSH uses an SSH-key credential's deploy key (or your console SSH key when none is selected).">
          <Input placeholder="https://github.com/acme/runspec-config.git" autoComplete="off" />
        </Form.Item>
        <Form.Item name="ref" label="Tag / ref" rules={[{ required: true, message: 'Required' }]}
          tooltip="A tag (recommended, for versioned rollouts) or a branch name.">
          <AutoComplete
            options={tags.map(t => ({ value: t }))}
            onFocus={() => { if (!tags.length) loadTags() }}
            placeholder="v1.4.0"
          >
            <Input addonAfter={<a onClick={loadTags}>refresh tags</a>} />
          </AutoComplete>
        </Form.Item>
        <Form.Item name="credential_id" label="Credential"
          tooltip="A Credentials-tab entry. For HTTPS use a token/userpass credential (username + token). For SSH use an ssh-key credential (a per-repo deploy key); without one, SSH falls back to your console SSH key.">
          <Select allowClear placeholder="None (public / anonymous)"
            options={creds.map(c => ({ value: c.id, label: `${c.label}${c.kind === 'ssh-key' ? ' (ssh-key)' : c.username ? ` (${c.username})` : ''}` }))} />
        </Form.Item>
        <Form.Item name="files" label="Files to pull"
          tooltip="Files missing from the repo at that ref are skipped, not an error.">
          <Select mode="multiple" options={fileOptions} placeholder="config files to sync" />
        </Form.Item>
        <Form.Item
          name="preserve"
          label="Preserve (keep local on pull)"
          tooltip={'Keys whose local value a pull must never overwrite. Format: "<file>:<dotted.path>", with * matching any host/entry. e.g. config.toml:mailbox.folder or runspec_hosts.toml:*.room_user'}
          help={cfg?.preserve_default?.length ? <span style={{ fontSize: 11 }}>Always protected: {cfg.preserve_default.map(p => <code key={p} style={{ marginRight: 6 }}>{p}</code>)}</span> : undefined}
        >
          <Select mode="tags" tokenSeparators={[',', ' ']} open={false} suffixIcon={null}
            placeholder="config.toml:mailbox.folder  (Enter to add)" />
        </Form.Item>
        <Form.Item name="layers" label="Folder(s) in the repo (optional)"
          tooltip="Where in the repo the config files live. Leave blank to read the repo root. Add one folder to read just that folder. Add several to combine them in order — a shared base first, your team's folder last — so common config is shared and your team overlays its own on top (later folders win; your local entries are always kept)."
          help={<span style={{ fontSize: 11 }}>Blank = repo root. One folder, e.g. <code>teams/emea</code> = just that folder. Several, e.g. <code>_base</code> then <code>teams/emea</code> = shared base + your team on top (order = how they stack).</span>}>
          <Select mode="tags" placeholder="blank = repo root  ·  or e.g. _base then teams/emea" tokenSeparators={[',']} open={false} suffixIcon={null} style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        <Form.Item
          name="ca_bundle"
          label="CA bundle (optional)"
          tooltip="Path to a PEM file for verifying the server's TLS certificate. Needed for an internal CA or a TLS-inspecting corporate proxy — the pull uses certifi by default, not the OS/git cert store. Leave blank for public hosts. (HTTPS also reuses the SSH-tab proxy, if set.)"
          help={<span style={{ fontSize: 11 }}>For a private GitLab / corporate proxy that gives an <code>SSL CERTIFICATE_VERIFY_FAILED</code> error — point this at your internal root CA <code>.pem</code>.</span>}
        >
          <Input placeholder="~/certs/corp-root-ca.pem" style={{ fontFamily: 'monospace' }} autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="tls_verify"
          label="Verify TLS"
          valuePropName="checked"
          tooltip="Off skips certificate verification entirely (like git's http.sslVerify=false). Insecure — only on a trusted internal network. Prefer a CA bundle above."
          help={<span style={{ fontSize: 11 }}>Turn off only if you can't supply a CA bundle (e.g. your <code>git config</code> already sets <code>http.sslVerify false</code>).</span>}
        >
          <Switch size="small" />
        </Form.Item>
        <Form.Item
          name="backups_keep"
          label="Backups to keep per file"
          tooltip="Before a pull changes a file it copies the old one to <name>.bak-<timestamp>. This caps how many of those are kept per file — older ones are pruned after each pull. A pull that changes nothing makes no backup. 0 = keep all (never prune)."
          help={<span style={{ fontSize: 11 }}>Default 5. Set <code>0</code> to keep every backup (they'll accumulate in the config folder).</span>}
        >
          <InputNumber min={0} step={1} style={{ width: '100%' }} placeholder="5" />
        </Form.Item>

        <Space>
          <Button size="small" onClick={handleSave} loading={saving}>{saved ? 'Saved' : 'Save settings'}</Button>
          <Popconfirm
            title="Pull config from the repo?"
            description={<span style={{ fontSize: 12 }}>Merges config from the chosen tag (adds/updates, keeps your own).<br />Each file is backed up first.</span>}
            onConfirm={pull} okText="Pull" cancelText="Cancel" placement="topLeft">
            <Button size="small" type="primary" icon={<DownloadOutlined />} loading={pulling} disabled={engineMissing}>Pull now</Button>
          </Popconfirm>
        </Space>
      </Form>

      {cfg?.last_synced && (
        <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 12 }}>
          Last synced <code>{cfg.last_ref}</code> ({cfg.last_commit?.slice(0, 8)}) at {cfg.last_synced}
        </Text>
      )}

      {result && result.ok && (
        <Alert type="success" showIcon style={{ marginTop: 12 }}
          message={`Pulled at ${result.ref}`}
          description={<span style={{ fontSize: 12 }}>
            Wrote: {result.written?.length ? <code>{result.written.join(', ')}</code> : 'nothing'}.
            {result.skipped?.length ? <> Skipped (not in repo): <code>{result.skipped.join(', ')}</code>.</> : null}
            {result.backups?.length ? <> Backed up {result.backups.length} file(s).</> : null}
            {result.pruned?.length ? <> Pruned {result.pruned.length} old backup(s).</> : null}
            {' '}Hosts and triggers reloaded — no restart needed.
          </span>} />
      )}
      {result && !result.ok && (
        <Alert type="error" showIcon style={{ marginTop: 12 }} message="Pull failed" description={<span style={{ fontSize: 12 }}>{result.error}</span>} />
      )}

      <Divider style={{ margin: '18px 0 12px' }} />
      <Text strong style={{ fontSize: 13, display: 'block' }}>Config seed</Text>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', margin: '4px 0 10px' }}>
        Baseline config <strong>bundled inside this install's package</strong> — applied automatically on launch when the package version changes, so a fresh or air-gapped machine is configured with no repo or index reachable. It's the lowest layer: a git pull above and your own edits always win (except the keys you protect above).
      </Text>
      {seed?.available ? (
        <>
          <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
            Bundled: {Object.entries(seed.stamp).map(([d, v]) => <code key={d} style={{ marginRight: 6 }}>{d} v{v}</code>)}
          </Text>
          {seed.last_applied && (
            <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
              Last applied at {seed.last_applied}
            </Text>
          )}
          {seed.pending && (
            <Alert type="info" showIcon style={{ marginTop: 8 }}
              message="A newer seed is bundled"
              description={<span style={{ fontSize: 12 }}>It applies automatically on the next launch — or re-apply it now.</span>} />
          )}
          <div style={{ marginTop: 10 }}>
            <Popconfirm
              title="Re-apply the bundled config seed?"
              description={<span style={{ fontSize: 12 }}>Re-asserts the package's baseline over local config (your own entries and protected keys are kept).<br />Each changed file is backed up first.</span>}
              onConfirm={reapplySeed} okText="Re-apply" cancelText="Cancel" placement="topLeft">
              <Button size="small" loading={seedBusy}>Re-apply seed</Button>
            </Popconfirm>
          </div>
          {seedResult && seedResult.ok && seedResult.applied && (
            <Alert type="success" showIcon style={{ marginTop: 10 }}
              message="Seed applied"
              description={<span style={{ fontSize: 12 }}>
                Wrote: {seedResult.written?.length ? <code>{seedResult.written.join(', ')}</code> : 'nothing'}.
                {seedResult.backups?.length ? <> Backed up {seedResult.backups.length} file(s).</> : null}
                {' '}Hosts and triggers reloaded — no restart needed.
              </span>} />
          )}
          {seedResult && seedResult.ok && !seedResult.applied && (
            <Alert type="info" showIcon style={{ marginTop: 10 }} message="Seed already up to date" description={<span style={{ fontSize: 12 }}>Nothing changed.</span>} />
          )}
          {seedResult && !seedResult.ok && (
            <Alert type="error" showIcon style={{ marginTop: 10 }} message="Seed apply failed" description={<span style={{ fontSize: 12 }}>{seedResult.error}</span>} />
          )}
        </>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>No config seed bundled in this install.</Text>
      )}

      <Divider style={{ margin: '18px 0 12px' }} />
      <Text strong style={{ fontSize: 13, display: 'block' }}>Runbook docs</Text>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', margin: '4px 0 10px' }}>
        Generate a plain-markdown <strong>wiki</strong> from your runbooks + fleet — grouped by tag, one page per runbook, cross-linked to the runnables and hosts each names. It's for humans to follow during an outage (when the console may be down). Output is deterministic, so re-running only changes what your runbooks changed — point it at a local clone of the config repo and commit the docs back for the team.
      </Text>
      <Space.Compact style={{ width: '100%' }}>
        <Input size="small" value={docsDir} onChange={e => setDocsDir(e.target.value)}
          placeholder={docsDefault ? `${docsDefault}  (default)` : 'output directory'}
          style={{ fontFamily: 'monospace' }} autoComplete="off" />
        <Button size="small" type="primary" onClick={generateDocs} loading={docsBusy}>Generate</Button>
      </Space.Compact>
      {docsResult && docsResult.ok && (
        <Alert type="success" showIcon style={{ marginTop: 10 }}
          message={`Wrote ${docsResult.written?.length ?? 0} file(s)`}
          description={<span style={{ fontSize: 12 }}>Into <code>{docsResult.dir}</code>. Commit them from your repo clone to share with the team.</span>} />
      )}
      {docsResult && !docsResult.ok && (
        <Alert type="error" showIcon style={{ marginTop: 10 }} message="Doc generation failed" description={<span style={{ fontSize: 12 }}>{docsResult.error}</span>} />
      )}
    </div>
  )
}

// ── Runbooks ────────────────────────────────────────────────────────────────────

// One editor row for a runbook parameter. Stored as a runspec `[runbook.args]` entry —
// `host` is the console-only `ui:"host"` pseudo-type (→ a fleet-host dropdown in the
// run modal). `password` is intentionally absent: a runbook substitutes values into the
// prompt body, so secrets belong in a Credentials-bound runnable, not a runbook param.
interface RunbookArgRow {
  name?: string
  type?: string
  options?: string[]
  default?: string
  required?: boolean
  description?: string
}
interface RunbookFormValues {
  id: string
  title: string
  symptom?: string
  tags?: string[]
  steps: string
  args?: RunbookArgRow[]
}

const RUNBOOK_ARG_TYPES = ['str', 'int', 'float', 'flag', 'choice', 'path', 'host']

function argsRawToRows(raw: Record<string, unknown> | undefined): RunbookArgRow[] {
  if (!raw) return []
  return Object.entries(raw).map(([name, defUnknown]) => {
    const def = (defUnknown ?? {}) as Record<string, unknown>
    return {
      name,
      type: def.ui === 'host' ? 'host' : ((def.type as string) ?? 'str'),
      options: (def.options as string[]) ?? [],
      default: def.default !== undefined ? String(def.default) : undefined,
      required: !!def.required,
      description: (def.description as string) ?? '',
    }
  })
}

function rowsToArgsRaw(rows: RunbookArgRow[] | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const row of rows ?? []) {
    const name = (row.name ?? '').trim()
    if (!name) continue
    const def: Record<string, unknown> = {}
    if (row.type === 'host') { def.type = 'str'; def.ui = 'host' }
    else if (row.type === 'choice') { def.type = 'choice'; def.options = row.options ?? [] }
    else { def.type = row.type ?? 'str' }
    if (row.required) def.required = true
    const raw = row.default
    if (raw !== undefined && `${raw}`.trim() !== '') {
      if (row.type === 'int') def.default = Number.parseInt(`${raw}`, 10)
      else if (row.type === 'float') def.default = Number.parseFloat(`${raw}`)
      else if (row.type === 'flag') def.default = `${raw}`.toLowerCase() === 'true'
      else def.default = `${raw}`
    }
    if (row.description) def.description = row.description
    out[name] = def
  }
  return out
}

function RunbooksTab() {
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)  // '' = new
  const [form] = Form.useForm<RunbookFormValues>()

  const load = useCallback(() => bridge.get_runbooks().then(setRunbooks).catch(() => {}), [])
  // Load on mount, and refresh whenever runbooks change elsewhere — notably a
  // Config Sync pull, which merges new runbooks to disk and dispatches this event.
  // Without this the panel stayed stale until the drawer was reopened/restarted,
  // and a subsequent bulk save here would clobber the pulled-in runbooks it never
  // saw (they live in the in-memory `runbooks` list this rebuilds from).
  useEffect(() => {
    load()
    const onChanged = () => { load() }
    window.addEventListener('runspec:runbooks_changed', onChanged)
    return () => window.removeEventListener('runspec:runbooks_changed', onChanged)
  }, [load])

  const startAdd = () => { form.resetFields(); form.setFieldsValue({ args: [] }); setEditingKey('') }
  const startEdit = (s: Runbook) => {
    form.setFieldsValue({
      id: s.id, title: s.title, symptom: s.symptom, tags: s.tags ?? [],
      steps: s.steps, args: argsRawToRows(s.argsRaw),
    })
    setEditingKey(s.id)
  }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  // Persist the whole collection (mirrors save_jump_hosts). Existing runbooks are sent
  // by their stored argsRaw table; the edited one is rebuilt from the form rows.
  const persist = async (rows: Array<Record<string, unknown>>) => {
    const res = await bridge.save_runbooks(rows as unknown as Runbook[])
    if (!res.ok) { message.error(res.error || 'Could not save runbook'); return false }
    await load()
    // Refresh the `$` menu / run modal elsewhere in the app.
    window.dispatchEvent(new CustomEvent('runspec:runbooks_changed'))
    return true
  }

  const save = async () => {
    const values = await form.validateFields()
    const id = (values.id ?? '').trim()
    const edited: Record<string, unknown> = {
      id,
      title: (values.title ?? '').trim(),
      symptom: (values.symptom ?? '').trim(),
      steps: (values.steps ?? '').trim(),
      tags: values.tags ?? [],
      argsRaw: rowsToArgsRaw(values.args),
    }
    const others = runbooks
      .filter(s => s.id !== (editingKey || id))
      .map(s => ({
        id: s.id, title: s.title, symptom: s.symptom, steps: s.steps,
        tags: s.tags, argsRaw: s.argsRaw ?? {},
      }))
    if (await persist([...others, edited])) { setEditingKey(null); form.resetFields() }
  }

  const remove = async (id: string) => {
    const rows = runbooks
      .filter(s => s.id !== id)
      .map(s => ({ id: s.id, title: s.title, symptom: s.symptom, steps: s.steps, tags: s.tags, argsRaw: s.argsRaw ?? {} }))
    await persist(rows)
  }

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        Runbooks are reusable, parameterized runbooks. Invoke one from the chat prompt
        with <Typography.Text code>$</Typography.Text>: it collects the parameters
        below in a form, then runs the steps through the assistant (using SSH runnables,
        file relay, and filters). Parameters use the same fields as a runnable's args, and
        the body can reference them as <Typography.Text code>{'{{name}}'}</Typography.Text>.
        Runbooks sync with the rest of your config (Config Sync), so a team shares one set.
      </Typography.Paragraph>

      <Button type="dashed" icon={<PlusOutlined />} onClick={startAdd} style={{ marginBottom: 12 }}>
        New runbook
      </Button>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {runbooks.length === 0 && <Empty description="No runbooks yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        {runbooks.map(s => (
          <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
            <span style={{ minWidth: 200 }}>
              <Typography.Text code style={{ fontSize: 12 }}>${s.id}</Typography.Text>
            </span>
            <span style={{ flex: 1, fontSize: 12, color: '#888', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {s.title}{s.symptom ? ` — ${s.symptom}` : ''}
            </span>
            {(s.args ?? []).length > 0 && (
              <Tag style={{ fontSize: 10, margin: 0 }}>{(s.args ?? []).length} param{(s.args ?? []).length !== 1 ? 's' : ''}</Tag>
            )}
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(s)} disabled={editingKey !== null} />
            <Popconfirm title={`Remove ${s.id}?`} onConfirm={() => remove(s.id)}>
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={editingKey !== null} danger />
            </Popconfirm>
          </div>
        ))}
      </div>

      <Modal
        open={editingKey !== null}
        title={editingKey === '' ? 'New runbook' : `Edit — ${editingKey}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
      >
        <Form form={form} layout="vertical" size="small">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
            <Form.Item name="id" label="Id" rules={[{ required: true, message: 'Required' }]}>
              <Input placeholder="fetch-and-process-log" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
            </Form.Item>
            <Form.Item name="title" label="Title">
              <Input placeholder="Fetch & process a prod log" />
            </Form.Item>
          </div>
          <Form.Item name="symptom" label="When to use">
            <Input placeholder="Pull today's app log through a filter, then process it" />
          </Form.Item>
          <Form.Item name="tags" label="Tags">
            <Select mode="tags" placeholder="logs, relay" tokenSeparators={[',']} />
          </Form.Item>
          <Form.Item name="steps" label="Body / steps" rules={[{ required: true, message: 'Required' }]}
            help="Free text the assistant follows. Reference parameters as {{name}}.">
            <Input.TextArea rows={6} style={{ fontFamily: 'monospace', fontSize: 12 }}
              placeholder={'1. Use list-app-folders on {{host}} for {{app}}.\n2. Relay /srv/prod/{{app}}/logs/{{logfile}} via filter {{filter}} to jump-01:/home/me/logs.\n3. Run process-log --file /home/Jason/logs/{{logfile}}.'} />
          </Form.Item>

          <Divider style={{ margin: '8px 0' }} orientation="left" plain>Parameters</Divider>
          <Form.List name="args">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <div key={key} style={{ display: 'flex', gap: 6, alignItems: 'baseline', marginBottom: 6 }}>
                    <Form.Item {...rest} name={[name, 'name']} style={{ marginBottom: 0, width: 130 }}
                      rules={[{ required: true, message: 'name' }]}>
                      <Input placeholder="name" style={{ fontFamily: 'monospace' }} />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, 'type']} style={{ marginBottom: 0, width: 96 }} initialValue="str">
                      <Select options={RUNBOOK_ARG_TYPES.map(t => ({ label: t, value: t }))} />
                    </Form.Item>
                    <Form.Item noStyle shouldUpdate={(p, c) => p.args?.[name]?.type !== c.args?.[name]?.type}>
                      {() => form.getFieldValue(['args', name, 'type']) === 'choice' ? (
                        <Form.Item {...rest} name={[name, 'options']} style={{ marginBottom: 0, width: 150 }}>
                          <Select mode="tags" placeholder="options" tokenSeparators={[',']} />
                        </Form.Item>
                      ) : (
                        <Form.Item {...rest} name={[name, 'default']} style={{ marginBottom: 0, width: 150 }}>
                          <Input placeholder="default" />
                        </Form.Item>
                      )}
                    </Form.Item>
                    <Form.Item {...rest} name={[name, 'required']} valuePropName="checked" style={{ marginBottom: 0 }}>
                      <Tooltip title="Required"><Switch size="small" /></Tooltip>
                    </Form.Item>
                    <MinusCircleOutlined onClick={() => remove(name)} style={{ color: '#666', cursor: 'pointer' }} />
                  </div>
                ))}
                <Button type="dashed" size="small" onClick={() => add({ type: 'str' })} icon={<PlusOutlined />}>
                  Add parameter
                </Button>
              </>
            )}
          </Form.List>
        </Form>
      </Modal>
    </div>
  )
}

// ── Denylist (fleet kill switch) ────────────────────────────────────────────

export function DenylistTab() {
  const [hosts, setHosts] = useState<Host[]>([])
  const [targets, setTargets] = useState<string[]>([])
  const [runnableGlobs, setRunnableGlobs] = useState<string[]>([])
  const [userGlobs, setUserGlobs] = useState<string[]>([])
  const [hostGlobs, setHostGlobs] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const [mode, setMode] = useState<'enforce' | 'warn' | 'off'>('enforce')
  const [result, setResult] = useState('')
  const [busy, setBusy] = useState('')
  const [status, setStatus] = useState<DenylistHostStatus[]>([])
  const [loadingStatus, setLoadingStatus] = useState(false)

  useEffect(() => { bridge.get_hosts().then(setHosts) }, [])

  const scope = targets.length ? `${targets.length} selected host(s)` : 'ALL hosts'

  const refresh = useCallback(async () => {
    setLoadingStatus(true)
    try {
      setStatus(await bridge.denylist_status(targets))
    } catch (err) {
      message.error((err as Error)?.message || 'could not read denylist status')
    } finally {
      setLoadingStatus(false)
    }
  }, [targets])

  // Show live state on open and whenever the host selection changes.
  useEffect(() => { refresh() }, [refresh])

  const run = async (payload: DenylistPayload, label: string) => {
    setBusy(label)
    try {
      setResult(await bridge.manage_denylist({ ...payload, targets }))
      await refresh()  // reflect the change in the table immediately
    } catch (err) {
      message.error((err as Error)?.message || 'denylist action failed')
    } finally {
      setBusy('')
    }
  }

  const hostOptions = hosts.map(h => ({
    value: h.name,
    label: h.group ? `${h.name} (${h.group})` : h.name,
  }))

  const ruleSummary = (r: DenylistHostStatus): string => {
    if (!r.ok) return r.error || 'unreachable'
    if (r.disabled) return 'ALL disabled' + (r.reason ? ` (${r.reason})` : '')
    if (!r.rules?.length) return 'none'
    return r.rules
      .map(rule => `${rule.runnables.join(',')} [users ${rule.users.join(',')}; hosts ${rule.hosts.join(',')}]${rule.reason ? ` — ${rule.reason}` : ''}`)
      .join(' · ')
  }

  const statusColumns = [
    { title: 'Host', dataIndex: 'host', key: 'host' },
    { title: 'Venv', dataIndex: 'venv', key: 'venv' },
    {
      title: 'Mode',
      key: 'mode',
      render: (_: unknown, r: DenylistHostStatus) =>
        !r.ok ? <Tag color="default">—</Tag>
          : <Tag color={r.mode === 'off' ? 'default' : r.mode === 'warn' ? 'gold' : 'blue'}>{r.mode}</Tag>,
    },
    {
      title: 'Disabled',
      key: 'disabled',
      render: (_: unknown, r: DenylistHostStatus) => {
        if (!r.ok) return <Tag color="red">error</Tag>
        if (r.disabled) return <Tag color="red">ALL</Tag>
        if (r.rules?.length) return <Tag color="orange">{r.rules.length} rule{r.rules.length === 1 ? '' : 's'}</Tag>
        return <Tag color="green">none</Tag>
      },
    },
    {
      title: 'Detail',
      key: 'detail',
      render: (_: unknown, r: DenylistHostStatus) => (
        <Tooltip title={ruleSummary(r)}>
          <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: false }}>{ruleSummary(r)}</Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <div>
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        style={{ marginBottom: 12 }}
        message="Kill switch"
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            Disable or re-enable runspec runnables across the fleet. This writes each
            host's venv denylist via the core <Text code>runspec deny</Text> over SSH —
            it stops the <em>runnables</em> on those hosts, never this console, and
            “Re-enable all” always works. Remote hosts need <Text code>runspec ≥ 0.43.0</Text>.
          </Text>
        }
      />

      <div style={{ marginBottom: 8 }}>
        <Text style={{ fontSize: 12 }}>Target hosts</Text>
        <Select
          mode="multiple"
          allowClear
          size="small"
          style={{ width: '100%', marginTop: 4 }}
          placeholder="All hosts (leave empty to apply to the whole fleet)"
          value={targets}
          onChange={setTargets}
          options={hostOptions}
        />
      </div>

      <Table<DenylistHostStatus>
        size="small"
        rowKey={r => `${r.host}/${r.venv}`}
        columns={statusColumns}
        dataSource={status}
        loading={loadingStatus}
        pagination={false}
        style={{ marginBottom: 12 }}
        locale={{ emptyText: 'No hosts with a runspec path.' }}
      />

      <Space wrap style={{ marginBottom: 12 }}>
        <Button
          size="small"
          icon={loadingStatus ? <LoadingOutlined /> : <ReloadOutlined />}
          loading={loadingStatus}
          onClick={refresh}
        >
          Refresh status
        </Button>
        <Popconfirm
          title="Disable ALL runnables?"
          icon={<WarningOutlined style={{ color: '#faad14' }} />}
          description={
            <span style={{ fontSize: 12 }}>
              Blocks every runnable on {scope}.<br />
              Agent and manual runs are refused until you re-enable.
            </span>
          }
          okText="Disable all"
          okButtonProps={{ danger: true }}
          onConfirm={() => run({ action: 'all', reason }, 'all')}
        >
          <Button size="small" danger icon={<StopOutlined />} loading={busy === 'all'}>
            Disable all
          </Button>
        </Popconfirm>
        <Popconfirm
          title="Re-enable everything?"
          description={<span style={{ fontSize: 12 }}>Clears the denylist on {scope}.</span>}
          okText="Re-enable"
          onConfirm={() => run({ action: 'clear' }, 'clear')}
        >
          <Button size="small" icon={<ReloadOutlined />} loading={busy === 'clear'}>
            Re-enable all (clear)
          </Button>
        </Popconfirm>
      </Space>

      <Divider style={{ margin: '8px 0' }} orientation="left" plain>
        <Text type="secondary" style={{ fontSize: 12 }}>Disable specific runnables</Text>
      </Divider>
      <Space direction="vertical" style={{ width: '100%', marginBottom: 12 }} size={6}>
        <Select mode="tags" size="small" style={{ width: '100%' }} value={runnableGlobs} onChange={setRunnableGlobs} placeholder="Runnable name globs (e.g. deploy, restart-*) — empty = all" />
        <Select mode="tags" size="small" style={{ width: '100%' }} value={userGlobs} onChange={setUserGlobs} placeholder="Username globs — empty = all users" />
        <Select mode="tags" size="small" style={{ width: '100%' }} value={hostGlobs} onChange={setHostGlobs} placeholder="Hostname globs the rule applies to — empty = all hosts" />
        <Input size="small" placeholder="Reason (shown on the refusal)" value={reason} onChange={e => setReason(e.target.value)} />
        <Button
          size="small"
          icon={<StopOutlined />}
          loading={busy === 'add'}
          onClick={() => run({ action: 'add', runnable: runnableGlobs, user: userGlobs, host: hostGlobs, reason }, 'add')}
        >
          Add deny rule
        </Button>
      </Space>

      <Divider style={{ margin: '8px 0' }} orientation="left" plain>
        <Text type="secondary" style={{ fontSize: 12 }}>Enforcement mode</Text>
      </Divider>
      <Space style={{ marginBottom: 12 }}>
        <Segmented
          size="small"
          value={mode}
          onChange={v => setMode(v as 'enforce' | 'warn' | 'off')}
          options={[
            { label: 'Enforce', value: 'enforce' },
            { label: 'Warn', value: 'warn' },
            { label: 'Off', value: 'off' },
          ]}
        />
        <Button size="small" loading={busy === 'mode'} onClick={() => run({ action: 'mode', setting: mode }, 'mode')}>
          Apply mode
        </Button>
      </Space>

      {result && (
        <pre style={{ fontSize: 12, background: '#1a1a1a', border: '1px solid #2a2a2a', borderRadius: 6, padding: 10, whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>
          {result}
        </pre>
      )}
    </div>
  )
}

// ── Drawer ────────────────────────────────────────────────────────────────────

function ChatHistoryTab() {
  const [enabled, setEnabled] = useState(true)
  const [maxConvs, setMaxConvs] = useState<number>(200)
  const [maxAge, setMaxAge] = useState<number>(90)
  const [keepFirst, setKeepFirst] = useState<number>(1)
  const [keepRecent, setKeepRecent] = useState<number>(20)
  const [compactOnResume, setCompactOnResume] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    bridge.get_config().then(cfg => {
      const ch = (cfg.chat_history ?? {}) as Record<string, unknown>
      setEnabled(ch.enabled !== false)
      setMaxConvs(typeof ch.max_conversations === 'number' ? ch.max_conversations : 200)
      setMaxAge(typeof ch.max_age_days === 'number' ? ch.max_age_days : 90)
      setKeepFirst(typeof ch.resume_keep_first === 'number' ? ch.resume_keep_first : 1)
      setKeepRecent(typeof ch.resume_keep_recent === 'number' ? ch.resume_keep_recent : 20)
      setCompactOnResume(ch.compact_on_resume === true)
    })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      await bridge.save_config({ ...cfg, chat_history: { enabled, max_conversations: maxConvs, max_age_days: maxAge, resume_keep_first: keepFirst, resume_keep_recent: keepRecent, compact_on_resume: compactOnResume } })
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } finally { setSaving(false) }
  }

  const row = (label: string, control: ReactNode) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
      <span style={{ fontSize: 13 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        Save each chat so you can reopen and continue past conversations from the Console's <strong>History</strong> button.
        Transcripts are stored <strong>only on this machine</strong> (never synced) and may contain sensitive content.
        Old conversations are pruned to the limits below.
      </Typography.Paragraph>
      {row('Save chat history', <Switch checked={enabled} onChange={setEnabled} />)}
      {row('Keep at most (conversations)', <InputNumber min={0} value={maxConvs} onChange={v => setMaxConvs(Number(v) || 0)} disabled={!enabled} />)}
      {row('Delete after (days)', <InputNumber min={0} value={maxAge} onChange={v => setMaxAge(Number(v) || 0)} disabled={!enabled} />)}
      <Typography.Paragraph type="secondary" style={{ fontSize: 11 }}>
        0 = no limit. Disabling stops saving new conversations; existing files stay until pruned or deleted.
      </Typography.Paragraph>

      <Divider style={{ margin: '14px 0' }} />
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        When you reopen a long conversation, the opening turns and the most recent turns are kept word-for-word;
        the turns in between are condensed so the chat fits the model's context. <strong>The saved transcript is never changed</strong> — only what the model sees on resume.
      </Typography.Paragraph>
      {row('Keep opening turns verbatim on resume', <InputNumber min={0} value={keepFirst} onChange={v => setKeepFirst(Number(v) || 0)} disabled={!enabled} />)}
      {row('Keep recent turns verbatim on resume', <InputNumber min={1} value={keepRecent} onChange={v => setKeepRecent(Number(v) || 1)} disabled={!enabled} />)}
      {row(
        'Summarize older turns with a model call',
        <Switch checked={compactOnResume} onChange={setCompactOnResume} disabled={!enabled} />,
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: 11 }}>
        Off (default): older turns are condensed to a short note listing what they covered — no model call.
        On: an LLM summarizes them on resume (one extra call, slight delay; falls back to the note if it fails).
      </Typography.Paragraph>
      <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
    </div>
  )
}

export function SettingsDrawer({ open, onClose, onHostsChanged, onKeyChanged, connectedHosts, allRemoteHosts }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width="min(96vw, 1040px)" open={open} onClose={onClose}>
      <Tabs
        size="small"
        tabPosition="left"
        items={[
          { key: 'ssh',       label: 'SSH',        children: <SshTab onKeyChanged={onKeyChanged} connectedHosts={connectedHosts} allRemoteHosts={allRemoteHosts} /> },
          { key: 'llm',       label: 'Model / API',  children: <LlmTab /> },
          { key: 'jumpHosts', label: 'Jump Hosts',  children: <JumpHostsTab onHostsChanged={onHostsChanged} /> },
          { key: 'runbooks',    label: 'Runbooks',     children: <RunbooksTab /> },
          { key: 'selfService', label: 'Self-Service', children: <SelfServiceTab /> },
          { key: 'filters',   label: 'Filters',    children: <FiltersTab /> },
          { key: 'repos',     label: 'Repos',      children: <ReposTab /> },
          { key: 'credentials', label: 'Credentials', children: <CredentialsTab /> },
          { key: 'denylist',  label: 'Denylist',   children: <DenylistTab /> },
          { key: 'configSync', label: 'Config Sync', children: <ConfigSyncTab /> },
          { key: 'mailbox',   label: 'Mailbox',    children: <MailboxesTab /> },
          { key: 'teams',     label: 'Teams',      children: <TeamsTab /> },
          { key: 'sounds',    label: 'Sounds & Notifications', children: <SoundsTab /> },
          { key: 'chatHistory', label: 'Chat History', children: <ChatHistoryTab /> },
        ]}
      />
    </Drawer>
  )
}
