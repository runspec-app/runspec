// Config Sync → "Review changes" modal: what pulling a tag will change, per file,
// before the operator accepts. The backend (bridge.sync_preview) computes the
// post-pull table with the same merge the pull uses, so the diff shown here is
// what lands; the Pull button passes the previewed commit back so a tag moved
// in between is refused rather than silently applied.
import { Alert, Button, Collapse, Empty, Modal, Space, Tag, Typography } from 'antd'
import { DownloadOutlined } from '@ant-design/icons'
import type { SyncPreview, SyncPreviewFile } from '../bridge'
import { MONO_FONT, textColor, textMuted, textSecondary, useIsDark } from '../ThemeContext'

const { Text } = Typography

export type DiffLineKind = 'add' | 'del' | 'hunk' | 'file' | 'ctx'

/** Classify one unified-diff line for colouring. */
export function diffLineKind(line: string): DiffLineKind {
  if (line.startsWith('+++') || line.startsWith('---')) return 'file'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return 'ctx'
}

/** One-line headline for a file: "+1 added · 2 updated · 3 kept" etc. */
export function summaryLine(f: SyncPreviewFile): string {
  if (f.status === 'missing') return 'not in the repo at this ref — skipped'
  if (f.status === 'invalid') return 'not valid TOML — the pull would be refused'
  if (f.status === 'unchanged') return 'no changes'
  const s = f.summary
  if (!s) return f.status === 'new' ? 'new file' : 'changed'
  const noun = s.kind === 'mapping' ? 'key' : 'entry'
  const plural = (n: number) => (n === 1 ? noun : noun === 'entry' ? 'entries' : 'keys')
  const parts: string[] = []
  if (s.added.length) parts.push(`${s.added.length} ${plural(s.added.length)} added`)
  if (s.updated.length) parts.push(`${s.updated.length} updated`)
  if (s.removed.length) parts.push(`${s.removed.length} removed`)
  if (s.kind === 'collection' && s.unchanged) parts.push(`${s.unchanged} unchanged`)
  return parts.join(' · ') || (f.status === 'new' ? 'new file' : 'formatting only')
}

const STATUS_TAG: Record<SyncPreviewFile['status'], { color?: string; label: string }> = {
  new: { color: 'green', label: 'new' },
  changed: { color: 'blue', label: 'changes' },
  unchanged: { label: 'no changes' },
  missing: { label: 'skipped' },
  invalid: { color: 'red', label: 'invalid' },
}

export function DiffView({ diff }: { diff: string }) {
  const isDark = useIsDark()
  const colours: Record<DiffLineKind, { color: string; background?: string }> = {
    add: { color: isDark ? '#95de64' : '#237804', background: isDark ? 'rgba(82,196,26,0.12)' : 'rgba(82,196,26,0.10)' },
    del: { color: isDark ? '#ff7875' : '#a8071a', background: isDark ? 'rgba(255,77,79,0.12)' : 'rgba(255,77,79,0.08)' },
    hunk: { color: textMuted(isDark) },
    file: { color: textSecondary(isDark) },
    ctx: { color: textColor(isDark) },
  }
  const lines = diff.replace(/\n$/, '').split('\n')
  return (
    <pre
      data-testid="sync-diff"
      style={{ fontFamily: MONO_FONT, fontSize: 12, margin: 0, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre' }}
    >
      {lines.map((line, i) => {
        const kind = diffLineKind(line)
        return (
          <div key={i} data-kind={kind} style={{ ...colours[kind], padding: '0 6px' }}>
            {line || ' '}
          </div>
        )
      })}
    </pre>
  )
}

interface Props {
  open: boolean
  loading: boolean
  pulling: boolean
  preview: SyncPreview | null
  onPull: () => void
  onCancel: () => void
}

export function ConfigSyncPreviewModal({ open, loading, pulling, preview, onPull, onCancel }: Props) {
  const isDark = useIsDark()
  const files = preview?.files ?? []
  const withDiff = files.filter(f => f.status === 'changed' || f.status === 'new')
  const quiet = files.filter(f => !(f.status === 'changed' || f.status === 'new'))
  const invalid = files.some(f => f.status === 'invalid')
  const short = (sha?: string) => (sha ? sha.slice(0, 8) : '')

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      width={860}
      title={preview?.ok ? `Review changes from ${preview.ref}` : 'Review changes'}
      footer={
        <Space>
          <Button onClick={onCancel}>Cancel</Button>
          <Button
            type="primary"
            icon={<DownloadOutlined />}
            loading={pulling}
            disabled={loading || !preview?.ok || invalid}
            onClick={onPull}
          >
            {preview?.ok && !preview.changed ? 'Pull anyway' : 'Pull these changes'}
          </Button>
        </Space>
      }
    >
      {loading && <Text type="secondary">Fetching {preview?.ref ?? 'the tag'} and comparing with your config…</Text>}
      {!loading && preview && !preview.ok && (
        <Alert type="error" showIcon message="Couldn't preview the pull" description={preview.error} />
      )}
      {!loading && preview?.ok && (
        <>
          <Text style={{ fontSize: 13, display: 'block', marginBottom: 10, color: textSecondary(isDark) }}>
            {preview.current_ref
              ? <>From <code>{preview.current_ref}</code> ({short(preview.current_commit)}) to <code>{preview.ref}</code> ({short(preview.commit)}).</>
              : <>Pulling <code>{preview.ref}</code> ({short(preview.commit)}) for the first time.</>}
            {' '}Your own entries and protected keys are kept; each changed file is backed up first.
          </Text>
          {invalid && (
            <Alert type="error" showIcon style={{ marginBottom: 10 }}
              message="A file in the repo isn't valid TOML, so the pull would write nothing. Fix it in the repo first." />
          )}
          {withDiff.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Nothing would change — your config already matches this ref." />
          ) : (
            <Collapse
              size="small"
              defaultActiveKey={withDiff.length <= 3 ? withDiff.map(f => f.name) : [withDiff[0].name]}
              items={withDiff.map(f => ({
                key: f.name,
                label: (
                  <Space size={8}>
                    <span style={{ fontFamily: MONO_FONT }}>{f.name}</span>
                    <Tag color={STATUS_TAG[f.status].color} style={{ margin: 0 }}>{STATUS_TAG[f.status].label}</Tag>
                    <Text type="secondary" style={{ fontSize: 12 }}>{summaryLine(f)}</Text>
                  </Space>
                ),
                children: f.diff ? <DiffView diff={f.diff} /> : <Text type="secondary">No textual difference.</Text>,
              }))}
            />
          )}
          {quiet.length > 0 && (
            <div style={{ marginTop: 10 }}>
              {quiet.map(f => (
                <div key={f.name} style={{ fontSize: 12, marginTop: 3, color: textMuted(isDark) }}>
                  <span style={{ fontFamily: MONO_FONT }}>{f.name}</span>: {summaryLine(f)}
                  {f.error ? <> — <code>{f.error}</code></> : null}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Modal>
  )
}
