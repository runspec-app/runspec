// Settings → Memory Sync: where the knowledge wiki is shared. Its own repo and
// credential (never Config Sync's), so a wiki token can't reach config. A push
// writes only facts/<lane>.toml on contrib/<lane>; a pull reads main's facts/.
// Backed by bridge.get_memory_sync_config / save_memory_sync_config /
// memory_sync_test (bridge_memory_sync.py). The Wiki repo section seeds an
// empty repo from the OKF template and refreshes its generated runspec/ pages
// (memory_sync_create_repo / memory_sync_refresh_docs, okf.py).

import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Divider, Form, Input, Select, Space, Switch, Typography } from 'antd'
import { ApiOutlined, BookOutlined, SyncOutlined } from '@ant-design/icons'

import {
  bridge, type Credential, type MemorySyncConfig, type MemorySyncSettings,
  type WikiRefreshResult, type WikiRepoCreateResult,
} from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text, Paragraph } = Typography

type FormValues = Omit<MemorySyncSettings, 'credential_id'> & { credential_id?: string }

/** What the last Save / Test did, shown under the buttons until the next one. */
export interface SyncResult { ok: boolean; text: string }

/** A thrown bridge call becomes a readable line; a form-validation reject is null. */
export function errorText(e: unknown): string | null {
  if (e && typeof e === 'object' && 'errorFields' in e) return null
  return String((e as Error)?.message || e || 'Something went wrong.')
}

export function MemorySyncTab() {
  const [form] = Form.useForm<FormValues>()
  const [cfg, setCfg] = useState<MemorySyncConfig | null>(null)
  const [creds, setCreds] = useState<Credential[]>([])
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [user, setUser] = useState('')
  const [result, setResult] = useState<SyncResult | null>(null)

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
      email: (v.email ?? '').trim(), credential_id: v.credential_id || '', ca_bundle: (v.ca_bundle ?? '').trim(),
      tls_verify: v.tls_verify !== false,
    })
    if (!r.ok) { setResult({ ok: false, text: `Not saved: ${r.error || 'unknown error'}` }); return false }
    return true
  }

  const fail = (e: unknown) => {
    const text = errorText(e)
    if (text) setResult({ ok: false, text })
  }

  const save = async () => {
    setSaving(true); setResult(null)
    try {
      if (await persist()) { setResult({ ok: true, text: 'Saved.' }); load() }
    } catch (e) { fail(e) } finally { setSaving(false) }
  }

  const test = async () => {
    setTesting(true); setResult(null)
    try {
      if (!(await persist())) return
      load()
      const r = await bridge.memory_sync_test()
      if (!r.ok) setResult({ ok: false, text: `Could not reach the repo: ${r.error || 'unknown error'}` })
      else if (r.empty) setResult({ ok: true, text: 'Connected. The repo is empty; your first push creates your branch.' })
      else {
        const team = r.branch_exists ? 'the team branch exists' : 'the team branch does not exist yet'
        const mine = r.contrib_exists ? 'your branch exists' : 'your branch is created on your first push'
        setResult({ ok: true, text: `Connected: ${team}, ${mine}.` })
      }
    } catch (e) { fail(e) } finally { setTesting(false) }
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
        <Form.Item
          name="email" label="Commit email"
          extra="The email on your git account. GitLab can refuse a push whose author it doesn't recognise."
          rules={[{ pattern: /^[^\s<>@]+@[^\s<>]+$/, message: "That doesn't look like an email." }]}
        >
          <Input placeholder={cfg?.email_default} style={{ maxWidth: 360 }} />
        </Form.Item>
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
      {result && (
        <Alert
          type={result.ok ? 'success' : 'error'} showIcon closable onClose={() => setResult(null)}
          style={{ marginTop: 16 }} message={result.text}
        />
      )}
      <Divider style={{ margin: '24px 0 16px' }} />
      <WikiRepoSection save={persist} />
    </div>
  )
}

export function createText(r: WikiRepoCreateResult): SyncResult {
  if (!r.ok) return { ok: false, text: r.error || 'Could not create the wiki repo.' }
  if (r.review) {
    return {
      ok: true,
      text: `${r.base} already exists, so the template (${r.files} files) went to ${r.branch}. `
        + `Open a merge request from ${r.branch} into ${r.base} and merge it to set up the wiki.`,
    }
  }
  return { ok: true, text: `Created the wiki on ${r.branch} (${r.files} files). Open the repo to see the README.` }
}

export function refreshText(r: WikiRefreshResult): SyncResult {
  if (!r.ok) return { ok: false, text: r.error || 'Could not refresh the runspec pages.' }
  if (!r.pages) return { ok: true, text: 'The runspec pages are already up to date.' }
  if (r.changed === false) return { ok: true, text: `${r.branch} already has these changes; merge it to publish them.` }
  return { ok: true, text: `Pushed ${r.pages} changed page(s) to ${r.branch}. Merge it into the team branch to publish them.` }
}

/** Create a new wiki repo from the OKF template, or refresh its runspec/ pages.
 *  ``save`` writes the form first so the buttons act on what's on screen. */
export function WikiRepoSection({ save }: { save: () => Promise<boolean> }) {
  const [busy, setBusy] = useState<'' | 'create' | 'refresh'>('')
  const [result, setResult] = useState<SyncResult | null>(null)

  const run = async (which: 'create' | 'refresh') => {
    setBusy(which); setResult(null)
    try {
      if (!(await save())) return
      setResult(which === 'create'
        ? createText(await bridge.memory_sync_create_repo())
        : refreshText(await bridge.memory_sync_refresh_docs()))
    } catch (e) {
      const text = errorText(e)
      if (text) setResult({ ok: false, text })
    } finally { setBusy('') }
  }

  return (
    <div>
      <Text strong>Wiki repo</Text>
      <Paragraph type="secondary" style={{ marginTop: 4 }}>
        Start an empty repo from the wiki template: one Markdown page per thing in the Open
        Knowledge Format, a README, CI for GitLab and GitHub that lints changes and publishes
        a site from release tags, and pages about runspec generated from this console's
        documentation. If the team branch already exists it goes to <Text code>setup/wiki-template</Text> for a merge request instead. Refresh pushes
        updated runspec pages to <Text code>refresh/runspec-docs</Text> for review.
      </Paragraph>
      <Space wrap>
        <Button icon={<BookOutlined />} loading={busy === 'create'} disabled={!!busy && busy !== 'create'} onClick={() => run('create')}>
          Create wiki repo
        </Button>
        <Button icon={<SyncOutlined />} loading={busy === 'refresh'} disabled={!!busy && busy !== 'refresh'} onClick={() => run('refresh')}>
          Refresh runspec pages
        </Button>
      </Space>
      {result && (
        <Alert
          type={result.ok ? 'success' : 'error'} showIcon closable onClose={() => setResult(null)}
          style={{ marginTop: 16 }} message={result.text}
        />
      )}
    </div>
  )
}

/** Mirrors knowledge.lane_name: lower-case, [a-z0-9._-] only. */
export function laneOf(user: string): string {
  const lane = user.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[.\-_]+|[.\-_]+$/g, '')
  return lane.slice(0, 64) || 'user'
}
