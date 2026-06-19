import { useEffect, useState } from 'react'
import { Modal, Input, Select, Typography, Tag, message } from 'antd'
import type { ArgDef, Skill } from '../bridge'
import { ArgInput } from './FormsView'

const { Text } = Typography
const { TextArea } = Input

interface SkillRunModalProps {
  skill: Skill | null
  hostNames: string[]
  onClose: () => void
  // Fire the skill: the rendered body + values are sent to the agent (the console
  // turn is tagged with the skill). ConsoleView owns the bridge.run_skill call.
  onRun: (skillId: string, values: Record<string, unknown>, extra: string) => void
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

// One field. A skill's params are real runspec args, so the common types reuse the
// Forms-tab ArgInput verbatim. Two skill-specific cases: a `ui:"host"` slot → fleet
// dropdown, and `path` → plain text (a skill path is usually a *remote* path, so we
// don't browse the local console filesystem).
function SkillField({ arg, hostNames, value, error, onChange }: {
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
        style={{ fontFamily: 'monospace', fontSize: 12 }}
      />
    )
  }
  return <ArgInput arg={arg} host="" runnable="" value={value} error={error} onChange={onChange} />
}

export function SkillRunModal({ skill, hostNames, onClose, onRun }: SkillRunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [extra, setExtra] = useState('')

  useEffect(() => {
    if (!skill) return
    setValues(defaultsFor(skill.args ?? []))
    setErrors({})
    setExtra('')
  }, [skill?.id])

  if (!skill) return null
  const args = skill.args ?? []

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
    onRun(skill.id, values, extra.trim())
    onClose()
    message.success(`$${skill.id} started`)
  }

  return (
    <Modal
      open
      onCancel={onClose}
      onOk={submit}
      okText="Run skill"
      width={560}
      title={`Skill: ${skill.title || skill.id}`}
    >
      {skill.symptom && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 12 }}>
          {skill.symptom}
        </Text>
      )}
      {skill.argsError && (
        <Text type="danger" style={{ display: 'block', marginBottom: 12, fontSize: 12 }}>
          This skill's parameters are misconfigured: {skill.argsError}
        </Text>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {args.map(a => (
          <div key={a.name}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
              <Text code style={{ fontSize: 12 }}>{a.name}</Text>
              <Tag style={{ fontSize: 10, margin: 0 }}>{a.ui === 'host' ? 'host' : a.type}</Tag>
              {a.required
                ? <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>required</Tag>
                : <Text type="secondary" style={{ fontSize: 10 }}>optional</Text>}
              {a.description && <Text type="secondary" style={{ fontSize: 11 }}>{a.description}</Text>}
            </div>
            <SkillField arg={a} hostNames={hostNames} value={values[a.name]} error={errors[a.name]} onChange={v => change(a.name, v)} />
            {errors[a.name] && <Text style={{ fontSize: 11, color: '#ff4d4f' }}>{errors[a.name]}</Text>}
          </div>
        ))}
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
            <Text style={{ fontSize: 12 }}>Additional instructions</Text>
            <Text type="secondary" style={{ fontSize: 10 }}>optional — appended to the skill</Text>
          </div>
          <TextArea
            value={extra}
            onChange={e => setExtra(e.target.value)}
            rows={3}
            placeholder="e.g. then summarise the errors and suggest a fix"
            style={{ fontSize: 12 }}
          />
        </div>
      </div>
    </Modal>
  )
}
