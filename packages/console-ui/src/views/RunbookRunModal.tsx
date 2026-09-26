import { useEffect, useState } from 'react'
import { Modal, Input, Select, Typography, Tag, message } from 'antd'
import type { ArgDef, Runbook } from '../bridge'
import { ArgGrid, ArgInput, DefaultArgsCollapse, TruncatedText, argSpansRow, partitionArgs } from './formsShared'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography
const { TextArea } = Input

interface RunbookRunModalProps {
  runbook: Runbook | null
  hostNames: string[]
  onClose: () => void
  // Fire the runbook: the rendered body + values are sent to the agent (the console
  // turn is tagged with the runbook). ConsoleView owns the bridge.run_runbook call.
  onRun: (runbookId: string, values: Record<string, unknown>, extra: string) => void
}

// Literal defaults seed the form; a $VAR_REF default is shown as a placeholder in
// ArgInput, not pre-filled (mirrors the Forms tab's RunModal).
function defaultsFor(args: ArgDef[]): Record<string, unknown> {
  const d: Record<string, unknown> = {}
  for (const a of args) {
    const def = a.default
    if (def !== undefined && !(typeof def === 'string' && def.startsWith('$'))) {
      d[a.name] = def
    }
  }
  return d
}

// One field. A runbook's params are real runspec args, so the common types reuse the
// Forms-tab ArgInput verbatim. Two runbook-specific cases: a `ui:"host"` slot → fleet
// dropdown, and `path` → plain text (a runbook path is usually a *remote* path, so we
// don't browse the local console filesystem).
// Exported so the Schedules tab's "Run a runbook" section renders values with the
// same rich, host-aware controls this modal uses.
export function RunbookField({ arg, hostNames, value, error, onChange }: {
  arg: ArgDef
  hostNames: string[]
  value: unknown
  error?: string
  onChange: (v: unknown) => void
}) {
  if (arg.ui === 'host') {
    return (
      <Select
        size="small"
        value={value as string | undefined}
        onChange={onChange}
        placeholder="Select host…"
        style={{ width: '100%' }}
        status={error ? 'error' : undefined}
        showSearch
        options={hostNames.map(h => ({ label: h, value: h }))}
      />
    )
  }
  if (arg.type === 'path') {
    return (
      <Input
        size="small"
        value={(value as string) ?? ''}
        onChange={e => onChange(e.target.value)}
        placeholder={arg.hint}
        status={error ? 'error' : undefined}
        style={{ fontFamily: MONO_FONT, fontSize: 13 }}
      />
    )
  }
  return <ArgInput arg={arg} host="" runnable="" value={value} error={error} onChange={onChange} />
}

// The dialog width the Forms tab uses: roomy enough for a two-column grid, wider
// still (3+ columns) when there are many params.
export function paramModalWidth(count: number): string {
  return count > 6 ? 'min(1160px, 96vw)' : 'min(860px, 94vw)'
}

// One param in the Forms-tab row shape: name + type/required tags on one line, the
// description on its own truncated line (hover for the full text), then the field.
export function ParamRow({ arg, hostNames, value, error, onChange }: {
  arg: ArgDef
  hostNames: string[]
  value: unknown
  error?: string
  onChange: (v: unknown) => void
}) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
        <Text code style={{ fontSize: 13 }}>{arg.name}</Text>
        <Tag style={{ fontSize: 11, margin: 0 }}>{arg.ui === 'host' ? 'host' : arg.type}</Tag>
        {arg.required
          ? <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
          : <Text type="secondary" style={{ fontSize: 11 }}>optional</Text>}
      </div>
      {arg.description && (
        <TruncatedText text={arg.description} style={{ fontSize: 12, marginBottom: 5, color: 'var(--rs-text-secondary)' }} />
      )}
      <RunbookField arg={arg} hostNames={hostNames} value={value} error={error} onChange={onChange} />
      {error && <Text style={{ fontSize: 12, color: '#ff4d4f' }}>{error}</Text>}
    </div>
  )
}

// The params laid out like a Forms-tab group: defaulted params folded into a
// collapsed "Default arguments" section, the rest in the responsive multi-column
// grid (wide controls span the row).
export function ParamGrid({ args, hostNames, values, errors, onChange }: {
  args: ArgDef[]
  hostNames: string[]
  values: Record<string, unknown>
  errors: Record<string, string>
  onChange: (name: string, v: unknown) => void
}) {
  const { visible, defaulted } = partitionArgs(args)
  const cell = (a: ArgDef) => (
    <div key={a.name} style={{ minWidth: 0, ...(argSpansRow(a) ? { gridColumn: '1 / -1' } : {}) }}>
      <ParamRow arg={a} hostNames={hostNames} value={values[a.name]} error={errors[a.name]} onChange={v => onChange(a.name, v)} />
    </div>
  )
  return (
    <>
      {defaulted.length > 0 && (
        <DefaultArgsCollapse count={defaulted.length}>
          <ArgGrid>{defaulted.map(cell)}</ArgGrid>
        </DefaultArgsCollapse>
      )}
      {visible.length > 0 && <ArgGrid>{visible.map(cell)}</ArgGrid>}
    </>
  )
}

export function RunbookRunModal({ runbook, hostNames, onClose, onRun }: RunbookRunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [extra, setExtra] = useState('')

  useEffect(() => {
    if (!runbook) return
    setValues(defaultsFor(runbook.args ?? []))
    setErrors({})
    setExtra('')
  }, [runbook?.id])

  if (!runbook) return null
  const args = runbook.args ?? []

  const change = (name: string, v: unknown) => {
    setValues(prev => ({ ...prev, [name]: v }))
    setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  // Front-line validation (required + range) mirrors the Forms RunModal; the bridge
  // re-validates with the core validator/types before running.
  const submit = () => {
    const errs: Record<string, string> = {}
    for (const a of args) {
      const v = values[a.name]
      if (a.required && (v === undefined || v === '')) {
        errs[a.name] = 'Required'
      } else if (a.range && (a.type === 'int' || a.type === 'float') && v !== undefined && v !== '') {
        const n = Number(v)
        const [lo, hi] = a.range
        if (n < lo || n > hi) errs[a.name] = `Must be between ${lo} and ${hi}`
      }
    }
    if (Object.keys(errs).length > 0) { setErrors(errs); return }
    onRun(runbook.id, values, extra.trim())
    onClose()
    message.success(`$${runbook.id} started`)
  }

  return (
    <Modal
      open
      onCancel={onClose}
      onOk={submit}
      okText="Run runbook"
      width={paramModalWidth(args.length)}
      title={`Runbook: ${runbook.title || runbook.id}`}
    >
      {runbook.symptom && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
          {runbook.symptom}
        </Text>
      )}
      {runbook.argsError && (
        <Text type="danger" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
          This runbook's parameters are misconfigured: {runbook.argsError}
        </Text>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <ParamGrid args={args} hostNames={hostNames} values={values} errors={errors} onChange={change} />
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
            <Text style={{ fontSize: 13 }}>Additional instructions</Text>
            <Text type="secondary" style={{ fontSize: 11 }}>optional — appended to the runbook</Text>
          </div>
          <TextArea
            value={extra}
            onChange={e => setExtra(e.target.value)}
            rows={3}
            placeholder="e.g. then summarise the errors and suggest a fix"
            style={{ fontSize: 13 }}
          />
        </div>
      </div>
    </Modal>
  )
}
