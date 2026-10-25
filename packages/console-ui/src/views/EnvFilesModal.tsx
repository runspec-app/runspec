/**
 * EnvFilesModal — manage each venv's `.runspec_env`, opened from the bottom of
 * the hosts panel beside the Denylist. `.runspec_env` (in the venv root) is the
 * dotenv file runspec loads into the environment before every run, so a value
 * set here reaches every runnable in that venv that reads the variable.
 *
 * Two modes:
 *  - **One venv** — a key/value table of one file, with the runnables that read
 *    each variable; secret-looking values arrive masked and are only fetched
 *    on Reveal. Saving sends just the changed keys; the backend patches the file
 *    by key (comments and untouched lines survive) and refuses if the file
 *    changed since it was loaded.
 *  - **Across venvs** — a variables × venvs grid of every venv in scope, read
 *    in one go. Cells are edited in place (or a row is set / removed in every
 *    venv shown), variables whose venvs disagree are tagged, and hidden secrets
 *    are compared by fingerprint without being revealed. Venvs with the same
 *    name and identical files share one column (an edit there changes each of them), so a
 *    fleet of same-named venvs reads as a few columns, and clicking a variable
 *    lists each value it has with the venvs holding it. Save sends each venv
 *    just its own changed keys, compare-and-swap, so one stale venv doesn't
 *    hold up the rest. Columns can be narrowed to a venv name, group, profile
 *    or host.
 *
 * Scope is the local venvs plus the connected remote hosts it is given.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, AutoComplete, Button, Empty, Input, Modal, Popconfirm, Popover, Segmented, Select, Space, Spin, Switch, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import {
  CodeOutlined, DeleteOutlined, EditOutlined, ExclamationCircleOutlined, EyeOutlined, LockOutlined, PlusOutlined, ReloadOutlined,
  SaveOutlined, SearchOutlined, UndoOutlined,
} from '@ant-design/icons'
import {
  bridge, type EnvFileReader, type EnvFileRef, type EnvFileTarget, type EnvFileVenvChange, type EnvFileView, type Host,
} from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
// Mirrors envfile._SECRET_RE on the backend (the backend decides what is masked;
// this only picks a password input for a value being typed).
const SECRET_RE = /(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|(^|_)PAT($|_)|API_?KEY|PRIVATE|CREDENTIAL|(^|_)AUTH($|_)|COOKIE|SESSION|(^|_)KEY$)/i

export const looksSecret = (key: string): boolean => SECRET_RE.test(key)
export const validKey = (key: string): boolean => KEY_RE.test(key)
export const refOf = (t: { host: string; runspec_path: string }): string => `${t.host}|${t.runspec_path}`

/** One editable row of the one-venv table. */
export interface DraftRow {
  key: string
  value: string
  secret: boolean
  masked: boolean       // value not loaded (secret) and not edited
  original: string | null  // loaded value (null = new row, or masked)
  isNew: boolean
  edited: boolean
  removed: boolean
  readers: EnvFileReader[]
}

export function draftFromView(view: EnvFileView): DraftRow[] {
  return (view.entries ?? []).map(e => ({
    key: e.key,
    value: e.value,
    secret: e.secret,
    masked: e.masked,
    original: e.masked ? null : e.value,
    isNew: false,
    edited: false,
    removed: false,
    readers: e.readers,
  }))
}

/** The `set` / `unset` a save sends — only what the operator changed. */
export function draftChanges(rows: DraftRow[]): { set: Record<string, string>; unset: string[] } {
  const set: Record<string, string> = {}
  const unset: string[] = []
  for (const r of rows) {
    if (r.removed) {
      if (!r.isNew) unset.push(r.key)
      continue
    }
    if (r.isNew || (r.edited && r.value !== r.original)) set[r.key] = r.value
  }
  return { set, unset }
}

function ReaderTags({ readers }: { readers: EnvFileReader[] }) {
  if (!readers.length) return <Text type="secondary" style={{ fontSize: 12 }}>no runnable here declares it</Text>
  return (
    <Space size={[4, 4]} wrap>
      {readers.map((r, i) => (
        <Tooltip key={i} title={<span>{r.command ? `${r.runnable} ${r.command}` : r.runnable} --{r.arg} ({r.via === 'auto' ? 'automatic' : 'declared env alias'}){r.description ? ` — ${r.description}` : ''}</span>}>
          <Tag style={{ fontFamily: MONO_FONT, fontSize: 11, marginInlineEnd: 0 }}>
            {r.runnable}{r.command ? ` ${r.command}` : ''} --{r.arg}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  )
}

export interface EnvFilesModalProps {
  open: boolean
  onClose: () => void
  /** The connected remote hosts in scope (local venvs are always included). */
  hosts: Host[]
}

export function EnvFilesModal({ open, onClose, hosts }: EnvFilesModalProps) {
  const [mode, setMode] = useState<'one' | 'many'>('one')
  const [targets, setTargets] = useState<EnvFileTarget[]>([])
  const [loadingTargets, setLoadingTargets] = useState(false)
  const hostKey = hosts.map(h => h.name).join('\u0000')

  const loadTargets = useCallback(async () => {
    setLoadingTargets(true)
    try {
      setTargets(await bridge.env_file_targets(hosts.map(h => h.name)))
    } catch (err) {
      message.error((err as Error)?.message || 'could not list venvs')
    } finally {
      setLoadingTargets(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- hostKey stands in for hosts
  }, [hostKey])

  useEffect(() => { if (open) loadTargets() }, [open, loadTargets])

  const options = useMemo(() => {
    const groups = new Map<string, { label: string; options: { value: string; label: string; disabled?: boolean }[] }>()
    for (const t of targets) {
      const g = t.local ? 'This machine' : t.group || 'Ungrouped'
      if (!groups.has(g)) groups.set(g, { label: g, options: [] })
      groups.get(g)!.options.push({
        value: refOf(t),
        label: `${t.host} · ${t.venv}${t.supported ? '' : ` — ${t.reason}`}`,
        disabled: !t.supported,
      })
    }
    return [...groups.values()]
  }, [targets])

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={mode === 'many' ? 'min(1600px, 96vw)' : 1000}
      title={<span><CodeOutlined style={{ marginRight: 8 }} />Env files — <Text code>.runspec_env</Text></span>}
      destroyOnHidden
    >
      <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
        Each venv's <Text code>.runspec_env</Text> is loaded into the environment before every run in that venv
        (a variable already set in the environment wins). Listed: the local venvs
        {hosts.length > 0 && <> and the venvs of the {hosts.length} connected host{hosts.length === 1 ? '' : 's'} in the active profile</>}.
      </Text>
      <Segmented
        value={mode}
        onChange={v => setMode(v as 'one' | 'many')}
        options={[{ label: 'One venv', value: 'one' }, { label: 'Across venvs', value: 'many' }]}
        style={{ marginBottom: 12 }}
      />
      {mode === 'one'
        ? <OneVenv targets={targets} options={options} loadingTargets={loadingTargets} />
        : <AcrossVenvs targets={targets} loadingTargets={loadingTargets} />}
    </Modal>
  )
}

// ── One venv ────────────────────────────────────────────────────────────────

function OneVenv({ targets, options, loadingTargets }: {
  targets: EnvFileTarget[]
  options: { label: string; options: { value: string; label: string; disabled?: boolean }[] }[]
  loadingTargets: boolean
}) {
  const [selected, setSelected] = useState<string>('')
  const [view, setView] = useState<EnvFileView | null>(null)
  const [rows, setRows] = useState<DraftRow[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [newKey, setNewKey] = useState('')

  const target = targets.find(t => refOf(t) === selected)
  const ref: EnvFileRef | null = target ? { host: target.host, runspec_path: target.runspec_path } : null

  // Default to the first supported venv once the list arrives.
  useEffect(() => {
    if (!selected && targets.length) {
      const first = targets.find(t => t.supported)
      if (first) setSelected(refOf(first))
    }
  }, [targets, selected])

  const load = useCallback(async (r: EnvFileRef) => {
    setLoading(true)
    try {
      const v = await bridge.env_file_read(r)
      setView(v)
      setRows(draftFromView(v))
    } catch (err) {
      setView(null)
      message.error((err as Error)?.message || 'could not read the file')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (ref) load(ref)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- selected identifies ref
  }, [selected, load])

  const changes = draftChanges(rows)
  const changeCount = Object.keys(changes.set).length + changes.unset.length

  const update = (i: number, patch: Partial<DraftRow>) =>
    setRows(prev => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  const reveal = async (i: number) => {
    if (!ref) return
    try {
      const res = await bridge.env_file_reveal({ ...ref, key: rows[i].key })
      update(i, { value: res.value, original: res.value, masked: false })
    } catch (err) {
      message.error((err as Error)?.message || 'could not reveal')
    }
  }

  const present = new Set(rows.filter(r => !r.removed).map(r => r.key))
  const suggestions = (view?.suggestions ?? []).filter(s => !present.has(s.key))
  const addKey = (key: string) => {
    const k = key.trim()
    if (!validKey(k)) { message.warning('A variable name is letters, digits and _, not starting with a digit.'); return }
    if (present.has(k)) { message.warning(`${k} is already in the file.`); return }
    const removedIdx = rows.findIndex(r => r.key === k && r.removed)
    if (removedIdx >= 0) { update(removedIdx, { removed: false }); setNewKey(''); return }
    const s = view?.suggestions?.find(x => x.key === k)
    setRows(prev => [...prev, {
      key: k, value: '', secret: s?.secret ?? looksSecret(k), masked: false, original: null,
      isNew: true, edited: true, removed: false, readers: s?.readers ?? [],
    }])
    setNewKey('')
  }

  const save = async () => {
    if (!ref || !view) return
    setSaving(true)
    try {
      const res = await bridge.env_file_save({ ...ref, sha: view.sha ?? '', ...changes })
      if (!res.ok) {
        message.error(res.error || 'save failed')
        if (res.conflict) await load(ref)
        return
      }
      const c = res.changes
      message.success(res.saved && c
        ? `Saved ${target?.host} · ${target?.venv}: ${[c.added.length && `${c.added.length} added`, c.changed.length && `${c.changed.length} changed`, c.removed.length && `${c.removed.length} removed`].filter(Boolean).join(', ') || 'no change'}`
        : 'Nothing to save')
      setView(res)
      setRows(draftFromView(res))
    } catch (err) {
      message.error((err as Error)?.message || 'save failed')
    } finally {
      setSaving(false)
    }
  }

  const columns = [
    {
      title: 'Variable',
      key: 'key',
      width: 280,
      render: (_: unknown, r: DraftRow) => (
        <span style={{ fontFamily: MONO_FONT, fontSize: 12, textDecoration: r.removed ? 'line-through' : undefined }}>
          {r.secret && <LockOutlined style={{ marginRight: 4 }} />}{r.key}
          {r.isNew && <Tag color="green" style={{ marginLeft: 6 }}>new</Tag>}
          {!r.isNew && r.edited && r.value !== r.original && !r.removed && <Tag color="gold" style={{ marginLeft: 6 }}>edited</Tag>}
        </span>
      ),
    },
    {
      title: 'Value',
      key: 'value',
      render: (_: unknown, r: DraftRow) => {
        const i = rows.indexOf(r)
        if (r.removed) return <Text type="secondary" style={{ fontSize: 12 }}>will be removed</Text>
        if (r.masked) {
          return (
            <Space size={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>•••••••• (hidden)</Text>
              <Button size="small" type="text" icon={<EyeOutlined />} onClick={() => reveal(i)} aria-label={`Reveal ${r.key}`}>Reveal</Button>
              <Button size="small" type="text" onClick={() => update(i, { masked: false, value: '', edited: true })}>Replace</Button>
            </Space>
          )
        }
        const props = {
          size: 'small' as const,
          value: r.value,
          'aria-label': `Value of ${r.key}`,
          onChange: (e: React.ChangeEvent<HTMLInputElement>) => update(i, { value: e.target.value, edited: true }),
          style: { fontFamily: MONO_FONT, fontSize: 12 },
        }
        return r.secret ? <Input.Password {...props} autoComplete="new-password" /> : <Input {...props} />
      },
    },
    {
      title: 'Read by',
      key: 'readers',
      width: 260,
      render: (_: unknown, r: DraftRow) => <ReaderTags readers={r.readers} />,
    },
    {
      title: '',
      key: 'act',
      width: 44,
      render: (_: unknown, r: DraftRow) => {
        const i = rows.indexOf(r)
        if (r.removed) return <Button size="small" type="text" icon={<UndoOutlined />} aria-label={`Keep ${r.key}`} onClick={() => update(i, { removed: false })} />
        return (
          <Button
            size="small" type="text" danger icon={<DeleteOutlined />} aria-label={`Remove ${r.key}`}
            onClick={() => (r.isNew ? setRows(prev => prev.filter((_, j) => j !== i)) : update(i, { removed: true }))}
          />
        )
      },
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 8, width: '100%' }} wrap>
        <Select
          showSearch
          size="small"
          style={{ minWidth: 380 }}
          placeholder="Pick a venv"
          loading={loadingTargets}
          value={selected || undefined}
          onChange={v => {
            if (changeCount === 0) { setSelected(v); return }
            Modal.confirm({ title: 'Discard unsaved changes?', okText: 'Discard', onOk: () => setSelected(v) })
          }}
          options={options}
          optionFilterProp="label"
          aria-label="Venv"
        />
        <Button size="small" icon={<ReloadOutlined />} disabled={!ref} loading={loading} onClick={() => ref && load(ref)}>Reload</Button>
      </Space>

      {!ref && !loadingTargets && <Empty description="No venv to manage." />}
      {loading && !view && <Spin />}
      {view && ref && (
        <>
          <div style={{ fontSize: 12, marginBottom: 8, display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            <Text code style={{ fontFamily: MONO_FONT }}>{view.path}</Text>
            {view.ok && !view.exists && <Tag>not created yet</Tag>}
            {view.mode && <Tooltip title="File mode"><Tag>{view.mode}</Tag></Tooltip>}
            {view.owner && <Tooltip title="Owner of the venv"><Tag>owner {view.owner}</Tag></Tooltip>}
            {view.as_user && <Tooltip title="The SSH login can't edit this file, so the console reads and writes it as this account (sudo -n -u)."><Tag color="blue">as {view.as_user}</Tag></Tooltip>}
          </div>
          {view.error && <Alert type="error" showIcon style={{ marginBottom: 8 }} message={view.error} />}
          {view.ok && !view.writable && <Alert type="warning" showIcon style={{ marginBottom: 8 }} message="Read-only" description={view.write_error || 'This file cannot be written from here.'} />}
          {(view.warnings ?? []).map((w, i) => <Alert key={i} type="warning" showIcon style={{ marginBottom: 8 }} message={w} />)}
          {(view.overrides ?? []).length > 0 && (
            <Alert
              type="info" showIcon style={{ marginBottom: 8 }}
              message={`${view.overrides!.length} runnable${view.overrides!.length === 1 ? '' : 's'} in this venv read their own file instead`}
              description={view.overrides!.map(o => `${o.runnable} → ${o.file}`).join('; ')}
            />
          )}
          {view.ok && (
            <>
              <Table<DraftRow>
                size="small"
                rowKey={r => `${r.key}${r.isNew ? '+new' : ''}`}
                columns={columns}
                dataSource={rows}
                pagination={false}
                scroll={{ y: 360 }}
                locale={{ emptyText: 'No variables set.' }}
                style={{ marginBottom: 8 }}
              />
              <Space wrap style={{ marginBottom: 4 }}>
                <AutoComplete
                  size="small"
                  style={{ width: 380 }}
                  value={newKey}
                  onChange={setNewKey}
                  onSelect={(v: string) => addKey(v)}
                  options={suggestions.map(s => ({
                    value: s.key,
                    label: (
                      <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
                        {s.secret && <LockOutlined style={{ marginRight: 4 }} />}{s.key}
                        <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                          {s.readers.map(r => `${r.runnable} --${r.arg}`).join(', ')}
                        </Text>
                      </span>
                    ),
                  }))}
                  filterOption={(input, opt) => String(opt?.value ?? '').toUpperCase().includes(input.toUpperCase())}
                  placeholder="Add a variable (suggestions are what this venv's runnables read)"
                  aria-label="New variable"
                  onKeyDown={e => { if (e.key === 'Enter' && newKey && !suggestions.some(s => s.key === newKey)) addKey(newKey) }}
                />
                <Button size="small" icon={<PlusOutlined />} disabled={!newKey} onClick={() => addKey(newKey)}>Add</Button>
                <div style={{ flex: 1 }} />
                <Button size="small" disabled={!changeCount} onClick={() => setRows(draftFromView(view))}>Discard</Button>
                <Popconfirm
                  title={`Save ${changeCount} change${changeCount === 1 ? '' : 's'}?`}
                  description={
                    <span style={{ fontSize: 13 }}>
                      {Object.keys(changes.set).length > 0 && <>Set: <Text code>{Object.keys(changes.set).join(', ')}</Text><br /></>}
                      {changes.unset.length > 0 && <>Remove: <Text code>{changes.unset.join(', ')}</Text><br /></>}
                      on {target?.host} · {target?.venv}
                    </span>
                  }
                  okText="Save"
                  onConfirm={save}
                  disabled={!changeCount || !view.writable}
                >
                  <Button size="small" type="primary" icon={<SaveOutlined />} loading={saving} disabled={!changeCount || !view.writable}>
                    Save{changeCount ? ` (${changeCount})` : ''}
                  </Button>
                </Popconfirm>
              </Space>
            </>
          )}
        </>
      )}
    </div>
  )
}

// ── Across venvs: a variables × venvs grid ──────────────────────────────────

/** A pending grid edit: a new value, or `null` = remove the key from that venv. */
export type CellEdit = string | null
/** Pending edits by venv ref, then by key. */
export type GridDraft = Record<string, Record<string, CellEdit>>

/** What one venv's file holds for one key, plus any pending edit. */
export interface GridCell {
  present: boolean      // the file sets it now
  value: string         // '' when masked
  masked: boolean
  fp: string            // masked only: fingerprint of the hidden value
  edit: CellEdit | undefined
}

export function gridCell(view: EnvFileView | undefined, key: string, edit: CellEdit | undefined): GridCell {
  const e = view?.entries?.find(x => x.key === key)
  return { present: !!e, value: e?.value ?? '', masked: !!e?.masked, fp: e?.fp ?? '', edit }
}

/** Whether the cell will hold a value after the pending edit. */
export const cellSet = (c: GridCell): boolean => (c.edit === undefined ? c.present : c.edit !== null)

/** A token that is equal for two cells holding the same value (secrets compare
 *  by fingerprint; a typed value can't be compared with a hidden one). `null`
 *  when the cell is unset. */
export function cellToken(c: GridCell): string | null {
  if (!cellSet(c)) return null
  if (c.edit !== undefined && c.edit !== null) return `v:${c.edit}`
  return c.masked ? `fp:${c.fp}` : `v:${c.value}`
}

/** How a variable looks across venvs: how many set it, how many distinct
 *  values they hold, and the most common value's token. */
export function rowSummary(cells: GridCell[]): { setIn: number; total: number; distinct: number; common: string | null } {
  const counts = new Map<string, number>()
  for (const c of cells) {
    const t = cellToken(c)
    if (t !== null) counts.set(t, (counts.get(t) ?? 0) + 1)
  }
  let common: string | null = null
  let best = 0
  for (const [t, n] of counts) if (n > best) { best = n; common = t }
  const setIn = [...counts.values()].reduce((a, b) => a + b, 0)
  return { setIn, total: cells.length, distinct: counts.size, common }
}

/** Record an edit, dropping it when it puts the cell back the way it was. */
export function withEdit(draft: GridDraft, view: EnvFileView | undefined, ref: string, key: string, edit: CellEdit | undefined): GridDraft {
  const cur = gridCell(view, key, undefined)
  const noop = edit === undefined
    || (edit === null && !cur.present)
    || (edit !== null && cur.present && !cur.masked && edit === cur.value)
  const venv = { ...(draft[ref] ?? {}) }
  if (noop) delete venv[key]
  else venv[key] = edit
  const next = { ...draft }
  if (Object.keys(venv).length) next[ref] = venv
  else delete next[ref]
  return next
}

/** One `env_file_save_many` change per venv with edits. */
export function gridChanges(draft: GridDraft, views: Record<string, EnvFileView>): EnvFileVenvChange[] {
  const out: EnvFileVenvChange[] = []
  for (const [ref, edits] of Object.entries(draft)) {
    const view = views[ref]
    if (!view) continue
    const set: Record<string, string> = {}
    const unset: string[] = []
    for (const [key, edit] of Object.entries(edits)) {
      if (edit === null) unset.push(key)
      else set[key] = edit
    }
    if (Object.keys(set).length || unset.length) {
      out.push({ host: view.host, runspec_path: view.runspec_path, sha: view.sha ?? '', set, unset })
    }
  }
  return out
}

/** What makes two venvs share a grid column: the same file as loaded (secrets
 *  by fingerprint), the same pending edits and the same writability. `null`
 *  for a venv that wasn't read, which always gets a column of its own. */
export function venvSignature(view: EnvFileView | undefined, edits: Record<string, CellEdit> | undefined): string | null {
  if (!view?.ok) return null
  const loaded = (view.entries ?? []).map(e => `${e.key}=${e.masked ? `fp:${e.fp ?? ''}` : `v:${e.value}`}`).sort()
  const pending = Object.entries(edits ?? {}).map(([k, v]) => `${k}=${v === null ? 'rm' : `v:${v}`}`).sort()
  return JSON.stringify([!!view.writable, loaded, pending])
}

/** One grid column: a venv, or several venvs with identical files. */
export interface VenvColumn { id: string; refs: string[] }

/** The grid's columns, in venv order. With `merge`, venvs with the same
 *  name (`nameOf`) and the same signature share one column (an edit there
 *  applies to each of them); venvs with different names never share one,
 *  even when their files match. */
export function venvColumns(refs: string[], views: Record<string, EnvFileView>, draft: GridDraft, merge: boolean, nameOf: (ref: string) => string = () => ''): VenvColumn[] {
  const out: VenvColumn[] = []
  const bySig = new Map<string, VenvColumn>()
  for (const ref of refs) {
    const file = merge ? venvSignature(views[ref], draft[ref]) : null
    const sig = file === null ? null : JSON.stringify([nameOf(ref), file])
    const col = sig === null ? undefined : bySig.get(sig)
    if (col) { col.refs.push(ref); continue }
    const fresh = { id: ref, refs: [ref] }
    if (sig !== null) bySig.set(sig, fresh)
    out.push(fresh)
  }
  return out
}

/** The venvs holding each value of one variable, most common first; the
 *  venvs that don't set it (`token: null`) last. */
export function valueGroups(refs: string[], views: Record<string, EnvFileView>, draft: GridDraft, key: string): { token: string | null; refs: string[] }[] {
  const by = new Map<string | null, string[]>()
  for (const ref of refs) {
    const t = cellToken(gridCell(views[ref], key, draft[ref]?.[key]))
    by.set(t, [...(by.get(t) ?? []), ref])
  }
  return [...by.entries()].map(([token, rs]) => ({ token, refs: rs }))
    .sort((a, b) => (a.token === null ? 1 : 0) - (b.token === null ? 1 : 0) || b.refs.length - a.refs.length)
}

const EDIT_BG = {
  added: 'rgba(82, 196, 26, 0.16)',
  changed: 'rgba(250, 173, 20, 0.18)',
  removed: 'rgba(255, 77, 79, 0.14)',
}
const COL_W = 240
const KEY_W = 300
const WRAP = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const

type Scope = string  // 'all' | 'venv:<v>' | 'group:<g>' | 'profile:<p>' | 'host:<h>'

function inScope(t: EnvFileTarget, scope: Scope): boolean {
  if (scope === 'all') return true
  const [kind, ...rest] = scope.split(':')
  const v = rest.join(':')
  if (kind === 'venv') return t.venv === v
  if (kind === 'group') return !t.local && t.group === v
  if (kind === 'profile') return t.profiles.includes(v)
  if (kind === 'host') return t.host === v
  return true
}

function AcrossVenvs({ targets, loadingTargets }: { targets: EnvFileTarget[]; loadingTargets: boolean }) {
  const supported = useMemo(() => targets.filter(t => t.supported), [targets])
  const unsupported = targets.length - supported.length
  const [views, setViews] = useState<Record<string, EnvFileView>>({})
  const [loading, setLoading] = useState(false)
  const [draft, setDraft] = useState<GridDraft>({})
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [extraKeys, setExtraKeys] = useState<string[]>([])
  const [scope, setScope] = useState<Scope>('all')
  const [filter, setFilter] = useState('')
  const [onlyDiff, setOnlyDiff] = useState(false)
  const [merge, setMerge] = useState(true)
  const [editing, setEditing] = useState<{ refs: string[]; key: string } | null>(null)
  const [editValue, setEditValue] = useState('')
  const [detailKey, setDetailKey] = useState<string | null>(null)
  const [newKey, setNewKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [problems, setProblems] = useState<string[]>([])

  const load = useCallback(async (list: EnvFileTarget[]) => {
    if (!list.length) return
    setLoading(true)
    try {
      const res = await bridge.env_file_read_many({ targets: list.map(t => ({ host: t.host, runspec_path: t.runspec_path })) })
      setViews(Object.fromEntries(res.views.map(v => [refOf(v), v])))
      setDraft({})
      setRevealed({})
      setProblems([])
    } catch (err) {
      message.error((err as Error)?.message || 'could not read the files')
    } finally {
      setLoading(false)
    }
  }, [])

  const targetKey = supported.map(refOf).join('\u0000')
  // eslint-disable-next-line react-hooks/exhaustive-deps -- targetKey stands in for supported
  useEffect(() => { load(supported) }, [targetKey, load])

  const targetOf = useMemo(() => new Map(supported.map(t => [refOf(t), t])), [supported])
  const shown = supported.filter(t => inScope(t, scope))
  const shownRefs = shown.map(refOf)
  const cols = venvColumns(shownRefs, views, draft, merge, ref => targetOf.get(ref)?.venv ?? ref)
  const editCount = Object.values(draft).reduce((n, e) => n + Object.keys(e).length, 0)
  const venvsEdited = Object.keys(draft).length

  const count = (s: Scope) => supported.filter(t => inScope(t, s)).length
  const venvNames = [...new Set(supported.map(t => t.venv))].sort()
  const groups = [...new Set(supported.filter(t => !t.local && t.group).map(t => t.group))].sort()
  const profiles = [...new Set(supported.flatMap(t => t.profiles))].sort()
  const hostNames = [...new Set(supported.map(t => t.host))]
  const scopeOptions = [
    { value: 'all', label: `All venvs (${supported.length})` },
    { label: 'Venv', options: venvNames.map(v => ({ value: `venv:${v}`, label: `${v} (${count(`venv:${v}`)})` })) },
    ...(groups.length ? [{ label: 'Group', options: groups.map(g => ({ value: `group:${g}`, label: `${g} (${count(`group:${g}`)})` })) }] : []),
    ...(profiles.length ? [{ label: 'Profile', options: profiles.map(p => ({ value: `profile:${p}`, label: `${p} (${count(`profile:${p}`)})` })) }] : []),
    { label: 'Host', options: hostNames.map(h => ({ value: `host:${h}`, label: `${h} (${count(`host:${h}`)})` })) },
  ]

  // Every key any shown venv sets (or has a pending edit for), plus keys added here.
  const allKeys = useMemo(() => {
    const keys = new Set<string>(extraKeys)
    for (const ref of shownRefs) {
      for (const e of views[ref]?.entries ?? []) keys.add(e.key)
      for (const k of Object.keys(draft[ref] ?? {})) keys.add(k)
    }
    return [...keys].sort()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- shownRefs is derived from scope + targets
  }, [views, draft, extraKeys, scope, targetKey])

  const readable = shownRefs.filter(r => views[r]?.ok)
  const readerMap = useMemo(() => {
    const m = new Map<string, EnvFileReader[]>()
    for (const v of Object.values(views)) {
      for (const e of v.entries ?? []) {
        const rs = m.get(e.key) ?? []
        for (const r of e.readers) if (!rs.some(x => x.runnable === r.runnable && x.arg === r.arg && x.command === r.command)) rs.push(r)
        m.set(e.key, rs)
      }
      for (const s of v.suggestions ?? []) {
        const rs = m.get(s.key) ?? []
        for (const r of s.readers) if (!rs.some(x => x.runnable === r.runnable && x.arg === r.arg && x.command === r.command)) rs.push(r)
        m.set(s.key, rs)
      }
    }
    return m
  }, [views])
  const secretKey = (key: string) => looksSecret(key) || Object.values(views).some(v => v.entries?.some(e => e.key === key && e.secret))
    || (readerMap.get(key) ?? []).some(r => r.password)

  const rows = allKeys.map(key => {
    const cells = readable.map(r => gridCell(views[r], key, draft[r]?.[key]))
    return { key, secret: secretKey(key), summary: rowSummary(cells) }
  }).filter(r => (!filter || r.key.toUpperCase().includes(filter.toUpperCase()))
    && (!onlyDiff || r.summary.distinct > 1 || r.summary.setIn < r.summary.total))

  // Hidden values that differ get a letter each (A, B, …), the same letter
  // for the same fingerprint, so venvs can be compared without revealing.
  const fpLetters = (key: string) => {
    const m = new Map<string, string>()
    for (const r of readable) {
      const c = gridCell(views[r], key, undefined)
      if (c.masked && !m.has(c.fp)) m.set(c.fp, String.fromCharCode(65 + m.size))
    }
    return m
  }

  const writable = (ref: string) => !!views[ref]?.ok && !!views[ref]?.writable
  const venvLabel = (ref: string) => { const t = targetOf.get(ref); return t ? `${t.host} · ${t.venv}` : ref }
  /** How a column is named in labels: `host · venv`, or `ops on 37 hosts`. */
  const colLabel = (refs: string[]) => {
    if (refs.length === 1) return venvLabel(refs[0])
    const ts = refs.map(r => targetOf.get(r)).filter((t): t is EnvFileTarget => !!t)
    const venvs = new Set(ts.map(t => t.venv))
    const hosts = new Set(ts.map(t => t.host))
    return venvs.size === 1 && hosts.size === refs.length ? `${ts[0].venv} on ${refs.length} hosts` : `${refs.length} venvs`
  }
  const edit = (refs: string[], key: string, e: CellEdit | undefined) =>
    setDraft(d => refs.reduce((acc, ref) => withEdit(acc, views[ref], ref, key, e), d))

  const startEdit = (refs: string[], key: string) => {
    const c = gridCell(views[refs[0]], key, draft[refs[0]]?.[key])
    const rv = revealed[`${refs[0]}|${key}`]
    setEditValue(c.edit != null ? c.edit : c.masked ? (rv ?? '') : c.value)
    setEditing({ refs, key })
  }
  const commitEdit = () => {
    if (!editing) return
    const { refs, key } = editing
    const c = gridCell(views[refs[0]], key, undefined)
    const rv = revealed[`${refs[0]}|${key}`]
    // An untouched hidden secret (or one revealed and left alone) is no edit.
    if (c.masked && (editValue === '' || editValue === rv) && draft[refs[0]]?.[key] === undefined) { setEditing(null); return }
    edit(refs, key, c.masked && editValue === rv ? undefined : editValue)
    setEditing(null)
  }

  // Venvs sharing a fingerprint hold the same value, so one reveal serves them all.
  const reveal = async (refs: string[], key: string) => {
    const v = views[refs[0]]
    if (!v) return
    try {
      const res = await bridge.env_file_reveal({ host: v.host, runspec_path: v.runspec_path, key })
      setRevealed(prev => ({ ...prev, ...Object.fromEntries(refs.map(r => [`${r}|${key}`, res.value])) }))
    } catch (err) {
      message.error((err as Error)?.message || 'could not reveal')
    }
  }

  const setEverywhere = (key: string, value: string) => edit(shownRefs.filter(writable), key, value)
  const removeEverywhere = (key: string) => edit(shownRefs.filter(writable), key, null)
  const undoRow = (key: string) => edit(shownRefs, key, undefined)

  const suggestions = useMemo(() => {
    const present = new Set(allKeys)
    return [...readerMap.entries()].filter(([k]) => !present.has(k)).map(([k, rs]) => ({ key: k, readers: rs })).sort((a, b) => a.key.localeCompare(b.key))
  }, [readerMap, allKeys])
  const addKey = (key: string) => {
    const k = key.trim()
    if (!validKey(k)) { message.warning('A variable name is letters, digits and _, not starting with a digit.'); return }
    if (!allKeys.includes(k)) setExtraKeys(prev => [...prev, k])
    setFilter('')
    setOnlyDiff(false)
    setNewKey('')
  }

  const changes = gridChanges(draft, views)
  // The save summary lists identical changes once, with the venvs they go to.
  const changeGroups = useMemo(() => {
    const by = new Map<string, { set: string[]; unset: string[]; refs: string[] }>()
    for (const c of changes) {
      const set = Object.keys(c.set).sort()
      const unset = [...c.unset].sort()
      const k = JSON.stringify([set, unset])
      const g = by.get(k) ?? { set, unset, refs: [] }
      g.refs.push(refOf(c))
      by.set(k, g)
    }
    return [...by.values()]
  }, [changes])
  const save = async () => {
    setSaving(true)
    try {
      const res = await bridge.env_file_save_many({ changes })
      const nextViews = { ...views }
      let nextDraft = { ...draft }
      const failed: string[] = []
      for (const r of res.rows) {
        const ref = refOf(r)
        if (r.view) nextViews[ref] = r.view
        if (r.ok) delete nextDraft[ref]
        else failed.push(`${r.host} · ${r.venv}: ${r.error || 'failed'}${r.conflict ? ' It has been reloaded; check its column and save again.' : ''}`)
      }
      // Drop edits a reload made moot (e.g. the venv now already has the value).
      for (const ref of Object.keys(nextDraft)) {
        for (const [k, e] of Object.entries(nextDraft[ref])) nextDraft = withEdit(nextDraft, nextViews[ref], ref, k, e)
      }
      setViews(nextViews)
      setDraft(nextDraft)
      setProblems(failed)
      if (res.failed) message.warning(`${res.saved} venv${res.saved === 1 ? '' : 's'} saved, ${res.failed} failed`)
      else message.success(`Saved ${res.saved} venv${res.saved === 1 ? '' : 's'}`)
    } catch (err) {
      message.error((err as Error)?.message || 'save failed')
    } finally {
      setSaving(false)
    }
  }

  const reload = () => {
    if (!editCount) { load(supported); return }
    Modal.confirm({ title: 'Discard unsaved changes?', okText: 'Discard and reload', onOk: () => load(supported) })
  }

  const renderCell = (refs: string[], key: string, common: string | null, distinct: number, letters: Map<string, string>) => {
    const ref = refs[0]
    const view = views[ref]
    if (!view) return null
    if (!view.ok) return <Text type="secondary" style={{ fontSize: 12 }}>unreadable</Text>
    const label = colLabel(refs)
    const c = gridCell(view, key, draft[ref]?.[key])
    const canEdit = !!view.writable
    if (editing && editing.refs[0] === ref && editing.key === key) {
      const inputProps = {
        size: 'small' as const,
        autoFocus: true,
        value: editValue,
        placeholder: c.masked ? 'new value' : '',
        'aria-label': `Value of ${key} on ${label}`,
        onChange: (e: React.ChangeEvent<HTMLInputElement>) => setEditValue(e.target.value),
        onPressEnter: commitEdit,
        onBlur: commitEdit,
        onKeyDown: (e: React.KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); setEditing(null) } },
        style: { fontFamily: MONO_FONT, fontSize: 12 },
      }
      return secretKey(key) ? <Input.Password {...inputProps} autoComplete="new-password" /> : <Input {...inputProps} />
    }
    const editKind = c.edit === undefined ? null : c.edit === null ? 'removed' : c.present ? 'changed' : 'added'
    const token = cellToken(c)
    const odd = token !== null && distinct > 1 && token !== common
    const rv = revealed[`${ref}|${key}`]
    let body: React.ReactNode
    if (c.edit === null) {
      body = <Text delete type="secondary" style={{ fontFamily: MONO_FONT, fontSize: 12, ...WRAP }}>{c.masked ? (rv ?? '••••••') : c.value || '""'}</Text>
    } else if (c.edit !== undefined) {
      body = <span style={{ fontFamily: MONO_FONT, fontSize: 12, ...WRAP }}>{secretKey(key) ? '•'.repeat(Math.min(Math.max(c.edit.length, 6), 12)) : c.edit || <Text type="secondary">""</Text>}</span>
    } else if (!c.present) {
      body = <Text type="secondary" style={{ fontSize: 12 }}>–</Text>
    } else if (c.masked) {
      const letter = distinct > 1 ? letters.get(c.fp) : undefined
      body = (
        <Space size={2}>
          {rv !== undefined
            ? <Text type={odd ? 'warning' : undefined} style={{ fontFamily: MONO_FONT, fontSize: 12, ...WRAP }}>{rv}</Text>
            : <Text type={odd ? 'warning' : 'secondary'} style={{ fontSize: 12 }}>••••••{letter && <Tooltip title={`Hidden value ${letter} — venvs marked with the same letter hold the same secret`}><Tag style={{ marginLeft: 4, fontSize: 10, lineHeight: '14px', padding: '0 3px' }}>{letter}</Tag></Tooltip>}</Text>}
          {rv === undefined && (
            <Tooltip title="Reveal">
              <Button size="small" type="text" icon={<EyeOutlined />} aria-label={`Reveal ${key} on ${label}`}
                onClick={e => { e.stopPropagation(); reveal(refs, key) }} style={{ height: 18, width: 18, minWidth: 18 }} />
            </Tooltip>
          )}
        </Space>
      )
    } else {
      body = (
        <Text type={odd ? 'warning' : undefined} style={{ fontFamily: MONO_FONT, fontSize: 12, ...WRAP }}>
          {c.value === '' ? <Text type="secondary">""</Text> : c.value}
        </Text>
      )
    }
    return (
      <div
        role={canEdit ? 'button' : undefined}
        tabIndex={canEdit ? 0 : undefined}
        aria-label={canEdit ? `Edit ${key} on ${label}` : undefined}
        onClick={canEdit ? () => startEdit(refs, key) : undefined}
        onKeyDown={canEdit ? e => { if (e.key === 'Enter') startEdit(refs, key) } : undefined}
        style={{
          display: 'flex', alignItems: 'flex-start', gap: 4, minHeight: 24, margin: '-4px -8px', padding: '4px 8px',
          cursor: canEdit ? 'pointer' : 'default', background: editKind ? EDIT_BG[editKind] : undefined,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>{body}</div>
        {editKind && (
          <Tooltip title={refs.length > 1 ? `Undo this change in all ${refs.length} venvs` : 'Undo this change'}>
            <Button size="small" type="text" icon={<UndoOutlined />} aria-label={`Undo ${key} on ${label}`}
              onClick={e => { e.stopPropagation(); edit(refs, key, undefined) }} style={{ height: 18, width: 18, minWidth: 18 }} />
          </Tooltip>
        )}
        {!editKind && canEdit && c.present && (
          <Tooltip title={refs.length > 1 ? `Remove from all ${refs.length} venvs` : 'Remove from this venv'}>
            <Button size="small" type="text" icon={<DeleteOutlined />} aria-label={`Remove ${key} on ${label}`}
              className="rs-env-cell-remove"
              onClick={e => { e.stopPropagation(); edit(refs, key, null) }} style={{ height: 18, width: 18, minWidth: 18 }} />
          </Tooltip>
        )}
      </div>
    )
  }

  const colHeader = (refs: string[]) => {
    const v = views[refs[0]]
    const ts = refs.map(r => targetOf.get(r)).filter((t): t is EnvFileTarget => !!t)
    const t = ts[0]
    const pending = draft[refs[0]] ? Object.keys(draft[refs[0]]).length : 0
    const many = refs.length > 1
    const hosts = [...new Set(ts.map(x => x.host))]
    const venvs = [...new Set(ts.map(x => x.venv))]
    const LIST_MAX = 40
    const tip = (
      <span>
        {many
          ? <>Same file in these {refs.length} venvs; an edit here changes all of them.<br />
            {ts.slice(0, LIST_MAX).map(x => <span key={refOf(x)}>{x.host} · {x.path}<br /></span>)}
            {ts.length > LIST_MAX && <>…and {ts.length - LIST_MAX} more<br /></>}</>
          : t?.path}
        {v?.as_user ? <><br />read and written as {v.as_user}</> : null}
        {v?.error ? <><br />{v.error}</> : null}
        {v?.ok && !v.writable ? <><br />{v.write_error || 'read-only'}</> : null}
      </span>
    )
    return (
      <Tooltip title={tip} styles={{ root: { maxWidth: 520 }, body: { maxHeight: 360, overflowY: 'auto' } }}>
        <div style={{ lineHeight: 1.25 }}>
          <div style={{ fontSize: 12, ...WRAP }}>
            {v && !v.ok && <ExclamationCircleOutlined style={{ color: '#ff4d4f', marginRight: 4 }} />}
            {v?.ok && !v.writable && <LockOutlined style={{ marginRight: 4 }} />}
            {many ? (venvs.length === 1 ? venvs[0] : `${refs.length} venvs`) : t?.host}
            {many && <Tag color="blue" style={{ marginLeft: 4, fontSize: 10, lineHeight: '14px', padding: '0 3px' }}>{hosts.length} host{hosts.length === 1 ? '' : 's'}</Tag>}
            {pending > 0 && <Tag color="gold" style={{ marginLeft: 4, fontSize: 10, lineHeight: '14px', padding: '0 3px' }}>{pending}</Tag>}
          </div>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 'normal', ...WRAP }}>
            {many ? `${hosts.slice(0, 2).join(', ')}${hosts.length > 2 ? ` +${hosts.length - 2}` : ''}` : t?.venv}
          </Text>
        </div>
      </Tooltip>
    )
  }

  type Row = (typeof rows)[number]
  const columns = [
    {
      title: 'Variable',
      key: 'key',
      width: KEY_W,
      fixed: 'left' as const,
      render: (_: unknown, r: Row) => {
        const { setIn, total, distinct } = r.summary
        const rowEdited = shownRefs.some(ref => draft[ref]?.[r.key] !== undefined)
        const readers = readerMap.get(r.key) ?? []
        return (
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Tooltip title={<span>{readers.length ? <ReaderTags readers={readers} /> : 'No runnable in these venvs declares it.'}<br />Click for every value it has and where.</span>}>
                <a
                  role="button" aria-label={`Show ${r.key} across venvs`} onClick={() => setDetailKey(r.key)}
                  style={{ fontFamily: MONO_FONT, fontSize: 12, wordBreak: 'break-all', color: 'inherit' }}
                >
                  {r.secret && <LockOutlined style={{ marginRight: 4 }} />}{r.key}
                </a>
              </Tooltip>
              <div style={{ marginTop: 2 }}>
                {distinct > 1 && <Tag color="gold" style={{ fontSize: 10, lineHeight: '15px', padding: '0 4px', cursor: 'pointer' }} onClick={() => setDetailKey(r.key)}>{distinct} values</Tag>}
                {total > 0 && setIn < total && <Tag style={{ fontSize: 10, lineHeight: '15px', padding: '0 4px' }}>set in {setIn} of {total}</Tag>}
              </div>
            </div>
            <SetEverywhere keyName={r.key} secret={r.secret} count={shownRefs.filter(writable).length} onSet={v => setEverywhere(r.key, v)} />
            <Tooltip title="Remove from every venv shown">
              <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label={`Remove ${r.key} everywhere`}
                disabled={!shownRefs.some(ref => writable(ref) && gridCell(views[ref], r.key, undefined).present)}
                onClick={() => removeEverywhere(r.key)} />
            </Tooltip>
            {rowEdited && (
              <Tooltip title="Undo this row's changes">
                <Button size="small" type="text" icon={<UndoOutlined />} aria-label={`Undo ${r.key}`} onClick={() => undoRow(r.key)} />
              </Tooltip>
            )}
          </div>
        )
      },
    },
    ...cols.map(col => ({
      key: col.id,
      width: COL_W,
      title: colHeader(col.refs),
      render: (_: unknown, r: Row) => renderCell(col.refs, r.key, r.summary.common, r.summary.distinct, r.summary.distinct > 1 ? fpLetters(r.key) : new Map()),
    })),
  ]

  const merged = cols.length < shownRefs.length

  return (
    <div>
      <style>{'.rs-env-grid .rs-env-cell-remove{opacity:0}.rs-env-grid td:hover .rs-env-cell-remove{opacity:1}'}</style>
      <Space wrap style={{ marginBottom: 8, width: '100%' }}>
        <Input
          size="small" allowClear style={{ width: 240 }} placeholder="Filter variables" prefix={<SearchOutlined />}
          value={filter} onChange={e => setFilter(e.target.value)} aria-label="Filter variables"
        />
        <Select
          size="small" style={{ minWidth: 220 }} value={scope} onChange={setScope} options={scopeOptions} aria-label="Venvs shown"
          showSearch optionFilterProp="label" popupMatchSelectWidth={false}
        />
        <Space size={4}>
          <Switch size="small" checked={merge} onChange={setMerge} aria-label="Group identical venvs" />
          <Text style={{ fontSize: 12 }}>Group identical venvs</Text>
        </Space>
        <Space size={4}>
          <Switch size="small" checked={onlyDiff} onChange={setOnlyDiff} aria-label="Only differences" />
          <Text style={{ fontSize: 12 }}>Only differences</Text>
        </Space>
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={reload}>Reload</Button>
        {merged && <Text type="secondary" style={{ fontSize: 12 }}>{shownRefs.length} venvs in {cols.length} column{cols.length === 1 ? '' : 's'}</Text>}
        {unsupported > 0 && <Text type="secondary" style={{ fontSize: 12 }}>{unsupported} venv{unsupported === 1 ? '' : 's'} can't be managed here (see One venv)</Text>}
      </Space>

      {problems.length > 0 && (
        <Alert
          type="error" showIcon closable onClose={() => setProblems([])} style={{ marginBottom: 8 }}
          message={`${problems.length} venv${problems.length === 1 ? '' : 's'} not saved`}
          description={<ul style={{ margin: 0, paddingLeft: 18 }}>{problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
        />
      )}

      {!supported.length && !loadingTargets && <Empty description="No venv to manage." />}
      {supported.length > 0 && (
        <Spin spinning={loading}>
          <Table<Row>
            className="rs-env-grid"
            size="small"
            bordered
            rowKey="key"
            columns={columns}
            dataSource={rows}
            pagination={false}
            scroll={{ x: KEY_W + COL_W * cols.length, y: '52vh' }}
            locale={{ emptyText: filter || onlyDiff ? 'No variable matches.' : 'No variables set in these venvs.' }}
            style={{ marginBottom: 8 }}
          />
        </Spin>
      )}

      <Space wrap style={{ width: '100%' }}>
        <AutoComplete
          size="small"
          style={{ width: 380 }}
          value={newKey}
          onChange={setNewKey}
          onSelect={(v: string) => addKey(v)}
          options={suggestions.map(s => ({
            value: s.key,
            label: (
              <span style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
                {s.key}
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>{s.readers.map(r => `${r.runnable} --${r.arg}`).join(', ')}</Text>
              </span>
            ),
          }))}
          filterOption={(input, opt) => String(opt?.value ?? '').toUpperCase().includes(input.toUpperCase())}
          placeholder="Add a variable row (suggestions are what these venvs' runnables read)"
          aria-label="New variable"
          onKeyDown={e => { if (e.key === 'Enter' && newKey && !suggestions.some(s => s.key === newKey)) addKey(newKey) }}
        />
        <Button size="small" icon={<PlusOutlined />} disabled={!newKey} onClick={() => addKey(newKey)}>Add</Button>
        <div style={{ flex: 1 }} />
        <Text type="secondary" style={{ fontSize: 12 }}>Click a cell to edit it, or a variable to see all its values.</Text>
        <Button size="small" disabled={!editCount} onClick={() => setDraft({})}>Discard</Button>
        <Popconfirm
          title={`Save ${editCount} change${editCount === 1 ? '' : 's'} in ${venvsEdited} venv${venvsEdited === 1 ? '' : 's'}?`}
          description={
            <div style={{ fontSize: 13, maxHeight: 260, overflowY: 'auto', maxWidth: 520 }}>
              {changeGroups.map(g => (
                <div key={g.refs[0]} style={{ marginBottom: 4 }}>
                  <Tooltip title={g.refs.length > 1 ? g.refs.map(venvLabel).join(', ') : undefined}>
                    <Text strong>{colLabel(g.refs)}</Text>
                  </Tooltip>
                  {g.set.length > 0 && <> set <Text code>{g.set.join(', ')}</Text></>}
                  {g.unset.length > 0 && <> remove <Text code>{g.unset.join(', ')}</Text></>}
                </div>
              ))}
              <Text type="secondary" style={{ fontSize: 12 }}>Each file changes only these keys, and only if it hasn't changed since it was loaded.</Text>
            </div>
          }
          okText="Save"
          onConfirm={save}
          disabled={!editCount}
        >
          <Button size="small" type="primary" icon={<SaveOutlined />} loading={saving} disabled={!editCount}>
            Save{editCount ? ` (${editCount})` : ''}
          </Button>
        </Popconfirm>
      </Space>

      <Modal
        open={detailKey !== null}
        onCancel={() => setDetailKey(null)}
        footer={<Button onClick={() => setDetailKey(null)}>Close</Button>}
        width={760}
        title={detailKey && <span><span style={{ fontFamily: MONO_FONT }}>{detailKey}</span> across {readable.length} venv{readable.length === 1 ? '' : 's'}</span>}
      >
        {detailKey && (
          <VariableValues
            keyName={detailKey}
            secret={secretKey(detailKey)}
            readers={readerMap.get(detailKey) ?? []}
            groups={valueGroups(readable, views, draft, detailKey)}
            letters={fpLetters(detailKey)}
            revealed={revealed}
            pending={ref => draft[ref]?.[detailKey] !== undefined}
            venvLabel={venvLabel}
            writableCount={readable.filter(writable).length}
            onReveal={refs => reveal(refs, detailKey)}
            onUseEverywhere={v => setEverywhere(detailKey, v)}
          />
        )}
      </Modal>
    </div>
  )
}

/** One variable across the venvs shown: each value it has, and where. */
function VariableValues({ keyName, secret, readers, groups, letters, revealed, pending, venvLabel, writableCount, onReveal, onUseEverywhere }: {
  keyName: string
  secret: boolean
  readers: EnvFileReader[]
  groups: { token: string | null; refs: string[] }[]
  letters: Map<string, string>
  revealed: Record<string, string>
  pending: (ref: string) => boolean
  venvLabel: (ref: string) => string
  writableCount: number
  onReveal: (refs: string[]) => void
  onUseEverywhere: (value: string) => void
}) {
  const total = groups.reduce((n, g) => n + g.refs.length, 0)
  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        {readers.length ? <ReaderTags readers={readers} /> : <Text type="secondary">No runnable in these venvs declares it.</Text>}
      </div>
      {groups.map(g => {
        const t = g.token
        const rv = t?.startsWith('fp:') ? revealed[`${g.refs[0]}|${keyName}`] : undefined
        const value = t?.startsWith('v:') ? t.slice(2) : rv
        const anyPending = g.refs.some(pending)
        let shown: React.ReactNode
        if (t === null) shown = <Text type="secondary">not set</Text>
        else if (t.startsWith('fp:') && rv === undefined) {
          const letter = letters.get(t.slice(3))
          shown = (
            <Space size={4}>
              <Text type="secondary">•••••• hidden{letters.size > 1 && letter ? ` value ${letter}` : ''}</Text>
              <Button size="small" type="link" icon={<EyeOutlined />} onClick={() => onReveal(g.refs)} aria-label={`Reveal ${keyName} value ${letter ?? ''}`.trim()}>Reveal</Button>
            </Space>
          )
        } else if (secret && t.startsWith('v:')) shown = <Text>{'•'.repeat(Math.min(Math.max((value ?? '').length, 6), 12))}</Text>
        else shown = <span style={{ fontFamily: MONO_FONT, fontSize: 13, ...WRAP }}>{value === '' ? <Text type="secondary">""</Text> : value}</span>
        return (
          <div key={t ?? '∅'} style={{ borderTop: '1px solid var(--rs-divider)', padding: '10px 0' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
              <div style={{ flex: 1, minWidth: 0 }}>{shown}</div>
              {anyPending && <Tag color="gold">unsaved</Tag>}
              <Tag>{g.refs.length} of {total} venv{total === 1 ? '' : 's'}</Tag>
              {value !== undefined && (
                <Tooltip title={`Set this value in every venv shown (${writableCount})`}>
                  <Button size="small" disabled={!writableCount || g.refs.length === total} onClick={() => onUseEverywhere(value)}>Use everywhere</Button>
                </Tooltip>
              )}
            </div>
            <div style={{ marginTop: 6, maxHeight: 96, overflowY: 'auto', display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {g.refs.map(r => <Tag key={r} style={{ margin: 0, fontSize: 11 }}>{venvLabel(r)}</Tag>)}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function SetEverywhere({ keyName, secret, count, onSet }: { keyName: string; secret: boolean; count: number; onSet: (v: string) => void }) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  const apply = () => { onSet(value); setOpen(false); setValue('') }
  const inputProps = {
    size: 'small' as const,
    autoFocus: true,
    value,
    placeholder: 'value',
    'aria-label': `Value of ${keyName} for every venv`,
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setValue(e.target.value),
    onPressEnter: apply,
    style: { width: 260, fontFamily: MONO_FONT, fontSize: 12 },
  }
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      title={<span>Set <Text code>{keyName}</Text> in {count} venv{count === 1 ? '' : 's'}</span>}
      content={
        <Space>
          {secret ? <Input.Password {...inputProps} autoComplete="new-password" /> : <Input {...inputProps} />}
          <Button size="small" type="primary" onClick={apply}>Set</Button>
        </Space>
      }
    >
      <Tooltip title="Set one value in every venv shown">
        <Button size="small" type="text" icon={<EditOutlined />} aria-label={`Set ${keyName} everywhere`} disabled={!count} />
      </Tooltip>
    </Popover>
  )
}
