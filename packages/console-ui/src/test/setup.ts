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
}
