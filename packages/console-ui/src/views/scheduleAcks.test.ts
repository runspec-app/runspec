import { describe, it, expect } from 'vitest'
import { SCHEDULE_ACKS_KEY, loadAcks, saveAcks, setAck } from './scheduleAcks'

function store(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init))
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v) } }
}

describe('schedule acknowledgements', () => {
  it('saves and loads which failed run each schedule has acknowledged', () => {
    const s = store()
    saveAcks(setAck({}, 'rs-1', 'card-9'), s)
    expect(loadAcks(s)).toEqual({ 'rs-1': 'card-9' })
  })

  it('undo removes the schedule', () => {
    expect(setAck({ 'rs-1': 'a', 'rs-2': 'b' }, 'rs-1', null)).toEqual({ 'rs-2': 'b' })
  })

  it('reads nothing from missing, broken or odd storage', () => {
    expect(loadAcks(null)).toEqual({})
    expect(loadAcks(store({ [SCHEDULE_ACKS_KEY]: 'not json' }))).toEqual({})
    expect(loadAcks(store({ [SCHEDULE_ACKS_KEY]: '[1]' }))).toEqual({})
    expect(loadAcks(store({ [SCHEDULE_ACKS_KEY]: '{"a":"x","b":3}' }))).toEqual({ a: 'x' })
  })
})
