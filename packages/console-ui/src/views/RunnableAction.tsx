// RunnableAction.tsx — the shared "pick a target, pick a runnable, fill its
// args, attach credentials" building blocks used by BOTH the Schedules modal
// (runnable-action schedules) and the saved-run editor (Forms tab). One source
// so the two dialogs render and behave identically; extracted from
// SchedulesView (which previously owned local copies).

import { type CSSProperties, type ReactNode } from 'react'
import { Collapse, Form, Segmented, Select, Space, Switch, Tag, Tooltip, Typography } from 'antd'
import type { ArgDef, Credential, Host, Runnable } from '../bridge'
import { ArgInput, bindingsForArgs, credEnvNames, partitionArgs, TruncatedText } from './formsShared'

const { Text } = Typography

// The targeting modes shared by runnable-action schedules and saved runs.
// 'profile' (fan out across a host profile's members) is saved-run only — a
// dialog offers it by passing `profileNames` to TargetFormItems.
export type Target = 'host' | 'group' | 'anyhost' | 'selected' | 'profile'

// Whether a host carries host profile `profile` (case-insensitive, matching the
// backend's hosts.host_in_profile).
export const hostInProfile = (h: Host, profile: string) =>
  !!profile && (h.profiles ?? []).some(p => p.toLowerCase() === profile.toLowerCase())

// The distinct runnables available on the targeted host(s): a single host's
// runnables, or (group / any-of / selected mode) every runnable present on any
// targeted host, de-duplicated by name. Pure — exported for unit tests.
export function runnablesForTarget(
  runnables: Runnable[],
  hosts: Host[],
  target: Target,
  sel: { host?: string; group?: string; hostsList?: string[]; selectedList?: string[]; profile?: string },
): Runnable[] {
  const targeted = target === 'group'
    ? new Set(hosts.filter(h => h.group === (sel.group ?? '')).map(h => h.name))
    : target === 'profile'
    ? new Set(hosts.filter(h => hostInProfile(h, sel.profile ?? '')).map(h => h.name))
    : target === 'anyhost' ? new Set(sel.hostsList ?? [])
    : target === 'selected' ? new Set(sel.selectedList ?? [])
    : null
  if (targeted === null) return runnables.filter(r => r.host === (sel.host ?? ''))
  return [...new Map(
    runnables.filter(r => targeted.has(r.host)).map(r => [r.name, r]),
  ).values()]
}

// A declared ArgDef → the runspec params-table entry a saved run stores for an
// arg marked "Open" (asked at run time). Carries the shape the run form needs —
// type / options / required / description / default / ui hint — regenerated from
// the live ArgDef on every save so it tracks the runnable's spec. Pure —
// exported for unit tests.
export function argDefToParamDef(arg: ArgDef): Record<string, unknown> {
  const def: Record<string, unknown> = { type: arg.type ?? 'str' }
  if (arg.type === 'choice' && arg.options?.length) def.options = [...arg.options]
  // Pin required-ness explicitly — BOTH true AND false. The stored params table
  // is re-parsed through runspec inference when the saved run is loaded/run,
  // where "no default present ⇒ required = true" would otherwise silently flip a
  // genuinely optional arg (e.g. a force-Open password, which carries no
  // default) to required — making the operator type a secret a selected
  // credential was meant to supply. Emitting required = false holds it optional.
  def.required = !!arg.required
  if (arg.description) def.description = arg.description
  const d = arg.default
  // A $VAR reference resolves on the target host — keep it as the stored default.
  if (d !== undefined && d !== null && d !== '') def.default = d
  if (arg.range) def.range = [...arg.range]
  if (arg.ui) def.ui = arg.ui
  return def
}

// The selected runnable's subcommand paths as Select options — one entry per
// command node, space-joined ("db", "db restore"; commands nest max two levels).
// Empty when the runnable declares no subcommands, so the saved-run editor can
// hide the field entirely for plain runnables. `value` is the space-joined path
// (what the saved run stores, split on whitespace at save); the dropdown label
// adds the command's description. Pure — exported for unit tests.
export function commandPathOptions(
  runnable: Runnable,
  labelStyle: CSSProperties,
): { value: string; label: ReactNode }[] {
  const out: { value: string; label: ReactNode }[] = []
  const walk = (node: Runnable, prefix: string[]) => {
    for (const [name, child] of Object.entries(node.commands ?? {})) {
      const path = [...prefix, name]
      out.push({
        value: path.join(' '),
        label: (
          <>
            <code>{path.join(' ')}</code>
            {child.description && (
              <span style={{ ...labelStyle, marginLeft: 8 }}>{child.description}</span>
            )}
          </>
        ),
      })
      walk(child, path)
    }
  }
  walk(runnable, [])
  return out
}

// The effective args at a subcommand path: the args declared along the way
// (inherited globals) merged with each level's own, child winning on a name
// collision — the same effective set `runspec serve` advertises per leaf tool.
// An empty/unknown path yields the args accumulated up to where it resolved
// (root args for []). Pure — exported for unit tests.
export function effectiveArgsForPath(runnable: Runnable, path: string[]): ArgDef[] {
  const merged = new Map<string, ArgDef>()
  const absorb = (args: ArgDef[] | undefined) => {
    for (const a of args ?? []) merged.set(a.name, a)
  }
  absorb(runnable.args)
  let node: Runnable = runnable
  for (const seg of path) {
    const child = node.commands?.[seg]
    if (!child) break
    absorb(child.args)
    node = child
  }
  return [...merged.values()]
}

// Split a saved-run editor's declared-arg state into what gets persisted:
// fixed `args` (typed values, baked in), generated `params` entries (Open args,
// incl. password args which are ALWAYS open — the synced file never stores a
// secret), and validation errors (a required arg must be either fixed or Open;
// a fixed numeric value must sit inside the arg's range). Pure — exported for
// unit tests and used by the saved-run editor on save.
export function splitFixedAndOpen(
  declared: ArgDef[],
  values: Record<string, unknown>,
  openArgs: Set<string>,
): {
  args: Record<string, unknown>
  params: Record<string, unknown>
  errors: Record<string, string>
} {
  const args: Record<string, unknown> = {}
  const params: Record<string, unknown> = {}
  const errors: Record<string, string> = {}
  for (const a of declared) {
    const open = openArgs.has(a.name) || a.type === 'password'
    if (open) {
      params[a.name] = argDefToParamDef(a)
      continue
    }
    const v = values[a.name]
    if (v === undefined || v === '') {
      if (a.required) errors[a.name] = 'Required — set a value or mark it Open'
      continue
    }
    if (a.range && (a.type === 'int' || a.type === 'float')) {
      const n = Number(v)
      const [lo, hi] = a.range
      if (n < lo || n > hi) {
        errors[a.name] = `Must be between ${lo} and ${hi}`
        continue
      }
    }
    args[a.name] = v
  }
  return { args, params, errors }
}

// One rich arg row — mirrors the Forms tab's ArgRow: monospace label + tags
// (type / required / optional / description), the rich `children` input, and an
// inline error message. `extra` renders trailing on the header row (the saved-run
// editor's Fixed/Open toggle).
export function ArgFieldRow({ arg, error, extra, children }: {
  arg: ArgDef
  error?: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
        <Text code style={{ fontSize: 13 }}>--{arg.name}</Text>
        <Tag style={{ fontSize: 11, margin: 0 }}>
          {(arg.ui === 'date' || arg.ui === 'datetime' || arg.ui === 'time' || arg.ui === 'month' || arg.ui === 'color' || arg.ui === 'host') ? arg.ui : arg.type}
        </Tag>
        {arg.required
          ? <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
          : <Text type="secondary" style={{ fontSize: 11 }}>optional</Text>
        }
        {extra && <span style={{ marginLeft: 'auto' }}>{extra}</span>}
      </div>
      {/* Description on its own full-width line, truncated with a hover tooltip
          (matches the Forms tab) so a long description doesn't blow up the row. */}
      {arg.description && (
        <TruncatedText text={arg.description} style={{ fontSize: 12, marginBottom: 5, color: 'var(--rs-text-secondary)' }} />
      )}
      {children}
      {error && <Text style={{ fontSize: 12, color: '#ff4d4f' }}>{error}</Text>}
    </div>
  )
}

// The runnable-args section shared by the Schedules modal and the saved-run
// editor. Uses the Forms tab's `ArgInput` verbatim (flag→Switch, choice→Select,
// int/float→InputNumber, path→PathInput, date/datetime/time/month/color→pickers,
// password→masked), and matches the Forms layout — defaulted args fold into a
// collapsed "Default arguments" section near the top; the rest lead the form.
//
// When `onToggleOpen` is provided (the saved-run editor), each row gains a small
// Fixed/Open switch: an Open arg is asked at run time (its value input renders
// disabled; the caller records it in the saved run's params table). A
// password-typed arg is FORCED Open — the synced saved-runs file must never
// store a secret value. The Schedules modal passes neither prop → unchanged.
export function RunnableArgsBlock({
  runnable, values, errors, onChange, labelStyle, openArgs, onToggleOpen,
}: {
  runnable: Runnable
  values: Record<string, unknown>
  errors: Record<string, string>
  onChange: (name: string, v: unknown) => void
  labelStyle: CSSProperties
  openArgs?: Set<string>
  onToggleOpen?: (name: string, open: boolean) => void
}) {
  const args = runnable.args ?? []
  const { visible, defaulted } = partitionArgs(args)
  const row = (a: ArgDef) => {
    const isPassword = a.type === 'password'
    const forcedOpen = !!onToggleOpen && isPassword
    const isOpen = forcedOpen || !!openArgs?.has(a.name)
    return (
      <ArgFieldRow
        key={a.name}
        arg={a}
        error={errors[a.name]}
        extra={onToggleOpen && (
          <Tooltip title={forcedOpen
            ? 'Password args are always asked at run time — the synced saved-run file never stores a secret.'
            : isOpen ? 'Asked at run time (an open parameter)' : 'Baked into the saved run'}>
            <Space size={4}>
              <Text type="secondary" style={{ fontSize: 11 }}>open</Text>
              <Switch
                size="small"
                checked={isOpen}
                disabled={forcedOpen}
                onChange={v => onToggleOpen(a.name, v)}
              />
            </Space>
          </Tooltip>
        )}
      >
        {isOpen ? (
          <Text type="secondary" style={{ fontSize: 12, fontStyle: 'italic' }}>
            Asked at run time{a.required ? ' (required)' : ''}.
          </Text>
        ) : (
          <ArgInput
            arg={a}
            host={runnable.host}
            runnable={runnable.name}
            runAs={runnable.runAs}
            group={runnable.group}
            value={values[a.name]}
            error={errors[a.name]}
            onChange={v => onChange(a.name, v)}
          />
        )}
      </ArgFieldRow>
    )
  }
  return (
    <>
      <div style={{ ...labelStyle, marginBottom: 8, marginTop: 4 }}>Arguments</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {defaulted.length > 0 && (
          <Collapse
            size="small"
            ghost
            style={{ marginLeft: -8, marginRight: -8 }}
            items={[{
              key: 'defaults',
              label: <Text type="secondary" style={{ fontSize: 12 }}>Default arguments ({defaulted.length})</Text>,
              children: <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>{defaulted.map(row)}</div>,
            }]}
          />
        )}
        {visible.map(row)}
      </div>
    </>
  )
}

// The credential picker shared by the Schedules modal and the saved-run editor.
// Mirrors the Forms run dialog: a multi-select of saved credentials, and a
// per-pick preview of the env vars it sets (name-derived, e.g.
// JIRA_USERNAME/JIRA_PASSWORD) plus any declared args it binds. Only ids are
// stored; the secret stays in the keychain and is injected at run time.
export function CredentialsSection({
  creds, selected, onChange, declaredArgNames, labelStyle,
}: {
  creds: Credential[]
  selected: string[]
  onChange: (ids: string[]) => void
  declaredArgNames: Set<string>
  labelStyle: CSSProperties
}) {
  const selectedCreds = selected
    .map(id => creds.find(c => c.id === id))
    .filter((c): c is Credential => !!c)
  const bindings = bindingsForArgs(
    selectedCreds.map(c => ({
      id: c.id, label: c.label, kind: c.kind, has_secret: !!c.has_secret,
      username: c.username ?? '', username_arg: c.username_arg ?? '', secret_arg: c.secret_arg ?? '',
    })),
    declaredArgNames,
  )
  return (
    <div style={{ marginTop: 14, borderTop: '1px solid rgba(128,128,128,0.2)', paddingTop: 12 }}>
      <Text style={{ fontSize: 13, display: 'block', marginBottom: 2 }}>Credentials</Text>
      <Text type="secondary" style={{ ...labelStyle, display: 'block', marginBottom: 6 }}>
        Applied when it runs — as env vars named after the credential
        (a credential <Text code style={{ fontSize: 11 }}>Jira</Text> sets{' '}
        <Text code style={{ fontSize: 11 }}>JIRA_USERNAME</Text>/<Text code style={{ fontSize: 11 }}>JIRA_PASSWORD</Text>),
        and into any argument it binds. The secret stays in the keychain; only the
        credential id is saved.
      </Text>
      <Select
        size="small"
        mode="multiple"
        value={selected}
        onChange={vs => onChange(vs as string[])}
        placeholder="Select credentials to apply…"
        style={{ width: '100%' }}
        options={creds.map(c => ({ label: c.label, value: c.id }))}
      />
      {selected.length > 0 && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 3 }}>
          {selectedCreds.map(c => {
            const names = credEnvNames(c.label, c.kind)
            const bound = Object.entries(bindings)
              .filter(([, b]) => b.label === c.label)
              .map(([argName]) => `--${argName}`)
            return (
              <Text key={c.id} type="secondary" style={{ fontSize: 12 }}>
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
  )
}

// The targeting section shared by the Schedules modal and the saved-run editor:
// a Target segmented control + the matching antd Form.Item selector. Both
// dialogs use the same form field names (host / hosts / selected_hosts / group),
// so the caller only supplies the fleet, the current target, and its change
// handlers. Host options show a live connectivity dot; groups and profiles show
// connected/member counts. Passing `profileNames` adds the "Host profile" mode
// (field name `profile`) — the saved-run editor does; schedules don't.
export function TargetFormItems({
  hosts, groupNames, profileNames, target, onTargetChange, onHostChange, labelStyle,
}: {
  hosts: Host[]
  groupNames: string[]
  profileNames?: string[]
  target: Target
  onTargetChange: (t: Target) => void
  onHostChange: () => void
  labelStyle: CSSProperties
}) {
  const hostOption = (h: Host) => (
    <Select.Option key={h.name} value={h.name}>
      <Space size={6}>
        <span style={{ fontSize: 9, color: h.connected ? '#52c41a' : '#595959' }}>●</span>
        {h.name}
      </Space>
    </Select.Option>
  )
  return (
    <>
      <Form.Item label="Target">
        <Segmented
          value={target}
          onChange={v => onTargetChange(v as Target)}
          options={[
            { label: 'One host', value: 'host' },
            { label: 'Any of hosts', value: 'anyhost' },
            { label: 'Selected hosts', value: 'selected' },
            { label: 'Host group', value: 'group', disabled: groupNames.length === 0 },
            ...(profileNames
              ? [{ label: 'Host profile', value: 'profile', disabled: profileNames.length === 0 }]
              : []),
          ]}
        />
        <div style={{ ...labelStyle, marginTop: 4 }}>
          {target === 'group'
            ? 'Runs the runnable on every host in the group (one run card per host).'
            : target === 'profile'
            ? 'Runs the runnable on every host in the profile at run time (one run card per host).'
            : target === 'anyhost'
            ? 'Runs on the first connected host from the list (failover) — one run.'
            : target === 'selected'
            ? 'Runs the runnable on every host you tick (one run card per host).'
            : 'Runs the runnable on a single host.'}
        </div>
      </Form.Item>

      {target === 'host' ? (
        <Form.Item name="host" label="Host" rules={[{ required: true }]}>
          <Select onChange={onHostChange}>
            {hosts.map(hostOption)}
          </Select>
        </Form.Item>
      ) : target === 'anyhost' ? (
        <Form.Item name="hosts" label="Hosts (any one — first connected)"
          rules={[{ required: true, message: 'Pick at least one host' }]}
          help="When it runs, the first connected host in this list is used.">
          <Select mode="multiple" onChange={onHostChange} placeholder="Pick candidate hosts"
            optionFilterProp="value">
            {hosts.map(hostOption)}
          </Select>
        </Form.Item>
      ) : target === 'selected' ? (
        <Form.Item name="selected_hosts" label="Hosts (runs on every one)"
          rules={[{ required: true, message: 'Pick at least one host' }]}
          help="When it runs, every host you tick gets its own run.">
          <Select mode="multiple" onChange={onHostChange} placeholder="Pick hosts to run on"
            optionFilterProp="value">
            {hosts.map(hostOption)}
          </Select>
        </Form.Item>
      ) : target === 'profile' ? (
        <Form.Item name="profile" label="Profile" rules={[{ required: true }]}>
          <Select onChange={onHostChange} placeholder="Select a host profile">
            {(profileNames ?? []).map(p => {
              const members = hosts.filter(h => hostInProfile(h, p))
              const up = members.filter(h => h.connected).length
              return (
                <Select.Option key={p} value={p}>
                  <Space size={6}>
                    {p}
                    <span style={{ ...labelStyle }}>{up}/{members.length} connected</span>
                  </Space>
                </Select.Option>
              )
            })}
          </Select>
        </Form.Item>
      ) : (
        <Form.Item name="group" label="Group" rules={[{ required: true }]}>
          <Select onChange={onHostChange} placeholder="Select a host group">
            {groupNames.map(g => {
              const members = hosts.filter(h => h.group === g)
              const up = members.filter(h => h.connected).length
              return (
                <Select.Option key={g} value={g}>
                  <Space size={6}>
                    {g}
                    <span style={{ ...labelStyle }}>{up}/{members.length} connected</span>
                  </Space>
                </Select.Option>
              )
            })}
          </Select>
        </Form.Item>
      )}
    </>
  )
}

// The runnable picker's option list, shared by both dialogs — data for the
// Select's `options` prop. It MUST be data, not a component rendering
// `<Select.Option>` children: antd's Select discovers options by inspecting
// its direct children elements and never renders a custom component to find
// them, so a component-in-children produced an EMPTY dropdown (the 0.191.0
// regression). `fallbackName` (the saved-run editor) keeps a stored runnable
// selectable even when it isn't currently discovered (fleet offline / host
// removed), so editing never loses it. Filter on `value` (the callers pass a
// value-matching filterOption; the label is JSX).
export function runnableSelectOptions(
  runnables: Runnable[],
  labelStyle: CSSProperties,
  fallbackName?: string,
): { value: string; label: ReactNode }[] {
  const out: { value: string; label: ReactNode }[] = []
  if (fallbackName && !runnables.some(r => r.name === fallbackName)) {
    out.push({
      value: fallbackName,
      label: (
        <>
          <code>/{fallbackName}</code>
          <span style={{ ...labelStyle, marginLeft: 8 }}>not currently discovered</span>
        </>
      ),
    })
  }
  for (const r of runnables) {
    out.push({
      value: r.name,
      label: (
        <>
          <code>/{r.name}</code>
          {r.description && <span style={{ ...labelStyle, marginLeft: 8 }}>{r.description}</span>}
        </>
      ),
    })
  }
  return out
}
