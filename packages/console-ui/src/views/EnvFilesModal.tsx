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
 *  - **Across venvs** — set or remove one key in many venvs at once (pick by
 *    host, group or profile), previewed first; no other key is touched.
 *
 * Scope is the local venvs plus the connected remote hosts it is given.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, AutoComplete, Button, Empty, Input, Modal, Popconfirm, Segmented, Select, Space, Spin, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import {
  CodeOutlined, DeleteOutlined, EyeOutlined, LockOutlined, PlusOutlined, ReloadOutlined, SaveOutlined, UndoOutlined,
} from '@ant-design/icons'
import {
  bridge, type EnvFileBulkRow, type EnvFileReader, type EnvFileRef, type EnvFileTarget, type EnvFileView, type Host,
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
      width={1000}
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
        : <AcrossVenvs targets={targets} options={options} />}
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

// ── Across venvs ────────────────────────────────────────────────────────────

const STATE_TAG: Record<string, { color: string; label: string }> = {
  absent: { color: 'default', label: 'not set' },
  same: { color: 'green', label: 'already this value' },
  differs: { color: 'gold', label: 'different value' },
  present: { color: 'gold', label: 'set' },
}

function AcrossVenvs({ targets, options }: {
  targets: EnvFileTarget[]
  options: { label: string; options: { value: string; label: string; disabled?: boolean }[] }[]
}) {
  const [key, setKey] = useState('')
  const [action, setAction] = useState<'set' | 'unset'>('set')
  const [value, setValue] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [rows, setRows] = useState<EnvFileBulkRow[] | null>(null)
  const [phase, setPhase] = useState<'' | 'preview' | 'apply'>('')
  const [applied, setApplied] = useState(false)
  const [busy, setBusy] = useState(false)

  const supported = targets.filter(t => t.supported)
  const groups = [...new Set(supported.filter(t => !t.local && t.group).map(t => t.group))].sort()
  const profiles = [...new Set(supported.flatMap(t => t.profiles))].sort()
  const add = (refs: string[]) => setPicked(prev => [...new Set([...prev, ...refs])])

  const payload = () => ({
    key: key.trim(),
    action,
    ...(action === 'set' ? { value } : {}),
    targets: supported.filter(t => picked.includes(refOf(t))).map(t => ({ host: t.host, runspec_path: t.runspec_path })),
  })
  const ready = validKey(key.trim()) && picked.length > 0

  // Any edit to the change or the venvs invalidates a preview.
  useEffect(() => { setRows(null); setApplied(false) }, [key, action, value, picked])

  const run = async (which: 'preview' | 'apply') => {
    setBusy(true)
    setPhase(which)
    try {
      if (which === 'preview') {
        setRows((await bridge.env_file_bulk_preview(payload())).rows)
      } else {
        const res = await bridge.env_file_bulk_apply(payload())
        setRows(res.rows)
        setApplied(true)
        if (res.failed) message.warning(`${res.changed} changed, ${res.failed} failed`)
        else message.success(`${res.changed} file${res.changed === 1 ? '' : 's'} changed (${res.ok - res.changed} already right)`)
      }
    } catch (err) {
      message.error((err as Error)?.message || `${which} failed`)
    } finally {
      setBusy(false)
    }
  }

  const columns = [
    { title: 'Host', dataIndex: 'host', key: 'host', width: 160 },
    { title: 'Venv', dataIndex: 'venv', key: 'venv', width: 120 },
    {
      title: applied ? 'Result' : 'Now',
      key: 'state',
      width: 170,
      render: (_: unknown, r: EnvFileBulkRow) => {
        if (!r.ok) return <Tag color="red">error</Tag>
        if (applied) return r.changed ? <Tag color="green">changed</Tag> : <Tag>no change needed</Tag>
        const s = STATE_TAG[r.state ?? ''] ?? { color: 'default', label: r.state ?? '' }
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: 'Detail',
      key: 'detail',
      render: (_: unknown, r: EnvFileBulkRow) =>
        r.error
          ? <Text type="danger" style={{ fontSize: 12, wordBreak: 'break-word' }}>{r.error}</Text>
          : r.as_user ? <Text type="secondary" style={{ fontSize: 12 }}>as {r.as_user}</Text> : null,
    },
  ]

  return (
    <div>
      <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 8 }}>
        <Space wrap>
          <Segmented size="small" value={action} onChange={v => setAction(v as 'set' | 'unset')} options={[{ label: 'Set', value: 'set' }, { label: 'Remove', value: 'unset' }]} />
          <Input
            size="small" style={{ width: 300, fontFamily: MONO_FONT }} placeholder="VARIABLE_NAME"
            value={key} onChange={e => setKey(e.target.value)} aria-label="Variable name"
            status={key && !validKey(key.trim()) ? 'error' : undefined}
          />
          {action === 'set' && (looksSecret(key)
            ? <Input.Password size="small" style={{ width: 300 }} placeholder="value" value={value} onChange={e => setValue(e.target.value)} aria-label="Value" autoComplete="new-password" />
            : <Input size="small" style={{ width: 300, fontFamily: MONO_FONT }} placeholder="value" value={value} onChange={e => setValue(e.target.value)} aria-label="Value" />)}
        </Space>
        <Select
          mode="multiple" size="small" allowClear style={{ width: '100%' }}
          placeholder="Venvs to change" value={picked} onChange={setPicked} options={options}
          optionFilterProp="label" maxTagCount="responsive" aria-label="Venvs"
        />
        <Space wrap size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>Add:</Text>
          <Button size="small" onClick={() => add(supported.map(refOf))}>All ({supported.length})</Button>
          {groups.length > 0 && (
            <Select size="small" style={{ minWidth: 140 }} placeholder="a group" value={null} aria-label="Add a group"
              options={groups.map(g => ({ value: g, label: g }))}
              onChange={(g: string) => add(supported.filter(t => t.group === g).map(refOf))} />
          )}
          {profiles.length > 0 && (
            <Select size="small" style={{ minWidth: 140 }} placeholder="a profile" value={null} aria-label="Add a profile"
              options={profiles.map(p => ({ value: p, label: p }))}
              onChange={(p: string) => add(supported.filter(t => t.profiles.includes(p)).map(refOf))} />
          )}
          {picked.length > 0 && <Button size="small" type="text" onClick={() => setPicked([])}>Clear</Button>}
        </Space>
        <Space>
          <Button size="small" disabled={!ready} loading={busy && phase === 'preview'} onClick={() => run('preview')}>Preview</Button>
          <Popconfirm
            title={`${action === 'set' ? 'Set' : 'Remove'} ${key.trim()} in ${picked.length} venv${picked.length === 1 ? '' : 's'}?`}
            description={<span style={{ fontSize: 13 }}>Only this variable changes; every other line in each file stays as it is.</span>}
            okText="Apply"
            onConfirm={() => run('apply')}
            disabled={!rows || applied}
          >
            <Button size="small" type="primary" disabled={!rows || applied} loading={busy && phase === 'apply'}>Apply</Button>
          </Popconfirm>
          {!rows && ready && <Text type="secondary" style={{ fontSize: 12 }}>Preview first to see each venv's current state.</Text>}
        </Space>
      </Space>
      {rows && (
        <Table<EnvFileBulkRow>
          size="small"
          rowKey={r => refOf(r)}
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ y: 320 }}
        />
      )}
    </div>
  )
}
