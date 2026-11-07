/**
 * HostScheduleModal — add or edit one schedule in a venv's runspec-scheduler
 * (the Host cron / Host daemon cards). Saving runs that venv's own
 * `runspec-scheduler add` as the venv owner, then `sync` on a cron venv so the
 * crontab follows; a daemon reloads by itself. The console never writes the
 * crontab directly, and a host schedule carries no credentials: password args
 * are left out (set them in the venv's .runspec_env on the host).
 */
import { useEffect, useMemo, useState } from 'react'
import { Modal, Form, Select, Input, Segmented, Switch, Space, Tag, Alert, Typography, message } from 'antd'
import {
  bridge, type ArgDef, type HostScheduleResult, type HostScheduleSave, type Runnable, type SharedSchedule,
} from '../bridge'
import { useIsDark, textSecondary, MONO_FONT } from '../ThemeContext'
import { initialFormValues, validateArgs } from './formsShared'
import { commandPathOptions, effectiveArgsForPath, RunnableArgsBlock, runnableSelectOptions } from './RunnableAction'
import type { SchedulerVenv } from './allSchedules'

const { Text } = Typography

export const HOST_CRON_PRESETS = [
  { label: 'Every 15 minutes', value: '*/15 * * * *' },
  { label: 'Every hour', value: '0 * * * *' },
  { label: 'Daily at 02:00', value: '0 2 * * *' },
  { label: 'Daily at 06:00', value: '0 6 * * *' },
  { label: 'Weekly (Sun 03:00)', value: '0 3 * * 0' },
  { label: 'Monthly (1st 04:00)', value: '0 4 1 * *' },
  { label: 'Custom…', value: '__custom__' },
]

/** The daemon's default per-fire timeout; status reports it when none is set. */
const DAEMON_DEFAULT_TIMEOUT_S = 21600

export interface HostScheduleEditing {
  venv: SchedulerVenv
  schedule: SharedSchedule
}

/** The args to store: no blanks, no secrets, and nothing equal to its default
 *  (so editing a schedule doesn't freeze today's defaults into it). */
export function hostScheduleArgs(args: ArgDef[], values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const a of args) {
    if (a.type === 'password') continue
    const v = values[a.name]
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v) && v.length === 0) continue
    if (a.default !== undefined && JSON.stringify(v) === JSON.stringify(a.default)) continue
    if (a.type === 'flag' && v === false && a.default === undefined) continue
    out[a.name] = v
  }
  return out
}

/** A fresh schedule id from the runnable (+ subcommand): `backup-nightly` style. */
export function suggestId(runnable: string, command: string[], taken: string[]): string {
  const base = [runnable, ...command].join('-').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 56) || 'schedule'
  if (!taken.includes(base)) return base
  for (let n = 2; n < 1000; n++) if (!taken.includes(`${base}-${n}`)) return `${base}-${n}`
  return `${base}-${Date.now()}`
}

/** The timeout field's starting text. A daemon reports its 6h default when the
 *  schedule sets none, so that shows blank rather than freezing it in. */
export function timeoutText(t: number | null | undefined, backend: 'cron' | 'daemon'): string {
  if (t == null) return ''
  if (backend === 'daemon' && t === DAEMON_DEFAULT_TIMEOUT_S) return ''
  if (t === 0) return '0'
  if (t % 86400 === 0) return `${t / 86400}d`
  if (t % 3600 === 0) return `${t / 3600}h`
  if (t % 60 === 0) return `${t / 60}m`
  return String(t)
}

interface Props {
  open: boolean
  venvs: SchedulerVenv[]
  runnables: Runnable[]
  /** Ids already used per venv key, to suggest a free one. */
  idsByVenv: Record<string, string[]>
  editing: HostScheduleEditing | null
  defaultVenvKey?: string
  onClose: () => void
  onSaved: (res: HostScheduleResult) => void
}

export function HostScheduleModal({ open, venvs, runnables, idsByVenv, editing, defaultVenvKey, onClose, onSaved }: Props) {
  const isDark = useIsDark()
  const labelStyle = { color: textSecondary(isDark), fontSize: 13 }
  const [venvKey, setVenvKey] = useState<string>('')
  const [id, setId] = useState('')
  const [idTouched, setIdTouched] = useState(false)
  const [runnableName, setRunnableName] = useState<string>('')
  const [command, setCommand] = useState<string>('')
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [argsJson, setArgsJson] = useState('')
  const [whenKind, setWhenKind] = useState<'cron' | 'every'>('cron')
  const [preset, setPreset] = useState('0 2 * * *')
  const [customCron, setCustomCron] = useState('')
  const [every, setEvery] = useState('')
  const [timezone, setTimezone] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [timeout, setTimeout_] = useState('')
  const [catchUp, setCatchUp] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const venv = venvs.find(v => v.key === venvKey) ?? editing?.venv ?? null
  const daemon = venv?.backend === 'daemon'
  const venvRunnables = useMemo(
    () => (venv ? runnables.filter(r => r.host === venv.host && r.group === venv.venv && r.name !== 'runspec-scheduler') : []),
    [runnables, venv?.host, venv?.venv],
  )
  const runnable = venvRunnables.find(r => r.name === runnableName) ?? null
  const commandPath = command.trim() ? command.trim().split(/\s+/) : []
  const allArgs = runnable ? effectiveArgsForPath(runnable, commandPath) : []
  const secretArgs = allArgs.filter(a => a.type === 'password')
  const formRunnable = runnable ? { ...runnable, args: allArgs.filter(a => a.type !== 'password') } : null
  const commandOptions = runnable ? commandPathOptions(runnable, labelStyle) : []

  useEffect(() => {
    if (!open) return
    setError('')
    setErrors({})
    setSaving(false)
    if (editing) {
      const s = editing.schedule
      const r = runnables.find(x => x.host === editing.venv.host && x.group === editing.venv.venv && x.name === s.runnable)
      const cmd = s.command ?? []
      setVenvKey(editing.venv.key)
      setId(s.id)
      setIdTouched(true)
      setRunnableName(s.runnable)
      setCommand(cmd.join(' '))
      setValues(r ? initialFormValues(effectiveArgsForPath(r, cmd), s.args) : {})
      setArgsJson(r ? '' : JSON.stringify(s.args ?? {}, null, 2))
      const cron = s.when.cron ?? ''
      setWhenKind(s.when.every ? 'every' : 'cron')
      setPreset(cron ? (HOST_CRON_PRESETS.some(p => p.value === cron) ? cron : '__custom__') : '0 2 * * *')
      setCustomCron(HOST_CRON_PRESETS.some(p => p.value === cron) ? '' : cron)
      setEvery(s.when.every ?? '')
      setTimezone(s.when.timezone ?? '')
      setEnabled(s.enabled)
      setTimeout_(timeoutText(s.timeout, editing.venv.backend))
      setCatchUp(!!s.catch_up)
      return
    }
    setVenvKey(defaultVenvKey && venvs.some(v => v.key === defaultVenvKey) ? defaultVenvKey : (venvs[0]?.key ?? ''))
    setId('')
    setIdTouched(false)
    setRunnableName('')
    setCommand('')
    setValues({})
    setArgsJson('')
    setWhenKind('cron')
    setPreset('0 2 * * *')
    setCustomCron('')
    setEvery('')
    setTimezone('')
    setEnabled(true)
    setTimeout_('')
    setCatchUp(false)
  }, [open, editing])

  const autoId = (name: string, cmd: string[]) => {
    if (idTouched || editing) return
    setId(name ? suggestId(name, cmd, idsByVenv[venvKey] ?? []) : '')
  }

  const pickVenv = (key: string) => {
    setVenvKey(key)
    setRunnableName('')
    setCommand('')
    setValues({})
    if (!idTouched) setId('')
    if (venvs.find(v => v.key === key)?.backend !== 'daemon') {
      setWhenKind('cron')
      setCatchUp(false)
    }
  }

  const pickRunnable = (name: string) => {
    setRunnableName(name)
    setCommand('')
    const r = venvRunnables.find(x => x.name === name)
    setValues(r ? initialFormValues(r.args) : {})
    setErrors({})
    autoId(name, [])
  }

  const pickCommand = (value?: string) => {
    const v = value ?? ''
    setCommand(v)
    const path = v.trim() ? v.trim().split(/\s+/) : []
    if (runnable) setValues(initialFormValues(effectiveArgsForPath(runnable, path)))
    setErrors({})
    autoId(runnableName, path)
  }

  const save = async () => {
    setError('')
    if (!venv) { setError('Pick a venv.'); return }
    if (!runnableName) { setError('Pick a runnable.'); return }
    if (runnable?.requireCommand && commandPath.length === 0) { setError('This runnable needs a subcommand.'); return }
    let args: Record<string, unknown> = {}
    if (formRunnable) {
      const errs = validateArgs(formRunnable.args, values)
      if (Object.keys(errs).length) { setErrors(errs); setError('Fill in the required arguments.'); return }
      args = hostScheduleArgs(allArgs, values)
    } else if (argsJson.trim()) {
      try {
        const parsed = JSON.parse(argsJson)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object')
        args = parsed as Record<string, unknown>
      } catch { setError('Arguments must be a JSON object, e.g. {"path": "/srv"}.'); return }
    }
    const cron = whenKind === 'cron' ? (preset === '__custom__' ? customCron.trim() : preset) : ''
    if (whenKind === 'cron' && !cron) { setError('Enter a cron expression.'); return }
    if (whenKind === 'every' && !every.trim()) { setError('Enter an interval, e.g. 15m.'); return }
    const payload: HostScheduleSave = {
      host: venv.host, venv: venv.venv, backend: venv.backend,
      id: id.trim(), runnable: runnableName, args,
      password_args: secretArgs.map(a => a.name),
      enabled,
      ...(editing ? { original_id: editing.schedule.id } : {}),
      ...(commandPath.length ? { command: commandPath } : {}),
      ...(whenKind === 'cron' ? { cron } : { every: every.trim() }),
      ...(timezone.trim() ? { timezone: timezone.trim() } : {}),
      ...(timeout.trim() ? { timeout: timeout.trim() } : {}),
      ...(daemon && catchUp ? { catch_up: true } : {}),
    }
    setSaving(true)
    try {
      const res = await bridge.host_schedule_save(payload)
      if (!res.ok) { setError(res.error || 'Save failed.'); return }
      onSaved(res)
    } catch (e) {
      message.error(`Couldn't save: ${String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  const venvOptions = venvs.map(v => ({
    value: v.key,
    label: (
      <Space size={6}>
        <span style={{ fontFamily: MONO_FONT }}>{v.host} / {v.venv}</span>
        <Tag bordered={false} color={v.backend === 'daemon' ? 'purple' : 'geekblue'} style={{ margin: 0 }}>
          {v.backend === 'daemon' ? 'daemon' : 'cron'}
        </Tag>
        {v.owner && <span style={labelStyle}>as {v.owner}</span>}
      </Space>
    ),
  }))

  return (
    <Modal
      open={open}
      title={editing ? `Edit host schedule ${editing.schedule.id}` : 'New host schedule'}
      okText={editing ? 'Save' : 'Add schedule'}
      onOk={save}
      confirmLoading={saving}
      onCancel={onClose}
      width={720}
      destroyOnHidden
    >
      <Form layout="vertical" component="div">
        <Form.Item label="Where" help={venv
          ? daemon
            ? 'This venv’s scheduler daemon picks the change up by itself.'
            : `Saved to this venv’s config, then its crontab is synced${venv.owner ? ` (as ${venv.owner})` : ''}. Only lines marked # runspec:sched: change.`
          : undefined}>
          <Select
            aria-label="Where"
            value={venvKey || undefined}
            onChange={pickVenv}
            options={venvOptions}
            disabled={!!editing}
            placeholder={venvs.length ? 'Pick a venv' : 'No venv with runspec-scheduler on the connected hosts'}
          />
        </Form.Item>

        <Form.Item label="Runnable" help={venv && venvRunnables.length === 0 && !editing
          ? 'No runnables found in this venv yet. Refresh the host from the sidebar if you just installed some.'
          : undefined}>
          <Select
            aria-label="Runnable"
            value={runnableName || undefined}
            onChange={pickRunnable}
            showSearch
            optionLabelProp="value"
            filterOption={(input, opt) => String(opt?.value ?? '').toLowerCase().includes(input.toLowerCase())}
            options={runnableSelectOptions(venvRunnables, labelStyle, runnableName || undefined)}
            placeholder="Select a runnable"
          />
        </Form.Item>

        {commandOptions.length > 0 && (
          <Form.Item label="Subcommand" help={runnable?.requireCommand ? undefined : 'Optional — leave blank to run the root command.'}>
            <Select aria-label="Subcommand" value={command || undefined} onChange={pickCommand} allowClear
              optionLabelProp="value" options={commandOptions} placeholder="Select a subcommand…" />
          </Form.Item>
        )}

        {formRunnable && formRunnable.args.length > 0 && (
          <RunnableArgsBlock
            runnable={formRunnable}
            values={values}
            errors={errors}
            onChange={(name, v) => {
              setValues(prev => ({ ...prev, [name]: v }))
              setErrors(prev => { const next = { ...prev }; delete next[name]; return next })
            }}
            labelStyle={labelStyle}
          />
        )}
        {secretArgs.length > 0 && (
          <Alert type="info" showIcon style={{ marginBottom: 16 }}
            message={`${secretArgs.map(a => a.name).join(', ')}: not stored in a host schedule`}
            description="A host schedule never carries a password. Set it on the host in the venv's .runspec_env (Env files in the hosts panel) so every fire reads it." />
        )}
        {!formRunnable && runnableName && (
          <Form.Item label="Arguments (JSON)" help="This runnable isn't in the console's list for this venv, so its arguments are edited as JSON.">
            <Input.TextArea aria-label="Arguments (JSON)" rows={3} value={argsJson} onChange={e => setArgsJson(e.target.value)}
              style={{ fontFamily: MONO_FONT }} placeholder='{"path": "/srv"}' />
          </Form.Item>
        )}

        <Form.Item label="Schedule id" help="The key on the host: the crontab marker and the History label sched:<id>.">
          <Input aria-label="Schedule id" value={id} onChange={e => { setId(e.target.value); setIdTouched(true) }}
            style={{ fontFamily: MONO_FONT }} placeholder="nightly-backup" maxLength={64} />
        </Form.Item>

        <Form.Item label="When">
          <Space direction="vertical" style={{ width: '100%' }}>
            {daemon && (
              <Segmented value={whenKind} onChange={v => setWhenKind(v as 'cron' | 'every')}
                options={[{ label: 'Cron', value: 'cron' }, { label: 'Interval', value: 'every' }]} />
            )}
            {whenKind === 'cron' ? (
              <Space wrap>
                <Select aria-label="Cron preset" value={preset} onChange={setPreset} options={HOST_CRON_PRESETS} style={{ width: 200 }} />
                {preset === '__custom__' && (
                  <Input aria-label="Cron expression" value={customCron} onChange={e => setCustomCron(e.target.value)}
                    placeholder="m h dom mon dow, e.g. 30 1 * * 1-5" style={{ width: 260, fontFamily: MONO_FONT }} />
                )}
              </Space>
            ) : (
              <Input aria-label="Interval" value={every} onChange={e => setEvery(e.target.value)} placeholder="15m, 1h, 1d"
                style={{ width: 200, fontFamily: MONO_FONT }} />
            )}
            <Input aria-label="Timezone" value={timezone} onChange={e => setTimezone(e.target.value)}
              placeholder="Timezone (blank = the host's local time), e.g. Europe/London" style={{ maxWidth: 420 }} />
          </Space>
        </Form.Item>

        <Space size="large" wrap align="start">
          <Space size={6}>
            <Switch size="small" checked={enabled} onChange={setEnabled} aria-label="Enabled" />
            <span style={labelStyle}>Enabled</span>
          </Space>
          <Space size={6}>
            <span style={labelStyle}>Timeout</span>
            <Input aria-label="Timeout" value={timeout} onChange={e => setTimeout_(e.target.value)} style={{ width: 110, fontFamily: MONO_FONT }}
              placeholder={daemon ? '6h' : 'none'} />
          </Space>
          {daemon && (
            <Space size={6}>
              <Switch size="small" checked={catchUp} onChange={setCatchUp} aria-label="Catch up" />
              <span style={labelStyle}>Run once if fires were missed while the daemon was down</span>
            </Space>
          )}
        </Space>
        <div style={{ marginTop: 6 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Timeout: 90m, 6h or seconds; 0 = no limit. Blank = {daemon ? 'the daemon’s 6h default' : 'no limit'}.
          </Text>
        </div>

        {error && <Alert type="error" showIcon style={{ marginTop: 14 }} message={error} />}
      </Form>
    </Modal>
  )
}
