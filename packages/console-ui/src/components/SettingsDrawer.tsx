import { useEffect, useState } from 'react'
import { Drawer, Form, Input, Select, Button, Divider, Typography, Tag, Space, Tabs, Popconfirm } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined } from '@ant-design/icons'
import { bridge, type JumpHost } from '../bridge'

const { Text } = Typography

interface SettingsDrawerProps {
  open: boolean
  onClose: () => void
}

function GeneralTab() {
  return (
    <>
      <Text strong style={{ fontSize: 13 }}>LLM / API</Text>
      <Form layout="vertical" size="small" style={{ marginTop: 10 }}>
        <Form.Item label="API base URL">
          <Input placeholder="https://api.anthropic.com" />
        </Form.Item>
        <Form.Item label="API key">
          <Input.Password placeholder="sk-ant-..." />
        </Form.Item>
        <Form.Item label="Model">
          <Input placeholder="claude-opus-4-7" />
        </Form.Item>
      </Form>

      <Divider />

      <Text strong style={{ fontSize: 13 }}>SSH defaults</Text>
      <Form layout="vertical" size="small" style={{ marginTop: 10 }}>
        <Form.Item label="Default username">
          <Input placeholder="your-username" />
        </Form.Item>
        <Form.Item label="Default identity file">
          <Input placeholder="~/.ssh/runspec_ed25519" />
        </Form.Item>
      </Form>

      <Divider />

      <Text type="secondary" style={{ fontSize: 12 }}>
        Settings are saved locally. API keys are stored in the OS keychain.
      </Text>
    </>
  )
}

const ROLE_COLOURS: Record<string, string> = { primary: 'blue', secondary: 'default' }

interface HostFormValues {
  name: string
  hostname: string
  user?: string
  region?: string
  datacenter?: string
  role: 'primary' | 'secondary'
  identityFile?: string
}

function JumpHostsTab() {
  const [hosts, setHosts] = useState<JumpHost[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null) // null=none ''=new
  const [form] = Form.useForm<HostFormValues>()

  useEffect(() => {
    bridge.get_config().then(cfg => setHosts((cfg.jumpHosts as JumpHost[]) ?? []))
  }, [])

  const persist = async (next: JumpHost[]) => {
    setHosts(next)
    await bridge.save_config({ jumpHosts: next })
  }

  const startAdd = () => {
    form.resetFields()
    setEditingKey('')
  }

  const startEdit = (h: JumpHost) => {
    form.setFieldsValue(h)
    setEditingKey(h.name)
  }

  const cancel = () => {
    setEditingKey(null)
    form.resetFields()
  }

  const save = async () => {
    const values = await form.validateFields()
    if (editingKey === '') {
      await persist([...hosts, values as JumpHost])
    } else {
      await persist(hosts.map(h => h.name === editingKey ? { ...h, ...values } : h))
    }
    setEditingKey(null)
    form.resetFields()
  }

  const remove = async (name: string) => {
    await persist(hosts.filter(h => h.name !== name))
    if (editingKey === name) setEditingKey(null)
  }

  const isEditing = editingKey !== null

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Saved to <code>jump_hosts.toml</code>. Adding a host connects and discovers runnables automatically.
        </Text>
        <Button
          size="small"
          icon={<PlusOutlined />}
          onClick={startAdd}
          disabled={isEditing}
        >
          Add host
        </Button>
      </div>

      {/* Inline add/edit form */}
      {editingKey !== null && (
        <div style={{
          border: '1px solid #333', borderRadius: 8, padding: '14px 14px 6px',
          marginBottom: 12, background: 'rgba(255,255,255,0.02)',
        }}>
          <Text strong style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
            {editingKey === '' ? 'New jump host' : `Edit — ${editingKey}`}
          </Text>
          <Form form={form} layout="vertical" size="small">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 12px' }}>
              <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="eu-dc1-primary" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
              </Form.Item>
              <Form.Item name="hostname" label="Hostname" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="hostname.company.com" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Form.Item name="role" label="Role" rules={[{ required: true, message: 'Required' }]}>
                <Select options={[
                  { value: 'primary', label: 'Primary' },
                  { value: 'secondary', label: 'Secondary' },
                ]} />
              </Form.Item>
              <Form.Item name="user" label="SSH user">
                <Input placeholder="inherits default" />
              </Form.Item>
              <Form.Item name="region" label="Region">
                <Input placeholder="europe" />
              </Form.Item>
              <Form.Item name="datacenter" label="Datacenter">
                <Input placeholder="datacenter-1" />
              </Form.Item>
            </div>
            <Form.Item name="identityFile" label="Identity file">
              <Input placeholder="~/.ssh/id_ed25519 (leave blank for SSH default)" />
            </Form.Item>
            <Space style={{ marginTop: 2, marginBottom: 8 }}>
              <Button size="small" type="primary" icon={<CheckOutlined />} onClick={save}>Save</Button>
              <Button size="small" icon={<CloseOutlined />} onClick={cancel}>Cancel</Button>
            </Space>
          </Form>
        </div>
      )}

      {/* Host list */}
      {hosts.length === 0 && editingKey === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No jump hosts configured. Add one to get started.</Text>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {hosts.map(h => (
          <div key={h.name} style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 10px', borderRadius: 6,
            border: '1px solid #2a2a2a', background: editingKey === h.name ? 'rgba(255,255,255,0.03)' : 'transparent',
          }}>
            <Text style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 130, flexShrink: 0 }}>{h.name}</Text>
            <Text type="secondary" title={h.hostname} style={{
              fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{h.hostname}</Text>
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              {h.region && <Tag style={{ fontSize: 10, margin: 0 }}>{h.region}</Tag>}
              {h.datacenter && <Tag style={{ fontSize: 10, margin: 0 }}>{h.datacenter}</Tag>}
              <Tag color={ROLE_COLOURS[h.role]} style={{ fontSize: 10, margin: 0 }}>{h.role}</Tag>
            </div>
            <Button
              type="text" size="small" icon={<EditOutlined />}
              onClick={() => startEdit(h)}
              disabled={isEditing}
              style={{ padding: '0 4px', color: '#666' }}
            />
            <Popconfirm
              title={`Remove ${h.name}?`}
              onConfirm={() => remove(h.name)}
              okText="Remove" cancelText="Cancel"
              placement="left"
            >
              <Button
                type="text" size="small" icon={<DeleteOutlined />}
                disabled={isEditing}
                style={{ padding: '0 4px', color: '#666' }}
                danger
              />
            </Popconfirm>
          </div>
        ))}
      </div>
    </div>
  )
}

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width={480} open={open} onClose={onClose}>
      <Tabs
        size="small"
        items={[
          { key: 'general',    label: 'General',     children: <GeneralTab /> },
          { key: 'jumpHosts',  label: 'Jump Hosts',  children: <JumpHostsTab /> },
        ]}
      />
    </Drawer>
  )
}
