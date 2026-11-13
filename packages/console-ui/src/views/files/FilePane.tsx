import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Input, Modal, Select, Spin, Tooltip, Typography, message } from 'antd'
import {
  ArrowUpOutlined, HomeOutlined, ReloadOutlined, FolderAddOutlined, FolderFilled,
  FileOutlined, CodeOutlined, LinkOutlined, EditOutlined, DeleteOutlined,
} from '@ant-design/icons'
import type { Credential, DirEntry, DirListing, Host } from '../../bridge'
import { bridge } from '../../bridge'
import { MONO_FONT, useAccent } from '../../ThemeContext'
import { AdHocHostFields, adHocValid, emptyAdHoc } from '../AdHocHostFields'
import {
  DRAG_MIME, LOCAL, joinPath, humanSize, humanTime, nextSelection, targetHost, targetLabel,
  type DragPayload, type PaneTarget, type Side,
} from './filesModel'
import { fullWhen } from '../../when'

const { Text } = Typography
const ADHOC = '__adhoc__'
// Rows rendered at once; the name filter narrows a bigger folder.
const RENDER_CAP = 500

export interface PaneState {
  target: PaneTarget | null
  listing: DirListing | null
  loading: boolean
  error: string
  selected: string[]
  // '' = the SSH login (or you, on this computer); else a venv owner the
  // pane browses the host as (sudo -n -u, on that venv's python).
  asUser: string
}

// One pane's state + loading. `open(target, path)` switches machine and folder
// together; an ad-hoc target only lists once the operator presses Connect.
export function usePane() {
  const [state, setState] = useState<PaneState>({ target: null, listing: null, loading: false, error: '', selected: [], asUser: '' })
  const req = useRef(0)
  const targetRef = useRef<PaneTarget | null>(null)
  const asRef = useRef('')

  const load = useCallback(async (path: string, target?: PaneTarget, select?: string[]) => {
    const t = target ?? targetRef.current
    if (!t) return
    const n = ++req.current
    setState(s => ({ ...s, loading: true, error: '' }))
    try {
      const res = t.kind === 'adhoc'
        ? await bridge.list_dir_adhoc(t.adhoc.jump, t.adhoc.target.trim(), path, t.adhoc.user.trim() || undefined,
          t.adhoc.password || undefined, undefined, false, t.adhoc.credentialId || undefined)
        : await bridge.list_dir(targetHost(t), path, undefined, false, undefined, false, asRef.current || undefined)
      if (n !== req.current) return
      if (res.error) setState(s => ({ ...s, loading: false, error: res.error as string }))
      else setState(s => ({ ...s, loading: false, listing: res, selected: select ?? [] }))
    } catch (e) {
      if (n === req.current) setState(s => ({ ...s, loading: false, error: String(e) }))
    }
  }, [])

  const open = useCallback((target: PaneTarget, path = '') => {
    targetRef.current = target
    asRef.current = ''
    req.current++
    setState({ target, listing: null, loading: false, error: '', selected: [], asUser: '' })
    if (target.kind !== 'adhoc' || adHocValid(target.adhoc)) load(path, target)
  }, [load])

  const setTarget = useCallback((target: PaneTarget) => {
    targetRef.current = target
    setState(s => ({ ...s, target }))
  }, [])

  // Switch the account the pane browses as, staying in the same folder.
  const setAsUser = useCallback((asUser: string, path: string) => {
    asRef.current = asUser
    setState(s => ({ ...s, asUser }))
    load(path)
  }, [load])

  const setSelected = useCallback((selected: string[]) => setState(s => ({ ...s, selected })), [])
  const refresh = useCallback((select?: string[]) => load(state.listing?.cwd ?? '', undefined, select), [load, state.listing?.cwd])

  return { ...state, load, open, setTarget, setAsUser, setSelected, refresh }
}

export type Pane = ReturnType<typeof usePane>

export function FilePane({
  side, pane, hosts, creds, workspacePath, dragFrom, onDragFrom, onDrop, onView, onFocus, focused,
}: {
  side: Side
  pane: Pane
  hosts: Host[]
  creds: Credential[]
  workspacePath: string
  // The side a drag started on (null when no drag is in flight), so only the
  // other pane lights up as a drop target.
  dragFrom: Side | null
  onDragFrom: (side: Side | null) => void
  onDrop: (payload: DragPayload, shift: boolean) => void
  onView: (path: string) => void
  onFocus: () => void
  focused: boolean
}) {
  const accent = useAccent()
  const [filter, setFilter] = useState('')
  const [pathText, setPathText] = useState('')
  const [over, setOver] = useState(false)
  const [newFolder, setNewFolder] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [renaming, setRenaming] = useState<{ old: string; name: string } | null>(null)
  const [deleting, setDeleting] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [accounts, setAccounts] = useState<{ login: string; accounts: string[] } | null>(null)
  const anchor = useRef<string | null>(null)
  const { target, listing, loading, error, selected, asUser } = pane
  const cwd = listing?.cwd ?? ''
  const hostName = target?.kind === 'host' ? target.host : ''

  // The accounts this host can be browsed as (its venv owners). They come
  // from discovery, which may still be running when the tab first opens on a
  // host, so read them again whenever discovery finishes.
  useEffect(() => {
    setAccounts(null)
    if (!hostName) return
    let live = true
    const load = () => { bridge.files_accounts(hostName).then(a => { if (live) setAccounts(a) }).catch(() => {}) }
    load()
    window.addEventListener('runspec:runnables_updated', load)
    return () => { live = false; window.removeEventListener('runspec:runnables_updated', load) }
  }, [hostName])
  const who = asUser || (target?.kind === 'local' ? 'you' : accounts?.login || 'the SSH login')

  useEffect(() => { setPathText(cwd); setFilter(''); anchor.current = null }, [cwd])

  const remote = hosts.filter(h => h.name !== LOCAL)
  const hostOptions = useMemo(() => {
    const byGroup = new Map<string, Host[]>()
    for (const h of remote) {
      const g = h.group || 'Other hosts'
      byGroup.set(g, [...(byGroup.get(g) ?? []), h])
    }
    const groups = [...byGroup.entries()].sort(([a], [b]) => (a === 'Other hosts' ? 1 : b === 'Other hosts' ? -1 : a.localeCompare(b)))
    return [
      { label: 'This computer', value: LOCAL },
      ...groups.map(([g, hs]) => ({
        label: g,
        options: hs.map(h => ({ label: h.connected ? h.name : `${h.name} (not connected)`, value: h.name, disabled: !h.connected })),
      })),
      { label: 'Ad-hoc host…', value: ADHOC },
    ]
  }, [remote])
  const jumpOptions = remote.map(h => ({ label: h.name, value: h.name, disabled: !h.connected }))

  const pick = (v: string) => {
    if (v === ADHOC) pane.open({ kind: 'adhoc', adhoc: emptyAdHoc() })
    else if (v === LOCAL) pane.open({ kind: 'local' }, workspacePath)
    else pane.open({ kind: 'host', host: v })
  }
  const selectValue = !target ? undefined : target.kind === 'local' ? LOCAL : target.kind === 'host' ? target.host : ADHOC

  const low = filter.trim().toLowerCase()
  const entries = (listing?.entries ?? []).filter(e => !low || e.name.toLowerCase().includes(low))
  const shown = entries.slice(0, RENDER_CAP)
  const names = shown.map(e => e.name)

  const openEntry = (e: DirEntry) => {
    const full = joinPath(cwd, e.name)
    if (e.type === 'directory') pane.load(full)
    else if (target?.kind !== 'adhoc') onView(full)
  }

  const click = (ev: React.MouseEvent, e: DirEntry) => {
    onFocus()
    const ctrl = ev.ctrlKey || ev.metaKey
    pane.setSelected(nextSelection(selected, names, e.name, anchor.current, { ctrl, shift: ev.shiftKey }))
    if (!ev.shiftKey) anchor.current = e.name
  }

  const dragStart = (ev: React.DragEvent, e: DirEntry) => {
    // Dragging an unselected file drags just that file; a selected one drags
    // every selected file (folders can't be copied yet, so they're left out).
    const names2 = selected.includes(e.name) ? selected : [e.name]
    if (!selected.includes(e.name)) pane.setSelected([e.name])
    const files = (listing?.entries ?? []).filter(x => names2.includes(x.name) && x.type === 'file')
    const payload: DragPayload = { side, paths: files.map(x => joinPath(cwd, x.name)) }
    ev.dataTransfer.setData(DRAG_MIME, JSON.stringify(payload))
    ev.dataTransfer.setData('text/plain', payload.paths.join('\n'))
    ev.dataTransfer.effectAllowed = 'copy'
    onDragFrom(side)
  }

  const acceptsDrop = dragFrom !== null && dragFrom !== side
  const dragOver = (ev: React.DragEvent) => {
    const ours = ev.dataTransfer.types.includes(DRAG_MIME)
    const outside = ev.dataTransfer.types.includes('Files')
    if ((ours && acceptsDrop) || (outside && !ours)) {
      ev.preventDefault()
      ev.dataTransfer.dropEffect = 'copy'
      if (!over) setOver(true)
    }
  }
  const drop = (ev: React.DragEvent) => {
    ev.preventDefault()
    setOver(false)
    onDragFrom(null)
    const raw = ev.dataTransfer.getData(DRAG_MIME)
    if (!raw) {
      if (ev.dataTransfer.files.length) {
        message.info('Dropping files from outside the console isn’t supported yet. Open their folder in the other pane and drag from there.')
      }
      return
    }
    try {
      const payload = JSON.parse(raw) as DragPayload
      if (payload.side !== side && payload.paths.length) onDrop(payload, ev.shiftKey)
    } catch { /* not ours */ }
  }

  const createFolder = async () => {
    if (!target || newFolder === null) return
    setCreating(true)
    try {
      const res = await bridge.make_dir(targetHost(target), cwd, newFolder, asUser || undefined)
      if (res.error) { message.error(res.error); return }
      setNewFolder(null)
      pane.refresh([newFolder.trim()])
    } finally { setCreating(false) }
  }

  const doRename = async () => {
    if (!target || !renaming) return
    setBusy(true)
    try {
      const res = await bridge.rename_path(targetHost(target), cwd, renaming.old, renaming.name.trim(), asUser || undefined)
      if (res.error) { message.error(res.error); return }
      setRenaming(null)
      pane.refresh([renaming.name.trim()])
    } catch (e) { message.error(String(e)) } finally { setBusy(false) }
  }

  const doDelete = async () => {
    if (!target || !deleting) return
    setBusy(true)
    try {
      const res = await bridge.delete_paths(targetHost(target), deleting.map(n => joinPath(cwd, n)), asUser || undefined)
      if (res.error) { message.error(res.error); return }
      setDeleting(null)
      const failed = res.failed ?? []
      const done = res.deleted?.length ?? 0
      if (failed.length) {
        const first = failed.slice(0, 3).map(f => `${f.path.split(/[\\/]/).pop()}: ${f.error}`).join('; ')
        message.warning(`Deleted ${done}, ${failed.length} not deleted (${first}${failed.length > 3 ? '; …' : ''})`)
      } else {
        message.success(`Deleted ${done === 1 ? deleting[0] : `${done} items`}`)
      }
      pane.refresh(failed.map(f => f.path.split(/[\\/]/).pop() ?? ''))
    } catch (e) { message.error(String(e)) } finally { setBusy(false) }
  }

  const canHostActions = !!target && target.kind !== 'adhoc' && !!listing
  const startRename = () => { if (canHostActions && selected.length === 1) setRenaming({ old: selected[0], name: selected[0] }) }
  const startDelete = () => { if (canHostActions && selected.length > 0) setDeleting([...selected]) }
  const files = (listing?.entries ?? []).filter(e => e.type === 'file').length
  const dirs = (listing?.entries ?? []).length - files
  const atWorkspace = target?.kind === 'local' && !!workspacePath && cwd === workspacePath
  const borderCol = over ? accent : focused ? 'var(--rs-border-strong)' : 'var(--rs-border)'

  return (
    <section
      aria-label={`${side === 'left' ? 'Left' : 'Right'} pane: ${target ? targetLabel(target) : 'no machine'}`}
      onDragOver={dragOver}
      onDragLeave={ev => { if (!ev.currentTarget.contains(ev.relatedTarget as Node)) setOver(false) }}
      onDrop={drop}
      onMouseDown={onFocus}
      data-testid={`pane-${side}`}
      style={{
        display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0,
        border: `1px solid ${borderCol}`, borderRadius: 8,
        background: over ? 'var(--rs-hover)' : 'var(--rs-surface-raised)',
        transition: 'border-color 120ms, background 120ms',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px 6px', flexWrap: 'wrap' }}>
        <Select
          size="small"
          value={selectValue}
          onChange={pick}
          placeholder="Pick a machine"
          style={{ minWidth: 200 }}
          options={hostOptions}
          showSearch
          optionFilterProp="label"
          aria-label={`${side} pane machine`}
        />
        {target?.kind === 'host' && (
          <Tooltip title={`Open a terminal on ${target.host}`}>
            <Button size="small" type="text" icon={<CodeOutlined />} aria-label="Open a terminal" onClick={() => bridge.launch_terminal(target.host).catch(e => message.error(String(e)))} />
          </Tooltip>
        )}
        {target?.kind === 'host' && accounts && accounts.accounts.length > 0 && (
          <Tooltip title="Browse this host as one of its venv owners (passwordless sudo to the venv's python). Copies to, from and between hosts run as that owner too; copying to several hosts skips any where it owns no venv.">
            <Select
              size="small"
              value={asUser}
              onChange={v => pane.setAsUser(v, cwd)}
              aria-label={`${side} pane browse as`}
              style={{ minWidth: 150 }}
              options={[
                { label: `as ${accounts.login || 'SSH login'}`, value: '' },
                ...accounts.accounts.map(a => ({ label: `as ${a} (venv owner)`, value: a })),
              ]}
            />
          </Tooltip>
        )}
        <div style={{ flex: 1 }} />
        {atWorkspace && <Text type="secondary" style={{ fontSize: 11 }}>chat workspace</Text>}
      </div>

      {target?.kind === 'adhoc' && (
        <div style={{ padding: '0 10px 6px', display: 'flex', flexDirection: 'column', gap: 6 }}>
          <AdHocHostFields value={target.adhoc} onChange={adhoc => pane.setTarget({ kind: 'adhoc', adhoc })} hostOptions={jumpOptions} creds={creds} />
          <div>
            <Button size="small" type="primary" icon={<LinkOutlined />} disabled={!adHocValid(target.adhoc)} onClick={() => pane.open(target)}>Connect</Button>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 4, padding: '0 10px 6px', alignItems: 'center' }}>
        <Tooltip title="Up"><Button size="small" icon={<ArrowUpOutlined />} aria-label="Up" disabled={!listing?.parent} onClick={() => listing?.parent != null && pane.load(listing.parent)} /></Tooltip>
        <Tooltip title={target?.kind === 'local' ? 'Chat workspace (or home)' : 'Home'}>
          <Button size="small" icon={<HomeOutlined />} aria-label="Home" disabled={!target || (target.kind === 'adhoc' && !listing)} onClick={() => pane.load(target?.kind === 'local' ? workspacePath : '')} />
        </Tooltip>
        <Tooltip title="Refresh"><Button size="small" icon={<ReloadOutlined />} aria-label="Refresh" disabled={!listing && !error} onClick={() => pane.refresh(selected)} /></Tooltip>
        <Input
          size="small"
          value={pathText}
          onChange={e => setPathText(e.target.value)}
          onPressEnter={() => pane.load(pathText.trim())}
          placeholder="folder path — Enter to open"
          aria-label={`${side} pane path`}
          style={{ fontFamily: MONO_FONT, fontSize: 12, flex: 1, minWidth: 0 }}
        />
        <Tooltip title={canHostActions ? 'New folder' : 'New folder works on this computer and configured hosts'}>
          <Button size="small" icon={<FolderAddOutlined />} aria-label="New folder" disabled={!canHostActions} onClick={() => setNewFolder('')} />
        </Tooltip>
        <Tooltip title={canHostActions ? 'Rename (F2)' : 'Rename works on this computer and configured hosts'}>
          <Button size="small" icon={<EditOutlined />} aria-label="Rename" disabled={!canHostActions || selected.length !== 1} onClick={startRename} />
        </Tooltip>
        <Tooltip title={canHostActions ? 'Delete (Del)' : 'Delete works on this computer and configured hosts'}>
          <Button size="small" icon={<DeleteOutlined />} aria-label="Delete" danger disabled={!canHostActions || selected.length === 0} onClick={startDelete} />
        </Tooltip>
      </div>

      <div style={{ padding: '0 10px 6px' }}>
        <Input size="small" allowClear value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter by name…" aria-label={`${side} pane name filter`} />
      </div>

      <div style={{
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 76px 150px', gap: 8, padding: '4px 12px',
        fontSize: 11, textTransform: 'uppercase', letterSpacing: '.05em', color: 'var(--rs-text-muted)',
        borderBottom: '1px solid var(--rs-border)',
      }}>
        <span>Name</span><span style={{ textAlign: 'right' }}>Size</span><span style={{ textAlign: 'right' }}>Modified</span>
      </div>

      <div role="listbox" aria-multiselectable aria-label={`${target ? targetLabel(target) : ''} files`} style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {error ? (
          <div style={{ padding: 16 }}><Text type="danger" style={{ fontSize: 13 }}>{error}</Text></div>
        ) : loading && !listing ? (
          <div style={{ padding: 24, textAlign: 'center' }}><Spin size="small" /></div>
        ) : !target ? (
          <div style={{ padding: 16 }}><Text type="secondary">Pick a machine above.</Text></div>
        ) : target.kind === 'adhoc' && !listing ? (
          <div style={{ padding: 16 }}><Text type="secondary">Enter the host and press Connect.</Text></div>
        ) : (
          <>
            {shown.map(e => {
              const isSel = selected.includes(e.name)
              const isFile = e.type === 'file'
              return (
                <div
                  key={e.name}
                  role="option"
                  aria-selected={isSel}
                  tabIndex={0}
                  draggable={isFile}
                  onDragStart={isFile ? ev => dragStart(ev, e) : undefined}
                  onDragEnd={() => onDragFrom(null)}
                  onClick={ev => click(ev, e)}
                  onDoubleClick={() => openEntry(e)}
                  onKeyDown={ev => {
                    if (ev.key === 'Enter') openEntry(e)
                    else if (ev.key === 'F2') { ev.preventDefault(); if (!isSel) pane.setSelected([e.name]); if (canHostActions) setRenaming({ old: e.name, name: e.name }) }
                    else if (ev.key === 'Delete') { ev.preventDefault(); if (canHostActions) setDeleting(isSel ? [...selected] : [e.name]) }
                    else if (ev.key === ' ') { ev.preventDefault(); pane.setSelected(isSel ? selected.filter(n => n !== e.name) : [...selected, e.name]) }
                  }}
                  title={isFile ? 'Drag to the other pane to copy · double-click to view' : 'Double-click to open'}
                  style={{
                    display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 76px 150px', gap: 8, alignItems: 'center',
                    padding: '4px 12px', cursor: isFile ? 'grab' : 'default', userSelect: 'none',
                    background: isSel ? `color-mix(in srgb, ${accent} 16%, transparent)` : undefined,
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  <span style={{ display: 'flex', gap: 8, alignItems: 'center', minWidth: 0, fontFamily: MONO_FONT, fontSize: 12 }}>
                    {isFile ? <FileOutlined style={{ color: 'var(--rs-text-muted)' }} /> : <FolderFilled style={{ color: '#d6a23c' }} />}
                    <Text style={{ fontFamily: MONO_FONT, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.name}</Text>
                  </span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: 'var(--rs-text-secondary)' }}>{isFile ? humanSize(e.size) : ''}</span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: 'var(--rs-text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={e.mtime ? fullWhen(e.mtime * 1000) : undefined}>{humanTime(e.mtime)}</span>
                </div>
              )
            })}
            {entries.length > shown.length && (
              <div style={{ padding: '8px 12px' }}><Text type="secondary" style={{ fontSize: 12 }}>{entries.length - shown.length} more. Filter by name to find them.</Text></div>
            )}
            {listing && entries.length === 0 && (
              <div style={{ padding: 16 }}><Text type="secondary">{low ? 'Nothing matches the filter.' : 'This folder is empty.'}</Text></div>
            )}
          </>
        )}
      </div>

      <div style={{ borderTop: '1px solid var(--rs-border)', padding: '4px 12px', fontSize: 12, color: 'var(--rs-text-muted)', display: 'flex', justifyContent: 'space-between', gap: 8 }}>
        <span>
          {listing ? `${dirs} folder${dirs === 1 ? '' : 's'}, ${files} file${files === 1 ? '' : 's'}` : ''}
          {selected.length > 0 ? ` · ${selected.length} selected` : ''}
          {listing?.truncated ? ' · only the first 5,000 entries are listed' : ''}
          {asUser ? ` · browsing as ${asUser}` : ''}
        </span>
        {loading && listing && <Spin size="small" />}
      </div>

      <Modal
        open={newFolder !== null}
        title={`New folder on ${target ? targetLabel(target) : ''}`}
        okText="Create"
        confirmLoading={creating}
        okButtonProps={{ disabled: !newFolder?.trim() }}
        onOk={createFolder}
        onCancel={() => setNewFolder(null)}
        destroyOnClose
      >
        <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>{cwd}</Text>
        <Input
          autoFocus
          value={newFolder ?? ''}
          onChange={e => setNewFolder(e.target.value)}
          onPressEnter={() => newFolder?.trim() && createFolder()}
          placeholder="folder name"
          aria-label="New folder name"
          style={{ marginTop: 8, fontFamily: MONO_FONT }}
        />
      </Modal>

      <ApproveCard
        open={renaming !== null}
        title={`Rename on ${target ? targetLabel(target) : ''}`}
        who={who}
        busy={busy}
        okDisabled={!renaming?.name.trim() || renaming?.name.trim() === renaming?.old}
        onApprove={doRename}
        onCancel={() => setRenaming(null)}
        note="Nothing is overwritten: if the new name is taken, the rename is refused."
      >
        <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>{renaming ? joinPath(cwd, renaming.old) : ''}</Text>
        <Input
          autoFocus
          value={renaming?.name ?? ''}
          onChange={e => setRenaming(r => r && { ...r, name: e.target.value })}
          onPressEnter={() => renaming?.name.trim() && renaming.name.trim() !== renaming.old && doRename()}
          aria-label="New name"
          style={{ marginTop: 8, fontFamily: MONO_FONT }}
        />
      </ApproveCard>

      <ApproveCard
        open={deleting !== null}
        title={`Delete ${deleting?.length === 1 ? deleting[0] : `${deleting?.length ?? 0} items`} on ${target ? targetLabel(target) : ''}?`}
        who={who}
        busy={busy}
        danger
        onApprove={doDelete}
        onCancel={() => setDeleting(null)}
        note="This can't be undone. A folder is deleted only when it is empty."
      >
        <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>{cwd}</Text>
        <ul style={{ margin: '6px 0 0', paddingLeft: 18, maxHeight: 220, overflowY: 'auto', fontFamily: MONO_FONT, fontSize: 12 }}>
          {(deleting ?? []).slice(0, 50).map(n => {
            const isDir = listing?.entries.find(x => x.name === n)?.type === 'directory'
            return <li key={n}>{n}{isDir ? '/' : ''}</li>
          })}
          {(deleting?.length ?? 0) > 50 && <li>and {(deleting?.length ?? 0) - 50} more</li>}
        </ul>
      </ApproveCard>
    </section>
  )
}

// The approval step for a change to files: what will happen, on which machine,
// as which account, and Approve / Cancel. Nothing runs until Approve.
function ApproveCard({ open, title, who, note, danger, busy, okDisabled, onApprove, onCancel, children }: {
  open: boolean
  title: string
  who: string
  note: string
  danger?: boolean
  busy: boolean
  okDisabled?: boolean
  onApprove: () => void
  onCancel: () => void
  children: React.ReactNode
}) {
  return (
    <Modal
      open={open}
      title={title}
      okText="Approve"
      okButtonProps={{ danger, disabled: okDisabled }}
      confirmLoading={busy}
      onOk={onApprove}
      onCancel={onCancel}
      destroyOnClose
    >
      <div style={{ borderLeft: '3px solid var(--rs-warning, #d6a23c)', paddingLeft: 10 }}>
        {children}
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <Text type="secondary">Runs as {who}. {note}</Text>
        </div>
      </div>
    </Modal>
  )
}
