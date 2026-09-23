import React from 'react'

/**
 * App-level error boundary. Without one, a single throw during render (e.g. a
 * child rendering against half-loaded data during a cold pywebview launch, or a
 * mount-time bridge call rejecting) unmounts the whole React tree to a blank
 * page — the "blank screen on load, fixed by a manual reload" symptom. This
 * catches the throw and shows a recoverable panel with a Reload button (which
 * re-runs against the now-settled bridge) instead of a white void.
 *
 * On a *transient* load/render error (most visibly "failed to fetch dynamically
 * imported module" from a chunk fetch that lost a cold-launch race, but also the
 * half-loaded-data case above) it **auto-reloads once** instead of showing the
 * panel — a reload almost always wins the second time. A `sessionStorage` guard
 * makes it at most one auto-reload per failed load sequence: App's mount effect
 * clears the guard on a healthy mount, so a genuine later error can auto-heal
 * again, but a reload that keeps failing falls through to the manual panel rather
 * than looping.
 *
 * It sits ABOVE App's ConfigProvider, so it can't use Ant theme tokens — it
 * styles from the global `--rs-*` CSS vars (defined at :root in index.css, which
 * main.tsx imports) so it themes correctly in dark and light without a provider.
 */
interface State { error: Error | null; reloading: boolean }

const RELOAD_GUARD = 'rs-chunk-reload'

// A transient module-load failure a reload almost always fixes: a chunk fetch
// that lost the cold-launch race. The wording differs across engines — Chromium /
// WebView2 says "Failed to fetch dynamically imported module", WebKitGTK says
// "Importing a module script failed" — so match the stable fragments (+ the
// bundler's ChunkLoadError name), case-insensitively. Deliberately narrow: an
// arbitrary render crash still shows the manual panel rather than auto-reloading.
function isTransientLoadError(error: unknown): boolean {
  const err = error as { name?: string; message?: string } | null
  if (!err) return false
  if (err.name === 'ChunkLoadError') return true
  const msg = String(err.message || err).toLowerCase()
  return (
    msg.includes('dynamically imported module') ||
    msg.includes('importing a module script failed')
  )
}

function guardAlreadyTripped(): boolean {
  try {
    return sessionStorage.getItem(RELOAD_GUARD) === '1'
  } catch {
    return false // no sessionStorage (private mode) ⇒ never auto-reload, show panel
  }
}

export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { error: null, reloading: false }

  static getDerivedStateFromError(error: Error): State {
    // Decide here (before render) whether to auto-reload, so render can show a
    // quiet "Reloading…" instead of flashing the panel first. The actual reload
    // is fired in componentDidCatch (side effects don't belong in render).
    const reloading = isTransientLoadError(error) && !guardAlreadyTripped()
    return { error, reloading }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Best-effort forward to the backend log. Guarded: the boundary must never
    // throw from its own error path, and the bridge may not be attached yet.
    try {
      const api = (window as unknown as { pywebview?: { api?: Record<string, ((...a: unknown[]) => unknown) | undefined> } }).pywebview?.api
      api?.log_client_error?.(`render crash: ${error?.message || error}`, info?.componentStack || '')
    } catch { /* ignore */ }
    // Auto-reload once for a transient load error. Set the guard first so a reload
    // that keeps failing shows the manual panel instead of looping.
    if (this.state.reloading) {
      try { sessionStorage.setItem(RELOAD_GUARD, '1') } catch { /* ignore */ }
      try { window.location.reload() } catch { /* ignore */ }
    }
  }

  render() {
    if (this.state.reloading) {
      // The reload is in flight — show a quiet placeholder, not the panel.
      return (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          minHeight: '100vh', color: 'var(--rs-text-muted, #999)',
          fontFamily: 'system-ui, -apple-system, sans-serif', fontSize: 13,
        }}>
          Reloading…
        </div>
      )
    }
    if (!this.state.error) return this.props.children
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        minHeight: '100vh', padding: 24, gap: 16, textAlign: 'center',
        background: 'var(--rs-bg, #141414)', color: 'var(--rs-text, #e6e6e6)',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}>
        <div style={{ fontSize: 16, fontWeight: 600 }}>The console hit a snag while loading.</div>
        <div style={{ fontSize: 13, color: 'var(--rs-text-muted, #999)', maxWidth: 460 }}>
          This is usually a transient startup race. Reloading almost always fixes it.
        </div>
        <button
          onClick={() => window.location.reload()}
          style={{
            padding: '8px 18px', fontSize: 14, cursor: 'pointer', borderRadius: 6,
            border: 'none', color: '#fff', background: 'var(--rs-accent, #1668dc)',
          }}
        >
          Reload
        </button>
        <details style={{ fontSize: 12, color: 'var(--rs-text-faint, #777)', maxWidth: 620 }}>
          <summary style={{ cursor: 'pointer' }}>Details</summary>
          <pre style={{ textAlign: 'left', whiteSpace: 'pre-wrap', marginTop: 8 }}>
            {String(this.state.error?.stack || this.state.error?.message || this.state.error)}
          </pre>
        </details>
      </div>
    )
  }
}
