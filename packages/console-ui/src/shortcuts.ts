/**
 * shortcuts.ts — the console's keyboard shortcut registry.
 *
 * One table (`SHORTCUTS`) is the single source of truth: the global listener
 * (`useShortcuts`, installed once in App.tsx) matches keydowns against it, the
 * help sheet (`ShortcutsHelp`) renders it, and the command palette lists its
 * actions with their key hints — so the docs can't drift from the bindings.
 *
 * Rules the dispatcher enforces:
 *  - Chorded keys (Ctrl / Alt) work everywhere, including inside text fields.
 *    Bare keys (`?`, `/`) only fire when focus is NOT in an editable field, so
 *    typing is never hijacked.
 *  - Nothing fires while a modal or drawer is open (the overlay owns the keys).
 *  - `RESERVED` browser/WebView2 keys are never bound: Ctrl+R / F5 reload is the
 *    operator's recovery path when the UI misbehaves, Ctrl+F finds text across
 *    the whole view, Ctrl+P prints, and the clipboard/undo keys stay native. A
 *    unit test fails if a binding ever collides with one of these.
 *  - Ctrl matches Cmd on macOS (metaKey), like the composer's Ctrl+M.
 *
 * Shortcuts whose `scope` is 'composer' are handled inside CommandInput (they
 * only make sense there); they're listed here so the help sheet shows them.
 */
import { useEffect, useRef } from 'react'

export type ShortcutId =
  | 'palette' | 'help' | 'settings' | 'focusComposer' | 'findRunnable'
  | 'switchTab' | 'refreshHosts' | 'approve' | 'deny'
  | 'newChat' | 'nextSession' | 'prevSession'
  | 'stopTurn' | 'dictation'

export type ShortcutGroup = 'General' | 'Navigation' | 'Console' | 'Command bar'

export interface ShortcutDef {
  id: ShortcutId
  /** Key combos, e.g. 'Ctrl+K', 'Alt+A', '?'. Any one triggers the shortcut. */
  keys: string[]
  /** How the help sheet / palette shows the keys (defaults to `keys`). */
  display?: string[]
  label: string
  group: ShortcutGroup
  /** Hidden when the console runs with AI off (`[console] ai = false`). */
  aiOnly?: boolean
  /** 'global' = the App-level listener; 'composer' = handled in CommandInput. */
  scope?: 'global' | 'composer'
  /** Offer it as an action in the Ctrl+K palette. */
  palette?: boolean
}

const TAB_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9'].map(n => `Ctrl+${n}`)

export const SHORTCUTS: readonly ShortcutDef[] = [
  { id: 'palette', keys: ['Ctrl+K'], label: 'Command palette', group: 'General' },
  { id: 'help', keys: ['Ctrl+/', '?'], label: 'Keyboard shortcuts', group: 'General', palette: true },
  { id: 'settings', keys: ['Ctrl+,'], label: 'Open Settings', group: 'General', palette: true },
  { id: 'refreshHosts', keys: ['Alt+R'], label: 'Refresh hosts', group: 'General', palette: true },
  { id: 'switchTab', keys: TAB_KEYS, display: ['Ctrl+1…9'], label: 'Switch to tab 1–9', group: 'Navigation' },
  { id: 'focusComposer', keys: ['Ctrl+L'], label: 'Focus the command bar', group: 'Navigation', palette: true },
  { id: 'findRunnable', keys: ['/'], label: 'Find a runnable in the command bar', group: 'Navigation' },
  { id: 'approve', keys: ['Alt+A'], label: 'Approve the pending confirmation', group: 'Console', palette: true },
  { id: 'deny', keys: ['Alt+D'], label: 'Deny the pending confirmation', group: 'Console', palette: true },
  { id: 'newChat', keys: ['Alt+N'], label: 'New chat', group: 'Console', aiOnly: true, palette: true },
  { id: 'nextSession', keys: ['Ctrl+Tab'], label: 'Next session', group: 'Console', palette: true },
  { id: 'prevSession', keys: ['Ctrl+Shift+Tab'], label: 'Previous session', group: 'Console', palette: true },
  { id: 'stopTurn', keys: ['Escape'], display: ['Esc'], label: 'Stop the running turn', group: 'Command bar', aiOnly: true, scope: 'composer' },
  { id: 'dictation', keys: ['Ctrl+M'], label: 'Start / stop dictation', group: 'Command bar', aiOnly: true, scope: 'composer' },
]

/** Browser / WebView2 keys the console must never take over. */
export const RESERVED: readonly string[] = [
  'Ctrl+R', 'F5', 'Ctrl+Shift+R', 'Ctrl+F5',  // reload — the recovery path for UI glitches
  'Ctrl+F', 'F3',                               // find across the view
  'Ctrl+P',                                     // print
  'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+A', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+Z',
  'Ctrl+Shift+I', 'F12',                        // devtools
]

export interface Combo { ctrl: boolean; alt: boolean; shift: boolean; key: string }

/** 'Ctrl+Shift+Tab' → { ctrl, shift, key: 'tab' }. Keys are lower-cased. */
export function parseCombo(s: string): Combo {
  const parts = s.split('+')
  // 'Ctrl++' style isn't used; a trailing empty part would be the '+' key.
  const key = parts[parts.length - 1]
  const mods = new Set(parts.slice(0, -1).map(m => m.toLowerCase()))
  return { ctrl: mods.has('ctrl'), alt: mods.has('alt'), shift: mods.has('shift'), key: key.toLowerCase() }
}

export function sameCombo(a: string, b: string): boolean {
  const x = parseCombo(a), y = parseCombo(b)
  return x.ctrl === y.ctrl && x.alt === y.alt && x.shift === y.shift && x.key === y.key
}

/** True when `combo` is a reserved browser key. */
export function isReserved(combo: string): boolean {
  return RESERVED.some(r => sameCombo(r, combo))
}

type KeyLike = Pick<KeyboardEvent, 'key' | 'code' | 'ctrlKey' | 'metaKey' | 'altKey' | 'shiftKey'>

// Punctuation that needs Shift on most layouts ('?') — don't demand an exact
// Shift match for it, compare the produced character instead.
const SHIFTED_CHARS = new Set(['?'])

/** Does the keyboard event match the combo string? */
export function matchesCombo(e: KeyLike, combo: string): boolean {
  const c = parseCombo(combo)
  if (c.ctrl !== (e.ctrlKey || e.metaKey)) return false
  if (c.alt !== e.altKey) return false
  const key = (e.key ?? '').toLowerCase()
  if (SHIFTED_CHARS.has(c.key)) return key === c.key
  if (c.shift !== e.shiftKey) return false
  if (key === c.key) return true
  // Alt+letter can produce a different character (macOS: Alt+A → 'å'); fall
  // back to the physical key code for letters and digits.
  if (/^[a-z]$/.test(c.key)) return e.code === `Key${c.key.toUpperCase()}`
  if (/^[0-9]$/.test(c.key)) return e.code === `Digit${c.key}`
  return false
}

/** Is the event target an editable field (input / textarea / contenteditable)? */
export function isEditableTarget(t: EventTarget | null): boolean {
  if (!t || !(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  const tag = t.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag === 'INPUT') {
    const type = (t as HTMLInputElement).type
    return !['button', 'checkbox', 'radio', 'submit', 'reset', 'range', 'color', 'file'].includes(type)
  }
  return false
}

/** Is an antd Modal or Drawer currently open? */
export function overlayOpen(doc: Document = document): boolean {
  if (doc.querySelector('.ant-drawer-open')) return true
  return Array.from(doc.querySelectorAll<HTMLElement>('.ant-modal-wrap'))
    .some(el => el.style.display !== 'none')
}

/** The shortcuts to show / bind for the active feature set. */
export function activeShortcuts(ai: boolean): ShortcutDef[] {
  return SHORTCUTS.filter(s => ai || !s.aiOnly)
}

/** Resolve a keydown to (shortcut, index of the matched key) — or null. */
export function resolveShortcut(
  e: KeyLike & { target: EventTarget | null },
  ai: boolean,
): { def: ShortcutDef; index: number } | null {
  const editable = isEditableTarget(e.target)
  for (const def of activeShortcuts(ai)) {
    if (def.scope === 'composer') continue
    for (let i = 0; i < def.keys.length; i++) {
      const combo = def.keys[i]
      if (isReserved(combo)) continue
      const c = parseCombo(combo)
      // Bare keys never fire while typing in a field.
      if (!c.ctrl && !c.alt && editable) continue
      if (matchesCombo(e, combo)) return { def, index: i }
    }
  }
  return null
}

/** Human-readable keys for a shortcut (help sheet, palette, tooltips). */
export function shortcutKeys(id: ShortcutId): string[] {
  const def = SHORTCUTS.find(s => s.id === id)
  if (!def) return []
  return def.display ?? def.keys
}

/** "Alt+A" — the first key of a shortcut, for a tooltip suffix. */
export function shortcutHint(id: ShortcutId): string {
  return shortcutKeys(id)[0] ?? ''
}

export type ShortcutHandlers = Partial<Record<ShortcutId, (index: number) => void>>

/**
 * Install the global keydown listener. `handlers` may change every render —
 * the latest set is read through a ref, so the listener is attached once.
 */
export function useShortcuts(handlers: ShortcutHandlers, ai: boolean): void {
  const ref = useRef({ handlers, ai })
  ref.current = { handlers, ai }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return
      const hit = resolveShortcut(e, ref.current.ai)
      if (!hit) return
      const run = ref.current.handlers[hit.def.id]
      if (!run) return
      if (overlayOpen()) return
      e.preventDefault()
      run(hit.index)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}

/**
 * Keyboard Approve / Deny for the oldest pending confirmation in the Console.
 * Never answers a gate the operator can't see: if the gate isn't on screen the
 * first press scrolls it into view and flashes it ('revealed'); a second press
 * answers it ('answered'). Clicks the card's own button, so edited args and the
 * audit trail behave exactly as a mouse click.
 */
export function answerPendingConfirm(
  action: 'approve' | 'deny',
  opts: { doc?: Document; inView?: (el: HTMLElement) => boolean } = {},
): 'answered' | 'revealed' | 'none' {
  const doc = opts.doc ?? document
  const inView = opts.inView ?? elementInView
  const gate = doc.querySelector<HTMLElement>('[data-rs-confirm-gate]')
  const btn = gate?.querySelector<HTMLElement>(`[data-rs-confirm="${action}"]`)
  if (!gate || !btn) return 'none'
  // The buttons are what the operator acts on — they must be on screen (a
  // long args preview can push them below the fold even when the card shows).
  if (!inView(btn)) {
    revealPendingConfirm(doc)
    return 'revealed'
  }
  btn.click()
  return 'answered'
}

/** Scroll the oldest pending confirmation into view and flash it. */
export function revealPendingConfirm(doc: Document = document): boolean {
  const gate = doc.querySelector<HTMLElement>('[data-rs-confirm-gate]')
  if (!gate) return false
  gate.scrollIntoView({ block: 'center' })
  gate.classList.remove('rs-flash')
  void gate.offsetWidth  // restart the animation on repeated presses
  gate.classList.add('rs-flash')
  return true
}

function elementInView(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect()
  if (r.width === 0 && r.height === 0) return false  // display:none (another tab)
  if (r.top < 0 || r.bottom > (window.innerHeight || document.documentElement.clientHeight)) return false
  // Clipped by a scrolling ancestor (the Console's output pane)?
  for (let p = el.parentElement; p; p = p.parentElement) {
    const o = getComputedStyle(p).overflowY
    if (o !== 'auto' && o !== 'scroll' && o !== 'hidden') continue
    const pr = p.getBoundingClientRect()
    if (r.top < pr.top || r.bottom > pr.bottom) return false
  }
  return true
}

/** Is any confirmation gate rendered (whether or not it's on screen)? */
export function hasPendingConfirm(doc: Document = document): boolean {
  return !!doc.querySelector('[data-rs-confirm-gate]')
}
