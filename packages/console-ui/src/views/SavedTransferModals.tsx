import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Input, Select, Radio, InputNumber, Button, Typography, Tag, Alert, message } from 'antd'
import { PlusOutlined, DeleteOutlined, CalendarOutlined } from '@ant-design/icons'
import type { Credential, Host, SavedTransfer, SavedTransferPreview, TransferEnd, TransferProgress } from '../bridge'
import { bridge } from '../bridge'
import { ParamRow, paramModalWidth } from './RunbookRunModal'
import { humanBytes } from './FilterPicker'
import { MONO_FONT } from '../ThemeContext'
import {
  type ParamRowState, detectDates, templatizeDates, widenToRotations, missingParams,
  rowsFromParams, paramsFromRows, slugify, defaultValues,
} from './savedTransfers'

const { Text } = Typography

type SourceKind = 'host' | 'group' | 'adhoc'

const label = (t: string) => <Text style={{ fontSize: 13 }}>{t}</Text>
const hint = (t: React.ReactNode) => <Text type="secondary" style={{ fontSize: 12 }}>{t}</Text>

// Build or edit a saved transfer. `initial` is either an existing entry (edit)
// or the Transfer dialog's current pick turned into a draft (save as).
export function SaveTransferModal({ initial, replaceId, hosts, workspacePath, onClose }: {
  initial: SavedTransfer
  replaceId?: string
  hosts: Host[]
  workspacePath?: string
  onClose: (savedId?: string) => void
}) {
  const remote = hosts.filter(h => h.name !== 'local')
  const groupNames = useMemo(() => [...new Set(remote.map(h => h.group).filter((g): g is string => !!g))].sort(), [remote])
  const [creds, setCreds] = useState<Credential[]>([])
  useEffect(() => { bridge.list_credentials().then(setCreds).catch(() => {}) }, [])

  const [title, setTitle] = useState(initial.title ?? '')
  const [id, setId] = useState(initial.id ?? '')
  const idTouched = useRef(!!initial.id)
  const [description, setDescription] = useState(initial.description ?? '')
  const [kind, setKind] = useState<SourceKind>(initial.group ? 'group' : initial.adhoc ? 'adhoc' : 'host')
  const [host, setHost] = useState(initial.host ?? '')
  const [group, setGroup] = useState(initial.group ?? (remote.find(h => h.name === initial.host)?.group ?? ''))
  const [adhoc, setAdhoc] = useState(initial.adhoc ?? '')
  const [jump, setJump] = useState(initial.jump ?? '')
  const [user, setUser] = useState(initial.user ?? '')
  const [credentialId, setCredentialId] = useState(initial.credential_id ?? '')
  const [pathsText, setPathsText] = useState((initial.paths ?? []).join('\n'))
  const [rows, setRows] = useState<ParamRowState[]>(() => rowsFromParams(initial.paramsRaw))
  const [latest, setLatest] = useState(initial.latest ?? 0)
  const [maxFiles, setMaxFiles] = useState<number | null>(initial.max_files ?? null)
  const [destMode, setDestMode] = useState<'workspace' | 'folder'>(initial.dest_dir ? 'folder' : 'workspace')
  const [destDir, setDestDir] = useState(initial.dest_dir ?? '')
  const [subdir, setSubdir] = useState(initial.subdir ?? '')
  const [filterArg, setFilterArg] = useState(initial.filter_arg ?? '')
  const [autonomy, setAutonomy] = useState(initial.autonomy ?? 'confirm')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const paths = pathsText.split('\n').map(p => p.trim()).filter(Boolean)
  const dates = detectDates(paths)
  const missing = missingParams([...paths, subdir, destMode === 'folder' ? destDir : '', filterArg], rows)
  const canWiden = paths.some(p => widenToRotations(p) !== p)

  const useDateParam = () => {
    const name = rows.some(r => r.name === 'day') ? 'date' : 'day'
    setPathsText(templatizeDates(paths, name).join('\n'))
    setSubdir(s => templatizeDates([s], name)[0])
    setRows(r => [...r, { name, default: 'yesterday', description: 'today, yesterday, -3d or a date (YYYY-MM-DD)' }])
  }

  const setRow = (i: number, patch: Partial<ParamRowState>) => setRows(r => r.map((x, j) => (j === i ? { ...x, ...patch } : x)))

  const save = async () => {
    setError('')
    const row: SavedTransfer = {
      id: id.trim(),
      title: title.trim() || undefined,
      description: description.trim() || undefined,
      paths,
      paramsRaw: paramsFromRows(rows),
      latest: latest || undefined,
      max_files: maxFiles || undefined,
      filter: initial.filter || undefined,
      filter_arg: initial.filter ? (filterArg || undefined) : undefined,
      dest_dir: destMode === 'folder' ? destDir.trim() || undefined : undefined,
      subdir: subdir.trim() || undefined,
      autonomy,
      ...(kind === 'host' ? { host } : kind === 'group' ? { group } : {
        adhoc: adhoc.trim(), jump: jump || undefined, user: user.trim() || undefined, credential_id: credentialId || undefined,
      }),
    }
    setSaving(true)
    try {
      const res = await bridge.save_saved_transfer(row, replaceId)
      if (!res.ok) { setError(res.error || 'Could not save'); return }
      message.success(`Saved transfer '${res.id}'`)
      onClose(res.id)
    } finally {
      setSaving(false)
    }
  }

  const hostOptions = remote.map(h => ({ label: h.name, value: h.name }))
  return (
    <Modal
      open
      width="min(760px, 96vw)"
      title={replaceId ? `Edit saved transfer: ${replaceId}` : 'Save transfer as a scenario'}
      onCancel={() => onClose()}
      onOk={save}
      okText="Save"
      okButtonProps={{ loading: saving, disabled: !id.trim() || paths.length === 0 }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <div style={{ flex: 2 }}>
            {label('Title')}
            <Input size="small" value={title} placeholder="App logs for a day"
              onChange={e => { setTitle(e.target.value); if (!idTouched.current) setId(slugify(e.target.value)) }} />
          </div>
          <div style={{ flex: 1 }}>
            {label('Id')}
            <Input size="small" value={id} placeholder="app-logs" style={{ fontFamily: MONO_FONT }}
              onChange={e => { idTouched.current = true; setId(e.target.value) }} />
          </div>
        </div>
        <div>
          {label('Description')} {hint('(optional — the agent reads it)')}
          <Input size="small" value={description} onChange={e => setDescription(e.target.value)} />
        </div>

        <div>
          {label('Source')}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
            <Select size="small" value={kind} onChange={setKind} style={{ width: 150 }} options={[
              { label: 'Fleet host', value: 'host' },
              { label: 'Every host in a group', value: 'group', disabled: groupNames.length === 0 },
              { label: 'Ad-hoc host', value: 'adhoc' },
            ]} />
            {kind === 'host' && <Select size="small" value={host || undefined} onChange={setHost} placeholder="host" style={{ minWidth: 170 }} options={hostOptions} showSearch />}
            {kind === 'group' && <Select size="small" value={group || undefined} onChange={setGroup} placeholder="group" style={{ minWidth: 170 }} options={groupNames.map(g => ({ label: g, value: g }))} />}
            {kind === 'adhoc' && (
              <>
                <Input size="small" value={adhoc} onChange={e => setAdhoc(e.target.value)} placeholder="target hostname" style={{ width: 170, fontFamily: MONO_FONT }} />
                <Select size="small" value={jump || undefined} onChange={v => setJump(v ?? '')} allowClear placeholder="jump host (optional)" style={{ minWidth: 150 }} options={hostOptions} />
                <Input size="small" value={user} onChange={e => setUser(e.target.value)} placeholder="user (optional)" style={{ width: 120 }} />
                {creds.length > 0 && (
                  <Select size="small" value={credentialId || undefined} onChange={v => setCredentialId(v ?? '')} allowClear placeholder="saved credential" style={{ width: 170 }}
                    options={creds.map(c => ({ label: c.label, value: c.id }))} />
                )}
              </>
            )}
          </div>
          {kind === 'group' && hint('Each host’s files land in a folder named after the host.')}
          {kind === 'adhoc' && hint('Key/agent auth, or a saved credential — a typed password is never saved.')}
        </div>

        <div>
          {label('Source paths')} {hint('(one per line)')}
          <Input.TextArea value={pathsText} onChange={e => setPathsText(e.target.value)} autoSize={{ minRows: 2, maxRows: 6 }}
            style={{ fontFamily: MONO_FONT, fontSize: 12, marginTop: 4 }} placeholder="/var/log/app/app-{{day:%Y-%m-%d}}*.log" />
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
            {dates.length > 0 && (
              <Button size="small" icon={<CalendarOutlined />} onClick={useDateParam}>
                Turn {dates[0]}{dates.length > 1 ? ` (+${dates.length - 1})` : ''} into a date parameter
              </Button>
            )}
            {canWiden && (
              <Button size="small" onClick={() => setPathsText(paths.map(widenToRotations).join('\n'))}>Also match rotated files (*)</Button>
            )}
          </div>
          <div style={{ marginTop: 4 }}>
            {hint(<>Wildcards <Text code>*</Text> <Text code>?</Text> <Text code>[..]</Text> match on the source host. Placeholders: <Text code>{'{{param}}'}</Text>, <Text code>{'{{day:%Y%m%d}}'}</Text> (a date param in any strftime format), and built-ins <Text code>{'{{today}}'}</Text> <Text code>{'{{yesterday}}'}</Text> <Text code>{'{{host}}'}</Text>.</>)}
          </div>
        </div>

        <div>
          {label('Parameters')} {hint('(filled in when it runs — no default means required)')}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
            {rows.map((r, i) => (
              <div key={i} style={{ display: 'flex', gap: 6 }}>
                <Input size="small" value={r.name} onChange={e => setRow(i, { name: e.target.value })} placeholder="name" style={{ width: 120, fontFamily: MONO_FONT }} />
                <Input size="small" value={r.default} onChange={e => setRow(i, { default: e.target.value })} placeholder="default (blank = required)" style={{ width: 190 }} />
                <Input size="small" value={r.description} onChange={e => setRow(i, { description: e.target.value })} placeholder="description" style={{ flex: 1 }} />
                <Button size="small" type="text" icon={<DeleteOutlined />} onClick={() => setRows(rs => rs.filter((_, j) => j !== i))} aria-label="Remove parameter" />
              </div>
            ))}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <Button size="small" icon={<PlusOutlined />} onClick={() => setRows(r => [...r, { name: '', default: '', description: '' }])}>Add parameter</Button>
              {missing.map(n => (
                <Button key={n} size="small" type="dashed" onClick={() => setRows(r => [...r, { name: n, default: '', description: '' }])}>Add {n}</Button>
              ))}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
          <div>
            {label('Which matches')}
            <div style={{ marginTop: 4 }}>
              <Radio.Group size="small" value={latest ? 'latest' : 'all'} onChange={e => setLatest(e.target.value === 'latest' ? (latest || 1) : 0)}>
                <Radio value="all">Every match</Radio>
                <Radio value="latest">Newest</Radio>
              </Radio.Group>
              {latest > 0 && <InputNumber size="small" min={1} max={100} value={latest} onChange={v => setLatest(v || 1)} style={{ width: 64 }} />}
              {latest > 0 && hint(' per path')}
            </div>
          </div>
          <div>
            {label('Max files')}
            <div style={{ marginTop: 4 }}>
              <InputNumber size="small" min={1} max={1000} value={maxFiles} placeholder="50" onChange={v => setMaxFiles(v)} style={{ width: 80 }} />
              {hint(' per host — more matches is an error')}
            </div>
          </div>
        </div>

        <div>
          {label('Destination')}
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 4 }}>
            <Radio.Group size="small" value={destMode} onChange={e => setDestMode(e.target.value)}>
              <Radio value="workspace">This chat’s workspace</Radio>
              <Radio value="folder">A folder</Radio>
            </Radio.Group>
            {destMode === 'folder' && (
              <Input size="small" value={destDir} onChange={e => setDestDir(e.target.value)} placeholder="C:\\cases or /home/me/cases" style={{ width: 240, fontFamily: MONO_FONT }} />
            )}
            <Input size="small" value={subdir} onChange={e => setSubdir(e.target.value)} addonBefore="subfolder" placeholder="optional, e.g. logs" style={{ width: 260, fontFamily: MONO_FONT }} />
          </div>
          {destMode === 'workspace' && workspacePath && hint('Files land where the agent can read them.')}
        </div>

        {initial.filter && (
          <div>
            {label(`Filter: ${initial.filter}`)}
            <Input size="small" value={filterArg} onChange={e => setFilterArg(e.target.value)} addonBefore="value" placeholder="{{txn}}" style={{ fontFamily: MONO_FONT, marginTop: 4 }} />
            {hint('Leave blank if the filter takes no value; placeholders work here too.')}
          </div>
        )}

        <div>
          {label('When the agent runs it')}
          <div style={{ marginTop: 4 }}>
            <Select size="small" value={autonomy} onChange={setAutonomy} style={{ width: 260 }} options={[
              { label: 'Ask me first (confirm)', value: 'confirm' },
              { label: 'Just run it (autonomous)', value: 'autonomous' },
              { label: 'Never — people only (manual)', value: 'manual' },
            ]} />
          </div>
          {hint('Previewing (listing the matches) never asks.')}
        </div>

        {error && <Alert type="error" showIcon message={error} />}
      </div>
    </Modal>
  )
}

type Prog = { file: string; index: number; count: number; transferred: number; total: number; host?: string }

// Run a saved transfer: fill its params, optionally preview the matches, then
// pull them, with progress and the outcome shown in place.
export function SavedTransferRunModal({ entry, hostNames, onClose }: {
  entry: SavedTransfer
  hostNames: string[]
  onClose: () => void
}) {
  const params = entry.params ?? []
  const [values, setValues] = useState<Record<string, unknown>>(() => defaultValues(entry))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [preview, setPreview] = useState<SavedTransferPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  const [dest, setDest] = useState('')
  const [prog, setProg] = useState<Prog | null>(null)
  const [result, setResult] = useState<TransferEnd | null>(null)
  const runRef = useRef<string | null>(null)

  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferProgress
      if (d.id !== runRef.current) return
      if (d.error) message.error(`${d.host ? `${d.host} · ` : ''}${d.file}: ${d.error}`)
      else setProg(d)
    }
    const onEnd = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferEnd
      if (d.id !== runRef.current) return
      setResult(d)
      setRunId(null)
      if (d.ok) message.success(`Fetched ${d.files} file${d.files === 1 ? '' : 's'}`)
    }
    window.addEventListener('runspec:transfer_progress', onProgress)
    window.addEventListener('runspec:transfer_end', onEnd)
    return () => {
      window.removeEventListener('runspec:transfer_progress', onProgress)
      window.removeEventListener('runspec:transfer_end', onEnd)
    }
  }, [])

  const change = (name: string, v: unknown) => {
    setValues(prev => ({ ...prev, [name]: v }))
    setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
    setPreview(null)
  }

  const checkRequired = () => {
    const errs: Record<string, string> = {}
    for (const a of params) {
      const v = values[a.name]
      if (a.required && (v === undefined || v === '')) errs[a.name] = 'Required'
    }
    setErrors(errs)
    return Object.keys(errs).length === 0
  }

  const doPreview = async () => {
    if (!checkRequired()) return
    setPreviewing(true)
    try { setPreview(await bridge.preview_saved_transfer(entry.id, values)) }
    catch (e) { setPreview({ error: String(e) }) }
    finally { setPreviewing(false) }
  }

  const run = async () => {
    if (!checkRequired()) return
    setResult(null)
    setProg(null)
    const res = await bridge.run_saved_transfer(entry.id, values)
    if (res.error || !res.id) { message.error(res.error || 'Could not start'); return }
    runRef.current = res.id
    setRunId(res.id)
    setDest(res.dest ?? '')
  }

  const running = !!runId
  return (
    <Modal
      open
      onCancel={onClose}
      width={paramModalWidth(params.length)}
      title={`Saved transfer: ${entry.title || entry.id}`}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={onClose}>Close</Button>
          {running
            ? <Button danger onClick={() => runId && bridge.cancel_transfer(runId)}>Cancel</Button>
            : <>
                <Button onClick={doPreview} loading={previewing}>Preview</Button>
                <Button type="primary" onClick={run} disabled={!!entry.error}>Run</Button>
              </>}
        </div>
      }
    >
      {entry.description && <Text type="secondary" style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>{entry.description}</Text>}
      {entry.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`This saved transfer is misconfigured: ${entry.error}`} />}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 12 }}>
        <div>
          <Text type="secondary" style={{ fontSize: 13 }}>from </Text><Tag style={{ margin: 0 }}>{entry.source}</Tag>
          <Text type="secondary" style={{ fontSize: 13 }}> into </Text><Tag style={{ margin: 0 }}>{entry.dest}</Tag>
          {entry.filter && <><Text type="secondary" style={{ fontSize: 13 }}> through filter </Text><Tag style={{ margin: 0 }}>{entry.filter}</Tag></>}
        </div>
        {entry.paths.map(p => <Text key={p} code style={{ fontSize: 12, fontFamily: MONO_FONT }}>{p}</Text>)}
      </div>
      {/* Every param shown — a scenario's few params (usually the day) are the
          whole point of running it, so none fold away under "Default arguments". */}
      {params.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 14, marginBottom: 12 }}>
          {params.map(a => (
            <ParamRow key={a.name} arg={a} hostNames={hostNames} value={values[a.name]} error={errors[a.name]} onChange={v => change(a.name, v)} />
          ))}
        </div>
      )}
      {preview && <PreviewList preview={preview} />}
      {(running || result) && (
        <div style={{ marginTop: 10 }}>
          {running && prog && (
            <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>
              {prog.host ? `${prog.host} · ` : ''}{prog.file} ({prog.index + 1}/{prog.count})
              {prog.total > 0 ? ` · ${Math.round((prog.transferred / prog.total) * 100)}%` : prog.transferred ? ` · ${humanBytes(prog.transferred)}` : ''}
            </Text>
          )}
          {running && !prog && <Text type="secondary" style={{ fontSize: 12 }}>Finding matches…</Text>}
          {result && (
            <Text type={result.ok ? 'success' : 'danger'} style={{ fontSize: 13 }}>
              {result.ok ? `Done — ${result.files} file${result.files === 1 ? '' : 's'} in ${dest}` : `Failed: ${result.error}`}
            </Text>
          )}
        </div>
      )}
    </Modal>
  )
}

function PreviewList({ preview }: { preview: SavedTransferPreview }) {
  if (preview.error) return <Alert type="error" showIcon message={preview.error} />
  const sources = preview.sources ?? []
  return (
    <div style={{ border: '1px solid var(--rs-border, rgba(128,128,128,.3))', borderRadius: 6, padding: 8, maxHeight: 240, overflow: 'auto' }}>
      <Text style={{ fontSize: 12 }}>{preview.total} file{preview.total === 1 ? '' : 's'} would land in </Text>
      <Text code style={{ fontSize: 12 }}>{preview.dest}</Text>
      {sources.map(s => (
        <div key={s.host} style={{ marginTop: 6 }}>
          {sources.length > 1 && <Tag style={{ fontSize: 11 }}>{s.host}</Tag>}
          {s.error && <div><Text type="danger" style={{ fontSize: 12 }}>{s.error}</Text></div>}
          {s.files.map(f => (
            <div key={f.path} style={{ fontSize: 12, fontFamily: MONO_FONT, display: 'flex', gap: 8 }}>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.path}>{f.path}</span>
              <Text type="secondary" style={{ fontSize: 12 }}>{humanBytes(f.size)}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>{new Date(f.mtime * 1000).toLocaleString()}</Text>
            </div>
          ))}
          {s.unmatched.map(u => <div key={u}><Text type="warning" style={{ fontSize: 12 }}>No match for <Text code>{u}</Text></Text></div>)}
        </div>
      ))}
    </div>
  )
}
