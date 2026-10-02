/**
 * Forms is the one home for runbook + saved-run management (they left the
 * Settings drawer): "New …" buttons at the top, and an edit icon on each card
 * that opens the editor without launching the card's run dialog.
 */
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { FormsView } from './FormsView'
import { FeaturesContext } from '../features'
import type { Runbook, SavedRun } from '../bridge'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn().mockResolvedValue(undefined) }),
}))

const savedRun = { id: 'restart-web', title: 'Restart web', runnable: 'svc', host: 'web-01', params: [] } as unknown as SavedRun
const runbook = { id: 'disk-full', title: 'Disk full', args: [], tags: [] } as unknown as Runbook

beforeEach(() => window.localStorage.clear())

function expandAll() {
  fireEvent.click(screen.getByRole('button', { name: /Expand all/ }))
}

function renderForms(ai = true) {
  const handlers = {
    onOpenSavedRun: vi.fn(), onOpenRunbook: vi.fn(),
    onEditSavedRun: vi.fn(), onEditRunbook: vi.fn(),
  }
  render(
    <FeaturesContext.Provider value={{ ai }}>
      <FormsView
        runnables={[]} hosts={[]} selectedHost="" activeScope={[]} onRunRunnable={vi.fn()}
        savedRuns={[savedRun]} runbooks={[runbook]} {...handlers}
      />
    </FeaturesContext.Provider>,
  )
  return handlers
}

describe('Forms → create / edit saved runs and runbooks', () => {
  it('New buttons open an empty editor', () => {
    const h = renderForms()
    fireEvent.click(screen.getByRole('button', { name: /New saved run/ }))
    fireEvent.click(screen.getByRole('button', { name: /New runbook/ }))
    expect(h.onEditSavedRun).toHaveBeenCalledWith('')
    expect(h.onEditRunbook).toHaveBeenCalledWith('')
  })

  it('a card edit icon opens that item without launching it', () => {
    const h = renderForms()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Edit saved run' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit runbook' }))
    expect(h.onEditSavedRun).toHaveBeenCalledWith('restart-web')
    expect(h.onEditRunbook).toHaveBeenCalledWith('disk-full')
    expect(h.onOpenSavedRun).not.toHaveBeenCalled()
    expect(h.onOpenRunbook).not.toHaveBeenCalled()
  })

  it('hides runbook authoring when AI is off', () => {
    renderForms(false)
    expandAll()
    expect(screen.queryByRole('button', { name: /New runbook/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit runbook' })).toBeNull()
    expect(screen.getByRole('button', { name: /New saved run/ })).toBeTruthy()
  })
})

describe('Forms → collapsible sections', () => {
  const runnable = { name: 'svc', group: 'ops', host: 'web-01', description: 'service' } as never

  function renderSections() {
    render(
      <FeaturesContext.Provider value={{ ai: true }}>
        <FormsView
          runnables={[runnable]} hosts={[]} selectedHost="" activeScope={[]} onRunRunnable={vi.fn()}
          savedRuns={[savedRun, { ...savedRun, id: 'other', title: 'Other thing' } as SavedRun]}
          runbooks={[runbook]}
          onOpenSavedRun={vi.fn()} onOpenRunbook={vi.fn()}
        />
      </FeaturesContext.Provider>,
    )
  }
  const header = (name: RegExp) => screen.getByRole('button', { name })

  it('saved runs and runbooks start collapsed with a count; runnable groups start open', () => {
    renderSections()
    expect(header(/Saved runs/).getAttribute('aria-expanded')).toBe('false')
    expect(header(/Saved runs/).textContent).toContain('2')
    expect(header(/Runbooks/).getAttribute('aria-expanded')).toBe('false')
    expect(header(/ ops/).getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByText('Restart web')).toBeNull()
  })

  it('the count follows the search while collapsed', () => {
    renderSections()
    fireEvent.change(screen.getByPlaceholderText(/Search/), { target: { value: 'restart' } })
    expect(header(/Saved runs/).textContent).toContain('1')
  })

  it('collapse all / expand all toggles every section', () => {
    renderSections()
    fireEvent.click(screen.getByRole('button', { name: /Collapse all/ }))
    expect(header(/ ops/).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: /Expand all/ }))
    expect(header(/Saved runs/).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('Restart web')).toBeTruthy()
  })

  it('a header click toggles just that section', () => {
    renderSections()
    fireEvent.click(header(/Runbooks/))
    expect(header(/Runbooks/).getAttribute('aria-expanded')).toBe('true')
    expect(header(/Saved runs/).getAttribute('aria-expanded')).toBe('false')
  })
})
