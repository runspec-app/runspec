import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// The guard lives inline in index.html so it runs BEFORE the module bundle — the
// whole point is to shim a null localStorage before the app's first getItem().
// That means it can't be imported as a module; instead we read it out of
// index.html and execute it against a controlled fake `window`, exactly as the
// browser would, so a regression in the guard is caught in CI.

const indexHtml = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../index.html'),
  'utf8',
)

function guardSource(): string {
  // The inline guard is the first bare <script> (the app entry is
  // <script type="module">, which this regex skips).
  const m = indexHtml.match(/<script>([\s\S]*?)<\/script>/)
  if (!m) throw new Error('inline localStorage guard <script> not found in index.html')
  return m[1]
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function runGuard(win: any): void {
  // eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
  new Function('window', guardSource())(win)
}

describe('index.html localStorage guard', () => {
  it('is present in index.html', () => {
    expect(guardSource()).toContain('localStorage')
  })

  it('installs a working in-memory shim when localStorage is null', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const win: any = { localStorage: null }
    runGuard(win)

    expect(win.localStorage).toBeTruthy()
    expect(win.localStorage.getItem('missing')).toBeNull()

    win.localStorage.setItem('k', 'v')
    expect(win.localStorage.getItem('k')).toBe('v')
    expect(win.localStorage.length).toBe(1)

    win.localStorage.removeItem('k')
    expect(win.localStorage.getItem('k')).toBeNull()
    expect(win.localStorage.length).toBe(0)
  })

  it('coerces stored values to strings (matches the real Storage API)', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const win: any = { localStorage: null }
    runGuard(win)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    win.localStorage.setItem('n', 42 as any)
    expect(win.localStorage.getItem('n')).toBe('42')
  })

  it('leaves a working localStorage untouched', () => {
    const real = {
      getItem: () => 'real',
      setItem: () => {},
      removeItem: () => {},
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const win: any = { localStorage: real }
    runGuard(win)
    // A usable store must not be replaced by the shim.
    expect(win.localStorage).toBe(real)
  })
})
