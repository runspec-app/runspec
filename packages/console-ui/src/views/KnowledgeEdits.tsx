// Edits to the team wiki's pages, kept on this console until they're proposed
// (backend: runspec_console/wiki_drafts.py). The Tools view uses these pieces:
//   DiffView      a unified diff of one draft against the team's page
//   PageEditor    a Markdown editor for one page's draft
//   ProposeBar    "N page edits not proposed yet · Propose changes", and the
//                 dialog that pushes them to contrib/<lane> and links the MR

import { useEffect, useState } from 'react'
import { Alert, Button, Input, Modal, Space, Typography, message } from 'antd'
import { BranchesOutlined, EditOutlined, ExportOutlined } from '@ant-design/icons'

import { bridge, type WikiDraft } from '../bridge'
import { MONO_FONT, RADIUS, surfaces, textColor, textMuted, textSecondary, useIsDark } from '../ThemeContext'

const { Text } = Typography

/** "1 page" / "3 pages". */
export function pages(n: number): string {
  return `${n} page${n === 1 ? '' : 's'}`
}

export function DiffView({ diff, maxHeight = 260 }: { diff: string; maxHeight?: number }) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  const lines = diff.split('\n').filter(l => !l.startsWith('---') && !l.startsWith('+++'))
  return (
    <div data-testid="draft-diff" style={{ fontFamily: MONO_FONT, fontSize: 12, lineHeight: 1.5, border: `1px solid ${sf.border}`, borderRadius: RADIUS.sm, overflow: 'auto', maxHeight }}>
      {lines.map((l, i) => {
        const add = l.startsWith('+'), del = l.startsWith('-'), hunk = l.startsWith('@@')
        return (
          <div key={i} style={{
            padding: '0 8px', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            color: hunk ? textMuted(isDark) : textColor(isDark),
            background: add ? 'rgba(74,194,107,0.14)' : del ? 'rgba(255,123,114,0.14)' : hunk ? sf.base : 'transparent',
          }}>{l || ' '}</div>
        )
      })}
    </div>
  )
}

export function PageEditor({ path, entity, onClose, onSaved }: {
  path: string
  entity: string
  onClose: () => void
  onSaved: () => void
}) {
  const [text, setText] = useState<string | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    bridge.knowledge_draft_text(path, entity).then(r => {
      if (!live) return
      if (!r.ok) { setError(r.error || 'Could not open the page.'); setText(''); return }
      setText(r.text ?? ''); setIsNew(r.base === null)
    }).catch(e => { if (live) { setError(String(e)); setText('') } })
    return () => { live = false }
  }, [path, entity])
  const save = async () => {
    if (text === null) return
    setSaving(true); setError('')
    try {
      const r = await bridge.knowledge_draft_save(path, text)
      if (!r.ok) { setError(r.error || 'Could not save.'); return }
      message.success(r.kept ? 'Saved as a page edit. Propose it when you are ready.' : 'No change from the team’s page.')
      onSaved(); onClose()
    } catch (e) { setError(String(e)) } finally { setSaving(false) }
  }
  return (
    <Modal open title={<span>{isNew ? 'New page ' : 'Edit '}<span style={{ fontFamily: MONO_FONT }}>{path}</span></span>}
      width={760} okText="Save edit" onOk={save} confirmLoading={saving} onCancel={onClose} okButtonProps={{ disabled: text === null }}>
      <Text style={{ display: 'block', marginBottom: 8, color: textSecondary(useIsDark()) }}>
        Your edit stays on this console until you propose it. Write what holds on any teammate&rsquo;s console: no host names, home directories or accounts.
      </Text>
      <Input.TextArea aria-label="Page Markdown" value={text ?? ''} onChange={e => setText(e.target.value)} autoSize={{ minRows: 14, maxRows: 26 }}
        style={{ fontFamily: MONO_FONT, fontSize: 13 }} disabled={text === null} />
      {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
    </Modal>
  )
}

export function ProposeBar({ drafts, configured, onChanged, onOpenSettings }: {
  drafts: WikiDraft[]
  configured: boolean
  onChanged: () => void
  onOpenSettings?: (tab: string) => void
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string; url?: string } | null>(null)
  if (drafts.length === 0 && !result) return null
  const propose = async () => {
    setBusy(true)
    try {
      const r = await bridge.memory_sync_propose()
      setOpen(false)
      if (!r.ok) setResult({ ok: false, text: `Propose failed: ${r.error || 'unknown error'}` })
      else setResult({
        ok: true,
        text: r.changed
          ? `Pushed ${pages(r.pages ?? 0)} to ${r.branch}. Open the merge request so the team can review it.`
          : `${r.branch} already has these edits.`,
        url: r.merge_request_url || undefined,
      })
      onChanged()
    } catch (e) {
      setResult({ ok: false, text: `Propose failed: ${String(e)}` })
    } finally { setBusy(false) }
  }
  return (
    <>
      {drafts.length > 0 && (
        <div data-testid="propose-bar" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderRadius: RADIUS.md, background: sf.raised, border: `1px solid ${sf.border}` }}>
          <EditOutlined style={{ color: textMuted(isDark) }} />
          <Text style={{ color: textColor(isDark) }}>{pages(drafts.length)} edited on this console, not proposed yet.</Text>
          <div style={{ flex: 1 }} />
          {configured
            ? <Button type="primary" icon={<BranchesOutlined />} onClick={() => setOpen(true)}>Propose changes</Button>
            : <Button onClick={() => onOpenSettings?.('memorySync')}>Set up the team wiki</Button>}
        </div>
      )}
      {result && (
        <Alert type={result.ok ? 'success' : 'error'} showIcon closable onClose={() => setResult(null)}
          message={<span>{result.text} {result.url && <a href={result.url} target="_blank" rel="noreferrer"><ExportOutlined /> Open merge request</a>}</span>} />
      )}
      <Modal open={open} width={760} title={`Propose ${pages(drafts.length)} to the team?`} okText="Propose changes" confirmLoading={busy}
        onOk={propose} onCancel={() => setOpen(false)}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text style={{ color: textSecondary(isDark) }}>
            These edits are pushed to your own contrib branch, then you open a merge request into the team branch. Nothing changes for the team until it is merged.
          </Text>
          {drafts.map(d => (
            <div key={d.path}>
              <Text style={{ fontFamily: MONO_FONT, color: textColor(isDark) }}>{d.path}</Text>
              <Text style={{ marginLeft: 8, fontSize: 12, color: textMuted(isDark) }}>{d.new ? 'new page · ' : ''}+{d.added} −{d.removed}</Text>
              <div style={{ marginTop: 4 }}><DiffView diff={d.diff} maxHeight={180} /></div>
            </div>
          ))}
        </Space>
      </Modal>
    </>
  )
}
