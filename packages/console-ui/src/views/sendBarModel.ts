/**
 * Pure helpers for the Send to terminals bar (SendBar.tsx). Named
 * sendBarModel so it can't collide with SendBar.tsx on a case-insensitive disk.
 */
import type { SendToTerminalsResult, TerminalWindow } from '../bridge'

/** Sending to more windows than this asks first (the risk ladder's fleet
 *  level). The person may tick "don't ask again" for that set of windows. */
export const CONFIRM_OVER = 10

// Lines and keys that stop, remove or end something: the command word at the
// start of the line or after ; & | (, optionally behind sudo. These ask every
// time they go to more than one window, whatever was remembered.
const DISRUPTIVE_LINE = /(^\s*|[;&|(]\s*)(sudo\s+(-\S+\s+)*)?(rm|rmdir|reboot|shutdown|halt|poweroff|kill|pkill|killall|mkfs(\.\w+)?|dd|truncate|userdel|groupdel|wipefs|shred|init\s+[06]|systemctl\s+(stop|restart|disable|mask|kill|reboot|poweroff|halt)|service\s+\S+\s+(stop|restart)|docker\s+(rm|stop|kill|system\s+prune)|kubectl\s+delete|iptables\s+-F|drop\s+(table|database)|chmod\s+-R|chown\s+-R|mv|apt(-get)?\s+(remove|purge|autoremove)|(yum|dnf)\s+(remove|erase)|pip3?\s+uninstall|crontab\s+-r)(?=$|[\s;&|)])/i
const DISRUPTIVE_KEYS = new Set(['ctrl-c', 'ctrl-d'])

export type SendPlan =
  | { kind: 'empty' }
  | { kind: 'send' }
  | { kind: 'ask'; disruptive: boolean; canRemember: boolean }

/** What a send should do before it goes, by the risk ladder:
 *  - an empty line is never sent (Enter alone has its own key button);
 *  - a hidden (password) send asks only past the window limit, like any line:
 *    it is usually the answer to a prompt the visible line raised, but if that
 *    prompt didn't appear everywhere, the text lands in shells and history;
 *  - a disruptive line or key to more than one window asks every time;
 *  - anything to more than CONFIRM_OVER windows asks, unless the person said
 *    not to ask again for exactly this set.
 *  Pure, for tests. */
export function planSend(opts: {
  ticked: string[]; line?: string; key?: string; hidden?: boolean; remembered: string | null; over?: number
}): SendPlan {
  const { ticked, line = '', key, hidden = false, remembered, over = CONFIRM_OVER } = opts
  if (!key && !line.trim()) return { kind: 'empty' }
  if (hidden) {
    return ticked.length > over && signature(ticked) !== remembered
      ? { kind: 'ask', disruptive: false, canRemember: true }
      : { kind: 'send' }
  }
  const disruptive = key ? DISRUPTIVE_KEYS.has(key) : DISRUPTIVE_LINE.test(line)
  if (disruptive && ticked.length > 1) return { kind: 'ask', disruptive: true, canRemember: false }
  if (ticked.length > over && signature(ticked) !== remembered) return { kind: 'ask', disruptive: false, canRemember: true }
  return { kind: 'send' }
}

/** A stable key for a set of window ids (order-insensitive). */
export function signature(ids: string[]): string {
  return [...ids].sort().join(',')
}

/** Windows that appeared after the bar's first list, so they join unticked
 *  instead of silently receiving the next line. Pure, for tests. */
export function lateArrivals(windows: TerminalWindow[], seen: Set<string>, firstLoad: boolean): string[] {
  if (firstLoad) return []
  return windows.filter(w => !seen.has(w.id)).map(w => w.id)
}

/** The ids to send to: windows listed now that aren't unticked. */
export function tickedIds(windows: TerminalWindow[], unticked: Set<string>): string[] {
  return windows.filter(w => !unticked.has(w.id)).map(w => w.id)
}

/** Shell-style history recall. `index` is the current position (`history.length`
 *  = the draft line); `dir` -1 goes back, +1 forward. Returns the new index. */
export function historyStep(length: number, index: number, dir: -1 | 1): number {
  return Math.max(0, Math.min(length, index + dir))
}

/** Add a sent line to the history: no blanks, no repeat of the last line, at
 *  most `max` lines kept. */
export function pushHistory(history: string[], line: string, max = 100): string[] {
  if (!line.trim() || history[history.length - 1] === line) return history
  return [...history, line].slice(-max)
}

/** One-line outcome of a send for the status line. */
export function describeResult(res: SendToTerminalsResult, what: string): string {
  const n = res.sent.length
  let out = `${what} → ${n} terminal${n === 1 ? '' : 's'}`
  if (res.missing.length) {
    const shown = res.missing.slice(0, 4).join(', ')
    const more = res.missing.length > 4 ? ` +${res.missing.length - 4}` : ''
    out += `. Not sent to ${res.missing.length} (closed or not ready): ${shown}${more}`
  }
  return out
}

/** The bar's key buttons, in display order: `key` is the name the backend takes. */
export const KEY_BUTTONS: { key: string; label: string; title: string }[] = [
  { key: 'enter', label: 'Enter', title: 'Press Enter' },
  { key: 'ctrl-c', label: 'Ctrl+C', title: 'Interrupt (Ctrl+C)' },
  { key: 'ctrl-d', label: 'Ctrl+D', title: 'End of input / log out (Ctrl+D)' },
  { key: 'tab', label: 'Tab', title: 'Tab' },
  { key: 'esc', label: 'Esc', title: 'Escape' },
  { key: 'up', label: '↑', title: 'Up arrow' },
  { key: 'down', label: '↓', title: 'Down arrow' },
  { key: 'left', label: '←', title: 'Left arrow' },
  { key: 'right', label: '→', title: 'Right arrow' },
]
