import { useEffect, useState } from 'react'
import { Table, Tag, Button, Modal, Form, Select, Input, Space, Segmented } from 'antd'
import { DeleteOutlined, PlusOutlined, MinusCircleOutlined } from '@ant-design/icons'
import { bridge, type Host, type Runnable, type Schedule, type ScheduleWhen } from '../bridge'
import { useIsDark } from '../ThemeContext'

interface SchedulesViewProps {
  hosts: Host[]
  runnables: Runnable[]
  selectedHost: string
}

const CRON_PRESETS = [
  { label: 'Every hour',       value: '0 * * * *' },
  { label: 'Daily at midnight',value: '0 0 * * *' },
  { label: 'Daily at 02:00',   value: '0 2 * * *' },
  { label: 'Daily at 06:00',   value: '0 6 * * *' },
  { label: 'Weekly (Sun 03:00)',value: '0 3 * * 0' },
  { label: 'Monthly (1st 04:00)',value: '0 4 1 * *' },
  { label: 'Custom…',          value: '__custom__' },
]

type Action = 'runnable' | 'event'
type WhenKind = 'cron' | 'every'

interface NewScheduleModalProps {
  open: boolean
  hosts: Host[]
  runnables: Runnable[]
  defaultHost: string
  onCancel: () => void
  onCreate: (s: Schedule) => void
}

function NewScheduleModal({ open, hosts, runnables, defaultHost, onCancel, onCreate }: NewScheduleModalProps) {
  const [form] = Form.useForm()
  const [action, setAction] = useState<Action>('runnable')
  const [whenKind, setWhenKind] = useState<WhenKind>('cron')
  const [selectedRunnable, setSelectedRunnable] = useState<Runnable | null>(null)
  const [cronPreset, setCronPreset] = useState<string>('')
  const [customCron, setCustomCron] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const isDark = useIsDark()

  const hostName: string = Form.useWatch('host', form) ?? defaultHost
  const hostRunnables = runnables.filter(r => r.host === hostName)

  const handleRunnableChange = (name: string) => {
    const r = hostRunnables.find(x => x.name === name) ?? null
    setSelectedRunnable(r)
    // Reset args fields when runnable changes
    const argKeys = r?.args.map(a => `arg_${a.name}`) ?? []
    const resetObj: Record<string, undefined> = {}
    argKeys.forEach(k => { resetObj[k] = undefined })
    form.setFieldsValue(resetObj)
  }

  const handleHostChange = () => {
    form.setFieldValue('runnable', undefined)
    setSelectedRunnable(null)
  }

  const resetAll = () => {
    form.resetFields()
    setAction('runnable')
    setWhenKind('cron')
    setCronPreset('')
    setCustomCron('')
    setSelectedRunnable(null)
  }

  const handleSubmit = async () => {
    const values = await form.validateFields()

    // Timing — cron (preset or custom) or an interval.
    const when: ScheduleWhen = {}
    if (whenKind === 'every') {
      const every = String(values.every ?? '').trim()
      if (!every) { form.setFields([{ name: 'every', errors: ['Required'] }]); return }
      when.every = every
    } else {
      const cronValue = cronPreset === '__custom__' ? customCron.trim() : cronPreset
      if (!cronValue) { form.setFields([{ name: 'cron', errors: ['Required'] }]); return }
      when.cron = cronValue
    }
    const tz = String(values.timezone ?? '').trim()
    if (tz) when.timezone = tz

    const base: Schedule = { id: '', action, when, enabled: true }
    if (action === 'event') {
      const event = String(values.event ?? '').trim()
      if (!event) { form.setFields([{ name: 'event', errors: ['Required'] }]); return }
      const fields: Record<string, string> = {}
      for (const f of (values.fields ?? []) as { key?: string; value?: string }[]) {
        if (f?.key?.trim()) fields[f.key.trim()] = String(f.value ?? '').trim()
      }
      base.event = event
      base.fields = fields
      base.id = `rs-${event}-${Date.now()}`
    } else {
      const args: Record<string, unknown> = {}
      for (const a of selectedRunnable?.args ?? []) {
        const v = values[`arg_${a.name}`]
        if (v !== undefined && v !== '') args[a.name] = v
      }
      base.host = values.host
      base.runnable = values.runnable
      base.args = args
      base.id = `rs-${values.runnable}-${Date.now()}`
    }

    setSubmitting(true)
    try {
      await bridge.create_schedule({ ...base } as unknown as Record<string, unknown>)
      onCreate(base)
      resetAll()
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = () => { resetAll(); onCancel() }

  const labelStyle = { color: isDark ? '#aaa' : '#666', fontSize: 12 }

  return (
    <Modal
      title="New schedule"
      open={open}
      onCancel={handleCancel}
      onOk={handleSubmit}
      okText="Create"
      confirmLoading={submitting}
      width={480}
    >
      <Form form={form} layout="vertical" initialValues={{ host: defaultHost, fields: [] }}>
        <Form.Item label="When it fires, it will…">
          <Segmented
            value={action}
            onChange={v => setAction(v as Action)}
            options={[
              { label: 'Run a runnable', value: 'runnable' },
              { label: 'Emit an event', value: 'event' },
            ]}
          />
          <div style={{ ...labelStyle, marginTop: 6 }}>
            {action === 'runnable'
              ? 'Runs the chosen runnable directly (no agent, no confirm gate).'
              : 'Emits a matchable event — set up Triggers (source = schedule) to react to it, like email.'}
          </div>
        </Form.Item>

        {/* ── Timing (shared) ── */}
        <Form.Item label="Schedule">
          <Segmented
            value={whenKind}
            onChange={v => setWhenKind(v as WhenKind)}
            options={[{ label: 'Cron', value: 'cron' }, { label: 'Interval', value: 'every' }]}
          />
        </Form.Item>

        {whenKind === 'cron' ? (
          <>
            <Form.Item name="cron" label="Cron" rules={[{ required: false }]}>
              <Select
                placeholder="Choose a preset or custom…"
                value={cronPreset || undefined}
                onChange={(v: string) => { setCronPreset(v); form.setFieldValue('cron', v) }}
              >
                {CRON_PRESETS.map(p => (
                  <Select.Option key={p.value} value={p.value}>{p.label}</Select.Option>
                ))}
              </Select>
            </Form.Item>
            {cronPreset === '__custom__' && (
              <Form.Item label="Cron expression" required>
                <Input
                  placeholder="e.g. 0 2 * * *"
                  value={customCron}
                  onChange={e => setCustomCron(e.target.value)}
                  style={{ fontFamily: 'monospace' }}
                />
              </Form.Item>
            )}
          </>
        ) : (
          <Form.Item name="every" label="Every" rules={[{ required: true, whitespace: true, message: 'Required' }]}
            help="A number followed by s / m / h / d — e.g. 15m, 1h, 1d.">
            <Input placeholder="15m" style={{ fontFamily: 'monospace' }} />
          </Form.Item>
        )}

        <Form.Item name="timezone" label="Timezone (optional)"
          help="IANA name, e.g. Europe/London. Blank = this PC's local time.">
          <Input placeholder="Europe/London" style={{ fontFamily: 'monospace' }} />
        </Form.Item>

        {/* ── Event action ── */}
        {action === 'event' && (
          <>
            <Form.Item name="event" label="Event name" rules={[{ required: true, whitespace: true, message: 'Required' }]}
              help="The event type triggers match on (source = schedule).">
              <Input placeholder="nightly_report" style={{ fontFamily: 'monospace' }} />
            </Form.Item>
            <div style={{ ...labelStyle, marginBottom: 8 }}>Fields (optional) — extra payload a trigger can match on</div>
            <Form.List name="fields">
              {(fields, { add, remove }) => (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 8 }}>
                  {fields.map(f => (
                    <div key={f.key} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <Form.Item name={[f.name, 'key']} noStyle>
                        <Input size="small" placeholder="scope" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <span style={{ color: '#888' }}>=</span>
                      <Form.Item name={[f.name, 'value']} noStyle>
                        <Input size="small" placeholder="prod" style={{ fontFamily: 'monospace' }} />
                      </Form.Item>
                      <MinusCircleOutlined onClick={() => remove(f.name)} style={{ color: '#888', cursor: 'pointer' }} />
                    </div>
                  ))}
                  <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add()} style={{ width: 'fit-content' }}>
                    Add field
                  </Button>
                </div>
              )}
            </Form.List>
          </>
        )}

        {/* ── Runnable action ── */}
        {action === 'runnable' && (
          <>
            <Form.Item name="host" label="Host" rules={[{ required: true }]}>
              <Select onChange={handleHostChange}>
                {hosts.map(h => (
                  <Select.Option key={h.name} value={h.name}>
                    <Space size={6}>
                      <span style={{ fontSize: 9, color: h.connected ? '#52c41a' : '#595959' }}>●</span>
                      {h.name}
                    </Space>
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>

            <Form.Item name="runnable" label="Runnable" rules={[{ required: true }]}>
              <Select
                placeholder="Select a runnable"
                onChange={handleRunnableChange}
                showSearch
                filterOption={(input, opt) =>
                  (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())
                }
              >
                {hostRunnables.map(r => (
                  <Select.Option key={r.name} value={r.name}>
                    <code>/{r.name}</code>
                    {r.description && <span style={{ ...labelStyle, marginLeft: 8 }}>{r.description}</span>}
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>

            {selectedRunnable && selectedRunnable.args.length > 0 && (
              <>
                <div style={{ ...labelStyle, marginBottom: 8, marginTop: 4 }}>Arguments</div>
                {selectedRunnable.args.map(a => (
                  <Form.Item
                    key={a.name}
                    name={`arg_${a.name}`}
                    label={<span style={{ fontFamily: 'monospace' }}>{a.name}</span>}
                    rules={a.required ? [{ required: true, message: `${a.name} is required` }] : []}
                    initialValue={a.default !== undefined ? String(a.default) : undefined}
                  >
                    {a.options ? (
                      <Select>
                        {a.options.map(o => (
                          <Select.Option key={o} value={o}>{o}</Select.Option>
                        ))}
                      </Select>
                    ) : (
                      <Input
                        placeholder={a.default !== undefined ? String(a.default) : undefined}
                        style={{ fontFamily: 'monospace' }}
                      />
                    )}
                  </Form.Item>
                ))}
              </>
            )}
          </>
        )}
      </Form>
    </Modal>
  )
}

export function SchedulesView({ hosts, runnables, selectedHost }: SchedulesViewProps) {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [modalOpen, setModalOpen] = useState(false)
  const isDark = useIsDark()

  useEffect(() => {
    bridge.get_schedules().then(setSchedules)
  }, [])

  const handleDelete = async (id: string) => {
    await bridge.delete_schedule(id)
    setSchedules(s => s.filter(x => x.id !== id))
  }

  const handleCreate = (s: Schedule) => {
    setSchedules(prev => [...prev, s])
    setModalOpen(false)
  }

  const dimCol = isDark ? '#888' : '#999'
  const whenText = (w: ScheduleWhen) => {
    const base = w.every ? `every ${w.every}` : (w.cron ?? '')
    return w.timezone ? `${base}  (${w.timezone})` : base
  }

  const columns = [
    {
      title: 'Action',
      key: 'action',
      render: (_: unknown, s: Schedule) =>
        s.action === 'event'
          ? <Space size={6}><Tag color="purple">event</Tag><code>{s.event}</code></Space>
          : <Space size={6}><Tag>run</Tag><code>/{s.runnable}</code></Space>,
    },
    {
      title: 'Where',
      key: 'where',
      render: (_: unknown, s: Schedule) =>
        s.action === 'runnable' && s.host ? <Tag>{s.host}</Tag> : <span style={{ color: dimCol }}>—</span>,
    },
    {
      title: 'Schedule',
      key: 'schedule',
      render: (_: unknown, s: Schedule) => <code style={{ fontSize: 12 }}>{whenText(s.when)}</code>,
    },
    {
      title: 'Next run',
      dataIndex: 'nextRun',
      key: 'nextRun',
      render: (v?: string) => <span style={{ color: dimCol }}>{v ?? '—'}</span>,
    },
    {
      title: '',
      key: 'actions',
      width: 48,
      render: (_: unknown, s: Schedule) => (
        <Button danger size="small" icon={<DeleteOutlined />} onClick={() => handleDelete(s.id)} />
      ),
    },
  ]

  const detailFor = (s: Schedule): Record<string, unknown> =>
    s.action === 'event' ? (s.fields ?? {}) : (s.args ?? {})

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} size="small" onClick={() => setModalOpen(true)}>
          New schedule
        </Button>
      </div>

      <Table
        dataSource={schedules}
        columns={columns}
        rowKey="id"
        size="small"
        expandable={{
          rowExpandable: s => Object.keys(detailFor(s)).length > 0,
          expandedRowRender: s => (
            <div style={{ paddingLeft: 24, fontFamily: 'monospace', fontSize: 12, color: dimCol }}>
              {Object.entries(detailFor(s)).map(([k, v]) => (
                <span key={k} style={{ marginRight: 12 }}>
                  <span style={{ color: isDark ? '#4fc1ff' : '#0958d9' }}>
                    {s.action === 'event' ? k : `--${k}`}
                  </span>={String(v)}
                </span>
              ))}
            </div>
          ),
        }}
      />

      <NewScheduleModal
        open={modalOpen}
        hosts={hosts}
        runnables={runnables}
        defaultHost={selectedHost}
        onCancel={() => setModalOpen(false)}
        onCreate={handleCreate}
      />
    </div>
  )
}
