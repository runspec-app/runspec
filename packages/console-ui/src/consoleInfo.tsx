// What the Console knows about the fleet, for the approval card: a runnable's
// one-line description and how many hosts a group holds. App provides it from
// discovery; the card reads it so an approval says what the call will *do*,
// not only which policy parked it.

import { createContext, useContext } from 'react'

export interface ConsoleInfo {
  /** The runnable's description (first host it was discovered on), if any. */
  describe: (name: string) => string | undefined
  /** How many hosts a group name stands for; 0 when it isn't a group. */
  groupSize: (name: string) => number
  /** The hosts a runnable was discovered on; empty when it isn't a runnable. */
  hostsOf?: (name: string) => string[]
}

export const ConsoleInfoContext = createContext<ConsoleInfo>({ describe: () => undefined, groupSize: () => 0 })
export const useConsoleInfo = () => useContext(ConsoleInfoContext)

// Verbs whose effect is hard to take back or interrupts something running.
const DISRUPTIVE = /(^|[-_ ])(delete|remove|rm|drop|purge|wipe|destroy|uninstall|deprovision|prune|clear|truncate|kill|stop|restart|reboot|shutdown|reset|revoke|rotate|format|upgrade|install)($|[-_ ])/i

export interface Consequence {
  /** One line on what the runnable does. */
  description?: string
  /** Hosts the call reaches when its target is a group (fan-out), else 0. */
  fanout: number
  /** Markers worth reading before Approve. */
  markers: { tone: 'failed' | 'attention'; text: string; tip: string }[]
}

/** Where an approval says the call runs, when the call names no host: a
 *  runnable found on one host runs there; one on several hosts, or a tool this
 *  console doesn't know, says no host was named (never a guess). Never empty,
 *  so a card always speaks to its target. */
export function fallbackTarget(name: string, info: ConsoleInfo): { host: string; note: string } {
  const hosts = info.hostsOf?.(name) ?? []
  if (hosts.length === 1) return { host: hosts[0], note: hosts[0] === 'local' ? ' (this computer)' : '' }
  if (hosts.length > 1) return { host: '', note: `no host named; it's on ${hosts.length} hosts` }
  return { host: '', note: 'no host named' }
}

/** What an approval should say about consequence: the runnable's purpose, a
 *  marker when it runs as root or reads as disruptive, and how many hosts a
 *  group target fans out to. Pure, for tests. */
export function approvalConsequence(opts: {
  name: string
  command?: string
  runsAs?: string
  host?: string
  info: ConsoleInfo
}): Consequence {
  const { name, command, runsAs, host, info } = opts
  const markers: Consequence['markers'] = []
  if (runsAs === 'root') {
    markers.push({ tone: 'failed', text: 'as root', tip: 'Runs with full administrator rights on the host.' })
  }
  if (DISRUPTIVE.test(name) || (command && DISRUPTIVE.test(command))) {
    markers.push({ tone: 'attention', text: 'changes the host', tip: 'Its name says it removes, stops, restarts or changes something, which may be hard to undo.' })
  }
  const fanout = host ? info.groupSize(host) : 0
  return { description: info.describe(name), fanout, markers }
}
