import { useEffect, useRef, useState } from 'react'
import { Modal, Segmented, Select, Button, Progress, Typography, Tag, message } from 'antd'
import { CloudUploadOutlined, CloudDownloadOutlined, SwapOutlined, FolderOpenOutlined } from '@ant-design/icons'
import type { Host, TransferProgress, TransferEnd } from '../bridge'
import { bridge } from '../bridge'
import { PathInput, BrowseModal } from './PathInput'
import { FilterPicker, humanBytes } from './FilterPicker'

const { Text } = Typography
const LOCAL = 'local'

type Direction = 'upload' | 'download'
type HostProg = { transferred: number; total: number; done?: boolean; ok?: boolean; error?: string | null }

export function TransferModal({ open, onClose, hosts, defaultRemoteHost }: {
  open: boolean
  onClose: () => void
  hosts: Host[]
  defaultRemoteHost?: string
}) {
  const remoteHosts = hosts.filter(h => h.name !== LOCAL)
  const [direction, setDirection] = useState<Direction>('download')
  const [selectedHosts, setSelectedHosts] = useState<string[]>([])
  const [sourcePaths, setSourcePaths] = useState<string[]>([])
  const [filterName, setFilterName] = useState('')
  const [filterArg, setFilterArg] = useState('')
  const [browseSourceOpen, setBrowseSourceOpen] = useState(false)
  const [destDir, setDestDir] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<TransferProgress | null>(null)
  const [hostProgress, setHostProgress] = useState<Record<string, HostProg>>({})
  const [result, setResult] = useState<{ ok: boolean; error?: string | null } | null>(null)
  const [destChecks, setDestChecks] = useState<Record<string, { exists: boolean; error?: string | null }> | null>(null)
  const [checking, setChecking] = useState(false)
  const idRef = useRef<string | null>(null)
  const checkRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const primaryHost = selectedHosts[0] ?? ''
  const isFanout = direction === 'upload' && selectedHosts.length > 1
  // Hosts the destination folder is missing on (flagged by the pre-flight check),
  // and the hosts a fan-out will actually push to (the rest).
  const missingHosts = isFanout && destChecks ? selectedHosts.filter(h => destChecks[h]?.exists === false) : []
  const validHosts = isFanout ? selectedHosts.filter(h => !destChecks || destChecks[h]?.exists !== false) : selectedHosts
  // upload = local → remote(s);  download = remote → local
  const sourceHost = direction === 'upload' ? LOCAL : primaryHost
  const destBrowseHost = direction === 'upload' ? primaryHost : LOCAL

  // Default the target host(s) once the modal opens and hosts have loaded — the
  // list is empty on first mount (async get_hosts), so a plain useState
  // initializer would leave the selector unset.
  useEffect(() => {
    if (!open) return
    setSelectedHosts(prev => {
      const valid = prev.filter(h => remoteHosts.some(r => r.name === h))
      if (valid.length) return valid
      const def = (defaultRemoteHost && defaultRemoteHost !== LOCAL && remoteHosts.some(r => r.name === defaultRemoteHost))
        ? defaultRemoteHost
        : (remoteHosts.find(r => r.connected)?.name ?? remoteHosts[0]?.name)
      return def ? [def] : []
    })
  }, [open, hosts])  // eslint-disable-line react-hooks/exhaustive-deps

  // Flipping direction swaps which side is local — clear the picked paths/progress.
  // The filter only applies to a download (remote source), so reset it too.
  useEffect(() => { setSourcePaths([]); setDestDir(''); setProgress(null); setHostProgress({}); setResult(null); setFilterName(''); setFilterArg('') }, [direction])

  // Fan-out pre-flight: does the destination folder exist on every target host?
  // (It's only browsed on the first host, so the others are unverified.)
  useEffect(() => {
    setDestChecks(null)
    if (!isFanout || !destDir) { setChecking(false); return }
    setChecking(true)
    const hostsSnapshot = [...selectedHosts]
    const dir = destDir
    if (checkRef.current) clearTimeout(checkRef.current)
    checkRef.current = setTimeout(async () => {
      try { setDestChecks(await bridge.check_dir_exists(hostsSnapshot, dir)) }
      catch { setDestChecks(null) }
      finally { setChecking(false) }
    }, 400)
    return () => { if (checkRef.current) clearTimeout(checkRef.current) }
  }, [isFanout, destDir, selectedHosts.join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferProgress
      if (d.id !== idRef.current) return
      if (d.host) {
        if (d.error) message.error(`${d.host} · ${d.file}: ${d.error}`)
        else setHostProgress(prev => ({ ...prev, [d.host!]: { transferred: d.transferred, total: d.total } }))
      } else if (d.error) {
        message.error(`${d.file}: ${d.error}`)
      } else {
        setProgress(d)
      }
    }
    const onEnd = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferEnd
      if (d.id !== idRef.current) return
      setRunning(false)
      setResult({ ok: d.ok, error: d.error })
      if (d.hosts) {
        setHostProgress(prev => {
          const next = { ...prev }
          for (const r of d.hosts!) next[r.host] = { ...(next[r.host] ?? { transferred: 0, total: 0 }), done: true, ok: r.ok, error: r.error }
          return next
        })
        if (d.ok) message.success(`Pushed to ${d.hosts.length} host${d.hosts.length === 1 ? '' : 's'}`)
        else message.error('One or more hosts failed')
      } else if (d.ok) {
        message.success(`Transferred ${d.files} file${d.files === 1 ? '' : 's'}`)
      } else {
        message.error(d.error || 'Transfer failed')
      }
    }
    window.addEventListener('runspec:transfer_progress', onProgress)
    window.addEventListener('runspec:transfer_end', onEnd)
    return () => {
      window.removeEventListener('runspec:transfer_progress', onProgress)
      window.removeEventListener('runspec:transfer_end', onEnd)
    }
  }, [])

  const start = async () => {
    setProgress(null)
    setResult(null)
    setHostProgress(isFanout ? Object.fromEntries(validHosts.map(h => [h, { transferred: 0, total: 0 }])) : {})
    const res = isFanout
      ? await bridge.start_fanout(sourcePaths, validHosts, destDir)
      : direction === 'upload'
        ? await bridge.start_transfer(LOCAL, sourcePaths, primaryHost, destDir)
        : await bridge.start_transfer(primaryHost, sourcePaths, LOCAL, destDir, filterName || undefined, filterArg || undefined)
    if (res.error || !res.id) {
      message.error(res.error || 'Could not start transfer')
      setHostProgress({})
      return
    }
    idRef.current = res.id
    setRunning(true)
  }

  const cancel = () => { if (idRef.current) bridge.cancel_transfer(idRef.current) }

  const canRun = (isFanout ? validHosts.length > 0 : selectedHosts.length > 0) && sourcePaths.length > 0 && !!destDir && !running
  const pct = progress && progress.total > 0 ? Math.round((progress.transferred / progress.total) * 100) : 0
  const fanRows = Object.keys(hostProgress)

  const label = (t: string, host: string) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
      <Text style={{ fontSize: 12 }}>{t}</Text>
      <Tag style={{ fontSize: 10, margin: 0 }}>{host || '—'}</Tag>
    </div>
  )

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={560}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><SwapOutlined /> Transfer files</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={onClose}>Close</Button>
          {running
            ? <Button danger onClick={cancel}>Cancel transfer</Button>
            : <Button type="primary" disabled={!canRun} onClick={start}>{isFanout ? `Push to ${validHosts.length} host${validHosts.length === 1 ? '' : 's'}` : 'Transfer'}</Button>}
        </div>
      }
    >
      {remoteHosts.length === 0 ? (
        <Text type="secondary">No remote hosts configured — add one to transfer files.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <Segmented
              value={direction}
              onChange={v => setDirection(v as Direction)}
              options={[
                { label: 'Download (remote → local)', value: 'download', icon: <CloudDownloadOutlined /> },
                { label: 'Upload (local → remote)', value: 'upload', icon: <CloudUploadOutlined /> },
              ]}
            />
            {direction === 'download' ? (
              <Select
                size="small"
                value={primaryHost || undefined}
                onChange={v => setSelectedHosts([v])}
                placeholder="Remote host"
                style={{ minWidth: 160 }}
                options={remoteHosts.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))}
              />
            ) : (
              <Select
                size="small"
                mode="multiple"
                value={selectedHosts}
                onChange={setSelectedHosts}
                placeholder="Target host(s) — pick several to fan out"
                style={{ minWidth: 240 }}
                maxTagCount="responsive"
                options={remoteHosts.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))}
              />
            )}
          </div>

          <div>
            {label('Source files', sourceHost)}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              {sourcePaths.map(p => (
                <Tag
                  key={p}
                  closable
                  title={p}
                  onClose={() => setSourcePaths(prev => prev.filter(x => x !== p))}
                  style={{ fontFamily: 'monospace', fontSize: 11 }}
                >
                  {p.split(/[\\/]/).pop()}
                </Tag>
              ))}
              <Button size="small" icon={<FolderOpenOutlined />} disabled={!sourceHost} onClick={() => setBrowseSourceOpen(true)}>
                {sourcePaths.length ? 'Add files' : 'Choose files'}
              </Button>
            </div>
          </div>
          {browseSourceOpen && (
            <BrowseModal
              host={sourceHost}
              multiSelect
              onPickMany={paths => setSourcePaths(prev => Array.from(new Set([...prev, ...paths])))}
              onClose={() => setBrowseSourceOpen(false)}
            />
          )}

          {direction === 'download' && (
            <FilterPicker filterName={filterName} filterArg={filterArg} onFilterName={setFilterName} onFilterArg={setFilterArg} />
          )}

          <div>
            {label('Destination folder', isFanout ? `${selectedHosts.length} hosts` : destBrowseHost)}
            <PathInput host={destBrowseHost} value={destDir} onChange={setDestDir} dirsOnly placeholder="destination folder" />
            {isFanout && (
              <Text type="secondary" style={{ fontSize: 10 }}>
                Browsing {primaryHost} — the same path is written to all {selectedHosts.length} hosts.
              </Text>
            )}
            {isFanout && destDir && (
              checking ? (
                <div><Text type="secondary" style={{ fontSize: 11 }}>Checking destination on hosts…</Text></div>
              ) : destChecks ? (
                missingHosts.length > 0 ? (
                  <div><Text type="warning" style={{ fontSize: 11 }}>⚠ Folder not found on {missingHosts.join(', ')} — these hosts will be skipped.</Text></div>
                ) : (
                  <div><Text type="success" style={{ fontSize: 11 }}>✓ Folder exists on all {selectedHosts.length} hosts.</Text></div>
                )
              ) : null
            )}
          </div>

          {fanRows.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {fanRows.map(h => {
                const hp = hostProgress[h]
                const p = hp.total > 0 ? Math.round((hp.transferred / hp.total) * 100) : (hp.done ? 100 : 0)
                const status = hp.done ? (hp.ok ? 'success' : 'exception') : 'active'
                return (
                  <div key={h} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Tag style={{ fontSize: 10, margin: 0, minWidth: 90, textAlign: 'center' }}>{h}</Tag>
                    <Progress percent={p} status={status} size="small" style={{ flex: 1, margin: 0 }} />
                  </div>
                )
              })}
            </div>
          ) : (running || progress) ? (
            <div>
              <Progress
                percent={progress?.indeterminate ? 100 : pct}
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
          ) : null}
          {result && !running && !isFanout && (
            <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 12 }}>
              {result.ok ? 'Done.' : `Failed: ${result.error}`}
            </Text>
          )}
        </div>
      )}
    </Modal>
  )
}
