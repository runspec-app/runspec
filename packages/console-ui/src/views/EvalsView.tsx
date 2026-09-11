import { useEffect, useMemo, useState } from 'react'
import {
  Row, Col, Card, Statistic, Button, Input, InputNumber, Select, Space, Switch,
  Table, Tag, Typography, Empty, Spin, message, Tooltip,
} from 'antd'
import {
  ReloadOutlined, ExperimentOutlined, ThunderboltOutlined, SaveOutlined,
  DeleteOutlined, PlusOutlined,
} from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import {
  bridge, type EvalReport, type EvalReportMeta, type EvalCaseResult,
  type EvalFixtureMeta,
} from '../bridge'

const { Text, Paragraph, Title } = Typography

// Verdict category → colour + label. Keys mirror evals.py CAT_* plus the
// improve.classify_bounce arg-bounce buckets (missing_required / unknown_arg /
// bad_choice / out_of_range / bad_command), so a failing eval reads the same as
// an Improve incident. Exported for the colocated test.
export const EVAL_CAT_META: Record<string, { color: string; label: string }> = {
  pass: { color: 'green', label: 'Pass' },
  wrong_tool: { color: 'red', label: 'Wrong tool' },
  wrong_host: { color: 'volcano', label: 'Wrong host' },
  wrong_args: { color: 'orange', label: 'Wrong args' },
  no_run: { color: 'gold', label: 'No run' },
  unexpected_run: { color: 'magenta', label: 'Unexpected run' },
  forbidden_tool: { color: 'red', label: 'Forbidden tool' },
  missing_required: { color: 'orange', label: 'Missing required arg' },
  unknown_arg: { color: 'orange', label: 'Unknown arg' },
  bad_choice: { color: 'orange', label: 'Bad choice' },
  out_of_range: { color: 'orange', label: 'Out of range' },
  bad_command: { color: 'orange', label: 'Bad command' },
  other: { color: 'default', label: 'Other' },
}

export function evalCatMeta(category: string): { color: string; label: string } {
  return EVAL_CAT_META[category] || { color: 'default', label: category }
}

const STARTER_FIXTURE = `# A tool-selection eval suite. Each [[eval]] is a fixed prompt + the runnable
# the agent SHOULD pick (with which args) — scored without executing anything.

[[eval]]
id = "example-restart"
prompt = "restart nginx on web-01"
expect_tool = "restart-service"
expect_host = "web-01"
expect_args = { service = "nginx" }

[[eval]]
id = "example-ambiguous"
prompt = "delete it"
expect_no_run = true   # ambiguous → the agent should ask, not guess
`

interface EvalsViewProps {
  active: boolean
}

export function EvalsView({ active }: EvalsViewProps) {
  const [fixtures, setFixtures] = useState<EvalFixtureMeta[]>([])
  const [fixtureId, setFixtureId] = useState('')
  const [fixtureText, setFixtureText] = useState('')
  const [samples, setSamples] = useState(1)
  const [validate, setValidate] = useState(false)
  const [telemetry, setTelemetry] = useState(false)
  const [running, setRunning] = useState(false)
  const [reports, setReports] = useState<EvalReportMeta[]>([])
  const [report, setReport] = useState<EvalReport | null>(null)
  const [loadedOnce, setLoadedOnce] = useState(false)

  const refresh = useMemo(
    () => async () => {
      try {
        const [fx, rs] = await Promise.all([
          bridge.list_eval_fixtures(),
          bridge.list_eval_reports(),
        ])
        setFixtures(fx)
        setReports(rs)
      } catch {
        /* best-effort */
      }
    },
    [],
  )

  useEffect(() => {
    if (active && !loadedOnce) {
      setLoadedOnce(true)
      refresh()
    }
  }, [active, loadedOnce, refresh])

  async function selectFixture(id: string) {
    setFixtureId(id)
    if (!id) return
    try {
      setFixtureText(await bridge.get_eval_fixture_text(id))
    } catch {
      /* best-effort */
    }
  }

  async function saveFixture() {
    const id = fixtureId.trim()
    if (!id) {
      message.warning('Name the fixture first')
      return
    }
    const res = await bridge.save_eval_fixture(id, fixtureText)
    if (res.ok) {
      message.success(`Saved ${id} (${res.cases} case${res.cases === 1 ? '' : 's'})`)
      await refresh()
    } else {
      message.error(res.reason || 'Could not save fixture')
    }
  }

  async function deleteFixture() {
    if (!fixtureId) return
    await bridge.delete_eval_fixture(fixtureId)
    setFixtureId('')
    setFixtureText('')
    await refresh()
  }

  async function run() {
    if (!fixtureText.trim()) {
      message.warning('Write or select a fixture first')
      return
    }
    setRunning(true)
    try {
      const res = await bridge.run_evals(fixtureText, '', samples, validate, telemetry)
      if (res.ok) {
        setReport(res)
        await refresh()
        const s = res.summary
        message.success(`${s.passed}/${s.total} passed (${Math.round(s.pass_rate * 100)}%)`)
      } else {
        message.error(res.reason)
      }
    } catch (e) {
      message.error(`Eval run failed: ${e}`)
    } finally {
      setRunning(false)
    }
  }

  async function openReport(id: string) {
    const rep = await bridge.load_eval_report(id)
    if (rep) setReport(rep)
  }

  async function removeReport(id: string) {
    await bridge.delete_eval_report(id)
    if (report?.id === id) setReport(null)
    await refresh()
  }

  if (!active) return null

  return (
    <div style={{ padding: 16, overflow: 'auto', height: '100%' }}>
      <Title level={4} style={{ marginTop: 0 }}>
        <ExperimentOutlined /> Evals
      </Title>
      <Paragraph type="secondary" style={{ maxWidth: 760 }}>
        Check which runnable the agent <i>would</i> pick — and with which arguments — for a
        fixed set of prompts, run against this console's real toolset and configured model,
        <b> without executing anything</b>. The proactive twin of Improve: write a suite of
        expectations, run it, and catch a regression before it ships. Turn on telemetry to send
        each case as a trace to your OTLP backend (Phoenix, LangSmith, …).
      </Paragraph>

      <Row gutter={16}>
        {/* ── controls ── */}
        <Col xs={24} md={9}>
          <Card
            size="small"
            title="Fixture"
            style={{ marginBottom: 12 }}
            extra={
              <Space>
                <Tooltip title="New fixture">
                  <Button
                    size="small"
                    icon={<PlusOutlined />}
                    onClick={() => { setFixtureId(''); setFixtureText(STARTER_FIXTURE) }}
                  />
                </Tooltip>
                <Button size="small" icon={<ReloadOutlined />} onClick={refresh} />
              </Space>
            }
          >
            <Space.Compact block style={{ marginBottom: 8 }}>
              <Select
                style={{ width: '55%' }}
                placeholder="Saved fixtures"
                value={fixtureId || undefined}
                onChange={selectFixture}
                options={fixtures.map(f => ({ value: f.id, label: `${f.id} (${f.cases})` }))}
                allowClear
                onClear={() => { setFixtureId(''); setFixtureText('') }}
              />
              <Input
                style={{ width: '45%' }}
                placeholder="id"
                value={fixtureId}
                onChange={e => setFixtureId(e.target.value)}
              />
            </Space.Compact>
            <Input.TextArea
              value={fixtureText}
              onChange={e => setFixtureText(e.target.value)}
              placeholder="TOML: one [[eval]] table per case…"
              autoSize={{ minRows: 8, maxRows: 20 }}
              style={{ fontFamily: 'monospace', fontSize: 12 }}
            />
            <Space style={{ marginTop: 8 }}>
              <Button icon={<SaveOutlined />} onClick={saveFixture}>Save</Button>
              <Button
                icon={<DeleteOutlined />}
                danger
                disabled={!fixtureId || !fixtures.some(f => f.id === fixtureId)}
                onClick={deleteFixture}
              >
                Delete
              </Button>
            </Space>
          </Card>

          <Card size="small" title="Run">
            <Space direction="vertical" style={{ width: '100%' }} size={10}>
              <Space>
                <Text>Samples per case</Text>
                <InputNumber
                  min={1}
                  max={20}
                  value={samples}
                  onChange={v => setSamples(Number(v) || 1)}
                />
              </Space>
              <Space>
                <Switch checked={validate} onChange={setValidate} size="small" />
                <Tooltip title="Also assert the model's args wouldn't bounce (preflight validation)">
                  <Text>Validate args</Text>
                </Tooltip>
              </Space>
              <Space>
                <Switch checked={telemetry} onChange={setTelemetry} size="small" />
                <Tooltip title="Emit one turn→llm→tool OTLP trace per case (needs [telemetry] enabled)">
                  <Text>Emit telemetry</Text>
                </Tooltip>
              </Space>
              <Button
                type="primary"
                block
                icon={<ThunderboltOutlined />}
                loading={running}
                onClick={run}
              >
                Run evals
              </Button>
            </Space>
          </Card>

          <Card
            size="small"
            title="Saved reports"
            style={{ marginTop: 12 }}
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={refresh} />}
          >
            {reports.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No runs yet" />
            ) : (
              <div style={{ maxHeight: 220, overflow: 'auto' }}>
                {reports.map(r => (
                  <div
                    key={r.id}
                    onClick={() => openReport(r.id)}
                    style={{
                      cursor: 'pointer',
                      padding: '6px 4px',
                      borderRadius: 4,
                      background: report?.id === r.id ? 'rgba(127,127,127,0.12)' : undefined,
                    }}
                  >
                    <Space size={6}>
                      <Tag color={r.summary.failed === 0 ? 'green' : 'red'}>
                        {r.summary.passed}/{r.summary.total}
                      </Tag>
                      <Text style={{ fontSize: 12 }}>
                        {r.generated_at?.slice(0, 16).replace('T', ' ')}
                      </Text>
                      <Button
                        size="small"
                        type="text"
                        icon={<DeleteOutlined />}
                        onClick={e => { e.stopPropagation(); removeReport(r.id) }}
                      />
                    </Space>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Col>

        {/* ── report ── */}
        <Col xs={24} md={15}>
          {running ? (
            <Card><Spin /> Running evals against the live agent…</Card>
          ) : !report ? (
            <Card>
              <Empty description="Run a fixture to see which tool the agent picks for each prompt." />
            </Card>
          ) : (
            <EvalReportPanel report={report} />
          )}
        </Col>
      </Row>
    </div>
  )
}

function EvalReportPanel({ report }: { report: EvalReport }) {
  const s = report.summary
  const multiSample = report.samples > 1

  const cols: ColumnType<EvalCaseResult>[] = [
    {
      title: 'Case',
      dataIndex: 'case_id',
      width: 170,
      render: (id: string) => <Text style={{ fontSize: 12 }}>{id}</Text>,
    },
    {
      title: 'Verdict',
      dataIndex: 'category',
      width: 150,
      render: (c: string, r) => {
        const meta = evalCatMeta(c)
        return (
          <Space size={4}>
            <Tag color={r.passed ? 'green' : meta.color}>{r.passed ? 'pass' : meta.label}</Tag>
            {multiSample && !r.passed && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {r.pass_count}/{r.samples}
              </Text>
            )}
          </Space>
        )
      },
      filters: [...new Set(report.cases.map(c => c.category))].map(c => ({
        text: evalCatMeta(c).label,
        value: c,
      })),
      onFilter: (v, r) => r.category === v,
    },
    {
      title: 'Chose',
      dataIndex: 'target',
      width: 160,
      render: (t: string) => (
        <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{t || '— (no run)'}</Text>
      ),
    },
    {
      title: 'Detail',
      dataIndex: 'reason',
      render: (reason: string) => <Text style={{ fontSize: 12 }}>{reason}</Text>,
    },
  ]

  return (
    <>
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={
          <Space wrap>
            <Tag color={s.failed === 0 ? 'green' : 'red'}>
              {Math.round(s.pass_rate * 100)}%
            </Tag>
            <Text type="secondary">
              {report.model || report.provider} · tool_mode {report.tool_mode}
              {multiSample ? ` · ${report.samples} samples/case` : ''}
            </Text>
          </Space>
        }
      >
        <Row gutter={16}>
          <Col span={6}><Statistic title="Pass rate" value={`${Math.round(s.pass_rate * 100)}%`} /></Col>
          <Col span={6}><Statistic title="Passed" value={s.passed} /></Col>
          <Col span={6}><Statistic title="Failed" value={s.failed} /></Col>
          <Col span={6}><Statistic title="Cases" value={s.total} /></Col>
        </Row>
        {Object.keys(s.by_category).length > 0 && (
          <Space wrap style={{ marginTop: 12 }}>
            {Object.entries(s.by_category).map(([cat, n]) => {
              const meta = evalCatMeta(cat)
              return <Tag key={cat} color={meta.color}>{meta.label}: {n}</Tag>
            })}
          </Space>
        )}
      </Card>

      <Card size="small" title="Per-case results">
        <Table
          size="small"
          rowKey="case_id"
          dataSource={report.cases}
          columns={cols}
          pagination={{ pageSize: 15, hideOnSinglePage: true }}
        />
      </Card>
    </>
  )
}
