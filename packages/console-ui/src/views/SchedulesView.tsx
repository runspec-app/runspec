import { useEffect, useState } from 'react'
import { Table, Tag, Button, Modal, Form, Select, Input, Space, Segmented, Switch, Tooltip, Popconfirm, Collapse, message } from 'antd'
import { DeleteOutlined, PlusOutlined, MinusCircleOutlined, EditOutlined, PlayCircleOutlined, CalendarOutlined, ReloadOutlined } from '@ant-design/icons'
import { bridge, type Host, type Runnable, type Schedule, type ScheduleWhen, type RunCard, type TriggerEvent } from '../bridge'
import { useIsDark } from '../ThemeContext'
import { RunInbox } from '../components/RunInbox'
import { EventLog } from '../components/EventLog'

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
type Target = 'host' | 'group'

interface ScheduleModalProps {
  open: boolean
  hosts: Host[]
  runnables: Runnable[]
  defaultHost: string
  editing: Schedule | null
  onCancel: () => void
  onSaved: () => void
}

function ScheduleModal({ open, hosts, runnables, defaultHost, editing, onCancel, onSaved }: ScheduleModalProps) {
  const [form] = Form.useForm()
  const [action, setAction] = useState<Action>('runnable')
  const [target, setTarget] = useState<Target>('host')
  const [whenKind, setWhenKind] = useState<WhenKind>('cron')
  const [selectedRunnable, setSelectedRunnable] = useState<Runnable | null>(null)
  const [cronPreset, setCronPreset] = useState<string>('')
  const [customCron, setCustomCron] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const isDark = useIsDark()

  // Distinct host groups (from each host's `group` field), for group targeting.
  const groupNames = [...new Set(hosts.map(h => h.group).filter((g): g is string => !!g))].sort()
  const hostName: string = Form.useWatch('host', form) ?? defaultHost
  const groupName: string = Form.useWatch('group', form) ?? ''
  // The runnable picker offers what the chosen target can run: a single host's
  // runnables, or (group mode) every runnable present on any group member.
  const groupMemberNames = new Set(hosts.filter(h => h.group === groupName).map(h => h.name))
  const hostRunnables = target === 'group'
    ? [...new Map(
        runnables.filter(r => groupMemberNames.has(r.host)).map(r => [r.name, r]),
      ).values()]
    : runnables.filter(r => r.host === hostName)
  const isEdit = !!editing

  const resetAll = () => {
    form.resetFields()
    setAction('runnable')
    setTarget('host')
    setWhenKind('cron')
    setCronPreset('')
    setCustomCron('')
    setSelectedRunnable(null)
  }

  // Prefill from the schedule being edited (or a clean slate for a new one)
  // whenever the dialog opens. Local state (action/whenKind/cron preset) lives
  // outside the Form, so it has to be seeded here too.
  useEffect(() => {
    if (!open) return
    if (!editing) {
      resetAll()
      form.setFieldsValue({ host: defaultHost, fields: [] })
      return
    }
    setAction(editing.action)
    setTarget(editing.group ? 'group' : 'host')
    const kind: WhenKind = editing.when.every ? 'every' : 'cron'
    setWhenKind(kind)
    if (kind === 'cron') {
      const cron = editing.when.cron ?? ''
      if (CRON_PRESETS.some(p => p.value === cron)) {
        setCronPreset(cron)
        setCustomCron('')
      } else {
        setCronPreset('__custom__')
        setCustomCron(cron)
      }
    } else {
      setCronPreset('')
      setCustomCron('')
    }
    const r = editing.group
      ? runnables.find(x => x.name === editing.runnable && hosts.some(h => h.group === editing.group && h.name === x.host)) ?? null
      : runnables.find(x => x.host === editing.host && x.name === editing.runnable) ?? null
    setSelectedRunnable(r)
    const fv: Record<string, unknown> = {
      host: editing.host ?? defaultHost,
      group: editing.group ?? '',
      runnable: editing.runnable,
      timezone: editing.when.timezone ?? '',
      cron: editing.when.cron ?? '',
      every: editing.when.every ?? '',
      event: editing.event ?? '',
      fields: Object.entries(editing.fields ?? {}).map(([key, value]) => ({ key, value })),
    }
    for (const [k, v] of Object.entries(editing.args ?? {})) fv[`arg_${k}`] = String(v)
    form.setFieldsValue(fv)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing])

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

  const handleTargetChange = (t: Target) => {
    setTarget(t)
    // Switching host↔group invalidates the chosen runnable (the available set
    // differs), so clear it.
    form.setFieldsValue({ runnable: undefined })
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

    const base: Schedule = { id: editing?.id ?? '', action, when, enabled: editing?.enabled ?? true }
    if (action === 'event') {
      const event = String(values.event ?? '').trim()
      if (!event) { form.setFields([{ name: 'event', errors: ['Required'] }]); return }
      const fields: Record<string, string> = {}
      for (const f of (values.fields ?? []) as { key?: string; value?: string }[]) {
        if (f?.key?.trim()) fields[f.key.trim()] = String(f.value ?? '').trim()
      }
      base.event = event
      base.fields = fields
      if (!base.id) base.id = `rs-${event}-${Date.now()}`
    } else {
      const args: Record<string, unknown> = {}
      for (const a of selectedRunnable?.args ?? []) {
        const v = values[`arg_${a.name}`]
        if (v !== undefined && v !== '') args[a.name] = v
      }
      if (target === 'group') {
        const group = String(values.group ?? '').trim()
        if (!group) { form.setFields([{ name: 'group', errors: ['Required'] }]); return }
        base.group = group
      } else {
        base.host = values.host
      }
      base.runnable = values.runnable
      base.args = args
      if (!base.id) base.id = `rs-${values.runnable}-${Date.now()}`
    }

    setSubmitting(true)
    try {
      if (isEdit) await bridge.update_schedule(base.id, { ...base } as unknown as Record<string, unknown>)
      else await bridge.create_schedule({ ...base } as unknown as Record<string, unknown>)
      message.success(isEdit ? 'Schedule updated' : 'Schedule created')
      onSaved()
      resetAll()
    } catch (e) {
      message.error(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = () => { resetAll(); onCancel() }

  const labelStyle = { color: isDark ? '#aaa' : '#666', fontSize: 12 }

  return (
    <Modal
      title={isEdit ? 'Edit schedule' : 'New schedule'}
      open={open}
      onCancel={handleCancel}
      onOk={handleSubmit}
      okText={isEdit ? 'Save' : 'Create'}
      confirmLoading={submitting}
      width={480}
      destroyOnClose
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
            <Form.Item label="Target">
              <Segmented
                value={target}
                onChange={v => handleTargetChange(v as Target)}
                options={[
                  { label: 'One host', value: 'host' },
                  { label: 'Host group', value: 'group', disabled: groupNames.length === 0 },
                ]}
              />
              <div style={{ ...labelStyle, marginTop: 4 }}>
                {target === 'group'
                  ? 'Runs the runnable on every host in the group (one run card per host).'
                  : 'Runs the runnable on a single host.'}
              </div>
            </Form.Item>

            {target === 'host' ? (
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
            ) : (
              <Form.Item name="group" label="Group" rules={[{ required: true }]}>
                <Select onChange={handleHostChange} placeholder="Select a host group">
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

// "in 2h 5m" / "in 30s" / "due" — a compact countdown to the next fire.
function relativeWhen(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now()
  if (Number.isNaN(ms)) return ''
  if (ms <= 0) return 'due'
  const mins = Math.round(ms / 60000)
  if (mins < 60) return `in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `in ${hours}h ${mins % 60}m`
  const days = Math.floor(hours / 24)
  return `in ${days}d ${hours % 24}h`
}

export function SchedulesView({ hosts, runnables, selectedHost }: SchedulesViewProps) {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Schedule | null>(null)
  // Run inbox (runnable-action schedule runs) + event-audit feed, mirroring the
  // Triggers tab. Cards arrive via runspec:schedule_card; their streamed output
  // (keyed by card_id) via runspec:output; events via runspec:schedule_event.
  const [cards, setCards] = useState<Record<string, RunCard>>({})
  const [streams, setStreams] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const [events, setEvents] = useState<TriggerEvent[]>([])
  const isDark = useIsDark()

  const reload = () => bridge.get_schedules().then(setSchedules)

  useEffect(() => { reload() }, [])

  // Seed + subscribe to the schedule run inbox and event log.
  useEffect(() => {
    const cardIds = new Set<string>()
    bridge.get_schedule_board().then(board => {
      board.forEach(c => cardIds.add(c.card_id))
      setCards(Object.fromEntries(board.map(c => [c.card_id, c])))
    }).catch(console.error)
    bridge.get_schedule_events().then(evs => setEvents([...evs].reverse())).catch(console.error)

    const onCard = (e: Event) => {
      const card = (e as CustomEvent).detail as RunCard
      cardIds.add(card.card_id)
      setCards(prev => ({ ...prev, [card.card_id]: card }))
    }
    const onOutput = (e: Event) => {
      const { id, line } = (e as CustomEvent).detail as { id: string; line: string }
      if (!cardIds.has(id)) return  // only schedule-run output
      setStreams(s => ({ ...s, [id]: (s[id] ?? '') + line + '\n' }))
    }
    const onEvent = (e: Event) => {
      const rec = (e as CustomEvent).detail as TriggerEvent
      setEvents(prev => [rec, ...prev].slice(0, 300))
    }
    window.addEventListener('runspec:schedule_card', onCard)
    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:schedule_event', onEvent)
    return () => {
      window.removeEventListener('runspec:schedule_card', onCard)
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:schedule_event', onEvent)
    }
  }, [])

  const handleDelete = async (id: string) => {
    await bridge.delete_schedule(id)
    setSchedules(s => s.filter(x => x.id !== id))
  }

  const handleSaved = () => {
    setModalOpen(false)
    setEditing(null)
    reload()
  }

  const openNew = () => { setEditing(null); setModalOpen(true) }
  const openEdit = (s: Schedule) => { setEditing(s); setModalOpen(true) }

  const handleToggle = async (s: Schedule, enabled: boolean) => {
    // Optimistic flip; persist via update_schedule, then refresh for nextRun.
    setSchedules(prev => prev.map(x => (x.id === s.id ? { ...x, enabled } : x)))
    try {
      await bridge.update_schedule(s.id, { ...s, enabled } as unknown as Record<string, unknown>)
      reload()
    } catch (e) {
      message.error(String(e))
      reload()
    }
  }

  const handleRunNow = async (s: Schedule) => {
    try {
      const res = await bridge.run_schedule_now(s.id)
      if (res.action === 'event') message.success(`Emitted event “${res.event}”`)
      else message.success(`Running /${res.runnable} on ${res.host}…`)
    } catch (e) {
      message.error(String(e))
    }
  }

  const dimCol = isDark ? '#888' : '#999'
  const keyCol = isDark ? '#4fc1ff' : '#0958d9'
  const whenText = (w: ScheduleWhen) => {
    const base = w.every ? `every ${w.every}` : (w.cron ?? '')
    return w.timezone ? `${base}  (${w.timezone})` : base
  }

  const detailFor = (s: Schedule): Record<string, unknown> =>
    s.action === 'event' ? (s.fields ?? {}) : (s.args ?? {})

  // Inline summary of the args/fields — first couple of entries as tiny tags,
  // with a "+N" overflow. Full set stays in the expandable row.
  const detailSummary = (s: Schedule) => {
    const entries = Object.entries(detailFor(s))
    if (entries.length === 0) return <span style={{ color: dimCol }}>—</span>
    const shown = entries.slice(0, 2)
    const prefix = s.action === 'event' ? '' : '--'
    return (
      <Space size={4} wrap>
        {shown.map(([k, v]) => (
          <span key={k} style={{ fontFamily: 'monospace', fontSize: 11 }}>
            <span style={{ color: keyCol }}>{prefix}{k}</span>
            <span style={{ color: dimCol }}>={String(v)}</span>
          </span>
        ))}
        {entries.length > shown.length && (
          <span style={{ color: dimCol, fontSize: 11 }}>+{entries.length - shown.length}</span>
        )}
      </Space>
    )
  }

  const columns = [
    {
      title: '',
      key: 'enabled',
      width: 56,
      render: (_: unknown, s: Schedule) => (
        <Tooltip title={s.enabled === false ? 'Disabled — won’t fire' : 'Enabled'}>
          <Switch size="small" checked={s.enabled !== false} onChange={v => handleToggle(s, v)} />
        </Tooltip>
      ),
    },
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
        s.action === 'runnable'
          ? (s.group
              ? <Tag color="geekblue">group: {s.group}</Tag>
              : (s.host ? <Tag>{s.host}</Tag> : <span style={{ color: dimCol }}>—</span>))
          : <span style={{ color: dimCol }}>—</span>,
    },
    {
      title: 'Schedule',
      key: 'schedule',
      render: (_: unknown, s: Schedule) => <code style={{ fontSize: 12 }}>{whenText(s.when)}</code>,
    },
    {
      title: 'Details',
      key: 'detail',
      render: (_: unknown, s: Schedule) => detailSummary(s),
    },
    {
      title: 'Next run',
      dataIndex: 'nextRun',
      key: 'nextRun',
      render: (v: string | undefined, s: Schedule) => {
        if (s.enabled === false) return <span style={{ color: dimCol }}>paused</span>
        if (!v) return <span style={{ color: dimCol }}>—</span>
        return (
          <Tooltip title={new Date(v).toLocaleString()}>
            <span style={{ color: dimCol }}>{relativeWhen(v)}</span>
          </Tooltip>
        )
      },
    },
    {
      title: '',
      key: 'actions',
      width: 132,
      render: (_: unknown, s: Schedule) => (
        <Space size={2}>
          <Tooltip title="Run now">
            <Button type="text" size="small" icon={<PlayCircleOutlined />} onClick={() => handleRunNow(s)} />
          </Tooltip>
          <Tooltip title="Edit">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(s)} />
          </Tooltip>
          <Popconfirm title="Delete this schedule?" okText="Delete" okButtonProps={{ danger: true }} onConfirm={() => handleDelete(s.id)}>
            <Button danger type="text" size="small" icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const scheduleTable = (
    <Table
      dataSource={schedules}
      columns={columns}
      rowKey="id"
      size="small"
      pagination={false}
      rowClassName={s => (s.enabled === false ? 'rs-schedule-disabled' : '')}
      expandable={{
        rowExpandable: s => Object.keys(detailFor(s)).length > 0,
        expandedRowRender: s => (
          <div style={{ paddingLeft: 24, fontFamily: 'monospace', fontSize: 12, color: dimCol }}>
            {Object.entries(detailFor(s)).map(([k, v]) => (
              <span key={k} style={{ marginRight: 12 }}>
                <span style={{ color: keyCol }}>
                  {s.action === 'event' ? k : `--${k}`}
                </span>={String(v)}
              </span>
            ))}
          </div>
        ),
      }}
    />
  )

  return (
    <div>
      {/* Schedules — collapsible table, mirroring the Triggers tab. Schedules are
          gated by the same working-hours rota (set on the Rota tab); fires outside
          it are skipped and recorded in the event log below. */}
      <Collapse
        size="small"
        defaultActiveKey={['schedules']}
        style={{ marginBottom: 20 }}
        items={[{
          key: 'schedules',
          label: (
            <span>
              <CalendarOutlined /> Schedules
              <span style={{ color: dimCol, fontSize: 12 }}>{' · '}{schedules.length} configured</span>
            </span>
          ),
          extra: (
            <Space size={8} onClick={e => e.stopPropagation()}>
              <Tooltip title="Reload schedules">
                <ReloadOutlined onClick={reload} style={{ color: dimCol }} />
              </Tooltip>
              <Button type="primary" icon={<PlusOutlined />} size="small" onClick={openNew}>
                New schedule
              </Button>
            </Space>
          ),
          children: scheduleTable,
        }]}
      />

      {/* Run inbox — runnable-action schedule runs (no agent, no gate). */}
      <RunInbox
        cards={cards}
        selected={selected}
        onSelect={setSelected}
        onStop={id => bridge.cancel_invocation(id).catch(console.error)}
        streams={streams}
        emptyText="No schedule runs yet."
      />

      {/* Event log — every scheduled fire + its disposition (shared widget). */}
      <EventLog events={events} hint="every scheduled fire, run or skipped" />

      <ScheduleModal
        open={modalOpen}
        hosts={hosts}
        runnables={runnables}
        defaultHost={selectedHost}
        editing={editing}
        onCancel={() => { setModalOpen(false); setEditing(null) }}
        onSaved={handleSaved}
      />
    </div>
  )
}
