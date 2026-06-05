import { useEffect, useRef, useState } from 'react'
import { Drawer, Form, Input, Button, Divider, Typography, Space, Tabs, Popconfirm, message, Tag, Tooltip, Select, Switch } from 'antd'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined, UploadOutlined, DownloadOutlined, UpOutlined, DownOutlined, ApiOutlined, LoadingOutlined, KeyOutlined, DesktopOutlined } from '@ant-design/icons'
import { bridge, type JumpHost, type TestResult, type RotateHostResult } from '../bridge'

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

// Keys the LLM form manages directly. Anything else under [llm] (e.g. the
// rotating-token api_key_command, or langserve's input_messages_key / *_key /
// tools_in_config knobs) is hand-edited in the TOML and must survive a Save —
// so handleSave preserves unmanaged keys instead of rebuilding the section.
const MANAGED_LLM_KEYS = ['provider', 'api_key', 'model', 'base_url', 'aws_region', 'system'] as const

// ── LLM tab ───────────────────────────────────────────────────────────────────

function LlmTab() {
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
      const llm = (cfg.llm ?? {}) as Record<string, string>
      const p = llm.provider ?? ''
      setProvider(p)
      form.setFieldsValue({
        provider: p,
        api_key: llm.api_key ?? '',
        model: llm.model ?? '',
        base_url: llm.base_url ?? '',
        aws_region: llm.aws_region ?? '',
        system: llm.system ?? '',
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
      // Start from the existing [llm] section so hand-edited advanced keys
      // (api_key_command, langserve input/output knobs) aren't wiped on Save;
      // set-or-delete only the keys the form owns.
      const llm: Record<string, unknown> = { ...((cfg.llm as Record<string, unknown>) ?? {}) }
      const next: Record<string, string> = {
        provider: v.provider ?? '',
        api_key: v.api_key ?? '',
        model: v.model ?? '',
        base_url: v.base_url ?? '',
        aws_region: v.aws_region ?? '',
        system: v.system?.trim() ?? '',
      }
      for (const k of MANAGED_LLM_KEYS) {
        if (next[k]) llm[k] = next[k]
        else delete llm[k]
      }
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
              help={provider === 'langserve' ? 'Bearer token. For rotating corporate tokens, set api_key_command in the config file instead.' : undefined}
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
      const ssh = (cfg.ssh ?? {}) as Record<string, string>
      setKeyCreatedAt(ssh.key_created_at ?? null)
      setKeyPath(ssh.identityFile ?? null)
      form.setFieldsValue({
        ssh_user: ssh.user ?? '',
        ssh_proxy: ssh.proxy ?? '',
        ssh_use_ssh_config: !!ssh.use_ssh_config,
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
      await bridge.save_config({ ...cfg, ssh: sshOut })
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
}

function toToml(hosts: JumpHost[]): string {
  return hosts.map(h => {
    const lines = [`[${h.name}]`, `hostname = "${h.hostname}"`]
    if (h.user) lines.push(`user = "${h.user}"`)
    if (h.port) lines.push(`port = ${h.port}`)
    if (h.identityFile) lines.push(`identity_file = "${h.identityFile}"`)
    if (h.group) lines.push(`group = "${h.group}"`)
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

  useEffect(() => {
    bridge.get_jump_hosts().then(setHosts)
  }, [])

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

  const cancel = () => { setEditingKey(null); form.resetFields() }

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
    if (editingKey === '') {
      await persist([...hosts, values as JumpHost])
    } else {
      await persist(hosts.map(h => h.name === editingKey ? { ...h, ...values } : h))
    }
    setEditingKey(null)
    form.resetFields()
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

      {editingKey !== null && (
        <div style={{ border: '1px solid #333', borderRadius: 8, padding: '14px 14px 6px', marginBottom: 12, background: 'rgba(255,255,255,0.02)' }}>
          <Text strong style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
            {editingKey === '' ? 'New jump host' : `Edit — ${editingKey}`}
          </Text>
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
            <Form.Item label="runspec path(s) on host">
              <Form.List name="runspec_paths">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...restField }) => (
                      <Space key={key} align="baseline" style={{ display: 'flex', marginBottom: 4 }}>
                        <Form.Item {...restField} name={name} style={{ marginBottom: 0, flex: 1 }}>
                          <Input placeholder="/home/user/.venv/bin/runspec" style={{ fontFamily: 'monospace', width: 312 }} />
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
            <Space style={{ marginTop: 2, marginBottom: 8 }}>
              <Button size="small" type="primary" icon={<CheckOutlined />} onClick={save}>Save</Button>
              <Button size="small" icon={<CloseOutlined />} onClick={cancel}>Cancel</Button>
            </Space>
          </Form>
        </div>
      )}

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
    </div>
  )
}

// ── Drawer ────────────────────────────────────────────────────────────────────

export function SettingsDrawer({ open, onClose, onHostsChanged, onKeyChanged, connectedHosts, allRemoteHosts }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width={480} open={open} onClose={onClose}>
      <Tabs
        size="small"
        items={[
          { key: 'ssh',       label: 'SSH',        children: <SshTab onKeyChanged={onKeyChanged} connectedHosts={connectedHosts} allRemoteHosts={allRemoteHosts} /> },
          { key: 'llm',       label: 'LLM / API',  children: <LlmTab /> },
          { key: 'jumpHosts', label: 'Jump Hosts',  children: <JumpHostsTab onHostsChanged={onHostsChanged} /> },
        ]}
      />
    </Drawer>
  )
}
