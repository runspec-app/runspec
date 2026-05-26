import { useEffect, useRef, useState } from 'react'
import { Drawer, Form, Input, Button, Divider, Typography, Space, Tabs, Popconfirm, message, Tag, Tooltip } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, CheckOutlined, CloseOutlined, UploadOutlined, DownloadOutlined, UpOutlined, DownOutlined, ApiOutlined, LoadingOutlined } from '@ant-design/icons'
import { bridge, type JumpHost, type TestResult } from '../bridge'

const { Text } = Typography

interface SettingsDrawerProps {
  open: boolean
  onClose: () => void
  onHostsChanged?: () => void
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

interface HostFormValues {
  name: string
  hostname: string
  runspec_path?: string
  user?: string
  port?: number
  identityFile?: string
  group?: string
}

function toToml(hosts: JumpHost[]): string {
  return hosts.map(h => {
    const lines = [`[${h.name}]`, `hostname = "${h.hostname}"`]
    if (h.user) lines.push(`user = "${h.user}"`)
    if (h.port) lines.push(`port = ${h.port}`)
    if (h.identityFile) lines.push(`identity_file = "${h.identityFile}"`)
    if (h.group) lines.push(`group = "${h.group}"`)
    return lines.join('\n')
  }).join('\n\n')
}

function downloadToml(content: string, filename = 'jump_hosts.toml') {
  const blob = new Blob([content], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function JumpHostsTab({ onHostsChanged }: { onHostsChanged?: () => void }) {
  const [hosts, setHosts] = useState<JumpHost[]>([])
  const [editingKey, setEditingKey] = useState<string | null>(null) // null=none ''=new
  const [form] = Form.useForm<HostFormValues>()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [testingHost, setTestingHost] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({})

  useEffect(() => {
    bridge.get_jump_hosts().then(setHosts)
  }, [])

  const persist = async (next: JumpHost[]) => {
    setHosts(next)
    await bridge.save_jump_hosts(next)
    onHostsChanged?.()
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

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!fileInputRef.current) return
    fileInputRef.current.value = ''
    if (!file) return
    const content = await file.text()
    const imported = await bridge.import_jump_hosts(content)
    if (imported.length === 0) { message.warning('No hosts found in file'); return }
    // Reload from bridge so format is always normalised (ssh → hostname/user/port)
    const updated = await bridge.get_jump_hosts()
    setHosts(updated)
    message.success(`Imported ${imported.length} host${imported.length !== 1 ? 's' : ''}`)
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

  const buildGroups = (hostList: JumpHost[]) => {
    const map = new Map<string, JumpHost[]>()
    const order: string[] = []
    for (const h of hostList) {
      const g = h.group ?? ''
      if (!map.has(g)) { map.set(g, []); order.push(g) }
      map.get(g)!.push(h)
    }
    return order.map(g => ({ name: g, hosts: map.get(g)! }))
  }

  const moveGroupUp = async (groupIdx: number) => {
    const groups = buildGroups(hosts)
    if (groupIdx === 0) return
    const next = [...groups];
    [next[groupIdx - 1], next[groupIdx]] = [next[groupIdx], next[groupIdx - 1]]
    await persist(next.flatMap(g => g.hosts))
  }

  const moveGroupDown = async (groupIdx: number) => {
    const groups = buildGroups(hosts)
    if (groupIdx === groups.length - 1) return
    const next = [...groups];
    [next[groupIdx], next[groupIdx + 1]] = [next[groupIdx + 1], next[groupIdx]]
    await persist(next.flatMap(g => g.hosts))
  }

  const moveHostUp = async (h: JumpHost) => {
    const groups = buildGroups(hosts)
    const group = groups.find(g => g.hosts.some(x => x.name === h.name))!
    const idx = group.hosts.findIndex(x => x.name === h.name)
    if (idx === 0) return
    const next = [...group.hosts];
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]]
    await persist(groups.flatMap(g => g.name === group.name ? next : g.hosts))
  }

  const moveHostDown = async (h: JumpHost) => {
    const groups = buildGroups(hosts)
    const group = groups.find(g => g.hosts.some(x => x.name === h.name))!
    const idx = group.hosts.findIndex(x => x.name === h.name)
    if (idx === group.hosts.length - 1) return
    const next = [...group.hosts];
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]]
    await persist(groups.flatMap(g => g.name === group.name ? next : g.hosts))
  }

  const handleTest = async (name: string) => {
    setTestingHost(name)
    setTestResults(r => { const n = { ...r }; delete n[name]; return n })
    try {
      const result = await bridge.test_host(name)
      setTestResults(r => ({ ...r, [name]: result }))
    } finally {
      setTestingHost(null)
    }
  }

  const isEditing = editingKey !== null

  return (
    <div>
      <input
        ref={fileInputRef}
        type="file"
        accept=".toml"
        style={{ display: 'none' }}
        onChange={handleImport}
      />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Saved to <code>jump_hosts.toml</code>. Adding a host connects and discovers runnables automatically.
        </Text>
        <Space size={6}>
          <Button
            size="small"
            icon={<UploadOutlined />}
            onClick={() => fileInputRef.current?.click()}
            disabled={isEditing}
          >
            Import
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => { downloadToml(toToml(hosts)); message.success('Exported jump_hosts.toml') }}
            disabled={isEditing || hosts.length === 0}
          >
            Export
          </Button>
          <Button
            size="small"
            icon={<PlusOutlined />}
            onClick={startAdd}
            disabled={isEditing}
          >
            Add host
          </Button>
        </Space>
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
                <Input placeholder="prod-eu-1" style={{ fontFamily: 'monospace' }} disabled={editingKey !== ''} />
              </Form.Item>
              <Form.Item name="hostname" label="Hostname" rules={[{ required: true, message: 'Required' }]}>
                <Input placeholder="hostname.company.com" style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Form.Item name="user" label="SSH user">
                <Input placeholder="inherits default" />
              </Form.Item>
              <Form.Item name="port" label="Port">
                <Input placeholder="22" type="number" />
              </Form.Item>
            </div>
            <Form.Item name="identityFile" label="Identity file">
              <Input placeholder="~/.ssh/id_ed25519 (leave blank for SSH default)" />
            </Form.Item>
            <Form.Item name="runspec_path" label="runspec path on host">
              <Input placeholder="/home/user/.venv/bin/runspec" style={{ fontFamily: 'monospace' }} />
            </Form.Item>
            <Form.Item name="group" label="Group">
              <Input placeholder="e.g. Production, Staging (optional — for sidebar organisation)" />
            </Form.Item>
            <Space style={{ marginTop: 2, marginBottom: 8 }}>
              <Button size="small" type="primary" icon={<CheckOutlined />} onClick={save}>Save</Button>
              <Button size="small" icon={<CloseOutlined />} onClick={cancel}>Cancel</Button>
            </Space>
          </Form>
        </div>
      )}

      {/* Host list — grouped */}
      {hosts.length === 0 && editingKey === null && (
        <Text type="secondary" style={{ fontSize: 12 }}>No jump hosts configured. Add one to get started.</Text>
      )}
      {(() => {
        const groups = buildGroups(hosts)
        return groups.map(({ name: groupName, hosts: groupHosts }, groupIdx) => (
          <div key={groupName || '__ungrouped__'} style={{ marginBottom: 12 }}>
            {(
              <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 6 }}>
                <Text type="secondary" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', flex: 1 }}>
                  {groupName || 'Hosts'}
                </Text>
                <Button
                  type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />}
                  onClick={() => moveGroupUp(groupIdx)}
                  disabled={isEditing || groupIdx === 0}
                  style={{ padding: '0 4px', color: '#555', height: 18 }}
                />
                <Button
                  type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />}
                  onClick={() => moveGroupDown(groupIdx)}
                  disabled={isEditing || groupIdx === groups.length - 1}
                  style={{ padding: '0 4px', color: '#555', height: 18 }}
                />
              </div>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {groupHosts.map((h, hostIdx) => (
                <div key={h.name} style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '7px 10px', borderRadius: 6,
                  border: '1px solid #2a2a2a', background: editingKey === h.name ? 'rgba(255,255,255,0.03)' : 'transparent',
                }}>
                  <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
                    <Button type="text" size="small" icon={<UpOutlined style={{ fontSize: 9 }} />}
                      onClick={() => moveHostUp(h)}
                      disabled={isEditing || hostIdx === 0}
                      style={{ padding: '0 3px', color: '#555', height: 14, lineHeight: 1 }}
                    />
                    <Button type="text" size="small" icon={<DownOutlined style={{ fontSize: 9 }} />}
                      onClick={() => moveHostDown(h)}
                      disabled={isEditing || hostIdx === groupHosts.length - 1}
                      style={{ padding: '0 3px', color: '#555', height: 14, lineHeight: 1 }}
                    />
                  </div>
                  <Text style={{ fontFamily: 'monospace', fontSize: 12, minWidth: 110, flexShrink: 0 }}>{h.name}</Text>
                  <Text type="secondary" title={`${h.user ? h.user + '@' : ''}${h.hostname}${h.port ? ':' + h.port : ''}`} style={{
                    fontFamily: 'monospace', fontSize: 11, flex: 1, minWidth: 0,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{h.user ? `${h.user}@` : ''}{h.hostname}{h.port ? `:${h.port}` : ''}</Text>
                  {(() => {
                    const r = testResults[h.name]
                    if (!r) return null
                    const ok = r.runspec_ok
                    const sshOnly = r.connected && !r.runspec_ok
                    const color = ok ? 'success' : sshOnly ? 'warning' : 'error'
                    const label = ok
                      ? `✓ ${r.runnable_count} runnable${r.runnable_count !== 1 ? 's' : ''}`
                      : sshOnly ? '✓ SSH · runspec failed' : '✗ failed'
                    const detail = r.stderr || r.stdout || `exit ${r.exit_code}`
                    return (
                      <Tooltip title={<pre style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', maxWidth: 340 }}>{detail}</pre>} placement="left">
                        <Tag color={color} style={{ margin: 0, cursor: 'default', fontSize: 11 }}>{label}</Tag>
                      </Tooltip>
                    )
                  })()}
                  <Tooltip title="Test connection">
                    <Button
                      type="text" size="small"
                      icon={testingHost === h.name ? <LoadingOutlined /> : <ApiOutlined />}
                      onClick={() => handleTest(h.name)}
                      disabled={isEditing || testingHost !== null}
                      style={{ padding: '0 4px', color: '#666' }}
                    />
                  </Tooltip>
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
        ))
      })()}
    </div>
  )
}

export function SettingsDrawer({ open, onClose, onHostsChanged }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width={480} open={open} onClose={onClose}>
      <Tabs
        size="small"
        items={[
          { key: 'general',    label: 'General',     children: <GeneralTab /> },
          { key: 'jumpHosts',  label: 'Jump Hosts',  children: <JumpHostsTab onHostsChanged={onHostsChanged} /> },
        ]}
      />
    </Drawer>
  )
}
