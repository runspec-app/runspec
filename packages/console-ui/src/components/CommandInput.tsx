import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Button, Tooltip, message } from 'antd'
import { SendOutlined, ThunderboltOutlined, StopOutlined } from '@ant-design/icons'
import type { Runnable, Runbook } from '../bridge'
import { ThemeContext, useAccent, textFaint, textMuted } from '../ThemeContext'

interface SlashItem {
  topRunnable: Runnable   // top-level runnable (host/group context)
  leaf: Runnable          // subcommand or the runnable itself
  commandPath: string[]   // [] for top-level
}

interface CommandInputProps {
  runnables: Runnable[]
  runbooks: Runbook[]
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[]) => void
  onOpenForm: (runnable: Runnable, commandPath: string[]) => void
  onOpenRunbook: (runbook: Runbook) => void
  onSendChat: (message: string) => void
  activeChatId?: string | null   // set while a chat turn is in flight
  onStopChat?: (id: string) => void
  history: string[]
  autoSwitch?: boolean
  onToggleAutoSwitch?: () => void
}

function formatUsage(args: Runnable['args']): string {
  return (args ?? []).map(a => {
    if (a.type === 'choice' && a.options?.length) {
      const opts = a.options.join('|')
      return a.required ? `--${a.name} ${opts}` : `[--${a.name} ${opts}]`
    }
    if (a.type === 'flag') return `[--${a.name}]`
    return a.required ? `--${a.name} <${a.type}>` : `[--${a.name}]`
  }).join('  ')
}

function parseInlineArgs(input: string): Record<string, unknown> {
  const args: Record<string, unknown> = {}
  const parts = input.trim().split(/\s+/).slice(1) // drop /runnable
  for (const part of parts) {
    if (!part.startsWith('--')) continue
    const eq = part.indexOf('=')
    if (eq === -1) args[part.slice(2)] = true
    else args[part.slice(2, eq)] = part.slice(eq + 1)
  }
  return args
}

function flattenToSlashItems(runnables: Runnable[]): SlashItem[] {
  const items: SlashItem[] = []

  function recurse(top: Runnable, node: Runnable, path: string[]) {
    const subs = Object.entries(node.commands ?? {})
    if (subs.length === 0) {
      // Leaf — always a slash item
      items.push({ topRunnable: top, leaf: node, commandPath: path })
    } else {
      // Has subcommands — include self only if it has its own args
      if ((node.args ?? []).length > 0) {
        items.push({ topRunnable: top, leaf: node, commandPath: path })
      }
      for (const [name, child] of subs) {
        recurse(top, child, [...path, name])
      }
    }
  }

  for (const r of runnables) recurse(r, r, [])
  return items
}

export function CommandInput({ runnables, runbooks, onRunRunnable, onOpenForm, onOpenRunbook, onSendChat, activeChatId, onStopChat, history, autoSwitch, onToggleAutoSwitch }: CommandInputProps) {
  const isDark = useContext(ThemeContext)
  const accent = useAccent()
  // Draft persistence: restore a half-typed prompt across tab switches and app
  // restarts (localStorage), so an in-progress message is never lost. Cleared on
  // send (setValue('') writes through below).
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem('runspec:composerDraft') ?? '' } catch { return '' }
  })
  const [slashItems, setSlashItems] = useState<SlashItem[]>([])
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashFilter, setSlashFilter] = useState('')
  const [slashIndex, setSlashIndex] = useState(0)
  // `$` opens the runbooks menu (parallel to `/` for runnables) — mutually exclusive.
  const [runbooksOpen, setRunbooksOpen] = useState(false)
  const [runbooksFilter, setRunbooksFilter] = useState('')
  const [runbooksIndex, setRunbooksIndex] = useState(0)
  const [historyIndex, setHistoryIndex] = useState(-1)
  // Stashes the in-progress draft when you step up into history, so stepping
  // back down to the bottom restores what you were typing instead of blanking.
  const draftRef = useRef('')
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Auto-grow the textarea to fit its content, growing *upward* over the
  // transcript (the composer is anchored at the bottom of a flex column, so extra
  // height pushes its top edge up). Cap at ~45% of the viewport, then scroll
  // internally — recomputed on window resize so the cap tracks the window.
  const resizeTextarea = useCallback(() => {
    const el = inputRef.current
    if (!el) return
    const cap = Math.max(160, Math.round(window.innerHeight * 0.45))
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, cap)}px`
    el.style.maxHeight = `${cap}px`
  }, [])

  useLayoutEffect(() => { resizeTextarea() }, [value, resizeTextarea])

  useEffect(() => {
    window.addEventListener('resize', resizeTextarea)
    return () => window.removeEventListener('resize', resizeTextarea)
  }, [resizeTextarea])

  // Persist the draft on every change so it survives a tab switch / restart.
  useEffect(() => {
    try { localStorage.setItem('runspec:composerDraft', value) } catch { /* ignore */ }
  }, [value])

  // Load a message into the composer for editing (Edit & resend on a chat card
  // dispatches this with the original prompt). We focus so the operator can tweak
  // and press Enter.
  useEffect(() => {
    const onLoad = (e: Event) => {
      const { text } = (e as CustomEvent).detail as { text: string }
      setValue(text)
      setSlashOpen(false)
      setRunbooksOpen(false)
      setHistoryIndex(-1)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
    window.addEventListener('runspec:load_composer', onLoad)
    return () => window.removeEventListener('runspec:load_composer', onLoad)
  }, [])

  useEffect(() => {
    setSlashItems(flattenToSlashItems(runnables))
  }, [runnables])

  const filteredItems = slashFilter
    ? slashItems.filter(i => {
        const low = slashFilter.toLowerCase()
        const fullName = [i.topRunnable.name, ...i.commandPath].join(' ')
        return fullName.toLowerCase().includes(low) ||
          i.topRunnable.group.toLowerCase().includes(low) ||
          i.topRunnable.host.toLowerCase().includes(low) ||
          (i.leaf.description ?? '').toLowerCase().includes(low)
      })
    : slashItems

  const filteredRunbooks = runbooksFilter
    ? runbooks.filter(s => {
        const low = runbooksFilter.toLowerCase()
        return (s.id ?? '').toLowerCase().includes(low) ||
          (s.title ?? '').toLowerCase().includes(low) ||
          (s.symptom ?? '').toLowerCase().includes(low) ||
          (s.tags ?? []).some(t => t.toLowerCase().includes(low))
      })
    : runbooks

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value
    setValue(v)
    setHistoryIndex(-1)

    if (v.startsWith('/')) {
      setSlashFilter(v.slice(1).split(' ')[0])
      setSlashOpen(true)
      setSlashIndex(0)
      setRunbooksOpen(false)
    } else if (v.startsWith('$')) {
      setRunbooksFilter(v.slice(1).split(' ')[0])
      setRunbooksOpen(true)
      setRunbooksIndex(0)
      setSlashOpen(false)
    } else {
      setSlashOpen(false)
      setRunbooksOpen(false)
    }
  }

  const selectSlashItem = (item: SlashItem) => {
    setSlashOpen(false)
    const inlineArgs = parseInlineArgs(value)
    setValue('')
    if (Object.keys(inlineArgs).length > 0) {
      onRunRunnable(item.topRunnable, inlineArgs, item.commandPath)
      return
    }
    const hasRequired = (item.leaf.args ?? []).some(a => a.required)
    const hasSubcommands = Object.keys(item.leaf.commands ?? {}).length > 0
    if (hasRequired || hasSubcommands) {
      onOpenForm(item.topRunnable, item.commandPath)
    } else {
      onRunRunnable(item.topRunnable, {}, item.commandPath)
    }
  }

  const selectRunbookItem = (runbook: Runbook) => {
    setRunbooksOpen(false)
    setValue('')
    onOpenRunbook(runbook)
  }

  const submit = () => {
    if (runbooksOpen && filteredRunbooks[runbooksIndex]) {
      selectRunbookItem(filteredRunbooks[runbooksIndex])
      return
    }
    const trimmed = value.trim()
    if (!trimmed) return
    if (slashOpen && filteredItems[slashIndex]) {
      selectSlashItem(filteredItems[slashIndex])
      return
    }
    // While a chat turn is in flight the primary action is Stop, not send.
    // Don't queue a second turn behind the lock — but give visible feedback
    // instead of silently swallowing the prompt (which reads as "app frozen").
    // The typed value is left intact so it can be resent after Stop.
    if (activeChatId) {
      message.info('Agent is working — press Stop to interrupt', 2)
      return
    }
    onSendChat(trimmed)
    setValue('')
    setSlashOpen(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashOpen) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSlashIndex(i => Math.min(i + 1, filteredItems.length - 1)); return }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSlashIndex(i => Math.max(i - 1, 0)); return }
      if (e.key === 'Escape')    { setSlashOpen(false); return }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault()
        if (filteredItems[slashIndex]) selectSlashItem(filteredItems[slashIndex])
        return
      }
    }

    if (runbooksOpen) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setRunbooksIndex(i => Math.min(i + 1, filteredRunbooks.length - 1)); return }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setRunbooksIndex(i => Math.max(i - 1, 0)); return }
      if (e.key === 'Escape')    { setRunbooksOpen(false); return }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault()
        if (filteredRunbooks[runbooksIndex]) selectRunbookItem(filteredRunbooks[runbooksIndex])
        return
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
      return
    }

    if (!slashOpen && !runbooksOpen) {
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        // Stash the live draft on the first step into history so we can restore
        // it on the way back down.
        if (historyIndex < 0) draftRef.current = value
        const next = Math.min(historyIndex + 1, history.length - 1)
        setHistoryIndex(next)
        setValue(history[history.length - 1 - next] ?? '')
        return
      }
      if (e.key === 'ArrowDown') {
        // Only walk history downward when we're already browsing it (entered via
        // ArrowUp). A bare ArrowDown on a freshly typed prompt must leave the
        // text alone — moving the caret — rather than clearing the input.
        if (historyIndex < 0) return
        e.preventDefault()
        const next = Math.max(historyIndex - 1, -1)
        setHistoryIndex(next)
        // Returning to the bottom restores the stashed draft rather than blanking.
        setValue(next === -1 ? draftRef.current : history[history.length - 1 - next] ?? '')
        return
      }
    }
  }

  // Theme-aware palette for the composer + its menus (the prompt was hard-coded
  // dark; now it follows the light/dark toggle like the rest of the chrome).
  const surfaceBg = isDark ? '#1a1a1a' : '#f5f5f5'
  const surfaceBorder = isDark ? '#333' : '#d9d9d9'
  const menuBg = isDark ? '#1e1e1e' : '#ffffff'
  const itemBorder = isDark ? '#222' : '#f0f0f0'
  const itemHover = isDark ? '#2a2a2a' : '#eef1f5'
  const promptChar = textFaint(isDark)
  const inputText = isDark ? '#d4d4d4' : '#222'
  const dimText = textMuted(isDark)
  const groupText = textMuted(isDark)
  const nameCol = accent
  const subCol = isDark ? '#89d4f5' : '#1677ff'

  return (
    <div style={{ position: 'relative' }}>
      {/* Runbooks dropdown ($) */}
      {runbooksOpen && filteredRunbooks.length > 0 && (
        <div style={{
          position: 'absolute', bottom: '100%', left: 0, right: 0, marginBottom: 6,
          background: menuBg, border: `1px solid ${surfaceBorder}`, borderRadius: 8,
          maxHeight: 280, overflowY: 'auto', zIndex: 100,
          boxShadow: isDark ? '0 -4px 16px rgba(0,0,0,0.5)' : '0 -4px 16px rgba(0,0,0,0.12)',
        }}>
          {filteredRunbooks.map((runbook, idx) => (
            <div
              key={runbook.id}
              onMouseDown={() => selectRunbookItem(runbook)}
              style={{
                padding: '8px 14px',
                cursor: 'pointer',
                background: idx === runbooksIndex ? itemHover : 'transparent',
                display: 'flex', alignItems: 'baseline', gap: 10,
                borderBottom: `1px solid ${itemBorder}`,
              }}
            >
              <span style={{ fontSize: 13, minWidth: 180 }}>
                <span style={{ color: '#bd93f9' }}>${runbook.id}</span>
              </span>
              {runbook.title && (
                <span style={{ fontSize: 12, color: inputText, flexShrink: 0 }}>{runbook.title}</span>
              )}
              {(runbook.args ?? []).length > 0 && (
                <span style={{ fontSize: 11, color: groupText, flexShrink: 0 }}>
                  {(runbook.args ?? []).map(a => a.name).join(' ')}
                </span>
              )}
              {runbook.symptom && (
                <span style={{
                  flex: 1, fontSize: 11, color: dimText,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  marginLeft: 10,
                }}>
                  {runbook.symptom}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Slash dropdown */}
      {slashOpen && filteredItems.length > 0 && (
        <div style={{
          position: 'absolute', bottom: '100%', left: 0, right: 0, marginBottom: 6,
          background: menuBg, border: `1px solid ${surfaceBorder}`, borderRadius: 8,
          maxHeight: 280, overflowY: 'auto', zIndex: 100,
          boxShadow: isDark ? '0 -4px 16px rgba(0,0,0,0.5)' : '0 -4px 16px rgba(0,0,0,0.12)',
        }}>
          {filteredItems.map((item, idx) => (
            <div
              key={`${item.topRunnable.host}/${item.topRunnable.group}/${item.topRunnable.name}/${item.commandPath.join('/')}`}
              onMouseDown={() => selectSlashItem(item)}
              style={{
                padding: '8px 14px',
                cursor: 'pointer',
                background: idx === slashIndex ? itemHover : 'transparent',
                display: 'flex', alignItems: 'baseline', gap: 10,
                borderBottom: `1px solid ${itemBorder}`,
              }}
            >
              <span style={{ fontSize: 13, minWidth: 180 }}>
                <span style={{ color: groupText }}>{item.topRunnable.group}/</span>
                <span style={{ color: nameCol }}>{item.topRunnable.name}</span>
                {item.commandPath.map(seg => (
                  <span key={seg}>
                    <span style={{ color: dimText }}> › </span>
                    <span style={{ color: subCol }}>{seg}</span>
                  </span>
                ))}
              </span>
              <span style={{ color: dimText, fontSize: 11 }}>{item.topRunnable.host}</span>
              {(item.leaf.args ?? []).length > 0 && (
                <span style={{
                  flex: 1, fontSize: 11, color: groupText, fontFamily: 'monospace',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  marginLeft: 10,
                }}>
                  {formatUsage(item.leaf.args)}
                </span>
              )}
              {item.leaf.description && (
                <span style={{
                  fontSize: 11, color: dimText, flexShrink: 0,
                  marginLeft: 10, whiteSpace: 'nowrap',
                }}>
                  {item.leaf.description}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Input row */}
      <div style={{
        display: 'flex', gap: 8, alignItems: 'flex-end',
        background: surfaceBg, border: `1px solid ${surfaceBorder}`,
        borderRadius: 8, padding: '8px 12px',
      }}>
        <span style={{ color: promptChar, fontSize: 14, paddingBottom: 2 }}>❯</span>
        <textarea
          ref={inputRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder="Type / for runnables, $ for runbooks, or ask the assistant..."
          style={{
            flex: 1, background: 'transparent', border: 'none', outline: 'none',
            color: inputText, fontSize: 14, resize: 'none', fontFamily: 'monospace',
            lineHeight: 1.5, minHeight: '42px', overflowY: 'auto',
          }}
        />
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, flexShrink: 0 }}>
          {onToggleAutoSwitch !== undefined && (
            <Tooltip
              title={autoSwitch ? 'Auto-switch to Console: on — click to disable' : 'Auto-switch to Console: off — click to enable'}
              placement="top"
            >
              <Button
                type="text"
                icon={<ThunderboltOutlined style={{ fontSize: 12 }} />}
                onClick={onToggleAutoSwitch}
                style={{
                  padding: '2px 4px', height: 20, width: 24,
                  color: autoSwitch
                    ? accent
                    : (textFaint(isDark)),
                }}
              />
            </Tooltip>
          )}
          {activeChatId ? (
            <Tooltip title="Stop — halt the assistant" placement="top">
              <Button
                type="text"
                icon={<StopOutlined />}
                onClick={() => onStopChat?.(activeChatId)}
                style={{ color: '#ff4d4f', padding: 4, transition: 'color 0.15s ease' }}
              />
            </Tooltip>
          ) : (
            <Button
              type="text"
              icon={<SendOutlined />}
              onClick={submit}
              style={{ color: value.trim() ? '#1677ff' : (textFaint(isDark)), padding: 4, transition: 'color 0.15s ease' }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
