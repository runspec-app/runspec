import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Button, Checkbox, Collapse, Input, Modal, Select, Tag, Tooltip, Typography, message } from 'antd'
import { SearchOutlined, RightOutlined, ArrowLeftOutlined, AppstoreOutlined, EditOutlined, PlusOutlined, DownOutlined } from '@ant-design/icons'
import type { ArgDef, Host, Runnable, Runbook, SavedRun } from '../bridge'
import { bridge } from '../bridge'
import { useIsDark, useAccent, withAlpha, surfaces, RADIUS, textSecondary, MONO_FONT } from '../ThemeContext'
import { runnableHidden } from '../toolVisibility'
import { useFeatures } from '../features'
import {
  RUNBOOKS_SECTION,
  SAVED_RUNS_SECTION,
  anySectionExpanded,
  groupSection,
  isSectionCollapsed,
  loadFormsSections,
  saveFormsSections,
  setAllSections,
  toggleSection,
  type FormsSectionsState,
} from './formsSections'
// Run-form helpers + light input components live in ./formsShared so the eager
// consumers (CommandInput, SettingsDrawer, RunnableAction, …) can import them
// without pinning this heavy view into the main chunk — App.tsx React.lazy-loads
// FormsView, so nothing else may statically import from it.
import {
  ArgGrid,
  ArgInput,
  DefaultArgsCollapse,
  TruncatedText,
  argNeedsValue,
  argSpansRow,
  autonomyTagProps,
  bindingsForArgs,
  credEnvNames,
  filterSavedRuns,
  initialFormValues,
  isVarRef,
  offerDebugSwitch,
  withDebugFlag,
  partitionArgs,
  type ArgBinding,
  type ArgInputProps,
  type SlimCred,
} from './formsShared'
// Re-export the shared helpers/components so the colocated tests (which import
// them from here) keep working. Production consumers import ./formsShared directly.
export {
  ArgInput,
  TruncatedText,
  argNeedsValue,
  argSpansRow,
  autonomyTagProps,
  bindingsForArgs,
  credEnvNames,
  credEnvPrefix,
  dedupToolName,
  filterSavedRuns,
  initialFormValues,
  isMultiChoice,
  legacyDedupToolName,
  matchRunnableForEdit,
  partitionArgs,
  sanitizeToolName,
  suppressPlainEnter,
} from './formsShared'
export type { ArgBinding } from './formsShared'

const { Text } = Typography

// The autonomy pill shown in the run dialog title and on each subcommand card.
function AutonomyTag({ level }: { level?: string }) {
  const { color, label } = autonomyTagProps(level)
  return (
    <Tooltip title={`autonomy: ${level || 'confirm'}`}>
      <Tag color={color} style={{ fontSize: 11, margin: 0 }}>{label}</Tag>
    </Tooltip>
  )
}

// A path segment the tree doesn't have stops the walk at the last node that
// exists (App validates edit-and-rerun paths first; this keeps a stale one from
// crashing the dialog).
function resolveNode(root: Runnable, path: string[]): Runnable {
  let node = root
  for (const seg of path) {
    const next = node.commands?.[seg]
    if (!next) break
    node = next
  }
  return node
}

function SectionLabel({ children }: { children: ReactNode }) {
  const isDark = useIsDark()
  return (
    <div style={{
      fontSize: 11, color: textSecondary(isDark),
      textTransform: 'uppercase', letterSpacing: '0.08em',
      paddingBottom: 5, borderBottom: `1px solid var(--rs-border)`,
    }}>
      {children}
    </div>
  )
}

function ArgRow({ arg, host, runnable, runAs, group, value, error, onChange, binding }: ArgInputProps) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
        <Text code style={{ fontSize: 13 }}>--{arg.name}</Text>
        <Tag color="default" style={{ fontSize: 11, margin: 0 }}>
          {arg.type === 'path' && arg.ui === 'directory' ? 'folder'
            : (arg.ui === 'date' || arg.ui === 'datetime' || arg.ui === 'time' || arg.ui === 'month' || arg.ui === 'color') ? arg.ui
              : arg.type}
        </Tag>
        {arg.inherited && (
          <Tooltip title="Inherited from a global arg or a parent command — applies to this run">
            <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>inherited</Tag>
          </Tooltip>
        )}
        {binding && (
          <Tooltip title={binding.role === 'secret'
            ? `This arg's secret is supplied by credential “${binding.label}” via the environment channel — leave blank to use it, or type to override.`
            : `Pre-filled from credential “${binding.label}” — edit if needed.`}>
            <Tag color="green" style={{ fontSize: 11, margin: 0 }}>from {binding.label}</Tag>
          </Tooltip>
        )}
        {arg.type === 'password'
          ? <Tooltip title="A secret — typed here and sent via the environment, never on the command line. Credentials are not copied into args on the form; use the credential section below for env-var credentials."><Tag color={arg.required ? 'orange' : 'default'} style={{ fontSize: 11, margin: 0 }}>{arg.required ? 'required secret' : 'secret'}</Tag></Tooltip>
          : arg.required
            ? <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
            : isVarRef(arg.default)
              ? <Text style={{ fontSize: 11, color: '#fa8c16', fontFamily: MONO_FONT }}>{arg.default as string}</Text>
              : <Text type="secondary" style={{ fontSize: 11 }}>optional</Text>
        }
      </div>
      {/* Description on its own full-width line, truncated with a hover tooltip —
          so a long description no longer fights the name/tags for the row and
          the field lays out cleanly in the multi-column grid. */}
      {arg.description && (
        <TruncatedText text={arg.description} style={{ fontSize: 12, marginBottom: 5, color: 'var(--rs-text-secondary)' }} />
      )}
      <ArgInput arg={arg} host={host} runnable={runnable} runAs={runAs} group={group} value={value} error={error} onChange={onChange} binding={binding} />
      {error && <Text style={{ fontSize: 12, color: '#ff4d4f' }}>{error}</Text>}
    </div>
  )
}

function ArgForm({ node, host, runnable, runAs, group, values, errors, onChange, boundArgs }: {
  node: Runnable
  host: string
  runnable: string
  runAs?: string
  group?: string
  values: Record<string, unknown>
  errors: Record<string, string>
  onChange: (name: string, v: unknown) => void
  boundArgs?: Record<string, ArgBinding>
}) {
  const args = node.args ?? []

  if (args.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 13 }}>
        No arguments — ready to run.
      </Text>
    )
  }

  // Globals/parent-command args (merged in by discovery) render in their own
  // section above the command's own args, so ambient values are clearly scoped.
  const inherited = args.filter(a => a.inherited)
  const own = args.filter(a => !a.inherited)

  // A grid cell for one arg — spans the full row for wide controls (path pickers).
  // `minWidth: 0` lets the cell shrink below its content's intrinsic width;
  // without it a grid item defaults to `min-width: auto` and a nowrap
  // description blows the column out (never truncating, so no ellipsis/tooltip).
  const row = (arg: ArgDef) => (
    <div key={arg.name} style={{ minWidth: 0, ...(argSpansRow(arg) ? { gridColumn: '1 / -1' } : {}) }}>
      <ArgRow
        arg={arg}
        host={host}
        runnable={runnable}
        runAs={runAs}
        group={group}
        value={values[arg.name]}
        error={errors[arg.name]}
        onChange={v => onChange(arg.name, v)}
        binding={boundArgs?.[arg.name]}
      />
    </div>
  )

  // Within a group, args that carry a default collapse into a "Default arguments"
  // section near the top; required and optional-no-default args stay visible below
  // it. Each set lays out in the responsive multi-column grid.
  const renderGroup = (groupArgs: ArgDef[]) => {
    const { visible, defaulted } = partitionArgs(groupArgs)
    return (
      <>
        {defaulted.length > 0 && (
          <DefaultArgsCollapse count={defaulted.length}>
            <ArgGrid>{defaulted.map(row)}</ArgGrid>
          </DefaultArgsCollapse>
        )}
        {visible.length > 0 && <ArgGrid>{visible.map(row)}</ArgGrid>}
      </>
    )
  }

  // Flat runnable (no inheritance) — one group, no section chrome.
  if (inherited.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {renderGroup(own)}
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <SectionLabel>Inherited</SectionLabel>
        {renderGroup(inherited)}
      </div>
      {own.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <SectionLabel>This command</SectionLabel>
          {renderGroup(own)}
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
  const border = 'var(--rs-border)'
  const hoverBg = surfaces(isDark).overlay

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 10 }}>
      {commands.map(([name, cmd]) => (
        <div
          key={name}
          onClick={() => onSelect(name)}
          style={{
            minWidth: 0,
            border: `1px solid ${border}`, borderRadius: RADIUS.md, padding: '14px 16px',
            cursor: 'pointer', transition: 'background 0.15s',
          }}
          onMouseEnter={e => (e.currentTarget.style.background = hoverBg)}
          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, marginBottom: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
              <Text style={{ fontFamily: MONO_FONT, fontSize: 14 }}>{name}</Text>
              {/* Each subcommand shows its own effective autonomy. */}
              <AutonomyTag level={cmd.autonomy} />
            </div>
            <RightOutlined style={{ fontSize: 11, color: 'var(--rs-text-faint)', flexShrink: 0 }} />
          </div>
          {cmd.description && (
            <TruncatedText text={cmd.description} style={{ fontSize: 13, color: 'var(--rs-text-secondary)' }} />
          )}
          {(cmd.args ?? []).filter(a => !a.inherited).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag style={{ fontSize: 11 }}>{(cmd.args ?? []).filter(a => !a.inherited).length} args</Tag>
            </div>
          )}
          {Object.keys(cmd.commands ?? {}).length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag style={{ fontSize: 11 }}>{Object.keys(cmd.commands!).length} subcommands</Tag>
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
  // Previous run's arg values to pre-fill (edit-and-rerun from a Console card or
  // History row). Overlaid on the spec defaults; only keys matching a declared
  // arg are applied. Absent for a normal form open.
  prefill?: Record<string, unknown>
}

interface RunModalProps {
  state: ModalState | null
  onClose: () => void
  onSubmit: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[], credentialIds?: string[]) => void
}

// Per-runnable (across hosts) memory of the operator's credential picks. Stores
// only credential *ids* — never secrets — like a browser prefilling a username.
const credPrefsKey = (runnable: string) => `cred-prefs:${runnable}`
function loadCredPrefs(runnable: string): string[] {
  try {
    const raw = localStorage.getItem(credPrefsKey(runnable))
    const ids = raw ? JSON.parse(raw) : []
    return Array.isArray(ids) ? ids.map(String) : []
  } catch {
    return []
  }
}
function saveCredPrefs(runnable: string, ids: string[]): void {
  try {
    if (ids.length) localStorage.setItem(credPrefsKey(runnable), JSON.stringify(ids))
    else localStorage.removeItem(credPrefsKey(runnable))
  } catch {
    // best-effort — prefill is a convenience, not a requirement
  }
}

function RunModal({ state, onClose, onSubmit }: RunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [credentialIds, setCredentialIds] = useState<string[]>([])
  const [creds, setCreds] = useState<SlimCred[]>([])
  const [debug, setDebug] = useState(false)

  // Reset form when the modal or node changes
  useEffect(() => {
    if (!state) return
    setValues(initialFormValues(state.currentNode.args ?? [], state.prefill))
    setErrors({})
    setCredentialIds([])
    setDebug(false)
    // All saved credentials are selectable — the runnable may read any env var
    // names, so we don't pre-filter by scope. Restore the operator's previous
    // pick for this runnable, dropping any ids that no longer exist.
    const runnableName = state.rootRunnable.name
    bridge.list_credentials()
      .then(list => {
        const slim: SlimCred[] = list.map(c => ({
          id: c.id, label: c.label, kind: c.kind, has_secret: !!c.has_secret,
          username: c.username ?? '', username_arg: c.username_arg ?? '', secret_arg: c.secret_arg ?? '',
        }))
        setCreds(slim)
        const existing = new Set(slim.map(c => c.id))
        setCredentialIds(loadCredPrefs(runnableName).filter(id => existing.has(id)))
      })
      .catch(() => setCreds([]))
  }, [state?.rootRunnable.name, state?.rootRunnable.host, state?.commandPath.join('/'), state?.prefill])

  // Which of the selected credentials bind a declared arg (username/secret), so
  // the run dialog can pre-fill the username, mark the secret as supplied, and
  // relax the required check — making a credential work on the Forms path for a
  // runnable whose interface is args (not bare env vars), just like for the agent.
  const declaredArgNames = useMemo(
    () => new Set((state?.currentNode.args ?? []).map(a => a.name)),
    [state?.currentNode],
  )
  const selectedCreds = useMemo(
    () => credentialIds
      .map(id => creds.find(c => c.id === id))
      .filter((c): c is SlimCred => !!c),
    [credentialIds, creds],
  )
  const boundArgs = useMemo(
    () => bindingsForArgs(selectedCreds, declaredArgNames),
    [selectedCreds, declaredArgNames],
  )

  // Pre-fill username-bound args (visible + editable). Only fill blanks so an
  // operator edit is never clobbered; secrets are never pre-filled into the UI
  // (the value never leaves the keyring — it's injected server-side).
  useEffect(() => {
    setValues(prev => {
      let changed = false
      const next = { ...prev }
      for (const [argName, b] of Object.entries(boundArgs)) {
        if (b.role !== 'username') continue
        const c = selectedCreds.find(x => x.username_arg === argName)
        if (c?.username && (next[argName] === undefined || next[argName] === '')) {
          next[argName] = c.username
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [boundArgs, selectedCreds])

  if (!state) return null

  const { rootRunnable, commandPath, currentNode } = state
  const hasSubcommands = Object.keys(currentNode.commands ?? {}).length > 0
  // A node can be run at its own level unless it *requires* a subcommand. So a
  // leaf always can; a runnable that has subcommands but doesn't require one can
  // ALSO run at the top level (with its own/global args) — offer both the picker
  // and a Run form. Only a require-command node is picker-only.
  const canRunHere = !currentNode.requireCommand

  const crumbs = [rootRunnable.name, ...commandPath]

  // The dialog widens for runnables that need the room — those with subcommands
  // (which pair a picker with a form) or a lot of args — so the multi-column grid
  // can lay out 3+ columns; a small flat form stays compact.
  const argCount = (currentNode.args ?? []).length
  const wideForm = hasSubcommands || argCount > 6
  const modalWidth = wideForm ? 'min(1160px, 96vw)' : 'min(860px, 94vw)'

  const handleChange = (name: string, v: unknown) => {
    setValues(prev => ({ ...prev, [name]: v }))
    setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const handleSubmit = () => {
    const errs: Record<string, string> = {}
    for (const arg of currentNode.args ?? []) {
      const v = values[arg.name]
      if (argNeedsValue(arg, v) && !boundArgs[arg.name]) {
        // A selected credential will supply this arg (username pre-fill or secret
        // via the env channel) — don't block submit on it being empty.
        errs[arg.name] = 'Required'
      } else if (arg.range && (arg.type === 'int' || arg.type === 'float') && v !== undefined && v !== '') {
        const n = Number(v)
        const [lo, hi] = arg.range
        if (n < lo || n > hi) errs[arg.name] = `Must be between ${lo} and ${hi}`
      }
    }
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    saveCredPrefs(rootRunnable.name, credentialIds)
    onSubmit(rootRunnable, withDebugFlag(values, debug, declaredArgNames), commandPath, credentialIds.length ? credentialIds : undefined)
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
      width={modalWidth}
      title={
        // paddingRight clears antd's absolutely-positioned close (X) button so
        // the right-aligned autonomy pill / run-as text don't slide underneath it.
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', paddingRight: 28 }}>
          {crumbs.map((seg, i) => (
            <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              {i > 0 && <RightOutlined style={{ fontSize: 11, color: 'var(--rs-text-faint)' }} />}
              <Text
                style={{
                  fontFamily: MONO_FONT, fontSize: 14,
                  color: i === crumbs.length - 1 ? undefined : 'var(--rs-text-muted)',
                  cursor: i < crumbs.length - 1 ? 'pointer' : 'default',
                }}
                onClick={() => i < crumbs.length - 1 ? navigateTo(i) : undefined}
              >
                {seg}
              </Text>
            </span>
          ))}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
            {/* The autonomy of the node currently in view — the root, or the
                subcommand navigated into — not just the top-level runnable
                (whose level a subcommand leaf overrides). */}
            {canRunHere && <AutonomyTag level={currentNode.autonomy} />}
            {rootRunnable.runAs && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                as <Text code style={{ fontSize: 12 }}>{rootRunnable.runAs}</Text>
              </Text>
            )}
          </div>
        </div>
      }
      footer={
        canRunHere ? (
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button onClick={onClose}>Cancel</Button>
            <Button type="primary" onClick={handleSubmit}>
              {hasSubcommands ? `Run /${crumbs.join(' ')}` : 'Run'}
            </Button>
          </div>
        ) : null
      }
    >
      <div>
        {currentNode.description && (
          <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 14 }}>
            {currentNode.description}
          </Text>
        )}
        {hasSubcommands && (
          <div style={{ marginBottom: canRunHere ? 18 : 0 }}>
            {currentNode.requireCommand ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
                <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>command required</Tag>
                <Text type="secondary" style={{ fontSize: 13 }}>
                  This runnable can't run on its own — pick a command to continue.
                </Text>
              </div>
            ) : (
              <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 14 }}>
                Choose a subcommand{canRunHere ? `, or run /${crumbs.join(' ')} at its top level below` : ''}:
              </Text>
            )}
            <SubcommandPicker node={currentNode} onSelect={name => {
              const newPath = [...commandPath, name]
              window.dispatchEvent(new CustomEvent('forms:navigate', {
                detail: { rootRunnable, commandPath: newPath, currentNode: resolveNode(rootRunnable, newPath) }
              }))
            }} />
          </div>
        )}
        {canRunHere && (
        <div>
          {hasSubcommands && (
            <div style={{ borderTop: '1px solid rgba(128,128,128,0.2)', paddingTop: 14, marginBottom: 4 }}>
              <Text type="secondary" style={{ fontSize: 12, fontStyle: 'italic' }}>
                Or run <Text code style={{ fontSize: 12 }}>{crumbs.join(' ')}</Text> directly:
              </Text>
            </div>
          )}
          <ArgForm node={currentNode} host={rootRunnable.host} runnable={rootRunnable.name} runAs={rootRunnable.runAs} group={rootRunnable.group} values={values} errors={errors} onChange={handleChange} boundArgs={boundArgs} />
          {offerDebugSwitch(declaredArgNames) && (
            <div style={{ marginTop: 12 }}>
              <Tooltip title="Runs with --debug: DEBUG log lines and full tracebacks in the output. A runnable with logging turned off ignores it.">
                <Checkbox checked={debug} onChange={e => setDebug(e.target.checked)}>
                  <Text style={{ fontSize: 13 }}>Debug logging</Text>
                </Checkbox>
              </Tooltip>
            </div>
          )}
          {creds.length > 0 && (
            <div style={{ marginTop: 14, borderTop: '1px solid rgba(128,128,128,0.2)', paddingTop: 12 }}>
              <Text style={{ fontSize: 13, display: 'block', marginBottom: 2 }}>Credentials</Text>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                A selected credential is applied two ways — whichever the runnable reads:
                as env vars named after it (a credential <Text code style={{ fontSize: 11 }}>Jira</Text> sets <Text code style={{ fontSize: 11 }}>JIRA_USERNAME</Text>/<Text code style={{ fontSize: 11 }}>JIRA_USER</Text>/<Text code style={{ fontSize: 11 }}>JIRA_PASSWORD</Text>),
                and — if it declares an argument binding matching a field above — into that argument (the username is pre-filled; the secret rides the environment, never argv).
              </Text>
              <Select
                size="small"
                mode="multiple"
                value={credentialIds}
                onChange={vs => setCredentialIds(vs as string[])}
                placeholder="Select credentials to pass…"
                style={{ width: '100%' }}
                options={creds.map(c => ({ label: c.label, value: c.id }))}
              />
              {credentialIds.length > 0 && (
                <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 3 }}>
                  {credentialIds.map(id => {
                    const c = creds.find(x => x.id === id)
                    if (!c) return null
                    const names = credEnvNames(c.label, c.kind)
                    // Bound args this credential fills on *this* runnable (only
                    // those it actually declares — so the hint matches the form).
                    const bound: string[] = []
                    if (c.username_arg && declaredArgNames.has(c.username_arg)) bound.push(`--${c.username_arg}`)
                    if (c.secret_arg && declaredArgNames.has(c.secret_arg)) bound.push(`--${c.secret_arg}`)
                    return (
                      <Text key={id} type="secondary" style={{ fontSize: 12 }}>
                        <Text style={{ fontSize: 12 }}>{c.label}</Text>
                        {' → '}
                        {names.length
                          ? names.map((n, i) => <span key={n}>{i > 0 && ', '}<Text code style={{ fontSize: 11 }}>{n}</Text></span>)
                          : <Text type="warning" style={{ fontSize: 12 }}>no env vars (rename or pick a userpass/password/token credential)</Text>}
                        {bound.length > 0 && (
                          <span> · binds {bound.map((n, i) => <span key={n}>{i > 0 && ', '}<Text code style={{ fontSize: 11 }}>{n}</Text></span>)}</span>
                        )}
                        {c.kind !== 'ssh-key' && !c.has_secret && <Text type="warning" style={{ fontSize: 12 }}> · no secret stored</Text>}
                      </Text>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </div>
        )}
      </div>
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
  const accent = useAccent()
  const [hovered, setHovered] = useState(false)

  const hostObj = hosts.find(h => h.name === runnable.host)
  const connected = hostObj?.connected ?? true
  const argCount = (runnable.args ?? []).length
  const requiredCount = (runnable.args ?? []).filter(a => a.required).length
  const subCount = Object.keys(runnable.commands ?? {}).length
  const isAuto = runnable.autonomy === 'autonomous'
  const isInstant = argCount === 0 && subCount === 0

  const sf = surfaces(isDark)
  const border = hovered ? withAlpha(accent, 0.6) : sf.border
  const bg = hovered
    ? withAlpha(accent, isDark ? 0.05 : 0.03)
    : sf.raised

  return (
    <div
      onClick={onClick}
      className="rs-card-lift"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: `1px solid ${border}`, borderRadius: RADIUS.lg, padding: '14px 16px',
        cursor: 'pointer', background: bg,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontSize: 9, color: connected ? '#52c41a' : '#595959', flexShrink: 0 }}>●</span>
        <Text style={{ fontFamily: MONO_FONT, fontSize: 14, fontWeight: 600 }}>{runnable.name}</Text>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          {isInstant && (
            <Tooltip title="No arguments — runs immediately on click">
              <Tag color="gold" style={{ fontSize: 11, margin: 0 }}>⚡ instant</Tag>
            </Tooltip>
          )}
          {isAuto && (
            <Tooltip title="autonomy: autonomous — the assistant can invoke this without asking">
              <Tag color="green" style={{ fontSize: 11, margin: 0 }}>auto</Tag>
            </Tooltip>
          )}
        </div>
      </div>
      {runnable.description && (
        <Text type="secondary" style={{ fontSize: 13, lineHeight: 1.4 }}>{runnable.description}</Text>
      )}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2, alignItems: 'center' }}>
        <Tag style={{ fontSize: 11, margin: 0 }}>{runnable.host}</Tag>
        {argCount > 0 && (
          <Tag color={requiredCount > 0 ? 'orange' : 'default'} style={{ fontSize: 11, margin: 0 }}>
            {argCount} arg{argCount !== 1 ? 's' : ''}{requiredCount > 0 ? ` · ${requiredCount} req` : ''}
          </Tag>
        )}
        {subCount > 0 && (
          runnable.requireCommand ? (
            <Tooltip title="A command is required — this runnable can't run without choosing one of its subcommands">
              <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>{subCount} subcommands · required</Tag>
            </Tooltip>
          ) : (
            <Tag style={{ fontSize: 11, margin: 0 }}>{subCount} subcommands</Tag>
          )
        )}
        {runnable.runAs && (
          <Text type="secondary" style={{ fontSize: 11, marginLeft: 'auto' }}>
            as <Text code style={{ fontSize: 11 }}>{runnable.runAs}</Text>
          </Text>
        )}
      </div>
    </div>
  )
}

// Card edit icon — opens the item's editor without launching the card's run dialog.
function CardEditButton({ title, onEdit }: { title: string; onEdit: () => void }) {
  return (
    <Tooltip title={title}>
      <Button
        type="text" size="small" icon={<EditOutlined />} aria-label={title}
        onClick={e => { e.stopPropagation(); onEdit() }}
        style={{ color: 'var(--rs-text-muted)' }}
      />
    </Tooltip>
  )
}

// A saved runbook, rendered as a launchable card alongside runnables. Clicking opens
// the same parameter form the `$` chat menu uses (App owns the RunbookRunModal).
function RunbookCard({ runbook, onClick, onEdit }: { runbook: Runbook; onClick: () => void; onEdit?: () => void }) {
  const isDark = useIsDark()
  const [hovered, setHovered] = useState(false)

  const argCount = (runbook.args ?? []).length
  const requiredCount = (runbook.args ?? []).filter(a => a.required).length

  const sf = surfaces(isDark)
  const accent = isDark ? '#bd93f9' : '#722ed1'
  const border = hovered ? withAlpha(accent, 0.6) : sf.border
  const bg = hovered ? withAlpha(accent, isDark ? 0.05 : 0.03) : sf.raised

  return (
    <div
      onClick={onClick}
      className="rs-card-lift"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: `1px solid ${border}`, borderRadius: RADIUS.lg, padding: '14px 16px',
        cursor: 'pointer', background: bg,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Text style={{ fontFamily: MONO_FONT, fontSize: 14, fontWeight: 600, color: accent }}>
          ${runbook.id}
        </Text>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
          {onEdit && <CardEditButton title="Edit runbook" onEdit={onEdit} />}
          <Tag color="purple" style={{ fontSize: 11, margin: 0 }}>runbook</Tag>
        </div>
      </div>
      {(runbook.title || runbook.symptom) && (
        <Text type="secondary" style={{ fontSize: 13, lineHeight: 1.4 }}>
          {runbook.title}{runbook.title && runbook.symptom ? ' — ' : ''}{runbook.symptom}
        </Text>
      )}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2, alignItems: 'center' }}>
        {argCount > 0 ? (
          <Tag color={requiredCount > 0 ? 'orange' : 'default'} style={{ fontSize: 11, margin: 0 }}>
            {argCount} param{argCount !== 1 ? 's' : ''}{requiredCount > 0 ? ` · ${requiredCount} req` : ''}
          </Tag>
        ) : (
          <Tag style={{ fontSize: 11, margin: 0 }}>no params</Tag>
        )}
        {(runbook.tags ?? []).slice(0, 3).map(t => (
          <Tag key={t} style={{ fontSize: 11, margin: 0 }}>{t}</Tag>
        ))}
      </div>
    </div>
  )
}

// A saved run — a deterministic preset — rendered as a launchable card. Clicking
// opens the params-only run dialog (App owns the SavedRunRunModal); a preset with
// no open params runs immediately (the App-level handler decides).
function SavedRunCard({ savedRun, onClick, onEdit }: { savedRun: SavedRun; onClick: () => void; onEdit?: () => void }) {
  const isDark = useIsDark()
  const [hovered, setHovered] = useState(false)

  const paramCount = (savedRun.params ?? []).length
  const requiredCount = (savedRun.params ?? []).filter(a => a.required).length

  const sf = surfaces(isDark)
  const accent = isDark ? '#8be9fd' : '#0958d9'
  const border = hovered ? withAlpha(accent, 0.6) : sf.border
  const bg = hovered ? withAlpha(accent, isDark ? 0.05 : 0.03) : sf.raised

  return (
    <div
      onClick={onClick}
      className="rs-card-lift"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: `1px solid ${border}`, borderRadius: RADIUS.lg, padding: '14px 16px',
        cursor: 'pointer', background: bg,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Text style={{ fontFamily: MONO_FONT, fontSize: 14, fontWeight: 600, color: accent }}>
          {savedRun.title || savedRun.id}
        </Text>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
          {onEdit && <CardEditButton title="Edit saved run" onEdit={onEdit} />}
          <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>saved run</Tag>
        </div>
      </div>
      <Text type="secondary" style={{ fontSize: 13, lineHeight: 1.4 }}>
        /{savedRun.runnable} on {savedRun.target || savedRun.host}
      </Text>
      {savedRun.description && (
        <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.4 }}>
          {savedRun.description}
        </Text>
      )}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2, alignItems: 'center' }}>
        {paramCount > 0 ? (
          <Tag color={requiredCount > 0 ? 'orange' : 'default'} style={{ fontSize: 11, margin: 0 }}>
            {paramCount} param{paramCount !== 1 ? 's' : ''}{requiredCount > 0 ? ` · ${requiredCount} req` : ''}
          </Tag>
        ) : (
          <Tag style={{ fontSize: 11, margin: 0 }}>runs immediately</Tag>
        )}
        {savedRun.argsError && (
          <Tag color="red" style={{ fontSize: 11, margin: 0 }}>misconfigured</Tag>
        )}
      </div>
    </div>
  )
}

// Text-search filter for the saved-runs lane. Saved runs are target-baked, so
// (like runbooks) they ignore the host/scope filters — only text search narrows
// them. Exported for unit tests.
// ─── Main view ───────────────────────────────────────────────────────────────

export interface PendingForm {
  runnable: Runnable
  commandPath: string[]
  // Prefill values for edit-and-rerun — opens the form populated with a previous
  // run's choices instead of bare spec defaults.
  prefill?: Record<string, unknown>
}

interface FormsViewProps {
  runnables: Runnable[]
  hosts: Host[]
  selectedHost: string
  activeScope: string[]
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[], credentialIds?: string[]) => void
  pendingForm?: PendingForm | null
  onPendingFormClear?: () => void
  runbooks?: Runbook[]
  onOpenRunbook?: (runbook: Runbook) => void
  savedRuns?: SavedRun[]
  onOpenSavedRun?: (savedRun: SavedRun) => void
  // Create / edit — the Forms tab is the one home for runbook + saved-run
  // management (App hosts the editor modals). `id` '' = new.
  onEditRunbook?: (id: string) => void
  onEditSavedRun?: (id: string) => void
}

// One collapsible Forms section (Saved runs, Runbooks, or a runnable group):
// a clickable header with a caret, and the item count shown while collapsed —
// the count follows the search, so a collapsed lane still says how many match.
function FormsSection({ title, count, collapsed, onToggle, children }: {
  title: string
  count: number
  collapsed: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const isDark = useIsDark()
  return (
    <div style={{ marginBottom: collapsed ? 12 : 28 }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        style={{
          display: 'flex', alignItems: 'center', gap: 8, width: '100%',
          background: 'none', border: 'none', cursor: 'pointer', padding: '0 0 6px',
          marginBottom: collapsed ? 0 : 12,
          borderBottom: '1px solid var(--rs-border)',
          fontSize: 11, color: textSecondary(isDark),
          textTransform: 'uppercase', letterSpacing: '0.08em', textAlign: 'left',
        }}
      >
        {collapsed
          ? <RightOutlined style={{ fontSize: 9 }} />
          : <DownOutlined style={{ fontSize: 9 }} />}
        <span>{title}</span>
        {collapsed && (
          <Tag style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: '16px', textTransform: 'none', letterSpacing: 0 }}>
            {count}
          </Tag>
        )}
      </button>
      {!collapsed && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
          gap: 10,
        }}>
          {children}
        </div>
      )}
    </div>
  )
}

export function FormsView({
  runnables, hosts, selectedHost, activeScope,
  onRunRunnable, pendingForm, onPendingFormClear,
  runbooks = [], onOpenRunbook,
  savedRuns = [], onOpenSavedRun,
  onEditRunbook, onEditSavedRun,
}: FormsViewProps) {
  const { ai } = useFeatures()
  const [search, setSearch] = useState('')
  const [sections, setSections] = useState<FormsSectionsState>(() => loadFormsSections())
  const updateSections = (next: FormsSectionsState) => {
    setSections(next)
    saveFormsSections(next)
  }
  const [modalState, setModalState] = useState<ModalState | null>(null)
  // [tool_visibility] hidden_from_all — runnables hidden everywhere in the
  // console, including this Forms list. The agent toolset + catalogs are filtered
  // server-side; this is the display filter for the Forms surface. Refreshed when
  // the Settings → Agent tools tab saves.
  const [hiddenAll, setHiddenAll] = useState<string[]>([])
  useEffect(() => {
    const load = () => bridge.get_tool_visibility().then(v => setHiddenAll(v.hidden_from_all ?? [])).catch(() => {})
    load()
    window.addEventListener('runspec:tool_visibility_changed', load)
    return () => window.removeEventListener('runspec:tool_visibility_changed', load)
  }, [])

  // Open modal when slash command routes a pending form here
  useEffect(() => {
    if (!pendingForm) return
    setModalState({
      rootRunnable: pendingForm.runnable,
      commandPath: pendingForm.commandPath,
      currentNode: resolveNode(pendingForm.runnable, pendingForm.commandPath),
      prefill: pendingForm.prefill,
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

  // Drop runnables hidden everywhere ([tool_visibility] hidden_from_all) before
  // any other filtering, so they never appear as Forms cards.
  const visibleRunnables = hiddenAll.length > 0
    ? runnables.filter(r => !runnableHidden(r.name, r.group, r.host, hiddenAll))
    : runnables

  // Filter by host selection and group scope
  const hostFiltered = selectedHost
    ? visibleRunnables.filter(r => r.host === selectedHost)
    : visibleRunnables

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

  // Runbooks are host-agnostic, so they ignore the host/scope filters — only the
  // text search narrows them. Shown as their own swimlane above the runnables.
  const filteredRunbooks = (onOpenRunbook && runbooks.length > 0)
    ? (search
        ? runbooks.filter(s => {
            const low = search.toLowerCase()
            return s.id.toLowerCase().includes(low) ||
              (s.title ?? '').toLowerCase().includes(low) ||
              (s.symptom ?? '').toLowerCase().includes(low) ||
              (s.tags ?? []).some(t => t.toLowerCase().includes(low))
          })
        : runbooks)
    : []

  const filteredSavedRuns = (onOpenSavedRun && savedRuns.length > 0)
    ? filterSavedRuns(savedRuns, search)
    : []

  // Group into swimlanes preserving existing group order
  const groupMap = new Map<string, Runnable[]>()
  for (const r of filtered) {
    if (!groupMap.has(r.group)) groupMap.set(r.group, [])
    groupMap.get(r.group)!.push(r)
  }

  // Sections currently on screen, for the bulk collapse/expand toggle.
  const sectionKeys = [
    ...(filteredSavedRuns.length > 0 ? [SAVED_RUNS_SECTION] : []),
    ...(filteredRunbooks.length > 0 ? [RUNBOOKS_SECTION] : []),
    ...[...groupMap.keys()].map(groupSection),
  ]
  const anyOpen = anySectionExpanded(sections, sectionKeys)
  const sectionProps = (key: string) => ({
    collapsed: isSectionCollapsed(sections, key),
    onToggle: () => updateSections(toggleSection(sections, key)),
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ marginBottom: 16, flexShrink: 0, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="Search name, group, host, description…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          allowClear
          style={{ maxWidth: 360, flex: '1 1 240px' }}
        />
        {onEditSavedRun && (
          <Button icon={<PlusOutlined />} onClick={() => onEditSavedRun('')}>New saved run</Button>
        )}
        {ai && onEditRunbook && (
          <Button icon={<PlusOutlined />} onClick={() => onEditRunbook('')}>New runbook</Button>
        )}
        {sectionKeys.length > 0 && (
          <Button
            type="text"
            style={{ marginLeft: 'auto' }}
            icon={anyOpen ? <RightOutlined /> : <DownOutlined />}
            onClick={() => updateSections(setAllSections(anyOpen ? 'collapsed' : 'expanded'))}
          >
            {anyOpen ? 'Collapse all' : 'Expand all'}
          </Button>
        )}
      </div>

      {filtered.length === 0 && filteredRunbooks.length === 0 && filteredSavedRuns.length === 0 ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--rs-text-faint)' }}>
          <div style={{ textAlign: 'center' }}>
            <AppstoreOutlined style={{ fontSize: 32, marginBottom: 12, display: 'block' }} />
            <Text type="secondary">Nothing matches the current filter.</Text>
          </div>
        </div>
      ) : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {filteredSavedRuns.length > 0 && (
            <FormsSection title="Saved runs" count={filteredSavedRuns.length} {...sectionProps(SAVED_RUNS_SECTION)}>
              {filteredSavedRuns.map(s => (
                <SavedRunCard key={s.id} savedRun={s} onClick={() => onOpenSavedRun?.(s)} onEdit={onEditSavedRun && (() => onEditSavedRun(s.id))} />
              ))}
            </FormsSection>
          )}
          {filteredRunbooks.length > 0 && (
            <FormsSection title="Runbooks" count={filteredRunbooks.length} {...sectionProps(RUNBOOKS_SECTION)}>
              {filteredRunbooks.map(s => (
                <RunbookCard key={s.id} runbook={s} onClick={() => onOpenRunbook?.(s)} onEdit={ai && onEditRunbook ? () => onEditRunbook(s.id) : undefined} />
              ))}
            </FormsSection>
          )}
          {[...groupMap.entries()].map(([group, groupRunnables]) => (
            <FormsSection key={group} title={group} count={groupRunnables.length} {...sectionProps(groupSection(group))}>
              {groupRunnables.map(r => (
                <RunnableCard
                  key={`${r.host}/${r.group}/${r.name}`}
                  runnable={r}
                  hosts={hosts}
                  onClick={() => handleCardClick(r)}
                />
              ))}
            </FormsSection>
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
