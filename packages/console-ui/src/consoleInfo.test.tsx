import { describe, it, expect } from 'vitest'
import { approvalConsequence, type ConsoleInfo } from './consoleInfo'

const info: ConsoleInfo = {
  describe: n => (n === 'restart-service' ? 'Restart a systemd service.' : undefined),
  groupSize: g => (g === 'prod-web' ? 12 : 0),
}

describe('approvalConsequence', () => {
  it('says what the call does, marks root and disruptive verbs, and counts a group fan-out', () => {
    const c = approvalConsequence({ name: 'restart-service', runsAs: 'root', host: 'prod-web', info })
    expect(c.description).toBe('Restart a systemd service.')
    expect(c.fanout).toBe(12)
    expect(c.markers.map(m => m.text)).toEqual(['as root', 'changes the host'])
  })

  it('a read-only call on one host carries no markers', () => {
    const c = approvalConsequence({ name: 'ping-host', host: 'web-01', info })
    expect(c).toEqual({ description: undefined, fanout: 0, markers: [] })
  })

  it('reads the subcommand too, and matches verbs as words, not inside names', () => {
    expect(approvalConsequence({ name: 'cron', command: 'remove', info }).markers.map(m => m.text)).toEqual(['changes the host'])
    expect(approvalConsequence({ name: 'restore-check', info }).markers).toEqual([])
    expect(approvalConsequence({ name: 'list-installed', info }).markers).toEqual([])
  })
})
