import { useEffect, useRef, useState } from 'react'
import { Modal, Segmented, Select, Button, Progress, Typography, Tag, Input, message } from 'antd'
import { NodeIndexOutlined, PlusOutlined, FolderOpenOutlined } from '@ant-design/icons'
import type { Host, TransferProgress, TransferEnd, RelaySource, Credential } from '../bridge'
import { bridge } from '../bridge'
import { PathInput, BrowseModal } from './PathInput'
import { FilterPicker, humanBytes } from './FilterPicker'

const { Text } = Typography
const LOCAL = 'local'

// Relay: stream file(s) from one host to another *through* the console. The win
// for jump-host setups — pull a log from an ad-hoc target (reached via a jump,
// with your own creds) onto the jump's filesystem for a local runnable to use.
export function RelayModal({ open, onClose, hosts }: { open: boolean; onClose: () => void; hosts: Host[] }) {
  const remoteHosts = hosts.filter(h => h.name !== LOCAL)
  const [mode, setMode] = useState<'adhoc' | 'configured'>('adhoc')
  const [jump, setJump] = useState('')
  const [target, setTarget] = useState('')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [credentialId, setCredentialId] = useState('')
  const [creds, setCreds] = useState<Credential[]>([])
  const [configuredSource, setConfiguredSource] = useState('')
  const [destHost, setDestHost] = useState('')
  const [destDir, setDestDir] = useState('')
  const [sourcePaths, setSourcePaths] = useState<string[]>([])
  const [filterName, setFilterName] = useState('')
  const [filterArg, setFilterArg] = useState('')
  const [pathInput, setPathInput] = useState('')
  const [browseOpen, setBrowseOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<TransferProgress | null>(null)
  const [result, setResult] = useState<{ ok: boolean; error?: string | null } | null>(null)
  const idRef = useRef<string | null>(null)

  useEffect(() => {
    if (!open) return
    const first = remoteHosts.find(h => h.connected)?.name ?? remoteHosts[0]?.name ?? ''
    // Jump host is deliberately left blank on open — auto-picking the first
    // host silently used one that may refuse direct-tcpip forwarding
    // (AllowTcpForwarding no), surfacing as "Administratively prohibited" on
    // Browse before the target credentials are even consulted. Force an
    // explicit pick instead so it's obvious which host is being asked to
    // forward.
    setDestHost(prev => prev || first)
    setConfiguredSource(prev => prev || first)
    bridge.list_credentials().then(setCreds).catch(() => { /* feature optional */ })
  }, [open, hosts])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferProgress
      if (d.id !== idRef.current) return
      if (d.error) message.error(`${d.file}: ${d.error}`)
      else setProgress(d)
    }
    const onEnd = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferEnd
      if (d.id !== idRef.current) return
      setRunning(false)
      setResult({ ok: d.ok, error: d.error })
      if (d.ok) message.success(`Relayed ${d.files} file${d.files === 1 ? '' : 's'}`)
      else message.error(d.error || 'Relay failed')
    }
    window.addEventListener('runspec:transfer_progress', onProgress)
    window.addEventListener('runspec:transfer_end', onEnd)
    return () => {
      window.removeEventListener('runspec:transfer_progress', onProgress)
      window.removeEventListener('runspec:transfer_end', onEnd)
    }
  }, [])

  const addPath = () => {
    const p = pathInput.trim()
    if (!p) return
    setSourcePaths(prev => Array.from(new Set([...prev, p])))
    setPathInput('')
  }

  const sourceValid = mode === 'adhoc' ? (!!target.trim() && !!jump) : !!configuredSource
  const canRun = sourceValid && sourcePaths.length > 0 && !!destHost && !!destDir && !running
  const pct = progress && progress.total > 0 ? Math.round((progress.transferred / progress.total) * 100) : 0

  const start = async () => {
    setProgress(null)
    setResult(null)
    const source: RelaySource = mode === 'adhoc'
      ? { kind: 'adhoc', host: target.trim(), user: user.trim() || undefined, jump, password: password || undefined, credential_id: credentialId || undefined }
      : { kind: 'host', host: configuredSource }
    const res = await bridge.start_relay(source, sourcePaths, destHost, destDir, filterName || undefined, filterArg || undefined)
    setPassword('')  // don't retain the password after handing it off
    if (res.error || !res.id) { message.error(res.error || 'Could not start relay'); return }
    idRef.current = res.id
    setRunning(true)
  }

  const cancel = () => { if (idRef.current) bridge.cancel_transfer(idRef.current) }

  // Closing the dialog tears down any cached ad-hoc browse connection and drops
  // the in-memory password.
  const handleClose = () => { bridge.close_adhoc().catch(() => {}); setPassword(''); setCredentialId(''); onClose() }

  const hostOptions = remoteHosts.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))

  return (
    <Modal
      open={open}
      onCancel={handleClose}
      width={600}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><NodeIndexOutlined /> Relay between hosts</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text type="secondary" style={{ fontSize: 11 }}>Bytes stream through this machine — the two hosts never connect directly.</Text>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={handleClose}>Close</Button>
            {running ? <Button danger onClick={cancel}>Cancel</Button> : <Button type="primary" disabled={!canRun} onClick={start}>Relay</Button>}
          </div>
        </div>
      }
    >
      {remoteHosts.length === 0 ? (
        <Text type="secondary">No remote hosts configured — add a jump host to relay files.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Text style={{ fontSize: 12 }}>Source</Text>
              <Segmented
                size="small"
                value={mode}
                onChange={v => setMode(v as 'adhoc' | 'configured')}
                options={[{ label: 'Ad-hoc via jump', value: 'adhoc' }, { label: 'Configured host', value: 'configured' }]}
              />
            </div>
            {mode === 'adhoc' ? (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Select size="small" value={jump || undefined} onChange={setJump} placeholder="Jump host" style={{ minWidth: 140 }} options={hostOptions} />
                <Input size="small" value={target} onChange={e => setTarget(e.target.value)} placeholder="target hostname" style={{ width: 200, fontFamily: 'monospace' }} />
                <Input size="small" value={user} onChange={e => setUser(e.target.value)} placeholder="user (optional)" style={{ width: 130 }} />
                {creds.length > 0 && (
                  <Select
                    size="small"
                    value={credentialId || undefined}
                    onChange={v => { setCredentialId(v ?? ''); if (v) setPassword('') }}
                    placeholder="saved credential"
                    style={{ width: 180 }}
                    allowClear
                    options={creds.map(c => ({ label: c.label, value: c.id }))}
                  />
                )}
                <Input.Password size="small" value={password} onChange={e => setPassword(e.target.value)} placeholder={credentialId ? 'using saved credential' : 'password — only if your key fails'} disabled={!!credentialId} autoComplete="new-password" style={{ width: 250 }} />
              </div>
            ) : (
              <Select size="small" value={configuredSource || undefined} onChange={setConfiguredSource} placeholder="Source host" style={{ minWidth: 180 }} options={hostOptions} />
            )}
          </div>

          <div>
            <Text style={{ fontSize: 12 }}>Source files <Text type="secondary" style={{ fontSize: 11 }}>(full paths on the source host)</Text></Text>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginTop: 6 }}>
              {sourcePaths.map(p => (
                <Tag key={p} closable title={p} onClose={() => setSourcePaths(prev => prev.filter(x => x !== p))} style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {p.split(/[\\/]/).pop()}
                </Tag>
              ))}
              <Input size="small" value={pathInput} onChange={e => setPathInput(e.target.value)} onPressEnter={addPath} placeholder="/var/log/app/today.log" style={{ width: 240, fontFamily: 'monospace' }} />
              <Button size="small" icon={<PlusOutlined />} onClick={addPath}>Add</Button>
              <Button
                size="small"
                icon={<FolderOpenOutlined />}
                disabled={mode === 'adhoc' ? (!jump || !target.trim()) : !configuredSource}
                onClick={() => setBrowseOpen(true)}
              >
                Browse
              </Button>
            </div>
          </div>
          {browseOpen && (
            <BrowseModal
              host={mode === 'adhoc' ? (target.trim() || 'source') : configuredSource}
              multiSelect
              lister={mode === 'adhoc'
                ? (path, dirsOnly) => bridge.list_dir_adhoc(jump, target.trim(), path, user.trim() || undefined, password || undefined, undefined, dirsOnly, credentialId || undefined)
                : undefined}
              onPickMany={paths => setSourcePaths(prev => Array.from(new Set([...prev, ...paths])))}
              onClose={() => setBrowseOpen(false)}
            />
          )}

          <FilterPicker filterName={filterName} filterArg={filterArg} onFilterName={setFilterName} onFilterArg={setFilterArg} />

          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Text style={{ fontSize: 12 }}>Destination</Text>
              <Select size="small" value={destHost || undefined} onChange={setDestHost} placeholder="Destination host" style={{ minWidth: 160 }} options={hostOptions} />
            </div>
            <PathInput host={destHost} value={destDir} onChange={setDestDir} dirsOnly placeholder="destination folder" />
          </div>

          {(running || progress) && (
            <div>
              <Progress
                percent={progress?.indeterminate ? (running ? 100 : 100) : pct}
                status={running ? 'active' : (result?.ok ? 'success' : 'exception')}
                showInfo={!progress?.indeterminate}
                size="small"
              />
              {progress && (
                <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
                  {progress.file}{progress.count > 1 ? ` (${progress.index + 1}/${progress.count})` : ''}
                  {progress.indeterminate ? ` · filtering… ${humanBytes(progress.transferred)} written` : ''}
                </Text>
              )}
            </div>
          )}
          {result && !running && (
            <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 12 }}>{result.ok ? 'Done.' : `Failed: ${result.error}`}</Text>
          )}
        </div>
      )}
    </Modal>
  )
}
