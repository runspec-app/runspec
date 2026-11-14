// Edits to the team wiki's pages, kept on this console until they're proposed
// (backend: runspec_console/wiki_drafts.py). The Tools view uses these pieces:
//   DiffView      a unified diff of one draft against the team's page
//   PageEditor    a Markdown editor for one page's draft
//   ProposeBar    "N page edits not proposed yet · Propose changes", and the
//                 dialog that pushes them to contrib/<lane> and links the MR

import { useEffect, useState } from 'react'
import { Alert, Button, Input, Modal, Segmented, Space, Typography, message } from 'antd'
import { useAccent } from '../ThemeContext'
import { BranchesOutlined, EditOutlined, ExportOutlined } from '@ant-design/icons'

import { bridge, type WikiDraft } from '../bridge'
import { Markdown } from '../components/Markdown'
import { MONO_FONT, RADIUS, surfaces, textColor, textMuted, textSecondary, useIsDark } from '../ThemeContext'
import { useRiskConfirm } from '../components/risk'

const { Text } = Typography

// Mirrors wiki_drafts.target_for: the page and section a fact is added to.
const PAGE_DIRS: Record<string, string> = {
  runnable: 'runnables', service: 'services', term: 'terms', topic: 'topics', team: 'teams', runbook: 'runbooks',
}
const PAGE_PRIORITY = ['runnable', 'service', 'term', 'topic', 'team', 'runbook']

/** The page (`runnables/cron.md`) and section a fact goes on, or null when none
 *  of its subjects has a page. Pure, for tests. */
export function pageTargetFor(fact: { kind: string; about: string[] }): { path: string; heading: string } | null {
  for (const kind of PAGE_PRIORITY) {
    const ref = fact.about.find(a => a.startsWith(kind + ':'))
    if (!ref) continue
    const slug = ref.slice(kind.length + 1).trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '') || 'page'
    const heading = kind === 'runnable'
      ? (fact.kind === 'pairing' || fact.kind === 'effect' ? 'Works with' : 'Watch out for')
      : 'Notes'
    return { path: `${PAGE_DIRS[kind]}/${slug}.md`, heading }
  }
  return null
}

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

/** A page's Markdown without its OKF frontmatter, for the editor's preview. */
export function pageBody(text: string): string {
  const m = text.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/)
  return m ? text.slice(m[0].length).replace(/^\s+/, '') : text
}

export function PageEditor({ path, entity, description = '', onClose, onSaved }: {
  path: string
  entity: string
  // The runnable's own description, prefilled under "What it's for" on a new page.
  description?: string
  onClose: () => void
  onSaved: () => void
}) {
  const [text, setText] = useState<string | null>(null)
  const [initial, setInitial] = useState<string | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'write' | 'preview'>('write')
  const isDark = useIsDark()
  useEffect(() => {
    let live = true
    bridge.knowledge_draft_text(path, entity, description).then(r => {
      if (!live) return
      if (!r.ok) { setError(r.error || 'Could not open the page.'); setText(''); return }
      setText(r.text ?? ''); setInitial(r.text ?? ''); setIsNew(r.base === null)
    }).catch(e => { if (live) { setError(String(e)); setText('') } })
    return () => { live = false }
  }, [path, entity, description])
  // Closing with unsaved typing asks first.
  // (This used antd's static Modal.confirm, which doesn't render under React
  // 19: the editor just stayed open.)
  const [askClose, closeHolder] = useRiskConfirm()
  const close = async () => {
    if (text !== null && initial !== null && text !== initial) {
      const a = await askClose({
        level: 'local', title: 'Close without saving?', action: path,
        consequence: 'Your changes to this page are lost.', okText: 'Close without saving',
      })
      if (a.ok) onClose()
    } else onClose()
  }
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
      width={760} okText="Save edit" onOk={save} confirmLoading={saving} onCancel={close} okButtonProps={{ disabled: text === null }}>
      {closeHolder}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 8 }}>
        <Text style={{ flex: 1, color: textSecondary(isDark) }}>
          Your edit stays on this console until you propose it. Write what holds on any teammate&rsquo;s console: no host names, home directories or accounts.
        </Text>
        <Segmented size="small" value={mode} onChange={v => setMode(v as 'write' | 'preview')}
          options={[{ label: 'Write', value: 'write' }, { label: 'Preview', value: 'preview' }]} />
      </div>
      {mode === 'write' ? (
        <Input.TextArea aria-label="Page Markdown" value={text ?? ''} onChange={e => setText(e.target.value)} autoSize={{ minRows: 14, maxRows: 26 }}
          style={{ fontFamily: MONO_FONT, fontSize: 13 }} disabled={text === null} />
      ) : (
        <div data-testid="page-preview" style={{ minHeight: 300, maxHeight: 560, overflowY: 'auto', padding: '8px 12px', borderRadius: RADIUS.md, border: `1px solid ${surfaces(isDark).border}`, background: surfaces(isDark).sunken }}>
          <Markdown color={textColor(isDark)}>{pageBody(text ?? '')}</Markdown>
        </div>
      )}
      {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
    </Modal>
  )
}

export function ProposeBar({ drafts, configured, onChanged, onOpenSettings, proposedBranch = '', mergeRequestUrl = '' }: {
  drafts: WikiDraft[]
  configured: boolean
  onChanged: () => void
  onOpenSettings?: (tab: string) => void
  // Where the last Propose went, for edits already proposed and waiting for merge.
  proposedBranch?: string
  mergeRequestUrl?: string
}) {
  const accent = useAccent()
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string; url?: string } | null>(null)
  const pending = drafts.filter(d => !d.proposed)
  const waiting = drafts.length - pending.length
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
          <Text style={{ color: textColor(isDark) }}>
            {pending.length > 0
              ? `${pages(pending.length)} edited on this console, not proposed yet.`
              : `${pages(waiting)} proposed in ${proposedBranch || 'your contrib branch'}, waiting for the team to merge.`}
            {pending.length > 0 && waiting > 0 && <Text style={{ color: textMuted(isDark) }}> {waiting} more waiting for merge.</Text>}
          </Text>
          <div style={{ flex: 1 }} />
          {waiting > 0 && mergeRequestUrl && (
            <a href={mergeRequestUrl} target="_blank" rel="noreferrer" style={{ color: accent }}><ExportOutlined /> Open merge request</a>
          )}
          {pending.length > 0 && (configured
            ? <Button type="primary" icon={<BranchesOutlined />} onClick={() => setOpen(true)}>Propose changes</Button>
            : <Button onClick={() => onOpenSettings?.('memorySync')}>Set up the team wiki</Button>)}
        </div>
      )}
      {result && (
        <Alert type={result.ok ? 'success' : 'error'} showIcon closable onClose={() => setResult(null)}
          message={<span>{result.text} {result.url && <a href={result.url} target="_blank" rel="noreferrer"><ExportOutlined /> Open merge request</a>}</span>} />
      )}
      <Modal open={open} width={760} title={`Propose ${pages(pending.length)} to the team?`} okText="Propose changes" confirmLoading={busy}
        onOk={propose} onCancel={() => setOpen(false)}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text style={{ color: textSecondary(isDark) }}>
            These edits are pushed to your own branch{proposedBranch ? ` (${proposedBranch})` : ''}, then you open a merge request into the team branch. Nothing changes for the team until it is merged.{waiting > 0 ? ` Edits already proposed go along unchanged.` : ''}
          </Text>
          {pending.map(d => (
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
