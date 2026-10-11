import { useEffect, useMemo, useState } from 'react'
import { AutoComplete, Button, InputNumber, Select, Space, Switch, Typography, message } from 'antd'
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { bridge, type HostGroup, type TerminalTheme } from '../bridge'
import { RADIUS, textSecondary, useIsDark } from '../ThemeContext'

const { Text } = Typography

// Monospace fonts that ship with Windows (Cascadia with Windows 11 / Terminal).
export const TERMINAL_FONTS = ['Cascadia Mono', 'Cascadia Code', 'Consolas', 'Lucida Console', 'Courier New']

// The `[terminal]` section of config.toml (see runspec_console/terminal_look.py).
export interface TerminalLookConfig {
  theme: string
  font: string
  font_size: number | null
  cursor: string
  cursor_blink: boolean | null
  scrollback: number | null
  padding: number | null
  group_themes: { group: string; theme: string }[]
}

export function lookFromConfig(raw: unknown): TerminalLookConfig {
  const t = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const num = (v: unknown) => (typeof v === 'number' && v > 0 ? v : null)
  const groups = (t.group_themes && typeof t.group_themes === 'object' ? t.group_themes : {}) as Record<string, unknown>
  return {
    theme: String(t.theme ?? ''),
    font: String(t.font ?? ''),
    font_size: num(t.font_size),
    cursor: String(t.cursor ?? ''),
    cursor_blink: typeof t.cursor_blink === 'boolean' ? t.cursor_blink : null,
    scrollback: num(t.scrollback),
    padding: num(t.padding),
    group_themes: Object.entries(groups).map(([group, theme]) => ({ group, theme: String(theme ?? '') })),
  }
}

/** The `[terminal]` table to save — unset fields are left out, so an untouched
 * look writes nothing and PuTTY keeps its own defaults. */
export function lookToConfig(look: TerminalLookConfig): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (look.theme) out.theme = look.theme
  if (look.font.trim()) out.font = look.font.trim()
  if (look.font_size) out.font_size = look.font_size
  if (look.cursor) out.cursor = look.cursor
  if (look.cursor_blink !== null) out.cursor_blink = look.cursor_blink
  if (look.scrollback) out.scrollback = look.scrollback
  if (look.padding) out.padding = look.padding
  const groups: Record<string, string> = {}
  for (const g of look.group_themes) if (g.group.trim() && g.theme) groups[g.group.trim()] = g.theme
  if (Object.keys(groups).length) out.group_themes = groups
  return out
}

/** A few lines of a shell session drawn in a theme's colours. */
export function TerminalPreview({ theme, font, size }: { theme?: TerminalTheme; font: string; size: number | null }) {
  const fg = theme?.fg ?? '#c0c0c0'
  const bg = theme?.bg ?? '#000000'
  const a = theme?.ansi ?? ['#000000', '#bb0000', '#00bb00', '#bbbb00', '#0000bb', '#bb00bb', '#00bbbb', '#bbbbbb']
  return (
    <div
      data-testid="terminal-preview"
      style={{
        background: bg, color: fg, borderRadius: RADIUS.sm, padding: '8px 10px', marginBottom: 12,
        fontFamily: `${font ? `'${font}', ` : ''}Consolas, 'Courier New', monospace`,
        fontSize: Math.round((size ?? 10) * 1.33), lineHeight: 1.35, whiteSpace: 'pre', overflowX: 'auto',
      }}
    >
      <div><span style={{ color: a[2] }}>ops@web-01</span>:<span style={{ color: a[4] }}>~/app</span>$ ls</div>
      <div><span style={{ color: a[4] }}>logs</span>  <span style={{ color: a[2] }}>run.sh</span>  config.toml  <span style={{ color: a[6] }}>current</span></div>
      <div><span style={{ color: a[2] }}>ops@web-01</span>:<span style={{ color: a[4] }}>~/app</span>$ systemctl status app</div>
      <div><span style={{ color: a[1] }}>● failed</span>  <span style={{ color: a[3] }}>warning: restarting</span>  <span style={{ color: a[5] }}>pid 4012</span></div>
      <div>
        <span style={{ color: a[2] }}>ops@web-01</span>:<span style={{ color: a[4] }}>~/app</span>$ <span style={{ background: theme?.cursor ?? fg }}> </span>
      </div>
    </div>
  )
}

/** Settings → SSH → Terminal look: fonts, colours and cursor for PuTTY windows. */
export function TerminalLook() {
  const isDark = useIsDark()
  const [themes, setThemes] = useState<TerminalTheme[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [look, setLook] = useState<TerminalLookConfig>(lookFromConfig(undefined))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    bridge.terminal_themes().then(ts => setThemes(ts ?? [])).catch(() => setThemes([]))
    bridge.get_groups().then((gs: HostGroup[]) => setGroups((gs ?? []).map(g => g.name))).catch(() => setGroups([]))
    bridge.get_config().then(cfg => setLook(lookFromConfig(cfg?.terminal))).catch(() => {})
  }, [])

  const themeOptions = useMemo(
    () => [{ value: '', label: "PuTTY's own colours" }, ...themes.map(t => ({ value: t.id, label: t.label }))],
    [themes],
  )
  const set = (patch: Partial<TerminalLookConfig>) => setLook(l => ({ ...l, ...patch }))

  const save = async () => {
    setSaving(true)
    try {
      const cfg = await bridge.get_config()
      const out = lookToConfig(look)
      const next: Record<string, unknown> = { ...cfg }
      if (Object.keys(out).length) next.terminal = out
      else delete next.terminal
      await bridge.save_config(next)
      message.success('Terminal look saved — new terminals use it')
    } catch (e) {
      message.error(`Could not save: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  const label = (s: string) => <Text style={{ fontSize: 12, color: textSecondary(isDark), display: 'block', marginBottom: 4 }}>{s}</Text>

  return (
    <div style={{ marginTop: 8 }}>
      <Text strong style={{ fontSize: 14, display: 'block', marginBottom: 4 }}>Terminal look</Text>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        Font, colours and cursor for PuTTY terminals. Applies to terminals opened from now on. Anything left blank keeps your own PuTTY Default Settings.
      </Text>

      <TerminalPreview theme={themes.find(t => t.id === look.theme)} font={look.font} size={look.font_size} />

      <Space wrap size={[12, 8]} align="start" style={{ marginBottom: 8 }}>
        <div style={{ width: 210 }}>
          {label('Colours')}
          <Select size="small" style={{ width: '100%' }} value={look.theme} options={themeOptions} onChange={v => set({ theme: v })} aria-label="Colours" />
        </div>
        <div style={{ width: 170 }}>
          {label('Font')}
          <AutoComplete
            size="small" style={{ width: '100%' }} value={look.font} placeholder="PuTTY default"
            options={TERMINAL_FONTS.map(f => ({ value: f }))} onChange={v => set({ font: v })} aria-label="Font"
          />
        </div>
        <div style={{ width: 80 }}>
          {label('Size (pt)')}
          <InputNumber size="small" min={6} max={72} style={{ width: '100%' }} value={look.font_size} placeholder="10" onChange={v => set({ font_size: v ?? null })} />
        </div>
      </Space>
      <Space wrap size={[12, 8]} align="start" style={{ marginBottom: 12 }}>
        <div style={{ width: 130 }}>
          {label('Cursor')}
          <Select
            size="small" style={{ width: '100%' }} value={look.cursor} onChange={v => set({ cursor: v })}
            options={[{ value: '', label: 'Default' }, { value: 'block', label: 'Block' }, { value: 'underline', label: 'Underline' }, { value: 'bar', label: 'Bar' }]}
          />
        </div>
        <div>
          {label('Blink')}
          <Switch size="small" checked={look.cursor_blink ?? false} onChange={v => set({ cursor_blink: v })} />
        </div>
        <div style={{ width: 110 }}>
          {label('Scrollback lines')}
          <InputNumber size="small" min={100} style={{ width: '100%' }} value={look.scrollback} placeholder="2000" onChange={v => set({ scrollback: v ?? null })} />
        </div>
        <div style={{ width: 90 }}>
          {label('Padding (px)')}
          <InputNumber size="small" min={0} max={40} style={{ width: '100%' }} value={look.padding} placeholder="1" onChange={v => set({ padding: v ?? null })} />
        </div>
      </Space>

      {label('Colours per host group (e.g. red for production)')}
      {look.group_themes.map((g, i) => (
        <Space key={i} style={{ display: 'flex', marginBottom: 6 }}>
          <Select
            size="small" style={{ width: 170 }} value={g.group || undefined} placeholder="Group" showSearch
            options={groups.map(n => ({ value: n, label: n }))}
            onChange={v => set({ group_themes: look.group_themes.map((x, j) => (j === i ? { ...x, group: v } : x)) })}
          />
          <Select
            size="small" style={{ width: 210 }} value={g.theme || undefined} placeholder="Colours"
            options={themes.map(t => ({ value: t.id, label: t.label }))}
            onChange={v => set({ group_themes: look.group_themes.map((x, j) => (j === i ? { ...x, theme: v } : x)) })}
          />
          <Button size="small" type="text" icon={<MinusCircleOutlined />} aria-label="Remove group colours"
            onClick={() => set({ group_themes: look.group_themes.filter((_, j) => j !== i) })} />
        </Space>
      ))}
      <Button size="small" type="dashed" icon={<PlusOutlined />} style={{ marginBottom: 12 }}
        onClick={() => set({ group_themes: [...look.group_themes, { group: '', theme: 'red-alert' }] })}>
        Add group colours
      </Button>

      <div>
        <Button size="small" type="primary" loading={saving} onClick={save}>Save terminal look</Button>
      </div>
    </div>
  )
}
