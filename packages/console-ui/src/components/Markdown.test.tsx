// Reply tables scroll sideways rather than splitting hosts and IPs mid-word.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Markdown, keepOnOneLine } from './Markdown'

describe('Markdown tables', () => {
  it('keeps short values and single tokens on one line, lets sentences wrap', () => {
    expect(keepOnOneLine('web-01')).toBe(true)
    expect(keepOnOneLine('OpenSSL 3.0.13 30 Jan 2024')).toBe(true)
    expect(keepOnOneLine('https://runbooks.example.com/a/very/long/path/that/has/no/spaces/at/all')).toBe(true)
    expect(keepOnOneLine('Disk usage on the root volume has been above ninety percent for an hour')).toBe(false)
  })

  it('renders cells that do not break words', () => {
    render(<Markdown>{'| host | ip |\n|---|---|\n| web-01 | 10.20.0.10 |'}</Markdown>)
    const cell = screen.getByText('web-01')
    expect(cell.style.whiteSpace).toBe('nowrap')
    expect((cell.closest('table') as HTMLElement).style.wordBreak).toBe('normal')
  })
})
