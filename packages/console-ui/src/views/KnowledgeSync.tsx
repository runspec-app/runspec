// The Knowledge tab's Memory Sync buttons: pull the team's facts, push your
// approved ones to your own contrib/ branch. Settings live in Settings → Memory
// Sync (components/MemorySyncTab.tsx); until a repo is set, one button opens it.
// The outcome of a push or pull goes to onResult, which the tab shows as its own
// line under the toolbar (SyncResultBanner) so a long message can't widen the row.
// When Pull reads release tags, the Pull button shows a dot for a newer tag (one
// ls-remote when the tab opens and after each pull).

import { useCallback, useEffect, useState } from 'react'
import { Alert, Badge, Button, Popconfirm, Space, Tooltip } from 'antd'
import { CloudDownloadOutlined, CloudUploadOutlined, TeamOutlined } from '@ant-design/icons'

import { bridge, type MemorySyncStatus, type MemorySyncUpdate } from '../bridge'
import { errorText, type SyncResult } from '../components/MemorySyncTab'

interface KnowledgeSyncProps {
  onOpenSettings?: () => void
  // The last push / pull outcome, or null when a new one starts.
  onResult?: (result: SyncResult | null) => void
}

function when(iso: string): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
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
  return `Bring in the team's facts from ${status.pull_source || status.branch}. ${last}${newer}`
}

export function KnowledgeSync({ onOpenSettings, onResult }: KnowledgeSyncProps) {
  const [status, setStatus] = useState<MemorySyncStatus | null>(null)
  const [busy, setBusy] = useState<'' | 'push' | 'pull'>('')
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
      <Tooltip title="Share approved facts with your team through a git repo (Settings → Memory Sync).">
        <Button icon={<TeamOutlined />} onClick={onOpenSettings}>Share with team</Button>
      </Tooltip>
    )
  }

  const push = async () => {
    setBusy('push'); setResult(null)
    try {
      const r = await bridge.memory_sync_push()
      if (!r.ok) setResult({ ok: false, text: `Push failed: ${r.error || 'unknown error'}` })
      else if (!r.changed) setResult({ ok: true, text: `Nothing new to push: ${r.branch} already has your ${r.facts ?? 0} fact(s).` })
      else setResult({ ok: true, text: `Pushed ${r.facts ?? 0} fact(s) to ${r.branch}. The team sees them once that branch is merged.` })
      load()
    } catch (e) {
      setResult({ ok: false, text: `Push failed: ${errorText(e)}` })
    } finally { setBusy('') }
  }

  const pull = async () => {
    setBusy('pull'); setResult(null)
    try {
      const r = await bridge.memory_sync_pull()
      if (!r.ok) setResult({ ok: false, text: `Pull failed: ${r.error || 'unknown error'}` })
      else {
        const skipped = r.skipped?.length ? ` Skipped ${r.skipped.join(', ')} (not a valid name).` : ''
        const from = r.ref ? ` from ${r.ref}` : ''
        setResult({ ok: true, text: `Pulled ${r.facts ?? 0} team fact(s)${from} by ${r.files ?? 0} contributor(s): ${r.added ?? 0} new, ${r.removed ?? 0} removed.${skipped}` })
      }
      load(); checkUpdate()
    } catch (e) {
      setResult({ ok: false, text: `Pull failed: ${errorText(e)}` })
    } finally { setBusy('') }
  }

  return (
    <Space.Compact>
      <Tooltip title={pullTip(status, update)}>
        <Button icon={<CloudDownloadOutlined />} loading={busy === 'pull'} onClick={pull}>
          <Badge dot={!!update?.available && !update.pinned} offset={[6, -2]}>Pull</Badge>
        </Button>
      </Tooltip>
      <Popconfirm
        title={`Push ${status.shareable} approved fact(s)?`}
        description={<div style={{ maxWidth: 320 }}>Only {status.path} on {status.contrib_branch} changes. The team sees these facts once that branch is merged into {status.branch}.</div>}
        okText="Push"
        onConfirm={push}
      >
        <Tooltip title={`${status.unpushed ? 'You have approved facts that are not pushed yet.' : 'Everything approved is pushed.'} Last push: ${when(status.last_push)}.`}>
          <Button icon={<CloudUploadOutlined />} loading={busy === 'push'}>
            <Badge dot={status.unpushed} offset={[6, -2]}>Push</Badge>
          </Button>
        </Tooltip>
      </Popconfirm>
    </Space.Compact>
  )
}

// The last push / pull outcome. It stays until the next one or until dismissed,
// so a failure (credentials, a refused push) can't slip by.
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
