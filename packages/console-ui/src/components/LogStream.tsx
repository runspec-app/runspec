// Shared renderer for a merged per-invocation log stream — the coloured,
// timestamped, run-id-tagged lines shown by the Logs tab's "View merged stream"
// drawer and by History's expanded-row "View full log" drill. Both read the same
// parsed JSON log records (from bridge.logs_view), so the rendering lives here
// once.

const LOG_LEVEL_COLOUR: Record<string, string> = {
  INFO: '#4fc1ff',
  WARNING: '#faad14',
  ERROR: '#ff4d4f',
  CRITICAL: '#ff4d4f',
  DEBUG: '#888',
}

export interface StreamRecord {
  ts?: string
  level?: string
  message?: string
  extra?: { run_id?: string }
}

export function LogStream({ records }: { records: Record<string, unknown>[] }) {
  return (
    <div style={{ background: '#0d0d0d', border: '1px solid #222', borderRadius: 6, padding: '8px 12px', fontFamily: 'monospace', fontSize: 12, lineHeight: 1.8 }}>
      {records.map((rec, i) => {
        const r = rec as StreamRecord
        const colour = LOG_LEVEL_COLOUR[r.level ?? ''] ?? '#d4d4d4'
        return (
          <div key={i}>
            <span style={{ color: '#555', marginRight: 8 }}>{r.ts ? new Date(r.ts).toLocaleTimeString() : '—'}</span>
            <span style={{ color: colour, marginRight: 8, minWidth: 56, display: 'inline-block' }}>{r.level ?? ''}</span>
            {r.extra?.run_id && <span style={{ color: '#7a6', marginRight: 8 }}>{String(r.extra.run_id).slice(0, 8)}</span>}
            <span style={{ color: '#d4d4d4' }}>{r.message ?? ''}</span>
          </div>
        )
      })}
    </div>
  )
}
