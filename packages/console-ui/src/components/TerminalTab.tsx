/**
 * TerminalTab.tsx — xterm.js-based SSH terminal tab.
 *
 * Each instance manages one terminal session (one plink process on the Python side).
 * Data flows:
 *   keystrokes → bridge.terminal_input(sessionId, base64)
 *   runspec:terminal_data → xterm.write(decoded)
 *   runspec:terminal_closed → display disconnected message
 */

import { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { bridge } from '../bridge'

interface TerminalTabProps {
  sessionId: string
  host: string
}

// UTF-8 string → base64 (handles multi-byte chars correctly)
function toBase64(str: string): string {
  const bytes = new TextEncoder().encode(str)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i])
  }
  return btoa(binary)
}

// base64 → Uint8Array (binary-safe; avoids btoa/atob UTF-8 issues)
function fromBase64(b64: string): Uint8Array {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes
}

export function TerminalTab({ sessionId, host }: TerminalTabProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"Cascadia Code", "Consolas", "Courier New", monospace',
      letterSpacing: 0,
      lineHeight: 1.2,
      scrollback: 5000,
      theme: {
        background: '#141414',
        foreground: '#d4d4d4',
        cursor: '#aeafad',
        selectionBackground: 'rgba(255,255,255,0.15)',
        black:         '#000000', brightBlack:   '#666666',
        red:           '#cd3131', brightRed:     '#f14c4c',
        green:         '#0dbc79', brightGreen:   '#23d18b',
        yellow:        '#e5e510', brightYellow:  '#f5f543',
        blue:          '#2472c8', brightBlue:    '#3b8eea',
        magenta:       '#bc3fbc', brightMagenta: '#d670d6',
        cyan:          '#11a8cd', brightCyan:    '#29b8db',
        white:         '#e5e5e5', brightWhite:   '#e5e5e5',
      },
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(containerRef.current)

    // Initial fit — defer one tick so the container has its final dimensions
    requestAnimationFrame(() => {
      fitAddon.fit()
      bridge.resize_terminal(sessionId, term.cols, term.rows)
    })

    // ── keyboard → SSH stdin ──────────────────────────────────────────────
    const onData = term.onData((data: string) => {
      bridge.terminal_input(sessionId, toBase64(data))
    })

    // ── SSH stdout → xterm ────────────────────────────────────────────────
    const handleTermData = (e: Event) => {
      const { id, data } = (e as CustomEvent).detail
      if (id !== sessionId) return
      term.write(fromBase64(data))
    }

    const handleTermClosed = (e: Event) => {
      const { id } = (e as CustomEvent).detail
      if (id !== sessionId) return
      term.write('\r\n\x1b[2m[Connection closed]\x1b[0m\r\n')
    }

    window.addEventListener('runspec:terminal_data', handleTermData)
    window.addEventListener('runspec:terminal_closed', handleTermClosed)

    // ── resize observer ───────────────────────────────────────────────────
    const observer = new ResizeObserver(() => {
      fitAddon.fit()
      bridge.resize_terminal(sessionId, term.cols, term.rows)
    })
    observer.observe(containerRef.current)

    return () => {
      onData.dispose()
      window.removeEventListener('runspec:terminal_data', handleTermData)
      window.removeEventListener('runspec:terminal_closed', handleTermClosed)
      observer.disconnect()
      term.dispose()
    }
  }, [sessionId])

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        background: '#141414',
        // Ensure the xterm canvas fills the container without scrollbars
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Subtle host label bar */}
      <div style={{
        padding: '3px 12px',
        fontSize: 11,
        color: '#555',
        borderBottom: '1px solid #1f1f1f',
        fontFamily: 'monospace',
        flexShrink: 0,
      }}>
        {host}
      </div>
      <div
        ref={containerRef}
        style={{ flex: 1, minHeight: 0, padding: '6px 4px 4px 4px' }}
      />
    </div>
  )
}
