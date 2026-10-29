import { describe, expect, it } from 'vitest'
import {
  detectDates, templatizeDates, widenToRotations, placeholderNames, missingParams,
  rowsFromParams, remoteTarget, paramsFromRows, slugify, defaultValues,
} from './savedTransfers'

describe('date detection', () => {
  it('finds dates in the separators file names use', () => {
    expect(detectDates(['/l/app-2026-10-29.log', '/l/x_20261028.gz', '/l/2026.10.27/a', '/l/v12026-10-29'])).toEqual([
      '2026-10-29', '20261028', '2026.10.27',
    ])
    expect(detectDates(['/l/app-2026-13-01.log', '/l/build-12345678'])).toEqual([])
  })

  it('turns them into a date placeholder in the same format', () => {
    expect(templatizeDates(['/l/app-2026-10-29.log', '/l/x_20261028.gz'], 'day')).toEqual([
      '/l/app-{{day:%Y-%m-%d}}.log', '/l/x_{{day:%Y%m%d}}.gz',
    ])
  })
})

describe('widenToRotations', () => {
  it('adds a wildcard before the extension', () => {
    expect(widenToRotations('/l/app-2026-10-29.log')).toBe('/l/app-2026-10-29*.log')
    expect(widenToRotations('/l/messages')).toBe('/l/messages*')
    expect(widenToRotations('C:\\logs\\app.log')).toBe('C:\\logs\\app*.log')
    expect(widenToRotations('/l/*.log')).toBe('/l/*.log')
  })
})

describe('params', () => {
  it('lists placeholders and the ones not declared yet', () => {
    expect(placeholderNames(['/l/{{svc}}/{{ day:%Y }}', '{{host}}'])).toEqual(['svc', 'day', 'host'])
    expect(missingParams(['/l/{{svc}}/{{day:%Y}}-{{today}}'], [{ name: 'day', default: '', description: '' }])).toEqual(['svc'])
  })

  it('round-trips a params table, keeping keys the editor does not show', () => {
    const raw = { day: { default: 'yesterday', description: 'which day' }, env: { options: ['dev', 'prod'] } }
    const rows = rowsFromParams(raw)
    expect(rows).toEqual([
      { name: 'day', default: 'yesterday', description: 'which day' },
      { name: 'env', default: '', description: '', extra: { options: ['dev', 'prod'] } },
    ])
    expect(paramsFromRows(rows)).toEqual(raw)
    expect(paramsFromRows([{ name: ' ', default: 'x', description: '' }])).toEqual({})
  })

  it('seeds run values from defaults', () => {
    expect(defaultValues({ id: 'a', paths: [], params: [
      { name: 'day', type: 'str', required: false, default: 'today' },
      { name: 'svc', type: 'str', required: true },
    ] })).toEqual({ day: 'today' })
  })
})

it('slugifies a title into an id', () => {
  expect(slugify('App logs — for a Day!')).toBe('app-logs-for-a-day')
})

describe('remoteTarget', () => {
  const hosts = [
    { name: 'local' },
    { name: 'web-01', group: 'web' },
    { name: 'web-02', group: 'web' },
    { name: 'db-01', group: 'db' },
    { name: 'solo' },
  ]
  it('maps one host to to_host', () => {
    expect(remoteTarget(['db-01'], hosts)).toEqual({ to_host: 'db-01' })
  })
  it('maps a whole group to to_group', () => {
    expect(remoteTarget(['web-02', 'web-01'], hosts)).toEqual({ to_group: 'web' })
  })
  it('refuses a hand-picked subset or a mix', () => {
    expect(remoteTarget([], hosts)).toBeNull()
    expect(remoteTarget(['web-01', 'db-01'], hosts)).toBeNull()
    expect(remoteTarget(['solo', 'web-01'], hosts)).toBeNull()
  })
})
