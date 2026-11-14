import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Modal, Tooltip } from 'antd'

import { antTone, EmptyState, IconAction, pressable, SectionSwitch, withLabel, OnceHint, PageHeader, PageToolbar, RowActions, StatusTag, hintSeen, statusTone, type StatusTone } from './kit'
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

  it('running, failed, waiting and attention stay apart under any brand', () => {
    const hue = (hex: string) => {
      const n = parseInt(hex.slice(1), 16)
      const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => v / 255)
      const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
      if (d === 0) return 0
      const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4
      return (h * 60 + 360) % 360
    }
    const gap = (a: number, b: number) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b))
    for (const isDark of [true, false]) {
      for (const brand of ['#9b031a', '#e8590c', '#0b7a6e', '#2f54eb']) {
        const accent = readableAccent(brand, isDark)
        const c = (t: StatusTone) => statusTone(t, isDark, accent).color
        // Running is never the brand: it has its own fixed colour.
        expect(c('running')).toBe(statusTone('running', isDark, '#123456').color)
        expect(gap(hue(c('running')), hue(c('failed'))), `${brand} running vs failed`).toBeGreaterThan(60)
        expect(gap(hue(c('running')), hue(c('attention')))).toBeGreaterThan(60)
      }
    }
  })

  it('waiting and attention carry different icons', () => {
    render(<><StatusTag tone="waiting" testId="w">being handled</StatusTag><StatusTag tone="attention" testId="a">stale</StatusTag></>)
    expect(screen.getByTestId('w').querySelector('.anticon-pause-circle')).toBeTruthy()
    expect(screen.getByTestId('a').querySelector('.anticon-exclamation-circle')).toBeTruthy()
  })

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

describe('pressable', () => {
  it('makes a div a keyboard button that ignores keys from nested controls', () => {
    const go = vi.fn()
    render(<div {...pressable(go, { expanded: false })} data-testid="row">row <input aria-label="inner" /></div>)
    const row = screen.getByRole('button', { name: /row/ })
    expect(row.getAttribute('tabindex')).toBe('0')
    expect(row.getAttribute('aria-expanded')).toBe('false')
    fireEvent.keyDown(row, { key: 'Enter' })
    fireEvent.keyDown(row, { key: ' ' })
    fireEvent.keyDown(screen.getByLabelText('inner'), { key: 'Enter' })
    expect(go).toHaveBeenCalledTimes(2)
  })
})

describe('PageHeader colour', () => {
  it('uses the theme text colour, not an inherited black', () => {
    render(<PageHeader title="Logs" />)
    expect(screen.getByRole('heading', { name: 'Logs' }).style.color).not.toBe('inherit')
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
        more={[{ key: 'del', label: 'Delete', icon: <DeleteOutlined />, danger: true, onClick: del, confirm: { title: 'Delete this run?', what: '/backup on prod-1' } }]}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Rerun' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(rerun).toHaveBeenCalled()
    expect(edit).toHaveBeenCalled()
    expect(screen.queryByText('Delete')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }))
    fireEvent.click(await screen.findByText('Delete'))
    // The shared dialog asks (antd's static Modal.confirm never rendered).
    expect(await screen.findByText('Delete this run?')).toBeTruthy()
    // It names the object, not the button's own label.
    expect(screen.getByTestId('risk-action').textContent).toBe('/backup on prod-1')
    expect(del).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('risk-ok'))
    await waitFor(() => expect(del).toHaveBeenCalled())
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

describe('withLabel', () => {
  it('names an unnamed control after its row, and leaves a named one alone', () => {
    const Ctl = (p: Record<string, unknown>) => <input data-testid="a" aria-label={p['aria-label'] as string} />
    render(<>{withLabel(<Ctl />, 'Enable sounds')}{withLabel(<input data-testid="b" aria-label="Own" />, 'Row')}</>)
    expect(screen.getByTestId('a').getAttribute('aria-label')).toBe('Enable sounds')
    expect(screen.getByTestId('b').getAttribute('aria-label')).toBe('Own')
  })
})

describe('SectionSwitch', () => {
  it('shows the sections, a count where something waits, and switches', () => {
    const go = vi.fn()
    render(<SectionSwitch label="Automation" value="schedules" onChange={go} sections={[
      { key: 'schedules', label: 'Schedules' },
      { key: 'triggers', label: 'Triggers', count: 2, countTone: 'waiting' },
      { key: 'rota', label: 'Working hours' },
    ]} />)
    expect(screen.getByText('2').getAttribute('data-tone')).toBe('waiting')
    fireEvent.click(screen.getByText('Working hours'))
    expect(go).toHaveBeenCalledWith('rota')
  })
})
