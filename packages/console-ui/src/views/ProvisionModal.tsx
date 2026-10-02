import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Input, Checkbox, Select, Segmented, Tag, Alert, Collapse, Space, Tooltip, message } from 'antd'
import { CloudServerOutlined, ReloadOutlined, CheckCircleFilled, CloseCircleFilled, LoadingOutlined, WarningFilled } from '@ant-design/icons'
import {
  bridge,
  type ProvisionOptions,
  type ProvisionInterpreter,
  type ProvisionPayload,
  type ProvisionPreflightRow,
  type ProvisionResult,
} from '../bridge'
import { MONO_FONT, RADIUS, useIsDark, textMuted, textSecondary } from '../ThemeContext'
import { PathInput } from './PathInput'

const { Text } = Typography

type PyMode = 'path' | 'version'

interface HostProgress {
  step: string
  lines: string[]
  done: boolean
  ok?: boolean
  summary?: string
  warnings?: string[]
}

/** Join a parent directory and a venv folder name into an absolute POSIX path. */
export function joinDest(parent: string, name: string): string {
  const p = (parent || '').trim().replace(/\/+$/, '')
  const n = (name || '').trim().replace(/^\/+|\/+$/g, '')
  if (!p || !n) return ''
  return `${p}/${n}`
}

/** Host names a group / profile chip ticks (connected hosts only). */
export function membersOf(
  hosts: ProvisionOptions['hosts'],
  kind: 'group' | 'profile',
  name: string,
): string[] {
  return hosts
    .filter(h => h.connected && (kind === 'group' ? h.group === name : h.profiles.includes(name)))
    .map(h => h.name)
}

/** The provision request the form describes (shared by preflight and start). */
export function buildPayload(f: {
  hosts: string[]
  parent: string
  name: string
  pyMode: PyMode
  pyPath: string
  pyVersion: string
  runAs: string
  become: 'sudo' | 'su'
  packages: string[]
  repo: string
}): ProvisionPayload {
  return {
    hosts: f.hosts,
    dest: joinDest(f.parent, f.name),
    python: (f.pyMode === 'path' ? f.pyPath : f.pyVersion).trim(),
    run_as: f.runAs.trim(),
    become_method: f.become,
    packages: f.packages.map(p => p.trim()).filter(Boolean),
    repo: f.repo || undefined,
  }
}

const STEP_LABEL: Record<string, string> = {
  preflight: 'checking',
  folder: 'creating folder as root',
  create: 'creating venv',
  pip: 'upgrading pip',
  install: 'installing packages',
  inbox: 'setting up logs inbox',
  verify: 'verifying',
}

// Provision a runspec venv on one or many hosts. Modelled on Copy key to all
// hosts: a host checklist (with group / profile quick-picks), one location,
// interpreter, run-as account and package list for every host, a preflight
// table that changes nothing, then a parallel run with per-host logs.
export function ProvisionModal({
  open,
  initialHosts,
  onClose,
  onDone,
}: {
  open: boolean
  initialHosts?: string[]
  onClose: () => void
  onDone?: () => void
}) {
  const isDark = useIsDark()
  const [opts, setOpts] = useState<ProvisionOptions | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [parent, setParent] = useState('/opt/venvs')
  const [name, setName] = useState('')
  const [pyMode, setPyMode] = useState<PyMode>('path')
  const [pyPath, setPyPath] = useState('')
  const [browsePy, setBrowsePy] = useState('')
  const [pyVersion, setPyVersion] = useState('3.12')
  const [pythons, setPythons] = useState<ProvisionInterpreter[]>([])
  const [pyLoading, setPyLoading] = useState(false)
  const [runAs, setRunAs] = useState('')
  const [become, setBecome] = useState<'sudo' | 'su'>('sudo')
  const [packages, setPackages] = useState<string[]>([])
  const [repo, setRepo] = useState('')
  const [checking, setChecking] = useState(false)
  const [rows, setRows] = useState<ProvisionPreflightRow[] | null>(null)
  const [op, setOp] = useState<string | null>(null)
  const [progress, setProgress] = useState<Record<string, HostProgress>>({})
  const [results, setResults] = useState<ProvisionResult[] | null>(null)
  const opRef = useRef<string | null>(null)

  const refHost = selected[0] ?? ''
  const running = op !== null && results === null

  // Reset on the closed→open transition only (see CopyKeyAllModal).
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setRows(null); setOp(null); opRef.current = null; setProgress({}); setResults(null)
      setPythons([]); setPyPath('')
      bridge.provision_options().then(o => {
        setOpts(o)
        const connected = new Set(o.hosts.filter(h => h.connected).map(h => h.name))
        setSelected((initialHosts ?? []).filter(h => connected.has(h)))
      }).catch(e => message.error(String(e)))
    }
    wasOpen.current = open
  }, [open, initialHosts])

  const loadPythons = useCallback(async (extra?: string) => {
    if (!refHost) return
    setPyLoading(true)
    try {
      const res = await bridge.provision_interpreters({ host: refHost, extra_paths: extra ? [extra] : [] })
      if (!res.ok) { message.error(res.error || 'Could not list interpreters'); return }
      setPythons(res.pythons)
      setPyPath(prev => {
        if (extra) return res.pythons.find(p => p.path === extra || p.real === extra)?.path ?? prev
        if (prev && res.pythons.some(p => p.path === prev)) return prev
        return res.pythons.find(p => p.runspec_ok)?.path ?? ''
      })
      if (extra && !res.pythons.some(p => p.path === extra || p.real === extra)) {
        message.warning(`${extra} didn't run as a Python interpreter on ${refHost}.`)
      }
    } catch (e) {
      message.error(String(e))
    } finally {
      setPyLoading(false)
    }
  }, [refHost])

  // The interpreter list follows the reference host (the first one ticked).
  useEffect(() => { if (open && refHost) loadPythons() }, [open, refHost, loadPythons])

  // Live progress for the running op.
  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as { op: string; host: string; phase: string; step?: string; line?: string; ok?: boolean; summary?: string; warnings?: string[] }
      if (!d || d.op !== opRef.current) return
      setProgress(prev => {
        const cur: HostProgress = prev[d.host] ?? { step: '', lines: [], done: false }
        const next = { ...cur }
        if (d.phase === 'step' && d.step) next.step = d.step
        if (d.phase === 'line' && d.line !== undefined) next.lines = [...cur.lines, d.line].slice(-400)
        if (d.phase === 'done') { next.done = true; next.ok = d.ok; next.summary = d.summary; next.warnings = d.warnings }
        return { ...prev, [d.host]: next }
      })
    }
    const onDoneEvt = (e: Event) => {
      const d = (e as CustomEvent).detail as { op: string; kind: string; results: ProvisionResult[] }
      if (!d || d.op !== opRef.current) return
      setResults(d.results)
      const ok = d.results.filter(r => r.ok).length
      if (ok === d.results.length) message.success(`Provisioned on ${ok} host${ok !== 1 ? 's' : ''}`)
      else message.warning(`Provisioned on ${ok} of ${d.results.length} hosts`)
      onDone?.()
    }
    window.addEventListener('runspec:provision_progress', onProgress)
    window.addEventListener('runspec:provision_done', onDoneEvt)
    return () => {
      window.removeEventListener('runspec:provision_progress', onProgress)
      window.removeEventListener('runspec:provision_done', onDoneEvt)
    }
  }, [onDone])

  const payload = useMemo(
    () => buildPayload({ hosts: selected, parent, name, pyMode, pyPath, pyVersion, runAs, become, packages, repo }),
    [selected, parent, name, pyMode, pyPath, pyVersion, runAs, become, packages, repo],
  )
  // Any change to the request invalidates an earlier preflight.
  useEffect(() => { if (!op) setRows(null) }, [payload, op])

  const formReady = payload.hosts.length > 0 && !!payload.dest && !!payload.python
  const readyHosts = (rows ?? []).filter(r => r.ok).map(r => r.host)

  const check = async () => {
    if (!formReady) return
    setChecking(true)
    try {
      const res = await bridge.provision_preflight(payload)
      setRows(res.rows)
    } catch (e) {
      message.error(String(e))
    } finally {
      setChecking(false)
    }
  }

  const start = async () => {
    if (readyHosts.length === 0) return
    try {
      setProgress({}); setResults(null)
      const res = await bridge.provision_start({ ...payload, hosts: readyHosts })
      opRef.current = res.op
      setOp(res.op)
    } catch (e) {
      message.error(String(e))
    }
  }

  const retryFailed = () => {
    const failed = (results ?? []).filter(r => !r.ok).map(r => r.host)
    setSelected(failed)
    setOp(null); opRef.current = null; setResults(null); setProgress({}); setRows(null)
  }

  const hosts = opts?.hosts ?? []
  const options = hosts.map(h => ({
    value: h.name,
    disabled: !h.connected,
    label: (
      <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
        {h.name}
        {!h.connected && <Text type="secondary" style={{ fontSize: 11 }}> (not connected)</Text>}
      </span>
    ),
  }))
  const addMembers = (kind: 'group' | 'profile', n: string) =>
    setSelected(prev => [...new Set([...prev, ...membersOf(hosts, kind, n)])])

  const pyOptions = pythons.map(p => ({
    value: p.path,
    label: (
      <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
        {p.path} <Text type="secondary" style={{ fontSize: 11 }}>{p.version}{p.real !== p.path ? ` → ${p.real}` : ''}</Text>
        {!p.runspec_ok && <Tag color="warning" style={{ marginLeft: 6, fontSize: 10 }}>too old for runspec</Tag>}
      </span>
    ),
  }))
  const pickedPy = pythons.find(p => p.path === pyPath)

  const label = (s: string) => <Text style={{ fontSize: 12, color: textSecondary(isDark) }}>{s}</Text>
  const box = { border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: '8px 10px' }

  const footer = (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
      {running && <Button onClick={() => op && bridge.provision_cancel(op)}>Cancel</Button>}
      {results && results.some(r => !r.ok) && <Button icon={<ReloadOutlined />} onClick={retryFailed}>Retry failed</Button>}
      <Button onClick={onClose}>Close</Button>
      {!op && <Button onClick={check} loading={checking} disabled={!formReady}>Check hosts</Button>}
      {!op && (
        <Button type="primary" onClick={start} disabled={readyHosts.length === 0}>
          Provision {readyHosts.length || ''} host{readyHosts.length !== 1 ? 's' : ''}
        </Button>
      )}
    </div>
  )

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={680}
      maskClosable={false}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><CloudServerOutlined /> Provision a runspec venv</span>}
      footer={footer}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, opacity: op ? 0.6 : 1, pointerEvents: op ? 'none' : 'auto' }}>
        <Text style={{ fontSize: 13 }}>
          Creates a Python venv on each selected host, installs runspec and your packages into it, sets up the logs inbox, and adds the new path to the host.
        </Text>

        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
            {label('Hosts')}
            {(opts?.groups ?? []).map(g => (
              <Tag key={`g:${g}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => addMembers('group', g)}>+ Group: {g}</Tag>
            ))}
            {(opts?.profiles ?? []).map(p => (
              <Tag key={`p:${p}`} style={{ cursor: 'pointer', margin: 0 }} onClick={() => addMembers('profile', p)}>+ Profile: {p}</Tag>
            ))}
            {selected.length > 0 && <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSelected([])}>Clear</Button>}
          </div>
          {hosts.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 13 }}>No remote hosts configured.</Text>
          ) : (
            <div style={{ ...box, maxHeight: 140, overflowY: 'auto' }}>
              <Checkbox.Group value={selected} onChange={v => setSelected(v as string[])} options={options} style={{ display: 'flex', flexDirection: 'column', gap: 2 }} />
            </div>
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
          <div>
            {label('Parent folder')}
            <PathInput host={refHost || 'host'} value={parent} onChange={setParent} dirsOnly browseDisabled={!refHost} placeholder="/opt/venvs" />
          </div>
          <div>
            {label('Venv name')}
            <Input value={name} onChange={e => setName(e.target.value)} placeholder="ops" style={{ fontFamily: MONO_FONT }} aria-label="Venv name" />
          </div>
        </div>
        {payload.dest && <Text style={{ fontSize: 12, color: textMuted(isDark), marginTop: -6 }}>Venv: <Text code>{payload.dest}</Text></Text>}

        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            {label('Python')}
            <Segmented
              size="small"
              value={pyMode}
              onChange={v => setPyMode(v as PyMode)}
              options={[{ label: 'Pick an interpreter', value: 'path' }, { label: 'Same version on each host', value: 'version' }]}
            />
          </div>
          {pyMode === 'path' ? (
            <Space.Compact style={{ width: '100%' }}>
              <Select
                style={{ flex: 1 }}
                value={pyPath || undefined}
                onChange={setPyPath}
                options={pyOptions}
                loading={pyLoading}
                disabled={!refHost}
                placeholder={refHost ? `Interpreters on ${refHost}` : 'Tick a host first'}
                notFoundContent={pyLoading ? <LoadingOutlined /> : 'None found; browse for one'}
              />
              <div style={{ width: 220 }}>
                <PathInput host={refHost || 'host'} value={browsePy} onChange={setBrowsePy} browseDisabled={!refHost} placeholder="or browse to one…" />
              </div>
              <Tooltip title="Check this interpreter on the host and pick it">
                <Button disabled={!browsePy.trim().startsWith('/')} onClick={() => { loadPythons(browsePy.trim()); setBrowsePy('') }}>Use</Button>
              </Tooltip>
            </Space.Compact>
          ) : (
            <Input value={pyVersion} onChange={e => setPyVersion(e.target.value)} placeholder="3.12" style={{ width: 120, fontFamily: MONO_FONT }} aria-label="Python version" />
          )}
          <Text style={{ fontSize: 11, color: textMuted(isDark) }}>
            {pyMode === 'path'
              ? `Uses this path on every host. runspec needs Python ${opts?.min_python ?? '3.10'} or newer.`
              : 'Each host uses its own interpreter of this version (e.g. /usr/bin/python3.12 on one, /opt/python312/bin/python3.12 on another).'}
          </Text>
          {pyMode === 'path' && pickedPy && !pickedPy.runspec_ok && (
            <Alert type="warning" showIcon style={{ marginTop: 6 }} message={`Python ${pickedPy.version} is too old to run runspec; pick ${opts?.min_python ?? '3.10'} or newer.`} />
          )}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 110px', gap: 8 }}>
          <div>
            {label('Run as (owner of the venv)')}
            <Input value={runAs} onChange={e => setRunAs(e.target.value)} placeholder="the SSH user, or a service account" style={{ fontFamily: MONO_FONT }} aria-label="Run as" />
          </div>
          <div>
            {label('Become via')}
            <Select value={become} onChange={setBecome} options={[{ value: 'sudo', label: 'sudo' }, { value: 'su', label: 'su' }]} style={{ width: '100%' }} disabled={!runAs.trim()} />
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
          <div>
            {label('Packages (runspec is always installed)')}
            <Select mode="tags" value={packages} onChange={setPackages} placeholder="runspec-linux, runspec-fs…" style={{ width: '100%' }} tokenSeparators={[',', ' ']} open={false} suffixIcon={null} />
          </div>
          <div>
            {label('Package index')}
            <Select
              value={repo}
              onChange={setRepo}
              style={{ width: '100%' }}
              options={[{ value: '', label: "Host's own pip config" }, ...(opts?.repos ?? []).map(r => ({ value: r.id, label: r.name }))]}
            />
          </div>
        </div>
      </div>

      {rows && !op && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {label(`Preflight: ${readyHosts.length} of ${rows.length} ready (nothing has been changed yet)`)}
          {rows.map(r => (
            <div key={r.host} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
              {r.ok ? <CheckCircleFilled style={{ color: '#52c41a', marginTop: 3 }} /> : <CloseCircleFilled style={{ color: '#ff4d4f', marginTop: 3 }} />}
              <span style={{ fontFamily: MONO_FONT, minWidth: 110 }}>{r.host}</span>
              <span style={{ flex: 1, minWidth: 0 }}>
                {r.python && <Text style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{r.python} {r.version && `(${r.version})`}</Text>}
                {r.user && <Text type="secondary" style={{ fontSize: 11 }}> as {r.user}</Text>}
                {r.blockers.map(b => <div key={b}><Text type="danger" style={{ fontSize: 12 }}>{b}</Text></div>)}
                {r.warnings.map(w => <div key={w}><Text type="warning" style={{ fontSize: 12 }}><WarningFilled /> {w}</Text></div>)}
              </span>
            </div>
          ))}
        </div>
      )}

      {op && (
        <Collapse
          size="small"
          style={{ marginTop: 14 }}
          items={Object.keys(progress).length === 0
            ? [{ key: 'wait', label: <Text type="secondary"><LoadingOutlined /> Starting…</Text>, children: null }]
            : Object.entries(progress).map(([host, p]) => ({
              key: host,
              label: (
                <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {!p.done ? <LoadingOutlined /> : p.ok ? <CheckCircleFilled style={{ color: '#52c41a' }} /> : <CloseCircleFilled style={{ color: '#ff4d4f' }} />}
                  <span style={{ fontFamily: MONO_FONT }}>{host}</span>
                  <Text type="secondary" style={{ fontSize: 12 }}>{p.done ? p.summary : STEP_LABEL[p.step] ?? p.step}</Text>
                  {(p.warnings ?? []).length > 0 && <Tooltip title={(p.warnings ?? []).join('\n')}><WarningFilled style={{ color: '#faad14' }} /></Tooltip>}
                </span>
              ),
              children: (
                <pre style={{ margin: 0, maxHeight: 220, overflow: 'auto', fontFamily: MONO_FONT, fontSize: 11, whiteSpace: 'pre-wrap' }}>
                  {p.lines.join('\n') || '…'}
                </pre>
              ),
            }))}
        />
      )}
    </Modal>
  )
}
