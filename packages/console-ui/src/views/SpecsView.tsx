import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Collapse, Empty, Input, Segmented, Tag, Tooltip, Typography } from 'antd'
import { SearchOutlined, FormOutlined, EyeInvisibleOutlined, LockOutlined } from '@ant-design/icons'
import { useIsDark, textColor, textSecondary, textFaint, textMuted, useAccent, withAlpha } from '../ThemeContext'
import { runnableHidden } from '../toolVisibility'
import { CopyButton } from '../components/runShared'
import { bridge } from '../bridge'
import type { Runnable, ArgDef, ToolVisibility, DenylistHostStatus, DenylistRule } from '../bridge'

const { Text } = Typography

// Library defaults for per-runnable fields that are always in effect, so the
// inspector can show them dimmed as "default" when the TOML doesn't set them.
const SPEC_DEFAULTS: Record<string, unknown> = {
  autonomy: 'confirm',
  output: 'text',
  serve: true,
  discoverable: true,
  become_method: 'sudo',
}

const LIST_WIDTH_KEY = 'rs-specs-list-width'
const LIST_MIN = 280
const LIST_MAX = 640

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
}

// ── Pure helpers (exported for tests) ─────────────────────────────────────────

/** Shell glob (`*`/`?`) → anchored RegExp. Mirrors toolVisibility.globToRe and
 *  the core denylist matcher. */
function globToRe(glob: string): RegExp {
  let re = ''
  for (const ch of glob) {
    if (ch === '*') re += '.*'
    else if (ch === '?') re += '.'
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&')
  }
  return new RegExp(`^${re}$`)
}
function globAny(globs: string[] | undefined, target: string): boolean {
  if (!globs || globs.length === 0) return false
  return globs.some(g => { const s = (g ?? '').trim(); return s ? globToRe(s).test(target) : false })
}

export type Visibility = 'visible' | 'agent' | 'all'

/** Effective agent visibility of a runnable under [tool_visibility]. `all`
 *  (hidden everywhere) is checked first since it implies agent-hidden too. */
export function effectiveVisibility(
  name: string, group: string | undefined, host: string | undefined, vis: ToolVisibility | null,
): Visibility {
  if (!vis) return 'visible'
  if (runnableHidden(name, group, host, vis.hidden_from_all)) return 'all'
  if (runnableHidden(name, group, host, vis.hidden_from_agent)) return 'agent'
  return 'visible'
}

export interface DenylistMatch {
  checked: boolean            // did we get a status for this host?
  mode?: string               // enforce | warn | off (from the host's venv)
  disabled: boolean           // the whole venv is disabled (all runnables)
  rules: DenylistRule[]       // rules whose runnable+host globs match this runnable
}

/** Whether a runnable is denylisted on its host, given the (lazily fetched)
 *  per-host status. The reserved `runspec` control-plane name is exempt (the core
 *  denylist never self-locks). Pure — exported for tests. */
export function matchesDenylist(
  name: string, host: string, statuses: DenylistHostStatus[] | undefined | null,
): DenylistMatch {
  if (!statuses) return { checked: false, disabled: false, rules: [] }
  const forHost = statuses.filter(s => s.host === host)
  if (forHost.length === 0) return { checked: true, disabled: false, rules: [] }
  if (name === 'runspec') return { checked: true, mode: forHost[0].mode, disabled: false, rules: [] }
  let mode: string | undefined
  let disabled = false
  const rules: DenylistRule[] = []
  for (const s of forHost) {
    if (s.mode) mode = s.mode
    if (s.disabled) disabled = true
    for (const r of s.rules ?? []) {
      if (globAny(r.runnables, name) && globAny(r.hosts, host)) rules.push(r)
    }
  }
  return { checked: true, mode, disabled, rules }
}

/** Pretty-printed JSON with a circular-ref guard and a size cap — for the raw
 *  spec panel. Mirrors DevView.safeJson. Exported for tests. */
export function prettyJson(value: unknown, cap = 200_000): string {
  const seen = new WeakSet<object>()
  let out: string
  try {
    out = JSON.stringify(value, (_k, v) => {
      if (typeof v === 'bigint') return `${v}n`
      if (typeof v === 'function') return `[Function ${v.name || 'anonymous'}]`
      if (v && typeof v === 'object') {
        if (seen.has(v as object)) return '[Circular]'
        seen.add(v as object)
      }
      return v
    }, 2) ?? String(value)
  } catch {
    out = String(value)
  }
  return out.length > cap ? out.slice(0, cap) + '\n… (truncated)' : out
}

const specKey = (r: Runnable) => `${r.host}/${r.group}/${r.name}`

// ── Small presentational pieces ───────────────────────────────────────────────

function SectionLabel({ label }: { label: string }) {
  const isDark = useIsDark()
  return (
    <div style={{
      fontSize: 10, fontFamily: 'monospace', color: textFaint(isDark),
      textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 6, userSelect: 'none',
    }}>
      {label}
    </div>
  )
}

function autonomyColor(v: string | undefined): string {
  return v === 'autonomous' ? 'green' : v === 'supervised' ? 'gold' : v === 'manual' ? 'red' : 'default'
}
function AutonomyTag({ level }: { level: string | undefined }) {
  const v = level || 'confirm'
  return <Tag color={autonomyColor(v)} style={{ fontSize: 10, margin: 0 }}>{v}</Tag>
}

function FieldRow({ name, value, isDefault }: { name: string; value: React.ReactNode; isDefault?: boolean }) {
  const isDark = useIsDark()
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', minHeight: 22 }}>
      <span style={{
        fontSize: 11, fontFamily: 'monospace', color: isDefault ? textFaint(isDark) : textMuted(isDark),
        width: 150, flexShrink: 0, userSelect: 'none',
      }}>{name}</span>
      <span style={{ fontSize: 12, opacity: isDefault ? 0.5 : 1, display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
        {value}
        {isDefault && <span style={{ fontSize: 10, fontFamily: 'monospace', color: textFaint(isDark), fontStyle: 'italic' }}>default</span>}
      </span>
    </div>
  )
}

// One argument, showing every field the spec carries.
function ArgRow({ arg }: { arg: ArgDef }) {
  const isDark = useIsDark()
  const chips: React.ReactNode[] = []
  const push = (node: React.ReactNode) => chips.push(node)
  const typeLabel = arg.type === 'path' && arg.ui === 'directory' ? 'folder' : arg.type
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '4px 0' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap', fontFamily: 'monospace', fontSize: 12 }}>
        <Text code style={{ fontSize: 12 }}>--{arg.name}</Text>
        <Tag style={{ fontSize: 10, margin: 0 }}>{typeLabel}</Tag>
        {arg.ui && arg.ui !== 'directory' && <Tag style={{ fontSize: 10, margin: 0 }}>ui: {arg.ui}</Tag>}
        {arg.type === 'password'
          ? <Tag color={arg.required ? 'orange' : 'default'} style={{ fontSize: 10, margin: 0 }}>{arg.required ? 'required secret' : 'secret'}</Tag>
          : arg.required
            ? <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>required</Tag>
            : isVarRef(arg.default)
              ? <Text style={{ fontSize: 10, color: '#fa8c16', fontFamily: 'monospace' }}>{String(arg.default)}</Text>
              : arg.default !== undefined
                ? <Text type="secondary" style={{ fontSize: 10 }}>default: {String(arg.default)}</Text>
                : <Text type="secondary" style={{ fontSize: 10 }}>optional</Text>}
        {arg.short && <Tag style={{ fontSize: 10, margin: 0 }}>-{arg.short.replace(/^-+/, '')}</Tag>}
        {typeof arg.position === 'number' && <Tag style={{ fontSize: 10, margin: 0 }}>pos {arg.position}</Tag>}
        {arg.multiple && <Tag style={{ fontSize: 10, margin: 0 }}>multiple</Tag>}
        {arg.autonomy && <AutonomyTag level={arg.autonomy} />}
        {arg.deprecated && <Tag color="red" style={{ fontSize: 10, margin: 0 }}>deprecated</Tag>}
      </div>
      {(() => {
        if (arg.options) push(<span key="opt">options: [{arg.options.join(' | ')}]</span>)
        if (arg.range) push(<span key="rng">range: {arg.range[0]}–{arg.range[1]}</span>)
        if (arg.pattern) push(<span key="pat">pattern: <Text code style={{ fontSize: 10 }}>{arg.pattern}</Text></span>)
        if (arg.min_length !== undefined || arg.max_length !== undefined)
          push(<span key="len">length: {arg.min_length ?? 0}–{arg.max_length ?? '∞'}</span>)
        if (arg.delimiter) push(<span key="del">delimiter: <Text code style={{ fontSize: 10 }}>{arg.delimiter}</Text></span>)
        if (arg.env) push(<span key="env">env: {(Array.isArray(arg.env) ? arg.env : [arg.env]).join(', ')}</span>)
        return chips.length > 0 ? (
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontFamily: 'monospace', fontSize: 11, color: textMuted(isDark), paddingLeft: 2 }}>
            {chips}
          </div>
        ) : null
      })()}
      {arg.description && <Text type="secondary" style={{ fontSize: 12, paddingLeft: 2 }}>{arg.description}</Text>}
      {arg.deprecated && typeof arg.deprecated === 'string' && (
        <Text style={{ fontSize: 11, color: '#ff7875', paddingLeft: 2 }}>deprecated: {arg.deprecated}</Text>
      )}
      {arg.meta && (
        <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace', paddingLeft: 2 }}>meta: {JSON.stringify(arg.meta)}</Text>
      )}
    </div>
  )
}

// ── Denylist section (lazy) ───────────────────────────────────────────────────

function DenylistSection({ runnable }: { runnable: Runnable }) {
  const isDark = useIsDark()
  const accent = useAccent()
  const [status, setStatus] = useState<DenylistHostStatus[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // Reset when the selected runnable's host changes.
  useEffect(() => { setStatus(null); setError('') }, [runnable.host])

  const check = () => {
    setLoading(true); setError('')
    bridge.denylist_status([runnable.host])
      .then(s => setStatus(s))
      .catch(e => setError(String(e?.message || e)))
      .finally(() => setLoading(false))
  }

  const match = matchesDenylist(runnable.name, runnable.host, status)
  const blocked = match.mode === 'enforce' && (match.disabled || match.rules.length > 0)
  const warned = match.mode === 'warn' && (match.disabled || match.rules.length > 0)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <SectionLabel label="Denylist" />
        {!match.checked && (
          <Button size="small" type="text" loading={loading} onClick={check}
            style={{ fontSize: 11, height: 20, padding: '0 6px', color: accent }}>
            Check on {runnable.host}
          </Button>
        )}
      </div>
      {error && <Text style={{ fontSize: 11, color: '#ff7875' }}>{error}</Text>}
      {match.checked && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {blocked ? <Tag color="red" style={{ fontSize: 10, margin: 0 }}>blocked</Tag>
              : warned ? <Tag color="gold" style={{ fontSize: 10, margin: 0 }}>warn</Tag>
              : <Tag color="green" style={{ fontSize: 10, margin: 0 }}>allowed</Tag>}
            <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
              mode: {match.mode || 'off'}{match.disabled ? ' · all disabled' : ''}
            </Text>
          </div>
          {match.rules.map((r, i) => (
            <Text key={i} type="secondary" style={{ fontSize: 11, fontFamily: 'monospace', color: textMuted(isDark) }}>
              rule: runnables [{r.runnables.join(', ')}] · hosts [{r.hosts.join(', ')}] · users [{r.users.join(', ')}]{r.reason ? ` — ${r.reason}` : ''}
            </Text>
          ))}
        </div>
      )}
    </div>
  )
}
// ── The inspector (right pane) ────────────────────────────────────────────────

function SpecInspector({ runnable, vis, onOpenForm }: {
  runnable: Runnable
  vis: ToolVisibility | null
  onOpenForm?: (r: Runnable, commandPath?: string[]) => void
}) {
  const isDark = useIsDark()
  const raw = runnable.rawSpec ?? {}
  const args = runnable.args ?? []
  const subcommands = runnable.commands ? Object.values(runnable.commands) : []

  const resolve = (key: string): { value: unknown; isDefault: boolean } | null => {
    if (key in raw) return { value: raw[key], isDefault: false }
    if (key in SPEC_DEFAULTS) return { value: SPEC_DEFAULTS[key], isDefault: true }
    return null
  }
  const autonomyField = resolve('autonomy') ?? { value: runnable.autonomy || 'confirm', isDefault: true }
  const outputField = resolve('output')
  const serveField = resolve('serve')
  const discoverableField = resolve('discoverable')

  const runAs = (raw.run_as as string | undefined) ?? runnable.runAs
  const becomeMethod = resolve('become_method')
  const becomeFlags = raw.become_flags as string | undefined
  const reason = raw['autonomy-reason'] as string | undefined
  const envFile = raw.runspec_env as string | undefined
  const groups = (raw.groups as Record<string, unknown> | undefined) || undefined

  type ExampleEntry = { description?: string; args?: Record<string, unknown> }
  const examples = raw.examples as ExampleEntry[] | undefined

  const KNOWN = new Set([
    'description', 'autonomy', 'output', 'serve', 'discoverable',
    'run_as', 'become_method', 'become_flags', 'autonomy-reason', 'runspec_env',
    'examples', 'groups', 'require-command', 'require_command',
  ])
  const customFields = Object.entries(raw).filter(([k]) => !KNOWN.has(k))

  const visibility = effectiveVisibility(runnable.name, runnable.group, runnable.host, vis)
  const isInternal = discoverableField && discoverableField.value === false

  // Full normalized spec for the raw view — rawSpec plus the structured args/commands
  // discovery pulls out, so nothing is hidden.
  const fullSpec = useMemo(() => ({
    ...raw,
    ...(args.length ? { args } : {}),
    ...(runnable.commands ? { commands: runnable.commands } : {}),
  }), [raw, args, runnable.commands])

  const border = isDark ? '#2a2a2a' : '#e8e8e8'

  return (
    <div style={{ padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* Header */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Text style={{ fontFamily: 'monospace', fontSize: 16, color: textColor(isDark) }}>
            <span style={{ color: textMuted(isDark), fontSize: 14 }}>{runnable.group}/</span>{runnable.name}
          </Text>
          <Tag style={{ margin: 0 }}>{runnable.host}</Tag>
          <AutonomyTag level={String(autonomyField.value)} />
          {visibility === 'agent' && (
            <Tooltip title="Hidden from the agent toolset ([tool_visibility] hidden_from_agent) — still usable in Forms">
              <Tag icon={<EyeInvisibleOutlined />} style={{ fontSize: 10, margin: 0 }}>agent-hidden</Tag>
            </Tooltip>
          )}
          {visibility === 'all' && (
            <Tooltip title="Hidden everywhere ([tool_visibility] hidden_from_all) — agent, Forms and catalogs">
              <Tag icon={<EyeInvisibleOutlined />} color="volcano" style={{ fontSize: 10, margin: 0 }}>hidden</Tag>
            </Tooltip>
          )}
          {isInternal && (
            <Tooltip title="discoverable = false — a hidden internal helper (not listed), still runnable directly">
              <Tag style={{ fontSize: 10, margin: 0 }}>internal</Tag>
            </Tooltip>
          )}
          {runnable.requireCommand && <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>command required</Tag>}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
            {onOpenForm && (
              <Tooltip title="Open this runnable in the Forms run dialog">
                <Button size="small" icon={<FormOutlined />} onClick={() => onOpenForm(runnable)}>Open in Forms</Button>
              </Tooltip>
            )}
            <CopyButton text={prettyJson(fullSpec)} title="Copy the full spec (JSON)" />
          </div>
        </div>
        {runnable.description && <Text type="secondary" style={{ fontSize: 13 }}>{runnable.description}</Text>}
      </div>

      {/* Settings */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        <SectionLabel label="Settings" />
        <FieldRow name="autonomy" isDefault={autonomyField.isDefault} value={<AutonomyTag level={String(autonomyField.value)} />} />
        {reason && <FieldRow name="autonomy-reason" value={<Text style={{ fontSize: 12, fontStyle: 'italic' }}>{reason}</Text>} />}
        {outputField && <FieldRow name="output" isDefault={outputField.isDefault} value={<Tag style={{ fontSize: 11, margin: 0 }}>{String(outputField.value)}</Tag>} />}
        {serveField && <FieldRow name="serve" isDefault={serveField.isDefault} value={<Tag color={serveField.value ? 'green' : 'default'} style={{ fontSize: 11, margin: 0 }}>{String(serveField.value)}</Tag>} />}
        {discoverableField && <FieldRow name="discoverable" isDefault={discoverableField.isDefault} value={<Tag color={discoverableField.value ? 'default' : 'volcano'} style={{ fontSize: 11, margin: 0 }}>{String(discoverableField.value)}</Tag>} />}
      </div>

      {/* Remote execution */}
      {(runAs || becomeFlags || envFile) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <SectionLabel label="Remote execution" />
          {runAs && <FieldRow name="run_as" value={<Tag color="orange" style={{ fontSize: 11, margin: 0 }}>{runAs}</Tag>} />}
          {runAs && becomeMethod && <FieldRow name="become_method" isDefault={becomeMethod.isDefault} value={<Tag style={{ fontSize: 11, margin: 0 }}>{String(becomeMethod.value)}</Tag>} />}
          {becomeFlags && <FieldRow name="become_flags" value={<Text code style={{ fontSize: 11 }}>{becomeFlags}</Text>} />}
          {envFile && <FieldRow name="runspec_env" value={<Text code style={{ fontSize: 11 }}>{envFile}</Text>} />}
        </div>
      )}

      {/* Visibility & control */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        <SectionLabel label="Visibility & control" />
        <FieldRow name="agent" value={
          visibility === 'visible'
            ? <Tag color="green" style={{ fontSize: 11, margin: 0 }}>visible</Tag>
            : visibility === 'agent'
              ? <Tag style={{ fontSize: 11, margin: 0 }}>hidden from agent</Tag>
              : <Tag color="volcano" style={{ fontSize: 11, margin: 0 }}>hidden everywhere</Tag>
        } />
        <DenylistSection runnable={runnable} />
      </div>

      {/* Arguments */}
      {args.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <SectionLabel label={`Arguments (${args.length})`} />
          {[...args].sort((a, b) => Number(!!b.required) - Number(!!a.required)).map(a => <ArgRow key={a.name} arg={a} />)}
        </div>
      )}

      {/* Argument groups (constraints) */}
      {groups && Object.keys(groups).length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <SectionLabel label="Argument groups" />
          {Object.entries(groups).map(([gname, g]) => {
            const gg = (g && typeof g === 'object' ? g : {}) as Record<string, unknown>
            const kind = ['exclusive', 'inclusive', 'at-least-one', 'exactly-one', 'conditional'].find(k => k in gg)
            const members = (gg.args as string[] | undefined) ?? []
            return (
              <div key={gname} style={{ fontSize: 12, fontFamily: 'monospace', color: textSecondary(isDark) }}>
                <Text code style={{ fontSize: 11 }}>{gname}</Text>{' '}
                {kind ? <Tag style={{ fontSize: 10, margin: '0 4px' }}>{kind}</Tag> : null}
                {members.length > 0 ? members.map(m => `--${m}`).join(', ') : JSON.stringify(gg)}
              </div>
            )
          })}
        </div>
      )}

      {/* Examples */}
      {examples && examples.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <SectionLabel label="Examples" />
          {examples.map((ex, i) => {
            const argStr = ex.args
              ? Object.entries(ex.args).map(([k, v]) => v === true ? `--${k}` : v === false ? '' : `--${k}=${v}`).filter(Boolean).join(' ')
              : ''
            return (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                {ex.description && <Text type="secondary" style={{ fontSize: 11 }}>{ex.description}</Text>}
                <Text code style={{ fontSize: 11 }}>{runnable.name}{argStr ? ` ${argStr}` : ''}</Text>
              </div>
            )
          })}
        </div>
      )}

      {/* Subcommands */}
      {subcommands.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <SectionLabel label="Subcommands" />
          {subcommands.map(sub => (
            <div key={sub.name} style={{ borderLeft: `2px solid ${border}`, paddingLeft: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Text code style={{ fontSize: 12 }}>{sub.name}</Text>
                <AutonomyTag level={sub.autonomy} />
                {sub.requireCommand && Object.keys(sub.commands ?? {}).length > 0 && (
                  <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>command required</Tag>
                )}
                {sub.description && <Text type="secondary" style={{ fontSize: 12 }}>{sub.description}</Text>}
              </div>
              {(sub.args ?? []).length > 0 && (
                <div style={{ marginTop: 4 }}>
                  {(sub.args ?? []).map(a => <ArgRow key={a.name} arg={a} />)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Custom fields */}
      {customFields.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <SectionLabel label="Custom fields" />
          {customFields.map(([k, v]) => (
            <FieldRow key={k} name={k} value={<Text style={{ fontSize: 12, fontFamily: 'monospace' }}>{JSON.stringify(v)}</Text>} />
          ))}
        </div>
      )}

      {/* Raw spec */}
      <Collapse
        ghost
        size="small"
        style={{ marginLeft: -8 }}
        items={[{
          key: 'raw',
          label: <Text type="secondary" style={{ fontSize: 11 }}>Raw spec (normalized JSON)</Text>,
          extra: <CopyButton text={prettyJson(fullSpec)} title="Copy raw spec" />,
          children: (
            <pre style={{
              margin: 0, padding: '10px 12px', fontFamily: 'monospace', fontSize: 11.5,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 460, overflow: 'auto',
              background: isDark ? '#141414' : '#f5f5f5', borderRadius: 6,
              color: isDark ? '#ccc' : '#333',
            }}>{prettyJson(fullSpec)}</pre>
          ),
        }]}
      />
    </div>
  )
}

// ── Container: master/detail ──────────────────────────────────────────────────

interface SpecsViewProps {
  runnables: Runnable[]
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
  onOpenForm?: (runnable: Runnable, commandPath?: string[]) => void
}

export function SpecsView({ runnables, selectedHost, activeScope, onScopeToggle, onOpenForm }: SpecsViewProps) {
  const isDark = useIsDark()
  const accent = useAccent()
  const [search, setSearch] = useState('')
  const [autonomyFilter, setAutonomyFilter] = useState<string>('all')
  const [visFilter, setVisFilter] = useState<string>('all')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [vis, setVis] = useState<ToolVisibility | null>(null)

  const [listWidth, setListWidth] = useState<number>(() => {
    try { const s = Number(localStorage.getItem(LIST_WIDTH_KEY)); if (s >= LIST_MIN && s <= LIST_MAX) return s } catch { /* ignore */ }
    return 340
  })
  const resizing = useRef(false)

  // Load [tool_visibility] (display-only; backend is authoritative). Refresh on save.
  useEffect(() => {
    const load = () => bridge.get_tool_visibility().then(setVis).catch(() => setVis(null))
    load()
    window.addEventListener('runspec:tool_visibility_changed', load)
    return () => window.removeEventListener('runspec:tool_visibility_changed', load)
  }, [])

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault()
    resizing.current = true
    const onMove = (ev: MouseEvent) => {
      if (!resizing.current) return
      const next = Math.max(LIST_MIN, Math.min(LIST_MAX, ev.clientX - 8))
      setListWidth(next)
    }
    const onUp = () => {
      resizing.current = false
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      try { localStorage.setItem(LIST_WIDTH_KEY, String(listWidth)) } catch { /* ignore */ }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }
  // Persist the width whenever it settles.
  useEffect(() => { try { localStorage.setItem(LIST_WIDTH_KEY, String(listWidth)) } catch { /* ignore */ } }, [listWidth])

  const q = search.trim().toLowerCase()
  const filtered = useMemo(() => runnables.filter(r => {
    if (selectedHost && r.host !== selectedHost) {
      // selectedHost only seeds the initial view; keep it as a soft filter unless searching
      if (!q) return r.host === selectedHost
    }
    if (autonomyFilter !== 'all' && (r.autonomy || 'confirm') !== autonomyFilter) return false
    if (visFilter !== 'all') {
      const ev = effectiveVisibility(r.name, r.group, r.host, vis)
      if (visFilter === 'visible' && ev !== 'visible') return false
      if (visFilter === 'hidden' && ev === 'visible') return false
    }
    if (!q) return true
    return (
      r.name.toLowerCase().includes(q) ||
      r.group.toLowerCase().includes(q) ||
      r.host.toLowerCase().includes(q) ||
      (r.description ?? '').toLowerCase().includes(q) ||
      (r.args ?? []).some(a => a.name.toLowerCase().includes(q) || (a.description ?? '').toLowerCase().includes(q))
    )
  }), [runnables, selectedHost, autonomyFilter, visFilter, vis, q])

  // Group filtered runnables by host for section headers.
  const grouped = useMemo(() => {
    const byHost = new Map<string, Runnable[]>()
    for (const r of [...filtered].sort((a, b) => `${a.group}/${a.name}`.localeCompare(`${b.group}/${b.name}`))) {
      const arr = byHost.get(r.host) ?? []
      arr.push(r); byHost.set(r.host, arr)
    }
    return [...byHost.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [filtered])

  const selected = useMemo(
    () => runnables.find(r => specKey(r) === selectedKey) ?? null,
    [runnables, selectedKey],
  )

  const border = isDark ? '#2a2a2a' : '#e8e8e8'

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0 }}>
      {/* List pane */}
      <div style={{ width: listWidth, flexShrink: 0, borderRight: `1px solid ${border}`, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ padding: '12px 12px 8px', display: 'flex', flexDirection: 'column', gap: 8, flexShrink: 0 }}>
          <Input
            prefix={<SearchOutlined />}
            placeholder="Search name, group, host, args…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            allowClear
            size="small"
          />
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <Segmented
              size="small"
              value={autonomyFilter}
              onChange={v => setAutonomyFilter(String(v))}
              options={[{ label: 'All', value: 'all' }, { label: 'Auto', value: 'autonomous' }, { label: 'Confirm', value: 'confirm' }]}
            />
            <Segmented
              size="small"
              value={visFilter}
              onChange={v => setVisFilter(String(v))}
              options={[{ label: 'Any', value: 'all' }, { label: 'Visible', value: 'visible' }, { label: 'Hidden', value: 'hidden' }]}
            />
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>{filtered.length} runnable{filtered.length !== 1 ? 's' : ''}</Text>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {grouped.map(([host, rows]) => (
            <div key={host}>
              <div style={{
                position: 'sticky', top: 0, zIndex: 1, padding: '4px 12px',
                fontSize: 10, fontFamily: 'monospace', textTransform: 'uppercase', letterSpacing: '0.08em',
                color: textMuted(isDark), background: isDark ? '#161616' : '#fafafa',
                borderBottom: `1px solid ${border}`, userSelect: 'none',
              }}>{host}</div>
              {rows.map(r => {
                const key = specKey(r)
                const active = key === selectedKey
                const v = effectiveVisibility(r.name, r.group, r.host, vis)
                const internal = (r.rawSpec?.discoverable === false)
                return (
                  <div
                    key={key}
                    className="rs-host-row"
                    onClick={() => setSelectedKey(key)}
                    style={{
                      padding: '7px 12px', cursor: 'pointer', borderBottom: `1px solid ${isDark ? '#1a1a1a' : '#f0f0f0'}`,
                      background: active ? withAlpha(accent, 0.14) : undefined,
                      borderLeft: `2px solid ${active ? accent : 'transparent'}`,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontFamily: 'monospace', fontSize: 13, color: textColor(isDark), flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        <span style={{ color: textMuted(isDark), fontSize: 12 }}>{r.group}/</span>{r.name}
                      </span>
                      {(r.autonomy === 'autonomous') && <Tag color="green" style={{ fontSize: 9, margin: 0, lineHeight: '16px' }}>auto</Tag>}
                      {v !== 'visible' && <Tooltip title={v === 'all' ? 'hidden everywhere' : 'hidden from agent'}><EyeInvisibleOutlined style={{ fontSize: 11, color: textFaint(isDark) }} /></Tooltip>}
                      {internal && <Tooltip title="internal helper (discoverable = false)"><LockOutlined style={{ fontSize: 11, color: textFaint(isDark) }} /></Tooltip>}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
                      <Tag
                        color={activeScope.includes(r.group) ? 'geekblue' : 'blue'}
                        onClick={e => { e.stopPropagation(); onScopeToggle(r.group) }}
                        style={{ cursor: 'pointer', fontSize: 9, margin: 0, lineHeight: '16px' }}
                        title={activeScope.includes(r.group) ? 'Remove group from scope' : 'Add group to scope'}
                      >{r.group}</Tag>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {(r.args ?? []).length} arg{(r.args ?? []).length !== 1 ? 's' : ''}
                        {r.commands ? ` · ${Object.keys(r.commands).length} sub` : ''}
                      </Text>
                    </div>
                  </div>
                )
              })}
            </div>
          ))}
          {filtered.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center', color: textMuted(isDark) }}>
              <Text type="secondary" style={{ fontSize: 12 }}>No runnables match.</Text>
            </div>
          )}
        </div>
      </div>

      {/* Resize handle */}
      <div
        className="rs-sidebar-resize"
        onMouseDown={startResize}
        style={{ width: 6, flexShrink: 0, cursor: 'col-resize', position: 'relative', marginLeft: -3 }}
      />

      {/* Detail pane */}
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
        {selected ? (
          <SpecInspector runnable={selected} vis={vis} onOpenForm={onOpenForm} />
        ) : (
          <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<Text type="secondary" style={{ fontSize: 13 }}>Select a runnable to inspect its full spec</Text>}
            />
          </div>
        )}
      </div>
    </div>
  )
}
