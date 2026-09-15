import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Drawer, Modal, Form, Input, InputNumber, Button, Divider, Typography, Space, Tabs, Popconfirm, message, Tag, Tooltip, Select, Switch, Collapse, Alert, Segmented, AutoComplete, Empty, Table, Slider, Progress } from 'antd'
import { PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined, UploadOutlined, DownloadOutlined, UpOutlined, DownOutlined, ApiOutlined, LoadingOutlined, KeyOutlined, DesktopOutlined, MailOutlined, ThunderboltOutlined, TeamOutlined, StopOutlined, ReloadOutlined, WarningOutlined, SearchOutlined, SoundOutlined } from '@ant-design/icons'
import { bridge, type JumpHost, type LocalVenvConfig, type HostGroup, type HostProfile, type Repo, type TestResult, type RotateHostResult, type Filter, type Credential, type Mailbox, type Watch, type TriggerListResult, type Team, type TeamsEvent, type SyncConfig, type SyncPullResult, type SeedStatus, type SeedOverride, type SeedApplyResult, type Runbook, type RunbookStep, type SavedRun, type Host, type DenylistPayload, type DenylistHostStatus, type SelfServiceEntry, type Runnable, type RoomHost, type McpServer, type McpHeaderCredential, type McpStatus, type McpServerStatus, type McpCredentialOption, type ToolVisibility, type NamedSound } from '../bridge'
import { CopyKeyModal } from '../views/CopyKeyModal'
import { CopyKeyAllModal } from '../views/CopyKeyAllModal'
import { PathInput } from '../views/PathInput'
import { credEnvNames, initialFormValues } from '../views/FormsView'
import { runnableHidden } from '../toolVisibility'
import {
  commandPathOptions, CredentialsSection, effectiveArgsForPath, RunnableArgsBlock,
  runnableSelectOptions, TargetFormItems, runnablesForTarget, splitFixedAndOpen,
  type Target,
} from '../views/RunnableAction'
import { MONO_FONT, RADIUS, useAccent, withAlpha } from '../ThemeContext'
import { useFeatures, visibleSettingsTabs } from '../features'

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
  { key: 'host_up', label: 'SSH host connected', def: 'synth:host_up' },
  { key: 'host_down', label: 'SSH host dropped', def: 'synth:host_down' },
  { key: 'room_up', label: 'Room reconnected', def: 'synth:room_up' },
  { key: 'room_down', label: 'Room connection dropped', def: 'synth:room_down' },
]
// Valid winsound SND_ALIAS names offered as alternatives to the synth set.
const SYSTEM_SOUND_ALIASES = ['SystemAsterisk', 'SystemExclamation', 'SystemHand', 'SystemQuestion', 'SystemDefault']
// Sentinel option value that opens the native file picker instead of being a token.
const PICK_FILE = '__pick_file__'
const fileBasename = (token: string) => token.slice('file:'.length).split(/[\\/]/).pop() || token
const soundOptions = (def: string, current: string, named: NamedSound[]) => [
  { value: def, label: 'Default (synthesized)' },
  ...SYSTEM_SOUND_ALIASES.map(a => ({ value: `system:${a}`, label: `Windows: ${a}` })),
  // Named sounds from the registry — pick one by id ("sound:<id>").
  ...named.map(s => ({ value: `sound:${s.id}`, label: `My sound: ${s.label}` })),
  ...(current.startsWith('file:') ? [{ value: current, label: `File: ${fileBasename(current)}` }] : []),
  { value: PICK_FILE, label: 'Custom file… (.wav / .mp3)' },
  { value: 'off', label: 'Off' },
]

export function SoundsTab() {
  const [enabled, setEnabled] = useState(true)
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [volume, setVolume] = useState(100)
  const [pack, setPack] = useState('')
  const [packInfo, setPackInfo] = useState<{ exists: boolean; covered: Record<string, string> } | null>(null)
  const [confirmNotif, setConfirmNotif] = useState(true)
  const [roomNotif, setRoomNotif] = useState(true)
  const [named, setNamed] = useState<NamedSound[]>([])
  const [newName, setNewName] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<{ toast_available: boolean; toast_reason: string; sound_available: boolean; window_found: boolean } | null>(null)

  const loadPackInfo = (dir: string) => {
    if (!dir) { setPackInfo(null); return }
    bridge.sound_pack_info(dir)
      .then(info => setPackInfo({ exists: info.exists, covered: info.covered }))
      .catch(() => setPackInfo(null))
  }

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
      setVolume(typeof sounds.volume === 'number' ? Math.max(0, Math.min(100, sounds.volume)) : 100)
      const p = typeof sounds.pack === 'string' ? sounds.pack : ''
      setPack(p)
      loadPackInfo(p)
      setConfirmNotif(con.confirm_notifications !== false)
      setRoomNotif(con.room_notifications !== false)
    })
    bridge.get_sounds().then(setNamed).catch(() => {})
  }, [])

  // The named-sound registry is its own file (import copies bytes, delete removes
  // them), so these persist immediately rather than batching into the main Save.
  const addNamedSound = () => {
    bridge.browse_sound_file().then(path => {
      if (!path) return
      bridge.import_sound(path, newName.trim()).then(res => {
        if (res.ok) { setNewName(''); bridge.get_sounds().then(setNamed).catch(() => {}) }
        else message.error(res.error || 'Could not add the sound')
      }).catch(() => message.error('Could not add the sound'))
    }).catch(() => {})
  }
  const deleteNamedSound = (id: string) => {
    bridge.delete_sound(id).then(res => {
      if (res.ok) setNamed(res.sounds ?? named.filter(s => s.id !== id))
      else message.error(res.error || 'Could not remove the sound')
    }).catch(() => message.error('Could not remove the sound'))
  }
  const renameNamedSound = (id: string, label: string) => {
    const rows = named.map(s => (s.id === id ? { ...s, label } : s))
    setNamed(rows)  // optimistic
    bridge.save_sounds(rows.map(({ id, label, file }) => ({ id, label, file }))).then(res => {
      if (!res.ok) message.error(res.error || 'Could not rename the sound')
    }).catch(() => {})
  }

  // A row's Select change: the "Custom file…" sentinel opens the native picker
  // (cancel keeps the previous choice); anything else is the token itself.
  const handleChoice = (key: string, v: string) => {
    if (v !== PICK_FILE) {
      setChoices(prev => ({ ...prev, [key]: v }))
      return
    }
    bridge.browse_sound_file().then(path => {
      if (path) setChoices(prev => ({ ...prev, [key]: `file:${path}` }))
    }).catch(() => {})
  }

  const handlePickPack = () => {
    bridge.browse_folder().then(dir => {
      if (!dir) return
      setPack(dir)
      loadPackInfo(dir)
    }).catch(() => {})
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      const con: Record<string, unknown> = { ...((cfg.console as Record<string, unknown>) ?? {}) }
      con.sounds = { enabled, volume, ...(pack ? { pack } : {}), ...choices }
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
      <span style={{ fontSize: 14 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Audio cues + desktop alerts so you stay aware while the console is minimized or in the background.
        Sounds play from the app, so they work even when minimized. The desktop <em>toast</em> needs the
        optional <Typography.Text code>notifications</Typography.Text> extra to be installed,
        but the <em>taskbar flash</em> and <em>sounds</em> do not. Each category can use its own
        <Typography.Text code>.wav</Typography.Text>/<Typography.Text code>.mp3</Typography.Text> file, or point a
        <em>sound pack</em> folder of category-named files at the whole set. Volume applies to synthesized and
        file cues; Windows system aliases follow the system mixer. Connection cues (host/room up &amp; down) are
        debounced — a flapping host won't chime more than once per half minute.
      </Typography.Paragraph>

      {row('Enable sounds', <Switch checked={enabled} onChange={setEnabled} />)}
      {row('Volume (synthesized + file cues)', (
        <Slider
          min={0} max={100} disabled={!enabled}
          value={volume} onChange={setVolume}
          style={{ width: 230, margin: '4px 6px' }}
        />
      ))}
      {/* Sound pack: a folder of category-named files (confirm.wav, host_down.mp3, …)
          applied to any category left on its default — one pick per personality. */}
      {row('Sound pack folder', (
        <Space size={4}>
          {pack && (
            <Typography.Text style={{ fontSize: 12, maxWidth: 150, fontFamily: MONO_FONT }} ellipsis={{ tooltip: pack }}>
              {pack}
            </Typography.Text>
          )}
          <Button size="small" disabled={!enabled} onClick={handlePickPack}>Browse…</Button>
          {pack && <Button size="small" type="text" onClick={() => { setPack(''); setPackInfo(null) }}>Clear</Button>}
        </Space>
      ))}
      {pack && packInfo && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -6 }}>
          {packInfo.exists
            ? `Covers ${Object.keys(packInfo.covered).length} of ${SOUND_CATEGORIES.length} categories (files named ${'confirm.wav'}, ${'host_down.mp3'}, …). Explicit picks below still win.`
            : 'Folder not found on this machine.'}
        </Typography.Paragraph>
      )}
      <Divider style={{ margin: '8px 0' }} />

      {/* Named sounds: an operator-added .wav/.mp3 saved with an id, reusable as
          a "My sound" pick below, in a trigger/schedule action, and by the agent's
          play_sound tool. The registry rides Config Sync; the audio files stay
          machine-local (imported copies), so a synced entry not present here shows
          "missing" and plays nothing. */}
      <Typography.Text strong style={{ fontSize: 14 }}>My sounds</Typography.Text>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
        Add your own sounds and name them, then reuse a name in the picks below, in a
        trigger or schedule action, and in what the agent can play. The registry syncs;
        the audio files stay on this machine (a synced name plays only where its file is present).
      </Typography.Paragraph>
      {named.map(s => (
        <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
          <Input
            size="small"
            value={s.label}
            onChange={e => setNamed(prev => prev.map(x => (x.id === s.id ? { ...x, label: e.target.value } : x)))}
            onBlur={e => renameNamedSound(s.id, e.target.value.trim() || s.id)}
            style={{ width: 200 }}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>
            sound:{s.id}
          </Typography.Text>
          {s.exists === false && (
            <Tooltip title="This sound's file isn't on this machine (it came from Config Sync). It will play only where the file is present.">
              <Tag color="warning" style={{ fontSize: 11, marginInlineEnd: 0 }}>missing</Tag>
            </Tooltip>
          )}
          <span style={{ flex: 1 }} />
          <Tooltip title="Preview this sound">
            <Button size="small" type="text" icon={<SoundOutlined />} onClick={() => bridge.preview_sound(`sound:${s.id}`).catch(() => {})} />
          </Tooltip>
          <Popconfirm title={`Remove "${s.label}"?`} onConfirm={() => deleteNamedSound(s.id)} okText="Remove" okButtonProps={{ danger: true }}>
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </div>
      ))}
      <Space size={4} style={{ marginTop: named.length ? 2 : 0 }}>
        <Input
          size="small"
          placeholder="Name (optional)"
          value={newName}
          onChange={e => setNewName(e.target.value)}
          onPressEnter={addNamedSound}
          style={{ width: 200 }}
        />
        <Button size="small" icon={<PlusOutlined />} onClick={addNamedSound}>Add from file…</Button>
      </Space>

      <Divider style={{ margin: '12px 0 8px' }} />
      {SOUND_CATEGORIES.map(c => row(
        c.label,
        <Space size={4}>
          <Select
            size="small"
            disabled={!enabled}
            value={choices[c.key] ?? c.def}
            onChange={v => handleChoice(c.key, v)}
            options={soundOptions(c.def, choices[c.key] ?? c.def, named)}
            popupMatchSelectWidth={false}
            style={{ width: 230 }}
          />
          <Tooltip title="Preview this cue (plays the current selection, saved or not)">
            <Button
              size="small" type="text" icon={<SoundOutlined />}
              onClick={() => bridge.test_sound(c.key, choices[c.key] ?? c.def).catch(() => {})}
            />
          </Tooltip>
        </Space>,
      ))}

      <Divider style={{ margin: '12px 0 8px' }} />
      <Typography.Text strong style={{ fontSize: 14 }}>Desktop notifications</Typography.Text>
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
        <pre style={{ marginTop: 12, fontSize: 13, background: 'rgba(127,127,127,0.12)', padding: 10, borderRadius: RADIUS.sm, whiteSpace: 'pre-wrap' }}>
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
                  style={{ fontFamily: MONO_FONT }}
                />
              </Form.Item>
              <Form.Item name="model" label="Model">
                <Select
                  showSearch
                  allowClear
                  placeholder={models.length ? 'Select a model' : 'Add a model above first'}
                  options={models.map(m => ({ value: m, label: m }))}
                  style={{ fontFamily: MONO_FONT }}
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
              <Input placeholder={provider === 'langserve' ? 'https://gateway.corp/my-chain' : 'https://...'} style={{ fontFamily: MONO_FONT }} />
            </Form.Item>
          )}
          {provider === 'bedrock' && (
            <>
              <Form.Item name="api_key" label="Proxy API key" help="Only needed if using a corporate Bedrock proxy">
                <Input.Password placeholder="token" />
              </Form.Item>
              <Form.Item name="aws_region" label="AWS region">
                <Input placeholder="us-east-1" style={{ fontFamily: MONO_FONT }} />
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
              label: <Text style={{ fontSize: 13 }}>Advanced</Text>,
              children: (
                <>
                  <Form.Item
                    name="api_key_command"
                    label="Token command"
                    help="Run this command to vend a short-lived token instead of a static key (rotating corporate creds). Its stdout is used as the token."
                  >
                    <Input placeholder="aws-vend-token --print" style={{ fontFamily: MONO_FONT }} />
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
                        <Text type="secondary" style={{ fontSize: 12 }}>LangServe wiring</Text>
                      </Divider>
                      <Form.Item name="auth_header" label="Auth header" help="HTTP header the token is sent in.">
                        <Input placeholder="Authorization" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <Form.Item name="auth_scheme" label="Auth scheme" help="Scheme prefix; blank sends the raw token.">
                        <Input placeholder="Bearer" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <Form.Item name="input_messages_key" label="Messages key" help="Key the chat history is sent under, inside input.">
                        <Input placeholder="messages" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <Form.Item name="input_tools_key" label="Tools key" help="Key the tool schemas are sent under.">
                        <Input placeholder="tools" style={{ fontFamily: MONO_FONT }} />
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
        {saved && <Text type="success" style={{ fontSize: 13 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 10 }}>
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

export function SshTab({ onKeyChanged, connectedHosts = [], allRemoteHosts = [] }: { onKeyChanged?: () => void; connectedHosts?: string[]; allRemoteHosts?: string[] }) {
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
  // Live per-host progress emitted by the backend during a rotation (via the
  // `runspec:rotate_progress` event) so a large-fleet rotation shows a moving
  // bar instead of a frozen "Rotating…" button.
  const [rotateProgress, setRotateProgress] = useState<{ done: number; total: number; host: string; phase: string } | null>(null)
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
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as { done?: number; total?: number; host?: string; phase?: string }
      setRotateProgress({
        done: Number(d.done ?? 0),
        total: Number(d.total ?? 0),
        host: String(d.host ?? ''),
        phase: String(d.phase ?? ''),
      })
    }
    window.addEventListener('runspec:rotate_progress', onProgress)
    return () => window.removeEventListener('runspec:rotate_progress', onProgress)
  }, [])
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
    setRotateProgress(null)
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
      // The per-host results panel now tells the full story; drop the live bar.
      setRotateProgress(null)
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
        <Input placeholder="http://proxy.corp:8080" style={{ fontFamily: MONO_FONT }} />
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
          label: <Text style={{ fontSize: 13 }}>Advanced</Text>,
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
                <Text type="secondary" style={{ fontSize: 12 }}>TCP keepalive</Text>
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
                <Text type="secondary" style={{ fontSize: 12 }}>Discovery</Text>
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
        <Text strong style={{ fontSize: 14 }}>SSH key</Text>
        {ageDays !== null && (
          <Tag
            color={ageDays >= 90 ? 'orange' : ageDays >= 75 ? 'gold' : 'success'}
            icon={<KeyOutlined />}
            style={{ margin: 0, fontSize: 12 }}
          >
            {ageDays === 0 ? 'created today' : `${ageDays}d old`}
          </Tag>
        )}
      </div>

      {ageDays !== null && ageDays >= 75 && (
        <div style={{
          padding: '6px 10px', borderRadius: RADIUS.sm, marginBottom: 10,
          background: ageDays >= 90 ? 'rgba(250,140,22,0.1)' : 'rgba(250,219,20,0.08)',
          border: `1px solid ${ageDays >= 90 ? 'rgba(250,140,22,0.3)' : 'rgba(250,219,20,0.3)'}`,
          fontSize: 13, color: ageDays >= 90 ? '#fa8c16' : '#d4b106',
        }}>
          {ageDays >= 90 ? `Key is ${ageDays} days old — rotation recommended.` : `Key is ${ageDays} days old — consider rotating soon.`}
        </div>
      )}

      {keyPath && (
        <div style={{ marginBottom: 10 }}>
          <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>
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
              <span style={{ fontSize: 13 }}>
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
                <span style={{ fontSize: 13 }}>
                  A new key will be pushed to every connected host using the current key,<br />
                  then verified before swapping. Active sessions stay alive throughout.
                </span>
              ) : (
                <span style={{ fontSize: 13, maxWidth: 320, display: 'inline-block' }}>
                  <strong style={{ color: '#fa8c16' }}>
                    {disconnected.length} host{disconnected.length !== 1 ? 's' : ''} disconnected:
                  </strong>{' '}
                  <span style={{ fontFamily: MONO_FONT }}>{disconnected.join(', ')}</span>.<br />
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

      {/* Live rotation progress — a moving bar while the fleet-wide push/verify runs */}
      {generating && rotateProgress && rotateProgress.total > 0 && (
        <div style={{ marginBottom: 10 }}>
          <Progress
            percent={Math.round((rotateProgress.done / rotateProgress.total) * 100)}
            size="small"
            status="active"
          />
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 2 }}>
            Pushing &amp; verifying {rotateProgress.done}/{rotateProgress.total} host{rotateProgress.total !== 1 ? 's' : ''}
            {rotateProgress.host ? (
              <> — last: <span style={{ fontFamily: MONO_FONT }}>{rotateProgress.host}</span>{rotateProgress.phase === 'failed' ? ' ✗' : ' ✓'}</>
            ) : '…'}
          </Text>
        </div>
      )}

      {/* Per-host rotation results */}
      {rotationResult && rotationResult.perHost.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          {rotationResult.perHost.map(h => {
            const icon = h.skipped ? '⚠' : h.verified ? '✓' : '✗'
            const color = h.skipped ? '#d4b106' : h.verified ? '#52c41a' : '#ff4d4f'
            return (
              <div key={h.host} style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 3 }}>
                <span style={{ color, fontFamily: MONO_FONT, fontSize: 12, flexShrink: 0 }}>{icon} {h.host}</span>
                {h.error && <Text type="secondary" style={{ fontSize: 11 }}>{h.error}</Text>}
              </div>
            )
          })}
          {!rotationResult.committed && (
            <Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
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
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            Run on each skipped/failing host to authorise the new key manually:
          </Text>
          <Typography.Paragraph
            copyable={{ tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: MONO_FONT, fontSize: 11, padding: '6px 10px',
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
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            Run on each host to authorise this key:
          </Text>
          <Typography.Paragraph
            copyable={{ tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: MONO_FONT, fontSize: 11, padding: '6px 10px',
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
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            Public key — add to each host's authorized_keys:
          </Text>
          <Typography.Paragraph
            copyable={{ text: storedPublicKey, tooltips: ['Copy', 'Copied!'] }}
            style={{
              fontFamily: MONO_FONT, fontSize: 11, padding: '6px 10px',
              background: 'rgba(255,255,255,0.04)', border: '1px solid #333',
              borderRadius: 4, wordBreak: 'break-all', margin: 0, color: '#52c41a',
            }}
          >
            {`echo "${storedPublicKey}" >> ~/.ssh/authorized_keys`}
          </Typography.Paragraph>
        </div>
      )}

      <Divider />

      <Text strong style={{ fontSize: 14 }}>Quick connect</Text>
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
        {saved && <Text type="success" style={{ fontSize: 13 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 10 }}>
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
  jump?: string
  profiles?: string[]
  room?: string
  room_port?: number
  room_user?: string
  room_via?: string
  room_exec_command?: string
  room_queue?: string
  room_catalog?: string[]
}

// Normalise raw host-form values into a host patch, ready to merge over the
// host being edited (`{ ...h, ...normalizeHostForm(values) }`) or spread into a
// new host. Pure + exported so the room/profile removal behaviour is unit-tested.
//
// IMPORTANT: to *clear* a field, set it to `undefined` — never `delete` it. The
// edit path merges `{ ...h, ...v }`, and a deleted key is absent from `v`, so
// the spread falls back to the originally-loaded host's value (the room /
// profiles you cleared would "come back" after save). `undefined` overwrites,
// and the bridge drops undefined keys on serialize, so the backend receives a
// host without the field and removes it.
export function normalizeHostForm(values: HostFormValues): HostFormValues {
  const v: HostFormValues = { ...values }
  v.jump = (v.jump ?? '').toString().trim() || undefined
  v.profiles = Array.isArray(v.profiles)
    ? v.profiles.map(s => s.toString().trim()).filter(Boolean)
    : undefined
  if (!v.profiles?.length) v.profiles = undefined
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
  // optional. room_port alone is enough. No port → clear every room field so
  // removing a room through the editor actually removes it.
  if (v.room_port != null && `${v.room_port}` !== '') {
    v.room_port = Number(v.room_port)
  } else {
    v.room = undefined
    v.room_queue = undefined
    v.room_port = undefined
    v.room_user = undefined
    v.room_via = undefined
    v.room_exec_command = undefined
  }
  // The catalog allowlist only means anything on a host that runs the room.
  if (!v.room_port || !v.room_catalog?.length) v.room_catalog = undefined
  return v
}

function toToml(hosts: JumpHost[]): string {
  return hosts.map(h => {
    const lines = [`[${h.name}]`, `hostname = "${h.hostname}"`]
    if (h.user) lines.push(`user = "${h.user}"`)
    if (h.port) lines.push(`port = ${h.port}`)
    if (h.identityFile) lines.push(`identity_file = "${h.identityFile}"`)
    if (h.group) lines.push(`group = "${h.group}"`)
    if (h.jump) lines.push(`jump = "${h.jump}"`)
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
      <Text type="secondary" style={{ fontSize: 13 }}>
        Groups are shared across consoles (Config Sync) so a runbook targeting a
        group behaves the same everywhere. Hosts pick their group from this list.
      </Text>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((g, i) => (
          <Space key={g.name} align="baseline" style={{ display: 'flex' }}>
            <Input
              size="small"
              style={{ width: 150, fontFamily: MONO_FONT }}
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
              <MinusCircleOutlined style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
            </Popconfirm>
          </Space>
        ))}
        {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No groups yet" />}
        <Space.Compact style={{ marginTop: 4 }}>
          <Input
            size="small"
            style={{ width: 200, fontFamily: MONO_FONT }}
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
      <Text type="secondary" style={{ fontSize: 13 }}>
        A profile is a named working-set of hosts. You load one at a time — only its
        hosts stay connected. Definitions are shared across consoles (Config Sync);
        which one you have loaded is local to this machine.
      </Text>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((p, i) => (
          <Space key={p.name} align="baseline" style={{ display: 'flex' }}>
            <Input
              size="small"
              style={{ width: 150, fontFamily: MONO_FONT }}
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
              <MinusCircleOutlined style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
            </Popconfirm>
          </Space>
        ))}
        {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No profiles yet" />}
        <Space.Compact style={{ marginTop: 4 }}>
          <Input
            size="small"
            style={{ width: 200, fontFamily: MONO_FONT }}
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

function LocalVenvsTab() {
  const [globs, setGlobs] = useState<string[]>([])
  const [resolved, setResolved] = useState<LocalVenvConfig['resolved']>([])
  const [ownVenv, setOwnVenv] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const apply = useCallback((cfg: LocalVenvConfig) => {
    setGlobs(cfg.globs ?? [])
    setResolved(cfg.resolved ?? [])
    setOwnVenv(cfg.own_venv ?? '')
  }, [])

  useEffect(() => { bridge.get_local_venv_config().then(apply) }, [apply])

  const setGlob = (i: number, v: string) => setGlobs(gs => gs.map((g, j) => (j === i ? v : g)))
  const removeGlob = (i: number) => setGlobs(gs => gs.filter((_, j) => j !== i))
  const addGlob = () => setGlobs(gs => [...gs, ''])

  const handleSave = async () => {
    setSaving(true)
    try {
      // Drop blank rows so an empty field doesn't trip the backend validator.
      const cleaned = globs.map(g => g.trim()).filter(Boolean)
      const cfg = await bridge.save_local_venv_config(cleaned)
      apply(cfg)
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Could not save local venvs')
    } finally { setSaving(false) }
  }

  return (
    <div style={{ maxWidth: 620 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Discover runnables in <strong>more than one local Python venv</strong> — the same multi-venv support remote hosts
        have, for this machine. The console's own venv is always discovered; add glob patterns below whose matched
        directories are each scanned for a venv (probed for <code>Scripts\runspec.exe</code> / <code>bin/runspec</code>).
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        These globs live in <code>config.toml</code>, so they ride <strong>Config Sync</strong> and the config seed — one
        versioned convention (e.g. <code>C:\venvs\*</code>) works across a fleet of identical machines, and each console
        resolves its own real venvs. The matches shown below are specific to <strong>this</strong> machine.
      </Typography.Paragraph>

      <Typography.Text strong style={{ fontSize: 14 }}>Venv search globs</Typography.Text>
      <div style={{ marginTop: 8 }}>
        {globs.map((g, i) => (
          <Space key={i} align="baseline" style={{ display: 'flex', marginBottom: 6 }}>
            <Input
              value={g}
              onChange={e => setGlob(i, e.target.value)}
              placeholder="C:\venvs\*"
              style={{ width: 420, fontFamily: MONO_FONT }}
            />
            <MinusCircleOutlined onClick={() => removeGlob(i)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
          </Space>
        ))}
        <Button type="dashed" size="small" onClick={addGlob} icon={<PlusOutlined />} style={{ marginTop: 2 }}>Add glob</Button>
      </div>

      <Divider style={{ margin: '16px 0' }} />
      <Typography.Text strong style={{ fontSize: 14 }}>Resolved on this machine</Typography.Text>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
        The venvs the console will discover locally (own venv first). Save to refresh after editing the globs.
      </Typography.Paragraph>
      {resolved.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No local venvs resolved yet" />
      ) : (
        <div>
          {resolved.map(r => (
            <div key={r.path} style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 4 }}>
              <Tag color="blue">{r.venv_name}</Tag>
              <Text style={{ fontSize: 13, fontFamily: MONO_FONT, wordBreak: 'break-all' }}>{r.path}</Text>
              {r.path === ownVenv && <Text type="secondary" style={{ fontSize: 12 }}>(console's own venv)</Text>}
            </div>
          ))}
        </div>
      )}

      <Divider style={{ margin: '16px 0' }} />
      <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
    </div>
  )
}

// One row per distinct runnable name (the agent hides by name across hosts).
interface RunnableRow { name: string; group: string; host: string; hosts: number }

function AgentToolsTab() {
  const [rows, setRows] = useState<RunnableRow[]>([])
  const [fromAgent, setFromAgent] = useState<string[]>([])
  const [fromAll, setFromAll] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    // The runnable list is scoped to the active host profile (the backend cache is
    // rebuilt from _active_host_entries on a profile switch), so re-fetch it when
    // discovery updates or hosts change — otherwise the tab shows the previous
    // profile's runnables until the app restarts. Only `rows` is reloaded; pending
    // (unsaved) visibility toggles in fromAgent/fromAll are left untouched.
    const loadRows = () => {
      bridge.get_runnables('all').then(rs => {
        const byName = new Map<string, RunnableRow>()
        for (const r of rs) {
          const ex = byName.get(r.name)
          if (ex) ex.hosts += 1
          else byName.set(r.name, { name: r.name, group: r.group ?? '', host: r.host ?? '', hosts: 1 })
        }
        setRows([...byName.values()].sort((a, b) => a.name.localeCompare(b.name)))
      }).catch(() => {})
    }
    loadRows()
    bridge.get_tool_visibility().then(v => {
      setFromAgent(v.hidden_from_agent ?? [])
      setFromAll(v.hidden_from_all ?? [])
    })
    window.addEventListener('runspec:runnables_updated', loadRows)
    window.addEventListener('runspec:hosts_updated', loadRows)
    return () => {
      window.removeEventListener('runspec:runnables_updated', loadRows)
      window.removeEventListener('runspec:hosts_updated', loadRows)
    }
  }, [])

  // Effective state for a row, matching name/group/host against both lists.
  const stateFor = (r: RunnableRow): 'visible' | 'agent' | 'all' => {
    if (runnableHidden(r.name, r.group, r.host, fromAll)) return 'all'
    if (runnableHidden(r.name, r.group, r.host, fromAgent)) return 'agent'
    return 'visible'
  }
  // A row hidden by a non-exact-name pattern (a glob or a group:/host: rule) —
  // its per-row control is read-only; the operator edits the Advanced lists.
  const viaPattern = (r: RunnableRow): 'agent' | 'all' | null => {
    if (fromAll.includes(r.name)) return null
    if (runnableHidden(r.name, r.group, r.host, fromAll)) return 'all'
    if (fromAgent.includes(r.name)) return null
    if (runnableHidden(r.name, r.group, r.host, fromAgent)) return 'agent'
    return null
  }

  const setRowState = (name: string, next: 'visible' | 'agent' | 'all') => {
    const drop = (xs: string[]) => xs.filter(x => x !== name)
    const add = (xs: string[]) => (xs.includes(name) ? xs : [...xs, name])
    if (next === 'visible') { setFromAgent(drop); setFromAll(drop) }
    else if (next === 'agent') { setFromAgent(xs => add(drop(xs))); setFromAll(drop) }
    else { setFromAll(xs => add(drop(xs))); setFromAgent(drop) }
  }

  const parseList = (text: string) =>
    text.split(/[\n,]/).map(s => s.trim()).filter(Boolean)

  const handleSave = async () => {
    setSaving(true)
    try {
      const payload: ToolVisibility = {
        hidden_from_agent: [...new Set(fromAgent.map(s => s.trim()).filter(Boolean))],
        hidden_from_all: [...new Set(fromAll.map(s => s.trim()).filter(Boolean))],
      }
      const out = await bridge.save_tool_visibility(payload)
      setFromAgent(out.hidden_from_agent ?? [])
      setFromAll(out.hidden_from_all ?? [])
      window.dispatchEvent(new CustomEvent('runspec:tool_visibility_changed'))
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Could not save tool visibility')
    } finally { setSaving(false) }
  }

  const q = query.trim().toLowerCase()
  const shown = q ? rows.filter(r => r.name.toLowerCase().includes(q) || r.group.toLowerCase().includes(q)) : rows
  const hiddenCount = rows.filter(r => stateFor(r) !== 'visible').length

  const columns = [
    {
      title: 'Runnable', dataIndex: 'name', key: 'name',
      render: (v: string, r: RunnableRow) => (
        <span><code>{v}</code>{r.hosts > 1 && <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>×{r.hosts} hosts</Text>}</span>
      ),
    },
    { title: 'Group', dataIndex: 'group', key: 'group', render: (v: string) => v ? <Tag>{v}</Tag> : <Text type="secondary">—</Text> },
    {
      title: 'Visibility', key: 'vis', width: 320,
      render: (_: unknown, r: RunnableRow) => {
        const via = viaPattern(r)
        return (
          <Space size="small">
            <Segmented
              size="small"
              value={stateFor(r)}
              disabled={via !== null}
              onChange={val => setRowState(r.name, val as 'visible' | 'agent' | 'all')}
              options={[
                { label: 'Visible', value: 'visible' },
                { label: 'Hide from agent', value: 'agent' },
                { label: 'Hide everywhere', value: 'all' },
              ]}
            />
            {via && <Tooltip title="Hidden by a pattern in the Advanced lists below — edit it there."><Tag color="orange">via pattern</Tag></Tooltip>}
          </Space>
        )
      },
    },
  ]

  return (
    <div style={{ maxWidth: 780 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Hide runnables you can't use from the <strong>agent's toolset</strong> — they stop bloating the model's tool list
        and it stops trying tools that will never work (e.g. Office 365 runnables where Microsoft Graph is blocked).
        <strong> Hide from agent</strong> removes a runnable from the chat/trigger/schedule agent only (still usable in the
        Forms tab); <strong>Hide everywhere</strong> also removes it from the Forms tab and the room/self-service catalogs.
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        This is <strong>curation, not a safety block</strong> — a hidden runnable is still installed and directly runnable
        (that's what the Denylist is for). The two lists live in <code>config.toml</code>, so they ride
        <strong> Config Sync</strong> and the config seed — a baseline can hide capabilities fleet-wide from one source.
      </Typography.Paragraph>

      <Space style={{ marginBottom: 8 }}>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="Filter runnables…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          style={{ width: 280 }}
        />
        <Text type="secondary" style={{ fontSize: 13 }}>{hiddenCount} hidden of {rows.length}</Text>
      </Space>

      {rows.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No runnables discovered yet" />
      ) : (
        <Table<RunnableRow>
          size="small"
          rowKey="name"
          dataSource={shown}
          columns={columns}
          pagination={{ pageSize: 12, size: 'small', hideOnSinglePage: true }}
        />
      )}

      <Collapse
        ghost
        style={{ marginTop: 8 }}
        items={[{
          key: 'advanced',
          label: 'Advanced — glob patterns',
          children: (
            <div>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                One pattern per line (or comma-separated). Globs match the runnable name (<code>outlook-*</code>); prefix
                with <code>group:</code> or <code>host:</code> to hide a whole group/host (<code>group:m365</code>).
              </Typography.Paragraph>
              <Text strong style={{ fontSize: 13 }}>Hidden from agent</Text>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 8 }}
                style={{ fontFamily: MONO_FONT, fontSize: 13, marginTop: 4 }}
                value={fromAgent.join('\n')}
                onChange={e => setFromAgent(parseList(e.target.value))}
                placeholder="outlook-*"
              />
              <Text strong style={{ fontSize: 13, display: 'block', marginTop: 10 }}>Hidden everywhere</Text>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 8 }}
                style={{ fontFamily: MONO_FONT, fontSize: 13, marginTop: 4 }}
                value={fromAll.join('\n')}
                onChange={e => setFromAll(parseList(e.target.value))}
                placeholder="group:m365"
              />
            </div>
          ),
        }]}
      />

      <Divider style={{ margin: '16px 0' }} />
      <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
    </div>
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
    // Coerce the room port, clear empty fields, and (crucially) clear removed
    // fields with `undefined` not delete — see normalizeHostForm.
    const v = normalizeHostForm(values)
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
        <Text type="secondary" style={{ fontSize: 13 }}>
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
                <Input placeholder="prod-eu-1" style={{ fontFamily: MONO_FONT }} disabled={editingKey !== ''} />
              </Form.Item>
              <Form.Item name="hostname" label="Hostname" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="hostname.company.com" style={{ fontFamily: MONO_FONT }} />
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
                        <MinusCircleOutlined onClick={() => remove(name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                      </Space>
                    ))}
                    <Button type="dashed" size="small" onClick={() => add()} icon={<PlusOutlined />} style={{ marginTop: 2 }}>Add path</Button>
                  </>
                )}
              </Form.List>
            </Form.Item>
            <Form.Item
              name="jump"
              label="Jump host (ProxyJump)"
              help="Reach this host through a jump host using the system ssh client, so ~/.ssh/config legacy algorithms apply (e.g. HostKeyAlgorithms +ssh-rsa for CentOS 6). An SSH host/alias as `ssh -J` expects. Needs an OpenSSH client on this machine. Leave blank for the normal (paramiko) transport."
            >
              <Input placeholder="bastion  (or user@bastion)" style={{ fontFamily: MONO_FONT }} />
            </Form.Item>
            <Form.Item
              name="group"
              label={
                <Space size={6}>
                  Group
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0, height: 'auto', fontSize: 13 }}
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
                    style={{ padding: 0, height: 'auto', fontSize: 13 }}
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
                <Input placeholder="defaults to console-room connect" style={{ fontFamily: MONO_FONT }} />
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
                help='The helpdesk queue this host staffs — becomes the room "helpdesk/<name>". Defaults to "helpdesk"; set a distinct name on each host so two rooms do not collide (a same-named room is skipped). Letters, digits, dashes — no spaces or "/" (it is a URL token; use a dash, e.g. "it-support").'
                rules={[
                  {
                    // Mirror the server's valid_queue: a queue token must not
                    // contain whitespace or the namespace separator, or no
                    // helpdesk spec is built and the room silently never connects.
                    validator: (_, value) =>
                      !value || /^[^\s/]+$/.test(String(value))
                        ? Promise.resolve()
                        : Promise.reject(new Error('No spaces or "/" — use a dash, e.g. "it-support"')),
                  },
                ]}
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
        <Text type="secondary" style={{ fontSize: 13 }}>No jump hosts configured. Add one to get started.</Text>
      )}

      {(() => {
        const groups = buildGroups(hosts)
        return groups.map(({ name: groupName, hosts: groupHosts }, groupIdx) => (
          <div key={groupName || '__ungrouped__'} style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 6 }}>
              <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em', flex: 1 }}>
                {groupName || 'Hosts'}
              </Text>
              <Button type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />} onClick={() => moveGroupUp(groupIdx)} disabled={isEditing || groupIdx === 0} style={{ padding: '0 4px', color: 'var(--rs-text-faint)', height: 18 }} />
              <Button type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />} onClick={() => moveGroupDown(groupIdx)} disabled={isEditing || groupIdx === groups.length - 1} style={{ padding: '0 4px', color: 'var(--rs-text-faint)', height: 18 }} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {groupHosts.map((h, hostIdx) => (
                <div key={h.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a', background: editingKey === h.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
                    <Button type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />} onClick={() => moveHostUp(h)} disabled={isEditing || hostIdx === 0} style={{ padding: '0 3px', color: 'var(--rs-text-faint)', height: 14, lineHeight: 1 }} />
                    <Button type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />} onClick={() => moveHostDown(h)} disabled={isEditing || hostIdx === groupHosts.length - 1} style={{ padding: '0 3px', color: 'var(--rs-text-faint)', height: 14, lineHeight: 1 }} />
                  </div>
                  <Text style={{ fontFamily: MONO_FONT, fontSize: 13, minWidth: 110, flexShrink: 0 }}>{h.name}</Text>
                  <Text type="secondary" title={`${h.user ? h.user + '@' : ''}${h.hostname}${h.port ? ':' + h.port : ''}`} style={{ fontFamily: MONO_FONT, fontSize: 12, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
                      <Tooltip title={<pre style={{ margin: 0, fontSize: 12, whiteSpace: 'pre-wrap', maxWidth: 340 }}>{detail}</pre>} placement="left">
                        <Tag color={color} style={{ margin: 0, cursor: 'default', fontSize: 12 }}>{label}</Tag>
                      </Tooltip>
                    )
                  })()}
                  <Tooltip title="Test connection">
                    <Button type="text" size="small" icon={testingHost === h.name ? <LoadingOutlined /> : <ApiOutlined />} onClick={() => handleTest(h.name)} disabled={isEditing || testingHost !== null} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
                  </Tooltip>
                  <Tooltip title="Copy SSH public key (authorize this machine)">
                    <Button type="text" size="small" icon={<KeyOutlined />} onClick={() => setCopyKeyHost(h.name)} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
                  </Tooltip>
                  <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(h)} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
                  <Popconfirm title={`Remove ${h.name}?`} onConfirm={() => remove(h.name)} okText="Remove" cancelText="Cancel" placement="left">
                    <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} danger />
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
        <Text type="secondary" style={{ fontSize: 13 }}>
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
              <Input placeholder="id-lines" style={{ fontFamily: MONO_FONT }} disabled={editingKey !== ''} />
            </Form.Item>
            <Form.Item
              name="pipeline"
              label="Pipeline"
              rules={[{ required: true, message: 'Required' }]}
              help="Runs on the source host; the file arrives on stdin. Use {{arg}} for a value you'll enter at transfer time. e.g. grep -F -- {{arg}} | cut -d, -f1,4"
            >
              <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="grep -F -- {{arg}}" style={{ fontFamily: MONO_FONT }} />
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
        <Text type="secondary" style={{ fontSize: 13 }}>No filters yet. Add one to shrink large transfers at the source.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {filters.map(f => (
          <div key={f.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a', background: editingKey === f.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <Text style={{ fontFamily: MONO_FONT, fontSize: 13, minWidth: 90, flexShrink: 0 }}>{f.name}</Text>
            {isParameterized(f.pipeline) && <Tag style={{ margin: 0, fontSize: 11 }}>arg</Tag>}
            <Text type="secondary" title={f.pipeline} style={{ fontFamily: MONO_FONT, fontSize: 12, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {f.pipeline}
            </Text>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(f)} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
            <Popconfirm title={`Remove ${f.name}?`} onConfirm={() => remove(f.name)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} danger />
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
  const accent = useAccent()
  const [entries, setEntries] = useState<SelfServiceEntry[]>([])
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [roomHosts, setRoomHosts] = useState<RoomHost[]>([])
  const [activeRoom, setActiveRoom] = useState('')
  const [switchingRoom, setSwitchingRoom] = useState(false)
  const [saving, setSaving] = useState(false)
  const [query, setQuery] = useState('')
  // null = not yet touched → show every group open. Once the operator collapses a
  // panel we honour their choice (search overrides both, see activeKeys below).
  const [openKeys, setOpenKeys] = useState<string[] | null>(null)

  const load = useCallback(() => {
    bridge.get_self_service().then(setEntries)
    bridge.get_runnables('all').then(setRunnables)
    bridge.get_room_hosts().then(setRoomHosts)
    bridge.get_active_room().then(setActiveRoom)
  }, [])
  useEffect(() => { load() }, [load])

  // Keep the room selector fresh: a host edited elsewhere (e.g. its room config
  // removed) must drop out of the dropdown without an app restart. hosts_updated
  // fires on every host save; room_connection catches live connect/drop flips.
  useEffect(() => {
    const reloadRooms = () => {
      bridge.get_room_hosts().then(setRoomHosts)
      bridge.get_active_room().then(setActiveRoom)
    }
    window.addEventListener('runspec:hosts_updated', reloadRooms)
    window.addEventListener('runspec:room_connection', reloadRooms)
    return () => {
      window.removeEventListener('runspec:hosts_updated', reloadRooms)
      window.removeEventListener('runspec:room_connection', reloadRooms)
    }
  }, [])

  const changeRoom = async (host: string) => {
    setSwitchingRoom(true)
    try {
      const res = await bridge.set_active_room(host)
      setActiveRoom(res.active)
    } finally { setSwitchingRoom(false) }
  }

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
      <div key={it.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 9px', borderRadius: RADIUS.sm, border: `1px solid ${on ? withAlpha(accent, 0.5) : 'var(--rs-border)'}`, background: on ? withAlpha(accent, 0.1) : 'transparent', minWidth: 0 }}>
        <Switch size="small" checked={on} disabled={saving} onChange={v => toggle(it.name, v)} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div title={it.name} style={{ fontFamily: MONO_FONT, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.name}</div>
          {it.description && <div title={it.description} style={{ fontSize: 11, color: 'var(--rs-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.description}</div>}
        </div>
        {it.missing && <Tag color="orange" style={{ margin: 0, fontSize: 11 }}>missing</Tag>}
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
          <Tag color={isOrphan ? 'orange' : 'geekblue'} style={{ margin: 0, fontFamily: MONO_FONT, fontSize: 12 }}>
            {isOrphan ? 'not discovered' : gk}
          </Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>{onCount}/{all.length} on</Text>
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
      {roomHosts.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 4 }}>Connected room</Text>
          <Space wrap>
            <Select
              size="small"
              style={{ minWidth: 280 }}
              value={activeRoom}
              loading={switchingRoom}
              disabled={switchingRoom}
              onChange={changeRoom}
              options={[
                { value: '', label: 'None — not connected' },
                ...roomHosts.map(r => ({
                  value: r.host,
                  label: `${r.host} · ${r.queue || r.room || 'room'}`,
                })),
              ]}
            />
          </Space>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
            {roomHosts.length > 1
              ? 'These hosts all run the same helpdesk, for redundancy. This console connects to one at a time — pick which host staffs it (machine-local; for failover, switch here and repoint your front door to that host).'
              : 'The room-configured host this console connects to (machine-local).'}
          </Text>
        </div>
      )}
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 12 }}>
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
        <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{totalOn} enabled · {totalDiscovered} discovered</Text>
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
        <Text type="secondary" style={{ fontSize: 13 }}>
          Repositories the agent may read and <strong>locally</strong> edit (the <code>repo_*</code> tools). This list is the allowlist — a repo not here is unreachable. Saved to <code>runspec_repos.toml</code>; rides Config Sync. Edits are local only (no commit/push).
        </Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add repo</Button>
      </div>

      {engineMissing && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="Git engine not installed"
          description={<span style={{ fontSize: 13 }}>Install the optional <code>git-sync</code> extra to clone/read repos.</span>} />
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
            <Input placeholder="payments" style={{ fontFamily: MONO_FONT }} disabled={editingKey !== ''} />
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
        <Text type="secondary" style={{ fontSize: 13 }}>No repos yet. Add one so the agent can read and edit its code.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {repos.map(r => (
          <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a', background: editingKey === r.name ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <Text style={{ fontFamily: MONO_FONT, fontSize: 13, minWidth: 90, flexShrink: 0 }}>{r.name}</Text>
            <Tag style={{ margin: 0, fontSize: 11 }}>{r.ref}</Tag>
            {r.checked_out
              ? <Tag color="green" style={{ margin: 0, fontSize: 11 }} title={`Checked out at ${r.commit}${r.checkout_dir ? ` — ${r.checkout_dir}` : ''}`}>synced {r.commit}</Tag>
              : <Tag style={{ margin: 0, fontSize: 11 }}>not synced</Tag>}
            {r.path && <Tag color="blue" style={{ margin: 0, fontSize: 11 }} title={`Custom checkout: ${r.path}`}>custom</Tag>}
            <Text type="secondary" title={`${r.url}${credLabel(r.credential_id) ? ` — auth: ${credLabel(r.credential_id)}` : ''}`} style={{ fontFamily: MONO_FONT, fontSize: 12, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.url}
            </Text>
            <Tooltip title="Sync now (clone / fetch)">
              <Button type="text" size="small" icon={syncing === r.name ? <LoadingOutlined /> : <ReloadOutlined />} onClick={() => sync(r.name)} disabled={isEditing || engineMissing || syncing !== null} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
            </Tooltip>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(r)} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
            <Popconfirm title={`Remove ${r.name}?`} description={r.path ? 'Your custom dev-folder checkout is left untouched.' : 'The app-data checkout on disk is also deleted.'} onConfirm={() => remove(r.name)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} danger />
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
        <Text type="secondary" style={{ fontSize: 13 }}>
          Secrets are stored in the OS keychain{backend?.backend ? <> (<code>{backend.backend}</code>)</> : null}; only metadata is saved to <code>runspec_credentials.toml</code>.
        </Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={isEditing}>Add</Button>
      </div>

      {backend && !backend.available && (
        <Text type="warning" style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
          No OS keychain is available, so secret values can't be stored. Install the optional <code>credentials</code> extra on a desktop session. Metadata-only entries still save.
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
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  An SSH-key credential authenticates a Config Sync SSH repo with its own deploy key (use it in Settings → Config Sync). The key files stay on disk — only the paths are saved.
                </Text>
                <Form.Item name="private_key" label="Private key path" rules={[{ required: true, message: 'Required' }]} tooltip="Path to the private key file (e.g. an ed25519 deploy key). Passed to the SSH client as the identity file.">
                  <Input placeholder="~/.ssh/runspec_ed25519" style={{ fontFamily: MONO_FONT }} autoComplete="off"
                    addonAfter={<a onClick={() => pickKey('private_key')}>Browse</a>} />
                </Form.Item>
                <Form.Item name="public_key" label="Public key path" tooltip="Optional — the matching .pub file. Informational; not required to connect.">
                  <Input placeholder="~/.ssh/runspec_ed25519.pub" style={{ fontFamily: MONO_FONT }} autoComplete="off"
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
                <div style={{ marginBottom: 12, padding: '8px 10px', borderRadius: RADIUS.sm, background: 'rgba(128,128,128,0.08)' }}>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                    The <b>label</b> drives the env var prefix. When this credential is passed to a run it sets:
                  </Text>
                  <div style={{ marginTop: 4 }}>
                    {envNames.length
                      ? envNames.map((n, i) => <span key={n}>{i > 0 && ' '}<code style={{ fontSize: 12 }}>{n}</code></span>)
                      : <Text type="secondary" style={{ fontSize: 12 }}>— (give the credential a label)</Text>}
                  </div>
                  <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                    Name it to match what the runnable reads from the environment.
                  </Text>
                </div>
                <Divider style={{ margin: '4px 0 12px' }} />
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  Binding (agent only) — which runnable arguments these values fill for agent-driven runs, and where the credential may be applied (glob patterns; blank = any). The Forms path uses the env vars above, not these.
                </Text>
                <Form.Item name="username_arg" label="Username → argument" tooltip="Runnable arg name that receives the username (passed normally).">
                  <Input placeholder="service-username" style={{ fontFamily: MONO_FONT }} autoComplete="off" />
                </Form.Item>
                <Form.Item name="secret_arg" label="Secret → argument" tooltip="Runnable arg name that receives the secret (passed via env, never argv).">
                  <Input placeholder="user-password" style={{ fontFamily: MONO_FONT }} autoComplete="off" />
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
        <Text type="secondary" style={{ fontSize: 13 }}>No credentials yet. Add one to let a runnable or relay use a stored identity.</Text>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {creds.map(c => (
          <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a', background: editingId === c.id ? 'rgba(255,255,255,0.03)' : 'transparent' }}>
            <KeyOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 13, flexShrink: 0 }} />
            <Text style={{ fontSize: 13, minWidth: 110, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.label}</Text>
            <Tag style={{ margin: 0, fontSize: 11 }}>{c.kind}</Tag>
            {c.has_secret
              ? <Tag color="green" style={{ margin: 0, fontSize: 11 }}>secret set</Tag>
              : <Tag style={{ margin: 0, fontSize: 11 }}>no secret</Tag>}
            <Text type="secondary" title={c.username} style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {c.username}
            </Text>
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(c)} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
            <Popconfirm title={`Remove ${c.label}?`} onConfirm={() => remove(c.id)} okText="Remove" cancelText="Cancel" placement="left">
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={isEditing} style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} danger />
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

  // A mailbox/watch's folders as an editable string[] — accepts the new `folders`
  // list and falls back to the legacy single `folder`. Empty = Inbox only.
  const foldersOf = (mb: { folders?: string[]; folder?: string } | null | undefined): string[] =>
    (mb?.folders && mb.folders.length ? mb.folders : mb?.folder ? [mb.folder] : [])
  // Drop blanks and a lone "Inbox" (the default) so it round-trips cleanly.
  const cleanFolders = (fs: string[] | undefined): string[] => {
    const out = (fs ?? []).map(f => f.trim()).filter(Boolean)
    return out.length === 1 && out[0].toLowerCase() === 'inbox' ? [] : out
  }

  useEffect(() => {
    bridge.get_mailbox().then(mb => {
      setEnabled(mb !== null)
      form.setFieldsValue({
        address: mb?.address ?? '',
        folders: foldersOf(mb),
        label: mb?.label ?? '',
        once_per_thread: !!mb?.once_per_thread,
      })
    }).catch(console.error)
  }, [form])
  useEffect(() => {
    bridge.get_watches().then(ws => setWatches(ws.map(w => ({ ...w, folders: foldersOf(w) })))).catch(console.error)
  }, [])
  useEffect(() => { bridge.config_dir().then(setConfigDir) }, [])

  const setWatch = (i: number, patch: Partial<Watch>) =>
    setWatches(ws => ws.map((w, j) => (j === i ? { ...w, ...patch } : w)))
  const addWatch = () => setWatches(ws => [...ws, { address: '', folders: [], label: '' }])
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
          folders: cleanFolders(v.folders),
          label: (v.label ?? '').trim(),
          once_per_thread: !!v.once_per_thread,
        })
      }
      await bridge.save_watches(watches.map(w => ({
        address: (w.address ?? '').trim(),
        folders: cleanFolders(w.folders),
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
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 12 }}>
        Monitor one Outlook inbox for the <code>new_mail</code> trigger source. Trigger
        rules &mdash; what to do with matched mail &mdash; live in <code>runspec_triggers.toml</code>.
      </Text>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <Switch size="small" checked={enabled} onChange={setEnabled} />
        <MailOutlined style={{ color: 'var(--rs-text-muted)' }} />
        <span style={{ fontSize: 14 }}>Monitor a mailbox</span>
      </div>

      {enabled && (
        <Form form={form} layout="vertical" size="small" style={{ marginTop: 4 }}>
          <Form.Item
            name="address"
            label="Mailbox email address"
            help="The account or shared mailbox to monitor. Leave blank for your own default account."
          >
            <Input placeholder="helpdesk@corp.com (blank = my mailbox)" style={{ fontFamily: MONO_FONT }} />
          </Form.Item>
          <Form.Item
            name="folders"
            label="Folders (optional)"
            help={'Folder paths under this mailbox, /-separated (type one and press Enter for each). Leave empty to monitor the Inbox only. Each folder is watched independently and a trigger can match on the folder it landed in.'}
          >
            <Select
              mode="tags"
              tokenSeparators={[',']}
              placeholder="Inbox (leave empty), or e.g. Tickets/Open"
              style={{ fontFamily: MONO_FONT, width: '100%' }}
              open={false}
              suffixIcon={null}
            />
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
              message="Shared mailbox watched by several consoles?"
              description={
                <span style={{ fontSize: 13 }}>
                  Without coordination, every console reacts to every new mail
                  (duplicate replies / runs). Enable a <b>claim</b> on the matching
                  trigger (Triggers tab) so one operator claims each email &mdash; the
                  item moves to a claim folder and only the winner proceeds. Otherwise
                  monitor one folder from one console, or let Outlook rules sort each
                  operator's mail into a personal folder.
                </span>
              }
            />
          )}
        </Form>
      )}

      <Divider style={{ margin: '16px 0 12px' }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <MailOutlined style={{ color: 'var(--rs-text-muted)' }} />
        <span style={{ fontSize: 14 }}>Also watch (optional)</span>
      </div>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
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
            style={{ fontFamily: MONO_FONT, flex: 2 }}
          />
          <Select
            size="small"
            mode="tags"
            tokenSeparators={[',']}
            placeholder='folders (blank = "Inbox")'
            value={w.folders ?? []}
            onChange={(vals: string[]) => setWatch(i, { folders: vals })}
            style={{ fontFamily: MONO_FONT, flex: 2 }}
            open={false}
            suffixIcon={null}
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
        {saved && <Text type="success" style={{ fontSize: 13 }}>Saved</Text>}
      </div>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 10 }}>
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
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 12 }}>
        The customer teams a visitor picks at sign-up — stored in the helpdesk room and shared across operators. <code>Default</code> is always present.
      </Text>

      {notConnected && (
        <Alert type="warning" showIcon style={{ marginBottom: 10 }}
          message="Not connected to a helpdesk room"
          description={<span style={{ fontSize: 13 }}>Connect a host with a room port + a matching <code>[mailbox]</code> (Settings → Jump Hosts + Triggers) to manage teams.</span>} />
      )}

      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <Input size="small" value={name} onChange={e => setName(e.target.value)} onPressEnter={add}
          placeholder="New team name (e.g. HR)" disabled={notConnected} />
        <Button size="small" type="primary" icon={<PlusOutlined />} onClick={add} loading={adding} disabled={notConnected || !name.trim()}>Add</Button>
      </div>

      {err && <Alert type="error" showIcon style={{ marginBottom: 10 }} message={err} />}

      {teams.length === 0 && !notConnected ? (
        <Text type="secondary" style={{ fontSize: 13 }}>No teams yet — add one above.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {teams.map(t => (
            <div key={t.slug} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a' }}>
              <TeamOutlined style={{ color: 'var(--rs-text-muted)', flexShrink: 0 }} />
              <Text style={{ fontSize: 14, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</Text>
              <Text type="secondary" style={{ fontFamily: MONO_FONT, fontSize: 12, flexShrink: 0 }}>{t.slug}</Text>
              {t.slug === 'default' ? (
                <Tag style={{ margin: 0, fontSize: 11 }}>protected</Tag>
              ) : (
                <Popconfirm title={`Remove team ${t.name}?`} onConfirm={() => remove(t.slug)} okText="Remove" cancelText="Cancel" placement="left">
                  <Button type="text" size="small" icon={<DeleteOutlined />} danger style={{ padding: '0 4px', color: 'var(--rs-text-muted)' }} />
                </Popconfirm>
              )}
            </div>
          ))}
        </div>
      )}
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 10 }}>
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
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 12 }}>
        Pull this console's config files from a git repository at a tag, so a fresh install can be fully configured from one source. Authentication uses a saved credential (Credentials tab). A pull is <strong>non-destructive</strong>: it adds and updates from the repo but keeps your own hosts/triggers/filters/credentials and the keys you protect below (each file is backed up first).
      </Text>

      {engineMissing && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="Git engine not installed"
          description={<span style={{ fontSize: 13 }}>Install the optional <code>git-sync</code> extra to enable pulling.</span>} />
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
          help={cfg?.preserve_default?.length ? <span style={{ fontSize: 12 }}>Always protected: {cfg.preserve_default.map(p => <code key={p} style={{ marginRight: 6 }}>{p}</code>)}</span> : undefined}
        >
          <Select mode="tags" tokenSeparators={[',', ' ']} open={false} suffixIcon={null}
            placeholder="config.toml:mailbox.folder  (Enter to add)" />
        </Form.Item>
        <Form.Item name="layers" label="Folder(s) in the repo (optional)"
          tooltip="Where in the repo the config files live. Leave blank to read the repo root. Add one folder to read just that folder. Add several to combine them in order — a shared base first, your team's folder last — so common config is shared and your team overlays its own on top (later folders win; your local entries are always kept)."
          help={<span style={{ fontSize: 12 }}>Blank = repo root. One folder, e.g. <code>teams/emea</code> = just that folder. Several, e.g. <code>_base</code> then <code>teams/emea</code> = shared base + your team on top (order = how they stack).</span>}>
          <Select mode="tags" placeholder="blank = repo root  ·  or e.g. _base then teams/emea" tokenSeparators={[',']} open={false} suffixIcon={null} style={{ fontFamily: MONO_FONT }} />
        </Form.Item>
        <Form.Item
          name="ca_bundle"
          label="CA bundle (optional)"
          tooltip="Path to a PEM file for verifying the server's TLS certificate. Needed for an internal CA or a TLS-inspecting corporate proxy — the pull uses certifi by default, not the OS/git cert store. Leave blank for public hosts. (HTTPS also reuses the SSH-tab proxy, if set.)"
          help={<span style={{ fontSize: 12 }}>For a private GitLab / corporate proxy that gives an <code>SSL CERTIFICATE_VERIFY_FAILED</code> error — point this at your internal root CA <code>.pem</code>.</span>}
        >
          <Input placeholder="~/certs/corp-root-ca.pem" style={{ fontFamily: MONO_FONT }} autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="tls_verify"
          label="Verify TLS"
          valuePropName="checked"
          tooltip="Off skips certificate verification entirely (like git's http.sslVerify=false). Insecure — only on a trusted internal network. Prefer a CA bundle above."
          help={<span style={{ fontSize: 12 }}>Turn off only if you can't supply a CA bundle (e.g. your <code>git config</code> already sets <code>http.sslVerify false</code>).</span>}
        >
          <Switch size="small" />
        </Form.Item>
        <Form.Item
          name="backups_keep"
          label="Backups to keep per file"
          tooltip="Before a pull changes a file it copies the old one to <name>.bak-<timestamp>. This caps how many of those are kept per file — older ones are pruned after each pull. A pull that changes nothing makes no backup. 0 = keep all (never prune)."
          help={<span style={{ fontSize: 12 }}>Default 5. Set <code>0</code> to keep every backup (they'll accumulate in the config folder).</span>}
        >
          <InputNumber min={0} step={1} style={{ width: '100%' }} placeholder="5" />
        </Form.Item>

        <Space>
          <Button size="small" onClick={handleSave} loading={saving}>{saved ? 'Saved' : 'Save settings'}</Button>
          <Popconfirm
            title="Pull config from the repo?"
            description={<span style={{ fontSize: 13 }}>Merges config from the chosen tag (adds/updates, keeps your own).<br />Each file is backed up first.</span>}
            onConfirm={pull} okText="Pull" cancelText="Cancel" placement="topLeft">
            <Button size="small" type="primary" icon={<DownloadOutlined />} loading={pulling} disabled={engineMissing}>Pull now</Button>
          </Popconfirm>
        </Space>
      </Form>

      {cfg?.last_synced && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
          Last synced <code>{cfg.last_ref}</code> ({cfg.last_commit?.slice(0, 8)}) at {cfg.last_synced}
        </Text>
      )}

      {result && result.ok && (
        <Alert type="success" showIcon style={{ marginTop: 12 }}
          message={`Pulled at ${result.ref}`}
          description={<span style={{ fontSize: 13 }}>
            Wrote: {result.written?.length ? <code>{result.written.join(', ')}</code> : 'nothing'}.
            {result.skipped?.length ? <> Skipped (not in repo): <code>{result.skipped.join(', ')}</code>.</> : null}
            {result.backups?.length ? <> Backed up {result.backups.length} file(s).</> : null}
            {result.pruned?.length ? <> Pruned {result.pruned.length} old backup(s).</> : null}
            {' '}Hosts and triggers reloaded — no restart needed.
          </span>} />
      )}
      {result && !result.ok && (
        <Alert type="error" showIcon style={{ marginTop: 12 }} message="Pull failed" description={<span style={{ fontSize: 13 }}>{result.error}</span>} />
      )}

      <Divider style={{ margin: '18px 0 12px' }} />
      <Text strong style={{ fontSize: 14, display: 'block' }}>Config seed</Text>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', margin: '4px 0 10px' }}>
        Baseline config <strong>bundled inside this install's package</strong> — applied automatically on launch when the package version changes, so a fresh or air-gapped machine is configured with no repo or index reachable. It's the lowest layer: a git pull above and your own edits always win (except the keys you protect above).
      </Text>
      {seed?.available ? (
        <>
          <Text type="secondary" style={{ fontSize: 13, display: 'block' }}>
            Bundled: {Object.entries(seed.stamp).map(([d, v]) => <code key={d} style={{ marginRight: 6 }}>{d} v{v}</code>)}
          </Text>
          {seed.last_applied && (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 2 }}>
              Last applied at {seed.last_applied}
            </Text>
          )}
          {seed.overrides && seed.overrides.length > 0 && (() => {
            const active = seed.overrides.filter(o => !o.preserved)
            const kept = seed.overrides.filter(o => o.preserved)
            const row = (o: SeedOverride, i: number) => (
              <div key={`${o.file}:${o.path}:${i}`} style={{ fontFamily: MONO_FONT, fontSize: 12, marginTop: 3 }}>
                <Tag style={{ margin: '0 4px 0 0', fontSize: 11 }}>{o.file}</Tag>
                <span>{o.path}</span>: local <code>{o.local || '(empty)'}</code> → seed <code>{o.seed || '(empty)'}</code>
                {o.preserved && <Tag color="green" style={{ marginLeft: 6, fontSize: 11 }}>protected</Tag>}
              </div>
            )
            return (
              <Alert
                type={active.length ? 'warning' : 'info'}
                showIcon
                style={{ marginTop: 8 }}
                message={active.length
                  ? `${active.length} local value${active.length > 1 ? 's' : ''} would be overwritten by the seed`
                  : 'Seeded keys differ from local (all protected)'}
                description={<div style={{ fontSize: 13 }}>
                  {active.length > 0 && (
                    <div style={{ marginBottom: kept.length ? 10 : 0 }}>
                      <div style={{ marginBottom: 2 }}>
                        These local edits are <strong>not</strong> protected — re-applying the seed (or the next package upgrade) reverts them to the seed value. Add the key to <em>Preserve</em> above to keep your value.
                      </div>
                      {active.map(row)}
                    </div>
                  )}
                  {kept.length > 0 && (
                    <div>
                      <div style={{ marginBottom: 2 }}>Protected by a preserve rule — your local value is kept on re-apply:</div>
                      {kept.map(row)}
                    </div>
                  )}
                </div>} />
            )
          })()}
          {seed.pending && (
            <Alert type="info" showIcon style={{ marginTop: 8 }}
              message="A newer seed is bundled"
              description={<span style={{ fontSize: 13 }}>It applies automatically on the next launch — or re-apply it now.</span>} />
          )}
          <div style={{ marginTop: 10 }}>
            <Popconfirm
              title="Re-apply the bundled config seed?"
              description={<span style={{ fontSize: 13 }}>Re-asserts the package's baseline over local config (your own entries and protected keys are kept).<br />Each changed file is backed up first.</span>}
              onConfirm={reapplySeed} okText="Re-apply" cancelText="Cancel" placement="topLeft">
              <Button size="small" loading={seedBusy}>Re-apply seed</Button>
            </Popconfirm>
          </div>
          {seedResult && seedResult.ok && seedResult.applied && (
            <Alert type="success" showIcon style={{ marginTop: 10 }}
              message="Seed applied"
              description={<span style={{ fontSize: 13 }}>
                Wrote: {seedResult.written?.length ? <code>{seedResult.written.join(', ')}</code> : 'nothing'}.
                {seedResult.backups?.length ? <> Backed up {seedResult.backups.length} file(s).</> : null}
                {' '}Hosts and triggers reloaded — no restart needed.
              </span>} />
          )}
          {seedResult && seedResult.ok && !seedResult.applied && (
            <Alert type="info" showIcon style={{ marginTop: 10 }} message="Seed already up to date" description={<span style={{ fontSize: 13 }}>Nothing changed.</span>} />
          )}
          {seedResult && !seedResult.ok && (
            <Alert type="error" showIcon style={{ marginTop: 10 }} message="Seed apply failed" description={<span style={{ fontSize: 13 }}>{seedResult.error}</span>} />
          )}
        </>
      ) : (
        <Text type="secondary" style={{ fontSize: 13 }}>No config seed bundled in this install.</Text>
      )}

      <Divider style={{ margin: '18px 0 12px' }} />
      <Text strong style={{ fontSize: 14, display: 'block' }}>Runbook docs</Text>
      <Text type="secondary" style={{ fontSize: 13, display: 'block', margin: '4px 0 10px' }}>
        Generate a plain-markdown <strong>wiki</strong> from your runbooks + fleet — grouped by tag, one page per runbook, cross-linked to the runnables and hosts each names. It's for humans to follow during an outage (when the console may be down). Output is deterministic, so re-running only changes what your runbooks changed — point it at a local clone of the config repo and commit the docs back for the team.
      </Text>
      <Space.Compact style={{ width: '100%' }}>
        <Input size="small" value={docsDir} onChange={e => setDocsDir(e.target.value)}
          placeholder={docsDefault ? `${docsDefault}  (default)` : 'output directory'}
          style={{ fontFamily: MONO_FONT }} autoComplete="off" />
        <Button size="small" type="primary" onClick={generateDocs} loading={docsBusy}>Generate</Button>
      </Space.Compact>
      {docsResult && docsResult.ok && (
        <Alert type="success" showIcon style={{ marginTop: 10 }}
          message={`Wrote ${docsResult.written?.length ?? 0} file(s)`}
          description={<span style={{ fontSize: 13 }}>Into <code>{docsResult.dir}</code>. Commit them from your repo clone to share with the team.</span>} />
      )}
      {docsResult && !docsResult.ok && (
        <Alert type="error" showIcon style={{ marginTop: 10 }} message="Doc generation failed" description={<span style={{ fontSize: 13 }}>{docsResult.error}</span>} />
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
  steps: RunbookStep[]
  args?: RunbookArgRow[]
}

const RUNBOOK_ARG_TYPES = ['str', 'int', 'float', 'flag', 'choice', 'path', 'host']

// Normalise a runbook's steps into editor rows. get_runbooks already returns
// structured rows, but tolerate a legacy string (best-effort split on "1." / "2)"
// lines, falling back to a single row) so an old value never breaks the editor.
function stepsToRows(steps: RunbookStep[] | string | undefined): RunbookStep[] {
  if (Array.isArray(steps)) return steps.map(s => ({ text: s.text ?? '', title: s.title }))
  const body = (steps ?? '').trim()
  if (!body) return []
  const lines = body.split('\n')
  if (!lines.some(l => /^\s*\d+[.)]\s+/.test(l))) return [{ text: body }]
  const rows: RunbookStep[] = []
  for (const line of lines) {
    const m = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (m) rows.push({ text: m[1].trim() })
    else if (rows.length) rows[rows.length - 1].text = `${rows[rows.length - 1].text}\n${line.trim()}`.trim()
    else rows.push({ text: line.trim() })
  }
  return rows.filter(r => r.text)
}

// Editor rows → the payload steps: drop blank rows, drop an empty title.
function rowsToSteps(rows: RunbookStep[] | undefined): RunbookStep[] {
  const out: RunbookStep[] = []
  for (const row of rows ?? []) {
    const text = (row?.text ?? '').trim()
    if (!text) continue
    const title = (row?.title ?? '').trim()
    out.push(title ? { title, text } : { text })
  }
  return out
}

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

  const startAdd = () => { form.resetFields(); form.setFieldsValue({ args: [], steps: [{ text: '' }] }); setEditingKey('') }
  const startEdit = (s: Runbook) => {
    form.setFieldsValue({
      id: s.id, title: s.title, symptom: s.symptom, tags: s.tags ?? [],
      steps: stepsToRows(s.steps), args: argsRawToRows(s.argsRaw),
    })
    setEditingKey(s.id)
  }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  // Refresh the `$` menu / run modal elsewhere in the app after a change.
  const afterChange = async () => {
    await load()
    window.dispatchEvent(new CustomEvent('runspec:runbooks_changed'))
  }

  // Save just the edited runbook (single-entry read-modify-write on the backend), NOT
  // the whole collection. The old bulk save rebuilt every other runbook from a stale
  // in-memory snapshot, so it clobbered runbooks changed on disk / via Config Sync since
  // load, and dropped their `discoverable` audience. Saving one entry leaves the rest
  // exactly as they are on disk.
  const save = async () => {
    const values = await form.validateFields()
    const id = (values.id ?? '').trim()
    const steps = rowsToSteps(values.steps)
    if (steps.length === 0) { message.error('Add at least one step'); return }
    const edited: Record<string, unknown> = {
      id,
      title: (values.title ?? '').trim(),
      symptom: (values.symptom ?? '').trim(),
      steps,
      tags: values.tags ?? [],
      argsRaw: rowsToArgsRaw(values.args),
    }
    const res = await bridge.save_runbook_entry(edited as unknown as Runbook)
    if (!res.ok) { message.error(res.error || 'Could not save runbook'); return }
    await afterChange()
    setEditingKey(null); form.resetFields()
  }

  const remove = async (id: string) => {
    const res = await bridge.delete_runbook(id)
    if (!res.ok) { message.error(res.error || 'Could not delete runbook'); return }
    await afterChange()
  }

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
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
          <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a' }}>
            <span style={{ minWidth: 200 }}>
              <Typography.Text code style={{ fontSize: 13 }}>${s.id}</Typography.Text>
            </span>
            <span style={{ flex: 1, fontSize: 13, color: 'var(--rs-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {s.title}{s.symptom ? ` — ${s.symptom}` : ''}
            </span>
            {(s.args ?? []).length > 0 && (
              <Tag style={{ fontSize: 11, margin: 0 }}>{(s.args ?? []).length} param{(s.args ?? []).length !== 1 ? 's' : ''}</Tag>
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
              <Input placeholder="fetch-and-process-log" style={{ fontFamily: MONO_FONT }} disabled={editingKey !== ''} />
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
          <Divider style={{ margin: '8px 0' }} orientation="left" plain>Steps</Divider>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            One step per box, in order — the assistant follows them as numbered prose and
            uses its own judgement (e.g. write “if the node is healthy, skip the restart”
            right in a step). Reference parameters as <Typography.Text code>{'{{name}}'}</Typography.Text>.
          </Typography.Paragraph>
          <Form.List name="steps">
            {(fields, { add, remove, move }) => (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 8 }}>
                {fields.map((field, index) => (
                  <div key={field.key} style={{ border: '1px solid #2a2a2a', borderRadius: RADIUS.sm, padding: 8 }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                      <span style={{ fontSize: 13, color: 'var(--rs-text-muted)', minWidth: 52 }}>Step {index + 1}</span>
                      <Form.Item name={[field.name, 'title']} noStyle>
                        <Input size="small" placeholder="Optional title" />
                      </Form.Item>
                      <Tooltip title="Move up"><Button type="text" size="small" icon={<UpOutlined />} disabled={index === 0} onClick={() => move(index, index - 1)} /></Tooltip>
                      <Tooltip title="Move down"><Button type="text" size="small" icon={<DownOutlined />} disabled={index === fields.length - 1} onClick={() => move(index, index + 1)} /></Tooltip>
                      <MinusCircleOutlined onClick={() => remove(field.name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                    </div>
                    <Form.Item name={[field.name, 'text']} noStyle rules={[{ required: true, whitespace: true, message: 'Step text is required' }]}>
                      <Input.TextArea autoSize={{ minRows: 2 }} style={{ fontFamily: MONO_FONT, fontSize: 13 }}
                        placeholder={'Relay /srv/prod/{{app}}/logs/{{logfile}} via filter {{filter}} to jump-01:/home/me/logs.'} />
                    </Form.Item>
                  </div>
                ))}
                <Button type="dashed" size="small" onClick={() => add({ text: '' })} icon={<PlusOutlined />}>
                  Add step
                </Button>
              </div>
            )}
          </Form.List>

          <Divider style={{ margin: '8px 0' }} orientation="left" plain>Parameters</Divider>
          <Form.List name="args">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <div key={key} style={{ border: '1px solid #2a2a2a', borderRadius: RADIUS.sm, padding: '6px 8px', marginBottom: 6 }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'baseline' }}>
                      <Form.Item {...rest} name={[name, 'name']} style={{ marginBottom: 0, width: 130 }}
                        rules={[{ required: true, message: 'name' }]}>
                        <Input placeholder="name" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'type']} style={{ marginBottom: 0, width: 96 }} initialValue="str">
                        <Select options={RUNBOOK_ARG_TYPES.map(t => ({ label: t, value: t }))} />
                      </Form.Item>
                      {/* choice shows options AND a default (drawn from those options); other
                          types show a plain default input. A default makes the param optional
                          (no default → inferred required). Re-render on type OR options change. */}
                      <Form.Item noStyle shouldUpdate={(p, c) =>
                        p.args?.[name]?.type !== c.args?.[name]?.type
                        || p.args?.[name]?.options !== c.args?.[name]?.options}>
                        {() => {
                          if (form.getFieldValue(['args', name, 'type']) !== 'choice') {
                            return (
                              <Form.Item {...rest} name={[name, 'default']} style={{ marginBottom: 0, width: 150 }}>
                                <Input placeholder="default (optional)" />
                              </Form.Item>
                            )
                          }
                          const opts: string[] = form.getFieldValue(['args', name, 'options']) ?? []
                          return (
                            <>
                              <Form.Item {...rest} name={[name, 'options']} style={{ marginBottom: 0, width: 140 }}>
                                <Select mode="tags" placeholder="options" tokenSeparators={[',']} />
                              </Form.Item>
                              <Form.Item {...rest} name={[name, 'default']} style={{ marginBottom: 0, width: 130 }}>
                                <Select allowClear placeholder="default" options={opts.map(o => ({ label: o, value: o }))} />
                              </Form.Item>
                            </>
                          )
                        }}
                      </Form.Item>
                      <Form.Item {...rest} name={[name, 'required']} valuePropName="checked" style={{ marginBottom: 0 }}>
                        <Tooltip title="Required (ignored when a default is set)"><Switch size="small" /></Tooltip>
                      </Form.Item>
                      <MinusCircleOutlined onClick={() => remove(name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                    </div>
                    <Form.Item {...rest} name={[name, 'description']} style={{ marginBottom: 0, marginTop: 6 }}>
                      <Input size="small" placeholder="description (optional) — shown in the run form" />
                    </Form.Item>
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

// ── Saved runs (deterministic presets) ──────────────────────────────────────

// A saved run's manual open params reuse the runbook parameter-row editor;
// `password` is additionally allowed here (a param is typed at run time and
// rides the secret env channel — unlike a runbook, nothing is substituted into
// a prompt body).
const SAVED_RUN_PARAM_TYPES = [...RUNBOOK_ARG_TYPES, 'password']

const SAVED_RUN_LABEL_STYLE: CSSProperties = { color: 'var(--rs-text-muted)', fontSize: 13 }

interface SavedRunFormValues {
  id: string
  title?: string
  description?: string
  runnable: string
  command?: string
  host?: string
  group?: string
  hosts?: string[]
  selected_hosts?: string[]
  args?: { key?: string; value?: string }[]
  params?: RunbookArgRow[]
}

const _srNorm = (s: string) => s.replace(/-/g, '_')

function SavedRunsTab() {
  const [savedRuns, setSavedRuns] = useState<SavedRun[]>([])
  const [hosts, setHosts] = useState<Host[]>([])
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [creds, setCreds] = useState<Credential[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null)  // '' = new
  const [form] = Form.useForm<SavedRunFormValues>()
  // Targeting + rich arg state live outside the antd Form so the editor can
  // reuse the shared TargetFormItems / RunnableArgsBlock (which the Schedules
  // modal also uses — one source, identical behavior). `openArgs` are declared
  // args flipped to "asked at run time" (they become params on save).
  const [target, setTarget] = useState<Target>('host')
  const [argValues, setArgValues] = useState<Record<string, unknown>>({})
  const [argErrors, setArgErrors] = useState<Record<string, string>>({})
  const [openArgs, setOpenArgs] = useState<Set<string>>(new Set())
  const [credentialIds, setCredentialIds] = useState<string[]>([])

  const load = useCallback(() => bridge.get_saved_runs().then(setSavedRuns).catch(() => {}), [])
  // Load on mount + refresh whenever saved runs change elsewhere (agent save,
  // Config Sync pull) — mirrors RunbooksTab. The fleet's discovered runnables
  // feed the target-aware runnable picker and the rich args form.
  useEffect(() => {
    load()
    bridge.get_hosts().then(setHosts).catch(() => {})
    bridge.list_credentials().then(setCreds).catch(() => {})
    const loadRunnables = () => bridge.get_runnables('all').then(setRunnables).catch(() => {})
    loadRunnables()
    const onChanged = () => { load() }
    window.addEventListener('runspec:saved_runs_changed', onChanged)
    window.addEventListener('runspec:runnables_updated', loadRunnables)
    return () => {
      window.removeEventListener('runspec:saved_runs_changed', onChanged)
      window.removeEventListener('runspec:runnables_updated', loadRunnables)
    }
  }, [load])

  const groupNames = [...new Set(hosts.map(h => h.group).filter((g): g is string => !!g))].sort()
  const hostName: string = Form.useWatch('host', form) ?? ''
  const groupName: string = Form.useWatch('group', form) ?? ''
  const hostsList: string[] = Form.useWatch('hosts', form) ?? []
  const selectedList: string[] = Form.useWatch('selected_hosts', form) ?? []
  const runnableName: string = Form.useWatch('runnable', form) ?? ''
  // What the chosen target can run — same derivation as the Schedules modal.
  const targetRunnables = runnablesForTarget(runnables, hosts, target, {
    host: hostName, group: groupName, hostsList, selectedList,
  })
  const editingEntry = editingKey ? savedRuns.find(s => s.id === editingKey) ?? null : null
  // The discovered runnable behind the picked name; null when the fleet doesn't
  // currently expose it (offline host) → the editor degrades to raw key=value
  // fixed args + manual params so editing still works.
  const selectedRunnable = targetRunnables.find(r => r.name === runnableName) ?? null
  // Subcommand handling: the field only exists for runnables that declare
  // subcommands (a dropdown of their paths); the picked path shifts which args
  // the rich block edits — the effective set at that path (inherited globals +
  // the command's own, child wins), same as the Forms dialog / `runspec serve`.
  const commandValue: string = Form.useWatch('command', form) ?? ''
  const commandPath = commandValue.trim() ? commandValue.trim().split(/\s+/) : []
  const commandOptions = selectedRunnable
    ? commandPathOptions(selectedRunnable, SAVED_RUN_LABEL_STYLE)
    : []
  const effectiveRunnable = selectedRunnable
    ? { ...selectedRunnable, args: effectiveArgsForPath(selectedRunnable, commandPath) }
    : null

  const startAdd = () => {
    form.resetFields()
    form.setFieldsValue({ args: [], params: [] })
    setTarget('host')
    setArgValues({}); setArgErrors({}); setOpenArgs(new Set())
    setCredentialIds([])
    setEditingKey('')
  }
  const startEdit = (s: SavedRun) => {
    form.setFieldsValue({
      id: s.id, title: s.title, description: s.description,
      runnable: s.runnable, command: (s.command ?? []).join(' '),
      host: s.host || undefined, group: s.group || undefined,
      hosts: s.hosts ?? [], selected_hosts: s.selected_hosts ?? [],
      // Fallback editors — used only while the runnable isn't discovered; the
      // rich seeding effect below takes over as soon as it resolves.
      args: Object.entries(s.args ?? {}).map(([key, value]) => ({ key, value: String(value) })),
      params: argsRawToRows(s.paramsRaw),
    })
    setTarget(s.group ? 'group' : s.selected_hosts?.length ? 'selected' : s.hosts?.length ? 'anyhost' : 'host')
    setArgValues({}); setArgErrors({}); setOpenArgs(new Set())
    setCredentialIds(s.credential_ids ?? [])
    setEditingKey(s.id)
  }
  const cancel = () => { setEditingKey(null); form.resetFields() }

  // Seed the rich arg state whenever the picked runnable (or its subcommand)
  // resolves — on edit (once discovery delivers it), and on every picker change.
  // Stored fixed args overlay the spec defaults (`initialFormValues`, shared
  // with Forms); stored params that match a declared arg become Open toggles,
  // the rest stay in the manual rows. A command carried over from a different
  // runnable is cleared (its paths don't apply).
  useEffect(() => {
    if (editingKey === null) return
    if (!selectedRunnable) return  // fallback editors keep their startEdit seed
    const stored = editingEntry && editingEntry.runnable === selectedRunnable.name ? editingEntry : null
    if (commandValue && !commandOptions.some(o => o.value === commandValue)) {
      form.setFieldValue('command', undefined)
      return  // re-runs with the cleared command
    }
    const effective = effectiveArgsForPath(selectedRunnable, commandPath)
    setArgValues(initialFormValues(effective, stored?.args))
    const declared = new Map(effective.map(a => [_srNorm(a.name), a.name]))
    const open = new Set<string>()
    const manual: RunbookArgRow[] = []
    for (const [pname, pdef] of Object.entries(stored?.paramsRaw ?? {})) {
      const match = declared.get(_srNorm(pname))
      if (match) open.add(match)
      else manual.push(...argsRawToRows({ [pname]: pdef as Record<string, unknown> }))
    }
    setOpenArgs(open)
    setArgErrors({})
    form.setFieldValue('params', manual)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingKey, selectedRunnable?.name, commandValue])

  const handleTargetChange = (t: Target) => {
    setTarget(t)
    // Switching targets invalidates the chosen runnable (the available set differs).
    form.setFieldsValue({ runnable: undefined })
    setArgValues({}); setArgErrors({}); setOpenArgs(new Set())
  }
  const handleHostChange = () => {
    form.setFieldValue('runnable', undefined)
    setArgValues({}); setArgErrors({}); setOpenArgs(new Set())
  }
  const changeArg = (name: string, v: unknown) => {
    setArgValues(prev => ({ ...prev, [name]: v }))
    setArgErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }
  const toggleOpen = (name: string, open: boolean) => {
    setOpenArgs(prev => {
      const next = new Set(prev)
      if (open) next.add(name); else next.delete(name)
      return next
    })
    setArgErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const toRow = (s: SavedRun): Record<string, unknown> => ({
    id: s.id, title: s.title, description: s.description,
    runnable: s.runnable, command: s.command ?? [],
    host: s.host, group: s.group, hosts: s.hosts, selected_hosts: s.selected_hosts,
    args: s.args ?? {}, paramsRaw: s.paramsRaw ?? {}, credential_ids: s.credential_ids ?? [],
  })

  const persist = async (rows: Array<Record<string, unknown>>) => {
    const res = await bridge.save_saved_runs(rows as unknown as SavedRun[])
    if (!res.ok) { message.error(res.error || 'Could not save saved run'); return false }
    await load()
    window.dispatchEvent(new CustomEvent('runspec:saved_runs_changed'))
    return true
  }

  const save = async () => {
    const values = await form.validateFields()
    const id = (values.id ?? '').trim()
    let args: Record<string, unknown>
    let paramsRaw: Record<string, unknown>
    if (selectedRunnable) {
      // Rich path: typed values → fixed args; Open (and password) args →
      // generated params; manual rows may add extras (e.g. subcommand-level
      // args) — a generated entry wins on a name collision.
      const split = splitFixedAndOpen(
        effectiveRunnable?.args ?? [], argValues, openArgs,
      )
      if (Object.keys(split.errors).length > 0) { setArgErrors(split.errors); return }
      args = split.args
      paramsRaw = { ...rowsToArgsRaw(values.params), ...split.params }
    } else {
      // Fallback path (runnable not discovered): raw key=value + manual params.
      args = {}
      for (const f of values.args ?? []) {
        if (f?.key?.trim()) args[f.key.trim()] = String(f.value ?? '').trim()
      }
      paramsRaw = rowsToArgsRaw(values.params)
    }
    const edited: Record<string, unknown> = {
      id,
      title: (values.title ?? '').trim(),
      description: (values.description ?? '').trim(),
      runnable: (values.runnable ?? '').trim(),
      command: (values.command ?? '').trim() ? (values.command ?? '').trim().split(/\s+/) : [],
      host: target === 'host' ? values.host : '',
      group: target === 'group' ? values.group : '',
      hosts: target === 'anyhost' ? (values.hosts ?? []) : [],
      selected_hosts: target === 'selected' ? (values.selected_hosts ?? []) : [],
      args,
      paramsRaw,
      credential_ids: credentialIds,
    }
    const others = savedRuns.filter(s => s.id !== (editingKey || id)).map(toRow)
    if (await persist([...others, edited])) { setEditingKey(null); form.resetFields() }
  }

  const remove = async (id: string) => {
    await persist(savedRuns.filter(s => s.id !== id).map(toRow))
  }

  return (
    <div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Saved runs are <strong>deterministic presets</strong>: a runnable + target host(s) +
        fixed args, with optional <strong>open parameters</strong> filled at run time. No
        agent involved — a saved run fires exactly as stored. They appear in the Forms tab,
        the assistant can list and run them (gated by the runnable's own autonomy), and the
        Schedules tab can fire one on a cadence. Saved runs sync with the rest of your
        config (Config Sync), so a team shares one set — fixed args must never contain
        secrets; attach a Credential instead.
      </Typography.Paragraph>

      <Button type="dashed" icon={<PlusOutlined />} onClick={startAdd} style={{ marginBottom: 12 }}>
        New saved run
      </Button>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {savedRuns.length === 0 && <Empty description="No saved runs yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        {savedRuns.map(s => (
          <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: RADIUS.sm, border: '1px solid #2a2a2a' }}>
            <span style={{ minWidth: 200 }}>
              <Typography.Text code style={{ fontSize: 13 }}>{s.id}</Typography.Text>
            </span>
            <span style={{ flex: 1, fontSize: 13, color: 'var(--rs-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              /{s.runnable} on {s.target || s.host}{s.title ? ` — ${s.title}` : ''}
            </span>
            {(s.params ?? []).length > 0 && (
              <Tag style={{ fontSize: 11, margin: 0 }}>{(s.params ?? []).length} param{(s.params ?? []).length !== 1 ? 's' : ''}</Tag>
            )}
            {s.argsError && <Tag color="red" style={{ fontSize: 11, margin: 0 }}>invalid</Tag>}
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(s)} disabled={editingKey !== null} />
            <Popconfirm title={`Remove ${s.id}?`} onConfirm={() => remove(s.id)}>
              <Button type="text" size="small" icon={<DeleteOutlined />} disabled={editingKey !== null} danger />
            </Popconfirm>
          </div>
        ))}
      </div>

      <Modal
        open={editingKey !== null}
        title={editingKey === '' ? 'New saved run' : `Edit — ${editingKey}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        maskClosable={false}
      >
        <Form form={form} layout="vertical" size="small">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
            <Form.Item name="id" label="Id" rules={[{ required: true, message: 'Required' }]}>
              <Input placeholder="restart-web" style={{ fontFamily: MONO_FONT }} disabled={editingKey !== ''} />
            </Form.Item>
            <Form.Item name="title" label="Title">
              <Input placeholder="Restart the web tier" />
            </Form.Item>
          </div>
          <Form.Item name="description" label="Description">
            <Input placeholder="Rolling restart of nginx across production" />
          </Form.Item>

          <TargetFormItems
            hosts={hosts}
            groupNames={groupNames}
            target={target}
            onTargetChange={handleTargetChange}
            onHostChange={handleHostChange}
            labelStyle={SAVED_RUN_LABEL_STYLE}
          />

          {/* Full-width, and the closed control shows just the runnable name
              (optionLabelProp) — the rich /name + description label is dropdown-
              only, so a long description can't overflow the modal. */}
          <Form.Item name="runnable" label="Runnable" rules={[{ required: true, message: 'Required' }]}>
            <Select
              placeholder="Select a runnable"
              showSearch
              optionLabelProp="value"
              filterOption={(input, opt) =>
                (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={runnableSelectOptions(
                targetRunnables, SAVED_RUN_LABEL_STYLE, editingEntry?.runnable,
              )}
            />
          </Form.Item>

          {/* Only runnables that declare subcommands get the field — a dropdown
              of their paths (the saved run stores the space-joined path). The
              free-text input survives solely in the undiscovered fallback. */}
          {selectedRunnable && commandOptions.length > 0 && (
            <Form.Item
              name="command"
              label="Subcommand"
              rules={selectedRunnable.requireCommand
                ? [{ required: true, message: 'This runnable requires a subcommand' }]
                : undefined}
              help={selectedRunnable.requireCommand ? undefined : 'Optional — leave blank to run the root command.'}
            >
              <Select
                placeholder="Select a subcommand…"
                allowClear
                optionLabelProp="value"
                options={commandOptions}
              />
            </Form.Item>
          )}
          {runnableName !== '' && !selectedRunnable && (
            <Form.Item name="command" label="Subcommand (optional)" help="Space-separated path, e.g. db restore">
              <Input placeholder="" style={{ fontFamily: MONO_FONT }} />
            </Form.Item>
          )}

          {effectiveRunnable && (effectiveRunnable.args ?? []).length > 0 && (
            <RunnableArgsBlock
              runnable={effectiveRunnable}
              values={argValues}
              errors={argErrors}
              onChange={changeArg}
              labelStyle={SAVED_RUN_LABEL_STYLE}
              openArgs={openArgs}
              onToggleOpen={toggleOpen}
            />
          )}

          {runnableName !== '' && !selectedRunnable && (
            <>
              <Divider style={{ margin: '8px 0' }} orientation="left" plain>Fixed args (baked in)</Divider>
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                This runnable isn't currently discovered on the targeted host(s), so args
                are edited as raw key=value pairs. Reconnect the fleet for typed fields.
              </Typography.Text>
              <Form.List name="args">
                {(fields, { add, remove: removeRow }) => (
                  <>
                    {fields.map(({ key, name, ...rest }) => (
                      <div key={key} style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                        <Form.Item {...rest} name={[name, 'key']} noStyle>
                          <Input size="small" placeholder="service" style={{ fontFamily: MONO_FONT, width: 160 }} />
                        </Form.Item>
                        <span style={{ color: 'var(--rs-text-muted)' }}>=</span>
                        <Form.Item {...rest} name={[name, 'value']} noStyle>
                          <Input size="small" placeholder="nginx" style={{ fontFamily: MONO_FONT, flex: 1 }} />
                        </Form.Item>
                        <MinusCircleOutlined onClick={() => removeRow(name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                      </div>
                    ))}
                    <Button type="dashed" size="small" onClick={() => add()} icon={<PlusOutlined />}>
                      Add fixed arg
                    </Button>
                  </>
                )}
              </Form.List>
            </>
          )}

          <Divider style={{ margin: '8px 0' }} orientation="left" plain>
            {selectedRunnable ? 'Additional open parameters' : 'Open parameters (filled at run time)'}
          </Divider>
          {selectedRunnable && (
            <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
              Declared args flip Open with the toggle above. Add rows here only for extras
              the runnable doesn't declare at the chosen subcommand.
            </Typography.Text>
          )}
          <Form.List name="params">
            {(fields, { add, remove: removeRow }) => (
              <>
                {fields.map(({ key, name, ...rest }) => (
                  <div key={key} style={{ display: 'flex', gap: 6, alignItems: 'baseline', marginBottom: 6 }}>
                    <Form.Item {...rest} name={[name, 'name']} style={{ marginBottom: 0, width: 130 }}
                      rules={[{ required: true, message: 'name' }]}>
                      <Input placeholder="name" style={{ fontFamily: MONO_FONT }} />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, 'type']} style={{ marginBottom: 0, width: 96 }} initialValue="str">
                      <Select options={SAVED_RUN_PARAM_TYPES.map(t => ({ label: t, value: t }))} />
                    </Form.Item>
                    <Form.Item noStyle shouldUpdate={(p, c) => p.params?.[name]?.type !== c.params?.[name]?.type}>
                      {() => form.getFieldValue(['params', name, 'type']) === 'choice' ? (
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
                    <MinusCircleOutlined onClick={() => removeRow(name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                  </div>
                ))}
                <Button type="dashed" size="small" onClick={() => add({ type: 'str' })} icon={<PlusOutlined />}>
                  Add parameter
                </Button>
              </>
            )}
          </Form.List>

          <CredentialsSection
            creds={creds}
            selected={credentialIds}
            onChange={setCredentialIds}
            declaredArgNames={new Set((effectiveRunnable?.args ?? []).map(a => a.name))}
            labelStyle={SAVED_RUN_LABEL_STYLE}
          />
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
          <Text style={{ fontSize: 13 }} ellipsis={{ tooltip: false }}>{ruleSummary(r)}</Text>
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
          <Text type="secondary" style={{ fontSize: 13 }}>
            Disable or re-enable runspec runnables across the fleet. This writes each
            host's venv denylist via the core <Text code>runspec deny</Text> over SSH —
            it stops the <em>runnables</em> on those hosts, never this console, and
            “Re-enable all” always works. Remote hosts need <Text code>runspec ≥ 0.43.0</Text>.
          </Text>
        }
      />

      <div style={{ marginBottom: 8 }}>
        <Text style={{ fontSize: 13 }}>Target hosts</Text>
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
            <span style={{ fontSize: 13 }}>
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
          description={<span style={{ fontSize: 13 }}>Clears the denylist on {scope}.</span>}
          okText="Re-enable"
          onConfirm={() => run({ action: 'clear' }, 'clear')}
        >
          <Button size="small" icon={<ReloadOutlined />} loading={busy === 'clear'}>
            Re-enable all (clear)
          </Button>
        </Popconfirm>
      </Space>

      <Divider style={{ margin: '8px 0' }} orientation="left" plain>
        <Text type="secondary" style={{ fontSize: 13 }}>Disable specific runnables</Text>
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
        <Text type="secondary" style={{ fontSize: 13 }}>Enforcement mode</Text>
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
        <pre style={{ fontSize: 13, background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: 10, whiteSpace: 'pre-wrap', maxHeight: 240, overflow: 'auto' }}>
          {result}
        </pre>
      )}
    </div>
  )
}

// ── Drawer ────────────────────────────────────────────────────────────────────

export function TelemetryTab() {
  const [enabled, setEnabled] = useState(false)
  const [endpoint, setEndpoint] = useState('')
  const [protocol, setProtocol] = useState('http/protobuf')
  const [serviceName, setServiceName] = useState('runspec-console')
  const [sampleRatio, setSampleRatio] = useState<number>(1)
  const [captureContent, setCaptureContent] = useState(false)
  const [maxContentChars, setMaxContentChars] = useState<number>(0)
  const [caBundle, setCaBundle] = useState('')
  const [tlsVerify, setTlsVerify] = useState(true)
  const [headers, setHeaders] = useState<Array<{ k: string; v: string }>>([])
  const [apiKeyCommand, setApiKeyCommand] = useState('')
  const [apiKeyHeader, setApiKeyHeader] = useState('x-api-key')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<Awaited<ReturnType<typeof bridge.send_test_trace>> | null>(null)

  useEffect(() => {
    bridge.get_config().then(cfg => {
      const t = (cfg.telemetry ?? {}) as Record<string, unknown>
      setEnabled(t.enabled === true)
      setEndpoint(typeof t.endpoint === 'string' ? t.endpoint : '')
      setProtocol(typeof t.protocol === 'string' && t.protocol ? t.protocol : 'http/protobuf')
      setServiceName(typeof t.service_name === 'string' && t.service_name ? t.service_name : 'runspec-console')
      setSampleRatio(typeof t.sample_ratio === 'number' ? t.sample_ratio : 1)
      setCaptureContent(t.capture_content === true)
      setMaxContentChars(typeof t.max_content_chars === 'number' ? t.max_content_chars : 0)
      setCaBundle(typeof t.ca_bundle === 'string' ? t.ca_bundle : '')
      setTlsVerify(t.tls_verify !== false)
      setApiKeyCommand(typeof t.api_key_command === 'string' ? t.api_key_command : '')
      setApiKeyHeader(typeof t.api_key_header === 'string' && t.api_key_header ? t.api_key_header : 'x-api-key')
      const h = (t.headers ?? {}) as Record<string, unknown>
      setHeaders(
        h && typeof h === 'object'
          ? Object.entries(h).map(([k, v]) => ({ k, v: String(v) }))
          : [],
      )
    })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      const headerObj: Record<string, string> = {}
      for (const { k, v } of headers) if (k.trim()) headerObj[k.trim()] = v
      await bridge.save_config({
        ...cfg,
        telemetry: {
          enabled,
          endpoint: endpoint.trim(),
          protocol: protocol.trim() || 'http/protobuf',
          service_name: serviceName.trim() || 'runspec-console',
          sample_ratio: sampleRatio,
          capture_content: captureContent,
          max_content_chars: Math.max(0, Math.floor(maxContentChars || 0)),
          ca_bundle: caBundle.trim(),
          tls_verify: tlsVerify,
          headers: headerObj,
          api_key_command: apiKeyCommand.trim(),
          api_key_header: apiKeyHeader.trim() || 'x-api-key',
        },
      })
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } finally { setSaving(false) }
  }

  // Save the current form, then ask the backend to emit + flush one test trace.
  // Saving first means the test exercises exactly what's on screen (the backend
  // reconfigures the tracer from the saved config), so "Save then test" is one click.
  const handleTest = async () => {
    setTesting(true); setTestResult(null)
    try {
      await handleSave()
      setTestResult(await bridge.send_test_trace())
    } catch (e) {
      setTestResult({ ok: false, reason: String((e as Error)?.message || e) })
    } finally { setTesting(false) }
  }

  const row = (label: string, control: ReactNode) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
      <span style={{ fontSize: 14 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 620 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Export a trace per agent turn — <strong>turn → LLM call → tool call</strong> — over
        OpenTelemetry (OTLP) to any OTel backend (LangSmith, Langfuse, Phoenix, Jaeger, Tempo).
        Spans follow the OpenTelemetry GenAI conventions. Requires the optional <code>otel</code> extra
        to be installed. Standard <code>OTEL_*</code> environment
        variables fill in anything left blank below.
      </Typography.Paragraph>

      <div style={{ marginBottom: 12 }}>
        <Button
          size="small"
          onClick={() => {
            setEnabled(true)
            setEndpoint('http://127.0.0.1:6006/v1/traces')
            setCaptureContent(true)
          }}
        >
          Use local Phoenix
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 13, marginLeft: 8 }}>
          Fills the endpoint for a locally-run <a href="https://github.com/Arize-ai/phoenix" target="_blank" rel="noreferrer">Arize Phoenix</a> and
          enables content capture — run <code>phoenix serve</code> in its own venv first, then Save.
        </Typography.Text>
      </div>

      {row('Enable tracing', <Switch checked={enabled} onChange={setEnabled} />)}
      {row('OTLP endpoint', <Input style={{ width: 380 }} placeholder="https://…/v1/traces  (blank ⇒ OTEL_EXPORTER_OTLP_ENDPOINT)" value={endpoint} onChange={e => setEndpoint(e.target.value)} disabled={!enabled} />)}
      {row('Protocol', <Select style={{ width: 180 }} value={protocol} onChange={setProtocol} disabled={!enabled} options={[{ value: 'http/protobuf', label: 'http/protobuf' }]} />)}
      {row('Service name', <Input style={{ width: 260 }} value={serviceName} onChange={e => setServiceName(e.target.value)} disabled={!enabled} />)}
      {row('Sample ratio', <InputNumber min={0} max={1} step={0.1} value={sampleRatio} onChange={v => setSampleRatio(typeof v === 'number' ? v : 1)} disabled={!enabled} />)}

      <Divider style={{ margin: '14px 0' }} />
      <Typography.Paragraph style={{ fontSize: 13, marginBottom: 6 }}>
        <strong>Headers</strong> — extra HTTP headers on every export (e.g. a LangSmith API key
        and project). Stored in <code>config.toml</code> like the model API key, not the OS keychain.
      </Typography.Paragraph>
      {headers.map((h, i) => (
        <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
          <Input placeholder="header" style={{ width: 220 }} value={h.k} disabled={!enabled}
            onChange={e => setHeaders(prev => prev.map((x, j) => j === i ? { ...x, k: e.target.value } : x))} />
          <Input.Password placeholder="value" style={{ width: 300 }} value={h.v} disabled={!enabled}
            onChange={e => setHeaders(prev => prev.map((x, j) => j === i ? { ...x, v: e.target.value } : x))} />
          <Button icon={<MinusCircleOutlined />} disabled={!enabled} onClick={() => setHeaders(prev => prev.filter((_, j) => j !== i))} />
        </div>
      ))}
      <Button size="small" icon={<PlusOutlined />} disabled={!enabled} onClick={() => setHeaders(prev => [...prev, { k: '', v: '' }])}>Add header</Button>

      <Divider style={{ margin: '14px 0' }} />
      <Typography.Paragraph style={{ fontSize: 13, marginBottom: 6 }}>
        <strong>API key from a command</strong> — instead of storing the key above, resolve it at
        startup from a command's output and inject it under the header named below. The config only
        holds the <em>command</em> (e.g. <code>keyring get langsmith key</code>, <code>vault read …</code>,
        or a bundled helper), so a config seed / sync can distribute it without ever shipping the key
        in the clear. Mirrors the model <code>api_key_command</code>.
      </Typography.Paragraph>
      {row('API key command', <Input style={{ width: 380 }} placeholder="e.g. keyring get langsmith key" value={apiKeyCommand} onChange={e => setApiKeyCommand(e.target.value)} disabled={!enabled} />)}
      {row('Inject under header', <Input style={{ width: 220 }} placeholder="x-api-key" value={apiKeyHeader} onChange={e => setApiKeyHeader(e.target.value)} disabled={!enabled || !apiKeyCommand.trim()} />)}

      <Divider style={{ margin: '14px 0' }} />
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="Content capture (privacy)"
        description={
          <span style={{ fontSize: 13 }}>
            Off (default): spans carry <strong>structure only</strong> — models, token counts,
            tool names, decisions, durations. On: prompt / completion / tool-argument <strong>text</strong>
            is also exported (secret-looking arguments are scrubbed first). Only enable this if your
            backend is trusted — prompts and arguments can contain sensitive data.
          </span>
        }
      />
      {row('Capture prompt / completion / argument text', <Switch checked={captureContent} onChange={setCaptureContent} disabled={!enabled} />)}
      {row(
        'Max captured content characters (0 = no limit)',
        <InputNumber min={0} step={1000} value={maxContentChars} onChange={v => setMaxContentChars(typeof v === 'number' ? v : 0)} disabled={!enabled || !captureContent} />,
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -2 }}>
        Each captured prompt / completion / argument value is recorded in full by default, so a trace shows the
        real prompt (including the search-mode tool menu) — not a slice. Set a ceiling only to bound span size.
      </Typography.Paragraph>

      <Divider style={{ margin: '14px 0' }} />
      {row('CA bundle (private CA / TLS proxy)', <Input style={{ width: 360 }} placeholder="path to a .pem" value={caBundle} onChange={e => setCaBundle(e.target.value)} disabled={!enabled} />)}
      {row('Verify TLS certificate', <Switch checked={tlsVerify} onChange={setTlsVerify} disabled={!enabled} />)}

      <Space style={{ marginTop: 6 }}>
        <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
        <Button loading={testing} disabled={!enabled} onClick={handleTest}>Send test trace</Button>
      </Space>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 8, marginBottom: 0 }}>
        Saves, then emits one sample <strong>turn → LLM call → tool call</strong> trace and flushes it
        immediately — so you can confirm the backend receives it (and, with content capture on, renders
        prompt / completion / argument text) without waiting for a real agent turn.
      </Typography.Paragraph>
      {testResult && (
        <Alert
          type={testResult.ok ? 'success' : 'error'}
          showIcon
          style={{ marginTop: 10 }}
          message={testResult.ok ? 'Test trace sent' : 'Could not send a test trace'}
          description={
            testResult.ok ? (
              <span style={{ fontSize: 13 }}>
                Sent {testResult.spans?.length ?? 3} spans to <code>{testResult.endpoint}</code>
                {testResult.trace_id ? <> — trace <code>{testResult.trace_id}</code></> : null}.{' '}
                {testResult.capture_content
                  ? 'Content capture is on (sample text attached; secret-looking args scrubbed).'
                  : 'Content capture is off (structure only).'}{' '}
                Open your backend and look for it{testResult.flushed ? ' (flushed now)' : ' (batched — may take a moment)'}.
              </span>
            ) : (
              <span style={{ fontSize: 13 }}>{testResult.reason}</span>
            )
          }
        />
      )}
    </div>
  )
}

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
      <span style={{ fontSize: 14 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Save each chat so you can reopen and continue past conversations from the Console's <strong>History</strong> button.
        Transcripts are stored <strong>only on this machine</strong> (never synced) and may contain sensitive content.
        Old conversations are pruned to the limits below.
      </Typography.Paragraph>
      {row('Save chat history', <Switch checked={enabled} onChange={setEnabled} />)}
      {row('Keep at most (conversations)', <InputNumber min={0} value={maxConvs} onChange={v => setMaxConvs(Number(v) || 0)} disabled={!enabled} />)}
      {row('Delete after (days)', <InputNumber min={0} value={maxAge} onChange={v => setMaxAge(Number(v) || 0)} disabled={!enabled} />)}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        0 = no limit. Disabling stops saving new conversations; existing files stay until pruned or deleted.
      </Typography.Paragraph>

      <Divider style={{ margin: '14px 0' }} />
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        When you reopen a long conversation, the opening turns and the most recent turns are kept word-for-word;
        the turns in between are condensed so the chat fits the model's context. <strong>The saved transcript is never changed</strong> — only what the model sees on resume.
      </Typography.Paragraph>
      {row('Keep opening turns verbatim on resume', <InputNumber min={0} value={keepFirst} onChange={v => setKeepFirst(Number(v) || 0)} disabled={!enabled} />)}
      {row('Keep recent turns verbatim on resume', <InputNumber min={1} value={keepRecent} onChange={v => setKeepRecent(Number(v) || 1)} disabled={!enabled} />)}
      {row(
        'Summarize older turns with a model call',
        <Switch checked={compactOnResume} onChange={setCompactOnResume} disabled={!enabled} />,
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        Off (default): older turns are condensed to a short note listing what they covered — no model call.
        On: an LLM summarizes them on resume (one extra call, slight delay; falls back to the note if it fails).
      </Typography.Paragraph>
      <Button type="primary" loading={saving} onClick={handleSave}>{saved ? 'Saved' : 'Save'}</Button>
    </div>
  )
}

// ── MCP tab (external MCP servers the chat agent connects to) ────────────────

const MCP_TRANSPORTS = [
  { value: 'stdio', label: 'stdio — local subprocess' },
  { value: 'http', label: 'http — streamable HTTP' },
  { value: 'sse', label: 'sse — server-sent events' },
]
const MCP_AUTONOMY = [
  { value: '', label: 'Confirm (default) — prompt before each call' },
  { value: 'autonomous', label: 'Autonomous — trusted, run without prompting' },
  { value: 'manual', label: 'Manual — human only (agent can’t call it)' },
]

type McpKV = { k: string; v: string }
const mcpKvToObj = (rows: McpKV[]): Record<string, string> => {
  const o: Record<string, string> = {}
  for (const { k, v } of rows) if (k.trim()) o[k.trim()] = v
  return o
}
const mcpObjToKv = (o?: Record<string, string>): McpKV[] =>
  Object.entries(o ?? {}).map(([k, v]) => ({ k, v: String(v) }))
const blankMcpServer = (): McpServer => ({ name: '', transport: 'stdio', enabled: true, autonomy: '' })

// A small key=value row editor (env vars / headers), mirroring the Telemetry
// tab's headers editor.
function KvEditor({ rows, onChange, keyPlaceholder, valuePlaceholder }: {
  rows: McpKV[]; onChange: (rows: McpKV[]) => void; keyPlaceholder: string; valuePlaceholder: string
}) {
  const set = (i: number, patch: Partial<McpKV>) => onChange(rows.map((r, idx) => idx === i ? { ...r, ...patch } : r))
  return (
    <div>
      {rows.map((r, i) => (
        <Space key={i} style={{ display: 'flex', marginBottom: 6 }} align="baseline">
          <Input size="small" placeholder={keyPlaceholder} value={r.k} style={{ width: 200, fontFamily: MONO_FONT }} onChange={e => set(i, { k: e.target.value })} />
          <Input size="small" placeholder={valuePlaceholder} value={r.v} style={{ width: 260 }} onChange={e => set(i, { v: e.target.value })} />
          <MinusCircleOutlined onClick={() => onChange(rows.filter((_, idx) => idx !== i))} />
        </Space>
      ))}
      <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => onChange([...rows, { k: '', v: '' }])}>Add</Button>
    </div>
  )
}

// Rows of (header name → keychain credential), for a server that needs more than
// one secret header (e.g. a Jira PAT and a Confluence PAT). Only the header name
// and the chosen credential's id are stored — the secret stays in the keychain and
// is resolved at connect time (see mcp_client.apply_header_credentials).
function McpHeaderCredEditor({ rows, options, onChange }: {
  rows: McpHeaderCredential[]; options: McpCredentialOption[]; onChange: (rows: McpHeaderCredential[]) => void
}) {
  const set = (i: number, patch: Partial<McpHeaderCredential>) =>
    onChange(rows.map((r, idx) => idx === i ? { ...r, ...patch } : r))
  return (
    <div>
      {rows.map((r, i) => (
        <Space key={i} style={{ display: 'flex', marginBottom: 6 }} align="baseline">
          <Input size="small" placeholder="X-Jira-PAT" value={r.header} style={{ width: 200, fontFamily: MONO_FONT }} onChange={e => set(i, { header: e.target.value })} />
          <Select size="small" allowClear placeholder="Credential" value={r.credential || undefined} style={{ width: 260 }}
            onChange={v => set(i, { credential: v ?? '' })}
            options={[{ value: '', label: 'None' }, ...options.map(c => ({ value: c.id, label: `${c.label} (${c.kind})` }))]} />
          <MinusCircleOutlined onClick={() => onChange(rows.filter((_, idx) => idx !== i))} />
        </Space>
      ))}
      <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => onChange([...rows, { header: '', credential: '' }])}>Add secret header</Button>
    </div>
  )
}

function McpServerStatusTag({ st }: { st?: McpServerStatus }) {
  if (!st) return <Tag>unknown</Tag>
  if (st.connected) return <Tag color="green">connected · {st.tool_count} tool{st.tool_count === 1 ? '' : 's'}</Tag>
  if (st.error) return <Tooltip title={st.error}><Tag color="red">error</Tag></Tooltip>
  return <Tag>not connected</Tag>
}

export function McpTab() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [sectionEnabled, setSectionEnabled] = useState(true)
  const [status, setStatus] = useState<McpStatus | null>(null)
  const [credOptions, setCredOptions] = useState<McpCredentialOption[]>([])
  const [editing, setEditing] = useState<number | null>(null)  // -1 = new, >=0 = edit index
  const [draft, setDraft] = useState<McpServer>(blankMcpServer())
  const [argsText, setArgsText] = useState('')
  const [envRows, setEnvRows] = useState<McpKV[]>([])
  const [headerRows, setHeaderRows] = useState<McpKV[]>([])
  const [callTimeout, setCallTimeout] = useState(60)
  const [connectTimeout, setConnectTimeout] = useState(20)
  const [busy, setBusy] = useState(false)
  // Last-persisted timeouts, so a blur only re-saves when a value actually
  // changed (the number inputs persist on blur, not per keystroke).
  const savedTimeouts = useRef({ call: 60, connect: 20 })

  const loadCfg = useCallback(() => {
    bridge.get_mcp_config().then(cfg => {
      setServers(cfg.servers ?? [])
      setSectionEnabled(cfg.enabled !== false)
      const call = cfg.call_timeout ?? 60
      const connect = cfg.connect_timeout ?? 20
      setCallTimeout(call); setConnectTimeout(connect)
      savedTimeouts.current = { call, connect }
    })
  }, [])
  const loadStatus = useCallback(() => { bridge.mcp_status().then(setStatus) }, [])
  const loadCreds = useCallback(() => { bridge.mcp_credential_options().then(setCredOptions) }, [])
  useEffect(() => { loadCfg(); loadStatus(); loadCreds() }, [loadCfg, loadStatus, loadCreds])
  useEffect(() => {
    // Keep the credential dropdown fresh when the Credentials tab edits one.
    window.addEventListener('runspec:credentials-changed', loadCreds)
    return () => window.removeEventListener('runspec:credentials-changed', loadCreds)
  }, [loadCreds])

  const statusFor = (name: string) => status?.servers.find(s => s.name === name)

  const persist = async (
    nextServers: McpServer[],
    enabled = sectionEnabled,
    call = callTimeout,
    connect = connectTimeout,
  ) => {
    setBusy(true)
    try {
      const st = await bridge.save_mcp_config({
        enabled, call_timeout: call, connect_timeout: connect, servers: nextServers,
      })
      setServers(nextServers); setStatus(st)
      savedTimeouts.current = { call, connect }
      message.success('MCP servers saved')
      return true
    } catch (e) {
      message.error(String((e as Error)?.message || e))
      return false
    } finally { setBusy(false) }
  }

  // Persist a timeout edit only when it changed (called on blur / Enter).
  const commitTimeouts = () => {
    if (callTimeout !== savedTimeouts.current.call || connectTimeout !== savedTimeouts.current.connect) {
      persist(servers)
    }
  }

  const startAdd = () => { setDraft(blankMcpServer()); setArgsText(''); setEnvRows([]); setHeaderRows([]); setEditing(-1) }
  const startEdit = (i: number) => {
    const s = servers[i]
    setDraft({ ...s, autonomy: s.autonomy ?? '' })
    setArgsText((s.args ?? []).join('\n'))
    setEnvRows(mcpObjToKv(s.env)); setHeaderRows(mcpObjToKv(s.headers))
    setEditing(i)
  }

  const saveDraft = async () => {
    const s: McpServer = {
      ...draft,
      name: draft.name.trim(),
      args: argsText.split('\n').map(a => a.trim()).filter(Boolean),
      env: mcpKvToObj(envRows),
      headers: mcpKvToObj(headerRows),
      header_credentials: (draft.header_credentials ?? [])
        .map(b => ({ header: b.header.trim(), credential: b.credential }))
        .filter(b => b.header && b.credential),
      credential: (draft.credential ?? '').trim(),
      credential_header: (draft.credential_header ?? '').trim(),
      proxy: (draft.proxy ?? '').trim(),
      command: (draft.command ?? '').trim(),
      url: (draft.url ?? '').trim(),
      // A positive number overrides the section default; anything else inherits
      // (kept null so the backend drops it from config.toml).
      connect_timeout: typeof draft.connect_timeout === 'number' && draft.connect_timeout > 0 ? draft.connect_timeout : null,
      call_timeout: typeof draft.call_timeout === 'number' && draft.call_timeout > 0 ? draft.call_timeout : null,
    }
    if (!s.name) { message.error('Name is required'); return }
    const dup = servers.some((o, i) => o.name === s.name && i !== editing)
    if (dup) { message.error(`Another server is already named "${s.name}"`); return }
    const next = editing === -1 ? [...servers, s] : servers.map((old, i) => i === editing ? s : old)
    if (await persist(next)) setEditing(null)
  }

  const remove = async (i: number) => { await persist(servers.filter((_, idx) => idx !== i)) }
  // Inline per-entry enable/disable — no need to open the edit dialog.
  const toggleEnabled = async (i: number, val: boolean) => {
    await persist(servers.map((s, idx) => (idx === i ? { ...s, enabled: val } : s)))
  }
  const reconnect = async () => { setBusy(true); try { setStatus(await bridge.mcp_reload()) } finally { setBusy(false) } }

  const isRemote = draft.transport === 'http' || draft.transport === 'sse'
  const editingName = editing !== null && editing >= 0 ? servers[editing]?.name : ''

  const columns = [
    { title: 'Name', dataIndex: 'name', key: 'name', render: (v: string) => <code>{v}</code> },
    { title: 'Transport', dataIndex: 'transport', key: 'transport' },
    { title: 'Endpoint', key: 'endpoint', render: (_: unknown, s: McpServer) => <Text style={{ fontSize: 13 }} type="secondary">{s.transport === 'stdio' ? [s.command, ...(s.args ?? [])].filter(Boolean).join(' ') : s.url}</Text> },
    { title: 'Auth', key: 'auth', render: (_: unknown, s: McpServer) => s.credential ? <Tag icon={<KeyOutlined />}>{s.credential}</Tag> : <Text type="secondary" style={{ fontSize: 13 }}>—</Text> },
    { title: 'Status', key: 'status', render: (_: unknown, s: McpServer) => s.enabled === false ? <Tag>disabled</Tag> : <McpServerStatusTag st={statusFor(s.name)} /> },
    { title: 'Enabled', key: 'enabled', width: 80, render: (_: unknown, s: McpServer, i: number) => (
      <Switch size="small" checked={s.enabled !== false} loading={busy} onChange={v => toggleEnabled(i, v)} aria-label={`Enable ${s.name}`} />
    ) },
    { title: '', key: 'actions', width: 90, render: (_: unknown, _s: McpServer, i: number) => (
      <Space>
        <EditOutlined onClick={() => startEdit(i)} />
        <Popconfirm title="Remove this MCP server?" onConfirm={() => remove(i)} okText="Remove" cancelText="Cancel">
          <DeleteOutlined />
        </Popconfirm>
      </Space>
    ) },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 13 }}>
          External MCP servers the chat agent connects to; their tools appear as <code>mcp_&lt;server&gt;_&lt;tool&gt;</code>, gated by the normal autonomy/confirm system.
        </Text>
        <Space>
          <Button size="small" icon={<ReloadOutlined />} loading={busy} onClick={reconnect}>Reconnect</Button>
          <Button size="small" type="primary" icon={<PlusOutlined />} onClick={startAdd}>Add</Button>
        </Space>
      </div>

      {status && !status.available && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message="MCP support not installed"
          description={<>Install the optional <code>mcp</code> extra to connect to external servers. {status.reason}</>} />
      )}

      <Space size="large" style={{ marginBottom: 12 }} wrap>
        <Space>
          <Switch size="small" checked={sectionEnabled} onChange={v => { setSectionEnabled(v); persist(servers, v) }} />
          <Text style={{ fontSize: 13 }}>Enable external MCP servers</Text>
        </Space>
        <Space>
          <Text style={{ fontSize: 13 }} type="secondary">Default connect timeout</Text>
          <InputNumber size="small" min={1} max={600} value={connectTimeout} style={{ width: 90 }} aria-label="Default connect timeout"
            onChange={v => setConnectTimeout(v ?? 20)} onBlur={commitTimeouts} onPressEnter={commitTimeouts} addonAfter="s" />
        </Space>
        <Space>
          <Text style={{ fontSize: 13 }} type="secondary">Default call timeout</Text>
          <InputNumber size="small" min={1} max={3600} value={callTimeout} style={{ width: 90 }} aria-label="Default call timeout"
            onChange={v => setCallTimeout(v ?? 60)} onBlur={commitTimeouts} onPressEnter={commitTimeouts} addonAfter="s" />
        </Space>
      </Space>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: -6, marginBottom: 12 }}>
        These apply to every server unless a server sets its own override (in its edit dialog).
      </Text>

      {servers.length === 0
        ? <Empty description="No MCP servers configured" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        : <Table size="small" rowKey="name" dataSource={servers} columns={columns} pagination={false} />}

      <Modal
        open={editing !== null}
        title={editing === -1 ? 'New MCP server' : `Edit — ${editingName}`}
        width={680}
        onCancel={() => setEditing(null)}
        onOk={saveDraft}
        okText="Save"
        confirmLoading={busy}
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form layout="vertical" size="small">
          <Form.Item label="Name" required tooltip="Short id; namespaces the tools (mcp_<name>_<tool>).">
            <Input placeholder="github" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
          </Form.Item>
          <Form.Item label="Transport">
            <Select options={MCP_TRANSPORTS} value={draft.transport} onChange={v => setDraft({ ...draft, transport: v })} />
          </Form.Item>
          {draft.transport === 'stdio' ? (
            <>
              <Form.Item label="Command" required tooltip="The executable to spawn (a stdio MCP server).">
                <Input placeholder="uvx" style={{ fontFamily: MONO_FONT }} value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })} />
              </Form.Item>
              <Form.Item label="Arguments" tooltip="One per line.">
                <Input.TextArea autoSize={{ minRows: 2 }} placeholder={'mcp-server-filesystem\n/srv/data'} style={{ fontFamily: MONO_FONT }} value={argsText} onChange={e => setArgsText(e.target.value)} />
              </Form.Item>
              <Form.Item label="Environment variables">
                <KvEditor rows={envRows} onChange={setEnvRows} keyPlaceholder="NAME" valuePlaceholder="value" />
              </Form.Item>
              <Form.Item label="Working directory">
                <Input placeholder="(optional)" style={{ fontFamily: MONO_FONT }} value={draft.cwd} onChange={e => setDraft({ ...draft, cwd: e.target.value })} />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item label="URL" required tooltip="The MCP server endpoint.">
                <Input placeholder="https://mcp.example.com/mcp" style={{ fontFamily: MONO_FONT }} value={draft.url} onChange={e => setDraft({ ...draft, url: e.target.value })} />
              </Form.Item>
              <Form.Item label="Headers" tooltip="Static request headers. A credential-derived Authorization header is added on top (unless you set one here).">
                <KvEditor rows={headerRows} onChange={setHeaderRows} keyPlaceholder="Header" valuePlaceholder="value" />
              </Form.Item>
            </>
          )}

          <Divider style={{ margin: '4px 0 12px' }} />

          <Form.Item label="Credential" tooltip="A Credentials-tab entry (keychain-backed). For http/sse it becomes an Authorization header (or the header below); for stdio it's injected as the credential's env vars. Only the id is stored. Pick “None” (or the clear button) to remove it.">
            <Select allowClear placeholder="None" value={draft.credential || undefined}
              onChange={v => setDraft({ ...draft, credential: v ?? '' })}
              options={[{ value: '', label: 'None' }, ...credOptions.map(c => ({ value: c.id, label: `${c.label} (${c.kind})` }))]} />
          </Form.Item>
          {isRemote && (
            <Form.Item label="Credential header" tooltip="Send the secret raw under this header (e.g. X-API-Key) instead of Authorization: Bearer.">
              <Input placeholder="Authorization (default)" value={draft.credential_header} onChange={e => setDraft({ ...draft, credential_header: e.target.value })} />
            </Form.Item>
          )}
          {isRemote && (
            <Form.Item label="Secret headers" tooltip="For a server needing more than one secret header (e.g. a Jira PAT and a Confluence PAT). Each maps a header name to a keychain credential; only the name + id are stored, the token is resolved at connect time. A static header of the same name above wins.">
              <McpHeaderCredEditor rows={draft.header_credentials ?? []} options={credOptions} onChange={rows => setDraft({ ...draft, header_credentials: rows })} />
            </Form.Item>
          )}
          <Form.Item label="Proxy" tooltip="Per-server HTTP CONNECT proxy — for servers on a different corporate network. Applies to http/sse; exported as *_PROXY for a stdio subprocess.">
            <Input placeholder="http://proxy:8080 (optional)" style={{ fontFamily: MONO_FONT }} value={draft.proxy} onChange={e => setDraft({ ...draft, proxy: e.target.value })} />
          </Form.Item>
          <Form.Item label="Timeout overrides" tooltip="Override the section's default connect / call timeouts for just this server. Leave blank to inherit.">
            <Space size="large">
              <Space size={6}>
                <Text style={{ fontSize: 13 }} type="secondary">Connect</Text>
                <InputNumber min={1} max={600} style={{ width: 130 }} aria-label="Connect timeout override"
                  placeholder={`${connectTimeout} (default)`} addonAfter="s"
                  value={draft.connect_timeout ?? undefined}
                  onChange={v => setDraft({ ...draft, connect_timeout: typeof v === 'number' ? v : null })} />
              </Space>
              <Space size={6}>
                <Text style={{ fontSize: 13 }} type="secondary">Call</Text>
                <InputNumber min={1} max={3600} style={{ width: 130 }} aria-label="Call timeout override"
                  placeholder={`${callTimeout} (default)`} addonAfter="s"
                  value={draft.call_timeout ?? undefined}
                  onChange={v => setDraft({ ...draft, call_timeout: typeof v === 'number' ? v : null })} />
              </Space>
            </Space>
          </Form.Item>
          <Form.Item label="Autonomy" tooltip="How this server's tools are gated. Confirm is safe-by-default.">
            <Select options={MCP_AUTONOMY} value={draft.autonomy ?? ''} onChange={v => setDraft({ ...draft, autonomy: v })} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Switch size="small" checked={draft.enabled !== false} onChange={v => setDraft({ ...draft, enabled: v })} />
              <Text style={{ fontSize: 13 }}>Enabled</Text>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

const OWN_MCP_AUTONOMY = [
  { value: '', label: 'Per-runnable (default)' },
  { value: 'autonomous', label: 'autonomous — run without asking' },
  { value: 'confirm', label: 'confirm — park for approval' },
  { value: 'supervised', label: 'supervised — park for approval' },
  { value: 'manual', label: 'manual — refuse (operator-only)' },
]

const OWN_MCP_TOOL_STYLE = [
  { value: 'leaf', label: 'Leaf — one tool per subcommand per host (serve-identical)' },
  { value: 'runnable', label: 'Per runnable — one tool per distinct runnable (fewest tools)' },
]

export function McpServerTab() {
  const [enabled, setEnabled] = useState(false)
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState<number>(8765)
  const [token, setToken] = useState('')
  const [autonomy, setAutonomy] = useState('')
  const [confirmWait, setConfirmWait] = useState<number>(50)
  const [toolStyle, setToolStyle] = useState('leaf')
  const [exposeBuiltins, setExposeBuiltins] = useState(false)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<Awaited<ReturnType<typeof bridge.mcp_server_status>> | null>(null)

  const loadStatus = useCallback(() => { bridge.mcp_server_status().then(setStatus).catch(() => {}) }, [])

  useEffect(() => {
    bridge.get_mcp_server_config().then(c => {
      setEnabled(c.enabled)
      setHost(c.host || '127.0.0.1')
      setPort(c.port || 8765)
      setToken(c.token || '')
      setAutonomy(c.default_autonomy || '')
      setConfirmWait(typeof c.confirm_wait === 'number' ? c.confirm_wait : 50)
      setToolStyle(c.tool_style || 'leaf')
      setExposeBuiltins(!!c.expose_builtins)
    })
    loadStatus()
    const t = setInterval(loadStatus, 4000)
    return () => clearInterval(t)
  }, [loadStatus])

  const handleSave = async () => {
    setSaving(true)
    try {
      const st = await bridge.save_mcp_server_config({
        enabled,
        host: host.trim() || '127.0.0.1',
        port: Math.max(1, Math.min(65535, Math.floor(port || 8765))),
        token: token.trim() || null,
        default_autonomy: autonomy || null,
        confirm_wait: Math.max(1, Math.floor(confirmWait || 50)),
        tool_style: toolStyle || 'leaf',
        expose_builtins: exposeBuiltins,
      })
      setStatus(st)
      message.success('Saved')
    } catch (e) {
      message.error(String((e as Error)?.message || e))
    } finally { setSaving(false) }
  }

  const row = (label: string, control: ReactNode) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
      <span style={{ fontSize: 14 }}>{label}</span>
      {control}
    </div>
  )

  return (
    <div style={{ maxWidth: 620 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        Expose this console's fleet runnables to an external coding agent (Claude Code, Cursor)
        as an <strong>MCP server</strong> on localhost. A <code>confirm</code>-level tool call parks
        for your approval in the console (the same confirm card the agent uses); <code>autonomous</code> runs
        immediately, <code>manual</code> is refused. Runs land in History tagged <code>mcp</code>. Off by default —
        it opens a listening socket. Requires the optional <code>mcp</code> extra to be installed.
      </Typography.Paragraph>

      {row('Enable server', <Switch checked={enabled} onChange={setEnabled} />)}
      {row('Host', <Input style={{ width: 200, fontFamily: MONO_FONT }} value={host} onChange={e => setHost(e.target.value)} disabled={!enabled} />)}
      {row('Port', <InputNumber min={1} max={65535} value={port} onChange={v => setPort(typeof v === 'number' ? v : 8765)} disabled={!enabled} />)}
      {row('Bearer token (optional)', <Input.Password style={{ width: 260 }} placeholder="none — localhost only" value={token} onChange={e => setToken(e.target.value)} disabled={!enabled} />)}
      {row('Default autonomy floor', <Select style={{ width: 260 }} value={autonomy} onChange={setAutonomy} options={OWN_MCP_AUTONOMY} disabled={!enabled} />)}
      {row('Confirm wait (seconds)', <InputNumber min={1} max={3600} value={confirmWait} onChange={v => setConfirmWait(typeof v === 'number' ? v : 50)} disabled={!enabled} />)}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -2 }}>
        How long a parked call waits for your approval before returning a resumable “awaiting
        approval” result. Keep it under your MCP client's own tool-call timeout
        (Claude Code's <code>MCP_TOOL_TIMEOUT</code> defaults to 60s).
      </Typography.Paragraph>

      {row('Tool style', <Select style={{ width: 360 }} value={toolStyle} onChange={setToolStyle} options={OWN_MCP_TOOL_STYLE} disabled={!enabled} />)}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -2 }}>
        <strong>Leaf</strong> mirrors <code>runspec serve</code> — every subcommand is its own tool,
        once per host (<code>jira_get-issue</code>…, plus <code>host__</code> copies), so a multi-host,
        multi-verb fleet can expose hundreds. <strong>Per runnable</strong> emits one tool per
        <em>distinct</em> runnable with a <code>command</code> arg to pick the verb and a <code>host</code>
        arg when it's on several hosts — collapsing both dimensions so the count matches this
        console's own tool list. Autonomy is still resolved per chosen subcommand either way.
      </Typography.Paragraph>

      {row('Expose built-in tools', <Switch checked={exposeBuiltins} onChange={setExposeBuiltins} disabled={!enabled} />)}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -2 }}>
        Also publish the console's own <em>console-specific</em> built-in tools so the coding agent
        gets your ops/fleet powers, not just the runnables: <code>console_status</code>,
        <code>search_history</code>, memory + runbooks, host→local file transfer
        (<code>download_file</code>/<code>relay_file</code>), Outlook, and the action tools
        (<code>create_document</code>, <code>create_schedule</code>, …). Reads run; writes park in the
        confirm card. The config + workspace/exec/web tools are never exposed. Off by default — it
        widens what an external process can drive.
      </Typography.Paragraph>

      <Button type="primary" onClick={handleSave} loading={saving} style={{ marginTop: 4 }}>Save</Button>

      <Divider style={{ margin: '14px 0' }} />
      <Typography.Paragraph style={{ fontSize: 13, marginBottom: 6 }}><strong>Status</strong></Typography.Paragraph>
      {status?.running ? (
        <Space direction="vertical" size={2}>
          <Space><Tag color="green">running</Tag><Typography.Text style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{status.url}</Typography.Text></Space>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>{status.tools} tool(s){status.builtins ? ` + ${status.builtins} built-in(s)` : ''} · auth {status.auth ? 'on' : 'off'}</Typography.Text>
          <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 6, marginBottom: 0 }}>
            Connect Claude Code:&nbsp;
            <code>claude mcp add --transport http runspec {status.url}{status.auth ? ' --header "Authorization: Bearer <token>"' : ''}</code>
          </Typography.Paragraph>
        </Space>
      ) : (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {status?.error ? `not running — ${status.error}` : 'not running'}
        </Typography.Text>
      )}
    </div>
  )
}

export function SettingsDrawer({ open, onClose, onHostsChanged, onKeyChanged, connectedHosts, allRemoteHosts }: SettingsDrawerProps) {
  // `[console] ai` off-switch: the agent tabs (Model / API, Agent tools,
  // Runbooks, MCP client, Chat History, Telemetry) are hidden; MCP Server stays.
  const features = useFeatures()
  return (
    <Drawer title="Settings" placement="right" width="min(96vw, 1040px)" open={open} onClose={onClose}>
      {/* With AI off ([console] ai = false) the agent Settings tabs are simply
          hidden (visibleSettingsTabs below) — no notice is shown. A locked-down
          console never advertises AI or how to re-enable it. */}
      <Tabs
        size="small"
        tabPosition="left"
        items={visibleSettingsTabs([
          { key: 'ssh',       label: 'SSH',        children: <SshTab onKeyChanged={onKeyChanged} connectedHosts={connectedHosts} allRemoteHosts={allRemoteHosts} /> },
          { key: 'llm',       label: 'Model / API',  children: <LlmTab /> },
          { key: 'jumpHosts', label: 'Jump Hosts',  children: <JumpHostsTab onHostsChanged={onHostsChanged} /> },
          { key: 'localVenvs', label: 'Local venvs', children: <LocalVenvsTab /> },
          { key: 'agentTools', label: 'Agent tools', children: <AgentToolsTab /> },
          { key: 'runbooks',    label: 'Runbooks',     children: <RunbooksTab /> },
          { key: 'savedRuns',   label: 'Saved runs',   children: <SavedRunsTab /> },
          { key: 'selfService', label: 'Self-Service', children: <SelfServiceTab /> },
          { key: 'filters',   label: 'Filters',    children: <FiltersTab /> },
          { key: 'repos',     label: 'Repos',      children: <ReposTab /> },
          { key: 'credentials', label: 'Credentials', children: <CredentialsTab /> },
          { key: 'denylist',  label: 'Denylist',   children: <DenylistTab /> },
          { key: 'mcp',       label: 'MCP',        children: <McpTab /> },
          { key: 'mcpServer', label: 'MCP Server', children: <McpServerTab /> },
          { key: 'configSync', label: 'Config Sync', children: <ConfigSyncTab /> },
          { key: 'mailbox',   label: 'Mailbox',    children: <MailboxesTab /> },
          { key: 'teams',     label: 'Teams',      children: <TeamsTab /> },
          { key: 'sounds',    label: 'Sounds & Notifications', children: <SoundsTab /> },
          { key: 'chatHistory', label: 'Chat History', children: <ChatHistoryTab /> },
          { key: 'telemetry', label: 'Telemetry', children: <TelemetryTab /> },
        ], features)}
      />
    </Drawer>
  )
}
