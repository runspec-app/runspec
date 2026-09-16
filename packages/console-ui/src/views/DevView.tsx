import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Table, Tag, Button, Tooltip, Space, Typography } from 'antd'
import {
  ClearOutlined,
  PauseCircleOutlined,
  CaretRightOutlined,
  BugOutlined,
  VerticalAlignBottomOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { bridge } from '../bridge'
import {
  subscribe,
  getSnapshot,
  getStats,
  clear,
  setPaused,
  isPaused,
  type DevEntry,
  type DevCategory,
  getErrorSnapshot,
} from '../devbus'
import { useIsDark, textMuted, MONO_FONT } from '../ThemeContext'

const { Text } = Typography

const CATEGORIES: DevCategory[] = [
  'bridge-call', 'output', 'token', 'tool', 'run_end', 'usage', 'discovery', 'ssh', 'event', 'error',
]

const CATEGORY_COLOR: Record<DevCategory, string> = {
  'bridge-call': 'blue',
  output: 'cyan',
  token: 'geekblue',
  tool: 'purple',
  run_end: 'green',
  usage: 'gold',
  discovery: 'default',
  ssh: 'magenta',
  event: 'default',
  error: 'red',
}

const STATUS_COLOR: Record<DevEntry['status'], string> = {
  pending: 'processing',
  ok: 'success',
  error: 'error',
}

function fmtTime(ts: number): string {
  const d = new Date(ts)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms = String(d.getMilliseconds()).padStart(3, '0')
  return `${hh}:${mm}:${ss}.${ms}`
}

// Render-time guard: stringify safely (drop circular refs) and cap the output so
// a giant arg/result payload never freezes the table.
function safeJson(value: unknown, cap = 20_000): string {
  const seen = new WeakSet()
  let out: string
  try {
    out = JSON.stringify(
      value,
      (_k, v) => {
        if (typeof v === 'object' && v !== null) {
          if (seen.has(v)) return '[Circular]'
          seen.add(v)
        }
        if (typeof v === 'function') return '[Function]'
        if (typeof v === 'bigint') return v.toString()
        return v
      },
      2,
    )
  } catch (e) {
    out = String(e)
  }
  if (out == null) return String(value)
  return out.length > cap ? out.slice(0, cap) + '\n… truncated' : out
}

export function DevView() {
  const isDark = useIsDark()
  const entries = useSyncExternalStore(subscribe, getSnapshot)
  // getStats() is a plain getter — re-derive each render off the entries snapshot.
  const stats = useMemo(() => getStats(), [entries])

  const [activeCategories, setActiveCategories] = useState<Set<DevCategory>>(new Set())
  const [paused, setPausedState] = useState(isPaused())
  const [autoScroll, setAutoScroll] = useState(true)
  const [debugEnabled, setDebugEnabled] = useState<boolean | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bridge.is_debug_enabled().then(setDebugEnabled).catch(() => setDebugEnabled(false))
  }, [])

  // Errors are retained beyond the main ring (devbus ERROR_CAP), so when the
  // `error` filter is on the rows come from the retained set — the header's
  // "errors N" counter and the filtered table then agree instead of the rows
  // silently aging out after CAP entries.
  const retainedErrors = useMemo(() => getErrorSnapshot(), [entries])
  const shown = useMemo(() => {
    if (activeCategories.size === 0) return entries
    if (activeCategories.size === 1 && activeCategories.has('error')) return retainedErrors
    if (activeCategories.has('error')) {
      const inRing = new Set(entries.map(e => e.id))
      const rest = activeCategories
      const merged = entries.filter(e => rest.has(e.category))
      const aged = retainedErrors.filter(e => !inRing.has(e.id))
      return aged.length ? [...aged, ...merged].sort((a, b) => a.id - b.id) : merged
    }
    return entries.filter(e => activeCategories.has(e.category))
  }, [entries, activeCategories, retainedErrors])

  // Auto-scroll to the newest entry as rows arrive (unless paused or disabled).
  useEffect(() => {
    if (!autoScroll || paused) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [shown.length, autoScroll, paused])

  const toggleCategory = (c: DevCategory) => {
    setActiveCategories(prev => {
      const next = new Set(prev)
      if (next.has(c)) next.delete(c)
      else next.add(c)
      return next
    })
  }

  const togglePause = () => {
    const next = !paused
    setPaused(next)
    setPausedState(next)
  }

  const handleInspector = () => {
    bridge.open_devtools().catch(() => {})
  }

  const mutedCol = textMuted(isDark)

  const columns: ColumnsType<DevEntry> = [
    {
      title: 'Time',
      dataIndex: 'ts',
      width: 110,
      render: (ts: number) => <Text style={{ fontSize: 13, color: mutedCol }}>{fmtTime(ts)}</Text>,
    },
    {
      title: '',
      dataIndex: 'direction',
      width: 64,
      render: (dir: DevEntry['direction']) => (
        <Text style={{ fontSize: 13, color: dir === 'call' ? '#4fc1ff' : '#b37feb' }}>
          {dir === 'call' ? 'call →' : 'event ←'}
        </Text>
      ),
    },
    {
      title: 'Category',
      dataIndex: 'category',
      width: 110,
      render: (c: DevCategory) => (
        <Tag color={CATEGORY_COLOR[c]} style={{ fontSize: 12, margin: 0 }}>{c}</Tag>
      ),
    },
    {
      title: 'Name',
      dataIndex: 'name',
      width: 200,
      render: (name: string, row: DevEntry) => (
        <Space size={4}>
          <Text style={{ fontSize: 13, fontFamily: MONO_FONT }}>{name}</Text>
          {row.count && row.count > 1 ? (
            <Tag color="default" style={{ fontSize: 11, margin: 0, padding: '0 5px' }}>×{row.count}</Tag>
          ) : null}
        </Space>
      ),
    },
    {
      title: 'Summary',
      dataIndex: 'summary',
      ellipsis: true,
      render: (s: string, row: DevEntry) => (
        <Text style={{ fontSize: 13, color: row.status === 'error' ? '#ff7875' : undefined }} ellipsis>
          {row.error ? row.error : s}
        </Text>
      ),
    },
    {
      title: 'ms',
      dataIndex: 'durationMs',
      width: 70,
      align: 'right',
      render: (d?: number) => (d == null ? '' : <Text style={{ fontSize: 13, color: mutedCol }}>{Math.round(d)}</Text>),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      width: 90,
      render: (s: DevEntry['status']) => (
        <Tag color={STATUS_COLOR[s]} style={{ fontSize: 12, margin: 0 }}>{s}</Tag>
      ),
    },
  ]

  const inspectorTooltip =
    debugEnabled === false
      ? 'Restart with the `--devtools` flag (or RUNSPEC_CONSOLE_DEVTOOLS=1) to enable the inspector'
      : 'Right-click anywhere → Inspect, or press F12'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      {/* Counters */}
      <Space size={16} style={{ marginBottom: 10, fontSize: 14 }}>
        <Text type="secondary">calls <Text strong>{stats.calls}</Text></Text>
        <Text type="secondary">events <Text strong>{stats.events}</Text></Text>
        <Tooltip title={`${retainedErrors.length} error row${retainedErrors.length === 1 ? '' : 's'} retained (the error filter shows them even after they age out of the buffer)`}>
          <Text type="secondary">errors <Text strong style={{ color: stats.errors > 0 ? '#ff4d4f' : undefined }}>{stats.errors}</Text>{retainedErrors.length !== stats.errors && <Text type="secondary"> · {retainedErrors.length} retained</Text>}</Text>
        </Tooltip>
        <Text type="secondary">buffer <Text strong>{stats.total}</Text></Text>
      </Space>

      {/* Controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <Button size="small" icon={<ClearOutlined />} onClick={clear}>Clear</Button>
        <Button
          size="small"
          icon={paused ? <CaretRightOutlined /> : <PauseCircleOutlined />}
          onClick={togglePause}
        >
          {paused ? 'Resume' : 'Pause'}
        </Button>
        <Tooltip title="Follow new entries">
          <Button
            size="small"
            type={autoScroll ? 'primary' : 'default'}
            icon={<VerticalAlignBottomOutlined />}
            onClick={() => setAutoScroll(v => !v)}
          >
            Auto-scroll
          </Button>
        </Tooltip>
        <Tooltip title={inspectorTooltip}>
          <Button
            size="small"
            icon={<BugOutlined />}
            disabled={debugEnabled === false}
            onClick={handleInspector}
          >
            Open Chromium Inspector
          </Button>
        </Tooltip>

        <span style={{ width: 1, height: 18, background: 'var(--rs-border-strong)', margin: '0 4px' }} />

        {CATEGORIES.map(c => (
          <Tag
            key={c}
            onClick={() => toggleCategory(c)}
            color={activeCategories.has(c) ? CATEGORY_COLOR[c] : undefined}
            style={{ cursor: 'pointer', fontSize: 12, margin: 0, userSelect: 'none' }}
          >
            {c}
          </Tag>
        ))}
        {activeCategories.size > 0 && (
          <Button size="small" type="text" onClick={() => setActiveCategories(new Set())}
            style={{ fontSize: 12, color: 'var(--rs-text-muted)', padding: '0 4px' }}>
            clear filter
          </Button>
        )}
      </div>

      {/* Timeline */}
      <div ref={scrollRef} style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        <Table<DevEntry>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={shown}
          pagination={false}
          expandable={{
            expandedRowRender: (row: DevEntry) => (
              <pre style={{
                margin: 0, fontSize: 13, fontFamily: MONO_FONT, whiteSpace: 'pre-wrap',
                wordBreak: 'break-word', maxHeight: 320, overflow: 'auto',
                color: isDark ? '#ccc' : '#333',
              }}>
                {safeJson(row.detail)}
              </pre>
            ),
          }}
        />
      </div>
    </div>
  )
}
