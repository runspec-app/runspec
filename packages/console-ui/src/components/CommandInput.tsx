import { useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Button, Tooltip } from 'antd'
import { SendOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { Runnable } from '../bridge'
import { ThemeContext } from '../ThemeContext'

interface SlashItem {
  topRunnable: Runnable   // top-level runnable (host/group context)
  leaf: Runnable          // subcommand or the runnable itself
  commandPath: string[]   // [] for top-level
}

interface CommandInputProps {
  runnables: Runnable[]
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>, commandPath: string[]) => void
  onOpenForm: (runnable: Runnable, commandPath: string[]) => void
  onSendChat: (message: string) => void
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

export function CommandInput({ runnables, onRunRunnable, onOpenForm, onSendChat, history, autoSwitch, onToggleAutoSwitch }: CommandInputProps) {
  const isDark = useContext(ThemeContext)
  const [value, setValue] = useState('')
  const [slashItems, setSlashItems] = useState<SlashItem[]>([])
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashFilter, setSlashFilter] = useState('')
  const [slashIndex, setSlashIndex] = useState(0)
  const [historyIndex, setHistoryIndex] = useState(-1)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [value])

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

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value
    setValue(v)
    setHistoryIndex(-1)

    if (v.startsWith('/')) {
      setSlashFilter(v.slice(1).split(' ')[0])
      setSlashOpen(true)
      setSlashIndex(0)
    } else {
      setSlashOpen(false)
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

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed) return
    if (slashOpen && filteredItems[slashIndex]) {
      selectSlashItem(filteredItems[slashIndex])
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

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
      return
    }

    if (!slashOpen) {
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        const next = Math.min(historyIndex + 1, history.length - 1)
        setHistoryIndex(next)
        setValue(history[history.length - 1 - next] ?? '')
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        const next = Math.max(historyIndex - 1, -1)
        setHistoryIndex(next)
        setValue(next === -1 ? '' : history[history.length - 1 - next] ?? '')
        return
      }
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      {/* Slash dropdown */}
      {slashOpen && filteredItems.length > 0 && (
        <div style={{
          position: 'absolute', bottom: '100%', left: 0, right: 0, marginBottom: 6,
          background: '#1e1e1e', border: '1px solid #333', borderRadius: 8,
          maxHeight: 280, overflowY: 'auto', zIndex: 100,
          boxShadow: '0 -4px 16px rgba(0,0,0,0.5)',
        }}>
          {filteredItems.map((item, idx) => (
            <div
              key={`${item.topRunnable.host}/${item.topRunnable.group}/${item.topRunnable.name}/${item.commandPath.join('/')}`}
              onMouseDown={() => selectSlashItem(item)}
              style={{
                padding: '8px 14px',
                cursor: 'pointer',
                background: idx === slashIndex ? '#2a2a2a' : 'transparent',
                display: 'flex', alignItems: 'baseline', gap: 10,
                borderBottom: '1px solid #222',
              }}
            >
              <span style={{ fontSize: 13, minWidth: 180 }}>
                <span style={{ color: '#666' }}>{item.topRunnable.group}/</span>
                <span style={{ color: '#4fc1ff' }}>{item.topRunnable.name}</span>
                {item.commandPath.map(seg => (
                  <span key={seg}>
                    <span style={{ color: '#555' }}> › </span>
                    <span style={{ color: '#89d4f5' }}>{seg}</span>
                  </span>
                ))}
              </span>
              <span style={{ color: '#777', fontSize: 11 }}>{item.topRunnable.host}</span>
              {(item.leaf.args ?? []).length > 0 && (
                <span style={{
                  flex: 1, fontSize: 11, color: '#555', fontFamily: 'monospace',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  marginLeft: 10,
                }}>
                  {formatUsage(item.leaf.args)}
                </span>
              )}
              {item.leaf.description && (
                <span style={{
                  fontSize: 11, color: '#777', flexShrink: 0,
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
        background: '#1a1a1a', border: '1px solid #333',
        borderRadius: 8, padding: '8px 12px',
      }}>
        <span style={{ color: '#555', fontSize: 14, paddingBottom: 2 }}>❯</span>
        <textarea
          ref={inputRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder="Type / for runnables (supports --help and inline args) or ask the LLM..."
          style={{
            flex: 1, background: 'transparent', border: 'none', outline: 'none',
            color: '#d4d4d4', fontSize: 14, resize: 'none',
            lineHeight: 1.5, minHeight: '42px', maxHeight: '240px', overflowY: 'auto',
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
                    ? (isDark ? '#4fc1ff' : '#0958d9')
                    : (isDark ? '#444' : '#ccc'),
                }}
              />
            </Tooltip>
          )}
          <Button
            type="text"
            icon={<SendOutlined />}
            onClick={submit}
            style={{ color: value.trim() ? '#1677ff' : (isDark ? '#444' : '#ccc'), padding: 4 }}
          />
        </div>
      </div>
    </div>
  )
}
