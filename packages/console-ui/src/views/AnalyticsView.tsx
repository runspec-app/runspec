import { useContext, useEffect, useMemo, useState } from 'react'
import { Row, Col, Card, Statistic, Select, Segmented, Table, Tag, Tooltip, Alert, Empty, Spin, Typography } from 'antd'
import { ReloadOutlined, WarningOutlined } from '@ant-design/icons'
import { Area, Line, Column, Pie } from '@ant-design/plots'
import type { ColumnType } from 'antd/es/table'
import { bridge, type AnalyticsData, type AnalyticsBucket, type AnalyticsException } from '../bridge'
import { ThemeContext, useAccent } from '../ThemeContext'
import {
  filterBuckets, computeKpis, runsOverTime, successRateOverTime,
  durationTrend, topRunnables, operatorBreakdown, initiatedBreakdown,
} from './analytics'

const { Text } = Typography

// Status palette; the series accent comes from useAccent() so charts follow a
// white-label accent_color too, not just the shell chrome.
const SUCCESS = '#52c41a'
const FAILURE = '#ff4d4f'
const WARNING = '#faad14'

// At most 30 days — the History cache's window, which remote hosts are
// aggregated from (the backend clamps a wider request to 30 too).
const RANGE_OPTIONS = [
  { label: '7d', value: 7 },
  { label: '30d', value: 30 },
]

interface AnalyticsViewProps {
  selectedHost: string
  activeScope: string[]
  active: boolean
}

export function AnalyticsView({ selectedHost, activeScope, active }: AnalyticsViewProps) {
  const isDark = useContext(ThemeContext)
  const ACCENT = useAccent()
  const BRAND_RANGE = useMemo(
    () => [ACCENT, '#722ed1', WARNING, '#13c2c2', '#eb2f96', SUCCESS, '#fa8c16', '#2f54eb'],
    [ACCENT],
  )
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [loading, setLoading] = useState(true)

  // Fetch-scope filters — changing these refetches (server recomputes
  // percentiles + the daily series for the new scope).
  const [sinceDays, setSinceDays] = useState(30)
  const [runnableFilter, setRunnableFilter] = useState<string | undefined>(undefined)

  // Client-side filters — applied instantly over the `buckets` grain.
  const [groupFilter, setGroupFilter] = useState<string[]>([])
  const [operatorFilter, setOperatorFilter] = useState<string[]>([])
  const [runbookFilter, setRunbookFilter] = useState<string[]>([])
  const [initiatedBy, setInitiatedBy] = useState<'all' | 'user' | 'llm'>('all')

  const plotTheme = isDark ? 'classicDark' : 'classic'

  useEffect(() => {
    let cancelled = false
    let inFlight = false
    // Analytics is a per-host drill off the Logs tab, so the host IS the sidebar
    // selection (same model as History) — one host's scan, not a fleet-wide
    // fan-out. There's deliberately no separate host picker here; it duplicated
    // the sidebar selection.
    const hostsArg = selectedHost ? [selectedHost] : []
    // The view stays mounted across tab switches (App keeps it alive once first
    // opened), so `data` persists — returning shows the dashboard instantly.
    // Poll only while visible so a hidden tab isn't running the 45 s fleet scan
    // in the background.
    if (hostsArg.length === 0 || !active) { setLoading(false); return }
    const load = () => {
      // Skip a scheduled refresh while one is still running, and always clear
      // loading (even on failure) so a rejected scan shows the empty state
      // instead of spinning forever.
      if (inFlight) return
      inFlight = true
      bridge.get_analytics(hostsArg, sinceDays, runnableFilter)
        .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
        .catch(() => { if (!cancelled) setLoading(false) })
        .finally(() => { inFlight = false })
    }
    setLoading(true)
    load()
    const id = setInterval(load, 45000)
    return () => { cancelled = true; clearInterval(id) }
  }, [sinceDays, runnableFilter, selectedHost, active])

  // activeScope (sidebar group tags) acts as an implicit group filter when the
  // view's own group filter is empty.
  const effectiveGroups = groupFilter.length > 0 ? groupFilter : activeScope

  const filteredBuckets = useMemo<AnalyticsBucket[]>(
    () => data ? filterBuckets(data.buckets, effectiveGroups, operatorFilter, initiatedBy, runbookFilter) : [],
    [data, effectiveGroups, operatorFilter, initiatedBy, runbookFilter],
  )

  const clientFiltersActive =
    effectiveGroups.length > 0 || operatorFilter.length > 0 || initiatedBy !== 'all' || runbookFilter.length > 0

  // ── derived series (pure helpers in ./analytics, unit-tested separately) ────
  const kpis = useMemo(() => computeKpis(filteredBuckets), [filteredBuckets])
  const runsSeries = useMemo(
    () => runsOverTime(data?.daily ?? [], filteredBuckets), [data, filteredBuckets])
  const rateSeries = useMemo(
    () => successRateOverTime(data?.daily ?? [], filteredBuckets), [data, filteredBuckets])
  const durationSeries = useMemo(() => durationTrend(data?.daily ?? []), [data])
  const topRuns = useMemo(() => topRunnables(filteredBuckets), [filteredBuckets])
  const operators = useMemo(() => operatorBreakdown(filteredBuckets), [filteredBuckets])
  const initiated = useMemo(() => initiatedBreakdown(filteredBuckets), [filteredBuckets])

  if (loading && !data) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (!data) return <Empty description="No analytics data" />

  const dims = data.dimensions
  const cardStyle = { background: 'var(--rs-surface-raised)', borderColor: 'var(--rs-border)' }
  const chartHeight = 240
  // Clicking a column/bar/slice drills into that runnable.
  const drillToRunnable = ({ chart }: { chart: { on: (e: string, cb: (evt: { data?: { data?: Record<string, unknown> } }) => void) => void } }) => {
    chart.on('element:click', (evt) => {
      const datum = evt?.data?.data
      const run = datum?.runnable
      if (typeof run === 'string') setRunnableFilter(run)
    })
  }

  const exceptionColumns: ColumnType<AnalyticsException>[] = [
    { title: 'Exception', dataIndex: 'exception', key: 'exception',
      render: (e: string) => <Text style={{ fontSize: 13 }}>{e}</Text> },
    { title: 'Runnable', dataIndex: 'runnable', key: 'runnable',
      render: (r: string) => <Tag color="blue" style={{ cursor: 'pointer' }} onClick={() => setRunnableFilter(r)}>{r}</Tag> },
    { title: 'Host', dataIndex: 'host', key: 'host', render: (h: string) => <Tag>{h}</Tag> },
    { title: 'Count', dataIndex: 'count', key: 'count', align: 'right',
      sorter: (a, b) => a.count - b.count, defaultSortOrder: 'descend',
      render: (c: number) => <Tag color="red">{c}</Tag> },
    { title: 'Last seen', dataIndex: 'lastTs', key: 'lastTs',
      render: (t: string) => <Text type="secondary" style={{ fontSize: 13 }}>{new Date(t).toLocaleString()}</Text> },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Filter bar */}
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10 }}>
        <Segmented
          options={RANGE_OPTIONS}
          value={sinceDays}
          onChange={(v) => setSinceDays(v as number)}
        />
        <Select
          mode="multiple" allowClear placeholder={activeScope.length ? `scope: ${activeScope.join(', ')}` : 'All groups'}
          style={{ minWidth: 160 }} value={groupFilter} onChange={setGroupFilter}
          options={dims.groups.map(g => ({ label: g, value: g }))}
          maxTagCount="responsive"
        />
        <Select
          allowClear placeholder="All runnables"
          style={{ minWidth: 170 }} value={runnableFilter} onChange={setRunnableFilter}
          options={dims.runnables.map(r => ({ label: r, value: r }))}
          showSearch
        />
        <Select
          mode="multiple" allowClear placeholder="All operators"
          style={{ minWidth: 170 }} value={operatorFilter} onChange={setOperatorFilter}
          options={dims.operators.map(o => ({ label: o, value: o }))}
          maxTagCount="responsive"
        />
        {(dims.runbooks?.length ?? 0) > 0 && (
          <Select
            mode="multiple" allowClear placeholder="All runbooks"
            style={{ minWidth: 170 }} value={runbookFilter} onChange={setRunbookFilter}
            options={dims.runbooks.map(s => ({ label: `$${s}`, value: s }))}
            maxTagCount="responsive"
          />
        )}
        <Segmented
          options={[{ label: 'All', value: 'all' }, { label: 'User', value: 'user' }, { label: 'Agent', value: 'llm' }]}
          value={initiatedBy}
          onChange={(v) => setInitiatedBy(v as 'all' | 'user' | 'llm')}
        />
        {loading && <Spin size="small" indicator={<ReloadOutlined spin />} />}
        <div style={{ flex: 1 }} />
        <Text type="secondary" style={{ fontSize: 13 }}>
          {data.since} → {data.until}
        </Text>
      </div>

      {data.partial && (
        <Alert
          type="warning" showIcon icon={<WarningOutlined />}
          message="Partial data — some hosts could not be scanned"
          description={data.errors.map(e => `${e.host}: ${e.message}`).join('  •  ')}
        />
      )}

      {(data.eventsUnknown?.length ?? 0) > 0 && (
        <Alert
          type="info" showIcon
          message="Log events and exceptions are incomplete for some hosts"
          description={`Some runs on ${data.eventsUnknown!.join(', ')} were recorded by a runspec older than 0.64, which didn't store their log-event counts or exception in the runs index. Upgrade runspec on the host; runs recorded after that are complete.`}
        />
      )}

      {/* KPI cards */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}><Card size="small" style={cardStyle}><Statistic title="Total runs" value={kpis.total} valueStyle={{ color: ACCENT }} /></Card></Col>
        <Col xs={12} sm={6}><Card size="small" style={cardStyle}><Statistic title="Success rate" value={kpis.rate} precision={1} suffix="%" valueStyle={{ color: kpis.rate >= 95 ? SUCCESS : kpis.rate >= 80 ? WARNING : FAILURE }} /></Card></Col>
        <Col xs={12} sm={6}>
          <Tooltip title={clientFiltersActive ? 'Duration reflects the host/runnable/date scope, not the group/operator/agent filters' : ''}>
            <Card size="small" style={cardStyle}><Statistic title="Duration p50" value={(data.durationMs.p50 / 1000).toFixed(2)} suffix="s" /></Card>
          </Tooltip>
        </Col>
        <Col xs={12} sm={6}>
          <Tooltip title={clientFiltersActive ? 'Duration reflects the host/runnable/date scope, not the group/operator/agent filters' : ''}>
            <Card size="small" style={cardStyle}><Statistic title="Duration p95" value={(data.durationMs.p95 / 1000).toFixed(2)} suffix="s" valueStyle={{ color: WARNING }} /></Card>
          </Tooltip>
        </Col>
      </Row>

      {/* Row 1: runs over time + success rate */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card size="small" title="Runs over time" style={cardStyle}>
            {kpis.total === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Area
                data={runsSeries} xField="date" yField="value" colorField="status"
                stack height={chartHeight} theme={plotTheme} legend={{ color: { position: 'top' } }}
                scale={{ color: { domain: ['success', 'failure'], range: [SUCCESS, FAILURE] } }}
                style={{ fillOpacity: 0.6 }} axis={{ x: { labelAutoHide: true } }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card size="small" title="Success rate %" style={cardStyle}>
            {kpis.total === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Line
                data={rateSeries.filter(d => d.rate !== null)} xField="date" yField="rate"
                height={chartHeight} theme={plotTheme} shapeField="smooth"
                scale={{ y: { domain: [0, 100] } }} style={{ stroke: ACCENT }}
                axis={{ x: { labelAutoHide: true }, y: { labelFormatter: (v: number) => `${v}%` } }}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* Row 2: top runnables + duration trend */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card size="small" title="Top runnables" extra={<Text type="secondary" style={{ fontSize: 12 }}>click to drill</Text>} style={cardStyle}>
            {topRuns.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Column
                data={topRuns} xField="runnable" yField="runs"
                height={chartHeight} theme={plotTheme} onReady={drillToRunnable}
                style={{ fill: ACCENT }} axis={{ x: { labelAutoRotate: true } }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="Duration p50 / p95 (ms)" style={cardStyle}>
            {kpis.total === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Line
                data={durationSeries} xField="date" yField="value" colorField="metric"
                height={chartHeight} theme={plotTheme} shapeField="smooth"
                scale={{ color: { domain: ['p50', 'p95'], range: [ACCENT, WARNING] } }}
                legend={{ color: { position: 'top' } }} axis={{ x: { labelAutoHide: true } }}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* Row 3: operator + initiatedBy */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card size="small" title="Runs by operator" style={cardStyle}>
            {operators.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Pie
                data={operators} angleField="value" colorField="type"
                height={chartHeight} theme={plotTheme} innerRadius={0.5}
                scale={{ color: { range: BRAND_RANGE } }} legend={{ color: { position: 'right' } }}
                label={{ text: 'value', position: 'inside' }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="User vs agent" style={cardStyle}>
            {kpis.total === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
              <Pie
                data={initiated} angleField="value" colorField="type"
                height={chartHeight} theme={plotTheme} innerRadius={0.5}
                scale={{ color: { domain: ['user', 'agent'], range: [ACCENT, '#722ed1'] } }}
                legend={{ color: { position: 'right' } }} label={{ text: 'value', position: 'inside' }}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* Row 4: exceptions */}
      <Card size="small" title="Top exceptions" style={cardStyle}>
        <Table
          dataSource={data.exceptions}
          columns={exceptionColumns}
          rowKey={(r) => `${r.exception}|${r.runnable}|${r.host}`}
          size="small"
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: 'No exceptions in this window 🎉' }}
        />
      </Card>
    </div>
  )
}
