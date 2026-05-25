import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Button } from 'antd'
import { SendOutlined } from '@ant-design/icons'
import type { Runnable } from '../bridge'

interface SlashItem {
  host: string
  runnable: Runnable
}

interface CommandInputProps {
  runnables: Runnable[]
  onRunRunnable: (runnable: Runnable, args: Record<string, unknown>) => void
  onSendChat: (message: string) => void
  history: string[]
}

export function CommandInput({ runnables, onRunRunnable, onSendChat, history }: CommandInputProps) {
  const [value, setValue] = useState('')
  const [slashItems, setSlashItems] = useState<SlashItem[]>([])
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashFilter, setSlashFilter] = useState('')
  const [slashIndex, setSlashIndex] = useState(0)
  const [historyIndex, setHistoryIndex] = useState(-1)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Auto-grow: runs after every value change (typing, paste, history nav, clear)
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [value])

  // Build slash items from runnables
  useEffect(() => {
    setSlashItems(runnables.map(r => ({ host: r.host, runnable: r })))
  }, [runnables])

  const filteredItems = slashFilter
    ? slashItems.filter(i =>
        i.runnable.name.includes(slashFilter) ||
        i.host.includes(slashFilter) ||
        (i.runnable.description ?? '').toLowerCase().includes(slashFilter.toLowerCase())
      )
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
    // For now: invoke with empty args. A full implementation would show an arg form.
    onRunRunnable(item.runnable, {})
    setValue('')
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
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSlashIndex(i => Math.min(i + 1, filteredItems.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSlashIndex(i => Math.max(i - 1, 0))
        return
      }
      if (e.key === 'Escape') {
        setSlashOpen(false)
        return
      }
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

    // Up/down for history (when input is empty or not in slash mode)
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
              key={`${item.host}/${item.runnable.group}/${item.runnable.name}`}
              onMouseDown={() => selectSlashItem(item)}
              style={{
                padding: '8px 14px',
                cursor: 'pointer',
                background: idx === slashIndex ? '#2a2a2a' : 'transparent',
                display: 'flex', alignItems: 'baseline', gap: 10,
                borderBottom: '1px solid #222',
              }}
            >
              <span style={{ fontFamily: 'monospace', fontSize: 13, minWidth: 160 }}>
                <span style={{ color: '#666' }}>{item.runnable.group}/</span>
                <span style={{ color: '#4fc1ff' }}>{item.runnable.name}</span>
              </span>
              <span style={{ color: '#777', fontSize: 11 }}>{item.host}</span>
              {item.runnable.description && (
                <span style={{ color: '#999', fontSize: 12, marginLeft: 'auto' }}>
                  {item.runnable.description}
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
        <span style={{ color: '#555', fontFamily: 'monospace', fontSize: 14, paddingBottom: 2 }}>❯</span>
        <textarea
          ref={inputRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder="Type / for runnables or ask the LLM..."
          style={{
            flex: 1, background: 'transparent', border: 'none', outline: 'none',
            color: '#d4d4d4', fontFamily: 'monospace', fontSize: 14, resize: 'none',
            lineHeight: 1.5, minHeight: '42px', maxHeight: '240px', overflowY: 'auto',
          }}
        />
        <Button
          type="text"
          icon={<SendOutlined />}
          onClick={submit}
          style={{ color: value.trim() ? '#1677ff' : '#444', padding: 4 }}
        />
      </div>
    </div>
  )
}
