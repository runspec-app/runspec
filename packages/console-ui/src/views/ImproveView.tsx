import { useEffect, useMemo, useState } from 'react'
import {
  Row, Col, Card, Statistic, Button, Input, InputNumber, Space, Table, Tag,
  Typography, Alert, Empty, Spin, message, Tooltip, Segmented,
} from 'antd'
import {
  ReloadOutlined, ExperimentOutlined, BulbOutlined, MailOutlined, DeleteOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge, type ImproveReport, type ImproveReportMeta, type ImproveIncident,
} from '../bridge'

const { Text, Paragraph, Title } = Typography

// Incident category → colour + label. Kept in sync with improve.py's CAT_* set.
const CAT_META: Record<string, { color: string; label: string }> = {
  durable_error: { color: 'red', label: 'Durable error' },
  stop: { color: 'volcano', label: 'Stopped (paused)' },
  run_failure: { color: 'red', label: 'Run failure' },
  arg_bounce: { color: 'orange', label: 'Arg bounce' },
  recovery: { color: 'gold', label: 'Self-recovery' },
  empty_search: { color: 'blue', label: 'Empty search' },
  autonomy_denied: { color: 'purple', label: 'Denied' },
  autonomy_edited: { color: 'geekblue', label: 'Edited' },
}

interface ImproveViewProps {
  active: boolean
}

export function ImproveView({ active }: ImproveViewProps) {
  const [reports, setReports] = useState<ImproveReportMeta[]>([])
  const [report, setReport] = useState<ImproveReport | null>(null)
  const [windowDays, setWindowDays] = useState<number>(30)
  const [comment, setComment] = useState('')
  const [running, setRunning] = useState(false)
  const [synthing, setSynthing] = useState(false)
  const [loadedOnce, setLoadedOnce] = useState(false)

  const refreshList = useMemo(
    () => async () => {
      try {
        setReports(await bridge.list_improve_reports())
      } catch {
        /* best-effort */
      }
    },
    [],
  )

  useEffect(() => {
    if (active && !loadedOnce) {
      setLoadedOnce(true)
      refreshList()
    }
  }, [active, loadedOnce, refreshList])

  async function run(withComment: boolean) {
    setRunning(true)
    try {
      const rep = await bridge.run_improve_report(windowDays, withComment ? comment : '')
      setReport(rep)
      if (withComment) setComment('')
      await refreshList()
      message.success(`Report generated — ${rep.metrics?.turns ?? 0} turn(s) analysed`)
    } catch (e) {
      message.error(`Could not generate report: ${e}`)
    } finally {
      setRunning(false)
    }
  }

  async function open(id: string) {
    const rep = await bridge.load_improve_report(id)
    if (rep) setReport(rep)
  }

  async function synthesize() {
    if (!report) return
    setSynthing(true)
    try {
      const rep = await bridge.synthesize_improve_report(report.id)
      setReport(rep)
      if (rep.synthesis_error) message.warning(rep.synthesis_error)
      else message.success('Recommendations synthesized')
      await refreshList()
    } catch (e) {
      message.error(`Synthesis failed: ${e}`)
    } finally {
      setSynthing(false)
    }
  }

  async function emailDevelopers() {
    if (!report) return
    if (!report.developer_emails?.length) {
      message.warning('No developer emails configured — set [improve] developer_emails in config.')
      return
    }
    try {
      await bridge.improve_mailto(report.id)
    } catch (e) {
      message.error(`Could not open mail client: ${e}`)
    }
  }

  async function remove(id: string) {
    await bridge.delete_improve_report(id)
    if (report?.id === id) setReport(null)
    await refreshList()
  }

  if (!active) return null

  return (
    <div style={{ padding: 16, overflow: 'auto', height: '100%' }}>
      <Title level={4} style={{ marginTop: 0 }}>
        <BulbOutlined /> Improve
      </Title>
      <Paragraph type="secondary" style={{ maxWidth: 720 }}>
        Measure how effective the agent, its runnables, and the app are — mined from this
        console's own saved chats and run history (tool search, argument bounces, stopped
        turns, errors, autonomy denials). Run an evaluation, or file a problem report with a
        comment. Reports are addressed to the configured developers; use the mail button to
        hand one off.
      </Paragraph>

      <Row gutter={16}>
        {/* ── controls ── */}
        <Col xs={24} md={7}>
          <Card size="small" title="Report a problem" style={{ marginBottom: 12 }}>
            <Input.TextArea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Describe what went wrong — e.g. 'the agent keeps picking the wrong host'. Runs the evaluation alongside your comment."
              autoSize={{ minRows: 3, maxRows: 8 }}
            />
            <Button
              type="primary"
              block
              icon={<ExperimentOutlined />}
              style={{ marginTop: 8 }}
              loading={running}
              disabled={!comment.trim()}
              onClick={() => run(true)}
            >
              Run with my comment
            </Button>
          </Card>

          <Card size="small" title="Evaluation sweep">
            <Space>
              <Text>Window</Text>
              <InputNumber
                min={1}
                max={365}
                value={windowDays}
                onChange={(v) => setWindowDays(Number(v) || 30)}
                addonAfter="days"
              />
            </Space>
            <Button
              block
              icon={<ExperimentOutlined />}
              style={{ marginTop: 8 }}
              loading={running}
              onClick={() => run(false)}
            >
              Run evaluation
            </Button>
          </Card>

          <Card
            size="small"
            title="Saved reports"
            style={{ marginTop: 12 }}
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={refreshList} />}
          >
            {reports.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No reports yet" />
            ) : (
              <div style={{ maxHeight: 260, overflow: 'auto' }}>
                {reports.map((r) => (
                  <div
                    key={r.id}
                    onClick={() => open(r.id)}
                    style={{
                      cursor: 'pointer',
                      padding: '6px 4px',
                      borderRadius: 4,
                      background: report?.id === r.id ? 'rgba(127,127,127,0.12)' : undefined,
                    }}
                  >
                    <Space size={4}>
                      <Tag color={r.kind === 'problem_report' ? 'volcano' : 'blue'}>
                        {r.kind === 'problem_report' ? 'problem' : 'eval'}
                      </Tag>
                      {r.has_synthesis && <Tag color="green">rec</Tag>}
                      <Text style={{ fontSize: 12 }}>{r.ts?.slice(0, 16).replace('T', ' ')}</Text>
                      <Button
                        size="small"
                        type="text"
                        icon={<DeleteOutlined />}
                        onClick={(e) => {
                          e.stopPropagation()
                          remove(r.id)
                        }}
                      />
                    </Space>
                    {r.comment && (
                      <div>
                        <Text type="secondary" ellipsis style={{ fontSize: 12 }}>
                          {r.comment}
                        </Text>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Col>

        {/* ── report ── */}
        <Col xs={24} md={17}>
          {running ? (
            <Card><Spin /> Analysing…</Card>
          ) : !report ? (
            <Card>
              <Empty description="Run an evaluation or file a problem report to see the analysis." />
            </Card>
          ) : (
            <ReportPanel
              report={report}
              synthing={synthing}
              onSynthesize={synthesize}
              onEmail={emailDevelopers}
            />
          )}
        </Col>
      </Row>
    </div>
  )
}

function pct(n: number | undefined): string {
  return `${n ?? 0}%`
}

function ReportPanel({
  report, synthing, onSynthesize, onEmail,
}: {
  report: ImproveReport
  synthing: boolean
  onSynthesize: () => void
  onEmail: () => void
}) {
  const m = report.metrics || {}
  const s = report.scores || {}

  const incidentCols: ColumnType<ImproveIncident>[] = [
    {
      title: 'Category',
      dataIndex: 'category',
      width: 140,
      render: (c: string) => {
        const meta = CAT_META[c] || { color: 'default', label: c }
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
      filters: Object.entries(CAT_META).map(([k, v]) => ({ text: v.label, value: k })),
      onFilter: (v, r) => r.category === v,
    },
    {
      title: 'Runnable / tool',
      width: 160,
      render: (_: unknown, r) => r.runnable || r.tool || '—',
    },
    {
      title: 'Evidence',
      dataIndex: 'evidence',
      render: (e: string) => <Text style={{ fontSize: 12 }}>{e}</Text>,
    },
  ]

  const recs = report.synthesis?.recommendations || []

  return (
    <>
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={
          <Space>
            <Tag color={report.kind === 'problem_report' ? 'volcano' : 'blue'}>
              {report.kind === 'problem_report' ? 'Problem report' : 'Evaluation'}
            </Tag>
            <Text type="secondary">
              {report.product_name} · last {report.window_days}d · {m.turns ?? 0} turn(s)
            </Text>
          </Space>
        }
        extra={
          <Space>
            <Button
              icon={<BulbOutlined />}
              loading={synthing}
              onClick={onSynthesize}
            >
              Synthesize recommendations
            </Button>
            <Tooltip
              title={
                report.developer_emails?.length
                  ? `Email ${report.developer_emails.join(', ')}`
                  : 'No developer_emails configured'
              }
            >
              <Button
                type="primary"
                icon={<MailOutlined />}
                disabled={!report.developer_emails?.length}
                onClick={onEmail}
              >
                Email developers
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {report.comment && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="Reported problem"
            description={report.comment}
          />
        )}
        <Row gutter={16}>
          <Col span={6}>
            <Statistic title="Arg-bounce rate" value={pct(s.arg_bounce_rate_pct)} />
          </Col>
          <Col span={6}>
            <Statistic title="Stops (paused)" value={m.stops ?? 0} />
          </Col>
          <Col span={6}>
            <Statistic title="Durable errors" value={m.durable_errors ?? 0} />
          </Col>
          <Col span={6}>
            <Statistic title="Run-history failures" value={pct(s.history_failure_rate_pct)} />
          </Col>
        </Row>
        <Row gutter={16} style={{ marginTop: 12 }}>
          <Col span={6}>
            <Statistic title="Searches" value={m.searches ?? 0} />
          </Col>
          <Col span={6}>
            <Statistic title="Empty searches" value={pct(s.empty_search_rate_pct)} />
          </Col>
          <Col span={6}>
            <Statistic title="Search before run" value={pct(s.search_before_run_pct)} />
          </Col>
          <Col span={6}>
            <Statistic
              title="Autonomy denied/edited"
              value={`${m.autonomy_denied ?? 0} / ${m.autonomy_edited ?? 0}`}
            />
          </Col>
        </Row>
      </Card>

      {report.synthesis_error && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={report.synthesis_error} />
      )}

      {(report.synthesis?.summary || recs.length > 0) && (
        <Card size="small" title="Recommendations" style={{ marginBottom: 12 }}>
          {report.synthesis?.summary && <Paragraph>{report.synthesis.summary}</Paragraph>}
          {report.synthesis?.correlation && (
            <Paragraph type="secondary">
              <b>Correlation:</b> {report.synthesis.correlation}
            </Paragraph>
          )}
          {recs.map((r, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <Space size={4} wrap>
                <Tag color="cyan">{r.category}</Tag>
                <Text strong>{r.target}</Text>
                <Tag>{r.confidence}</Tag>
              </Space>
              <div>
                <Text type="secondary">{r.why}</Text>
              </div>
              <div>→ {r.fix}</div>
            </div>
          ))}
        </Card>
      )}

      <Card
        size="small"
        title={`Flagged incidents (${report.incident_shown} of ${report.incident_total})`}
        style={{ marginBottom: 12 }}
      >
        <Table
          size="small"
          rowKey={(r) => `${r.conversation_id}-${r.turn_index}-${r.category}-${r.evidence.slice(0, 12)}`}
          dataSource={report.incidents}
          columns={incidentCols}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
        />
      </Card>

      {report.learnings_toml && (
        <Card
          size="small"
          title="Portable learnings (for your Config Sync repo / seed)"
          extra={
            <Segmented
              options={[{ label: 'Copy', value: 'copy' }]}
              onChange={() => {
                navigator.clipboard?.writeText(report.learnings_toml || '')
                message.success('Copied learnings TOML')
              }}
            />
          }
        >
          <pre style={{ maxHeight: 220, overflow: 'auto', fontSize: 12, margin: 0 }}>
            {report.learnings_toml}
          </pre>
        </Card>
      )}
    </>
  )
}
