import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Button, ColorPicker, Collapse, DatePicker, Input, InputNumber, Modal, Select, Slider, Switch, Tag, TimePicker, Tooltip, Typography, message } from 'antd'
import { SearchOutlined, RightOutlined, ArrowLeftOutlined, AppstoreOutlined, InfoCircleOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import type { ArgDef, Host, Runnable, Runbook, SavedRun } from '../bridge'
import { bridge } from '../bridge'
import { useIsDark, useAccent, withAlpha, surfaces, RADIUS, textSecondary, MONO_FONT } from '../ThemeContext'
import { PathInput } from './PathInput'
import { runnableHidden } from '../toolVisibility'

const { Text } = Typography

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
}

// Whether the run form should block submission because a required arg has no
// value. For a human at a form, a required arg must be typed — *including* a
// password arg: credentials are never copied into args on the form path, so the
// only way to supply one is the masked field (sent via the RUNSPEC_*_ARG_* env
// channel). Credential reuse for humans goes through the name-derived env-var
// section instead, which targets runnables that read bare env vars. Exported for
// unit tests.
export function argNeedsValue(arg: ArgDef, value: unknown): boolean {
  if (!arg.required) return false
  // A multi-select holds an array — an empty one is "no value".
  if (Array.isArray(value)) return value.length === 0
  return value === undefined || value === ''
}

// Whether a choice arg should render as a multi-select (Ant `<Select mode="multiple">`,
// value = string[]) rather than a single-value picker. Driven by `multiple` — the
// runtime multi-value contract: core accepts a list and validates each item against
// `options` — or by an explicit `ui = "multiselect"` presentational hint (pair it
// with `multiple = true`, since `multiple` is what makes the arg genuinely
// multi-valued). Exported for unit tests.
export function isMultiChoice(arg: ArgDef): boolean {
  return arg.type === 'choice' && !!arg.options && (arg.multiple === true || arg.ui === 'multiselect')
}

// String fields auto-grow on multiple lines, but a bare Enter should not insert a
// newline (it's noise in a single-value field) — only Shift+Enter does. Returns
// true when the key was suppressed. Exported for unit tests.
export function suppressPlainEnter(e: { key: string; shiftKey: boolean; preventDefault: () => void }): boolean {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    return true
  }
  return false
}

// Split a group's args for the run form. The collapsed "Default arguments"
// section holds only args that actually carry a default value; everything else —
// required args AND optional args with no default — stays visible, because an
// optional arg with no default is still something the operator may need to type
// (it doesn't belong under a "Default arguments" heading). A flag (default
// `false`) or a `$VAR`-ref default counts as having a default. Exported for tests.
export function partitionArgs(args: ArgDef[]): { visible: ArgDef[]; defaulted: ArgDef[] } {
  const hasDefault = (a: ArgDef) => a.default !== undefined && a.default !== null
  return {
    visible: args.filter(a => !hasDefault(a)),
    defaulted: args.filter(a => hasDefault(a)),
  }
}

// In the multi-column arg grid, most fields sit in a column cell, but a few need
// the full row width to stay usable: path/folder pickers (long paths + a browse
// button). Exported for unit tests.
export function argSpansRow(arg: ArgDef): boolean {
  return arg.type === 'path' || arg.ui === 'directory'
}

// Runnable-id ↔ Runnable resolution helpers now live in ./formsMatch so App can
// call matchRunnableForEdit without importing this (heavy) module — see that
// file. Re-exported here for back-compat (the colocated test imports them here).
export {
  sanitizeToolName,
  dedupToolName,
  legacyDedupToolName,
  matchRunnableForEdit,
} from './formsMatch'

// Tag colour + short label for an autonomy level. Pure — exported for tests.
// Undefined/blank is treated as the safe default ("confirm"), so a runnable or
// subcommand always shows a level rather than nothing.
export function autonomyTagProps(level?: string): { color: string; label: string } {
  switch ((level || 'confirm').toLowerCase()) {
    case 'autonomous': return { color: 'green', label: 'auto' }
    case 'supervised': return { color: 'gold', label: 'supervised' }
    case 'manual': return { color: 'red', label: 'manual' }
    default: return { color: 'default', label: 'confirm' }
  }
}

// The autonomy pill shown in the run dialog title and on each subcommand card.
function AutonomyTag({ level }: { level?: string }) {
  const { color, label } = autonomyTagProps(level)
  return (
    <Tooltip title={`autonomy: ${level || 'confirm'}`}>
      <Tag color={color} style={{ fontSize: 11, margin: 0 }}>{label}</Tag>
    </Tooltip>
  )
}

// A single-line, ellipsised text that reveals its FULL content on hover *and*
// click, with an always-visible info icon so the affordance is discoverable —
// used for arg + subcommand descriptions, which used to wrap and blow up the row
// height. The tooltip and icon are unconditional: an earlier version attached the
// tooltip *only* when a runtime scrollWidth/clientWidth measurement judged the
// text overflowed, but that measurement runs while the run dialog is still
// animating open (and reads 0/0 for args inside the collapsed "Default arguments"
// section), so it frequently decided "not overflowing" and left a clipped
// description with no tooltip, no `help` cursor and no icon — the reported
// "truncated, mouse-over does nothing, no ⓘ" bug. Now hover or click the row (or
// the ⓘ) always shows the full text; the measurement gate is gone. The text still
// clips to one line via CSS ellipsis for a compact preview, and the ancestor
// grid/flex cells must still allow shrinking (`min-width: 0`) for that clip.
// Colour comes from the theme-aware --rs-text-secondary var so it works in both
// dark and light.
export function TruncatedText({ text, style }: { text: string; style?: CSSProperties }) {
  return (
    <Tooltip title={text} trigger={['hover', 'click']}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0, cursor: 'help', ...style }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
          {text}
        </span>
        <InfoCircleOutlined style={{ flexShrink: 0, fontSize: 12, opacity: 0.55 }} />
      </span>
    </Tooltip>
  )
}

// Responsive multi-column layout for a group of arg rows: fills as many ~260px
// columns as the (dynamically sized) dialog allows — 3+ when wide — and
// collapses to one when narrow. `auto-fit` stretches the items to fill the row
// when there are only a few, so a short form doesn't leave empty tracks.
// `align-items: start` so a taller field (a grown textarea) doesn't stretch its
// row-mates.
function ArgGrid({ children }: { children: ReactNode }) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
      gap: 14, alignItems: 'start',
    }}>
      {children}
    </div>
  )
}

// Mirrors credentials.env_prefix / env_field_map in the Python bridge: a saved
// credential applied to a run is injected as plain env vars whose names derive
// from its label + kind by a strict convention (label "Jira" + userpass →
// JIRA_USERNAME, JIRA_PASSWORD). Used to preview the names the run will set.
export function credEnvPrefix(label: string): string {
  return label.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase()
}
// Mirrors credentials._KIND_ENV_FIELDS, including the aliases — userpass emits
// both _USERNAME and _USER; token emits both _TOKEN and _PAT — so a credential
// labelled "JSM" feeds runspec-jsm (JSM_USER/JSM_PAT) and runspec-snow.
const CRED_KIND_SUFFIXES: Record<string, string[]> = {
  userpass: ['USERNAME', 'USER', 'PASSWORD'],
  password: ['PASSWORD'],
  token: ['TOKEN', 'PAT'],
  'ssh-key': [],
}
export function credEnvNames(label: string, kind: string): string[] {
  const prefix = credEnvPrefix(label)
  if (!prefix) return []
  return (CRED_KIND_SUFFIXES[kind] ?? []).map(s => `${prefix}_${s}`)
}

// A selected credential whose username_arg/secret_arg matches a declared arg
// binds that arg: the username pre-fills it (visible, editable), the secret is
// supplied server-side via the env channel. Drives the per-arg "from <label>"
// indicator so the fill is never silent.
export interface ArgBinding { label: string; role: 'username' | 'secret' }
type SlimCred = {
  id: string; label: string; kind: string; has_secret: boolean
  username: string; username_arg: string; secret_arg: string
}

// Map declared-arg-name → binding, from the operator's selected credentials.
export function bindingsForArgs(
  selected: SlimCred[],
  declaredArgs: Set<string>,
): Record<string, ArgBinding> {
  const out: Record<string, ArgBinding> = {}
  for (const c of selected) {
    if (c.username_arg && declaredArgs.has(c.username_arg) && !out[c.username_arg]) {
      out[c.username_arg] = { label: c.label, role: 'username' }
    }
    if (c.secret_arg && declaredArgs.has(c.secret_arg) && !out[c.secret_arg]) {
      out[c.secret_arg] = { label: c.label, role: 'secret' }
    }
  }
  return out
}

// Initial run-form values: the spec defaults ($VAR refs left blank so they show
// as placeholders), overlaid with a previous run's values for edit-and-rerun.
// The overlay is filtered to declared args so a stale/removed key can't ride into
// the run; a password arg is never pre-filled (secrets aren't recorded). Exported
// for unit tests.
export function initialFormValues(
  args: ArgDef[],
  prefill?: Record<string, unknown>,
): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  const declared = new Set(args.map(a => a.name))
  for (const arg of args) {
    if (arg.default !== undefined && !isVarRef(arg.default)) {
      values[arg.name] = arg.default
    }
  }
  for (const [k, v] of Object.entries(prefill ?? {})) {
    const arg = args.find(a => a.name === k)
    if (!declared.has(k) || arg?.type === 'password') continue
    values[k] = v
  }
  return values
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
  onChange: (v: unknown) => void
  binding?: ArgBinding
}

export function ArgInput({ arg, host, runnable, runAs, group, value, error, onChange, binding }: ArgInputProps) {
  const isDark = useIsDark()
  // `hint` (if declared) is the form placeholder; otherwise fall back to a
  // visible $VAR_REF default so the operator sees where the value comes from.
  const placeholder = arg.hint ?? (isVarRef(arg.default) ? arg.default as string : undefined)

  const inputStyle = {
    fontFamily: MONO_FONT,
    fontSize: 13,
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
    // A `multiple` choice (or ui="multiselect") is an Ant multi-select whose
    // value is a string[]; the backend serialises the list into repeated
    // `--flag v` args (executor.args_to_argv), and core validates each item
    // against `options`. A scalar default/prefill is coerced up to a one-element
    // array so the control accepts it.
    const multi = isMultiChoice(arg)
    const multiValue = Array.isArray(value) ? (value as string[]) : value != null ? [value as string] : []
    return (
      <Select
        size="small"
        mode={multi ? 'multiple' : undefined}
        allowClear={multi}
        value={multi ? multiValue : (value as string | undefined)}
        onChange={onChange}
        placeholder={multi ? 'Select one or more…' : 'Select…'}
        style={{ width: '100%', fontFamily: MONO_FONT, fontSize: 13 }}
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
        placeholder={binding?.role === 'secret'
          ? `supplied by credential “${binding.label}” — type to override`
          : (placeholder ?? 'secret — sent as an env var, not on the command line')}
        autoComplete="new-password"
        style={inputStyle}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.type === 'int' || arg.type === 'float') {
    const step = arg.type === 'float' ? 0.1 : 1
    // A bounded numeric can render as a slider when it opts in with ui="slider"
    // and declares a range; otherwise (and with no range) it's the number box.
    if (arg.ui === 'slider' && arg.range) {
      const [min, max] = arg.range
      const current = typeof value === 'number' ? value : (typeof arg.default === 'number' ? arg.default : min)
      return (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Slider
            min={min}
            max={max}
            step={step}
            value={current}
            onChange={onChange}
            style={{ flex: 1, margin: 0 }}
          />
          <InputNumber
            size="small"
            value={value as number | undefined}
            onChange={onChange}
            step={step}
            min={min}
            max={max}
            style={{ width: 80, ...inputStyle }}
            status={error ? 'error' : undefined}
          />
        </div>
      )
    }
    return (
      <InputNumber
        size="small"
        value={value as number | undefined}
        onChange={onChange}
        placeholder={placeholder}
        step={step}
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

  if (arg.ui === 'date' || arg.ui === 'datetime') {
    // Date / date-time picker on a `str` arg (type stays str). The form value is
    // an ISO string — date-only `YYYY-MM-DD` for `date`, a full timestamp for
    // `datetime` — so the wire contract is an ordinary string, as before.
    const withTime = arg.ui === 'datetime'
    const dt = typeof value === 'string' && value ? dayjs(value) : undefined
    return (
      <DatePicker
        size="small"
        value={dt && dt.isValid() ? dt : null}
        showTime={withTime}
        onChange={d => onChange(d ? (withTime ? d.toISOString() : d.format('YYYY-MM-DD')) : undefined)}
        placeholder={placeholder ?? (withTime ? 'Select date & time…' : 'Select date…')}
        style={{ width: '100%', ...inputStyle }}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.ui === 'month') {
    // Month-granularity picker on a `str` arg. Value stored as a `YYYY-MM` string;
    // anchor to the first of the month to parse the string back into a Dayjs.
    const m = typeof value === 'string' && value ? dayjs(`${value}-01`) : undefined
    return (
      <DatePicker
        picker="month"
        size="small"
        value={m && m.isValid() ? m : null}
        onChange={d => onChange(d ? d.format('YYYY-MM') : undefined)}
        placeholder={placeholder ?? 'Select month…'}
        style={{ width: '100%', ...inputStyle }}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.ui === 'time') {
    // Time-only picker on a `str` arg. Value stored as an `HH:mm:ss` string; the
    // control needs a Dayjs, so anchor the string to an arbitrary date to parse
    // and format just the time back out.
    const t = typeof value === 'string' && value ? dayjs(`2000-01-01 ${value}`) : undefined
    return (
      <TimePicker
        size="small"
        value={t && t.isValid() ? t : null}
        onChange={d => onChange(d ? d.format('HH:mm:ss') : undefined)}
        placeholder={placeholder ?? 'Select time…'}
        style={{ width: '100%', ...inputStyle }}
        status={error ? 'error' : undefined}
      />
    )
  }

  if (arg.ui === 'color') {
    // Colour picker on a `str` arg. Value stored as a `#rrggbb` hex string.
    const hex = typeof value === 'string' && value ? value : undefined
    return (
      <ColorPicker
        size="small"
        value={hex ?? null}
        onChange={c => onChange(c.toHexString())}
        showText
        format="hex"
      />
    )
  }

  // Plain string field. A TextArea that auto-grows as the operator adds lines;
  // Shift+Enter inserts a newline, a bare Enter is suppressed (no stray newline).
  return (
    <Input.TextArea
      size="small"
      value={value as string ?? ''}
      onChange={e => onChange(e.target.value)}
      onKeyDown={suppressPlainEnter}
      autoSize={{ minRows: 1, maxRows: 8 }}
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

// The defaulted/optional args of a group, folded into a section that is
// collapsed by default (no defaultActiveKey) so the form leads with the required
// fields. `ghost` keeps it visually light, matching the flat layout.
function DefaultArgsCollapse({ count, children }: { count: number; children: ReactNode }) {
  return (
    <Collapse
      size="small"
      ghost
      style={{ marginLeft: -8, marginRight: -8 }}
      items={[{
        key: 'defaults',
        label: <Text type="secondary" style={{ fontSize: 12 }}>Default arguments ({count})</Text>,
        children,
      }]}
    />
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

  // Reset form when the modal or node changes
  useEffect(() => {
    if (!state) return
    setValues(initialFormValues(state.currentNode.args ?? [], state.prefill))
    setErrors({})
    setCredentialIds([])
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
    onSubmit(rootRunnable, values, commandPath, credentialIds.length ? credentialIds : undefined)
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

// A saved runbook, rendered as a launchable card alongside runnables. Clicking opens
// the same parameter form the `$` chat menu uses (App owns the RunbookRunModal).
function RunbookCard({ runbook, onClick }: { runbook: Runbook; onClick: () => void }) {
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
        <div style={{ marginLeft: 'auto' }}>
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
function SavedRunCard({ savedRun, onClick }: { savedRun: SavedRun; onClick: () => void }) {
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
        <div style={{ marginLeft: 'auto' }}>
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
export function filterSavedRuns(savedRuns: SavedRun[], search: string): SavedRun[] {
  if (!search) return savedRuns
  const low = search.toLowerCase()
  return savedRuns.filter(s =>
    s.id.toLowerCase().includes(low) ||
    (s.title ?? '').toLowerCase().includes(low) ||
    (s.description ?? '').toLowerCase().includes(low) ||
    (s.runnable ?? '').toLowerCase().includes(low) ||
    (s.target ?? '').toLowerCase().includes(low)
  )
}

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
}

export function FormsView({
  runnables, hosts, selectedHost, activeScope,
  onRunRunnable, pendingForm, onPendingFormClear,
  runbooks = [], onOpenRunbook,
  savedRuns = [], onOpenSavedRun,
}: FormsViewProps) {
  const isDark = useIsDark()
  const [search, setSearch] = useState('')
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

  const borderCol = 'var(--rs-border)'

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
            <div style={{ marginBottom: 28 }}>
              <div style={{
                fontSize: 11, color: textSecondary(isDark),
                textTransform: 'uppercase', letterSpacing: '0.08em',
                marginBottom: 12, paddingBottom: 6,
                borderBottom: `1px solid ${borderCol}`,
              }}>
                Saved runs
              </div>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                gap: 10,
              }}>
                {filteredSavedRuns.map(s => (
                  <SavedRunCard key={s.id} savedRun={s} onClick={() => onOpenSavedRun?.(s)} />
                ))}
              </div>
            </div>
          )}
          {filteredRunbooks.length > 0 && (
            <div style={{ marginBottom: 28 }}>
              <div style={{
                fontSize: 11, color: textSecondary(isDark),
                textTransform: 'uppercase', letterSpacing: '0.08em',
                marginBottom: 12, paddingBottom: 6,
                borderBottom: `1px solid ${borderCol}`,
              }}>
                Runbooks
              </div>
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                gap: 10,
              }}>
                {filteredRunbooks.map(s => (
                  <RunbookCard key={s.id} runbook={s} onClick={() => onOpenRunbook?.(s)} />
                ))}
              </div>
            </div>
          )}
          {[...groupMap.entries()].map(([group, groupRunnables]) => (
            <div key={group} style={{ marginBottom: 28 }}>
              <div style={{
                fontSize: 11, color: textSecondary(isDark),
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
