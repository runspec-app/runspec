// The Knowledge tab's Memory Sync buttons: pull the team's facts, push your
// approved ones to your own contrib/ branch. Settings live in Settings → Memory
// Sync (components/MemorySyncTab.tsx); until a repo is set, one button opens it.

import { useCallback, useEffect, useState } from 'react'
import { Badge, Button, Popconfirm, Space, Tooltip, message } from 'antd'
import { CloudDownloadOutlined, CloudUploadOutlined, TeamOutlined } from '@ant-design/icons'

import { bridge, type MemorySyncStatus } from '../bridge'

interface KnowledgeSyncProps {
  onOpenSettings?: () => void
}

function when(iso: string): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export function KnowledgeSync({ onOpenSettings }: KnowledgeSyncProps) {
  const [status, setStatus] = useState<MemorySyncStatus | null>(null)
  const [busy, setBusy] = useState<'' | 'push' | 'pull'>('')

  const load = useCallback(() => {
    bridge.memory_sync_status().then(setStatus).catch(() => setStatus(null))
  }, [])
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
    setBusy('push')
    try {
      const r = await bridge.memory_sync_push()
      if (!r.ok) message.error(r.error || 'Push failed.')
      else if (!r.changed) message.info(`Nothing new to push: ${r.branch} already has your ${r.facts ?? 0} fact(s).`)
      else message.success(`Pushed ${r.facts ?? 0} fact(s) to ${r.branch}. The team sees them once that branch is merged.`, 5)
      load()
    } finally { setBusy('') }
  }

  const pull = async () => {
    setBusy('pull')
    try {
      const r = await bridge.memory_sync_pull()
      if (!r.ok) message.error(r.error || 'Pull failed.')
      else {
        const skipped = r.skipped?.length ? ` Skipped ${r.skipped.join(', ')} (not a valid name).` : ''
        message.success(`Pulled ${r.facts ?? 0} team fact(s) from ${r.files ?? 0} contributor(s): ${r.added ?? 0} new, ${r.removed ?? 0} removed.${skipped}`, 5)
      }
      load()
    } finally { setBusy('') }
  }

  return (
    <Space.Compact>
      <Tooltip title={`Bring in the team's merged facts from ${status.branch}. Last pull: ${when(status.last_pull)}.`}>
        <Button icon={<CloudDownloadOutlined />} loading={busy === 'pull'} onClick={pull}>Pull</Button>
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
