/**
 * Pure helpers for the Send to terminals bar (SendBar.tsx).
 */
import type { SendToTerminalsResult, TerminalWindow } from '../bridge'

/** Sending to more windows than this asks first — once per set of windows. */
export const CONFIRM_OVER = 10

/** A stable key for a set of window ids (order-insensitive). */
export function signature(ids: string[]): string {
  return [...ids].sort().join(',')
}

/** Whether sending to `ticked` should ask first: more than `CONFIRM_OVER`
 *  windows, and not the same set the person already said yes to. */
export function needsConfirm(ticked: string[], confirmed: string | null, over = CONFIRM_OVER): boolean {
  return ticked.length > over && signature(ticked) !== confirmed
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
