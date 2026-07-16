import { describe, it, expect } from 'vitest'
import { normalizeHostForm } from './SettingsDrawer'

// Regression: removing a room (or clearing profiles) through the host editor
// must actually remove it. The editor merges `{ ...h, ...normalizeHostForm(v) }`,
// so a *cleared* field has to be `undefined` (which overwrites h) — a deleted /
// absent key falls back to h's old value, and the room "comes back" after save.
// The bug the user hit: room config returned after restart; only a manual TOML
// edit removed it.

// Mirror the edit path: the previously-saved host merged with the normalized form.
function applyEdit(h: Record<string, unknown>, values: Record<string, unknown>) {
  const patch = normalizeHostForm(values as never) as unknown as Record<string, unknown>
  return { ...h, ...patch }
}

describe('normalizeHostForm — room removal', () => {
  it('clearing the room port removes every room field on the merged host', () => {
    const h = {
      name: 'web01',
      hostname: 'web01',
      room_port: 8765,
      room: 'ops',
      room_queue: 'it-support',
      room_user: 'alice',
      room_via: 'exec',
      room_exec_command: 'socat - TCP:127.0.0.1:8765',
      room_catalog: ['reset-password'],
    }
    // Operator clears the Room port field (empty string from <Input type=number>).
    const merged = applyEdit(h, { name: 'web01', hostname: 'web01', room_port: '' })

    for (const key of [
      'room_port',
      'room',
      'room_queue',
      'room_user',
      'room_via',
      'room_exec_command',
      'room_catalog',
    ]) {
      // The old value must NOT survive the merge — undefined (dropped on
      // serialize), never the previously-saved value.
      expect(merged[key], `${key} should be cleared`).toBeUndefined()
    }
  })

  it('keeps room fields when the port is still set', () => {
    const merged = applyEdit(
      { name: 'web01', hostname: 'web01' },
      { name: 'web01', hostname: 'web01', room_port: 8765, room_queue: 'it-support' },
    )
    expect(merged.room_port).toBe(8765)
    expect(merged.room_queue).toBe('it-support')
  })
})

describe('normalizeHostForm — profile clearing', () => {
  it('clearing all profiles removes them from the merged host', () => {
    const h = { name: 'web01', hostname: 'web01', profiles: ['devops', 'app-support'] }
    // Operator removes every tag from the multi-select → [].
    const merged = applyEdit(h, { name: 'web01', hostname: 'web01', profiles: [] })
    expect(merged.profiles).toBeUndefined()
  })

  it('preserves profiles that are still selected', () => {
    const merged = applyEdit(
      { name: 'web01', hostname: 'web01', profiles: ['devops'] },
      { name: 'web01', hostname: 'web01', profiles: ['devops', 'app-support'] },
    )
    expect(merged.profiles).toEqual(['devops', 'app-support'])
  })
})
