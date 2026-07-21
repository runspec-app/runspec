import { describe, it, expect } from 'vitest'
import { filterSavedRuns } from './FormsView'
import type { SavedRun } from '../bridge'

const savedRun = (over: Partial<SavedRun>): SavedRun => ({
  id: 'x', title: '', description: '', runnable: '', command: [],
  host: '', group: '', hosts: [], selected_hosts: [],
  args: {}, credential_ids: [], params: [],
  ...over,
})

const RUNS = [
  savedRun({ id: 'restart-web', title: 'Restart web tier', runnable: 'service-restart', group: 'Production', target: 'group Production' }),
  savedRun({ id: 'disk-check', description: 'Free space report', runnable: 'df', host: 'web01', target: 'web01' }),
]

describe('filterSavedRuns (Forms lane)', () => {
  it('returns everything for an empty search', () => {
    expect(filterSavedRuns(RUNS, '')).toEqual(RUNS)
  })

  it('matches on id, title, description, runnable, and target (case-insensitive)', () => {
    expect(filterSavedRuns(RUNS, 'RESTART-WEB').map(s => s.id)).toEqual(['restart-web'])
    expect(filterSavedRuns(RUNS, 'web tier').map(s => s.id)).toEqual(['restart-web'])
    expect(filterSavedRuns(RUNS, 'free space').map(s => s.id)).toEqual(['disk-check'])
    expect(filterSavedRuns(RUNS, 'df').map(s => s.id)).toEqual(['disk-check'])
    expect(filterSavedRuns(RUNS, 'production').map(s => s.id)).toEqual(['restart-web'])
  })

  it('returns nothing when nothing matches', () => {
    expect(filterSavedRuns(RUNS, 'zzz')).toEqual([])
  })
})
