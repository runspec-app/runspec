import { useEffect, useRef, useState } from 'react'
import { Modal, Segmented, Select, Button, Progress, Typography, Tag, message } from 'antd'
import { CloudUploadOutlined, CloudDownloadOutlined, SwapOutlined, FolderOpenOutlined } from '@ant-design/icons'
import type { Host, TransferProgress, TransferEnd } from '../bridge'
import { bridge } from '../bridge'
import { PathInput, BrowseModal } from './PathInput'

const { Text } = Typography
const LOCAL = 'local'

type Direction = 'upload' | 'download'

export function TransferModal({ open, onClose, hosts, defaultRemoteHost }: {
  open: boolean
  onClose: () => void
  hosts: Host[]
  defaultRemoteHost?: string
}) {
  const remoteHosts = hosts.filter(h => h.name !== LOCAL)
  const [direction, setDirection] = useState<Direction>('download')
  const [remoteHost, setRemoteHost] = useState<string>('')
  const [sourcePaths, setSourcePaths] = useState<string[]>([])
  const [browseSourceOpen, setBrowseSourceOpen] = useState(false)
  const [destDir, setDestDir] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<TransferProgress | null>(null)
  const [result, setResult] = useState<{ ok: boolean; error?: string | null } | null>(null)
  const idRef = useRef<string | null>(null)

  // upload = local → remote;  download = remote → local
  const sourceHost = direction === 'upload' ? LOCAL : remoteHost
  const destHost = direction === 'upload' ? remoteHost : LOCAL

  // Pick a default remote host once the modal opens and hosts have loaded — the
  // host list is empty on first mount (async get_hosts), so a plain useState
  // initializer would leave the dropdown unselected.
  useEffect(() => {
    if (!open) return
    setRemoteHost(prev => {
      if (remoteHosts.some(h => h.name === prev)) return prev
      if (defaultRemoteHost && defaultRemoteHost !== LOCAL && remoteHosts.some(h => h.name === defaultRemoteHost)) return defaultRemoteHost
      return remoteHosts.find(h => h.connected)?.name ?? remoteHosts[0]?.name ?? ''
    })
  }, [open, hosts])  // eslint-disable-line react-hooks/exhaustive-deps

  // A change of direction or host invalidates the picked paths.
  useEffect(() => { setSourcePaths([]); setDestDir(''); setProgress(null); setResult(null) }, [direction, remoteHost])

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
      if (d.ok) message.success(`Transferred ${d.files} file${d.files === 1 ? '' : 's'}`)
      else message.error(d.error || 'Transfer failed')
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
    const res = await bridge.start_transfer(sourceHost, sourcePaths, destHost, destDir)
    if (res.error || !res.id) {
      message.error(res.error || 'Could not start transfer')
      return
    }
    idRef.current = res.id
    setRunning(true)
  }

  const cancel = () => { if (idRef.current) bridge.cancel_transfer(idRef.current) }

  const canRun = !!remoteHost && sourcePaths.length > 0 && !!destDir && !running
  const pct = progress && progress.total > 0 ? Math.round((progress.transferred / progress.total) * 100) : 0

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
            : <Button type="primary" disabled={!canRun} onClick={start}>Transfer</Button>}
        </div>
      }
    >
      {remoteHosts.length === 0 ? (
        <Text type="secondary">No remote hosts configured — add one to transfer files.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <Segmented
              value={direction}
              onChange={v => setDirection(v as Direction)}
              options={[
                { label: 'Download (remote → local)', value: 'download', icon: <CloudDownloadOutlined /> },
                { label: 'Upload (local → remote)', value: 'upload', icon: <CloudUploadOutlined /> },
              ]}
            />
            <Select
              size="small"
              value={remoteHost || undefined}
              onChange={setRemoteHost}
              placeholder="Remote host"
              style={{ minWidth: 140 }}
              options={remoteHosts.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))}
            />
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
              <Button size="small" icon={<FolderOpenOutlined />} onClick={() => setBrowseSourceOpen(true)}>
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

          <div>
            {label('Destination folder', destHost)}
            <PathInput host={destHost} value={destDir} onChange={setDestDir} dirsOnly placeholder="destination folder" />
          </div>

          {(running || progress) && (
            <div>
              <Progress percent={pct} status={running ? 'active' : (result?.ok ? 'success' : 'exception')} size="small" />
              {progress && (
                <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
                  {progress.file}{progress.count > 1 ? ` (${progress.index + 1}/${progress.count})` : ''}
                </Text>
              )}
            </div>
          )}
          {result && !running && (
            <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 12 }}>
              {result.ok ? 'Done.' : `Failed: ${result.error}`}
            </Text>
          )}
        </div>
      )}
    </Modal>
  )
}
