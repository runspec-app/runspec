/**
 * LogCleanupModal — a venv's log retention policy (runspec >= 0.80.0): what it
 * is, when it last ran, change or remove it, and preview / apply it now.
 *
 * The policy lives in `{venv}/runspec_logs_retention.toml`; the venv owner's own
 * runs apply it at most once a day. Every change runs the venv's own
 * `runspec logs retention` as the venv owner (via sudo as the owner, else root;
 * core drops root to the owner), and core records it on the venv's audit ledger.
 */
import { useCallback, useEffect, useState } from 'react'
import { Modal, Button, Alert, Descriptions, Input, Space, Spin, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { bridge, type LogRetentionPolicy, type LogRetentionResult, type LogRetentionRunResult } from '../bridge'
import { MONO_FONT } from '../ThemeContext'
import { retentionOff, retentionProblem } from './ProvisionModal'
import { humanBytes } from './RunsIndexModal'
import { fmtWhen, plural } from '../when'
import { useRiskConfirm } from '../components/risk'

const { Text } = Typography

export interface LogCleanupTarget { host: string; group: string }

type Form = Required<LogRetentionPolicy>

const EMPTY: Form = { compact_after: '', delete_after: '', max_total_size: '' }
const SIZE_RE = /^\d+(\.\d+)?\s*(b|kb|mb|gb)?$/i

/** What's wrong with the form, or '' (all blank = remove the policy). */
export function cleanupProblem(f: Form): string {
  const c = retentionOff(f.compact_after) ? '' : f.compact_after.trim()
  const d = retentionOff(f.delete_after) ? '' : f.delete_after.trim()
  const m = retentionOff(f.max_total_size) ? '' : f.max_total_size.trim()
  if (!c && !d && !m) return ''
  if (c || d) {
    const p = retentionProblem({ compact_after: c, delete_after: d })
    if (p) return p
  }
  if (m && !SIZE_RE.test(m)) return `"${m}" isn't a size: use e.g. 500MB or 5GB.`
  return ''
}

/** One line for a policy: "compress after 7d, delete after 90d, keep at most 5GB". */
export function policyText(p: LogRetentionPolicy | null | undefined): string {
  if (!p) return ''
  return [
    p.compact_after ? `compress after ${p.compact_after}` : '',
    p.delete_after ? `delete after ${p.delete_after}` : '',
    p.max_total_size ? `keep at most ${p.max_total_size}` : '',
  ].filter(Boolean).join(', ')
}

const same = (f: Form, p: LogRetentionPolicy | null | undefined) =>
  (['compact_after', 'delete_after', 'max_total_size'] as const).every(k => (retentionOff(f[k]) ? '' : f[k].trim().toLowerCase()) === (p?.[k] ?? '').toLowerCase())

export function LogCleanupModal({ target, onClose }: { target: LogCleanupTarget | null; onClose: () => void }) {
  const [info, setInfo] = useState<LogRetentionResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState<Form>(EMPTY)
  const [busy, setBusy] = useState<'' | 'save' | 'off' | 'preview' | 'apply'>('')
  const [error, setError] = useState('')
  const [preview, setPreview] = useState('')

  const fill = (r: LogRetentionResult) => {
    setInfo(r)
    setForm({ ...EMPTY, ...(r.policy ?? {}) })
  }
  const load = useCallback((t: LogCleanupTarget) => {
    setLoading(true)
    Promise.resolve(bridge.logs_retention(t.host, t.group))
      .then(fill)
      .catch(e => setInfo({ ok: false, group: t.group, error: String(e) }))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    setInfo(null); setError(''); setPreview(''); setForm(EMPTY); setBusy('')
    if (target) load(target)
  }, [target, load])

  if (!target) return null
  const { host, group } = target
  const problem = cleanupProblem(form)
  const val = (v: string) => (retentionOff(v) ? '' : v.trim())
  const blank = !val(form.compact_after) && !val(form.delete_after) && !val(form.max_total_size)
  const unchanged = same(form, info?.policy)
  const hasPolicy = !!info?.policy

  const save = async (off: boolean) => {
    setBusy(off ? 'off' : 'save'); setError(''); setPreview('')
    try {
      const payload = off ? { off: true } : {
        compact_after: val(form.compact_after), delete_after: val(form.delete_after), max_total_size: val(form.max_total_size),
      }
      const r = await bridge.logs_retention_set(host, group, payload)
      if (!r.ok) { setError(r.error || 'Saving the policy failed.'); return }
      const as = r.as_user ? ` (as ${r.as_user})` : ''
      message.success(r.policy ? `Log cleanup for ${group}: ${policyText(r.policy)}${as}.` : `Removed the log cleanup policy for ${group}${as}.`)
      load(target)
    } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

  const [askRisk, riskHolder] = useRiskConfirm()
  // Deleting logs can't be undone (the risk ladder's destructive level): run
  // the same pass dry first, show what it would remove, and have the venv's
  // name typed before the real pass.
  const applyNow = async () => {
    setBusy('preview'); setError(''); setPreview('')
    let r: LogRetentionRunResult
    try { r = await bridge.logs_retention_run(host, group, true) } catch (e) { setError(String(e)); setBusy(''); return }
    setBusy('')
    if (!r.ok) { setError(r.error || 'The cleanup pass failed.'); return }
    if (r.skipped || (!(r.compacted ?? 0) && !(r.deleted ?? 0))) {
      setPreview(r.skipped ? (r.error || `Nothing to do (${r.skipped}).`) : 'Nothing to compress or delete right now.')
      return
    }
    const a = await askRisk({
      level: 'destructive',
      title: `Clean up the logs of ${group} now?`,
      action: `Compress ${plural(r.compacted ?? 0, 'run file')}\nDelete ${plural(r.deleted ?? 0, 'file')} (${humanBytes(r.freed_bytes)} freed)`,
      targets: [`${host} / ${group}`],
      consequence: 'Runs as the venv owner now, instead of waiting for the next daily pass. Deleted run files are gone from History.',
      typeToConfirm: group,
      okText: 'Apply now',
    })
    if (a.ok) await runNow(false)
  }

  const runNow = async (dryRun: boolean) => {
    setBusy(dryRun ? 'preview' : 'apply'); setError(''); setPreview('')
    try {
      const r = await bridge.logs_retention_run(host, group, dryRun)
      if (!r.ok) { setError(r.error || 'The cleanup pass failed.'); return }
      if (r.skipped) { setPreview(r.error || `Nothing to do (${r.skipped}).`); return }
      const line = dryRun
        ? `Right now it would compress ${plural(r.compacted ?? 0, 'run file')} and delete ${r.deleted ?? 0} (${humanBytes(r.freed_bytes)} freed).`
        : `Compressed ${plural(r.compacted ?? 0, 'run file')}, deleted ${r.deleted ?? 0} (${humanBytes(r.freed_bytes)} freed).`
      if (dryRun) setPreview(line)
      else { message.success(line); load(target) }
    } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

  const field = (key: keyof Form, label: string, placeholder: string, help: string) => (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 140, fontWeight: 500 }}>{label}</span>
        <Input size="small" aria-label={label} value={form[key]} placeholder={placeholder}
          onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
          style={{ width: 140, fontFamily: MONO_FONT }} />
      </div>
      <Text type="secondary" style={{ fontSize: 12, marginLeft: 148, display: 'block' }}>{help}</Text>
    </div>
  )

  const last = info?.last
  return (
    <Modal open width={620} onCancel={onClose} destroyOnHidden
      title={<span>Log cleanup · <span style={{ fontFamily: MONO_FONT }}>{host} / {group}</span></span>}
      footer={[<Button key="close" onClick={onClose}>Close</Button>]}>
      <Text type="secondary" style={{ fontSize: 13 }}>
        The venv's own cleanup rule for its <Text code>logs/</Text>. The venv owner's runs apply it at most once a day:
        old run files are compressed into one archive per day, then deleted. Every change runs the venv's{' '}
        <Text code>runspec logs retention</Text> as the venv owner and is recorded on its audit ledger.
      </Text>

      <div style={{ marginTop: 14, minHeight: 60 }}>
        {loading && !info ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 20 }}><Spin /></div>
        ) : info && !info.ok ? (
          <Alert type={info.too_old ? 'warning' : 'error'} showIcon message="Couldn't read the policy" description={info.error} />
        ) : info ? (
          <Descriptions size="small" column={1} bordered title={<span style={{ fontSize: 14 }}>Now</span>}
            extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} aria-label="Reload" onClick={() => load(target)} />}>
            <Descriptions.Item label="Policy">
              {info.policy_error
                ? <Text type="danger">{info.policy_error}</Text>
                : hasPolicy ? policyText(info.policy) : <Text type="secondary">none: logs here grow until pruned by hand</Text>}
            </Descriptions.Item>
            <Descriptions.Item label="Last pass">
              {last
                ? <span>{fmtWhen(last.ts)}: compressed {last.compacted ?? 0}, deleted {last.deleted ?? 0} ({humanBytes(last.freed_bytes)} freed){last.error ? <Text type="danger">, failed: {last.error}</Text> : null}</span>
                : <Text type="secondary">{hasPolicy ? 'not yet (the owner\'s next run applies it)' : '—'}</Text>}
            </Descriptions.Item>
            {info.file && (
              <Descriptions.Item label="File"><span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{info.file}</span></Descriptions.Item>
            )}
          </Descriptions>
        ) : null}
      </div>

      {info?.ok && (
        <>
          <div style={{ marginTop: 16 }}>
            {field('compact_after', 'Compress after', '7d', 'Run files older than this go into a gzip archive (one per day). Blank or off = never compress.')}
            {field('delete_after', 'Delete after', '90d', 'Run files and archives older than this are deleted. Blank or off = never delete.')}
            {field('max_total_size', 'Keep at most', '5GB', 'Then the oldest are deleted until logs/ fits. Blank = no size cap.')}
            <Text type="secondary" style={{ fontSize: 12 }}>Durations like 7d, 2w or 72h; sizes like 500MB or 5GB. Clear every field to remove the policy.</Text>
          </div>
          {problem && <Alert type="error" showIcon style={{ marginTop: 10 }} message={problem} />}
          <Space wrap style={{ marginTop: 12 }}>
            <Button type="primary" disabled={!!problem || unchanged || busy !== ''} loading={busy === 'save' || busy === 'off'}
              onClick={() => save(blank)}>
              {blank && hasPolicy ? 'Remove policy' : 'Save'}
            </Button>
            <Button disabled={!hasPolicy || !unchanged || busy !== ''} loading={busy === 'preview'} onClick={() => runNow(true)}>
              Preview now
            </Button>
            <Button danger disabled={!hasPolicy || !unchanged || busy !== ''} loading={busy === 'apply'} onClick={() => { void applyNow() }}>
              Apply now…
            </Button>
          </Space>
          {hasPolicy && !unchanged && <div><Text type="secondary" style={{ fontSize: 12 }}>Save first to preview or apply the new values.</Text></div>}
          {preview && <Alert type="info" showIcon style={{ marginTop: 10 }} message={preview} />}
        </>
      )}
      {error && <Alert type="error" showIcon style={{ marginTop: 12 }} message={error} />}
      {riskHolder}
    </Modal>
  )
}
