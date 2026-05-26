import { useEffect, useState } from 'react'
import { Button, Input, InputNumber, Modal, Select, Switch, Tag, Tooltip, Typography, message } from 'antd'
import { SearchOutlined, RightOutlined, ArrowLeftOutlined, AppstoreOutlined } from '@ant-design/icons'
import type { ArgDef, Host, Runnable } from '../bridge'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
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
  value: unknown
  error?: string
  onChange: (v: unknown) => void
}

function ArgInput({ arg, value, error, onChange }: ArgInputProps) {
  const isDark = useIsDark()
  const placeholder = isVarRef(arg.default) ? arg.default as string : undefined

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

  if (arg.type === 'int' || arg.type === 'float') {
    return (
      <InputNumber
        size="small"
        value={value as number | undefined}
        onChange={onChange}
        placeholder={placeholder}
        step={arg.type === 'float' ? 0.1 : 1}
        style={{ width: '100%', ...inputStyle }}
        status={error ? 'error' : undefined}
      />
    )
  }

  return (
    <Input
      size="small"
      value={value as string ?? ''}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder ?? (arg.type === 'path' ? '/path/to/…' : undefined)}
      style={inputStyle}
      status={error ? 'error' : undefined}
    />
  )
}

function ArgForm({ node, values, errors, onChange }: {
  node: Runnable
  values: Record<string, unknown>
  errors: Record<string, string>
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {args.map(arg => (
        <div key={arg.name}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
            <Text code style={{ fontSize: 12 }}>--{arg.name}</Text>
            <Tag color="default" style={{ fontSize: 10, margin: 0 }}>{arg.type}</Tag>
            {arg.required
              ? <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>required</Tag>
              : isVarRef(arg.default)
                ? <Text style={{ fontSize: 10, color: '#fa8c16', fontFamily: 'monospace' }}>{arg.default as string}</Text>
                : <Text type="secondary" style={{ fontSize: 10 }}>optional</Text>
            }
            {arg.description && (
              <Text type="secondary" style={{ fontSize: 11, marginLeft: 2 }}>{arg.description}</Text>
            )}
          </div>
          <ArgInput
            arg={arg}
            value={values[arg.name]}
            error={errors[arg.name]}
            onChange={v => onChange(arg.name, v)}
          />
          {errors[arg.name] && (
            <Text style={{ fontSize: 11, color: '#ff4d4f' }}>{errors[arg.name]}</Text>
          )}
        </div>
      ))}
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
          {(cmd.args ?? []).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag style={{ fontSize: 10 }}>{(cmd.args ?? []).length} args</Tag>
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
  onSubmit: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[]) => void
}

function RunModal({ state, onClose, onSubmit }: RunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})

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
  }, [state?.rootRunnable.name, state?.commandPath.join('/')])

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
      if (arg.required && (values[arg.name] === undefined || values[arg.name] === '')) {
        errs[arg.name] = 'Required'
      }
    }
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    onSubmit(rootRunnable, values, commandPath)
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
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 14 }}>
            {currentNode.description ?? 'Choose a subcommand:'}
          </Text>
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
          <ArgForm node={currentNode} values={values} errors={errors} onChange={handleChange} />
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
          {subCount > 0 && <Tag style={{ fontSize: 10, margin: 0 }}>{subCount} subcommands</Tag>}
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
        {runnable.runAs && (
          <Text type="secondary" style={{ fontSize: 10, marginLeft: 'auto' }}>
            as <Text code style={{ fontSize: 10 }}>{runnable.runAs}</Text>
          </Text>
        )}
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
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[]) => void
  pendingForm?: PendingForm | null
  onPendingFormClear?: () => void
}

export function FormsView({
  runnables, hosts, selectedHost, activeScope,
  onRunRunnable, pendingForm, onPendingFormClear,
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

      {filtered.length === 0 ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#555' }}>
          <div style={{ textAlign: 'center' }}>
            <AppstoreOutlined style={{ fontSize: 32, marginBottom: 12, display: 'block' }} />
            <Text type="secondary">No runnables match the current filter.</Text>
          </div>
        </div>
      ) : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
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
                gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))',
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
