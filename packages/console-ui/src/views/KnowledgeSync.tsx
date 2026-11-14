// The Knowledge tab's Memory Sync button: Pull brings in the team's pages and
// facts. Sharing goes through Propose changes on the Tools view
// (KnowledgeEdits.tsx); the old facts Push button is gone (0.411.0). Settings
// live in Settings → Memory Sync (components/MemorySyncTab.tsx); until a repo is
// set, one button opens it. The outcome of a pull goes to onResult, which the tab
// shows as its own line under the toolbar (SyncResultBanner) so a long message
// can't widen the row. When Pull reads release tags, the Pull button shows a dot
// for a newer tag (one ls-remote when the tab opens and after each pull).

import { useCallback, useEffect, useState } from 'react'
import { Alert, Badge, Button, Tooltip } from 'antd'
import { CloudDownloadOutlined, TeamOutlined } from '@ant-design/icons'

import { bridge, type MemorySyncStatus, type MemorySyncUpdate } from '../bridge'
import { errorText, type SyncResult } from '../components/MemorySyncTab'
import { fmtWhen } from '../when'

interface KnowledgeSyncProps {
  onOpenSettings?: () => void
  // The last pull outcome, or null when a new one starts.
  onResult?: (result: SyncResult | null) => void
}

function when(iso: string): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : fmtWhen(d)
}

// The Pull tooltip: what a pull reads, what it last read, and anything newer.
export function pullTip(status: MemorySyncStatus, update: MemorySyncUpdate | null): string {
  const last = status.last_pull
    ? `Last pull: ${status.last_pull_ref ? `${status.last_pull_ref}, ` : ''}${when(status.last_pull)}.`
    : 'Never pulled.'
  let newer = ''
  if (update?.available) {
    if (update.pinned) newer = ` ${update.latest} is out; change the pinned tag in Settings → Memory Sync to pull it.`
    else if (status.pull_from === 'branch') newer = ` ${status.branch} has new merges.`
    else newer = ` ${update.latest} is ready to pull.`
  }
  return `Bring in the team's pages and facts from ${status.pull_source || status.branch}. ${last}${newer}`
}

/** The Pull result line: pages first (the team wiki), then facts. */
export function pullText(r: {
  ref?: string; facts?: number; files?: number; added?: number; removed?: number; skipped?: string[]
  pages?: { pages: number; added: number; removed: number }; drafts_landed?: number
}): string {
  const from = r.ref ? ` from ${r.ref}` : ''
  const parts: string[] = []
  if (r.pages) parts.push(`${r.pages.pages} page${r.pages.pages === 1 ? '' : 's'} (${r.pages.added} new, ${r.pages.removed} removed)`)
  parts.push(`${r.facts ?? 0} team fact${r.facts === 1 ? '' : 's'} by ${r.files ?? 0} contributor${r.files === 1 ? '' : 's'}`)
  const landed = r.drafts_landed ? ` ${r.drafts_landed} of your page edits ${r.drafts_landed === 1 ? 'is' : 'are'} now merged.` : ''
  const skipped = r.skipped?.length ? ` Skipped ${r.skipped.join(', ')} (not a valid name).` : ''
  return `Pulled ${parts.join(' and ')}${from}.${landed}${skipped}`
}

export function KnowledgeSync({ onOpenSettings, onResult }: KnowledgeSyncProps) {
  const [status, setStatus] = useState<MemorySyncStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [update, setUpdate] = useState<MemorySyncUpdate | null>(null)
  const setResult = (r: SyncResult | null) => onResult?.(r)

  const load = useCallback(() => {
    bridge.memory_sync_status().then(setStatus).catch(() => setStatus(null))
  }, [])
  const checkUpdate = useCallback(() => {
    bridge.memory_sync_check_update().then(r => setUpdate(r.ok ? r : null)).catch(() => setUpdate(null))
  }, [])
  const configured = !!status?.configured
  useEffect(() => { if (configured) checkUpdate() }, [configured, checkUpdate])
  useEffect(() => {
    load()
    window.addEventListener('runspec:knowledge_updated', load)
    return () => window.removeEventListener('runspec:knowledge_updated', load)
  }, [load])

  if (!status) return null
  if (!status.configured) {
    return (
      <Tooltip title="Share the team wiki through a git repo (Settings → Memory Sync).">
        <Button icon={<TeamOutlined />} onClick={onOpenSettings}>Share with team</Button>
      </Tooltip>
    )
  }

  const pull = async () => {
    setBusy(true); setResult(null)
    try {
      const r = await bridge.memory_sync_pull()
      if (!r.ok) setResult({ ok: false, text: `Pull failed: ${r.error || 'unknown error'}` })
      else setResult({ ok: true, text: pullText(r) })
      load(); checkUpdate()
    } catch (e) {
      setResult({ ok: false, text: `Pull failed: ${errorText(e)}` })
    } finally { setBusy(false) }
  }

  return (
    <Tooltip title={pullTip(status, update)}>
      <Button icon={<CloudDownloadOutlined />} loading={busy} onClick={pull}>
        <Badge dot={!!update?.available && !update.pinned} offset={[6, -2]}>Pull</Badge>
      </Button>
    </Tooltip>
  )
}

// The last pull outcome. It stays until the next one or until dismissed, so a
// failure (credentials, an unreachable repo) can't slip by.
export function SyncResultBanner({ result, onClose }: { result: SyncResult | null, onClose: () => void }) {
  if (!result) return null
  return (
    <Alert
      type={result.ok ? 'success' : 'error'}
      message={result.text}
      showIcon
      closable
      onClose={onClose}
    />
  )
}
