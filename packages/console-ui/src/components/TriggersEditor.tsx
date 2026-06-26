import { useEffect, useState } from 'react'
import {
  Form, Modal, Input, Select, AutoComplete, Segmented, Switch, Button, Tag,
  Empty, Popconfirm, Typography, Tooltip, Alert, message,
} from 'antd'
import {
  PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined,
  ThunderboltOutlined, CheckOutlined, CloseOutlined, PlayCircleOutlined,
} from '@ant-design/icons'
import { bridge, type TriggerSourceInfo, type Host, type Runnable, type ArgDef } from '../bridge'

const { Text } = Typography

// ── Triggers editor (runspec_triggers.toml) ─────────────────────────────────
// Moved here from Settings → Triggers so trigger editing lives on the Triggers
// tab alongside the run inbox. Data (triggers/sources) + reload are owned by the
// Triggers tab and passed in, so the tab's header counts and source-availability
// alerts stay in sync with edits.

const AUTONOMY_OPTS = ['confirm', 'autonomous', 'supervised', 'manual']

interface ActionRow {
  run?: string
  map: { arg: string; template: string }[]
}

interface TriggerForm {
  name: string
  source?: string
  event?: string
  mode: 'agent' | 'rule'
  autonomy: string
  enabled: boolean
  match: { field: string; pattern: string }[]
  // Tri-state override of the mailbox's reply-suppression default.
  once_per_thread: 'default' | 'on' | 'off'
  rules?: string
  runnables?: string[]
  run?: string
  map: { arg: string; template: string }[]
  // Rule mode: single runnable vs a declarative list (UI-only switch, derived on
  // load from whether `actions` is present).
  rule_kind: 'single' | 'list'
  action_mode: 'sequential' | 'parallel'
  actions: ActionRow[]
  // Optional LLM pre-turn (rule mode) that fills the action args.
  pre_turn?: string
  // Agent mode: which built-in action/Outlook tools the trigger may use.
  action_allow?: string[]
  reply_draft: boolean
  reply_style?: string
}

export const triggerToForm = (t: Record<string, unknown>): TriggerForm => {
  const reply = (t.reply ?? {}) as { draft?: boolean; style?: string }
  const match = (t.match ?? {}) as Record<string, unknown>
  const map = (t.map ?? {}) as Record<string, unknown>
  const actionsRaw = Array.isArray(t.actions) ? (t.actions as Record<string, unknown>[]) : []
  return {
    name: String(t.name ?? ''),
    source: t.source ? String(t.source) : undefined,
    event: t.event ? String(t.event) : undefined,
    mode: t.mode === 'rule' ? 'rule' : 'agent',
    autonomy: t.autonomy ? String(t.autonomy) : 'confirm',
    enabled: t.enabled !== false,
    match: Object.entries(match).map(([field, pattern]) => ({ field, pattern: String(pattern) })),
    // Absent → inherit the mailbox default; an explicit bool → on/off override.
    once_per_thread: typeof t.once_per_thread === 'boolean' ? (t.once_per_thread ? 'on' : 'off') : 'default',
    rules: t.rules ? String(t.rules) : '',
    runnables: Array.isArray(t.runnables) ? (t.runnables as unknown[]).map(String) : [],
    run: t.run ? String(t.run) : '',
    map: Object.entries(map).map(([arg, template]) => ({ arg, template: String(template) })),
    rule_kind: actionsRaw.length ? 'list' : 'single',
    action_mode: t.action_mode === 'parallel' ? 'parallel' : 'sequential',
    actions: actionsRaw.map(a => ({
      run: a.run ? String(a.run) : '',
      map: Object.entries((a.map ?? {}) as Record<string, unknown>).map(([arg, template]) => ({ arg, template: String(template) })),
    })),
    pre_turn: t.pre_turn ? String(t.pre_turn) : '',
    action_allow: Array.isArray(t.action_allow) ? (t.action_allow as unknown[]).map(String) : [],
    reply_draft: !!reply.draft,
    reply_style: reply.style ? String(reply.style) : '',
  }
}

export const formToTrigger = (v: TriggerForm): Record<string, unknown> => {
  const matchObj: Record<string, string> = {}
  for (const m of v.match || []) if (m?.field?.trim()) matchObj[m.field.trim()] = (m.pattern ?? '').trim()
  const base: Record<string, unknown> = {
    name: (v.name ?? '').trim(),
    source: v.source,
    event: v.event,
    mode: v.mode,
    autonomy: v.autonomy,
    enabled: v.enabled,
    match: matchObj,
    reply: { draft: !!v.reply_draft, style: (v.reply_style ?? '').trim() },
  }
  // Only an explicit override crosses the bridge; 'default' omits the key so the
  // trigger inherits the mailbox default.
  if (v.once_per_thread === 'on') base.once_per_thread = true
  else if (v.once_per_thread === 'off') base.once_per_thread = false
  if (v.mode === 'rule') {
    base.pre_turn = (v.pre_turn ?? '').trim()
    if (v.rule_kind === 'list') {
      base.action_mode = v.action_mode || 'sequential'
      base.actions = (v.actions || [])
        .filter(a => a?.run?.trim())
        .map(a => {
          const m: Record<string, string> = {}
          for (const p of a.map || []) if (p?.arg?.trim()) m[p.arg.trim()] = (p.template ?? '').trim()
          return { run: (a.run ?? '').trim(), map: m }
        })
    } else {
      const mapObj: Record<string, string> = {}
      for (const m of v.map || []) if (m?.arg?.trim()) mapObj[m.arg.trim()] = (m.template ?? '').trim()
      base.run = (v.run ?? '').trim()
      base.map = mapObj
    }
  } else {
    base.rules = (v.rules ?? '').trim()
    base.runnables = (v.runnables ?? []).map(s => s.trim()).filter(Boolean)
    base.action_allow = (v.action_allow ?? []).map(s => s.trim()).filter(Boolean)
  }
  return base
}

// ── Run-target picker + arg editor (rule mode) ──────────────────────────────
// A rule `run` target is a string: "host__runnable" or "group:<group>__runnable".
// parseRun splits it into pieces for the picker; composeRun is the inverse and
// returns "" until both a target and a runnable are chosen (so the required-field
// rule fires on a half-made selection).

type RunKind = 'host' | 'group'

export function parseRun(run: string): { kind: RunKind; target: string; runnable: string } {
  const s = (run || '').trim()
  if (s.startsWith('group:')) {
    const rest = s.slice('group:'.length)
    const i = rest.indexOf('__')
    return i < 0
      ? { kind: 'group', target: rest, runnable: '' }
      : { kind: 'group', target: rest.slice(0, i), runnable: rest.slice(i + 2) }
  }
  const i = s.indexOf('__')
  return i < 0
    ? { kind: 'host', target: s, runnable: '' }
    : { kind: 'host', target: s.slice(0, i), runnable: s.slice(i + 2) }
}

export function composeRun(kind: RunKind, target: string, runnable: string): string {
  if (!target || !runnable) return ''
  return kind === 'group' ? `group:${target}__${runnable}` : `${target}__${runnable}`
}

// The declared args of the runnable a `run` target names (top-level args; for a
// group target any member carries the same spec). [] when nothing is resolvable.
export function argsForRun(run: string | undefined, runnables: Runnable[]): ArgDef[] {
  const p = parseRun(run || '')
  if (!p.runnable) return []
  const r = runnables.find(
    x => x.name === p.runnable && (p.kind === 'group' || x.host === p.target),
  )
  return r?.args ?? []
}

// A controlled form input for a rule `run` target: a Host/Group toggle, a target
// selector, and a runnable selector filtered to that target. Holds the parsed
// pieces locally so picking a host before a runnable doesn't wipe the selection,
// and re-seeds only when an externally-set value (opening another trigger) differs.
function RunPicker({
  value, onChange, hosts, runnables,
}: {
  value?: string
  onChange?: (v: string) => void
  hosts: Host[]
  runnables: Runnable[]
}) {
  const p = parseRun(value || '')
  const [kind, setKind] = useState<RunKind>(p.kind)
  const [target, setTarget] = useState(p.target)
  const [runnable, setRunnable] = useState(p.runnable)

  useEffect(() => {
    if ((value || '') !== composeRun(kind, target, runnable)) {
      const q = parseRun(value || '')
      setKind(q.kind); setTarget(q.target); setRunnable(q.runnable)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  const groups = [...new Set(hosts.map(h => h.group).filter((g): g is string => !!g))].sort()
  const runnablesFor = (k: RunKind, t: string): Runnable[] => {
    if (!t) return []
    if (k === 'group') {
      const members = new Set(hosts.filter(h => h.group === t).map(h => h.name))
      return [...new Map(runnables.filter(r => members.has(r.host)).map(r => [r.name, r])).values()]
    }
    return runnables.filter(r => r.host === t)
  }

  const emit = (k: RunKind, t: string, r: string) => onChange?.(composeRun(k, t, r))
  const onKind = (k: RunKind) => { setKind(k); setTarget(''); setRunnable(''); emit(k, '', '') }
  const onTarget = (t: string) => {
    setTarget(t)
    const keep = runnablesFor(kind, t).some(r => r.name === runnable) ? runnable : ''
    setRunnable(keep); emit(kind, t, keep)
  }
  const onRunnable = (r: string) => { setRunnable(r); emit(kind, target, r) }

  // Always include the current selection as an option, even if the host/runnable
  // is offline or undiscovered, so editing an existing trigger never loses it.
  const withCurrent = (opts: string[], cur: string) =>
    (cur && !opts.includes(cur) ? [cur, ...opts] : opts)
  const targetNames = withCurrent(kind === 'group' ? groups : hosts.map(h => h.name), target)
  const runnableNames = withCurrent(runnablesFor(kind, target).map(r => r.name), runnable)
  const dot = (name: string) => {
    const up = hosts.find(h => h.name === name)?.connected
    return <span style={{ fontSize: 9, color: up ? '#52c41a' : '#595959', marginRight: 6 }}>●</span>
  }

  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <Segmented
        size="small" value={kind} onChange={v => onKind(v as RunKind)}
        options={[{ label: 'Host', value: 'host' }, { label: 'Group', value: 'group', disabled: groups.length === 0 }]}
      />
      <Select
        size="small" showSearch style={{ minWidth: 150 }} placeholder={kind === 'group' ? 'group' : 'host'}
        value={target || undefined} onChange={onTarget}
        options={targetNames.map(t => ({ value: t, label: kind === 'host' ? <span>{dot(t)}{t}</span> : t }))}
      />
      <span style={{ color: '#666' }}>/</span>
      <Select
        size="small" showSearch style={{ minWidth: 170 }} placeholder="runnable" disabled={!target}
        value={runnable || undefined} onChange={onRunnable}
        options={runnableNames.map(r => ({ value: r, label: <code>/{r}</code> }))}
      />
    </div>
  )
}

// A controlled form input for a rule arg map ({arg, template}[]). The chosen
// runnable's declared args auto-list as labelled rows (required flagged, type/
// options shown as placeholder); each value is free text so it can be a literal
// OR a {{ payload field }} / {{ _llm.x }} template. Any map entry whose arg isn't
// in the spec stays as an editable "custom" row, and an Add custom arg button
// covers anything extra.
function ArgMapEditor({
  value, onChange, declaredArgs,
}: {
  value?: { arg: string; template: string }[]
  onChange?: (v: { arg: string; template: string }[]) => void
  declaredArgs: ArgDef[]
}) {
  const rows = value || []
  const declaredNames = new Set(declaredArgs.map(a => a.name))
  const declaredEntries = rows.filter(r => declaredNames.has(r.arg))
  const customRows = rows.filter(r => !declaredNames.has(r.arg))
  const templateOf = (name: string) => rows.find(r => r.arg === name)?.template ?? ''

  const setDeclared = (name: string, template: string) => {
    const others = rows.filter(r => r.arg !== name)
    onChange?.(template ? [...others, { arg: name, template }] : others)
  }
  const commitCustom = (next: { arg: string; template: string }[]) =>
    onChange?.([...declaredEntries, ...next])
  const setCustom = (i: number, field: 'arg' | 'template', val: string) => {
    const next = customRows.map((r, j) => (j === i ? { ...r, [field]: val } : r))
    commitCustom(next)
  }
  const addCustom = () => onChange?.([...rows, { arg: '', template: '' }])
  const removeCustom = (i: number) => commitCustom(customRows.filter((_, j) => j !== i))

  const hintFor = (a: ArgDef): string =>
    a.options?.length ? `one of ${a.options.join(' | ')}  (or {{ field }})`
      : a.type === 'flag' || a.type === 'bool' ? 'true / false  (or {{ field }})'
      : a.default !== undefined ? `default ${String(a.default)}  (or {{ field }})`
      : '{{ field }}'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {declaredArgs.map(a => (
        <div key={a.name} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 110, textAlign: 'right', color: '#aaa' }}>
            {a.name}{a.required ? <span style={{ color: '#cf5b5b' }}> *</span> : ''}
          </span>
          <span style={{ color: '#666' }}>→</span>
          <Input
            size="small" style={{ fontFamily: 'monospace' }} placeholder={hintFor(a)}
            value={templateOf(a.name)} onChange={e => setDeclared(a.name, e.target.value)}
          />
        </div>
      ))}
      {customRows.map((r, i) => (
        <div key={`c${i}`} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <Input
            size="small" style={{ fontFamily: 'monospace', minWidth: 110, maxWidth: 116 }} placeholder="arg"
            value={r.arg} onChange={e => setCustom(i, 'arg', e.target.value)}
          />
          <span style={{ color: '#666' }}>→</span>
          <Input
            size="small" style={{ fontFamily: 'monospace' }} placeholder="{{ field }}"
            value={r.template} onChange={e => setCustom(i, 'template', e.target.value)}
          />
          <MinusCircleOutlined onClick={() => removeCustom(i)} style={{ color: '#666', cursor: 'pointer' }} />
        </div>
      ))}
      <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addCustom} style={{ width: 'fit-content' }}>
        Add custom arg
      </Button>
    </div>
  )
}

// "Trigger now" — fire a trigger immediately with a synthetic event so it can be
// tested without waiting for a real one. A pasted message block (→ the `body`
// payload key) plus optional key=value fields the trigger's match/rules can use.
interface TestForm {
  body: string
  fields: { key: string; value: string }[]
}

function TestTriggerModal({ name, onClose }: { name: string | null; onClose: () => void }) {
  const [form] = Form.useForm<TestForm>()
  const [firing, setFiring] = useState(false)

  const fire = async () => {
    const v = await form.validateFields()
    const fields: Record<string, string> = {}
    for (const f of v.fields || []) if (f?.key?.trim()) fields[f.key.trim()] = (f.value ?? '').trim()
    setFiring(true)
    try {
      const res = await bridge.test_trigger(name as string, v.body || '', fields)
      if (res.ok) {
        message.success(`Fired ${name} — see the run inbox below`)
        onClose()
      } else {
        message.error(res.error || 'Could not fire the trigger.')
      }
    } finally {
      setFiring(false)
    }
  }

  return (
    <Modal
      open={name !== null}
      title={`Trigger now — ${name ?? ''}`}
      width={560}
      onCancel={onClose}
      onOk={fire}
      okText="Fire"
      confirmLoading={firing}
      okButtonProps={{ icon: <PlayCircleOutlined /> }}
      destroyOnClose
    >
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        Fires this trigger immediately with a synthetic event — bypassing the match
        rules and the working-hours rota — so you can test it without waiting for a
        real event. The message lands under the <code>body</code> field.
      </Text>
      <Form form={form} layout="vertical" size="small" initialValues={{ body: '', fields: [] }}>
        <Form.Item name="body" label="Message">
          <Input.TextArea
            autoSize={{ minRows: 3, maxRows: 10 }}
            placeholder="Paste the message body the trigger should act on…"
          />
        </Form.Item>
        <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>
          Fields (optional) — extra payload, e.g. <code>subject</code> = <code>VIP outage</code>
        </label>
        <Form.List name="fields">
          {(fields, { add, remove }) => (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {fields.map(f => (
                <div key={f.key} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <Form.Item name={[f.name, 'key']} noStyle><Input size="small" placeholder="subject" style={{ fontFamily: 'monospace' }} /></Form.Item>
                  <span style={{ color: '#666' }}>=</span>
                  <Form.Item name={[f.name, 'value']} noStyle><Input size="small" placeholder="VIP outage" style={{ fontFamily: 'monospace' }} /></Form.Item>
                  <MinusCircleOutlined onClick={() => remove(f.name)} style={{ color: '#666', cursor: 'pointer' }} />
                </div>
              ))}
              <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add()} style={{ width: 'fit-content' }}>Add field</Button>
            </div>
          )}
        </Form.List>
      </Form>
    </Modal>
  )
}

export function TriggersEditor({
  triggers,
  sources,
  reload,
}: {
  triggers: Record<string, unknown>[]
  sources: TriggerSourceInfo[]
  reload: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState<string | null>(null) // null = adding new
  const [testName, setTestName] = useState<string | null>(null) // "Trigger now" target
  const [form] = Form.useForm<TriggerForm>()
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const mode = Form.useWatch('mode', form)
  const source = Form.useWatch('source', form)
  const ruleKind = Form.useWatch('rule_kind', form)
  // The single-run target + the action-list rows, watched so the arg editor can
  // resolve the chosen runnable's declared args live.
  const runVal = Form.useWatch('run', form)
  const actionsVal = Form.useWatch('actions', form)
  const eventOpts = (sources.find(s => s.id === source)?.event_types ?? []).map(e => ({ value: e }))
  // Reply drafting + once-per-thread only make sense for message-bearing sources;
  // a schedule tick has nothing to reply to and no thread.
  const threaded = source === 'outlook' || source === 'chat' || source === 'email'
  const isChat = source === 'chat'

  // The built-in action tools the agent allowlist offers (Outlook entries appear
  // only when Outlook COM is reachable on the host).
  const [actionTools, setActionTools] = useState<{ name: string; label: string }[]>([])
  useEffect(() => {
    bridge.list_action_tools().then(setActionTools).catch(() => setActionTools([]))
  }, [])

  // Hosts + every discovered runnable, for the rule-mode Run picker / arg editor.
  // Runnables arrive asynchronously (SSH discovery finishes after the host list is
  // known), so refresh on the same events App does — otherwise the runnable picker
  // is empty whenever the editor mounted before discovery completed.
  const [hosts, setHosts] = useState<Host[]>([])
  const [runnables, setRunnables] = useState<Runnable[]>([])
  useEffect(() => {
    const loadHosts = () => bridge.get_hosts().then(setHosts).catch(() => {})
    const loadRunnables = () => bridge.get_runnables('all').then(setRunnables).catch(() => {})
    loadHosts()
    loadRunnables()
    window.addEventListener('runspec:hosts_updated', loadHosts)
    window.addEventListener('runspec:runnables_updated', loadRunnables)
    return () => {
      window.removeEventListener('runspec:hosts_updated', loadHosts)
      window.removeEventListener('runspec:runnables_updated', loadRunnables)
    }
  }, [])

  const startAdd = () => {
    setEditName(null); setErr('')
    form.resetFields()
    form.setFieldsValue({
      mode: 'agent', autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default',
      runnables: [], map: [], reply_draft: false,
      rule_kind: 'single', action_mode: 'sequential', actions: [], action_allow: [], pre_turn: '',
    })
    setEditing(true)
  }
  const startEdit = (t: Record<string, unknown>) => {
    setEditName(String(t.name)); setErr('')
    form.setFieldsValue(triggerToForm(t))
    setEditing(true)
  }
  const cancel = () => { setEditing(false); setErr(''); form.resetFields() }

  const save = async () => {
    let v: TriggerForm
    try { v = await form.validateFields() } catch { return }
    setSaving(true)
    try {
      const res = await bridge.save_trigger(formToTrigger(v))
      if (!res.ok) { setErr(res.error || 'Could not save the trigger.'); return }
      setEditing(false); setErr(''); form.resetFields()
      reload()
    } finally { setSaving(false) }
  }

  const toggle = async (name: string, enabled: boolean) => { await bridge.set_trigger_enabled(name, enabled); reload() }
  const remove = async (name: string) => { await bridge.delete_trigger(name); reload() }

  const pairRow = (listName: 'match' | 'map', keyName: string, valName: string, keyPh: string, valPh: string, addLabel: string) => (
    <Form.List name={listName}>
      {(fields, { add, remove: rm }) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 8 }}>
          {fields.map(f => (
            <div key={f.key} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <Form.Item name={[f.name, keyName]} noStyle><Input size="small" placeholder={keyPh} style={{ fontFamily: 'monospace' }} /></Form.Item>
              <span style={{ color: '#666' }}>→</span>
              <Form.Item name={[f.name, valName]} noStyle><Input size="small" placeholder={valPh} style={{ fontFamily: 'monospace' }} /></Form.Item>
              <MinusCircleOutlined onClick={() => rm(f.name)} style={{ color: '#666', cursor: 'pointer' }} />
            </div>
          ))}
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add()} style={{ width: 'fit-content' }}>{addLabel}</Button>
        </div>
      )}
    </Form.List>
  )

  // The declarative multi-runnable list (rule mode): each row is a runnable plus
  // its own arg map (nested Form.List). Args may reference {{ _llm.* }} from the
  // pre-turn as well as the event payload.
  const actionsList = () => (
    <Form.List name="actions">
      {(fields, { add, remove: rm }) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 8 }}>
          {fields.map(f => (
            <div key={f.key} style={{ border: '1px solid #2a2a2a', borderRadius: 6, padding: 8 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                <Form.Item name={[f.name, 'run']} noStyle rules={[{ required: true, whitespace: true, message: 'Required' }]}>
                  <RunPicker hosts={hosts} runnables={runnables} />
                </Form.Item>
                <MinusCircleOutlined onClick={() => rm(f.name)} style={{ color: '#666', cursor: 'pointer' }} />
              </div>
              <label style={{ fontSize: 11, color: '#888', display: 'block', marginBottom: 4 }}>{'Args — literal or {{ field }} / {{ _llm.x }} template'}</label>
              <Form.Item name={[f.name, 'map']} noStyle>
                <ArgMapEditor declaredArgs={argsForRun(actionsVal?.[f.name]?.run, runnables)} />
              </Form.Item>
            </div>
          ))}
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add({ run: '', map: [] })} style={{ width: 'fit-content' }}>Add runnable</Button>
        </div>
      )}
    </Form.List>
  )

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          React to a monitored event (<code>source:event</code>) — run a runnable (rule) or an agent turn. Saved to <code>runspec_triggers.toml</code>.
        </Text>
        <Button size="small" icon={<PlusOutlined />} onClick={startAdd} disabled={editing}>Add trigger</Button>
      </div>

      <Modal
        open={editing}
        title={editName === null ? 'New trigger' : `Edit — ${editName}`}
        width={680}
        onCancel={cancel}
        onOk={save}
        okText="Save"
        confirmLoading={saving}
        maskClosable={false}
        okButtonProps={{ icon: <CheckOutlined /> }}
        cancelButtonProps={{ icon: <CloseOutlined /> }}
      >
        <Form form={form} layout="vertical" size="small">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
            <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true, message: 'Required' }, { pattern: /^[^\s]+$/, message: 'No spaces' }]}>
              <Input placeholder="password-reset" style={{ fontFamily: 'monospace' }} disabled={editName !== null} />
            </Form.Item>
            <Form.Item name="source" label="Source" rules={[{ required: true, message: 'Required' }]}>
              <Select placeholder="outlook" options={sources.map(s => ({ value: s.id, label: s.available ? s.id : `${s.id} (unavailable)` }))} />
            </Form.Item>
            <Form.Item name="event" label="Event" rules={[{ required: true, whitespace: true, message: 'Required' }]}>
              <AutoComplete options={eventOpts} placeholder="new_mail" />
            </Form.Item>
            <Form.Item name="autonomy" label="Autonomy">
              <Select options={AUTONOMY_OPTS.map(a => ({ value: a }))} />
            </Form.Item>
          </div>

          <Form.Item name="mode" label="Mode">
            <Segmented options={[{ label: 'Agent (LLM turn)', value: 'agent' }, { label: 'Rule (fixed run)', value: 'rule' }]} />
          </Form.Item>

          <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>Match — every field's regex must match the event payload (empty = any event of that source/type)</label>
          {pairRow('match', 'field', 'pattern', 'subject', '(?i)password', 'Add match')}

          {threaded && (
            <Form.Item name="once_per_thread" label="Replies" style={{ marginBottom: 10 }}
              help="Fire on the first message in a conversation, then ignore replies in that thread (keyed on the email/chat thread id, survives restarts). Default follows the mailbox setting.">
              <Segmented options={[{ label: 'Mailbox default', value: 'default' }, { label: 'Once per thread', value: 'on' }, { label: 'Every message', value: 'off' }]} />
            </Form.Item>
          )}

          {mode === 'rule' ? (
            <>
              <Form.Item name="pre_turn" label="LLM pre-turn (optional)"
                help={'A single LLM step that reads the event and fills the args below — reference its output as {{ _llm.<name> }}. It only extracts; it never decides which runnables run.'}>
                <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="Extract the affected hostname and severity from the alert." />
              </Form.Item>
              <Form.Item name="rule_kind" label="Action">
                <Segmented options={[{ label: 'Single runnable', value: 'single' }, { label: 'Multiple runnables', value: 'list' }]} />
              </Form.Item>
              {ruleKind === 'list' ? (
                <>
                  <Form.Item name="action_mode" label="Run order"
                    help="Sequential runs each in turn; parallel runs them at once behind a single grouped confirm.">
                    <Segmented options={[{ label: 'Sequential', value: 'sequential' }, { label: 'Parallel', value: 'parallel' }]} />
                  </Form.Item>
                  {actionsList()}
                </>
              ) : (
                <>
                  <Form.Item name="run" label="Run" rules={[{ required: true, whitespace: true, message: 'Pick a host/group and runnable' }]} help="Runs this runnable when the trigger matches. Group runs it on every host in the group (one grouped confirm).">
                    <RunPicker hosts={hosts} runnables={runnables} />
                  </Form.Item>
                  <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>{'Args — value can be a literal or a {{ payload field }} / {{ _llm.x }} template'}</label>
                  <Form.Item name="map" noStyle>
                    <ArgMapEditor declaredArgs={argsForRun(runVal, runnables)} />
                  </Form.Item>
                </>
              )}
            </>
          ) : (
            <>
              <Form.Item name="rules" label="Agent instructions" help="The per-trigger system prompt the LLM follows.">
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="Triage the email and draft a reset link…" />
              </Form.Item>
              <Form.Item name="runnables" label="Runnable allowlist (optional)" help="Tools the agent may call. Empty = any runnable.">
                <Select mode="tags" tokenSeparators={[',', ' ']} open={false} suffixIcon={null} placeholder="reset-password, request-vm" />
              </Form.Item>
              {actionTools.length > 0 && (
                <Form.Item name="action_allow" label="Action tools (optional)"
                  help="Built-in actions the agent may take (create document, run a runbook, send mail, …). Empty = all available.">
                  <Select mode="multiple" allowClear placeholder="Any available action"
                    options={actionTools.map(a => ({ value: a.name, label: a.label }))} />
                </Form.Item>
              )}
            </>
          )}

          {threaded && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '0 12px' }}>
              <Form.Item name="reply_draft" label={isChat ? 'Propose a reply' : 'Draft a reply'} valuePropName="checked"
                help={isChat ? 'Offer a propose_reply tool — pre-fills the room composer for you to send.' : 'Offer a draft_reply tool — saves a draft for you to review and send (never sent automatically).'}>
                <Switch size="small" />
              </Form.Item>
              <Form.Item name="reply_style" label="Actions on results"
                help="What the agent should do with what the runnables return — e.g. summarise the failures, draft a concise reply, open a ticket if any are critical.">
                <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="Summarise the findings; for mail, draft a concise reply." />
              </Form.Item>
            </div>
          )}

          <Form.Item name="enabled" valuePropName="checked" style={{ marginBottom: 8 }}>
            <Switch size="small" /> <span style={{ fontSize: 12, color: '#888', marginLeft: 6 }}>Enabled</span>
          </Form.Item>

          {err && <Alert type="error" showIcon style={{ marginBottom: 4 }} message={err} />}
        </Form>
      </Modal>

      <TestTriggerModal name={testName} onClose={() => setTestName(null)} />

      {triggers.length === 0 && !editing ? (
        <Empty description={<span style={{ fontSize: 12, color: '#888' }}>No triggers yet. Add one to react to a monitored event.</span>} />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {triggers.map((t, i) => (
            <div key={`${String(t.name)} ${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 6, border: '1px solid #2a2a2a' }}>
              <ThunderboltOutlined style={{ color: '#666', flexShrink: 0 }} />
              <Text style={{ fontFamily: 'monospace', fontSize: 12, flexShrink: 0 }}>{String(t.name)}</Text>
              <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {String(t.source ?? '')}:{String(t.event ?? '')}
              </Text>
              <Tag color={t.mode === 'rule' ? 'geekblue' : 'purple'} style={{ margin: 0, fontSize: 10 }}>{String(t.mode ?? 'agent')}</Tag>
              <Switch size="small" checked={t.enabled !== false} onChange={v => toggle(String(t.name), v)} />
              <Tooltip title="Trigger now — fire with a test event">
                <Button type="text" size="small" icon={<PlayCircleOutlined />} onClick={() => setTestName(String(t.name))} disabled={editing} style={{ padding: '0 4px', color: '#666' }} />
              </Tooltip>
              <Button type="text" size="small" icon={<EditOutlined />} onClick={() => startEdit(t)} disabled={editing} style={{ padding: '0 4px', color: '#666' }} />
              <Popconfirm title={`Delete trigger ${String(t.name)}?`} onConfirm={() => remove(String(t.name))} okText="Delete" cancelText="Cancel" placement="left">
                <Button type="text" size="small" icon={<DeleteOutlined />} disabled={editing} style={{ padding: '0 4px', color: '#666' }} danger />
              </Popconfirm>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
