/**
 * CommandPalette — the Ctrl+K "do anything" box. A searchable list of every
 * action App hands it: shortcut actions (with their key hints), tabs, runnables
 * (open the form), runbooks, saved runs, hosts and host profiles.
 *
 * Built on `cmdk` (headless — filtering, ranking, arrow-key selection) inside an
 * antd Modal, themed from the --rs-* vars in index.css (`.rs-cmdk`). Generic on
 * purpose: App owns what the actions do; the palette only finds and runs one.
 */
import { useEffect, useRef, type ReactNode } from 'react'
import { Modal } from 'antd'
import { Command } from 'cmdk'
import { Keys } from './ShortcutsHelp'
import { useIsDark, textColor, textMuted } from '../ThemeContext'

export interface PaletteAction {
  /** Unique across the whole list (cmdk keys items by value). */
  id: string
  label: string
  /** Heading the item is grouped under ('Actions', 'Go to', 'Runnables', …). */
  group: string
  /** Secondary text (a description, a host list). Searched too. */
  detail?: string
  /** Shortcut shown on the right, e.g. 'Alt+A'. */
  hint?: string
  keywords?: string[]
  icon?: ReactNode
  run: () => void
}

export function CommandPalette({ open, onClose, actions }: {
  open: boolean
  onClose: () => void
  actions: PaletteAction[]
}) {
  const isDark = useIsDark()
  // antd's Modal moves focus into its own focus trap as it opens, which beats
  // `autoFocus` — focus the search box once it has settled instead.
  const inputRef = useRef<HTMLInputElement>(null)
  const focusInput = () => inputRef.current?.focus()
  useEffect(() => {
    if (!open) return
    const t = setTimeout(focusInput, 30)
    return () => clearTimeout(t)
  }, [open])
  const groups: string[] = []
  for (const a of actions) if (!groups.includes(a.group)) groups.push(a.group)

  const choose = (a: PaletteAction) => {
    onClose()
    // Run after the modal starts closing so focus moves can land (e.g. the
    // command bar) instead of returning to the palette's input.
    setTimeout(a.run, 0)
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      closable={false}
      width={580}
      style={{ top: 80 }}
      styles={{ content: { padding: 0, overflow: 'hidden' } }}
      afterOpenChange={o => { if (o) focusInput() }}
    >
      <Command className="rs-cmdk" label="Command palette" loop style={{ color: textColor(isDark) }}>
        <Command.Input ref={inputRef} autoFocus placeholder="Type a command, tab, runnable, host…" />
        <Command.List>
          <Command.Empty>No matches</Command.Empty>
          {groups.map(g => (
            <Command.Group key={g} heading={g}>
              {actions.filter(a => a.group === g).map(a => (
                <Command.Item
                  key={a.id}
                  value={a.id}
                  keywords={[a.label, ...(a.detail ? [a.detail] : []), ...(a.keywords ?? [])]}
                  onSelect={() => choose(a)}
                >
                  {a.icon && <span style={{ color: textMuted(isDark), display: 'inline-flex' }}>{a.icon}</span>}
                  <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {a.label}
                    {a.detail && <span style={{ color: textMuted(isDark), marginLeft: 8, fontSize: 12 }}>{a.detail}</span>}
                  </span>
                  {a.hint && <Keys combo={a.hint} />}
                </Command.Item>
              ))}
            </Command.Group>
          ))}
        </Command.List>
      </Command>
    </Modal>
  )
}
