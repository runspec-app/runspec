/**
 * AllSchedulesView — one list of every schedule that fires: this console's own,
 * plus each venv's runspec-scheduler schedules (cron or daemon backend) on every
 * connected host in the active profile and the local venvs. Read-only. The option
 * card above it (All / This console / Host cron / Host daemon) presets which
 * source it shows; the filters below narrow it further and are remembered.
 *
 * Rows, states, counts and filtering live in allSchedules.ts (pure, tested).
 */
import { useMemo, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, Alert, Empty, Input, Select, Switch, Space, Spin, theme } from 'antd'
import { ReloadOutlined, HistoryOutlined, InfoCircleOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { MONO_FONT, RADIUS, useIsDark, textColor, textMuted, textSecondary, withAlpha } from '../ThemeContext'
import {
  DEFAULT_FILTERS, STATE_COLOR, filterRows, shortDuration,
  type Collected, type DaemonVenv, type RowFilters, type RowSource, type RowState, type ScheduleRow, type ScheduleScope, type SortKey,
} from './allSchedules'
import { RUN_COLOR, ScheduleDetail, relTime } from './sharedSchedules'

const { Text } = Typography

const FILTERS_KEY = 'runspec.schedules.allFilters'

export function readFilters(): RowFilters {
  try {
    const raw = localStorage.getItem(FILTERS_KEY)
    if (!raw) return DEFAULT_FILTERS
    const v = JSON.parse(raw) as Partial<RowFilters>
    return {
      search: typeof v.search === 'string' ? v.search : '',
      source: Array.isArray(v.source) ? v.source : [],
      states: Array.isArray(v.states) ? v.states : [],
      owners: Array.isArray(v.owners) ? v.owners : [],
      runAs: Array.isArray(v.runAs) ? v.runAs : [],
      onlyProblems: !!v.onlyProblems,
      sort: (['next', 'last', 'host', 'runnable'] as SortKey[]).includes(v.sort as SortKey) ? v.sort as SortKey : 'next',
    }
  } catch { return DEFAULT_FILTERS }
}

function writeFilters(f: RowFilters) {
  try { localStorage.setItem(FILTERS_KEY, JSON.stringify(f)) } catch { /* storage unavailable — fine */ }
}

const SOURCE_LABEL: Record<RowSource, string> = {
  console: 'console', cron: 'host cron', daemon: 'host daemon', unknown: 'host',
}
const SOURCE_COLOR: Record<RowSource, string> = {
  console: 'cyan', cron: 'geekblue', daemon: 'purple', unknown: 'default',
}

const ALL_STATES: RowState[] = [
  'ok', 'running', 'last run failed', 'not installed', 'not installable', 'stale line',
  'scheduler stopped', 'scheduler stale', "can't read", 'disabled', 'unknown', 'legacy line',
]

function DaemonStrip({ d }: { d: DaemonVenv }) {
  const isDark = useIsDark()
  const st = d.daemon?.state ?? 'stopped'
  const color = st === 'running' ? 'green' : st === 'stale' ? 'orange' : 'red'
  const dm = d.daemon
  const bits: string[] = []
  if (dm && st !== 'stopped') {
    if (dm.pid) bits.push(`pid ${dm.pid}`)
    if (dm.uptime_s != null) bits.push(`up ${shortDuration(dm.uptime_s)}`)
    if (dm.heartbeat_age_s != null) bits.push(`heartbeat ${shortDuration(dm.heartbeat_age_s)} ago`)
    if (dm.firing) bits.push(`${dm.firing} firing now`)
  }
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '6px 10px',
      border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, background: 'var(--rs-surface-raised)',
    }}>
      <Tag color={color} style={{ margin: 0 }}>scheduler {st}</Tag>
      <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark) }}>{d.host} / {d.venv}</span>
      {d.owner && <span style={{ color: textMuted(isDark), fontSize: 12 }}>as {d.owner}</span>}
      {bits.length > 0 && <span style={{ color: textSecondary(isDark), fontSize: 12 }}>{bits.join(' · ')}</span>}
    </div>
  )
}

interface AllSchedulesViewProps {
  scope: ScheduleScope
  collected: Collected
  loading: boolean
  loaded: boolean            // a host read has finished at least once
  updatedAt: number | null
  hostCount: number          // hosts read (connected, in the active profile)
  activeProfile?: string
  onRefresh: () => void
  onOpenHistory?: (search: string) => void
}

export function AllSchedulesView({ scope, collected, loading, loaded, updatedAt, hostCount, activeProfile, onRefresh, onOpenHistory }: AllSchedulesViewProps) {
  const isDark = useIsDark()
  const { token } = theme.useToken()
  const [filters, setFiltersState] = useState<RowFilters>(readFilters)
  const setFilters = (patch: Partial<RowFilters>) => setFiltersState(prev => {
    const next = { ...prev, ...patch }
    writeFilters(next)
    return next
  })

  const scoped = useMemo(() => filterRows(collected.rows, scope, { ...DEFAULT_FILTERS, sort: filters.sort }), [collected.rows, scope, filters.sort])
  const rows = useMemo(() => filterRows(collected.rows, scope, filters), [collected.rows, scope, filters])
  const owners = useMemo(() => [...new Set(scoped.map(r => r.owner).filter(Boolean))].sort(), [scoped])
  const runAs = useMemo(() => [...new Set(scoped.map(r => r.runAs).filter(Boolean))].sort(), [scoped])
  const states = useMemo(() => ALL_STATES.filter(s => scoped.some(r => r.state === s) || filters.states.includes(s)), [scoped, filters.states])
  const notes = collected.notes.filter(n => scope === 'all' || n.source === scope)
  const daemons = scope === 'daemon' || scope === 'all' ? collected.daemons : []
  const narrowed = filters.search || filters.states.length || filters.owners.length || filters.runAs.length || filters.onlyProblems || (scope === 'all' && filters.source.length)

  const muted = textMuted(isDark)
  const secondary = textSecondary(isDark)

  const columns: ColumnType<ScheduleRow>[] = [
    {
      title: 'State',
      key: 'state',
      width: 150,
      render: (_: unknown, r) => (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
          <Tooltip title={r.stateTip}>
            <Tag color={STATE_COLOR[r.state]} style={{ margin: 0 }}>{r.state}</Tag>
          </Tooltip>
          {scope === 'all' && <Tag bordered={false} color={SOURCE_COLOR[r.source]} style={{ margin: 0, fontSize: 11 }}>{SOURCE_LABEL[r.source]}</Tag>}
        </div>
      ),
    },
    {
      title: 'Schedule',
      key: 'id',
      render: (_: unknown, r) => r.kind === 'unreadable'
        ? <Text type="secondary" style={{ fontSize: 13 }}>venv couldn’t be read</Text>
        : (
          <span>
            {r.name && <div style={{ fontWeight: 600 }}>{r.name}</div>}
            <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: r.name ? muted : undefined }}>{r.id}</span>
          </span>
        ),
    },
    {
      title: 'Runs',
      key: 'runnable',
      render: (_: unknown, r) => (
        <div style={{ fontFamily: MONO_FONT, fontSize: 13, maxWidth: 320 }}>
          <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.runnable || <span style={{ color: muted }}>—</span>}
          </div>
          {r.argsText && (
            <Tooltip title={r.argsText}>
              <div style={{ color: muted, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.argsText}
              </div>
            </Tooltip>
          )}
        </div>
      ),
    },
    { title: 'When', key: 'when', render: (_: unknown, r) => <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{r.when}</span> },
    {
      title: 'Next fire',
      key: 'next',
      render: (_: unknown, r) => r.nextRun
        ? <Tooltip title={new Date(r.nextRun).toLocaleString()}><span style={{ color: secondary }}>{relTime(r.nextRun)}</span></Tooltip>
        : <span style={{ color: muted }}>—</span>,
    },
    {
      title: 'Last run',
      key: 'last',
      render: (_: unknown, r) => {
        const l = r.lastRun
        if (!l) return <span style={{ color: muted }}>—</span>
        const label = (l.status === 'failed' || l.status === 'crashed') && l.exitCode != null ? `exit ${l.exitCode}` : l.status
        return (
          <Space size={4}>
            <Tag color={RUN_COLOR[l.status] ?? 'default'} style={{ margin: 0 }}>{label}</Tag>
            <Tooltip title={`${new Date(l.ts).toLocaleString()}${l.durationMs != null ? ` · ${(l.durationMs / 1000).toFixed(1)}s` : ''}`}>
              <span style={{ color: secondary, fontSize: 13 }}>{relTime(l.ts)}</span>
            </Tooltip>
            {onOpenHistory && r.historySearch && (
              <Tooltip title={`Open in History (search “${r.historySearch}”)`}>
                <Button type="text" size="small" icon={<HistoryOutlined />} aria-label="Open in History"
                  onClick={() => onOpenHistory(r.historySearch)} />
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: 'Where',
      key: 'where',
      render: (_: unknown, r) => (
        <div style={{ fontFamily: MONO_FONT, fontSize: 13 }}>
          <div style={{ whiteSpace: 'nowrap' }}>{r.host}{r.venv ? ` / ${r.venv}` : ''}</div>
          {(r.owner || r.runAs) && (
            <Tooltip title={`Venv owner${r.runAs ? ' → runs as' : ''}`}>
              <div style={{ color: muted, fontSize: 12, whiteSpace: 'nowrap' }}>
                {r.owner || '?'}{r.runAs && r.runAs !== r.owner ? ` → ${r.runAs}` : ''}
              </div>
            </Tooltip>
          )}
        </div>
      ),
    },
  ]

  const expanded = (r: ScheduleRow) => {
    if (r.shared) return <ScheduleDetail s={r.shared} daemon={r.source === 'daemon'} />
    if (r.kind === 'unreadable') return <Alert type="error" showIcon message="Couldn't read this venv's schedules" description={r.error} />
    if (r.kind === 'stale') return <Text type="secondary" style={{ fontSize: 13 }}>{r.stateTip}</Text>
    return (
      <div style={{ paddingLeft: 8, fontFamily: MONO_FONT, fontSize: 13, color: muted }}>
        {r.argsText || 'No arguments.'}
      </div>
    )
  }

  const selectStyle = { minWidth: 130 }
  const emptyText = !loaded && scope !== 'console'
    ? 'Reading schedules…'
    : scope === 'daemon' && collected.daemons.length === 0
      ? 'No venv uses the scheduler daemon yet. A venv moves to it on the host with runspec-scheduler 0.5.0 or later.'
      : narrowed ? 'No schedule matches these filters.' : 'No schedules.'

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <Input.Search
          allowClear
          placeholder="Search id, runnable, args · host:web group:prod"
          value={filters.search}
          onChange={e => setFilters({ search: e.target.value })}
          style={{ width: 300 }}
        />
        {scope === 'all' && (
          <Select mode="multiple" allowClear placeholder="Source" style={selectStyle} maxTagCount="responsive"
            value={filters.source} onChange={v => setFilters({ source: v })}
            options={[
              { label: 'This console', value: 'console' },
              { label: 'Host cron', value: 'cron' },
              { label: 'Host daemon', value: 'daemon' },
              { label: 'Unreadable venv', value: 'unknown' },
            ]} />
        )}
        <Select mode="multiple" allowClear placeholder="State" style={selectStyle} maxTagCount="responsive"
          value={filters.states} onChange={v => setFilters({ states: v })}
          options={states.map(s => ({ label: s, value: s }))} />
        {scope !== 'console' && (
          <>
            <Select mode="multiple" allowClear placeholder="Owner" style={selectStyle} maxTagCount="responsive"
              value={filters.owners} onChange={v => setFilters({ owners: v })}
              options={[...new Set([...owners, ...filters.owners])].map(o => ({ label: o, value: o }))} />
            <Select mode="multiple" allowClear placeholder="Run as" style={selectStyle} maxTagCount="responsive"
              value={filters.runAs} onChange={v => setFilters({ runAs: v })}
              options={[...new Set([...runAs, ...filters.runAs])].map(o => ({ label: o, value: o }))} />
          </>
        )}
        <Space size={6}>
          <Switch size="small" checked={filters.onlyProblems} onChange={v => setFilters({ onlyProblems: v })} aria-label="Only problems" />
          <span style={{ fontSize: 13, color: secondary }}>Only problems</span>
        </Space>
        <Select size="middle" value={filters.sort} onChange={v => setFilters({ sort: v })} style={{ width: 170 }}
          options={[
            { label: 'Sort: next fire', value: 'next' },
            { label: 'Sort: last run', value: 'last' },
            { label: 'Sort: host', value: 'host' },
            { label: 'Sort: runnable', value: 'runnable' },
          ]} />
        {scope !== 'console' && (
          <Space size={8} style={{ marginLeft: 'auto' }}>
            {activeProfile && (
              <Tooltip title={`Host schedules come from the connected hosts in the active profile "${activeProfile}" (and the local venvs).`}>
                <Tag style={{ margin: 0 }}>profile: {activeProfile}</Tag>
              </Tooltip>
            )}
            <span style={{ fontSize: 12, color: muted }}>
              {hostCount} host{hostCount === 1 ? '' : 's'}{updatedAt ? ` · read ${Date.now() - updatedAt < 10_000 ? 'just now' : relTime(new Date(Math.min(updatedAt, Date.now())).toISOString())}` : ''}
            </span>
            <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>Refresh</Button>
          </Space>
        )}
      </div>

      {daemons.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
          {daemons.map(d => <DaemonStrip key={d.key} d={d} />)}
        </div>
      )}

      {notes.map(n => (
        <Alert key={n.key} type="error" showIcon style={{ marginBottom: 8 }}
          message={<span><span style={{ fontFamily: MONO_FONT }}>{n.host} / {n.venv}</span>: {n.message}</span>} />
      ))}

      {rows.length === 0 && !loaded && scope !== 'console' ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spin /></div>
      ) : (
        <Table<ScheduleRow>
          dataSource={rows}
          columns={columns}
          rowKey="key"
          size="small"
          pagination={rows.length > 100 ? { pageSize: 100, showSizeChanger: false } : false}
          scroll={{ x: 'max-content' }}
          rowClassName={r => (r.enabled ? '' : 'rs-schedule-disabled')}
          onRow={r => (r.problem ? { style: { background: withAlpha(token.colorWarning, isDark ? 0.07 : 0.08) } } : {})}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} /> }}
          expandable={{ expandedRowRender: expanded, rowExpandable: r => r.kind !== 'schedule' || !!r.shared || !!r.argsText }}
        />
      )}

      {scope !== 'console' && collected.without.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: muted }}>
          No runspec-scheduler in {collected.without.length} venv{collected.without.length === 1 ? '' : 's'}: {collected.without.join(', ')}. Install it into a venv from the Marketplace to schedule that venv's runnables.
        </div>
      )}
      {scope !== 'console' && (
        <div style={{ marginTop: 6, fontSize: 12, color: muted }}>
          <InfoCircleOutlined style={{ marginRight: 4 }} />
          Read-only: host schedules are managed on the host with <Text code style={{ fontSize: 12 }}>runspec-scheduler</Text>.
        </div>
      )}
    </div>
  )
}
