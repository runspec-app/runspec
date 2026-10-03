/**
 * The shortcut registry: combo parsing/matching, the reserved browser keys
 * (reload / find / print must never be taken over), field safety for bare
 * keys, AI-off filtering, and keyboard Approve/Deny never answering a gate the
 * operator can't see.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  SHORTCUTS, RESERVED, parseCombo, sameCombo, isReserved, matchesCombo, resolveShortcut,
  activeShortcuts, overlayOpen, answerPendingConfirm, hasPendingConfirm, isEditableTarget,
} from './shortcuts'

const ev = (key: string, mods: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean; code?: string } = {}, target: EventTarget | null = document.body) => ({
  key, code: mods.code ?? '', ctrlKey: !!mods.ctrl, metaKey: !!mods.meta, altKey: !!mods.alt, shiftKey: !!mods.shift, target,
})

afterEach(() => { document.body.innerHTML = '' })

describe('combos', () => {
  it('parses modifiers and lower-cases the key', () => {
    expect(parseCombo('Ctrl+Shift+Tab')).toEqual({ ctrl: true, alt: false, shift: true, key: 'tab' })
    expect(sameCombo('ctrl+k', 'Ctrl+K')).toBe(true)
  })

  it('matches Ctrl or Cmd, and exact Alt/Shift', () => {
    expect(matchesCombo(ev('k', { ctrl: true }), 'Ctrl+K')).toBe(true)
    expect(matchesCombo(ev('k', { meta: true }), 'Ctrl+K')).toBe(true)
    expect(matchesCombo(ev('k', { ctrl: true, shift: true }), 'Ctrl+K')).toBe(false)
    expect(matchesCombo(ev('k'), 'Ctrl+K')).toBe(false)
  })

  it('falls back to the physical key for Alt+letter (macOS å)', () => {
    expect(matchesCombo(ev('å', { alt: true, code: 'KeyA' }), 'Alt+A')).toBe(true)
  })

  it("matches '?' whatever the Shift state", () => {
    expect(matchesCombo(ev('?', { shift: true }), '?')).toBe(true)
  })
})

describe('reserved browser keys', () => {
  it('reserves reload, find and print', () => {
    for (const k of ['Ctrl+R', 'F5', 'Ctrl+F', 'Ctrl+P']) expect(isReserved(k)).toBe(true)
  })

  it('no shortcut binds a reserved key', () => {
    for (const def of SHORTCUTS) for (const k of def.keys) {
      expect(isReserved(k), `${def.id} binds reserved ${k}`).toBe(false)
    }
    expect(RESERVED.length).toBeGreaterThan(0)
  })

  it('no two shortcuts share a key', () => {
    const seen: string[] = []
    for (const def of SHORTCUTS) for (const k of def.keys) {
      expect(seen.some(s => sameCombo(s, k)), `${k} bound twice`).toBe(false)
      seen.push(k)
    }
  })

  it('Ctrl+R / Ctrl+F / Ctrl+P resolve to nothing', () => {
    for (const k of ['r', 'f', 'p']) expect(resolveShortcut(ev(k, { ctrl: true }), true)).toBeNull()
  })
})

describe('resolveShortcut', () => {
  it('resolves Ctrl+3 to tab index 2', () => {
    const hit = resolveShortcut(ev('3', { ctrl: true }), true)
    expect(hit?.def.id).toBe('switchTab')
    expect(hit?.index).toBe(2)
  })

  it('ignores bare keys while typing in a field, but not chords', () => {
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    expect(isEditableTarget(ta)).toBe(true)
    expect(resolveShortcut(ev('/', {}, ta), true)).toBeNull()
    expect(resolveShortcut(ev('?', { shift: true }, ta), true)).toBeNull()
    expect(resolveShortcut(ev('/', {}, document.body), true)?.def.id).toBe('findRunnable')
    expect(resolveShortcut(ev('k', { ctrl: true }, ta), true)?.def.id).toBe('palette')
  })

  it('leaves composer-scoped keys (Esc, Ctrl+M) to the command bar', () => {
    expect(resolveShortcut(ev('Escape'), true)).toBeNull()
    expect(resolveShortcut(ev('m', { ctrl: true }), true)).toBeNull()
  })

  it('drops AI-only shortcuts when AI is off', () => {
    expect(resolveShortcut(ev('n', { alt: true }), true)?.def.id).toBe('newChat')
    expect(resolveShortcut(ev('n', { alt: true }), false)).toBeNull()
    expect(activeShortcuts(false).some(s => s.id === 'newChat')).toBe(false)
    // Confirm gates still come from the MCP server / triggers with AI off.
    expect(activeShortcuts(false).some(s => s.id === 'approve')).toBe(true)
  })
})

describe('overlayOpen', () => {
  it('sees an open modal or drawer, not a hidden one', () => {
    expect(overlayOpen()).toBe(false)
    const wrap = document.createElement('div')
    wrap.className = 'ant-modal-wrap'
    wrap.style.display = 'none'
    document.body.appendChild(wrap)
    expect(overlayOpen()).toBe(false)
    wrap.style.display = ''
    expect(overlayOpen()).toBe(true)
  })
})

describe('answerPendingConfirm', () => {
  function gate() {
    document.body.innerHTML = `
      <div data-rs-confirm-gate>
        <button data-rs-confirm="approve">Approve</button>
        <button data-rs-confirm="deny">Deny</button>
      </div>`
    const approve = vi.fn(), deny = vi.fn()
    document.querySelector('[data-rs-confirm="approve"]')!.addEventListener('click', approve)
    document.querySelector('[data-rs-confirm="deny"]')!.addEventListener('click', deny)
    return { approve, deny }
  }

  it('reports none when nothing is pending', () => {
    expect(hasPendingConfirm()).toBe(false)
    expect(answerPendingConfirm('approve', { inView: () => true })).toBe('none')
  })

  it('clicks the visible gate’s own button', () => {
    const { approve, deny } = gate()
    expect(answerPendingConfirm('deny', { inView: () => true })).toBe('answered')
    expect(deny).toHaveBeenCalledOnce()
    expect(approve).not.toHaveBeenCalled()
  })

  it('reveals an off-screen gate instead of answering it', () => {
    const { approve } = gate()
    expect(answerPendingConfirm('approve', { inView: () => false })).toBe('revealed')
    expect(approve).not.toHaveBeenCalled()
    expect(document.querySelector('[data-rs-confirm-gate]')!.classList.contains('rs-flash')).toBe(true)
  })

  it('treats an unlaid-out gate (hidden tab) as off screen by default', () => {
    const { approve } = gate()
    expect(answerPendingConfirm('approve')).toBe('revealed')
    expect(approve).not.toHaveBeenCalled()
  })
})
