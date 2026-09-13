import { useEffect, useState } from 'react'
import { Select, Input, Typography } from 'antd'
import { bridge, type Filter } from '../bridge'
import { isParameterized } from '../components/SettingsDrawer'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

// humanBytes — compact size for the indeterminate "N written" readout.
export function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`
}

// FilterPicker — select a saved source-side filter (Settings → Filters) and, when
// the chosen filter is parameterized ({{arg}}), prompt for its runtime value. The
// filtered pipeline runs on the source host so only matching output is transferred.
export function FilterPicker({
  filterName,
  filterArg,
  onFilterName,
  onFilterArg,
}: {
  filterName: string
  filterArg: string
  onFilterName: (name: string) => void
  onFilterArg: (arg: string) => void
}) {
  const [filters, setFilters] = useState<Filter[]>([])
  useEffect(() => { bridge.get_filters().then(setFilters).catch(() => {}) }, [])

  const selected = filters.find(f => f.name === filterName)
  const needsArg = selected ? isParameterized(selected.pipeline) : false

  return (
    <div>
      <Text style={{ fontSize: 13 }}>
        Filter <Text type="secondary" style={{ fontSize: 12 }}>(optional — runs on the source, shrinks the transfer)</Text>
      </Text>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginTop: 6 }}>
        <Select
          size="small"
          allowClear
          value={filterName || undefined}
          onChange={v => { onFilterName(v || ''); onFilterArg('') }}
          placeholder="No filter"
          style={{ minWidth: 180 }}
          options={filters.map(f => ({ label: f.name, value: f.name }))}
        />
        {needsArg && (
          <Input
            size="small"
            value={filterArg}
            onChange={e => onFilterArg(e.target.value)}
            placeholder={selected?.param_label || 'value'}
            style={{ width: 220, fontFamily: MONO_FONT }}
          />
        )}
      </div>
      {selected?.description && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>{selected.description}</Text>
      )}
    </div>
  )
}
