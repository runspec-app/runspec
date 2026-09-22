import React from 'react'

/**
 * App-level error boundary. Without one, a single throw during render (e.g. a
 * child rendering against half-loaded data during a cold pywebview launch, or a
 * mount-time bridge call rejecting) unmounts the whole React tree to a blank
 * page — the "blank screen on load, fixed by a manual reload" symptom. This
 * catches the throw and shows a recoverable panel with a Reload button (which
 * re-runs against the now-settled bridge) instead of a white void.
 *
 * It sits ABOVE App's ConfigProvider, so it can't use Ant theme tokens — it
 * styles from the global `--rs-*` CSS vars (defined at :root in index.css, which
 * main.tsx imports) so it themes correctly in dark and light without a provider.
 */
interface State { error: Error | null }

export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Best-effort forward to the backend log. Guarded: the boundary must never
    // throw from its own error path, and the bridge may not be attached yet.
    try {
      const api = (window as unknown as { pywebview?: { api?: Record<string, ((...a: unknown[]) => unknown) | undefined> } }).pywebview?.api
      api?.log_client_error?.(`render crash: ${error?.message || error}`, info?.componentStack || '')
    } catch { /* ignore */ }
  }

  render() {
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
