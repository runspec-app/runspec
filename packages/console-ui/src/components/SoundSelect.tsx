import { useEffect, useState } from 'react'
import { Select, Button, Space, Tooltip } from 'antd'
import { SoundOutlined } from '@ant-design/icons'
import { bridge, type NamedSound } from '../bridge'

// The bundled cue ids (mirrors sounds.KNOWN_SOUNDS in the backend). A picker
// value is what the backend's sounds.resolve_playable accepts: a bare built-in
// id ("confirm" → synth:confirm), a "sound:<id>" registry token, or "" (no cue).
const BUILTIN_CUES = [
  'confirm', 'success', 'error', 'stopped', 'trigger', 'reply',
  'message', 'host_up', 'host_down', 'room_up', 'room_down',
] as const

interface SoundSelectProps {
  value?: string
  onChange?: (value: string) => void
  disabled?: boolean
  size?: 'small' | 'middle' | 'large'
  width?: number | string
}

// A sound picker shared by the Triggers and Schedules editors: choose a named
// sound (from the runspec_sounds.toml registry, added in Settings → Sounds) or a
// built-in cue to play when the trigger/schedule fires. Empty = no sound. The ▶
// button auditions the current pick. Sounds only play on Windows; this is a
// no-op elsewhere.
export function SoundSelect({ value, onChange, disabled, size = 'middle', width = 240 }: SoundSelectProps) {
  const [named, setNamed] = useState<NamedSound[]>([])

  useEffect(() => {
    bridge.get_sounds().then(setNamed).catch(() => {})
  }, [])

  const options = [
    ...(named.length
      ? [{
          label: 'My sounds',
          options: named.map(s => ({ value: `sound:${s.id}`, label: s.label })),
        }]
      : []),
    {
      label: 'Built-in cues',
      options: BUILTIN_CUES.map(c => ({ value: c, label: c })),
    },
  ]

  return (
    <Space size={4}>
      <Select
        size={size}
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="No sound"
        disabled={disabled}
        value={value || undefined}
        onChange={v => onChange?.(v ?? '')}
        options={options}
        style={{ width }}
        popupMatchSelectWidth={false}
      />
      <Tooltip title="Preview this sound">
        <Button
          size={size === 'large' ? 'middle' : size}
          type="text"
          icon={<SoundOutlined />}
          disabled={disabled || !value}
          onClick={() => value && bridge.preview_sound(value).catch(() => {})}
        />
      </Tooltip>
    </Space>
  )
}
