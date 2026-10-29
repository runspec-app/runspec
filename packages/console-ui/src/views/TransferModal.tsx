import { useEffect, useRef, useState } from 'react'
import { Modal, Select, Button, Progress, Typography, Tag, Input, Popconfirm, Tooltip, message } from 'antd'
import { SwapOutlined, FolderOpenOutlined, SaveOutlined, PlayCircleOutlined, EditOutlined, DeleteOutlined, UnorderedListOutlined } from '@ant-design/icons'
import type { Host, TransferProgress, TransferEnd, TransferEndpoint, RelaySource, Credential, SavedTransfer } from '../bridge'
import { bridge } from '../bridge'
import { PathInput, BrowseModal } from './PathInput'
import { FilterPicker, humanBytes } from './FilterPicker'
import { AdHocHostFields, emptyAdHoc, adHocValid, adHocToEndpoint, type AdHocState } from './AdHocHostFields'
import { MONO_FONT } from '../ThemeContext'
import { SaveTransferModal, SavedTransferRunModal, SavedTransfersListModal } from './SavedTransferModals'
import { remoteTarget } from './savedTransfers'

const { Text } = Typography
const LOCAL = 'local'

// Kind of one endpoint (source or destination). 'local' = this computer; 'host'
// = a configured fleet host; 'adhoc' = an SSH host not in the fleet, reached via
// a jump. A transfer needs one 'local' side; two remote sides is a relay (the
// bytes stream through this machine); an ad-hoc destination is only reachable
// from 'local' (relay always writes to a configured host).
type Kind = 'local' | 'host' | 'adhoc'
type HostProg = { transferred: number; total: number; done?: boolean; ok?: boolean; error?: string | null }

const KIND_OPTIONS = [
  { label: 'This computer', value: 'local' },
  { label: 'Configured host', value: 'host' },
  { label: 'Ad-hoc host', value: 'adhoc' },
]

// One modal for every SFTP move: download (host→local), upload (local→host, incl.
// fan-out to several), and relay (host→host, or an ad-hoc source via a jump).
// Pick a source and a destination — each This computer / a configured host / an
// ad-hoc host — and the right engine is chosen on submit.
export function TransferModal({ open, onClose, hosts, defaultRemoteHost }: {
  open: boolean
  onClose: () => void
  hosts: Host[]
  defaultRemoteHost?: string
}) {
  const remoteHosts = hosts.filter(h => h.name !== LOCAL)
  const hostOptions = remoteHosts.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))

  const [srcKind, setSrcKind] = useState<Kind>('host')
  const [srcHost, setSrcHost] = useState('')
  const [srcAdhoc, setSrcAdhoc] = useState<AdHocState>(emptyAdHoc)
  const [dstKind, setDstKind] = useState<Kind>('local')
  const [dstHosts, setDstHosts] = useState<string[]>([])
  const [dstAdhoc, setDstAdhoc] = useState<AdHocState>(emptyAdHoc)
  const [creds, setCreds] = useState<Credential[]>([])

  const [sourcePaths, setSourcePaths] = useState<string[]>([])
  const [destName, setDestName] = useState('')
  const [filterName, setFilterName] = useState('')
  const [filterArg, setFilterArg] = useState('')
  const [browseSourceOpen, setBrowseSourceOpen] = useState(false)
  const [destDir, setDestDir] = useState('')
  const [workspacePath, setWorkspacePath] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<TransferProgress | null>(null)
  const [hostProgress, setHostProgress] = useState<Record<string, HostProg>>({})
  const [result, setResult] = useState<{ ok: boolean; error?: string | null } | null>(null)
  const [destChecks, setDestChecks] = useState<Record<string, { exists: boolean; error?: string | null }> | null>(null)
  const [checking, setChecking] = useState(false)
  const idRef = useRef<string | null>(null)
  // Saved transfers (scenarios): the picker row, the save/edit dialog and the
  // run dialog. `editing` holds the draft (and the id being replaced, if any).
  const [saved, setSaved] = useState<SavedTransfer[]>([])
  const [savedId, setSavedId] = useState('')
  const [editing, setEditing] = useState<{ draft: SavedTransfer; replaceId?: string } | null>(null)
  const [runningSaved, setRunningSaved] = useState<SavedTransfer | null>(null)
  const [managing, setManaging] = useState(false)
  const checkRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Fan-out (one local source → several configured hosts) is the only many-target
  // shape; relay and ad-hoc are single-target.
  const isFanout = srcKind === 'local' && dstKind === 'host' && dstHosts.length > 1
  const bothLocal = srcKind === 'local' && dstKind === 'local'
  const bothRemote = srcKind !== 'local' && dstKind !== 'local'
  const relayDestUnsupported = bothRemote && dstKind !== 'host'

  const srcOk = srcKind === 'local' || (srcKind === 'host' ? !!srcHost : adHocValid(srcAdhoc))
  const dstOk = dstKind === 'local' || (dstKind === 'host' ? dstHosts.length > 0 : adHocValid(dstAdhoc))

  const missingHosts = isFanout && destChecks ? dstHosts.filter(h => destChecks[h]?.exists === false) : []
  const validHosts = isFanout ? dstHosts.filter(h => !destChecks || destChecks[h]?.exists !== false) : dstHosts

  // Default the endpoints once the modal opens and hosts have loaded (the list is
  // empty on first mount — a plain useState initializer would leave it unset).
  useEffect(() => {
    if (!open) return
    const first = (defaultRemoteHost && defaultRemoteHost !== LOCAL && remoteHosts.some(r => r.name === defaultRemoteHost))
      ? defaultRemoteHost
      : (remoteHosts.find(r => r.connected)?.name ?? remoteHosts[0]?.name ?? '')
    setSrcHost(prev => prev || first)
    bridge.list_credentials().then(setCreds).catch(() => { /* feature optional */ })
    // Where the agent works — default a local destination here so a download
    // lands in the workspace the agent can then act on ('' when no active chat).
    bridge.current_workspace_path().then(setWorkspacePath).catch(() => { /* optional */ })
  }, [open, hosts])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return
    const load = () => { bridge.get_saved_transfers().then(setSaved).catch(() => {}) }
    load()
    window.addEventListener('runspec:saved_transfers_changed', load)
    return () => window.removeEventListener('runspec:saved_transfers_changed', load)
  }, [open])

  // Changing which machine the source is clears the picked (host-specific) paths.
  useEffect(() => { setSourcePaths([]); setDestName(''); setProgress(null); setHostProgress({}); setResult(null); setFilterName(''); setFilterArg('') },
    [srcKind, srcHost, srcAdhoc.jump, srcAdhoc.target])  // eslint-disable-line react-hooks/exhaustive-deps
  // Changing the destination clears the (host-specific) destination folder.
  useEffect(() => { setDestDir(''); setProgress(null); setHostProgress({}); setResult(null) },
    [dstKind, dstAdhoc.jump, dstAdhoc.target, dstHosts.join(',')])  // eslint-disable-line react-hooks/exhaustive-deps
  // A local destination defaults to the agent's workspace (once known / on switch
  // back to local), only while empty — never clobbers a folder the operator typed.
  useEffect(() => {
    if (open && dstKind === 'local' && workspacePath) setDestDir(prev => prev || workspacePath)
  }, [open, dstKind, workspacePath])

  // Fan-out pre-flight: does the destination folder exist on every target host?
  useEffect(() => {
    setDestChecks(null)
    if (!isFanout || !destDir) { setChecking(false); return }
    setChecking(true)
    const hostsSnapshot = [...dstHosts]
    const dir = destDir
    if (checkRef.current) clearTimeout(checkRef.current)
    checkRef.current = setTimeout(async () => {
      try { setDestChecks(await bridge.check_dir_exists(hostsSnapshot, dir)) }
      catch { setDestChecks(null) }
      finally { setChecking(false) }
    }, 400)
    return () => { if (checkRef.current) clearTimeout(checkRef.current) }
  }, [isFanout, destDir, dstHosts.join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

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

  const srcEndpoint = (): TransferEndpoint => srcKind === 'local' ? LOCAL : srcKind === 'host' ? srcHost : adHocToEndpoint(srcAdhoc)
  const dstEndpoint = (): TransferEndpoint => dstKind === 'local' ? LOCAL : dstKind === 'host' ? dstHosts[0] : adHocToEndpoint(dstAdhoc)

  const start = async () => {
    setProgress(null)
    setResult(null)
    setHostProgress(isFanout ? Object.fromEntries(validHosts.map(h => [h, { transferred: 0, total: 0 }])) : {})
    // A new filename only applies to a single file (the backend rejects renaming
    // several at once — the UI only offers it for one).
    const rename = sourcePaths.length === 1 ? (destName.trim() || undefined) : undefined
    let res: { id?: string; error?: string }
    if (isFanout) {
      res = await bridge.start_fanout(sourcePaths, validHosts, destDir, filterName || undefined, filterArg || undefined, rename)
    } else if (!bothRemote) {
      // Exactly one local side — a plain download/upload (either side may be ad-hoc).
      res = await bridge.start_transfer(srcEndpoint(), sourcePaths, dstEndpoint(), destDir, filterName || undefined, filterArg || undefined, rename)
    } else {
      // Two remote sides — relay through this machine. The dest is a configured host.
      const source: RelaySource = srcKind === 'adhoc' ? adHocToEndpoint(srcAdhoc) : { kind: 'host', host: srcHost }
      res = await bridge.start_relay(source, sourcePaths, dstHosts[0], destDir, filterName || undefined, filterArg || undefined, rename)
    }
    // Never retain a typed ad-hoc password after handing it off.
    setSrcAdhoc(a => ({ ...a, password: '' }))
    setDstAdhoc(a => ({ ...a, password: '' }))
    if (res.error || !res.id) {
      message.error(res.error || 'Could not start transfer')
      setHostProgress({})
      return
    }
    idRef.current = res.id
    setRunning(true)
  }

  const cancel = () => { if (idRef.current) bridge.cancel_transfer(idRef.current) }

  // Closing tears down any cached ad-hoc browse connection and drops passwords.
  const handleClose = () => {
    bridge.close_adhoc().catch(() => {})
    setSrcAdhoc(a => ({ ...a, password: '' }))
    setDstAdhoc(a => ({ ...a, password: '' }))
    onClose()
  }

  const disabledReason = bothLocal
    ? 'Pick a remote source or destination — a local-to-local copy isn’t a transfer.'
    : relayDestUnsupported
      ? 'An ad-hoc destination is only supported from This computer (relay writes to a configured host).'
      : ''
  const canRun = srcOk && dstOk && !bothLocal && !relayDestUnsupported
    && sourcePaths.length > 0 && !!destDir && !running
    && (!isFanout || validHosts.length > 0)
  const pct = progress && progress.total > 0 ? Math.round((progress.transferred / progress.total) * 100) : 0
  const fanRows = Object.keys(hostProgress)

  const srcBrowseHost = srcKind === 'local' ? LOCAL : srcKind === 'host' ? srcHost : (srcAdhoc.target.trim() || 'source')
  const srcLister = srcKind === 'adhoc'
    ? (path: string, dirsOnly: boolean) => bridge.list_dir_adhoc(srcAdhoc.jump, srcAdhoc.target.trim(), path, srcAdhoc.user.trim() || undefined, srcAdhoc.password || undefined, undefined, dirsOnly, srcAdhoc.credentialId || undefined)
    : undefined
  const dstBrowseHost = dstKind === 'local' ? LOCAL : dstKind === 'host' ? (dstHosts[0] || '') : (dstAdhoc.target.trim() || 'dest')
  const dstLister = dstKind === 'adhoc'
    ? (path: string, dirsOnly: boolean) => bridge.list_dir_adhoc(dstAdhoc.jump, dstAdhoc.target.trim(), path, dstAdhoc.user.trim() || undefined, dstAdhoc.password || undefined, undefined, dirsOnly, dstAdhoc.credentialId || undefined)
    : undefined

  const endpointLabel = (kind: Kind, host: string, adhoc: AdHocState) =>
    kind === 'local' ? 'local' : kind === 'host' ? (host || '—') : (adhoc.target.trim() || 'ad-hoc')

  // A scenario saves the picked files as patterns: a pull (remote → this
  // computer), an upload (this computer → a host or a whole group) or a relay
  // (host → host). A hand-picked subset of hosts can't be saved — a scenario
  // names one host or one group.
  const pickedTarget = dstKind === 'host' ? remoteTarget(dstHosts, hosts) : null
  const canSaveScenario = !bothLocal && !relayDestUnsupported && srcOk && dstOk && sourcePaths.length > 0
    && (dstKind !== 'host' || !!pickedTarget) && (dstKind === 'local' || !!destDir.trim())
  const scenarioDraft = (): SavedTransfer => ({
    id: '',
    paths: sourcePaths,
    ...(srcKind === 'local' ? { host: LOCAL } : srcKind === 'host' ? { host: srcHost } : {
      adhoc: srcAdhoc.target.trim(), jump: srcAdhoc.jump || undefined,
      user: srcAdhoc.user.trim() || undefined, credential_id: srcAdhoc.credentialId || undefined,
    }),
    ...(dstKind === 'host' ? pickedTarget : dstKind === 'adhoc' ? {
      to_adhoc: dstAdhoc.target.trim(), to_jump: dstAdhoc.jump || undefined,
      to_user: dstAdhoc.user.trim() || undefined, to_credential_id: dstAdhoc.credentialId || undefined,
    } : {}),
    filter: filterName || undefined,
    filter_arg: filterArg || undefined,
    // A local folder other than the chat workspace is kept; the workspace default is not
    // (it changes per chat — the scenario follows whichever chat runs it).
    dest_dir: dstKind !== 'local' ? destDir.trim() : destDir && destDir !== workspacePath ? destDir : undefined,
  })
  const selectedSaved = saved.find(t => t.id === savedId)
  const deleteSaved = async (id: string) => {
    const res = await bridge.delete_saved_transfer(id)
    if (!res.ok) message.error(res.error || 'Could not delete')
    else setSavedId('')
  }

  const runLabel = isFanout ? `Push to ${validHosts.length} host${validHosts.length === 1 ? '' : 's'}` : bothRemote ? 'Relay' : 'Transfer'

  return (
    <Modal
      open={open}
      onCancel={handleClose}
      width={600}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><SwapOutlined /> Transfer files</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Tooltip title={canSaveScenario
              ? 'Save this transfer — with wildcards, dates and parameters — to run again later (you or the agent)'
              : dstKind === 'host' && dstHosts.length > 1 && !pickedTarget
                ? 'A scenario sends to one host or every host in one group — pick one of those to save it'
                : 'Pick a source, files and a destination (a folder, for a remote one) to save a scenario'}>
              <Button icon={<SaveOutlined />} disabled={!canSaveScenario} onClick={() => setEditing({ draft: scenarioDraft() })}>Save as scenario…</Button>
            </Tooltip>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {bothRemote ? 'Bytes stream through this machine — the two hosts never connect directly.' : ''}
            </Text>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={handleClose}>Close</Button>
            {running
              ? <Button danger onClick={cancel}>Cancel transfer</Button>
              : <Button type="primary" disabled={!canRun} onClick={start}>{runLabel}</Button>}
          </div>
        </div>
      }
    >
      {remoteHosts.length === 0 ? (
        <Text type="secondary">No remote hosts configured — add one to transfer files.</Text>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingBottom: 10, borderBottom: '1px solid var(--rs-border, rgba(128,128,128,.25))' }}>
            <Text style={{ fontSize: 13, width: 76 }}>Saved</Text>
            <Select
              size="small"
              showSearch
              allowClear
              value={savedId || undefined}
              onChange={v => setSavedId(v ?? '')}
              placeholder={saved.length ? `${saved.length} saved transfer${saved.length === 1 ? '' : 's'}` : 'None yet — pick files below and Save as scenario…'}
              disabled={saved.length === 0}
              style={{ flex: 1, minWidth: 0 }}
              optionFilterProp="label"
              options={saved.map(t => ({ value: t.id, label: `${t.title || t.id} — ${t.source ?? ''}${t.direction && t.direction !== 'pull' ? ` → ${t.dest ?? ''}` : ''}` }))}
            />
            <Button size="small" type="primary" icon={<PlayCircleOutlined />} disabled={!selectedSaved} onClick={() => selectedSaved && setRunningSaved(selectedSaved)}>Run…</Button>
            <Tooltip title="Edit"><Button size="small" icon={<EditOutlined />} disabled={!selectedSaved} onClick={() => selectedSaved && setEditing({ draft: selectedSaved, replaceId: selectedSaved.id })} /></Tooltip>
            <Popconfirm title={`Delete saved transfer '${savedId}'?`} disabled={!selectedSaved} onConfirm={() => deleteSaved(savedId)}>
              <Tooltip title="Delete"><Button size="small" danger icon={<DeleteOutlined />} disabled={!selectedSaved} /></Tooltip>
            </Popconfirm>
            <Tooltip title="All saved transfers: run, edit, copy, delete or create one"><Button size="small" icon={<UnorderedListOutlined />} onClick={() => setManaging(true)}>Manage…</Button></Tooltip>
          </div>
          {/* Source */}
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Text style={{ fontSize: 13, width: 76 }}>Source</Text>
              <Select size="small" value={srcKind} onChange={v => setSrcKind(v as Kind)} style={{ width: 150 }} options={KIND_OPTIONS} />
              {srcKind === 'host' && (
                <Select size="small" value={srcHost || undefined} onChange={setSrcHost} placeholder="Source host" style={{ minWidth: 170 }} options={hostOptions} />
              )}
            </div>
            {srcKind === 'adhoc' && (
              <div style={{ marginLeft: 84 }}>
                <AdHocHostFields value={srcAdhoc} onChange={setSrcAdhoc} hostOptions={hostOptions} creds={creds} />
              </div>
            )}
          </div>

          {/* Source files */}
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
              <Text style={{ fontSize: 13 }}>Source files</Text>
              <Tag style={{ fontSize: 11, margin: 0 }}>{endpointLabel(srcKind, srcHost, srcAdhoc)}</Tag>
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              {sourcePaths.map(p => (
                <Tag key={p} closable title={p} onClose={() => setSourcePaths(prev => prev.filter(x => x !== p))} style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
                  {p.split(/[\\/]/).pop()}
                </Tag>
              ))}
              <Button size="small" icon={<FolderOpenOutlined />} disabled={!srcOk} onClick={() => setBrowseSourceOpen(true)}>
                {sourcePaths.length ? 'Add files' : 'Choose files'}
              </Button>
            </div>
          </div>
          {browseSourceOpen && (
            <BrowseModal
              host={srcBrowseHost}
              multiSelect
              lister={srcLister}
              onPickMany={paths => setSourcePaths(prev => Array.from(new Set([...prev, ...paths])))}
              onClose={() => setBrowseSourceOpen(false)}
            />
          )}

          <FilterPicker filterName={filterName} filterArg={filterArg} onFilterName={setFilterName} onFilterArg={setFilterArg} />

          {/* Destination */}
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Text style={{ fontSize: 13, width: 76 }}>Destination</Text>
              <Select size="small" value={dstKind} onChange={v => setDstKind(v as Kind)} style={{ width: 150 }} options={KIND_OPTIONS} />
              {dstKind === 'host' && (
                srcKind === 'local' ? (
                  <Select
                    size="small"
                    mode="multiple"
                    value={dstHosts}
                    onChange={setDstHosts}
                    placeholder="Target host(s) — pick several to fan out"
                    style={{ minWidth: 240 }}
                    maxTagCount="responsive"
                    options={hostOptions}
                  />
                ) : (
                  <Select size="small" value={dstHosts[0] || undefined} onChange={v => setDstHosts([v])} placeholder="Destination host" style={{ minWidth: 170 }} options={hostOptions} />
                )
              )}
            </div>
            {dstKind === 'adhoc' && (
              <div style={{ marginLeft: 84, marginBottom: 6 }}>
                <AdHocHostFields value={dstAdhoc} onChange={setDstAdhoc} hostOptions={hostOptions} creds={creds} />
              </div>
            )}
            {relayDestUnsupported ? (
              <Text type="warning" style={{ fontSize: 12 }}>{disabledReason}</Text>
            ) : dstKind !== 'local' && (
              <PathInput
                host={dstBrowseHost}
                value={destDir}
                onChange={setDestDir}
                dirsOnly
                lister={dstLister}
                browseDisabled={dstKind === 'adhoc' ? !adHocValid(dstAdhoc) : !dstBrowseHost}
                placeholder="destination folder"
              />
            )}
            {dstKind === 'local' && (
              <>
                <PathInput host={LOCAL} value={destDir} onChange={setDestDir} dirsOnly placeholder="destination folder" />
                {workspacePath && destDir === workspacePath && (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    Defaulted to this chat's agent workspace — files here are ready for the agent to work on.
                  </Text>
                )}
              </>
            )}
            {isFanout && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                Browsing {dstHosts[0]} — the same path is written to all {dstHosts.length} hosts.
              </Text>
            )}
            {isFanout && destDir && (
              checking ? (
                <div><Text type="secondary" style={{ fontSize: 12 }}>Checking destination on hosts…</Text></div>
              ) : destChecks ? (
                missingHosts.length > 0 ? (
                  <div><Text type="warning" style={{ fontSize: 12 }}>⚠ Folder not found on {missingHosts.join(', ')} — these hosts will be skipped.</Text></div>
                ) : (
                  <div><Text type="success" style={{ fontSize: 12 }}>✓ Folder exists on all {dstHosts.length} hosts.</Text></div>
                )
              ) : null
            )}
            {sourcePaths.length === 1 && !bothLocal && !relayDestUnsupported && (
              <div style={{ marginTop: 8 }}>
                <Text style={{ fontSize: 13 }}>
                  Rename on {dstKind === 'local' ? 'this computer' : 'target'}{' '}
                  <Text type="secondary" style={{ fontSize: 12 }}>(optional)</Text>
                </Text>
                <Input
                  size="small"
                  allowClear
                  value={destName}
                  onChange={e => setDestName(e.target.value)}
                  placeholder={`keep source name (${sourcePaths[0].split(/[\\/]/).pop()})`}
                  style={{ fontFamily: MONO_FONT, fontSize: 13, marginTop: 4 }}
                />
              </div>
            )}
          </div>

          {bothLocal && <Text type="warning" style={{ fontSize: 12 }}>{disabledReason}</Text>}

          {fanRows.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {fanRows.map(h => {
                const hp = hostProgress[h]
                const p = hp.total > 0 ? Math.round((hp.transferred / hp.total) * 100) : (hp.done ? 100 : 0)
                const status = hp.done ? (hp.ok ? 'success' : 'exception') : 'active'
                return (
                  <div key={h} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Tag style={{ fontSize: 11, margin: 0, minWidth: 90, textAlign: 'center' }}>{h}</Tag>
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
                <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>
                  {progress.file}{progress.count > 1 ? ` (${progress.index + 1}/${progress.count})` : ''}
                  {progress.indeterminate ? ` · filtering… ${humanBytes(progress.transferred)} written` : ''}
                </Text>
              )}
            </div>
          ) : null}
          {result && !running && !isFanout && (
            <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 13 }}>
              {result.ok ? 'Done.' : `Failed: ${result.error}`}
            </Text>
          )}
        </div>
      )}
      {managing && (
        <SavedTransfersListModal
          saved={saved}
          onClose={() => setManaging(false)}
          onRun={t => setRunningSaved(t)}
          onEdit={(t, replaceId) => setEditing({ draft: t, replaceId })}
          onNew={() => setEditing({ draft: { id: '', paths: [] } })}
        />
      )}
      {editing && (
        <SaveTransferModal
          initial={editing.draft}
          replaceId={editing.replaceId}
          hosts={hosts}
          workspacePath={workspacePath}
          onClose={id => { setEditing(null); if (id) setSavedId(id) }}
        />
      )}
      {runningSaved && (
        <SavedTransferRunModal entry={runningSaved} hostNames={hosts.map(h => h.name)} onClose={() => setRunningSaved(null)} />
      )}
    </Modal>
  )
}
