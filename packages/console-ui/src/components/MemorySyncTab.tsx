// Settings → Memory Sync: where the knowledge wiki is shared. Its own repo and
// credential (never Config Sync's), so a wiki token can't reach config. A push
// writes only facts/<lane>.toml on contrib/<lane>; a pull reads main's facts/.
// Backed by bridge.get_memory_sync_config / save_memory_sync_config /
// memory_sync_test (bridge_memory_sync.py).

import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Divider, Form, Input, Select, Space, Switch, Typography, message } from 'antd'
import { ApiOutlined } from '@ant-design/icons'

import { bridge, type Credential, type MemorySyncConfig, type MemorySyncSettings } from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text, Paragraph } = Typography

type FormValues = Omit<MemorySyncSettings, 'credential_id'> & { credential_id?: string }

export function MemorySyncTab() {
  const [form] = Form.useForm<FormValues>()
  const [cfg, setCfg] = useState<MemorySyncConfig | null>(null)
  const [creds, setCreds] = useState<Credential[]>([])
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [user, setUser] = useState('')

  const load = useCallback(() => {
    bridge.get_memory_sync_config().then(c => {
      setCfg(c)
      setUser(c.config.user)
      form.setFieldsValue({ ...c.config, credential_id: c.config.credential_id || undefined })
    })
  }, [form])
  useEffect(() => { load() }, [load])
  // Same as Config Sync: pick up credentials added on the Credentials tab live.
  useEffect(() => {
    const reload = () => { bridge.list_credentials().then(setCreds) }
    reload()
    window.addEventListener('runspec:credentials-changed', reload)
    return () => window.removeEventListener('runspec:credentials-changed', reload)
  }, [])

  const persist = async (): Promise<boolean> => {
    const v = await form.validateFields()
    const r = await bridge.save_memory_sync_config({
      url: (v.url ?? '').trim(), branch: (v.branch ?? '').trim(), user: (v.user ?? '').trim(),
      credential_id: v.credential_id || '', ca_bundle: (v.ca_bundle ?? '').trim(),
      tls_verify: v.tls_verify !== false,
    })
    if (!r.ok) { message.error(r.error || 'Could not save.'); return false }
    return true
  }

  const save = async () => {
    setSaving(true)
    try {
      if (await persist()) { message.success('Memory Sync saved.'); load() }
    } catch { /* validation shown inline */ } finally { setSaving(false) }
  }

  const test = async () => {
    setTesting(true)
    try {
      if (!(await persist())) return
      load()
      const r = await bridge.memory_sync_test()
      if (!r.ok) message.error(r.error || 'Could not reach the repo.')
      else if (r.empty) message.success('Connected. The repo is empty; your first push creates your branch.')
      else {
        const team = r.branch_exists ? 'the team branch exists' : 'the team branch does not exist yet'
        const mine = r.contrib_exists ? 'your branch exists' : 'your branch is created on your first push'
        message.success(`Connected: ${team}, ${mine}.`)
      }
    } catch { /* validation shown inline */ } finally { setTesting(false) }
  }

  const lane = cfg ? (user.trim() ? laneOf(user) : cfg.lane) : ''

  return (
    <div style={{ maxWidth: 640 }}>
      <Paragraph type="secondary">
        Share your approved wiki facts with the team through a git repo of their own. Pushing
        writes one file on your own branch; nothing reaches the team until someone merges that
        branch on the repo. Pulling brings in everyone's merged facts. Use a separate repo and a
        token that can write to it, not your Config Sync credential.
      </Paragraph>
      {cfg && !cfg.engine && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 16 }}
          message="The git engine isn't installed"
          description="Install the git-sync extra (pip install runspec-console[git-sync]) to use Memory Sync."
        />
      )}
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="url" label="Wiki repository" extra="HTTPS or SSH URL of an empty repo kept for the wiki.">
          <Input placeholder="https://gitlab.example.com/ops/wiki.git" style={{ fontFamily: MONO_FONT }} />
        </Form.Item>
        <Form.Item name="credential_id" label="Credential" extra="A token that can push to the repo (Settings → Credentials). Over SSH, an ssh-key credential or your SSH key.">
          <Select
            allowClear placeholder="None"
            options={creds.map(c => ({ value: c.id, label: `${c.label || c.id} (${c.kind})` }))}
          />
        </Form.Item>
        <Space size={16} wrap style={{ width: '100%' }} align="start">
          <Form.Item name="branch" label="Team branch" extra="What a pull reads." style={{ width: 220 }}>
            <Input placeholder="main" style={{ fontFamily: MONO_FONT }} />
          </Form.Item>
          <Form.Item name="user" label="Your name in the wiki" extra={cfg ? `Blank uses ${cfg.user_default}.` : undefined} style={{ width: 260 }}>
            <Input placeholder={cfg?.user_default} onChange={e => setUser(e.target.value)} />
          </Form.Item>
        </Space>
        {lane && (
          <Paragraph type="secondary" style={{ marginTop: -8 }}>
            Pushes write <Text code>facts/{lane}.toml</Text> on the branch <Text code>contrib/{lane}</Text>.
          </Paragraph>
        )}
        <Form.Item name="ca_bundle" label="CA bundle (HTTPS)" extra="A PEM file for an internal CA or a TLS-inspecting proxy.">
          <Input placeholder="optional" style={{ fontFamily: MONO_FONT }} />
        </Form.Item>
        <Form.Item name="tls_verify" label="Verify TLS certificates" valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
      <Divider style={{ margin: '16px 0' }} />
      <Space>
        <Button type="primary" loading={saving} onClick={save}>Save</Button>
        <Button icon={<ApiOutlined />} loading={testing} onClick={test}>Test connection</Button>
      </Space>
    </div>
  )
}

/** Mirrors knowledge.lane_name: lower-case, [a-z0-9._-] only. */
export function laneOf(user: string): string {
  const lane = user.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[.\-_]+|[.\-_]+$/g, '')
  return lane.slice(0, 64) || 'user'
}
