import { useEffect, useState } from 'react'
import { Modal, Typography, Tag, message } from 'antd'
import type { ArgDef, SavedRun } from '../bridge'
import { RunbookField } from './RunbookRunModal'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

interface SavedRunRunModalProps {
  savedRun: SavedRun | null
  hostNames: string[]
  onClose: () => void
  // Fire the saved run: only the open-param values travel — the runnable, target
  // hosts, fixed args, and credentials are baked into the preset and resolved
  // bridge-side. ConsoleView owns the bridge.run_saved_run call.
  onRun: (id: string, values: Record<string, unknown>) => void
}

// Literal defaults seed the form; a $VAR_REF default is shown as a placeholder,
// not pre-filled (mirrors RunbookRunModal / the Forms RunModal).
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

// The run dialog for a saved run: a read-only summary of what is baked in
// (target, fixed args, credentials), then a field per OPEN param. The dialog
// never picks a host — targeting is stored on the preset and displayed here.
export function SavedRunRunModal({ savedRun, hostNames, onClose, onRun }: SavedRunRunModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!savedRun) return
    setValues(defaultsFor(savedRun.params ?? []))
    setErrors({})
  }, [savedRun?.id])

  if (!savedRun) return null
  const params = savedRun.params ?? []
  const fixed = Object.entries(savedRun.args ?? {})

  const change = (name: string, v: unknown) => {
    setValues(prev => ({ ...prev, [name]: v }))
    setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  // Front-line validation (required + range) mirrors RunbookRunModal; the bridge
  // re-validates with the core validator/types before running.
  const submit = () => {
    const errs: Record<string, string> = {}
    for (const a of params) {
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
    onRun(savedRun.id, values)
    onClose()
    message.success(`Saved run '${savedRun.id}' started`)
  }

  const cmd = (savedRun.command ?? []).length > 0 ? ` ${savedRun.command.join(' ')}` : ''

  return (
    <Modal
      open
      onCancel={onClose}
      onOk={submit}
      okText="Run"
      width={560}
      title={`Saved run: ${savedRun.title || savedRun.id}`}
    >
      {savedRun.description && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
          {savedRun.description}
        </Text>
      )}
      {savedRun.argsError && (
        <Text type="danger" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
          This saved run is misconfigured: {savedRun.argsError}
        </Text>
      )}
      {/* Baked-in summary — read-only by design. */}
      <div style={{ marginBottom: 14, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div>
          <Text code style={{ fontSize: 13 }}>/{savedRun.runnable}{cmd}</Text>
          <Text type="secondary" style={{ fontSize: 13 }}> on {savedRun.target || savedRun.host}</Text>
        </div>
        {fixed.length > 0 && (
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
            <Text type="secondary" style={{ fontSize: 12 }}>fixed:</Text>
            {fixed.map(([k, v]) => (
              <Tag key={k} style={{ fontSize: 11, margin: 0, fontFamily: MONO_FONT }}>
                {k}={String(v)}
              </Tag>
            ))}
          </div>
        )}
        {(savedRun.credential_ids ?? []).length > 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {savedRun.credential_ids.length} credential{savedRun.credential_ids.length !== 1 ? 's' : ''} applied
            from the keychain at run time
          </Text>
        )}
      </div>
      {params.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {params.map(a => (
            <div key={a.name}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
                <Text code style={{ fontSize: 13 }}>{a.name}</Text>
                <Tag style={{ fontSize: 11, margin: 0 }}>{a.ui === 'host' ? 'host' : a.type}</Tag>
                {a.required
                  ? <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
                  : <Text type="secondary" style={{ fontSize: 11 }}>optional</Text>}
                {a.description && <Text type="secondary" style={{ fontSize: 12 }}>{a.description}</Text>}
              </div>
              <RunbookField arg={a} hostNames={hostNames} value={values[a.name]} error={errors[a.name]} onChange={v => change(a.name, v)} />
              {errors[a.name] && <Text style={{ fontSize: 12, color: '#ff4d4f' }}>{errors[a.name]}</Text>}
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}
