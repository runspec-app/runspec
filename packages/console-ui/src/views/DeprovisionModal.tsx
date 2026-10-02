import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Input, Checkbox, Select, Tag, Alert, Spin, message } from 'antd'
import { DeleteOutlined, CheckCircleFilled, CloseCircleFilled, LoadingOutlined } from '@ant-design/icons'
import {
  bridge,
  type DeprovisionPreviewRow,
  type DeprovisionTarget,
  type ProvisionOptions,
  type ProvisionResult,
} from '../bridge'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'
import { membersOf } from './ProvisionModal'

const { Text } = Typography

/** ``/opt/venvs/ops/bin/runspec`` → ``/opt/venvs/ops``. */
export function venvRoot(runspecPath: string): string {
  return runspecPath.replace(/\/+$/, '').replace(/\/bin\/runspec$/, '').replace(/\/bin$/, '')
}

/** Every distinct POSIX venv the hosts list, with the hosts that list it. */
export function venvChoices(hosts: ProvisionOptions['hosts']): { root: string; hosts: { host: string; runspec_path: string }[] }[] {
  const map = new Map<string, { host: string; runspec_path: string }[]>()
  for (const h of hosts) {
    for (const rp of h.runspec_paths) {
      if (!rp.startsWith('/')) continue
      const root = venvRoot(rp)
      const list = map.get(root) ?? []
      list.push({ host: h.name, runspec_path: rp })
      map.set(root, list)
    }
  }
  return [...map.entries()].map(([root, hs]) => ({ root, hosts: hs })).sort((a, b) => a.root.localeCompare(b.root))
}

export function formatKb(kb: number): string {
  if (kb >= 1024 * 1024) return `${(kb / 1024 / 1024).toFixed(1)} GB`
  if (kb >= 1024) return `${(kb / 1024).toFixed(1)} MB`
  return `${kb} KB`
}

// Remove a runspec venv from one or many hosts. Only a venv a host already
// lists can be picked; a read-only preview shows each host's path, owner, size
// and runnables (and what may reference them); the Delete button stays
// disabled until the venv's folder name is typed. Keep logs (default on)
// removes everything except logs/.
export function DeprovisionModal({
  open,
  initialTargets,
  onClose,
  onDone,
  zIndex,
}: {
  open: boolean
  initialTargets?: DeprovisionTarget[]
  onClose: () => void
  onDone?: () => void
  // Stack above a modal it is opened from (Ant siblings share zIndex 1000,
  // so render order alone can leave this dialog hidden underneath).
  zIndex?: number
}) {
  const isDark = useIsDark()
  const [opts, setOpts] = useState<ProvisionOptions | null>(null)
  const [root, setRoot] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [preview, setPreview] = useState<DeprovisionPreviewRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [keepLogs, setKeepLogs] = useState(true)
  const [confirm, setConfirm] = useState('')
  const [op, setOp] = useState<string | null>(null)
  const [results, setResults] = useState<ProvisionResult[] | null>(null)
  const opRef = useRef<string | null>(null)

  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setPreview(null); setConfirm(''); setKeepLogs(true); setOp(null); opRef.current = null; setResults(null)
      const first = initialTargets?.[0]
      setRoot(first ? venvRoot(first.runspec_path) : '')
      setSelected((initialTargets ?? []).map(t => t.host))
      bridge.provision_options().then(setOpts).catch(e => message.error(String(e)))
    }
    wasOpen.current = open
  }, [open, initialTargets])

  const choices = useMemo(() => venvChoices(opts?.hosts ?? []), [opts])
  const choice = choices.find(c => c.root === root)
  const connected = useMemo(() => new Set((opts?.hosts ?? []).filter(h => h.connected).map(h => h.name)), [opts])
  const targets: DeprovisionTarget[] = useMemo(
    () => (choice?.hosts ?? []).filter(h => selected.includes(h.host)),
    [choice, selected],
  )
  const name = root.split('/').pop() ?? ''

  useEffect(() => {
    if (!open || targets.length === 0) { setPreview(null); return }
    let alive = true
    setLoading(true)
    bridge.deprovision_preview({ targets })
      .then(res => { if (alive) setPreview(res.rows) })
      .catch(e => { if (alive) message.error(String(e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, targets])

  useEffect(() => {
    const onDoneEvt = (e: Event) => {
      const d = (e as CustomEvent).detail as { op: string; results: ProvisionResult[] }
      if (!d || d.op !== opRef.current) return
      setResults(d.results)
      const ok = d.results.filter(r => r.ok).length
      if (ok === d.results.length) message.success(`Deprovisioned ${name} on ${ok} host${ok !== 1 ? 's' : ''}`)
      else message.warning(`Deprovisioned on ${ok} of ${d.results.length} hosts`)
      onDone?.()
    }
    window.addEventListener('runspec:provision_done', onDoneEvt)
    return () => window.removeEventListener('runspec:provision_done', onDoneEvt)
  }, [onDone, name])

  const removable = (preview ?? []).filter(r => r.ok)
  const canDelete = removable.length > 0 && confirm.trim() === name && !op

  const del = async () => {
    if (!canDelete) return
    try {
      const res = await bridge.deprovision_start({
        targets: removable.map(r => ({ host: r.host, runspec_path: r.runspec_path })),
        confirm: confirm.trim(),
        keep_logs: keepLogs,
      })
      opRef.current = res.op
      setOp(res.op)
    } catch (e) {
      message.error(String(e))
    }
  }

  const hostOptions = (choice?.hosts ?? []).map(h => ({
    value: h.host,
    disabled: !connected.has(h.host),
    label: <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{h.host}{!connected.has(h.host) && <Text type="secondary" style={{ fontSize: 11 }}> (not connected)</Text>}</span>,
  }))
  const inChoice = (hs: string[]) => hs.filter(h => choice?.hosts.some(c => c.host === h))
  const label = (s: string) => <Text style={{ fontSize: 12, color: textSecondary(isDark) }}>{s}</Text>
  const refs = [...new Set((preview ?? []).flatMap(r => r.references))]

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={640}
      zIndex={zIndex}
      maskClosable={false}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><DeleteOutlined /> Deprovision a runspec venv</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={onClose}>Close</Button>
          {!results && (
            <Button danger type="primary" disabled={!canDelete} loading={op !== null && !results} onClick={del}>
              Delete on {removable.length} host{removable.length !== 1 ? 's' : ''}
            </Button>
          )}
        </div>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div>
          {label('Venv')}
          <Select
            style={{ width: '100%' }}
            value={root || undefined}
            onChange={v => { setRoot(v); setSelected([]); setConfirm('') }}
            placeholder="Pick a venv the hosts already list"
            disabled={op !== null}
            options={choices.map(c => ({
              value: c.root,
              label: <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{c.root} <Text type="secondary" style={{ fontSize: 11 }}>on {c.hosts.length} host{c.hosts.length !== 1 ? 's' : ''}</Text></span>,
            }))}
          />
        </div>

        {choice && (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
              {label('Hosts')}
              {(opts?.groups ?? []).map(g => (
                <Tag key={`g:${g}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => setSelected(p => [...new Set([...p, ...inChoice(membersOf(opts?.hosts ?? [], 'group', g))])])}>+ Group: {g}</Tag>
              ))}
              {(opts?.profiles ?? []).map(p => (
                <Tag key={`p:${p}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => setSelected(s => [...new Set([...s, ...inChoice(membersOf(opts?.hosts ?? [], 'profile', p))])])}>+ Profile: {p}</Tag>
              ))}
            </div>
            <div style={{ border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: '8px 10px', maxHeight: 120, overflowY: 'auto' }}>
              <Checkbox.Group value={selected} onChange={v => setSelected(v as string[])} options={hostOptions} disabled={op !== null} style={{ display: 'flex', flexDirection: 'column', gap: 2 }} />
            </div>
          </div>
        )}

        {loading && <Spin size="small" />}
        {preview && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {preview.map(r => {
              const res = results?.find(x => x.host === r.host)
              return (
                <div key={r.host} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
                  {res ? (res.ok ? <CheckCircleFilled style={{ color: '#52c41a', marginTop: 3 }} /> : <CloseCircleFilled style={{ color: '#ff4d4f', marginTop: 3 }} />)
                    : op ? <LoadingOutlined style={{ marginTop: 3 }} />
                    : r.ok ? <CheckCircleFilled style={{ color: textMuted(isDark), marginTop: 3 }} /> : <CloseCircleFilled style={{ color: '#ff4d4f', marginTop: 3 }} />}
                  <span style={{ fontFamily: MONO_FONT, minWidth: 100 }}>{r.host}</span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <Text style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{r.root}</Text>
                    {r.ok && <Text type="secondary" style={{ fontSize: 11 }}> · owner {r.owner || '?'} · {formatKb(r.size_kb)} (logs {formatKb(r.logs_kb)})</Text>}
                    {r.runnables.length > 0 && <div><Text type="secondary" style={{ fontSize: 11 }}>Runnables: {r.runnables.join(', ')}</Text></div>}
                    {r.blockers.map(b => <div key={b}><Text type="danger" style={{ fontSize: 12 }}>{b}</Text></div>)}
                    {res && <div><Text type={res.ok ? 'secondary' : 'danger'} style={{ fontSize: 11 }}>{res.summary}</Text></div>}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        {refs.length > 0 && (
          <Alert type="warning" showIcon message={`Runnables in this venv may be used by your ${refs.join(', ')}. They will stop working on these hosts.`} />
        )}

        {removable.length > 0 && !results && (
          <>
            <Checkbox checked={keepLogs} onChange={e => setKeepLogs(e.target.checked)} disabled={op !== null}>
              Keep logs <Text type="secondary" style={{ fontSize: 12 }}>(the folder stays with only its <Text code>logs/</Text> inside; a new venv at the same path picks its history back up)</Text>
            </Checkbox>
            <Alert
              type="error"
              showIcon
              message={keepLogs
                ? `Everything in ${root} except logs/ will be deleted on ${removable.length} host${removable.length !== 1 ? 's' : ''}.`
                : `${root} and its logs will be deleted on ${removable.length} host${removable.length !== 1 ? 's' : ''}. This can't be undone.`}
            />
            <div>
              {label(`Please confirm: type ${name} to enable Delete`)}
              <Input value={confirm} onChange={e => setConfirm(e.target.value)} placeholder={name} style={{ fontFamily: MONO_FONT }} aria-label="Confirm venv name" disabled={op !== null} />
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}
