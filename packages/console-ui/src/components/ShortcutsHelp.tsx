/**
 * ShortcutsHelp — the keyboard shortcut sheet (Ctrl+/ or `?`). Rendered
 * straight from the `SHORTCUTS` registry, so it can never drift from what the
 * keys actually do.
 */
import { Modal } from 'antd'
import { activeShortcuts, type ShortcutDef, type ShortcutGroup } from '../shortcuts'
import { useFeatures } from '../features'
import { useIsDark, textColor, textMuted, textSecondary } from '../ThemeContext'

const GROUP_ORDER: ShortcutGroup[] = ['General', 'Navigation', 'Console', 'Command bar']

/** One key cap per `+`-joined part: 'Ctrl+Shift+Tab' → Ctrl · Shift · Tab. */
export function Keys({ combo }: { combo: string }) {
  const parts = combo === '+' ? ['+'] : combo.split('+')
  return (
    <span style={{ display: 'inline-flex', gap: 3 }}>
      {parts.map((p, i) => <kbd key={i} className="rs-kbd">{p}</kbd>)}
    </span>
  )
}

export function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  const isDark = useIsDark()
  const { ai } = useFeatures()
  const defs = activeShortcuts(ai)
  const groups = GROUP_ORDER
    .map(g => [g, defs.filter(d => d.group === g)] as [ShortcutGroup, ShortcutDef[]])
    .filter(([, list]) => list.length > 0)

  return (
    <Modal open={open} onCancel={onClose} footer={null} title="Keyboard shortcuts" width={520}>
      {groups.map(([group, list]) => (
        <div key={group} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.4, color: textMuted(isDark), marginBottom: 6 }}>
            {group}
          </div>
          {list.map(d => (
            <div key={d.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 0', gap: 12 }}>
              <span style={{ color: textColor(isDark), fontSize: 13 }}>{d.label}</span>
              <span style={{ display: 'inline-flex', gap: 8, flexShrink: 0, color: textMuted(isDark), fontSize: 11 }}>
                {(d.display ?? d.keys).map((k, i) => (
                  <span key={k} style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
                    {i > 0 && 'or'}
                    <Keys combo={k} />
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      ))}
      <div style={{ fontSize: 12, color: textSecondary(isDark), marginTop: 4 }}>
        Single keys like <Keys combo="?" /> and <Keys combo="/" /> only work when you aren't typing in a field.
        Ctrl+R / F5 (reload), Ctrl+F (find) and Ctrl+P (print) keep their usual meaning.
      </div>
    </Modal>
  )
}
