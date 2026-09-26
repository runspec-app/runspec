import { useEffect, useState } from 'react'
import { Select } from 'antd'
import { bridge } from '../bridge'
import { MONO_FONT } from '../ThemeContext'

interface ModelSelectProps {
  value?: string
  onChange?: (value: string) => void
  placeholder?: string
  width?: number | string
}

// The operator's model list: [llm] models plus the active [llm] model, deduped.
export function modelOptions(llm: Record<string, unknown> | undefined): string[] {
  const list = Array.isArray(llm?.models) ? (llm!.models as unknown[]).map(String).filter(Boolean) : []
  const active = typeof llm?.model === 'string' ? llm.model : ''
  return active && !list.includes(active) ? [active, ...list] : list
}

// A model picker shared by the Triggers and Schedules editors (a trigger's or a
// scheduled prompt's own model) and Settings (the background utility model).
// Options come from the operator's [llm] models list; empty = use the [llm] model.
export function ModelSelect({ value, onChange, placeholder = 'Default model', width = 280 }: ModelSelectProps) {
  const [models, setModels] = useState<string[]>([])

  useEffect(() => {
    bridge.get_config()
      .then(cfg => setModels(modelOptions(cfg.llm as Record<string, unknown> | undefined)))
      .catch(() => {})
  }, [])

  // Keep a hand-edited value selectable even if it isn't in the list.
  const options = (value && !models.includes(value) ? [value, ...models] : models)
    .map(m => ({ value: m, label: m }))

  return (
    <Select
      allowClear
      showSearch
      placeholder={placeholder}
      value={value || undefined}
      onChange={v => onChange?.(v ?? '')}
      options={options}
      style={{ width, fontFamily: MONO_FONT }}
      popupMatchSelectWidth={false}
    />
  )
}
