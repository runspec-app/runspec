import { useEffect, useState } from 'react'
import { Table, Tag, Input, Typography, Divider } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { useIsDark } from '../ThemeContext'
import type { Runnable, ArgDef } from '../bridge'

const { Text } = Typography

// Library defaults for per-runnable fields that are always in effect
const SPEC_DEFAULTS: Record<string, unknown> = {
  autonomy: 'confirm',
  output: 'text',
  serve: true,
  become_method: 'sudo',  // only shown in Remote Execution section when run_as is set
}

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
}

function ArgRow({ arg }: { arg: ArgDef }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, fontFamily: 'monospace', fontSize: 12, flexWrap: 'wrap' }}>
      <Text code style={{ fontSize: 12, minWidth: 160 }}>--{arg.name}</Text>
      <Tag style={{ fontSize: 11, margin: 0 }}>{arg.type}</Tag>
      {arg.required ? (
        <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
      ) : isVarRef(arg.default) ? (
        <Text style={{ fontSize: 11, fontFamily: 'monospace', color: '#fa8c16' }}>{String(arg.default)}</Text>
      ) : arg.default !== undefined ? (
        <Text type="secondary" style={{ fontSize: 11 }}>default: {String(arg.default)}</Text>
      ) : (
        <Text type="secondary" style={{ fontSize: 11 }}>optional</Text>
      )}
      {arg.options && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          [{arg.options.join(' | ')}]
        </Text>
      )}
      {arg.description && (
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>{arg.description}</Text>
      )}
    </div>
  )
}

function FieldRow({
  name,
  value,
  isDefault,
  isDark,
}: {
  name: string
  value: React.ReactNode
  isDefault: boolean
  isDark: boolean
}) {
  const labelColor = isDefault
    ? (isDark ? '#444' : '#ccc')
    : (isDark ? '#888' : '#999')
  const valueOpacity = isDefault ? 0.45 : 1
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 0, minHeight: 22 }}>
      <span style={{
        fontSize: 11, fontFamily: 'monospace', color: labelColor,
        width: 140, flexShrink: 0, userSelect: 'none',
      }}>
        {name}
      </span>
      <span style={{ fontSize: 12, opacity: valueOpacity, display: 'flex', alignItems: 'center', gap: 4 }}>
        {value}
        {isDefault && (
          <span style={{ fontSize: 10, fontFamily: 'monospace', color: isDark ? '#555' : '#bbb', fontStyle: 'italic' }}>
            default
          </span>
        )}
      </span>
    </div>
  )
}

function autonomyTag(value: unknown) {
  const v = String(value)
  const color = v === 'autonomous' ? 'green' : v === 'supervised' ? 'orange' : v === 'manual' ? 'red' : 'default'
  return <Tag color={color} style={{ fontSize: 11, margin: 0 }}>{v}</Tag>
}

function SectionLabel({ label, isDark }: { label: string; isDark: boolean }) {
  return (
    <div style={{
      fontSize: 10, fontFamily: 'monospace', color: isDark ? '#444' : '#ccc',
      textTransform: 'uppercase', letterSpacing: '0.1em',
      marginBottom: 4, marginTop: 2, userSelect: 'none',
    }}>
      {label}
    </div>
  )
}

function SpecPanel({ runnable }: { runnable: Runnable }) {
  const isDark = useIsDark()
  const raw = runnable.rawSpec ?? {}
  const args = runnable.args ?? []
  const subcommands = runnable.commands ? Object.values(runnable.commands) : []

  // Resolve a field: explicit value from rawSpec, or library default, or null
  const resolve = (key: string): { value: unknown; isDefault: boolean } | null => {
    if (key in raw) return { value: raw[key], isDefault: false }
    if (key in SPEC_DEFAULTS) return { value: SPEC_DEFAULTS[key], isDefault: true }
    return null
  }

  // When no rawSpec is present at all, fall back to the processed runnable fields
  const autonomyField = resolve('autonomy') ?? { value: runnable.autonomy || 'confirm', isDefault: true }
  const outputField   = resolve('output')
  const serveField    = resolve('serve')

  const runAs        = (raw.run_as as string | undefined) ?? runnable.runAs
  const becomeMethod = resolve('become_method')
  const becomeFlags  = raw.become_flags as string | undefined
  const reason       = raw['autonomy-reason'] as string | undefined
  const envFile      = raw.runspec_env as string | undefined

  type ExampleEntry = { description?: string; args?: Record<string, unknown> }
  const examples = raw.examples as ExampleEntry[] | undefined

  // Anything not in the known set is a custom top-level field in the TOML
  const KNOWN = new Set([
    'description', 'autonomy', 'output', 'serve',
    'run_as', 'become_method', 'become_flags',
    'autonomy-reason', 'runspec_env', 'examples',
  ])
  const customFields = Object.entries(raw).filter(([k]) => !KNOWN.has(k))

  const hasRemote  = !!(runAs || becomeFlags)
  const hasContext = !!(reason || envFile)
  const hasCustom  = customFields.length > 0

  const labelCol = isDark ? '#888' : '#999'

  return (
    <div style={{ padding: '10px 48px 20px', display: 'flex', flexDirection: 'column', gap: 14 }}>

      {/* Core settings — always shown, all have library defaults */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        <SectionLabel label="Settings" isDark={isDark} />
        <FieldRow
          name="autonomy"
          value={autonomyTag(autonomyField.value)}
          isDefault={autonomyField.isDefault}
          isDark={isDark}
        />
        {outputField && (
          <FieldRow
            name="output"
            value={<Tag style={{ fontSize: 11, margin: 0 }}>{String(outputField.value)}</Tag>}
            isDefault={outputField.isDefault}
            isDark={isDark}
          />
        )}
        {serveField && (
          <FieldRow
            name="serve"
            value={
              <Tag
                color={serveField.value ? 'green' : 'default'}
                style={{ fontSize: 11, margin: 0 }}
              >
                {String(serveField.value)}
              </Tag>
            }
            isDefault={serveField.isDefault}
            isDark={isDark}
          />
        )}
      </div>

      {/* Remote execution — only when run_as is set or become_method is overridden */}
      {hasRemote && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <SectionLabel label="Remote Execution" isDark={isDark} />
          {runAs && (
            <FieldRow
              name="run_as"
              value={<Tag color="orange" style={{ fontSize: 11, margin: 0 }}>{runAs}</Tag>}
              isDefault={false}
              isDark={isDark}
            />
          )}
          {becomeMethod && (
            <FieldRow
              name="become_method"
              value={<Tag style={{ fontSize: 11, margin: 0 }}>{String(becomeMethod.value)}</Tag>}
              isDefault={becomeMethod.isDefault}
              isDark={isDark}
            />
          )}
          {becomeFlags && (
            <FieldRow
              name="become_flags"
              value={<Text code style={{ fontSize: 11 }}>{becomeFlags}</Text>}
              isDefault={false}
              isDark={isDark}
            />
          )}
        </div>
      )}

      {/* Context fields */}
      {hasContext && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <SectionLabel label="Context" isDark={isDark} />
          {reason && (
            <FieldRow
              name="autonomy-reason"
              value={<Text style={{ fontSize: 12, fontStyle: 'italic' }}>{reason}</Text>}
              isDefault={false}
              isDark={isDark}
            />
          )}
          {envFile && (
            <FieldRow
              name="runspec_env"
              value={<Text code style={{ fontSize: 11 }}>{envFile}</Text>}
              isDefault={false}
              isDark={isDark}
            />
          )}
        </div>
      )}

      {/* Examples */}
      {examples && examples.length > 0 && (
        <div>
          <SectionLabel label="Examples" isDark={isDark} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {examples.map((ex, i) => {
              const argStr = ex.args
                ? Object.entries(ex.args)
                    .map(([k, v]) => v === true ? `--${k}` : v === false ? '' : `--${k}=${v}`)
                    .filter(Boolean)
                    .join(' ')
                : ''
              return (
                <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  {ex.description && (
                    <Text type="secondary" style={{ fontSize: 11 }}>{ex.description}</Text>
                  )}
                  <Text code style={{ fontSize: 11 }}>
                    {runnable.name}{argStr ? ` ${argStr}` : ''}
                  </Text>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Custom fields from TOML not covered by the spec */}
      {hasCustom && (
        <div>
          <SectionLabel label="Custom" isDark={isDark} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {customFields.map(([k, v]) => (
              <FieldRow
                key={k}
                name={k}
                value={<Text style={{ fontSize: 12, fontFamily: 'monospace' }}>{JSON.stringify(v)}</Text>}
                isDefault={false}
                isDark={isDark}
              />
            ))}
          </div>
        </div>
      )}

      {/* Args */}
      {(args.length > 0 || runnable.commands) && <Divider style={{ margin: '2px 0' }} />}
      {args.length === 0 && !runnable.commands ? (
        <Text type="secondary" style={{ fontSize: 12 }}>No arguments.</Text>
      ) : args.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          <SectionLabel label="Arguments" isDark={isDark} />
          {args.map(arg => <ArgRow key={arg.name} arg={arg} />)}
        </div>
      ) : null}

      {/* Subcommands */}
      {subcommands.length > 0 && (
        <div>
          <Text style={{ fontSize: 11, color: labelCol, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Subcommands
          </Text>
          <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {subcommands.map(sub => (
              <div key={sub.name}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <Text code style={{ fontSize: 12 }}>{sub.name}</Text>
                  {sub.description && (
                    <Text type="secondary" style={{ fontSize: 12 }}>{sub.description}</Text>
                  )}
                  <Tag
                    color={sub.autonomy === 'autonomous' ? 'green' : sub.autonomy === 'supervised' ? 'orange' : 'default'}
                    style={{ fontSize: 10, margin: 0 }}
                  >
                    {sub.autonomy || 'confirm'}
                  </Tag>
                </div>
                {(sub.args ?? []).length > 0 && (
                  <div style={{ marginTop: 4, paddingLeft: 16, display: 'flex', flexDirection: 'column', gap: 3 }}>
                    {(sub.args ?? []).map(arg => <ArgRow key={arg.name} arg={arg} />)}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

interface SpecsViewProps {
  runnables: Runnable[]
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
}

export function SpecsView({ runnables, selectedHost, activeScope, onScopeToggle }: SpecsViewProps) {
  const [search, setSearch] = useState('')
  const [hostColumnFilter, setHostColumnFilter] = useState<string[]>(selectedHost ? [selectedHost] : [])

  useEffect(() => {
    setHostColumnFilter(selectedHost ? [selectedHost] : [])
  }, [selectedHost])

  const filtered = search
    ? runnables.filter(r =>
        r.name.toLowerCase().includes(search.toLowerCase()) ||
        r.group.toLowerCase().includes(search.toLowerCase()) ||
        r.host.toLowerCase().includes(search.toLowerCase()) ||
        (r.description ?? '').toLowerCase().includes(search.toLowerCase())
      )
    : runnables

  const hosts = [...new Set(runnables.map(r => r.host))]
  const groups = [...new Set(runnables.map(r => r.group))]

  const columns: ColumnType<Runnable>[] = [
    {
      title: 'Runnable',
      key: 'name',
      render: (_: unknown, r: Runnable) => (
        <span style={{ fontFamily: 'monospace', fontSize: 13 }}>
          <span style={{ color: '#666', fontSize: 12 }}>{r.group}/</span>{r.name}
        </span>
      ),
      sorter: (a, b) => `${a.group}/${a.name}`.localeCompare(`${b.group}/${b.name}`),
    },
    {
      title: 'Host',
      dataIndex: 'host',
      key: 'host',
      render: (h: string) => <Tag>{h}</Tag>,
      filters: hosts.map(h => ({ text: h, value: h })),
      onFilter: (value, record) => record.host === value,
      filteredValue: hostColumnFilter,
    },
    {
      title: 'Group',
      dataIndex: 'group',
      key: 'group',
      render: (g: string) => {
        const active = activeScope.includes(g)
        return (
          <Tag
            color={active ? 'geekblue' : 'blue'}
            onClick={() => onScopeToggle(g)}
            style={{ cursor: 'pointer', fontWeight: active ? 600 : 400 }}
            title={active ? 'Remove from scope' : 'Add to scope'}
          >
            {g}
          </Tag>
        )
      },
      filters: groups.map(g => ({ text: g, value: g })),
      onFilter: (value, record) => record.group === value,
      filteredValue: [],
    },
    {
      title: 'Description',
      dataIndex: 'description',
      key: 'description',
      render: (d: string) => d ?? <span style={{ color: '#555' }}>—</span>,
    },
    {
      title: 'Autonomy',
      key: 'autonomy',
      render: (_: unknown, r: Runnable) => (
        <Tag
          color={r.autonomy === 'autonomous' ? 'green' : 'default'}
          style={{ fontSize: 11 }}
        >
          {r.autonomy || 'confirm'}
        </Tag>
      ),
      filters: [
        { text: 'autonomous', value: 'autonomous' },
        { text: 'confirm', value: 'confirm' },
      ],
      onFilter: (value, record) => (record.autonomy || 'confirm') === value,
    },
    {
      title: 'Args',
      key: 'args',
      render: (_: unknown, r: Runnable) => {
        const count = (r.args ?? []).length
        const required = (r.args ?? []).filter(a => a.required).length
        if (count === 0) return <Text type="secondary" style={{ fontSize: 12 }}>none</Text>
        return <Text style={{ fontSize: 12 }}>{count} ({required} required)</Text>
      },
    },
  ]

  return (
    <div>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Search name, group, host, description…"
        value={search}
        onChange={e => setSearch(e.target.value)}
        style={{ marginBottom: 16, maxWidth: 360 }}
      />
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey={r => `${r.host}/${r.group}/${r.name}`}
        size="small"
        onChange={(_, filters) => setHostColumnFilter((filters['host'] as string[]) || [])}
        expandable={{
          expandedRowRender: r => <SpecPanel runnable={r} />,
          rowExpandable: () => true,
          expandRowByClick: false,
        }}
      />
    </div>
  )
}
