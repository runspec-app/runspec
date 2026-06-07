import { useState, useRef, useCallback, useEffect } from 'react'
import { AutoComplete, Button, Modal, List, Tag, Typography, Tooltip, Spin, Segmented, Checkbox } from 'antd'
import { FolderOpenOutlined, FolderFilled, FileOutlined, ArrowUpOutlined, HomeOutlined } from '@ant-design/icons'
import { bridge, type DirEntry, type DirListing } from '../bridge'

const { Text } = Typography

// Pick the path separator a host uses from an absolute path it returned. Remote
// hosts are POSIX ('/'); a local Windows host returns backslash paths.
function sepOf(absPath: string): string {
  return absPath.includes('\\') && !absPath.includes('/') ? '\\' : '/'
}

function joinPath(dir: string, name: string, sep: string): string {
  if (!dir) return name
  return dir.endsWith(sep) ? dir + name : dir + sep + name
}

// Split a half-typed path into the directory to list and the basename prefix to
// filter on. "/var/lo" → ["/var", "lo"]; "/var/" → ["/var", ""].
function splitTyped(text: string, sep: string): { dir: string; prefix: string } {
  if (!text) return { dir: '', prefix: '' }
  const i = text.lastIndexOf(sep)
  if (i < 0) return { dir: '', prefix: text }
  return { dir: text.slice(0, i + 1), prefix: text.slice(i + 1) }
}

interface PathInputProps {
  host: string
  value: string
  placeholder?: string
  dirsOnly?: boolean
  error?: boolean
  onChange: (v: string) => void
  runnable?: string   // runnable name — needed to resolve run_as for the service-account view
  runAs?: string      // the runnable's service account on this host, when configured
  group?: string      // venv, to disambiguate a same-named runnable across venvs
}

export function PathInput({ host, value, placeholder, dirsOnly, error, onChange, runnable, runAs, group }: PathInputProps) {
  const [options, setOptions] = useState<{ value: string; label: React.ReactNode }[]>([])
  const [browseOpen, setBrowseOpen] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reqRef = useRef(0)

  const inputStyle = { fontFamily: 'monospace', fontSize: 12 }

  // ── inline autocomplete ────────────────────────────────────────────────────
  const fetchCompletions = useCallback(async (text: string) => {
    const seq = ++reqRef.current
    const probe = splitTyped(text, '/').dir // best-effort; refine with real sep below
    try {
      const listing = await bridge.list_dir(host, probe, undefined, dirsOnly)
      if (seq !== reqRef.current) return // a newer keystroke won
      if (listing.error) { setOptions([]); return }
      const sep = sepOf(listing.cwd || '/')
      const { prefix } = splitTyped(text, sep)
      const low = prefix.toLowerCase()
      const opts = listing.entries
        .filter(e => e.name.toLowerCase().startsWith(low))
        .map(e => {
          const full = joinPath(listing.cwd, e.name, sep)
          return {
            value: e.type === 'directory' ? full + sep : full,
            label: (
              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>
                {e.type === 'directory'
                  ? <FolderFilled style={{ color: '#e8b339', marginRight: 6 }} />
                  : <FileOutlined style={{ color: '#888', marginRight: 6 }} />}
                {e.name}
              </span>
            ),
          }
        })
      setOptions(opts)
    } catch {
      if (seq === reqRef.current) setOptions([])
    }
  }, [host, dirsOnly])

  const handleSearch = (text: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchCompletions(text), 250)
  }

  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current) }, [])

  return (
    <>
      <div style={{ display: 'flex', gap: 6 }}>
        <AutoComplete
          size="small"
          value={value ?? ''}
          options={options}
          onSearch={handleSearch}
          onChange={v => onChange(v)}
          style={{ width: '100%' }}
        >
          <input
            className="ant-input ant-input-sm"
            placeholder={placeholder ?? (dirsOnly ? '/path/to/dir' : '/path/to/file')}
            style={{ ...inputStyle, width: '100%', borderColor: error ? '#ff4d4f' : undefined }}
          />
        </AutoComplete>
        <Tooltip title={`Browse ${host}`}>
          <Button size="small" icon={<FolderOpenOutlined />} onClick={() => setBrowseOpen(true)} />
        </Tooltip>
      </div>
      {browseOpen && (
        <BrowseModal
          host={host}
          startPath={value}
          dirsOnly={dirsOnly}
          runnable={runnable}
          runAs={runAs}
          group={group}
          onPick={p => { onChange(p); setBrowseOpen(false) }}
          onClose={() => setBrowseOpen(false)}
        />
      )}
    </>
  )
}

// ── browse modal ──────────────────────────────────────────────────────────────

export function BrowseModal({ host, startPath, dirsOnly, runnable, runAs, group, multiSelect, onPick, onPickMany, onClose }: {
  host: string
  startPath?: string
  dirsOnly?: boolean
  runnable?: string
  runAs?: string
  group?: string
  multiSelect?: boolean
  onPick?: (path: string) => void
  onPickMany?: (paths: string[]) => void
  onClose: () => void
}) {
  const [listing, setListing] = useState<DirListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [asSvc, setAsSvc] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // The service-account view only applies to a remote host with a run_as set.
  const canSvc = !!runAs && host !== 'local'

  const load = useCallback(async (path: string, svc: boolean) => {
    setLoading(true)
    setErr(null)
    try {
      const res = await bridge.list_dir(host, path, group, dirsOnly, runnable, svc)
      if (res.error) { setErr(res.error); setListing(null) }
      else setListing(res)
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoading(false)
    }
  }, [host, dirsOnly, group, runnable])

  useEffect(() => {
    // Start at the directory of the current value, else the user's home.
    const sep = startPath && startPath.includes('\\') && !startPath.includes('/') ? '\\' : '/'
    const start = startPath ? startPath.slice(0, startPath.lastIndexOf(sep) + 1) : ''
    load(start, false)
  }, [])

  const switchIdentity = (svc: boolean) => {
    setAsSvc(svc)
    load(listing?.cwd ?? '', svc)   // reload the current directory under the new identity
  }

  const cwd = listing?.cwd ?? ''
  const sep = sepOf(cwd || '/')

  const crumbs = (() => {
    if (!cwd) return [] as { label: string; path: string }[]
    if (cwd === '/') return [{ label: '/', path: '/' }]
    const parts = cwd.split(sep).filter(Boolean)
    let acc = sep === '/' ? '' : ''
    const out: { label: string; path: string }[] = sep === '/' ? [{ label: '/', path: '/' }] : []
    for (const p of parts) {
      acc = acc + (acc.endsWith(sep) || acc === '' ? '' : sep) + p
      const full = sep === '/' ? '/' + parts.slice(0, parts.indexOf(p) + 1).join('/') : acc
      out.push({ label: p, path: full })
    }
    return out
  })()

  const onEntry = (e: DirEntry) => {
    const full = joinPath(cwd, e.name, sep)
    if (e.type === 'directory') { load(full, asSvc); return }
    if (multiSelect) {
      setSelected(prev => {
        const next = new Set(prev)
        if (next.has(full)) next.delete(full)
        else next.add(full)
        return next
      })
      return
    }
    if (!dirsOnly) onPick?.(full)
  }

  return (
    <Modal
      open
      onCancel={onClose}
      width={560}
      title={<Text style={{ fontFamily: 'monospace', fontSize: 13 }}>{multiSelect ? `Select files · ${host}` : `Browse ${host}`}</Text>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
            {multiSelect ? `${selected.size} selected` : cwd}
          </Text>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={onClose}>Cancel</Button>
            {multiSelect ? (
              <Button type="primary" disabled={selected.size === 0} onClick={() => { onPickMany?.([...selected]); onClose() }}>
                Add {selected.size} file{selected.size === 1 ? '' : 's'}
              </Button>
            ) : dirsOnly ? (
              <Button type="primary" disabled={!cwd} onClick={() => onPick?.(cwd)}>
                Use this folder
              </Button>
            ) : null}
          </div>
        </div>
      }
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10, flexWrap: 'wrap' }}>
        <Tooltip title="Home">
          <Button size="small" icon={<HomeOutlined />} onClick={() => load('', asSvc)} />
        </Tooltip>
        <Tooltip title="Up">
          <Button
            size="small"
            icon={<ArrowUpOutlined />}
            disabled={!listing?.parent}
            onClick={() => listing?.parent != null && load(listing.parent, asSvc)}
          />
        </Tooltip>
        <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
          {crumbs.map((c, i) => (
            <span key={c.path} style={{ display: 'flex', alignItems: 'center' }}>
              {i > 0 && <span style={{ color: '#888', margin: '0 2px' }}>{sep}</span>}
              <Text
                style={{ fontFamily: 'monospace', fontSize: 12, cursor: 'pointer', color: '#4fc1ff' }}
                onClick={() => load(c.path, asSvc)}
              >
                {c.label}
              </Text>
            </span>
          ))}
        </div>
        {canSvc && (
          <div style={{ marginLeft: 'auto' }}>
            <Tooltip title="Browse as the runnable's service account (sudo)">
              <Segmented
                size="small"
                value={asSvc ? 'svc' : 'you'}
                onChange={v => switchIdentity(v === 'svc')}
                options={[
                  { label: 'You', value: 'you' },
                  { label: runAs as string, value: 'svc' },
                ]}
              />
            </Tooltip>
          </div>
        )}
      </div>

      {err ? (
        <div style={{ padding: 16 }}><Text type="danger" style={{ fontSize: 12 }}>{err}</Text></div>
      ) : loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}><Spin size="small" /></div>
      ) : (
        <div style={{ maxHeight: 360, overflowY: 'auto' }}>
          <List
            size="small"
            dataSource={listing?.entries ?? []}
            locale={{ emptyText: 'Empty directory' }}
            renderItem={(e) => {
              const full = joinPath(cwd, e.name, sep)
              const checkable = multiSelect && e.type === 'file'
              return (
                <List.Item
                  style={{ cursor: e.type === 'directory' || multiSelect || !dirsOnly ? 'pointer' : 'default', padding: '4px 8px' }}
                  onClick={() => onEntry(e)}
                >
                  <span style={{ fontFamily: 'monospace', fontSize: 12, display: 'inline-flex', alignItems: 'center', opacity: dirsOnly && e.type === 'file' ? 0.45 : 1 }}>
                    {checkable && (
                      <Checkbox
                        checked={selected.has(full)}
                        style={{ marginRight: 8 }}
                        onClick={ev => ev.stopPropagation()}
                        onChange={() => onEntry(e)}
                      />
                    )}
                    {e.type === 'directory'
                      ? <FolderFilled style={{ color: '#e8b339', marginRight: 8 }} />
                      : <FileOutlined style={{ color: '#888', marginRight: 8 }} />}
                    {e.name}
                  </span>
                </List.Item>
              )
            }}
          />
          {listing?.truncated && (
            <Tag color="orange" style={{ fontSize: 10, marginTop: 6 }}>listing truncated</Tag>
          )}
        </div>
      )}
    </Modal>
  )
}
