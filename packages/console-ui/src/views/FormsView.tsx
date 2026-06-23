import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Button, Input, InputNumber, Modal, Select, Switch, Tag, Tooltip, Typography, message } from 'antd'
import { SearchOutlined, RightOutlined, ArrowLeftOutlined, AppstoreOutlined } from '@ant-design/icons'
import type { ArgDef, Host, Runnable, Skill } from '../bridge'
import { bridge } from '../bridge'
import { useIsDark } from '../ThemeContext'
import { PathInput } from './PathInput'

const { Text } = Typography

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
}

// Whether the run form should block submission because a required arg has no
// value. A `password` arg never blocks: its value always arrives out-of-band — a
// bound credential's secret (injected from the keyring), an `env`/`$VAR` alias
// the form can't see, or the masked field itself — and the form can't validate a
// secret anyway. A non-secret required arg blocks when empty unless a credential
// covers it (`covered`). Exported for unit tests.
export function argNeedsValue(arg: ArgDef, value: unknown, covered: boolean): boolean {
  if (arg.type === 'password') return false
  if (!arg.required || covered) return false
  return value === undefined || value === ''
}

function resolveNode(root: Runnable, path: string[]): Runnable {
  let node = root
  for (const seg of path) {
    node = node.commands![seg]
  }
  return node
}

// ─── Arg form ────────────────────────────────────────────────────────────────

interface ArgInputProps {
  arg: ArgDef
  host: string
  runnable: string
  runAs?: string
  group?: string
  value: unknown
  error?: string
  covered?: boolean
  onChange: (v: unknown) => void
}

export function ArgInput({ arg, host, runnable, runAs, group, value, error, covered, onChange }: ArgInputProps) {
  const isDark = useIsDark()
  // `hint` (if declared) is the form placeholder; otherwise fall back to a
  // visible $VAR_REF default so the operator sees where the value comes from.
  const placeholder = arg.hint ?? (isVarRef(arg.default) ? arg.default as string : undefined)

  const inputStyle = {
    fontFamily: 'monospace',
    fontSize: 12,
    borderColor: error ? '#ff4d4f' : undefined,
  }

  if (arg.type === 'flag') {
    return (
      <Switch
        size="small"
        checked={!!value}
        onChange={onChange}
      />
    )
  }

  if (arg.type === 'choice' && arg.options) {
    return (
      <Select
        size="small"
        value={value as string | undefined}
        onChange={onChange}
        placeholder="Select…"
        style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
        status={error ? 'error' : undefined}
        options={arg.options.map(o => ({ label: o, value: o }))}
      />
    )
  }

  if (arg.type === 'password') {
    // Masked entry. The typed value is sent to the run as the
    // RUNSPEC_<RUNNABLE>_ARG_<NAME> env var by the bridge — never as a CLI arg —
    // so it stays out of argv, shell history, and the run/args record.
    return (
      <Input.Password
        size="small"
        value={value as string ?? ''}
        onChange={e => onChange(e.target.value)}
        placeholder={covered ? 'will be filled from the selected credential — leave blank' : (placeholder ?? 'secret — sent as an env var, not on the command line')}
        autoComplete="new-password"
        style={inputStyle}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.type === 'int' || arg.type === 'float') {
    return (
      <InputNumber
        size="small"
        value={value as number | undefined}
        onChange={onChange}
        placeholder={placeholder}
        step={arg.type === 'float' ? 0.1 : 1}
        min={arg.range?.[0]}
        max={arg.range?.[1]}
        style={{ width: '100%', ...inputStyle }}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.type === 'path') {
    // Browsable / autocompleting path field. Lists the host the runnable will
    // run on; `ui = "directory"` restricts picking to folders.
    return (
      <PathInput
        host={host}
        runnable={runnable}
        runAs={runAs}
        group={group}
        value={value as string ?? ''}
        placeholder={placeholder}
        dirsOnly={arg.ui === 'directory'}
        error={!!error}
        onChange={onChange}
      />
    )
  }

  return (
    <Input
      size="small"
      value={value as string ?? ''}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      style={inputStyle}
      status={error ? 'error' : undefined}
    />
  )
}

function SectionLabel({ children }: { children: ReactNode }) {
  const isDark = useIsDark()
  return (
    <div style={{
      fontSize: 10, color: isDark ? '#aaa' : '#999',
      textTransform: 'uppercase', letterSpacing: '0.08em',
      paddingBottom: 5, borderBottom: `1px solid ${isDark ? '#222' : '#e8e8e8'}`,
    }}>
      {children}
    </div>
  )
}

function ArgRow({ arg, host, runnable, runAs, group, value, error, covered, onChange }: ArgInputProps) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
        <Text code style={{ fontSize: 12 }}>--{arg.name}</Text>
        <Tag color="default" style={{ fontSize: 10, margin: 0 }}>
          {arg.type === 'path' && arg.ui === 'directory' ? 'folder' : arg.type}
        </Tag>
        {arg.inherited && (
          <Tooltip title="Inherited from a global arg or a parent command — applies to this run">
            <Tag color="blue" style={{ fontSize: 10, margin: 0 }}>inherited</Tag>
          </Tooltip>
        )}
        {covered
          ? <Tooltip title="A saved credential fills this argument from secure storage — no need to enter it"><Tag color="green" style={{ fontSize: 10, margin: 0 }}>from credential</Tag></Tooltip>
          : arg.type === 'password'
            ? <Tooltip title="A secret — supplied by a bound credential, an env var, or typed here. The form never blocks on it."><Tag color="default" style={{ fontSize: 10, margin: 0 }}>secret · optional here</Tag></Tooltip>
            : arg.required
              ? <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>required</Tag>
              : isVarRef(arg.default)
                ? <Text style={{ fontSize: 10, color: '#fa8c16', fontFamily: 'monospace' }}>{arg.default as string}</Text>
                : <Text type="secondary" style={{ fontSize: 10 }}>optional</Text>
        }
        {arg.description && (
          <Text type="secondary" style={{ fontSize: 11, marginLeft: 2 }}>{arg.description}</Text>
        )}
      </div>
      <ArgInput arg={arg} host={host} runnable={runnable} runAs={runAs} group={group} value={value} error={error} covered={covered} onChange={onChange} />
      {error && <Text style={{ fontSize: 11, color: '#ff4d4f' }}>{error}</Text>}
    </div>
  )
}

function ArgForm({ node, host, runnable, runAs, group, values, errors, coveredArgs, onChange }: {
  node: Runnable
  host: string
  runnable: string
  runAs?: string
  group?: string
  values: Record<string, unknown>
  errors: Record<string, string>
  coveredArgs?: Set<string>
  onChange: (name: string, v: unknown) => void
}) {
  const args = node.args ?? []

  if (args.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        No arguments — ready to run.
      </Text>
    )
  }

  // Globals/parent-command args (merged in by discovery) render in their own
  // section above the command's own args, so ambient values are clearly scoped.
  const inherited = args.filter(a => a.inherited)
  const own = args.filter(a => !a.inherited)

  const row = (arg: ArgDef) => (
    <ArgRow
      key={arg.name}
      arg={arg}
      host={host}
      runnable={runnable}
      runAs={runAs}
      group={group}
      value={values[arg.name]}
      error={errors[arg.name]}
      covered={coveredArgs?.has(arg.name)}
      onChange={v => onChange(arg.name, v)}
    />
  )

  // Flat runnable (no inheritance) — render exactly as before, no section chrome.
  if (inherited.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {own.map(row)}
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <SectionLabel>Inherited</SectionLabel>
        {inherited.map(row)}
      </div>
      {own.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <SectionLabel>This command</SectionLabel>
          {own.map(row)}
        </div>
      )}
    </div>
  )
}

// ─── Subcommand picker ────────────────────────────────────────────────────────

function SubcommandPicker({ node, onSelect }: {
  node: Runnable
  onSelect: (name: string) => void
}) {
  const isDark = useIsDark()
  const commands = Object.entries(node.commands ?? {})
  const border = isDark ? '#2a2a2a' : '#e8e8e8'
  const hoverBg = isDark ? '#1e1e1e' : '#f5f5f5'

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
      {commands.map(([name, cmd]) => (
        <div
          key={name}
          onClick={() => onSelect(name)}
          style={{
            border: `1px solid ${border}`, borderRadius: 8, padding: '14px 16px',
            cursor: 'pointer', transition: 'background 0.15s',
          }}
          onMouseEnter={e => (e.currentTarget.style.background = hoverBg)}
          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
            <Text style={{ fontFamily: 'monospace', fontSize: 13 }}>{name}</Text>
            <RightOutlined style={{ fontSize: 10, color: '#555' }} />
          </div>
          {cmd.description && (
            <Text type="secondary" style={{ fontSize: 12 }}>{cmd.description}</Text>
          )}
          {(cmd.args ?? []).filter(a => !a.inherited).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag style={{ fontSize: 10 }}>{(cmd.args ?? []).filter(a => !a.inherited).length} args</Tag>
            </div>
          )}
          {Object.keys(cmd.commands ?? {}).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag style={{ fontSize: 10 }}>{Object.keys(cmd.commands!).length} subcommands</Tag>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ─── Run modal ───────────────────────────────────────────────────────────────

interface ModalState {
  rootRunnable: Runnable
  commandPath: string[]
  currentNode: Runnable
}

interface RunModalProps {
  state: ModalState | null
  onClose: () => void
  onSubmit: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[], credentialId?: string) => void
}

function RunModal({ state, onClose, onSubmit }: RunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [credentialId, setCredentialId] = useState('')
  const [creds, setCreds] = useState<{ id: string; label: string; has_secret: boolean; secret_arg: string; username_arg: string; username: string }[]>([])

  // Reset form when the modal or node changes
  useEffect(() => {
    if (!state) return
    const defaults: Record<string, unknown> = {}
    for (const arg of state.currentNode.args ?? []) {
      if (arg.default !== undefined && !isVarRef(arg.default)) {
        defaults[arg.name] = arg.default
      }
    }
    setValues(defaults)
    setErrors({})
    setCredentialId('')
    // Offer any in-scope credentials for this runnable as an optional override.
    bridge.credentials_for_runnable(state.rootRunnable.host, state.rootRunnable.name)
      .then(setCreds).catch(() => setCreds([]))
  }, [state?.rootRunnable.name, state?.rootRunnable.host, state?.commandPath.join('/')])

  // Arg names a credential will fill at run time, so a required field they cover
  // doesn't block the form (the secret is injected from the keyring, never typed
  // here). Mirrors the backend's apply rules: an explicitly picked credential
  // applies its bindings; otherwise every in-scope credential auto-applies, but a
  // binding shared by two of them is ambiguous (the backend skips it) so it isn't
  // counted. secret_arg counts only when a secret is actually stored; username_arg
  // only when a username is set.
  const coveredArgs = useMemo(() => {
    const effective = credentialId ? creds.filter(c => c.id === credentialId) : creds
    const secretCount: Record<string, number> = {}
    const userCount: Record<string, number> = {}
    for (const c of effective) {
      if (c.secret_arg) secretCount[c.secret_arg] = (secretCount[c.secret_arg] ?? 0) + 1
      if (c.username_arg) userCount[c.username_arg] = (userCount[c.username_arg] ?? 0) + 1
    }
    const covered = new Set<string>()
    for (const c of effective) {
      const unambiguous = (n: string, counts: Record<string, number>) => !!credentialId || counts[n] === 1
      if (c.secret_arg && c.has_secret && unambiguous(c.secret_arg, secretCount)) covered.add(c.secret_arg)
      if (c.username_arg && c.username && unambiguous(c.username_arg, userCount)) covered.add(c.username_arg)
    }
    return covered
  }, [creds, credentialId])

  if (!state) return null

  const { rootRunnable, commandPath, currentNode } = state
  const hasSubcommands = Object.keys(currentNode.commands ?? {}).length > 0
  const isAtArgs = !hasSubcommands

  const crumbs = [rootRunnable.name, ...commandPath]

  const handleChange = (name: string, v: unknown) => {
    setValues(prev => ({ ...prev, [name]: v }))
    setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const handleSubmit = () => {
    const errs: Record<string, string> = {}
    for (const arg of currentNode.args ?? []) {
      const v = values[arg.name]
      if (argNeedsValue(arg, v, coveredArgs.has(arg.name))) {
        errs[arg.name] = 'Required'
      } else if (arg.range && (arg.type === 'int' || arg.type === 'float') && v !== undefined && v !== '') {
        const n = Number(v)
        const [lo, hi] = arg.range
        if (n < lo || n > hi) errs[arg.name] = `Must be between ${lo} and ${hi}`
      }
    }
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    onSubmit(rootRunnable, values, commandPath, credentialId || undefined)
    onClose()
    message.success(`/${[rootRunnable.name, ...commandPath].join(' ')} submitted`)
  }

  const navigateTo = (depth: number) => {
    const newPath = commandPath.slice(0, depth)
    const newNode = resolveNode(rootRunnable, newPath)
    // We can't mutate state prop — bubble up via onClose then re-open, so instead
    // we dispatch a custom event the parent listens to for back navigation
    window.dispatchEvent(new CustomEvent('forms:navigate', { detail: { rootRunnable, commandPath: newPath, currentNode: newNode } }))
  }

  return (
    <Modal
      open={true}
      onCancel={onClose}
      width={540}
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {crumbs.map((seg, i) => (
            <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              {i > 0 && <RightOutlined style={{ fontSize: 10, color: '#555' }} />}
              <Text
                style={{
                  fontFamily: 'monospace', fontSize: 13,
                  color: i === crumbs.length - 1 ? undefined : '#888',
                  cursor: i < crumbs.length - 1 ? 'pointer' : 'default',
                }}
                onClick={() => i < crumbs.length - 1 ? navigateTo(i) : undefined}
              >
                {seg}
              </Text>
            </span>
          ))}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
            {rootRunnable.autonomy === 'autonomous' && (
              <Tooltip title="autonomy: autonomous">
                <Tag color="green" style={{ fontSize: 10, margin: 0 }}>auto</Tag>
              </Tooltip>
            )}
            {rootRunnable.runAs && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                as <Text code style={{ fontSize: 11 }}>{rootRunnable.runAs}</Text>
              </Text>
            )}
          </div>
        </div>
      }
      footer={
        isAtArgs ? (
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button onClick={onClose}>Cancel</Button>
            <Button type="primary" onClick={handleSubmit}>Run</Button>
          </div>
        ) : null
      }
    >
      {hasSubcommands ? (
        <div>
          {currentNode.description && (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: currentNode.requireCommand ? 8 : 14 }}>
              {currentNode.description}
            </Text>
          )}
          {currentNode.requireCommand ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
              <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>command required</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                This runnable can't run on its own — pick a command to continue.
              </Text>
            </div>
          ) : (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 14 }}>
              Choose a subcommand:
            </Text>
          )}
          <SubcommandPicker node={currentNode} onSelect={name => {
            const newPath = [...commandPath, name]
            window.dispatchEvent(new CustomEvent('forms:navigate', {
              detail: { rootRunnable, commandPath: newPath, currentNode: resolveNode(rootRunnable, newPath) }
            }))
          }} />
        </div>
      ) : (
        <div>
          {currentNode.description && (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 16 }}>
              {currentNode.description}
            </Text>
          )}
          <ArgForm node={currentNode} host={rootRunnable.host} runnable={rootRunnable.name} runAs={rootRunnable.runAs} group={rootRunnable.group} values={values} errors={errors} coveredArgs={coveredArgs} onChange={handleChange} />
          {creds.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Text style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                Credential <Text type="secondary" style={{ fontSize: 11 }}>(in-scope ones apply automatically — pick to override)</Text>
              </Text>
              <Select
                size="small"
                value={credentialId || undefined}
                onChange={v => setCredentialId(v ?? '')}
                placeholder="auto (by scope)"
                style={{ width: '100%' }}
                allowClear
                options={creds.map(c => ({ label: c.label, value: c.id }))}
              />
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}

// ─── Runnable card ────────────────────────────────────────────────────────────

function RunnableCard({ runnable, hosts, onClick }: {
  runnable: Runnable
  hosts: Host[]
  onClick: () => void
}) {
  const isDark = useIsDark()
  const [hovered, setHovered] = useState(false)

  const hostObj = hosts.find(h => h.name === runnable.host)
  const connected = hostObj?.connected ?? true
  const argCount = (runnable.args ?? []).length
  const requiredCount = (runnable.args ?? []).filter(a => a.required).length
  const subCount = Object.keys(runnable.commands ?? {}).length
  const isAuto = runnable.autonomy === 'autonomous'
  const isInstant = argCount === 0 && subCount === 0

  const border = hovered
    ? (isDark ? '#4fc1ff' : '#1677ff')
    : (isDark ? '#2a2a2a' : '#e0e0e0')
  const bg = hovered
    ? (isDark ? 'rgba(79,193,255,0.04)' : 'rgba(22,119,255,0.03)')
    : (isDark ? '#141414' : '#fafafa')

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: `1px solid ${border}`, borderRadius: 8, padding: '14px 16px',
        cursor: 'pointer', background: bg, transition: 'border-color 0.15s, background 0.15s',
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontSize: 9, color: connected ? '#52c41a' : '#595959', flexShrink: 0 }}>●</span>
        <Text style={{ fontFamily: 'monospace', fontSize: 13, fontWeight: 600 }}>{runnable.name}</Text>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          {isInstant && (
            <Tooltip title="No arguments — runs immediately on click">
              <Tag color="gold" style={{ fontSize: 10, margin: 0 }}>⚡ instant</Tag>
            </Tooltip>
          )}
          {isAuto && (
            <Tooltip title="autonomy: autonomous — the LLM can invoke this without asking">
              <Tag color="green" style={{ fontSize: 10, margin: 0 }}>auto</Tag>
            </Tooltip>
          )}
        </div>
      </div>
      {runnable.description && (
        <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.4 }}>{runnable.description}</Text>
      )}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2, alignItems: 'center' }}>
        <Tag style={{ fontSize: 10, margin: 0 }}>{runnable.host}</Tag>
        {argCount > 0 && (
          <Tag color={requiredCount > 0 ? 'orange' : 'default'} style={{ fontSize: 10, margin: 0 }}>
            {argCount} arg{argCount !== 1 ? 's' : ''}{requiredCount > 0 ? ` · ${requiredCount} req` : ''}
          </Tag>
        )}
        {subCount > 0 && (
          runnable.requireCommand ? (
            <Tooltip title="A command is required — this runnable can't run without choosing one of its subcommands">
              <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>{subCount} subcommands · required</Tag>
            </Tooltip>
          ) : (
            <Tag style={{ fontSize: 10, margin: 0 }}>{subCount} subcommands</Tag>
          )
        )}
        {runnable.runAs && (
          <Text type="secondary" style={{ fontSize: 10, marginLeft: 'auto' }}>
            as <Text code style={{ fontSize: 10 }}>{runnable.runAs}</Text>
          </Text>
        )}
      </div>
    </div>
  )
}

// A saved skill, rendered as a launchable card alongside runnables. Clicking opens
// the same parameter form the `$` chat menu uses (App owns the SkillRunModal).
function SkillCard({ skill, onClick }: { skill: Skill; onClick: () => void }) {
  const isDark = useIsDark()
  const [hovered, setHovered] = useState(false)

  const argCount = (skill.args ?? []).length
  const requiredCount = (skill.args ?? []).filter(a => a.required).length

  const accent = isDark ? '#bd93f9' : '#722ed1'
  const border = hovered ? accent : (isDark ? '#2a2a2a' : '#e0e0e0')
  const bg = hovered
    ? (isDark ? 'rgba(189,147,249,0.05)' : 'rgba(114,46,209,0.03)')
    : (isDark ? '#141414' : '#fafafa')

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: `1px solid ${border}`, borderRadius: 8, padding: '14px 16px',
        cursor: 'pointer', background: bg, transition: 'border-color 0.15s, background 0.15s',
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Text style={{ fontFamily: 'monospace', fontSize: 13, fontWeight: 600, color: accent }}>
          ${skill.id}
        </Text>
        <div style={{ marginLeft: 'auto' }}>
          <Tag color="purple" style={{ fontSize: 10, margin: 0 }}>skill</Tag>
        </div>
      </div>
      {(skill.title || skill.symptom) && (
        <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.4 }}>
          {skill.title}{skill.title && skill.symptom ? ' — ' : ''}{skill.symptom}
        </Text>
      )}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2, alignItems: 'center' }}>
        {argCount > 0 ? (
          <Tag color={requiredCount > 0 ? 'orange' : 'default'} style={{ fontSize: 10, margin: 0 }}>
            {argCount} param{argCount !== 1 ? 's' : ''}{requiredCount > 0 ? ` · ${requiredCount} req` : ''}
          </Tag>
        ) : (
          <Tag style={{ fontSize: 10, margin: 0 }}>no params</Tag>
        )}
        {(skill.tags ?? []).slice(0, 3).map(t => (
          <Tag key={t} style={{ fontSize: 10, margin: 0 }}>{t}</Tag>
        ))}
      </div>
    </div>
  )
}

// ─── Main view ───────────────────────────────────────────────────────────────

export interface PendingForm {
  runnable: Runnable
  commandPath: string[]
}

interface FormsViewProps {
  runnables: Runnable[]
  hosts: Host[]
  selectedHost: string
  activeScope: string[]
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[], credentialId?: string) => void
  pendingForm?: PendingForm | null
  onPendingFormClear?: () => void
  skills?: Skill[]
  onOpenSkill?: (skill: Skill) => void
}

export function FormsView({
  runnables, hosts, selectedHost, activeScope,
  onRunRunnable, pendingForm, onPendingFormClear,
  skills = [], onOpenSkill,
}: FormsViewProps) {
  const isDark = useIsDark()
  const [search, setSearch] = useState('')
  const [modalState, setModalState] = useState<ModalState | null>(null)

  // Open modal when slash command routes a pending form here
  useEffect(() => {
    if (!pendingForm) return
    setModalState({
      rootRunnable: pendingForm.runnable,
      commandPath: pendingForm.commandPath,
      currentNode: resolveNode(pendingForm.runnable, pendingForm.commandPath),
    })
    onPendingFormClear?.()
  }, [pendingForm])

  // Internal breadcrumb / subcommand picker navigation via custom event
  useEffect(() => {
    const onNavigate = (e: Event) => {
      const { rootRunnable, commandPath, currentNode } = (e as CustomEvent).detail as ModalState
      setModalState({ rootRunnable, commandPath, currentNode })
    }
    window.addEventListener('forms:navigate', onNavigate)
    return () => window.removeEventListener('forms:navigate', onNavigate)
  }, [])

  const openModal = (runnable: Runnable, commandPath: string[] = []) => {
    setModalState({
      rootRunnable: runnable,
      commandPath,
      currentNode: resolveNode(runnable, commandPath),
    })
  }

  const handleCardClick = (runnable: Runnable) => {
    const hasSubcommands = Object.keys(runnable.commands ?? {}).length > 0
    const hasAnyArgs = (runnable.args ?? []).length > 0
    if (!hasSubcommands && !hasAnyArgs) {
      // Zero args, zero subcommands — run immediately
      onRunRunnable(runnable, {}, [])
      return
    }
    openModal(runnable)
  }

  // Filter by host selection and group scope
  const hostFiltered = selectedHost
    ? runnables.filter(r => r.host === selectedHost)
    : runnables

  const scopeFiltered = activeScope.length > 0
    ? hostFiltered.filter(r => activeScope.includes(r.group))
    : hostFiltered

  // Text search — also searches subcommand names/descriptions
  const searchMatch = (r: Runnable, q: string): boolean => {
    const low = q.toLowerCase()
    if (r.name.toLowerCase().includes(low)) return true
    if ((r.description ?? '').toLowerCase().includes(low)) return true
    if (r.host.toLowerCase().includes(low)) return true
    if (r.group.toLowerCase().includes(low)) return true
    return Object.entries(r.commands ?? {}).some(([name, cmd]) =>
      name.includes(low) || (cmd.description ?? '').toLowerCase().includes(low)
    )
  }

  const filtered = search
    ? scopeFiltered.filter(r => searchMatch(r, search))
    : scopeFiltered

  // Skills are host-agnostic, so they ignore the host/scope filters — only the
  // text search narrows them. Shown as their own swimlane above the runnables.
  const filteredSkills = (onOpenSkill && skills.length > 0)
    ? (search
        ? skills.filter(s => {
            const low = search.toLowerCase()
            return s.id.toLowerCase().includes(low) ||
              (s.title ?? '').toLowerCase().includes(low) ||
              (s.symptom ?? '').toLowerCase().includes(low) ||
              (s.tags ?? []).some(t => t.toLowerCase().includes(low))
          })
        : skills)
    : []

  // Group into swimlanes preserving existing group order
  const groupMap = new Map<string, Runnable[]>()
  for (const r of filtered) {
    if (!groupMap.has(r.group)) groupMap.set(r.group, [])
    groupMap.get(r.group)!.push(r)
  }

  const borderCol = isDark ? '#222' : '#e0e0e0'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ marginBottom: 16, flexShrink: 0 }}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="Search name, group, host, description…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          allowClear
          style={{ maxWidth: 360 }}
        />
      </div>

      {filtered.length === 0 && filteredSkills.length === 0 ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555' }}>
          <div style={{ textAlign: 'center' }}>
            <AppstoreOutlined style={{ fontSize: 32, marginBottom: 12, display: 'block' }} />
            <Text type="secondary">Nothing matches the current filter.</Text>
          </div>
        </div>
      ) : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {filteredSkills.length > 0 && (
            <div style={{ marginBottom: 28 }}>
              <div style={{
                fontSize: 10, color: isDark ? '#aaa' : '#999',
                textTransform: 'uppercase', letterSpacing: '0.08em',
                marginBottom: 12, paddingBottom: 6,
                borderBottom: `1px solid ${borderCol}`,
              }}>
                Skills
              </div>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                gap: 10,
              }}>
                {filteredSkills.map(s => (
                  <SkillCard key={s.id} skill={s} onClick={() => onOpenSkill?.(s)} />
                ))}
              </div>
            </div>
          )}
          {[...groupMap.entries()].map(([group, groupRunnables]) => (
            <div key={group} style={{ marginBottom: 28 }}>
              <div style={{
                fontSize: 10, color: isDark ? '#aaa' : '#999',
                textTransform: 'uppercase', letterSpacing: '0.08em',
                marginBottom: 12, paddingBottom: 6,
                borderBottom: `1px solid ${borderCol}`,
              }}>
                {group}
              </div>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                gap: 10,
              }}>
                {groupRunnables.map(r => (
                  <RunnableCard
                    key={`${r.host}/${r.group}/${r.name}`}
                    runnable={r}
                    hosts={hosts}
                    onClick={() => handleCardClick(r)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <RunModal
        state={modalState}
        onClose={() => setModalState(null)}
        onSubmit={onRunRunnable}
      />
    </div>
  )
}
