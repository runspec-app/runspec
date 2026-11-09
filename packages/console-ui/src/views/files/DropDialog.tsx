import { useEffect, useMemo, useState } from 'react'
import { Modal, Radio, Input, Select, Checkbox, Typography, Button, Space } from 'antd'
import { FilterOutlined } from '@ant-design/icons'
import type { Filter, Host } from '../../bridge'
import { bridge } from '../../bridge'
import { MONO_FONT } from '../../ThemeContext'
import { FiltersTab, isParameterized } from '../../components/SettingsDrawer'
import {
  baseName, copyButtonLabel, fanoutCandidates, lastFilterFor, planCopy, targetLabel,
  type PaneTarget,
} from './filesModel'

const { Text } = Typography

export interface DropChoice {
  filter: string        // '' = copy the whole file(s)
  filterArg: string
  destName: string
  alsoTo: string[]      // extra hosts for a fan-out upload
  saveAsTransfer: boolean
}

// The dialog a drop opens: where from / where to, copy whole or through one of
// the saved filters (remembered per source folder), a new name for one file,
// and for an upload to a host, the other hosts in its group to copy to as well.
export function DropDialog({ src, dst, paths, srcDir, destDir, hosts, onCancel, onCopy }: {
  src: PaneTarget
  dst: PaneTarget
  paths: string[]
  srcDir: string
  destDir: string
  hosts: Host[]
  onCancel: () => void
  onCopy: (choice: DropChoice) => void
}) {
  const [filters, setFilters] = useState<Filter[]>([])
  const [filter, setFilter] = useState(() => lastFilterFor(src, srcDir))
  const [filterArg, setFilterArg] = useState('')
  const [destName, setDestName] = useState('')
  const [alsoTo, setAlsoTo] = useState<string[]>([])
  const [save, setSave] = useState(false)
  const [manage, setManage] = useState(false)
  const [checks, setChecks] = useState<Record<string, { exists: boolean; error?: string | null }> | null>(null)

  useEffect(() => {
    const load = () => { bridge.get_filters().then(setFilters).catch(() => {}) }
    load()
    window.addEventListener('runspec:filters_changed', load)
    return () => window.removeEventListener('runspec:filters_changed', load)
  }, [])

  // A remembered filter that has since been deleted falls back to "whole file".
  useEffect(() => {
    if (filter && filters.length && !filters.some(f => f.name === filter)) setFilter('')
  }, [filters, filter])

  const candidates = useMemo(() => (src.kind === 'local' && dst.kind === 'host' ? fanoutCandidates(dst.host, hosts) : []), [src, dst, hosts])
  const plan = planCopy(src, dst, alsoTo)

  // Fan-out pre-flight: is the destination folder on every extra host?
  useEffect(() => {
    setChecks(null)
    if (!alsoTo.length || !destDir) return
    let live = true
    const t = setTimeout(() => {
      bridge.check_dir_exists(alsoTo, destDir).then(r => { if (live) setChecks(r) }).catch(() => {})
    }, 300)
    return () => { live = false; clearTimeout(t) }
  }, [alsoTo.join(','), destDir])  // eslint-disable-line react-hooks/exhaustive-deps
  const missing = checks ? alsoTo.filter(h => checks[h]?.exists === false) : []
  const keptAlso = alsoTo.filter(h => !missing.includes(h))

  const selected = filters.find(f => f.name === filter)
  const needsArg = selected ? isParameterized(selected.pipeline) : false
  const runsOn = src.kind === 'local' ? 'this computer' : targetLabel(src)
  const one = paths.length === 1
  const title = `Copy ${one ? baseName(paths[0]) : `${paths.length} files`} to ${targetLabel(dst)}`
  const canCopy = !plan.error && (!needsArg || !!filterArg.trim())

  const copy = () => onCopy({ filter, filterArg: filterArg.trim(), destName: one ? destName.trim() : '', alsoTo: keptAlso, saveAsTransfer: save })

  return (
    <Modal
      open
      title={title}
      width={560}
      onCancel={onCancel}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
          <Checkbox checked={save} onChange={e => setSave(e.target.checked)} disabled={!!plan.error}>Save as a saved transfer</Checkbox>
          <Space>
            <Button onClick={onCancel}>Cancel</Button>
            <Button type="primary" disabled={!canCopy} onClick={copy}>{copyButtonLabel(plan.kind, keptAlso.length + 1)}</Button>
          </Space>
        </div>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '48px 1fr', gap: '4px 10px', fontSize: 13 }}>
          <Text type="secondary">From</Text>
          <div style={{ fontFamily: MONO_FONT, fontSize: 12, overflowWrap: 'anywhere' }}>
            <b>{targetLabel(src)}</b>{' '}
            {one ? paths[0] : <>{srcDir} <Text type="secondary">({paths.map(baseName).join(', ')})</Text></>}
          </div>
          <Text type="secondary">To</Text>
          <div style={{ fontFamily: MONO_FONT, fontSize: 12, overflowWrap: 'anywhere' }}><b>{targetLabel(dst)}</b> {destDir}</div>
        </div>

        {plan.error ? (
          <Text type="warning">{plan.error}</Text>
        ) : (
          <>
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <Text strong style={{ fontSize: 13 }}>Filter</Text>
                <Button size="small" type="link" icon={<FilterOutlined />} onClick={() => setManage(true)}>Manage filters…</Button>
              </div>
              <Radio.Group value={filter} onChange={e => { setFilter(e.target.value); setFilterArg('') }} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <Radio value="">
                  Copy the whole {one ? 'file' : 'files'}
                </Radio>
                {filters.map(f => (
                  <Radio key={f.name} value={f.name}>
                    <span>{f.name}</span>{' '}
                    <Text type="secondary" style={{ fontSize: 12, fontFamily: MONO_FONT }}>{f.pipeline}</Text>
                    {f.description && <div><Text type="secondary" style={{ fontSize: 12 }}>{f.description}</Text></div>}
                  </Radio>
                ))}
              </Radio.Group>
              {needsArg && (
                <Input
                  size="small"
                  value={filterArg}
                  onChange={e => setFilterArg(e.target.value)}
                  placeholder={selected?.param_label || 'value'}
                  aria-label={selected?.param_label || 'Filter value'}
                  style={{ marginTop: 8, width: 280, fontFamily: MONO_FONT }}
                />
              )}
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
                {filter
                  ? `The filter runs on ${runsOn}, so only its output is copied.`
                  : filters.length === 0 ? 'No filters saved yet. Manage filters… adds one.' : 'Pick a filter to copy only the matching lines.'}
              </Text>
            </div>

            {one && (
              <div>
                <Text style={{ fontSize: 13 }}>Save as <Text type="secondary" style={{ fontSize: 12 }}>(optional)</Text></Text>
                <Input
                  size="small"
                  allowClear
                  value={destName}
                  onChange={e => setDestName(e.target.value)}
                  placeholder={`keep the name (${baseName(paths[0])})`}
                  aria-label="Save as name"
                  style={{ marginTop: 4, fontFamily: MONO_FONT }}
                />
              </div>
            )}

            {candidates.length > 0 && (
              <div>
                <Text style={{ fontSize: 13 }}>Also copy to <Text type="secondary" style={{ fontSize: 12 }}>(same folder, other hosts in the group)</Text></Text>
                <Select
                  size="small"
                  mode="multiple"
                  value={alsoTo}
                  onChange={setAlsoTo}
                  placeholder="only this host"
                  aria-label="Also copy to"
                  style={{ width: '100%', marginTop: 4 }}
                  maxTagCount="responsive"
                  options={candidates.map(h => ({ label: h, value: h }))}
                />
                {missing.length > 0 && (
                  <Text type="warning" style={{ fontSize: 12 }}>The folder isn’t on {missing.join(', ')}, so {missing.length === 1 ? 'that host is' : 'those hosts are'} skipped.</Text>
                )}
              </div>
            )}

            {plan.kind === 'relay' && (
              <Text type="secondary" style={{ fontSize: 12 }}>The bytes go through this computer. The two hosts never connect to each other.</Text>
            )}
          </>
        )}
      </div>

      <Modal open={manage} title="Filters" width="min(92vw, 820px)" footer={null} onCancel={() => setManage(false)} destroyOnClose>
        <FiltersTab />
      </Modal>
    </Modal>
  )
}
