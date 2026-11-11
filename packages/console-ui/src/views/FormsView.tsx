import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Button, Checkbox, Collapse, Input, Modal, Segmented, Select, Tag, Tooltip, Typography, message } from 'antd'
import { SearchOutlined, RightOutlined, ArrowLeftOutlined, AppstoreOutlined, EditOutlined, PlusOutlined, DownOutlined, InfoCircleOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ArgDef, Host, Runnable, Runbook, SavedRun } from '../bridge'
import { bridge } from '../bridge'
import { useIsDark, useAccent, withAlpha, surfaces, RADIUS, textSecondary, MONO_FONT } from '../ThemeContext'
import { runnableHidden } from '../toolVisibility'
import { selectForms, loadFormsScope, saveFormsScope, needsFanoutConfirm, targetsForRun, type FormsEntry, type FormsScope } from './formsSearch'
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
  ArgField,
  ArgError,
  ArgFieldHeader,
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

// A section heading inside the run dialog ("Inherited", "This command",
// "Credentials"): sentence case, semibold, with a hairline under it.
function SectionLabel({ children, hint }: { children: ReactNode; hint?: string }) {
  const isDark = useIsDark()
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 6,
      fontSize: 12, fontWeight: 600, color: textSecondary(isDark),
      paddingBottom: 6, borderBottom: '1px solid var(--rs-border)',
    }}>
      {children}
      {hint && (
        <Tooltip title={hint}>
          <InfoCircleOutlined style={{ fontSize: 12, color: 'var(--rs-text-muted)', cursor: 'help' }} />
        </Tooltip>
      )}
    </div>
  )
}

// One form field: name + type/required header, a description of up to two
// lines, the control, then any validation message.
function ArgRow({ arg, host, runnable, runAs, group, value, error, onChange, binding }: ArgInputProps) {
  const bindingTag = binding && (
    <Tooltip title={binding.role === 'secret'
      ? `This arg's secret is supplied by credential “${binding.label}” via the environment channel — leave blank to use it, or type to override.`
      : `Pre-filled from credential “${binding.label}” — edit if needed.`}>
      <Tag color="green" style={{ fontSize: 11, lineHeight: '16px', margin: 0 }}>from {binding.label}</Tag>
    </Tooltip>
  )
  return (
    <ArgField header={<ArgFieldHeader arg={arg} extra={bindingTag} />} description={arg.description}>
      <ArgInput arg={arg} host={host} runnable={runnable} runAs={runAs} group={group} value={value} error={error} onChange={onChange} binding={binding} />
      <ArgError text={error} />
    </ArgField>
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 28 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <SectionLabel hint="Global args and parent-command args. They apply to this run.">Inherited</SectionLabel>
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
  // The runnable's copy on every host its card covers (best pick first), when
  // it is on more than one: the dialog then offers a multi-select of hosts and
  // one submission runs on each picked host. Absent ⇒ runs on rootRunnable.host.
  hostRecords?: Runnable[]
}

interface RunModalProps {
  state: ModalState | null
  /** Sidebar hosts, for the connection dot beside each host in the host picker. */
  hosts?: Host[]
  onClose: () => void
  onSubmit: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[], credentialIds?: string[], targets?: Runnable[]) => void
}

// Most restrictive first — the escalation rule (most restrictive level wins).
const AUTONOMY_RANK: Record<string, number> = { manual: 3, confirm: 2, supervised: 1, autonomous: 0 }

/** The level this submission runs at: the command's own, raised by any arg that
 *  carries its own autonomy override and was given a value. */
function formAutonomy(node: Runnable, values: Record<string, unknown>): string {
  let level = node.autonomy || 'confirm'
  for (const a of node.args ?? []) {
    const v = values[a.name]
    if (!a.autonomy || v === undefined || v === '' || v === false || v === null) continue
    if ((AUTONOMY_RANK[a.autonomy] ?? 2) > (AUTONOMY_RANK[level] ?? 2)) level = a.autonomy
  }
  return level
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

function RunModal({ state, hosts = [], onClose, onSubmit }: RunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [credentialIds, setCredentialIds] = useState<string[]>([])
  const [creds, setCreds] = useState<SlimCred[]>([])
  const [debug, setDebug] = useState(false)
  // Hosts this submission runs on, when the card covers several (hostRecords).
  const [targetHosts, setTargetHosts] = useState<string[]>([])
  // A multi-host run waiting for the operator's one "yes, all of these" click.
  const [fanout, setFanout] = useState<{
    run: Runnable[]; missing: string[]; level: string
    args: Record<string, unknown>; creds: string[] | undefined
  } | null>(null)

  // A new card (not a subcommand step inside the same one) starts on its best
  // host — the one a single click used to run on.
  useEffect(() => {
    if (!state) return
    setTargetHosts([state.rootRunnable.host])
    setFanout(null)
  }, [state?.rootRunnable.name, state?.rootRunnable.host, state?.hostRecords])

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

  const { rootRunnable, commandPath, currentNode, hostRecords } = state
  const multiHost = (hostRecords?.length ?? 0) > 1
  // Where the form's host-aware inputs (path pickers) browse: the first picked host.
  const formHost = multiHost ? (hostRecords!.find(r => targetHosts.includes(r.host))?.host ?? rootRunnable.host) : rootRunnable.host
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
    if (multiHost && targetHosts.length === 0) { message.warning('Pick at least one host to run on.'); return }
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    const cmd = `/${[rootRunnable.name, ...commandPath].join(' ')}`
    const args = withDebugFlag(values, debug, declaredArgNames)
    const creds = credentialIds.length ? credentialIds : undefined
    if (!multiHost) {
      saveCredPrefs(rootRunnable.name, credentialIds)
      onSubmit(rootRunnable, args, commandPath, creds)
      onClose()
      message.success(`${cmd} submitted`)
      return
    }
    const { run, missing } = targetsForRun(hostRecords!, targetHosts, commandPath)
    if (run.length === 0) {
      message.error(`None of the picked hosts has ${cmd} — their copy of ${rootRunnable.name} is a different version.`)
      return
    }
    const level = formAutonomy(currentNode, values)
    if (!needsFanoutConfirm(run.length, level) && missing.length === 0) { startOn(run, args, creds); return }
    // Ask once, naming every host (an in-dialog modal — antd's static
    // Modal.confirm doesn't render under React 19).
    setFanout({ run, missing, level, args, creds })
  }

  const startOn = (run: Runnable[], args: Record<string, unknown>, creds: string[] | undefined) => {
    const cmd = `/${[rootRunnable.name, ...commandPath].join(' ')}`
    saveCredPrefs(rootRunnable.name, credentialIds)
    onSubmit(run[0], args, commandPath, creds, run)
    setFanout(null)
    onClose()
    message.success(run.length === 1 ? `${cmd} submitted on ${run[0].host}` : `${cmd} submitted on ${run.length} hosts`)
  }

  const navigateTo = (depth: number) => {
    const newPath = commandPath.slice(0, depth)
    const newNode = resolveNode(rootRunnable, newPath)
    // We can't mutate state prop — bubble up via onClose then re-open, so instead
    // we dispatch a custom event the parent listens to for back navigation
    window.dispatchEvent(new CustomEvent('forms:navigate', { detail: { rootRunnable, commandPath: newPath, currentNode: newNode } }))
  }

  const fanoutCmd = `/${crumbs.join(' ')}`
  const fanoutAsks = !!fanout && needsFanoutConfirm(fanout.run.length, fanout.level)
  return (
    <>
    <Modal
      open={!!fanout}
      onCancel={() => setFanout(null)}
      width={480}
      title={fanout && (fanout.run.length === 1 ? `Run ${fanoutCmd} on ${fanout.run[0].host}?` : `Run ${fanoutCmd} on ${fanout.run.length} hosts?`)}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={() => setFanout(null)}>Back</Button>
          <Button type="primary" onClick={() => fanout && startOn(fanout.run, fanout.args, fanout.creds)}>
            {fanout && fanout.run.length > 1 ? `Run on ${fanout.run.length} hosts` : 'Run'}
          </Button>
        </div>
      }
    >
      {fanout && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {fanoutAsks && (
            <Text type="secondary" style={{ fontSize: 13 }}>
              This command is <Text code style={{ fontSize: 12 }}>{fanout.level}</Text>, so it asks once before running on more than one host.
            </Text>
          )}
          <div data-rs-fanout-hosts style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {fanout.run.map(r => <Tag key={r.host} style={{ fontFamily: MONO_FONT, margin: 0 }}>{r.host}</Tag>)}
          </div>
          {fanout.missing.length > 0 && (
            <Text type="warning" style={{ fontSize: 12 }}>
              Skipped — their version has no {fanoutCmd}: {fanout.missing.join(', ')}
            </Text>
          )}
        </div>
      )}
    </Modal>
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
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8 }}>
            {/* Run options sit with the Run button, not among the args. */}
            {offerDebugSwitch(declaredArgNames) && (
              <Tooltip title="Runs with --debug: DEBUG log lines and full tracebacks in the output. A runnable with logging turned off ignores it.">
                <Checkbox checked={debug} onChange={e => setDebug(e.target.checked)} style={{ marginRight: 'auto' }}>
                  <Text style={{ fontSize: 13 }}>Debug logging</Text>
                </Checkbox>
              </Tooltip>
            )}
            <Button onClick={onClose}>Cancel</Button>
            <Button type="primary" onClick={handleSubmit} disabled={multiHost && targetHosts.length === 0}>
              {(hasSubcommands ? `Run /${crumbs.join(' ')}` : 'Run') +
                (multiHost && targetHosts.length > 1 ? ` on ${targetHosts.length} hosts` : '')}
            </Button>
          </div>
        ) : null
      }
    >
      <div>
        {currentNode.description && (
          <Text type="secondary" style={{ fontSize: 13, lineHeight: 1.55, display: 'block', marginBottom: 20, maxWidth: '75ch' }}>
            {currentNode.description}
          </Text>
        )}
        {multiHost && (
          <HostTargets
            records={hostRecords!}
            hosts={hosts}
            value={targetHosts}
            onChange={setTargetHosts}
          />
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
            <div style={{ marginBottom: 14 }}>
              <SectionLabel>
                <span>Or run <span style={{ fontFamily: MONO_FONT, fontWeight: 500 }}>{crumbs.join(' ')}</span> directly</span>
              </SectionLabel>
            </div>
          )}
          <ArgForm node={currentNode} host={formHost} runnable={rootRunnable.name} runAs={rootRunnable.runAs} group={rootRunnable.group} values={values} errors={errors} onChange={handleChange} boundArgs={boundArgs} />
          {creds.length > 0 && (
            <div style={{ marginTop: 28, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <SectionLabel hint="A selected credential is applied two ways, whichever the runnable reads: as env vars named after it (a credential “Jira” sets JIRA_USERNAME, JIRA_USER and JIRA_PASSWORD), and, if it binds an argument on this form, into that argument. The username is pre-filled; the secret goes through the environment, never the command line.">
                Credentials
              </SectionLabel>
              <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.5 }}>
                Sets env vars named after each credential, and fills any argument it binds.
              </Text>
              <Select
                mode="multiple"
                value={credentialIds}
                onChange={vs => setCredentialIds(vs as string[])}
                placeholder="Select credentials to pass…"
                style={{ width: '100%' }}
                options={creds.map(c => ({ label: c.label, value: c.id }))}
              />
              {credentialIds.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
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
    </>
  )
}

// The run dialog's host picker for a runnable found on several hosts: pick one
// or more; one submission then runs on each (one Console run card per host).
function HostTargets({ records, hosts, value, onChange }: {
  records: Runnable[]
  hosts: Host[]
  value: string[]
  onChange: (hosts: string[]) => void
}) {
  const connected = (h: string) => hosts.find(x => x.name === h)?.connected ?? true
  const all = records.map(r => r.host)
  const live = all.filter(connected)
  return (
    <div data-rs-host-targets style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
        <Text style={{ fontSize: 13 }}>Run on</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {value.length > 1 ? `${value.length} of ${all.length} hosts — runs on each` : `found on ${all.length} hosts — pick one or more`}
        </Text>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <Button type="link" size="small" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => onChange(all)}>
            All {all.length}
          </Button>
          {live.length > 0 && live.length < all.length && (
            <>
              <Text type="secondary" style={{ fontSize: 12 }}>·</Text>
              <Button type="link" size="small" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => onChange(live)}>
                Connected {live.length}
              </Button>
            </>
          )}
        </span>
      </div>
      <Select
        mode="multiple"
        aria-label="Hosts to run on"
        value={value}
        onChange={vs => onChange(all.filter(h => (vs as string[]).includes(h)))}
        placeholder="Pick hosts…"
        style={{ width: '100%' }}
        maxTagCount="responsive"
        options={records.map(r => ({
          value: r.host,
          label: (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <span style={{ fontSize: 9, color: connected(r.host) ? '#52c41a' : '#595959' }}>●</span>
              <span style={{ fontFamily: MONO_FONT }}>{r.host}</span>
            </span>
          ),
        }))}
      />
    </div>
  )
}

// ─── Runnable card ────────────────────────────────────────────────────────────

function RunnableCard({ entry, hosts, onPick }: {
  entry: FormsEntry
  hosts: Host[]
  onPick: (entry: FormsEntry) => void
}) {
  const isDark = useIsDark()
  const accent = useAccent()
  const [hovered, setHovered] = useState(false)

  const runnable = entry.records[0]
  const isConnected = (host: string) => hosts.find(h => h.name === host)?.connected ?? true
  const connected = entry.records.some(r => isConnected(r.host))
  const multi = entry.records.length > 1
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

  const card = (
    <div
      onClick={() => onPick(entry)}
      className="rs-card-lift"
      data-rs-forms-card={entry.key}
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
              <Tag color="gold" icon={<ThunderboltOutlined />} style={{ fontSize: 11, margin: 0 }}>instant</Tag>
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
        {multi ? (
          <Tooltip title={entry.records.map(r => r.host).join(', ')}>
            <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>{entry.records.length} hosts</Tag>
          </Tooltip>
        ) : (
          <Tag style={{ fontSize: 11, margin: 0 }}>{runnable.host}</Tag>
        )}
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
  // On several hosts the run dialog asks which host(s) to run on.
  return card
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
  /** The active host profile's name ('' = All hosts), for the scope toggle's label. */
  activeProfile?: string
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
  runnables, hosts, selectedHost, activeScope, activeProfile = '',
  onRunRunnable, pendingForm, onPendingFormClear,
  runbooks = [], onOpenRunbook,
  savedRuns = [], onOpenSavedRun,
  onEditRunbook, onEditSavedRun,
}: FormsViewProps) {
  const { ai } = useFeatures()
  const [search, setSearch] = useState('')
  const [scopePref, setScopePref] = useState<FormsScope>(() => loadFormsScope())
  const updateScope = (next: FormsScope) => {
    setScopePref(next)
    saveFormsScope(next)
  }
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
      // A step within the same card keeps its host picker (and the picked hosts).
      setModalState(prev => ({
        rootRunnable, commandPath, currentNode,
        ...(prev?.hostRecords && prev.rootRunnable === rootRunnable ? { hostRecords: prev.hostRecords } : {}),
      }))
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

  const handleCardClick = (entry: FormsEntry) => {
    const runnable = entry.records[0]
    if (entry.records.length > 1) {
      // On several hosts: the dialog asks which host(s) — even with no args.
      setModalState({ rootRunnable: runnable, commandPath: [], currentNode: runnable, hostRecords: entry.records })
      return
    }
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
  // A runnable whose runspec.toml says `discoverable = false` (an internal
  // helper) is never a Forms card either.
  const visibleRunnables = runnables.filter(r =>
    r.rawSpec?.discoverable !== false &&
    !(hiddenAll.length > 0 && runnableHidden(r.name, r.group, r.host, hiddenAll)))

  // Host scope (selected host, or every host in the profile) + text search,
  // collapsed to one card per runnable — see formsSearch.ts.
  const selection = selectForms({
    runnables: visibleRunnables, hosts, selectedHost,
    scope: scopePref, activeScope, search,
  })
  const filtered = selection.entries
  const allHostsLabel = activeProfile ? `All in ${activeProfile}` : 'All hosts'

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
  const groupMap = new Map<string, FormsEntry[]>()
  for (const e of filtered) {
    if (!groupMap.has(e.group)) groupMap.set(e.group, [])
    groupMap.get(e.group)!.push(e)
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
          placeholder="Search name, description, subcommand… (host:web group:prod)"
          value={search}
          onChange={e => setSearch(e.target.value)}
          allowClear
          style={{ maxWidth: 360, flex: '1 1 240px' }}
        />
        {selectedHost && (
          <Tooltip title={selection.scope === 'profile' && scopePref === 'host'
            ? 'A host: or group: filter searches every host in the profile'
            : 'Show runnables on the selected host, or on every host in the current profile'}>
            <Segmented
              size="middle"
              value={selection.scope}
              onChange={v => updateScope(v as FormsScope)}
              options={[
                { label: selectedHost, value: 'host' },
                { label: allHostsLabel, value: 'profile' },
              ]}
            />
          </Tooltip>
        )}
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

      {selection.elsewhere > 0 && (
        <div style={{ marginTop: -8, marginBottom: 12, fontSize: 12, flexShrink: 0 }}>
          <Text type="secondary">
            {selection.elsewhere} more {selection.elsewhere === 1 ? 'match' : 'matches'} on other hosts.{' '}
          </Text>
          <Button type="link" size="small" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => updateScope('profile')}>
            Search {activeProfile ? `all of ${activeProfile}` : 'all hosts'}
          </Button>
        </div>
      )}

      {filtered.length === 0 && filteredRunbooks.length === 0 && filteredSavedRuns.length === 0 ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--rs-text-muted)' }}>
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
          {[...groupMap.entries()].map(([group, groupEntries]) => (
            <FormsSection key={group} title={group} count={groupEntries.length} {...sectionProps(groupSection(group))}>
              {groupEntries.map(e => (
                <RunnableCard
                  key={e.key}
                  entry={e}
                  hosts={hosts}
                  onPick={handleCardClick}
                />
              ))}
            </FormsSection>
          ))}
        </div>
      )}

      <RunModal
        state={modalState}
        hosts={hosts}
        onClose={() => setModalState(null)}
        onSubmit={onRunRunnable}
      />
    </div>
  )
}
