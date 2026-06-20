import { useCallback, useEffect, useRef, useState } from 'react'
import { Drawer, Modal, Form, Input, InputNumber, Button, Divider, Typography, Space, Tabs, Popconfirm, message, Tag, Tooltip, Select, Switch, Collapse, Alert, Segmented, AutoComplete, Empty } from 'antd'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined, UploadOutlined, DownloadOutlined, UpOutlined, DownOutlined, ApiOutlined, LoadingOutlined, KeyOutlined, DesktopOutlined, MailOutlined, ThunderboltOutlined, TeamOutlined } from '@ant-design/icons'
import { bridge, type JumpHost, type TestResult, type RotateHostResult, type Filter, type Credential, type Mailbox, type TriggerListResult, type Team, type TeamsEvent, type SyncConfig, type SyncPullResult, type Skill } from '../bridge'
import { CopyKeyModal } from '../views/CopyKeyModal'
import { PathInput } from '../views/PathInput'

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

// ── LLM tab ───────────────────────────────────────────────────────────────────

export function LlmTab() {
  const [form] = Form.useForm()
  const [provider, setProvider] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [configDir, setConfigDir] = useState<string>('')
  const [providerOptions, setProviderOptions] = useState(BUILTIN_PROVIDER_OPTIONS)

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
      form.setFieldsValue({
        provider: p,
        api_key: (llm.api_key as string) ?? '',
        model: (llm.model as string) ?? '',
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
    if (!form.getFieldValue('model') && PROVIDER_MODELS[val]) {
      form.setFieldValue('model', PROVIDER_MODELS[val])
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
            <Form.Item name="model" label="Model">
              <Input placeholder={PROVIDER_MODELS[provider] ?? ''} style={{ fontFamily: 'monospace' }} />
            </Form.Item>
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
      ] as const) {
        const n = v[field]
        if (n !== undefined && n !== null && n !== '') sshOut[key] = Number(n)
        else delete sshOut[key]
      }
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

function JumpHostsTab({ onHostsChanged }: { onHostsChanged?: () => void }) {
  const [hosts, setHosts] = useState<JumpHost[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [form] = Form.useForm<HostFormValues>()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [testingHost, setTestingHost] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({})
  const [copyKeyHost, setCopyKeyHost] = useState<string | null>(null)

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
  }, [])

  // Tear down any browse connection opened from the add/edit form on unmount.
  useEffect(() => () => { bridge.close_adhoc().catch(() => {}) }, [])

  const persist = async (next: JumpHost[]) => {
    setHosts(next)
    await bridge.save_jump_hosts(next)
    onHostsChanged?.()
  }

  const startAdd = () => { form.resetFields(); setEditingKey('') }

  const startEdit = (h: JumpHost) => {
    form.setFieldsValue({ ...h, runspec_paths: h.runspec_paths ?? [] })
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
            <Form.Item name="group" label="Group">
              <Input placeholder="e.g. Production, Staging (optional)" />
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
              {/* Group-chat `room` and an explicit `room_queue` override are hidden
                  in the helpdesk-focused build (the queue defaults to "helpdesk").
                  Kept hidden so an existing config still round-trips; remove
                  `hidden` to re-expose. */}
              <Form.Item name="room" hidden>
                <Input />
              </Form.Item>
              <Form.Item name="room_queue" hidden>
                <Input />
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
  const isSshKey = kind === 'ssh-key'

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
                  <Input placeholder="~/.ssh/runspec_ed25519" style={{ fontFamily: 'monospace' }} autoComplete="off" />
                </Form.Item>
                <Form.Item name="public_key" label="Public key path" tooltip="Optional — the matching .pub file. Informational; not required to connect.">
                  <Input placeholder="~/.ssh/runspec_ed25519.pub" style={{ fontFamily: 'monospace' }} autoComplete="off" />
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
                <Divider style={{ margin: '4px 0 12px' }} />
                <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>
                  Binding — which runnable arguments these values fill, and where the credential may be applied (glob patterns; blank = any).
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
  useEffect(() => { bridge.config_dir().then(setConfigDir) }, [])

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
            help="Default for this mailbox: triggers fire on the first message in a conversation and ignore later replies. Each trigger can override this.">
            <Switch size="small" /> <span style={{ fontSize: 12, color: '#888', marginLeft: 6 }}>Suppress replies by default (once per thread)</span>
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

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <Button type="primary" size="small" loading={saving} onClick={save}>Save</Button>
        {saved && <Text type="success" style={{ fontSize: 12 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
        Saved to <code>config.toml</code> in <code>{configDir || '%APPDATA%/runspec-console'}</code>. Changes restart the trigger engine.
      </Text>
    </div>
  )
}

// ── Triggers editor (runspec_triggers.toml) ─────────────────────────────────

const AUTONOMY_OPTS = ['confirm', 'autonomous', 'supervised', 'manual']

interface TriggerForm {
  name: string
  source?: string
  event?: string
  mode: 'agent' | 'rule'
  autonomy: string
  enabled: boolean
  match: { field: string; pattern: string }[]
  // Tri-state override of the mailbox's reply-suppression default.
  once_per_thread: 'default' | 'on' | 'off'
  rules?: string
  runnables?: string[]
  run?: string
  map: { arg: string; template: string }[]
  reply_draft: boolean
  reply_style?: string
}

const triggerToForm = (t: Record<string, unknown>): TriggerForm => {
  const reply = (t.reply ?? {}) as { draft?: boolean; style?: string }
  const match = (t.match ?? {}) as Record<string, unknown>
  const map = (t.map ?? {}) as Record<string, unknown>
  return {
    name: String(t.name ?? ''),
    source: t.source ? String(t.source) : undefined,
    event: t.event ? String(t.event) : undefined,
    mode: t.mode === 'rule' ? 'rule' : 'agent',
    autonomy: t.autonomy ? String(t.autonomy) : 'confirm',
    enabled: t.enabled !== false,
    match: Object.entries(match).map(([field, pattern]) => ({ field, pattern: String(pattern) })),
    // Absent → inherit the mailbox default; an explicit bool → on/off override.
    once_per_thread: typeof t.once_per_thread === 'boolean' ? (t.once_per_thread ? 'on' : 'off') : 'default',
    rules: t.rules ? String(t.rules) : '',
    runnables: Array.isArray(t.runnables) ? (t.runnables as unknown[]).map(String) : [],
    run: t.run ? String(t.run) : '',
    map: Object.entries(map).map(([arg, template]) => ({ arg, template: String(template) })),
    reply_draft: !!reply.draft,
    reply_style: reply.style ? String(reply.style) : '',
  }
}

const formToTrigger = (v: TriggerForm): Record<string, unknown> => {
  const matchObj: Record<string, string> = {}
  for (const m of v.match || []) if (m?.field?.trim()) matchObj[m.field.trim()] = (m.pattern ?? '').trim()
  const base: Record<string, unknown> = {
    name: (v.name ?? '').trim(),
    source: v.source,
    event: v.event,
    mode: v.mode,
    autonomy: v.autonomy,
    enabled: v.enabled,
    match: matchObj,
    reply: { draft: !!v.reply_draft, style: (v.reply_style ?? '').trim() },
  }
  // Only an explicit override crosses the bridge; 'default' omits the key so the
  // trigger inherits the mailbox default.
  if (v.once_per_thread === 'on') base.once_per_thread = true
  else if (v.once_per_thread === 'off') base.once_per_thread = false
  if (v.mode === 'rule') {
    const mapObj: Record<string, string> = {}
    for (const m of v.map || []) if (m?.arg?.trim()) mapObj[m.arg.trim()] = (m.template ?? '').trim()
    base.run = (v.run ?? '').trim()
    base.map = mapObj
  } else {
    base.rules = (v.rules ?? '').trim()
    base.runnables = (v.runnables ?? []).map(s => s.trim()).filter(Boolean)
  }
  return base
}

function TriggersEditor() {
  const [data, setData] = useState<TriggerListResult | null>(null)
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState<string | null>(null) // null = adding new
  const [form] = Form.useForm<TriggerForm>()
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const mode = Form.useWatch('mode', form)
  const source = Form.useWatch('source', form)

  const reload = useCallback(() => bridge.list_triggers().then(setData).catch(console.error), [])
  useEffect(() => { reload() }, [reload])

  const sources = data?.sources ?? []
  const triggers = data?.triggers ?? []
  const errors = data?.errors ?? []
  const eventOpts = (sources.find(s => s.id === source)?.event_types ?? []).map(e => ({ value: e }))

  const startAdd = () => {
    setEditName(null); setErr('')
    form.resetFields()
    form.setFieldsValue({ mode: 'agent', autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default', runnables: [], map: [], reply_draft: false })
    setEditing(true)
  }
  const startEdit = (t: Record<string, unknown>) => {
    setEditName(String(t.name)); setErr('')
    form.setFieldsValue(triggerToForm(t))
    setEditing(true)
  }
  const cancel = () => { setEditing(false); setErr(''); form.resetFields() }

  const save = async () => {
    let v: TriggerForm
    try { v = await form.validateFields() } catch { return }
    setSaving(true)
    try {
      const res = await bridge.save_trigger(formToTrigger(v))
      if (!res.ok) { setErr(res.error || 'Could not save the trigger.'); return }
      setEditing(false); setErr(''); form.resetFields()
      await reload()
    } finally { setSaving(false) }
  }

  const toggle = async (name: string, enabled: boolean) => { await bridge.set_trigger_enabled(name, enabled); await reload() }
  const remove = async (name: string) => { await bridge.delete_trigger(name); await reload() }

  const pairRow = (listName: 'match' | 'map', keyName: string, valName: string, keyPh: string, valPh: string, addLabel: string) => (
    <Form.List name={listName}>
      {(fields, { add, remove: rm }) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}>
          {fields.map(f => (
            <div key={f.key} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <Form.Item name={[f.name, keyName]} noStyle><Input size="small" placeholder={keyPh} style={{ fontFamily: 'monospace' }} /></Form.Item>
              <span style={{ color: '#666' }}>→</span>
              <Form.Item name={[f.name, valName]} noStyle><Input size="small" placeholder={valPh} style={{ fontFamily: 'monospace' }} /></Form.Item>
              <MinusCircleOutlined onClick={() => rm(f.name)} style={{ color: '#666', cursor: 'pointer' }} />
            </div>
          ))}
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add()} style={{ width: 'fit-content' }}>{addLabel}</Button>
        </div>
      )}
    </Form.List>
  )

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, gap: 12 }}>
        <Text strong style={{ fontSize: 13 }}>Triggers</Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={editing}>Add trigger</Button>
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        React to a monitored event (<code>source:event</code>) — run a runnable (rule) or an agent turn. Saved to <code>runspec_triggers.toml</code>.
      </Text>

      {errors.length > 0 && (
        <Alert type="error" showIcon style={{ marginBottom: 10 }} message="Some triggers couldn't be loaded"
          description={<ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>} />
      )}

      <Modal
        open={editing}
        title={editName === null ? 'New trigger' : `Edit — ${editName}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        confirmLoading={saving}
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
              <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true, message: 'Required' }, { pattern: /^[^\s]+$/, message: 'No spaces' }]}>
                <Input placeholder="password-reset" style={{ fontFamily: 'monospace' }} disabled={editName !== null} />
              </Form.Item>
              <Form.Item name="source" label="Source" rules={[{ required: true, message: 'Required' }]}>
                <Select placeholder="outlook" options={sources.map(s => ({ value: s.id, label: s.available ? s.id : `${s.id} (unavailable)` }))} />
              </Form.Item>
              <Form.Item name="event" label="Event" rules={[{ required: true, whitespace: true, message: 'Required' }]}>
                <AutoComplete options={eventOpts} placeholder="new_mail" />
              </Form.Item>
              <Form.Item name="autonomy" label="Autonomy">
                <Select options={AUTONOMY_OPTS.map(a => ({ value: a }))} />
              </Form.Item>
            </div>

            <Form.Item name="mode" label="Mode">
              <Segmented options={[{ label: 'Agent (LLM turn)', value: 'agent' }, { label: 'Rule (fixed run)', value: 'rule' }]} />
            </Form.Item>

            <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>Match — every field's regex must match the event payload (empty = any event of that source/type)</label>
            {pairRow('match', 'field', 'pattern', 'subject', '(?i)password', 'Add match')}

            <Form.Item name="once_per_thread" label="Replies" style={{ marginBottom: 10 }}
              help="Fire on the first message in a conversation, then ignore replies in that thread (keyed on the email/chat thread id, survives restarts). Default follows the mailbox setting.">
              <Segmented options={[{ label: 'Mailbox default', value: 'default' }, { label: 'Once per thread', value: 'on' }, { label: 'Every message', value: 'off' }]} />
            </Form.Item>

            {mode === 'rule' ? (
              <>
                <Form.Item name="run" label="Run" rules={[{ required: true, whitespace: true, message: 'Required for rule mode' }]} help="host__runnable — the runnable to run.">
                  <Input placeholder="web01__reset-password" style={{ fontFamily: 'monospace' }} />
                </Form.Item>
                <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>{'Arg map — runnable arg → {{ payload field }}'}</label>
                {pairRow('map', 'arg', 'template', 'user', '{{ from }}', 'Add arg')}
              </>
            ) : (
              <>
                <Form.Item name="rules" label="Agent instructions" help="The per-trigger system prompt the LLM follows.">
                  <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="Triage the email and draft a reset link…" />
                </Form.Item>
                <Form.Item name="runnables" label="Runnable allowlist (optional)" help="Tools the agent may call. Empty = any runnable.">
                  <Select mode="tags" tokenSeparators={[',', ' ']} open={false} suffixIcon={null} placeholder="reset-password, request-vm" />
                </Form.Item>
              </>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px', alignItems: 'end' }}>
              <Form.Item name="reply_draft" label="Draft a reply" valuePropName="checked" help="Offer a draft_reply tool (email triggers).">
                <Switch size="small" />
              </Form.Item>
              <Form.Item name="reply_style" label="Reply style (optional)">
                <Input placeholder="formal, concise…" />
              </Form.Item>
            </div>

            <Form.Item name="enabled" valuePropName="checked" style={{ marginBottom: 8 }}>
              <Switch size="small" /> <span style={{ fontSize: 12, color: '#888', marginLeft: 6 }}>Enabled</span>
            </Form.Item>

            {err && <Alert type="error" showIcon style={{ marginBottom: 4 }} message={err} />}
        </Form>
      </Modal>

      {triggers.length === 0 && !editing ? (
        <Empty description={<span style={{ fontSize: 12, color: '#888' }}>No triggers yet. Add one to react to a monitored event.</span>} />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {triggers.map((t, i) => (
            <div key={`${String(t.name)} ${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
              <ThunderboltOutlined style={{ color: '#666', flexShrink: 0 }} />
              <Text style={{ fontFamily: 'monospace', fontSize: 12, flexShrink: 0 }}>{String(t.name)}</Text>
              <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {String(t.source ?? '')}:{String(t.event ?? '')}
              </Text>
              <Tag color={t.mode === 'rule' ? 'geekblue' : 'purple'} style={{ margin: 0, fontSize: 10 }}>{String(t.mode ?? 'agent')}</Tag>
              <Switch size="small" checked={t.enabled !== false} onChange={v => toggle(String(t.name), v)} />
              <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(t)} disabled={editing} style={{ padding: '0 4px', color: '#666' }} />
              <Popconfirm title={`Delete trigger ${String(t.name)}?`} onConfirm={() => remove(String(t.name))} okText="Delete" cancelText="Cancel" placement="left">
                <Button type="text" size="small" icon={<DeleteOutlined />} disabled={editing} style={{ padding: '0 4px', color: '#666' }} danger />
              </Popconfirm>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// The Triggers tab: the monitored mailbox + the triggers editor.
function TriggersTab() {
  return (
    <div>
      <TriggersEditor />
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
  subpath?: string
  ca_bundle?: string
  tls_verify?: boolean
  files: string[]
  preserve?: string[]
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

  const load = useCallback(() => {
    bridge.get_sync_config().then(c => {
      setCfg(c)
      form.setFieldsValue({
        url: c.url, ref: c.ref, credential_id: c.credential_id || undefined,
        subpath: c.subpath, ca_bundle: c.ca_bundle, tls_verify: c.tls_verify !== false,
        files: c.files?.length ? c.files : c.files_default,
        preserve: c.preserve ?? [],
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
      credential_id: v.credential_id || '', subpath: v.subpath?.trim() || '',
      ca_bundle: v.ca_bundle?.trim() || '', tls_verify: v.tls_verify !== false,
      files: v.files ?? [], preserve: v.preserve ?? [],
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
        <Form.Item name="subpath" label="Subdirectory (optional)"
          tooltip="A directory within the repo that holds the config files (blank = repo root).">
          <Input placeholder="" style={{ fontFamily: 'monospace' }} autoComplete="off" />
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
            {' '}Hosts and triggers reloaded — no restart needed.
          </span>} />
      )}
      {result && !result.ok && (
        <Alert type="error" showIcon style={{ marginTop: 12 }} message="Pull failed" description={<span style={{ fontSize: 12 }}>{result.error}</span>} />
      )}
    </div>
  )
}

// ── Skills ────────────────────────────────────────────────────────────────────

// One editor row for a skill parameter. Stored as a runspec `[skill.args]` entry —
// `host` is the console-only `ui:"host"` pseudo-type (→ a fleet-host dropdown in the
// run modal). `password` is intentionally absent: a skill substitutes values into the
// prompt body, so secrets belong in a Credentials-bound runnable, not a skill param.
interface SkillArgRow {
  name?: string
  type?: string
  options?: string[]
  default?: string
  required?: boolean
  description?: string
}
interface SkillFormValues {
  id: string
  title: string
  symptom?: string
  tags?: string[]
  steps: string
  args?: SkillArgRow[]
}

const SKILL_ARG_TYPES = ['str', 'int', 'float', 'flag', 'choice', 'path', 'host']

function argsRawToRows(raw: Record<string, unknown> | undefined): SkillArgRow[] {
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

function rowsToArgsRaw(rows: SkillArgRow[] | undefined): Record<string, unknown> {
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

function SkillsTab() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)  // '' = new
  const [form] = Form.useForm<SkillFormValues>()

  const load = () => bridge.get_skills().then(setSkills).catch(() => {})
  useEffect(() => { load() }, [])

  const startAdd = () => { form.resetFields(); form.setFieldsValue({ args: [] }); setEditingKey('') }
  const startEdit = (s: Skill) => {
    form.setFieldsValue({
      id: s.id, title: s.title, symptom: s.symptom, tags: s.tags ?? [],
      steps: s.steps, args: argsRawToRows(s.argsRaw),
    })
    setEditingKey(s.id)
  }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  // Persist the whole collection (mirrors save_jump_hosts). Existing skills are sent
  // by their stored argsRaw table; the edited one is rebuilt from the form rows.
  const persist = async (rows: Array<Record<string, unknown>>) => {
    const res = await bridge.save_skills(rows as unknown as Skill[])
    if (!res.ok) { message.error(res.error || 'Could not save skill'); return false }
    await load()
    // Refresh the `$` menu / run modal elsewhere in the app.
    window.dispatchEvent(new CustomEvent('runspec:skills_changed'))
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
    const others = skills
      .filter(s => s.id !== (editingKey || id))
      .map(s => ({
        id: s.id, title: s.title, symptom: s.symptom, steps: s.steps,
        tags: s.tags, argsRaw: s.argsRaw ?? {},
      }))
    if (await persist([...others, edited])) { setEditingKey(null); form.resetFields() }
  }

  const remove = async (id: string) => {
    const rows = skills
      .filter(s => s.id !== id)
      .map(s => ({ id: s.id, title: s.title, symptom: s.symptom, steps: s.steps, tags: s.tags, argsRaw: s.argsRaw ?? {} }))
    await persist(rows)
  }

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        Skills are reusable, parameterized runbooks. Invoke one from the chat prompt
        with <Typography.Text code>$</Typography.Text>: it collects the parameters
        below in a form, then runs the steps through the assistant (using SSH runnables,
        file relay, and filters). Parameters use the same fields as a runnable's args, and
        the body can reference them as <Typography.Text code>{'{{name}}'}</Typography.Text>.
        Skills sync with the rest of your config (Config Sync), so a team shares one set.
      </Typography.Paragraph>

      <Button type="dashed" icon={<PlusOutlined />} onClick={startAdd} style={{ marginBottom: 12 }}>
        New skill
      </Button>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {skills.length === 0 && <Empty description="No skills yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        {skills.map(s => (
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
        title={editingKey === '' ? 'New skill' : `Edit — ${editingKey}`}
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
                      <Select options={SKILL_ARG_TYPES.map(t => ({ label: t, value: t }))} />
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

// ── Drawer ────────────────────────────────────────────────────────────────────

export function SettingsDrawer({ open, onClose, onHostsChanged, onKeyChanged, connectedHosts, allRemoteHosts }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width="min(96vw, 1040px)" open={open} onClose={onClose}>
      <Tabs
        size="small"
        tabPosition="left"
        items={[
          { key: 'ssh',       label: 'SSH',        children: <SshTab onKeyChanged={onKeyChanged} connectedHosts={connectedHosts} allRemoteHosts={allRemoteHosts} /> },
          { key: 'llm',       label: 'LLM / API',  children: <LlmTab /> },
          { key: 'jumpHosts', label: 'Jump Hosts',  children: <JumpHostsTab onHostsChanged={onHostsChanged} /> },
          { key: 'skills',    label: 'Skills',     children: <SkillsTab /> },
          { key: 'filters',   label: 'Filters',    children: <FiltersTab /> },
          { key: 'credentials', label: 'Credentials', children: <CredentialsTab /> },
          { key: 'configSync', label: 'Config Sync', children: <ConfigSyncTab /> },
          { key: 'mailbox',   label: 'Mailbox',    children: <MailboxesTab /> },
          { key: 'triggers',  label: 'Triggers',   children: <TriggersTab /> },
          { key: 'teams',     label: 'Teams',      children: <TeamsTab /> },
        ]}
      />
    </Drawer>
  )
}
