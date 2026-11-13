import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Modal, Tooltip } from 'antd'

import { antTone, EmptyState, IconAction, OnceHint, PageHeader, PageToolbar, RowActions, StatusTag, hintSeen, statusTone, type StatusTone } from './kit'
import { contrastRatio, readableAccent, surfaces, DEFAULT_ACCENT, ThemeContext } from '../ThemeContext'
import { CopyOutlined, DeleteOutlined } from '@ant-design/icons'

// Composite a translucent rgba() over an opaque hex: the tag's real background.
function over(rgba: string, base: string): string {
  if (rgba === 'transparent') return base
  const m = rgba.match(/rgba\((\d+), (\d+), (\d+), ([\d.]+)\)/)!
  const [r, g, b, a] = [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])]
  const n = parseInt(base.slice(1), 16)
  const mix = (fg: number, bg: number) => Math.round(fg * a + bg * (1 - a))
  const out = [mix(r, (n >> 16) & 255), mix(g, (n >> 8) & 255), mix(b, n & 255)]
  return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('')
}

const TONES: StatusTone[] = ['ok', 'running', 'waiting', 'attention', 'failed', 'info', 'neutral']

describe('statusTone', () => {
  for (const isDark of [true, false]) {
    for (const brand of [undefined, '#0b7a6e', '#9b031a', '#2f54eb']) {
      it(`every tone reads at 4.5:1 on every surface (${isDark ? 'dark' : 'light'}, ${brand ?? 'default'})`, () => {
        const accent = brand ? readableAccent(brand, isDark) : (isDark ? DEFAULT_ACCENT.dark : DEFAULT_ACCENT.light)
        const sf = surfaces(isDark)
        for (const tone of TONES) {
          const c = statusTone(tone, isDark, accent)
          for (const base of [sf.sunken, sf.base, sf.raised]) {
            expect(contrastRatio(c.color, over(c.bg, base)), `${tone} on ${base}`).toBeGreaterThanOrEqual(4.5)
          }
        }
      })
    }
  }

  it('failed never shares the accent when the brand is red', () => {
    const accent = readableAccent('#9b031a', true)
    expect(statusTone('failed', true, accent).color).not.toBe(statusTone('running', true, accent).color)
  })
})

describe('StatusTag', () => {
  it('renders its tone and label', () => {
    render(<StatusTag tone="failed" testId="t">exit 2</StatusTag>)
    const t = screen.getByTestId('t')
    expect(t.textContent).toBe('exit 2')
    expect(t.getAttribute('data-tone')).toBe('failed')
  })
})

describe('antTone and clickable StatusTag', () => {
  it('maps Ant preset colours to tones', () => {
    expect(antTone('green')).toBe('ok')
    expect(antTone('processing')).toBe('running')
    expect(antTone('orange')).toBe('attention')
    expect(antTone('red')).toBe('failed')
    expect(antTone('geekblue')).toBe('info')
    expect(antTone(undefined)).toBe('neutral')
  })

  it('a clickable tag is a keyboard button', () => {
    const go = vi.fn()
    render(<StatusTag tone="attention" onClick={go}>3 updates</StatusTag>)
    const b = screen.getByRole('button', { name: '3 updates' })
    fireEvent.keyDown(b, { key: 'Enter' })
    fireEvent.click(b)
    expect(go).toHaveBeenCalledTimes(2)
  })
})

describe('StatusTag inside a Tooltip', () => {
  it('shows the outer tooltip on hover', async () => {
    render(<Tooltip title="autonomy: confirm"><StatusTag tone="info">confirm</StatusTag></Tooltip>)
    fireEvent.mouseEnter(screen.getByText('confirm'))
    expect(await screen.findByText('autonomy: confirm')).toBeTruthy()
  })
})

describe('IconAction', () => {
  it('has an accessible name and calls onClick', () => {
    const onClick = vi.fn()
    render(<IconAction label="Copy log" icon={<CopyOutlined />} onClick={onClick} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy log' }))
    expect(onClick).toHaveBeenCalled()
  })
})

describe('RowActions', () => {
  it('shows primary and secondaries, keeps "more" behind a menu, and confirms a risky one', async () => {
    const rerun = vi.fn(), edit = vi.fn(), del = vi.fn()
    render(
      <RowActions
        primary={{ key: 'rerun', label: 'Rerun', onClick: rerun }}
        secondary={[{ key: 'edit', label: 'Edit', onClick: edit }]}
        more={[{ key: 'del', label: 'Delete', icon: <DeleteOutlined />, danger: true, onClick: del, confirm: 'Delete this run?' }]}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Rerun' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(rerun).toHaveBeenCalled()
    expect(edit).toHaveBeenCalled()
    expect(screen.queryByText('Delete')).toBeNull()
    const confirm = vi.spyOn(Modal, 'confirm').mockImplementation(() => ({ destroy: () => {}, update: () => {} }) as ReturnType<typeof Modal.confirm>)
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }))
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    const opts = confirm.mock.calls[0][0]
    expect(opts.title).toBe('Delete this run?')
    expect(opts.okText).toBe('Delete')
    expect(del).not.toHaveBeenCalled()
    ;(opts.onOk as () => void)()
    expect(del).toHaveBeenCalled()
    confirm.mockRestore()
  })
})

describe('OnceHint', () => {
  beforeEach(() => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v) },
      removeItem: (k: string) => { store.delete(k) },
      clear: () => store.clear(),
    })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('shows until dismissed, then stays gone', () => {
    const { unmount } = render(<OnceHint id="x">Click a run</OnceHint>)
    expect(screen.getByText('Click a run')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss this tip' }))
    expect(screen.queryByText('Click a run')).toBeNull()
    expect(hintSeen('x')).toBe(true)
    unmount()
    render(<OnceHint id="x">Click a run</OnceHint>)
    expect(screen.queryByText('Click a run')).toBeNull()
  })
})

describe('EmptyState, PageToolbar, PageHeader', () => {
  it('EmptyState shows one line and its action', () => {
    const go = vi.fn()
    render(<ThemeContext.Provider value={false}><EmptyState message="No runs in 7 days" action={{ label: 'Show 30 days', onClick: go }} /></ThemeContext.Provider>)
    expect(screen.getByText('No runs in 7 days')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show 30 days' }))
    expect(go).toHaveBeenCalled()
  })

  it('PageToolbar and PageHeader place their parts', () => {
    render(<>
      <PageHeader title="History" description="Every run" actions={<button>Export</button>} />
      <PageToolbar actions={<button>Refresh</button>}><input aria-label="search" /></PageToolbar>
    </>)
    expect(screen.getByRole('heading', { name: 'History' })).toBeTruthy()
    expect(screen.getByText('Every run')).toBeTruthy()
    expect(screen.getByTestId('page-toolbar').textContent).toContain('Refresh')
  })
})
