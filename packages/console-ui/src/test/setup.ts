// Vitest setup — runs before each test file.
//
// jsdom doesn't implement matchMedia or ResizeObserver, but antd components
// (and a few of ours) call them on mount, so rendering throws without these
// stubs. Guarded by `typeof window` so the file is a no-op under the node
// environment if a test ever opts out of jsdom.
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Unmount React trees between tests so DOM state / event listeners don't leak.
afterEach(() => cleanup())

if (typeof window !== 'undefined') {
  if (!window.matchMedia) {
    window.matchMedia = (query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList
  }

  if (!('ResizeObserver' in window)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    ;(window as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub
  }

  // jsdom has no layout, so no scrollIntoView — cmdk (the Ctrl+K palette) and
  // the shortcut reveal call it.
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = function scrollIntoView() {}
  }

  // jsdom implements getComputedStyle(elt) but throws a noisy "Not implemented:
  // window.getComputedStyle(elt, pseudoElt)" when a pseudo-element arg is passed —
  // which antd's scrollbar measurement (rc-util measureScrollbarSize) does every
  // time a Modal / Drawer / portal with a scroll-locker mounts. It's harmless
  // (the measurement just reads a width jsdom reports as 0) but floods stderr and
  // buries real output. Wrap the real implementation to ignore the pseudo-element
  // arg so the call resolves instead of throwing.
  const realGetComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = ((elt: Element) =>
    realGetComputedStyle(elt)) as typeof window.getComputedStyle
}
