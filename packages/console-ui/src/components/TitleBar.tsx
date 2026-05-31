import React, { useCallback, useEffect, useRef, useState } from 'react'
import { MinusOutlined, BorderOutlined, CloseOutlined, BlockOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'

const BAR_BG = '#1677ff'   // Ant Design blue-6
const BAR_FG = '#ffffff'
const BAR_HEIGHT = 36
const HOVER_BG = 'rgba(255,255,255,0.12)'
const CLOSE_HOVER_BG = '#e81123'

interface DragState {
  startX: number; startY: number
  initX: number;  initY: number
  raf: number | null
}

interface TitleBarProps {
  title?: string
}

export function TitleBar({ title = 'runspec console' }: TitleBarProps) {
  const drag = useRef<DragState | null>(null)
  const [maximized, setMaximized] = useState(false)

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    // Only respond to primary button on the drag region itself,
    // not on the window-control buttons (they stop propagation).
    if (e.button !== 0) return
    drag.current = {
      startX: e.screenX, startY: e.screenY,
      initX: window.screenX, initY: window.screenY,
      raf: null,
    }
  }, [])

  const onDoubleClick = useCallback(() => {
    bridge.toggle_maximize_window()
    setMaximized(m => !m)
  }, [])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = drag.current
      if (!d) return
      if (d.raf !== null) return
      d.raf = requestAnimationFrame(() => {
        const d2 = drag.current
        if (!d2) return
        d2.raf = null
        const dx = e.screenX - d2.startX
        const dy = e.screenY - d2.startY
        bridge.move_window(Math.round(d2.initX + dx), Math.round(d2.initY + dy))
      })
    }
    const onUp = () => {
      if (drag.current?.raf != null) cancelAnimationFrame(drag.current.raf)
      drag.current = null
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
  }, [])

  const handleMinimize = (e: React.MouseEvent) => {
    e.stopPropagation()
    bridge.minimize_window()
  }
  const handleMaximize = (e: React.MouseEvent) => {
    e.stopPropagation()
    bridge.toggle_maximize_window()
    setMaximized(m => !m)
  }
  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation()
    bridge.close_window()
  }
  const stopDrag = (e: React.MouseEvent) => { e.stopPropagation() }

  return (
    <div
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      style={{
        height: BAR_HEIGHT,
        flexShrink: 0,
        background: BAR_BG,
        color: BAR_FG,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        userSelect: 'none',
        WebkitUserSelect: 'none',
        // Fallback drag region — pywebview/WebView2 honours this on Windows.
        // Buttons set WebkitAppRegion: 'no-drag' so clicks register.
        ...({ WebkitAppRegion: 'drag' } as React.CSSProperties),
      }}
    >
      <div
        style={{
          paddingLeft: 14,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontSize: 12,
          fontFamily: 'monospace',
          letterSpacing: '0.02em',
        }}
      >
        {title}
      </div>
      <div
        onMouseDown={stopDrag}
        style={{
          display: 'flex',
          height: '100%',
          ...({ WebkitAppRegion: 'no-drag' } as React.CSSProperties),
        }}
      >
        <WindowButton onClick={handleMinimize} title="Minimize">
          <MinusOutlined style={{ fontSize: 12 }} />
        </WindowButton>
        <WindowButton onClick={handleMaximize} title={maximized ? 'Restore' : 'Maximize'}>
          {maximized
            ? <BlockOutlined style={{ fontSize: 11 }} />
            : <BorderOutlined style={{ fontSize: 11 }} />}
        </WindowButton>
        <WindowButton onClick={handleClose} title="Close" closeButton>
          <CloseOutlined style={{ fontSize: 12 }} />
        </WindowButton>
      </div>
    </div>
  )
}

function WindowButton({
  onClick, title, closeButton, children,
}: {
  onClick: (e: React.MouseEvent) => void
  title: string
  closeButton?: boolean
  children: React.ReactNode
}) {
  const [hover, setHover] = useState(false)
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={title}
      style={{
        width: 46,
        height: '100%',
        background: hover ? (closeButton ? CLOSE_HOVER_BG : HOVER_BG) : 'transparent',
        color: BAR_FG,
        border: 'none',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 0,
        transition: 'background 0.12s',
        ...({ WebkitAppRegion: 'no-drag' } as React.CSSProperties),
      }}
    >
      {children}
    </button>
  )
}
