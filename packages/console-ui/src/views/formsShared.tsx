// formsShared — the run-form helpers + light input components shared between the
// (lazy-loaded) FormsView and its eager consumers (CommandInput, SettingsDrawer,
// RunnableAction, RunbookRunModal, SchedulesView).
//
// These used to live in FormsView.tsx, but importing them from there pinned the
// whole heavy view (RunModal, the card grid, the container) into the main chunk,
// so App.tsx's `React.lazy(FormsView)` was ineffective (the bundler warned). They
// now live here — a light module the eager consumers import — so FormsView is
// reached only via the dynamic import and actually code-splits. FormsView re-exports
// the public helpers for the colocated tests.
import { type CSSProperties, type ReactNode } from 'react'
import {
  Collapse,
  ColorPicker,
  DatePicker,
  Input,
  InputNumber,
  Select,
  Slider,
  Switch,
  TimePicker,
  Tooltip,
  Typography,
} from 'antd'
import { InfoCircleOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import type { ArgDef, SavedRun } from '../bridge'
import { useIsDark, MONO_FONT } from '../ThemeContext'
import { PathInput } from './PathInput'

// Runnable-id ↔ Runnable resolution helpers live in ./formsMatch so App can call
// matchRunnableForEdit without importing a heavy view. Re-exported here (and via
// FormsView for back-compat) so a single import site serves them.
export {
  sanitizeToolName,
  dedupToolName,
  legacyDedupToolName,
  matchRunnableForEdit,
} from './formsMatch'

export function isVarRef(val: unknown): val is string {
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

// A single-line, ellipsised text that reveals its FULL content on hover *and*
// click, with an always-visible info icon so the affordance is discoverable —
// used for arg + subcommand descriptions. The tooltip and icon are unconditional
// (an earlier scrollWidth/clientWidth gate ran while the dialog was still
// animating and mis-decided "not overflowing", leaving a clipped description with
// no tooltip). The text clips to one line via CSS ellipsis; ancestor grid/flex
// cells must allow shrinking (`min-width: 0`). Colour comes from the theme-aware
// --rs-text-secondary var so it works in both dark and light.
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
export type SlimCred = {
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
/**
 * The run dialog's "Debug logging" switch. Core adds a `--debug` flag to every
 * runnable whose logging is on, but it isn't in runspec.toml, so the form never
 * lists it. When the switch is on this adds `debug: true` to the submitted args;
 * the backend moves it onto the env channel (never argv), so a runnable without
 * the flag just ignores it. A runnable that declares its own `debug` arg keeps
 * it as a normal field, and the switch isn't offered (`offerDebugSwitch`).
 */
export function offerDebugSwitch(declaredArgNames: Set<string>): boolean {
  return !declaredArgNames.has('debug')
}

export function withDebugFlag(
  values: Record<string, unknown>,
  debug: boolean,
  declaredArgNames: Set<string>,
): Record<string, unknown> {
  if (!debug || !offerDebugSwitch(declaredArgNames)) return values
  return { ...values, debug: true }
}

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

// ─── Arg input ───────────────────────────────────────────────────────────────

export interface ArgInputProps {
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

// Free-text filter over a saved run's id/title/description/runnable/target.
// Exported for unit tests.
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

// ── shared run-form layout (Forms tab, runbook + saved-run dialogs) ───────────

// Responsive multi-column layout for a group of arg rows: fills as many ~260px
// columns as the (dynamically sized) dialog allows — 3+ when wide — and
// collapses to one when narrow. `auto-fit` stretches the items to fill the row
// when there are only a few, so a short form doesn't leave empty tracks.
// `align-items: start` so a taller field (a grown textarea) doesn't stretch its
// row-mates.
export function ArgGrid({ children }: { children: ReactNode }) {
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


// The defaulted/optional args of a group, folded into a section that is
// collapsed by default (no defaultActiveKey) so the form leads with the required
// fields. `ghost` keeps it visually light, matching the flat layout.
export function DefaultArgsCollapse({ count, children }: { count: number; children: ReactNode }) {
  return (
    <Collapse
      size="small"
      ghost
      style={{ marginLeft: -8, marginRight: -8 }}
      items={[{
        key: 'defaults',
        label: <Typography.Text type="secondary" style={{ fontSize: 12 }}>Default arguments ({count})</Typography.Text>,
        children,
      }]}
    />
  )
}
