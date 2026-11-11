import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Progress, Select, Spin, Tag, Tooltip, Typography, message } from 'antd'
import {
  PlayCircleOutlined, EditOutlined, DeleteOutlined, PlusOutlined, UnorderedListOutlined,
  FolderOpenOutlined, ReloadOutlined, SaveOutlined, EyeOutlined, SwapOutlined, MenuFoldOutlined, MenuUnfoldOutlined,
} from '@ant-design/icons'
import type { Credential, FileView, Host, SavedTransfer } from '../../bridge'
import { bridge } from '../../bridge'
import { MONO_FONT } from '../../ThemeContext'
import { SaveTransferModal, SavedTransferRunModal, SavedTransfersListModal } from '../SavedTransferModals'
import { FilePane, usePane, type Pane } from './FilePane'
import { DropDialog, type DropChoice } from './DropDialog'
import { useTransfers, percent, type TransferItem } from './useTransfers'
import {
  LOCAL, baseName, draftFromCopy, endpointOf, humanSize, joinPath, otherSide, panesForSaved,
  planCopy, rememberFilter, relaySource, targetHost, targetKey, targetLabel,
  type DragPayload, type PaneTarget, type Side,
} from './filesModel'

const { Text } = Typography
const RAIL_KEY = 'runspec.files.rail'

interface PendingDrop { from: Side; to: Side; paths: string[] }

// The Files tab: two panes, each this computer / a fleet host / an ad-hoc host;
// drag files from one to the other to copy them (the drop dialog offers the
// saved filters); saved transfers listed on the left; copies run in the
// Transfers strip at the bottom so you can keep browsing.
export function FilesView({ hosts, selectedHost, active }: {
  hosts: Host[]
  selectedHost?: string
  active: boolean
}) {
  const left = usePane()
  const right = usePane()
  const panes: Record<Side, Pane> = { left, right }
  const [focused, setFocused] = useState<Side>('right')
  const [dragFrom, setDragFrom] = useState<Side | null>(null)
  const [drop, setDrop] = useState<PendingDrop | null>(null)
  const [viewing, setViewing] = useState<{ side: Side; path: string } | null>(null)
  const [workspacePath, setWorkspacePath] = useState('')
  const [creds, setCreds] = useState<Credential[]>([])
  const [rail, setRail] = useState(() => { try { return localStorage.getItem(RAIL_KEY) !== 'off' } catch { return true } })
  const [saved, setSaved] = useState<SavedTransfer[]>([])
  const [editing, setEditing] = useState<{ draft: SavedTransfer; replaceId?: string } | null>(null)
  const [runningSaved, setRunningSaved] = useState<SavedTransfer | null>(null)
  const [managing, setManaging] = useState(false)
  const started = useRef(false)

  // A finished copy refreshes whichever pane shows the folder it landed in.
  const { items, add, clearFinished } = useTransfers(useCallback((it: TransferItem) => {
    if (it.ok && it.error) message.warning(`Copied ${it.title}; ${it.error}`)
    else if (it.ok) message.success(`Copied ${it.title}`)
    else message.error(it.error || `Copy of ${it.title} failed`)
    for (const p of [left, right]) {
      if (p.target && targetKey(p.target) === it.destKey && p.listing?.cwd === it.destDir) p.refresh(it.ok ? it.names : p.selected)
    }
  }, [left, right]))

  // Workspace: the left pane starts there, and Home on this computer goes back.
  useEffect(() => {
    if (!active) return
    bridge.current_workspace_path().then(setWorkspacePath).catch(() => {})
  }, [active])

  useEffect(() => {
    bridge.list_credentials().then(setCreds).catch(() => {})
    const load = () => { bridge.get_saved_transfers().then(setSaved).catch(() => {}) }
    load()
    window.addEventListener('runspec:saved_transfers_changed', load)
    return () => window.removeEventListener('runspec:saved_transfers_changed', load)
  }, [])

  // First open: this computer on the left (the chat workspace), the selected
  // host — or the first connected one — on the right.
  useEffect(() => {
    if (!active || started.current || hosts.length === 0) return
    started.current = true
    const remote = hosts.filter(h => h.name !== LOCAL)
    const pick = (selectedHost && selectedHost !== LOCAL && remote.some(h => h.name === selectedHost && h.connected))
      ? selectedHost
      : remote.find(h => h.connected)?.name
    bridge.current_workspace_path().catch(() => '').then(ws => {
      left.open({ kind: 'local' }, ws || '')
      if (pick) right.open({ kind: 'host', host: pick })
      else right.open({ kind: 'local' })
    })
  }, [active, hosts, selectedHost])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { try { localStorage.setItem(RAIL_KEY, rail ? 'on' : 'off') } catch { /* private window */ } }, [rail])

  // ── copying ──────────────────────────────────────────────────────────────

  const startCopy = async (pd: PendingDrop, choice: DropChoice) => {
    const srcPane = panes[pd.from]
    const dstPane = panes[pd.to]
    const src = srcPane.target
    const dst = dstPane.target
    const destDir = dstPane.listing?.cwd ?? ''
    if (!src || !dst || !destDir) { message.error('Open a destination folder first'); return }
    const plan = planCopy(src, dst, choice.alsoTo)
    if (plan.error) { message.error(plan.error); return }
    const filter = choice.filter || undefined
    const arg = choice.filter ? (choice.filterArg || undefined) : undefined
    const rename = pd.paths.length === 1 ? (choice.destName || undefined) : undefined
    let res: { id?: string; error?: string }
    if (plan.kind === 'fanout' && dst.kind === 'host') {
      res = await bridge.start_fanout(pd.paths, [dst.host, ...choice.alsoTo], destDir, filter, arg, rename, dstPane.asUser || undefined)
    } else if (plan.kind === 'relay' && dst.kind === 'host') {
      res = await bridge.start_relay(relaySource(src)!, pd.paths, dst.host, destDir, filter, arg, rename, srcPane.asUser || undefined, dstPane.asUser || undefined)
    } else {
      // Only one side is a host here, so a "Browse as" account is that host's.
      const asUser = srcPane.asUser || dstPane.asUser || undefined
      res = await bridge.start_transfer(endpointOf(src), pd.paths, endpointOf(dst), destDir, filter, arg, rename, asUser)
    }
    if (res.error || !res.id) { message.error(res.error || 'Could not start the copy'); return }
    rememberFilter(src, srcPane.listing?.cwd ?? '', choice.filter)
    const names = rename ? [rename] : pd.paths.map(baseName)
    add({
      id: res.id,
      title: pd.paths.length === 1 ? baseName(pd.paths[0]) : `${pd.paths.length} files`,
      route: `${targetLabel(src)} → ${targetLabel(dst)}${choice.alsoTo.length ? ` +${choice.alsoTo.length}` : ''}`,
      filter,
      destKey: targetKey(dst),
      destDir,
      names,
    })
    if (choice.saveAsTransfer) {
      const draft = draftFromCopy({
        src, dst, paths: pd.paths, destDir, hosts, alsoTo: choice.alsoTo,
        filter: choice.filter, filterArg: choice.filterArg, destName: choice.destName, workspacePath,
      })
      if (draft) setEditing({ draft })
      else message.warning('A saved transfer copies to one host or a whole group, so this one can’t be saved as picked.')
    }
  }

  const onDrop = (to: Side) => (payload: DragPayload, shift: boolean) => {
    const pd = { from: payload.side, to, paths: payload.paths }
    if (!pd.paths.length) { message.info('Only files can be copied for now, not folders.'); return }
    if (!panes[to].listing) { message.error('Open a folder in that pane first'); return }
    if (shift) {
      startCopy(pd, { filter: '', filterArg: '', destName: '', alsoTo: [], saveAsTransfer: false })
      return
    }
    setDrop(pd)
  }

  // Toolbar actions work on the pane you last clicked in.
  const fp = panes[focused]
  const selectedFiles = (fp.listing?.entries ?? []).filter(e => e.type === 'file' && fp.selected.includes(e.name))
  const selectedPaths = selectedFiles.map(e => joinPath(fp.listing!.cwd, e.name))
  const copySelected = () => {
    if (!selectedPaths.length) return
    onDrop(otherSide(focused))({ side: focused, paths: selectedPaths }, false)
  }
  const saveSelected = () => {
    const other = panes[otherSide(focused)]
    if (!fp.target || !other.target) return
    const draft = draftFromCopy({ src: fp.target, dst: other.target, paths: selectedPaths, destDir: other.listing?.cwd ?? '', hosts, workspacePath })
    if (draft) setEditing({ draft })
    else message.warning('Pick a host or this computer in the other pane to save a transfer.')
  }
  const canView = selectedPaths.length === 1 && fp.target?.kind !== 'adhoc'
  const otherTarget = panes[otherSide(focused)].target
  const copyPlan = fp.target && otherTarget ? planCopy(fp.target, otherTarget) : { error: 'Pick a machine in both panes' }

  // ── saved transfers ─────────────────────────────────────────────────────

  const openInPanes = (t: SavedTransfer) => {
    const layout = panesForSaved(t, hosts, workspacePath)
    if (layout.left) left.open(layout.left.target, layout.left.path)
    if (layout.right) right.open(layout.right.target, layout.right.path)
    if (!layout.left && !layout.right) message.info('This saved transfer uses an ad-hoc host, so it can’t be opened in the panes.')
  }
  const deleteSaved = async (id: string) => {
    const res = await bridge.delete_saved_transfer(id)
    if (!res.ok) message.error(res.error || 'Could not delete')
  }

  const running = items.filter(it => !it.done).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }} data-testid="files-view">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderBottom: '1px solid var(--rs-border)', flexWrap: 'wrap' }}>
        <Tooltip title={rail ? 'Hide saved transfers' : 'Show saved transfers'}>
          <Button size="small" icon={rail ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />} aria-pressed={rail} onClick={() => setRail(r => !r)}>Saved transfers</Button>
        </Tooltip>
        <Tooltip title={copyPlan.error ?? (selectedPaths.length ? `Copy the selected file${selectedPaths.length === 1 ? '' : 's'} to the other pane` : 'Select files first (or drag them across)')}>
          <Button size="small" icon={<SwapOutlined />} disabled={!selectedPaths.length || !!copyPlan.error} onClick={copySelected}>
            {focused === 'right' ? '← Copy to other pane' : 'Copy to other pane →'}
          </Button>
        </Tooltip>
        <Tooltip title="Save the selected files and the other pane's folder as a saved transfer you (or the assistant) can run again">
          <Button size="small" icon={<SaveOutlined />} disabled={!selectedPaths.length || !!copyPlan.error} onClick={saveSelected}>Save as saved transfer…</Button>
        </Tooltip>
        <Tooltip title={canView ? 'Show the end of the file' : 'Select one file on this computer or a configured host'}>
          <Button size="small" icon={<EyeOutlined />} disabled={!canView} onClick={() => setViewing({ side: focused, path: selectedPaths[0] })}>View</Button>
        </Tooltip>
        <div style={{ flex: 1 }} />
        <Text type="secondary" style={{ fontSize: 12 }}>Drag files between the panes to copy. Hold Shift to copy without the dialog.</Text>
      </div>

      <div style={{ flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: rail ? '250px minmax(0,1fr)' : 'minmax(0,1fr)' }}>
        {rail && (
          <aside aria-label="Saved transfers" style={{ borderRight: '1px solid var(--rs-border)', background: 'var(--rs-surface-sunken)', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px 6px' }}>
              <Text type="secondary" style={{ fontSize: 11, fontWeight: 600, letterSpacing: '.07em', textTransform: 'uppercase' }}>Saved transfers</Text>
              <span>
                <Tooltip title="New saved transfer"><Button size="small" type="text" icon={<PlusOutlined />} aria-label="New saved transfer" onClick={() => setEditing({ draft: { id: '', paths: [] } })} /></Tooltip>
                <Tooltip title="All saved transfers"><Button size="small" type="text" icon={<UnorderedListOutlined />} aria-label="Manage saved transfers" onClick={() => setManaging(true)} /></Tooltip>
              </span>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '0 10px 10px', display: 'flex', flexDirection: 'column', gap: 8 }}>
              {saved.length === 0 && (
                <Text type="secondary" style={{ fontSize: 12, padding: '4px 2px' }}>
                  None yet. Select files in a pane and press “Save as saved transfer…”, or tick “Save as a saved transfer” when you drop.
                </Text>
              )}
              {saved.map(t => (
                <div key={t.id} style={{ border: '1px solid var(--rs-border)', background: 'var(--rs-surface-raised)', borderRadius: 8, padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 3 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, alignItems: 'center' }}>
                    <Text strong style={{ fontSize: 13, minWidth: 0 }} ellipsis={{ tooltip: t.title || t.id }}>{t.title || t.id}</Text>
                    <Tag style={{ margin: 0, fontSize: 11 }} color={t.direction === 'push' ? 'gold' : t.direction === 'relay' ? 'purple' : 'cyan'}>{t.direction ?? 'pull'}</Tag>
                  </div>
                  <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: `${t.source ?? ''} → ${t.dest ?? ''}` }}>{t.source ?? '—'} → {t.dest ?? '—'}</Text>
                  {t.filter && <Text type="secondary" style={{ fontSize: 12 }}>Filter: {t.filter}</Text>}
                  {t.error && <Text type="danger" style={{ fontSize: 12 }}>{t.error}</Text>}
                  <div style={{ display: 'flex', gap: 4, marginTop: 4, flexWrap: 'wrap' }}>
                    <Button size="small" type="primary" icon={<PlayCircleOutlined />} disabled={!!t.error} onClick={() => setRunningSaved(t)}>Run…</Button>
                    <Tooltip title="Open its source and destination folders in the panes"><Button size="small" icon={<FolderOpenOutlined />} aria-label={`Open ${t.id} in panes`} onClick={() => openInPanes(t)} /></Tooltip>
                    <Tooltip title="Edit"><Button size="small" icon={<EditOutlined />} aria-label={`Edit ${t.id}`} onClick={() => setEditing({ draft: t, replaceId: t.id })} /></Tooltip>
                    <Popconfirm title={`Delete saved transfer '${t.id}'?`} onConfirm={() => deleteSaved(t.id)}>
                      <Tooltip title="Delete"><Button size="small" danger icon={<DeleteOutlined />} aria-label={`Delete ${t.id}`} /></Tooltip>
                    </Popconfirm>
                  </div>
                </div>
              ))}
            </div>
          </aside>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 10, padding: 10, minHeight: 0 }}>
          {(['left', 'right'] as Side[]).map(side => (
            <FilePane
              key={side}
              side={side}
              pane={panes[side]}
              hosts={hosts}
              creds={creds}
              workspacePath={workspacePath}
              dragFrom={dragFrom}
              onDragFrom={setDragFrom}
              onDrop={onDrop(side)}
              onView={path => setViewing({ side, path })}
              onFocus={() => setFocused(side)}
              focused={focused === side}
            />
          ))}
        </div>
      </div>

      <div aria-label="Transfers" aria-live="polite" style={{ borderTop: '1px solid var(--rs-border)', background: 'var(--rs-surface-sunken)', padding: '6px 12px', maxHeight: 170, overflowY: 'auto', flexShrink: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 600, letterSpacing: '.07em', textTransform: 'uppercase' }}>
            Transfers{running ? ` · ${running} running` : ''}
          </Text>
          {items.some(it => it.done) && <Button size="small" type="link" onClick={clearFinished}>Clear finished</Button>}
        </div>
        {items.length === 0 ? (
          <Text type="secondary" style={{ fontSize: 12 }}>Nothing running. Copies show here while they run, so you can keep browsing.</Text>
        ) : items.map(it => <TransferRow key={it.id} it={it} />)}
      </div>

      {drop && panes[drop.from].target && panes[drop.to].target && (
        <DropDialog
          src={panes[drop.from].target!}
          dst={panes[drop.to].target!}
          paths={drop.paths}
          srcDir={panes[drop.from].listing?.cwd ?? ''}
          destDir={panes[drop.to].listing?.cwd ?? ''}
          hosts={hosts}
          srcAs={panes[drop.from].asUser}
          dstAs={panes[drop.to].asUser}
          onCancel={() => setDrop(null)}
          onCopy={choice => { const pd = drop; setDrop(null); startCopy(pd, choice) }}
        />
      )}
      {viewing && panes[viewing.side].target && (
        <ViewFileModal host={targetHost(panes[viewing.side].target!)} asUser={panes[viewing.side].asUser} label={targetLabel(panes[viewing.side].target!)} path={viewing.path} onClose={() => setViewing(null)} />
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
          onClose={() => setEditing(null)}
        />
      )}
      {runningSaved && (
        <SavedTransferRunModal entry={runningSaved} hostNames={hosts.map(h => h.name)} onClose={() => setRunningSaved(null)} />
      )}
    </div>
  )
}

function TransferRow({ it }: { it: TransferItem }) {
  const pct = percent(it)
  const status = it.done ? (it.ok ? 'success' : 'exception') : 'active'
  const detail = it.done
    ? (it.ok ? 'Done' : (it.error || 'Failed'))
    : it.indeterminate ? `filtering… ${humanSize(it.transferred)} written`
      : it.count && it.count > 1 ? `${(it.index ?? 0) + 1} of ${it.count}` : ''
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 180px auto', gap: 12, alignItems: 'center', fontSize: 12, padding: '3px 0' }}>
      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={[...it.errors, it.error ?? ''].filter(Boolean).join('\n') || undefined}>
        <Text style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{it.title}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}> · {it.route}{it.filter ? ` · filter: ${it.filter}` : ''}</Text>
        {it.errors.length > 0 && <Text type="danger" style={{ fontSize: 12 }}> · {it.errors.length} error{it.errors.length === 1 ? '' : 's'}</Text>}
      </span>
      <Progress percent={it.indeterminate && !it.done ? 100 : pct} status={status} showInfo={false} size="small" style={{ margin: 0 }} />
      <span style={{ display: 'flex', gap: 6, alignItems: 'center', justifyContent: 'flex-end', minWidth: 110 }}>
        <Text type={it.done ? (it.ok ? 'success' : 'danger') : 'secondary'} style={{ fontSize: 12, maxWidth: 260 }} ellipsis={{ tooltip: detail }}>{detail || `${pct}%`}</Text>
        {!it.done && <Button size="small" type="link" danger onClick={() => bridge.cancel_transfer(it.id)}>Cancel</Button>}
      </span>
    </div>
  )
}

const LINE_CHOICES = [200, 500, 2000, 5000]

export function ViewFileModal({ host, asUser = '', label, path, onClose }: { host: string; asUser?: string; label: string; path: string; onClose: () => void }) {
  const [lines, setLines] = useState(500)
  const [view, setView] = useState<FileView | null>(null)
  const [loading, setLoading] = useState(false)
  const [find, setFind] = useState('')
  const preRef = useRef<HTMLPreElement | null>(null)

  const load = useCallback(async (n: number) => {
    setLoading(true)
    try { setView(await bridge.view_file(host, path, n, asUser || undefined)) }
    catch (e) { setView({ error: String(e) }) }
    finally { setLoading(false) }
  }, [host, path, asUser])
  useEffect(() => { load(lines) }, [load, lines])
  // Logs are read from the bottom: start scrolled to the end.
  useEffect(() => { const el = preRef.current; if (el) el.scrollTop = el.scrollHeight }, [view])

  const low = find.trim().toLowerCase()
  const text = view?.text ?? ''
  const shown = low ? text.split('\n').filter(l => l.toLowerCase().includes(low)).join('\n') : text

  return (
    <Modal
      open
      onCancel={onClose}
      width="min(1100px, 94vw)"
      title={<span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{label} · {path}</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {view && !view.error && !view.binary
              ? `${view.partial ? `Last ${view.lines} lines` : `All ${view.lines} lines`} of ${humanSize(view.size ?? 0)}. Read-only.`
              : ''}
          </Text>
          <Button onClick={onClose}>Close</Button>
        </div>
      }
    >
      <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
        <Select size="small" value={lines} onChange={setLines} aria-label="Lines to show" style={{ width: 150 }}
          options={LINE_CHOICES.map(n => ({ label: `Last ${n} lines`, value: n }))} />
        <Tooltip title="Reload"><Button size="small" icon={<ReloadOutlined />} aria-label="Reload" onClick={() => load(lines)} /></Tooltip>
        <Input size="small" allowClear value={find} onChange={e => setFind(e.target.value)} placeholder="Show only lines containing…" aria-label="Show only lines containing" style={{ maxWidth: 320 }} />
        {loading && <Spin size="small" />}
      </div>
      {view?.error ? (
        <Text type="danger">{view.error}</Text>
      ) : view?.binary ? (
        <Text type="secondary">This doesn’t look like a text file, so it isn’t shown. Copy it to this computer to open it.</Text>
      ) : (
        <pre ref={preRef} style={{
          margin: 0, height: '62vh', overflow: 'auto', fontFamily: MONO_FONT, fontSize: 12, lineHeight: 1.45,
          background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: 6, padding: '8px 10px',
          whiteSpace: 'pre',
        }}>{shown || (view && !loading ? (low ? 'No line matches.' : 'The file is empty.') : '')}</pre>
      )}
    </Modal>
  )
}
