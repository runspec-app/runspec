import React, { useCallback, useEffect, useRef } from 'react'
import { bridge } from '../bridge'

const H = 6    // handle thickness px
const MIN_W = 1024
const MIN_H = 600

type Dir = 'n' | 's' | 'e' | 'w' | 'nw' | 'ne' | 'sw' | 'se'

const HANDLES: { dir: Dir; cursor: string; style: React.CSSProperties }[] = [
  { dir: 'n',  cursor: 'n-resize',  style: { top: 0,    left: H,  right: H,  height: H } },
  { dir: 's',  cursor: 's-resize',  style: { bottom: 0, left: H,  right: H,  height: H } },
  { dir: 'e',  cursor: 'e-resize',  style: { right: 0,  top: H,   bottom: H, width: H  } },
  { dir: 'w',  cursor: 'w-resize',  style: { left: 0,   top: H,   bottom: H, width: H  } },
  { dir: 'nw', cursor: 'nw-resize', style: { top: 0,    left: 0,  width: H,  height: H } },
  { dir: 'ne', cursor: 'ne-resize', style: { top: 0,    right: 0, width: H,  height: H } },
  { dir: 'sw', cursor: 'sw-resize', style: { bottom: 0, left: 0,  width: H,  height: H } },
  { dir: 'se', cursor: 'se-resize', style: { bottom: 0, right: 0, width: H,  height: H } },
]

interface DragState {
  dir: Dir
  startX: number; startY: number
  initX: number;  initY: number
  initW: number;  initH: number
  raf: number | null
}

export function ResizeHandles() {
  const drag = useRef<DragState | null>(null)

  const onMouseDown = useCallback((dir: Dir, e: React.MouseEvent) => {
    e.preventDefault()
    drag.current = {
      dir,
      startX: e.screenX, startY: e.screenY,
      initX: window.screenX, initY: window.screenY,
      initW: window.outerWidth, initH: window.outerHeight,
      raf: null,
    }
  }, [])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = drag.current
      if (!d) return
      if (d.raf !== null) return
      d.raf = requestAnimationFrame(() => {
        const d = drag.current
        if (!d) return
        d.raf = null

        const dx = e.screenX - d.startX
        const dy = e.screenY - d.startY
        const { dir } = d
        let newW = d.initW, newH = d.initH, newX = d.initX, newY = d.initY

        if (dir.includes('e')) newW = d.initW + dx
        if (dir.includes('w')) { newW = d.initW - dx; newX = d.initX + dx }
        if (dir.includes('s')) newH = d.initH + dy
        if (dir.includes('n')) { newH = d.initH - dy; newY = d.initY + dy }

        if (newW < MIN_W) { if (dir.includes('w')) newX = d.initX + d.initW - MIN_W; newW = MIN_W }
        if (newH < MIN_H) { if (dir.includes('n')) newY = d.initY + d.initH - MIN_H; newH = MIN_H }

        bridge.resize_window(Math.round(newW), Math.round(newH))
        if (newX !== d.initX || newY !== d.initY) bridge.move_window(Math.round(newX), Math.round(newY))
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

  return (
    <>
      {HANDLES.map(({ dir, cursor, style }) => (
        <div
          key={dir}
          onMouseDown={e => onMouseDown(dir, e)}
          style={{
            position: 'fixed',
            cursor,
            zIndex: 10000,
            userSelect: 'none',
            WebkitAppRegion: 'no-drag',
            ...style,
          } as React.CSSProperties}
        />
      ))}
    </>
  )
}
