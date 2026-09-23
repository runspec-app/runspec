/**
 * ErrorBoundary: a transient module-load error (the cold-launch "failed to fetch
 * dynamically imported module" race) auto-reloads once, guarded so it can't loop;
 * everything else falls through to the manual "hit a snag" panel.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ErrorBoundary } from './ErrorBoundary'

function Boom({ message, name }: { message: string; name?: string }): React.ReactElement {
  const err = new Error(message)
  if (name) err.name = name
  throw err
}

let reload: ReturnType<typeof vi.fn>
let errSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  sessionStorage.clear()
  reload = vi.fn()
  // jsdom's location.reload throws "Not implemented"; replace it with a spy.
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload },
    writable: true,
    configurable: true,
  })
  // React logs the caught error to console.error; silence it for a clean run.
  errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  errSpy.mockRestore()
  sessionStorage.clear()
})

describe('ErrorBoundary', () => {
  it('auto-reloads once on a transient chunk error and sets the guard', () => {
    render(
      <ErrorBoundary>
        <Boom message="Failed to fetch dynamically imported module: http://x/assets/Y.js" />
      </ErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('rs-chunk-reload')).toBe('1')
    expect(screen.getByText('Reloading…')).toBeTruthy()
  })

  it('treats the WebKitGTK wording and ChunkLoadError name as transient too', () => {
    const { unmount } = render(
      <ErrorBoundary>
        <Boom message="Importing a module script failed." />
      </ErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
    unmount()
    sessionStorage.clear()
    reload.mockClear()
    render(
      <ErrorBoundary>
        <Boom message="loading chunk 5 failed" name="ChunkLoadError" />
      </ErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('shows the manual panel (no reload) when the guard is already set — no loop', () => {
    sessionStorage.setItem('rs-chunk-reload', '1')
    render(
      <ErrorBoundary>
        <Boom message="Failed to fetch dynamically imported module" />
      </ErrorBoundary>,
    )
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByText('The console hit a snag while loading.')).toBeTruthy()
  })

  it('shows the manual panel immediately for a non-transient error', () => {
    render(
      <ErrorBoundary>
        <Boom message="Cannot read properties of undefined (reading foo)" />
      </ErrorBoundary>,
    )
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByText('The console hit a snag while loading.')).toBeTruthy()
  })

  it('renders children unchanged when nothing throws', () => {
    render(
      <ErrorBoundary>
        <div>all good</div>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all good')).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
  })
})
