import { useState } from 'react'
import {
  Form, Modal, Input, Select, AutoComplete, Segmented, Switch, Button, Tag,
  Empty, Popconfirm, Typography, Tooltip, Alert, message,
} from 'antd'
import {
  PlusOutlined, MinusCircleOutlined, EditOutlined, DeleteOutlined,
  ThunderboltOutlined, CheckOutlined, CloseOutlined, PlayCircleOutlined,
} from '@ant-design/icons'
import { bridge, type TriggerSourceInfo } from '../bridge'

const { Text } = Typography

// ── Triggers editor (runspec_triggers.toml) ─────────────────────────────────
// Moved here from Settings → Triggers so trigger editing lives on the Triggers
// tab alongside the run inbox. Data (triggers/sources) + reload are owned by the
// Triggers tab and passed in, so the tab's header counts and source-availability
// alerts stay in sync with edits.

const AUTONOMY_OPTS = ['confirm', 'autonomous', 'supervised', 'manual']

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
  reply_draft: boolean
  reply_style?: string
}

const triggerToForm = (t: Record<string, unknown>): TriggerForm => {
  const reply = (t.reply ?? {}) as { draft?: boolean; style?: string }
  const match = (t.match ?? {}) as Record<string, unknown>
  const map = (t.map ?? {}) as Record<string, unknown>
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
    reply_draft: !!reply.draft,
    reply_style: reply.style ? String(reply.style) : '',
  }
}

const formToTrigger = (v: TriggerForm): Record<string, unknown> => {
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
    const mapObj: Record<string, string> = {}
    for (const m of v.map || []) if (m?.arg?.trim()) mapObj[m.arg.trim()] = (m.template ?? '').trim()
    base.run = (v.run ?? '').trim()
    base.map = mapObj
  } else {
    base.rules = (v.rules ?? '').trim()
    base.runnables = (v.runnables ?? []).map(s => s.trim()).filter(Boolean)
  }
  return base
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
  const eventOpts = (sources.find(s => s.id === source)?.event_types ?? []).map(e => ({ value: e }))

  const startAdd = () => {
    setEditName(null); setErr('')
    form.resetFields()
    form.setFieldsValue({ mode: 'agent', autonomy: 'confirm', enabled: true, match: [], once_per_thread: 'default', runnables: [], map: [], reply_draft: false })
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

          <Form.Item name="once_per_thread" label="Replies" style={{ marginBottom: 10 }}
            help="Fire on the first message in a conversation, then ignore replies in that thread (keyed on the email/chat thread id, survives restarts). Default follows the mailbox setting.">
            <Segmented options={[{ label: 'Mailbox default', value: 'default' }, { label: 'Once per thread', value: 'on' }, { label: 'Every message', value: 'off' }]} />
          </Form.Item>

          {mode === 'rule' ? (
            <>
              <Form.Item name="run" label="Run" rules={[{ required: true, whitespace: true, message: 'Required for rule mode' }]} help="host__runnable — the runnable to run.">
                <Input placeholder="web01__reset-password" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <label style={{ fontSize: 12, color: '#888', display: 'block', marginBottom: 4 }}>{'Arg map — runnable arg → {{ payload field }}'}</label>
              {pairRow('map', 'arg', 'template', 'user', '{{ from }}', 'Add arg')}
            </>
          ) : (
            <>
              <Form.Item name="rules" label="Agent instructions" help="The per-trigger system prompt the LLM follows.">
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="Triage the email and draft a reset link…" />
              </Form.Item>
              <Form.Item name="runnables" label="Runnable allowlist (optional)" help="Tools the agent may call. Empty = any runnable.">
                <Select mode="tags" tokenSeparators={[',', ' ']} open={false} suffixIcon={null} placeholder="reset-password, request-vm" />
              </Form.Item>
            </>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px', alignItems: 'end' }}>
            <Form.Item name="reply_draft" label="Draft a reply" valuePropName="checked" help="Offer a draft_reply tool (email triggers).">
              <Switch size="small" />
            </Form.Item>
            <Form.Item name="reply_style" label="Reply style (optional)">
              <Input placeholder="formal, concise…" />
            </Form.Item>
          </div>

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
