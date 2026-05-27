import { useEffect, useState } from 'react'
import { Table, Tag, Button, Modal, Form, Select, Input, Space } from 'antd'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { bridge, type Host, type Runnable, type Schedule } from '../bridge'
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

  const handleSubmit = async () => {
    const values = await form.validateFields()
    const cronValue = cronPreset === '__custom__' ? customCron.trim() : cronPreset
    if (!cronValue) {
      form.setFields([{ name: 'schedule', errors: ['Required'] }])
      return
    }
    const args: Record<string, unknown> = {}
    for (const a of selectedRunnable?.args ?? []) {
      const v = values[`arg_${a.name}`]
      if (v !== undefined && v !== '') args[a.name] = v
    }
    const id = `rs-${values.runnable}-${Date.now()}`
    const schedule: Schedule = {
      id,
      runnable: values.runnable,
      host: values.host,
      schedule: cronValue,
      ...(Object.keys(args).length > 0 ? { args } : {}),
    }
    setSubmitting(true)
    try {
      await bridge.create_schedule({ ...schedule })
      onCreate(schedule)
      form.resetFields()
      setCronPreset('')
      setCustomCron('')
      setSelectedRunnable(null)
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = () => {
    form.resetFields()
    setCronPreset('')
    setCustomCron('')
    setSelectedRunnable(null)
    onCancel()
  }

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
      <Form form={form} layout="vertical" initialValues={{ host: defaultHost }}>
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

        <Form.Item name="schedule" label="Schedule" rules={[{ required: false }]}>
          <Select
            placeholder="Choose a preset or custom…"
            value={cronPreset || undefined}
            onChange={(v: string) => { setCronPreset(v); form.setFieldValue('schedule', v) }}
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

  const columns = [
    {
      title: 'Runnable',
      dataIndex: 'runnable',
      key: 'runnable',
      render: (n: string) => <code>/{n}</code>,
    },
    {
      title: 'Host',
      dataIndex: 'host',
      key: 'host',
      render: (h: string) => <Tag>{h}</Tag>,
    },
    {
      title: 'Schedule',
      dataIndex: 'schedule',
      key: 'schedule',
      render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code>,
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
          rowExpandable: s => !!(s.args && Object.keys(s.args).length > 0),
          expandedRowRender: s => (
            <div style={{ paddingLeft: 24, fontFamily: 'monospace', fontSize: 12, color: dimCol }}>
              {Object.entries(s.args ?? {}).map(([k, v]) => (
                <span key={k} style={{ marginRight: 12 }}>
                  <span style={{ color: isDark ? '#4fc1ff' : '#0958d9' }}>--{k}</span>={String(v)}
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
