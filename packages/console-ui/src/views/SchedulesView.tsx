import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Table, Button, Modal, Form, Select, Input, InputNumber, Space, Segmented, Switch, Tooltip, Collapse, Typography, message } from 'antd'
import { DeleteOutlined, PlusOutlined, MinusCircleOutlined, EditOutlined, PlayCircleOutlined, CalendarOutlined, ReloadOutlined } from '@ant-design/icons'
import { bridge, type SharedSchedulesVenv, type ArgDef, type Credential, type Host, type Runnable, type Schedule, type ScheduleWhen, type Runbook, type SavedRun, type SavedTransfer, type RunCard, type TriggerEvent, type EvalFixtureMeta } from '../bridge'
import { useIsDark, useAccent, textMuted, textSecondary, MONO_FONT } from '../ThemeContext'
import { useFeatures, visibleScheduleActions } from '../features'
import { RunInbox } from '../components/RunInbox'
import { EventLog } from '../components/EventLog'
import { SoundSelect } from '../components/SoundSelect'
import { ModelSelect } from '../components/ModelSelect'
import { initialFormValues, validateArgs } from './formsShared'
import { RunbookField } from './RunbookRunModal'
import { AllSchedulesView, WhenCell } from './AllSchedulesView'
import { EmptyState, IconAction, RowActions, StatusTag } from '../components/kit'
import { ScheduleScopeCards } from './ScheduleScopeCards'
import { collectRows, scopeCounts, type ScheduleScope } from './allSchedules'
import {
  ArgFieldRow, commandPathOptions, CredentialsSection, effectiveArgsForPath,
  RunnableArgsBlock, runnableSelectOptions, TargetFormItems, runnablesForTarget,
  type Target,
} from './RunnableAction'

const { Text } = Typography

const SCOPE_KEY = 'runspec.schedules.scope'

/** The remembered option card. `shared` is the pre-cards name of Host cron. */
export function readScheduleScope(): ScheduleScope {
  try {
    const v = localStorage.getItem(SCOPE_KEY)
    if (v === 'shared') return 'cron'
    return v === 'console' || v === 'cron' || v === 'daemon' || v === 'all' ? v : 'all'
  } catch { return 'all' }
}

/** The hosts whose venvs the All list reads: local plus the connected hosts in
 *  the active profile (the `hosts` list is already the profile's working set). */
export function scheduleTargets(hosts: Host[]): string[] {
  const names = hosts.filter(h => h.connected || h.name === 'local').map(h => h.name)
  return ['local', ...names.filter(n => n !== 'local')].filter((n, i, a) => a.indexOf(n) === i)
}

// Host schedule reads survive leaving the tab: reopening it shows the last read at
// once and refreshes underneath (one `runspec-scheduler status` per venv is an SSH
// round trip per venv, so it is worth not blanking the list for it).
let hostReadCache: { key: string; venvs: SharedSchedulesVenv[]; at: number } | null = null
export function _resetHostReadCache() { hostReadCache = null }

interface SchedulesViewProps {
  hosts: Host[]
  runnables: Runnable[]
  selectedHost: string
  activeProfile?: string
  /** Open the History tab with this search (a schedule's `sched:<id>` label). */
  onOpenHistory?: (search: string) => void
}

const CRON_PRESETS = [
  { label: 'Every hour',       value: '0 * * * *' },
  { label: 'Daily at midnight',value: '0 0 * * *' },
  { label: 'Daily at 02:00',   value: '0 2 * * *' },
  { label: 'Daily at 06:00',   value: '0 6 * * *' },
  { label: 'Weekly (Sun 03:00)',value: '0 3 * * 0' },
  { label: 'Monthly (1st 04:00)',value: '0 4 1 * *' },
  { label: 'Custom…',          value: '__custom__' },
]

type Action = 'runnable' | 'event' | 'prompt' | 'runbook' | 'saved_run' | 'saved_transfer' | 'eval'
type WhenKind = 'cron' | 'every'

// Front-line validation of an arg map lives in formsShared (shared with the
// host-schedule editor); re-exported here for its existing tests.
export { validateArgs }

interface ScheduleModalProps {
  open: boolean
  hosts: Host[]
  runnables: Runnable[]
  defaultHost: string
  editing: Schedule | null
  onCancel: () => void
  onSaved: () => void
}

function ScheduleModal({ open, hosts, runnables, defaultHost, editing, onCancel, onSaved }: ScheduleModalProps) {
  const [form] = Form.useForm()
  const [action, setAction] = useState<Action>('runnable')
  // `[console] ai` off-switch: the agent-turn actions (prompt / runbook /
  // evals) are not offered — the backend refuses them as invalid schedules.
  const features = useFeatures()
  const [target, setTarget] = useState<Target>('host')
  const [whenKind, setWhenKind] = useState<WhenKind>('cron')
  const [selectedRunnable, setSelectedRunnable] = useState<Runnable | null>(null)
  const [cronPreset, setCronPreset] = useState<string>('')
  const [customCron, setCustomCron] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  // Runnable/runbook arg values live outside the antd Form so we can reuse the
  // FormsView `ArgInput` — a controlled component that stores native types
  // (flag → boolean, int → number, etc.) rather than strings. The antd Form
  // still owns everything else (cron, host, group, event fields, …).
  const [argValues, setArgValues] = useState<Record<string, unknown>>({})
  const [argErrors, setArgErrors] = useState<Record<string, string>>({})
  const [runbookValues, setRunbookValues] = useState<Record<string, unknown>>({})
  const [runbookErrors, setRunbookErrors] = useState<Record<string, string>>({})
  const [savedRuns, setSavedRuns] = useState<SavedRun[]>([])
  const [savedRunValues, setSavedRunValues] = useState<Record<string, unknown>>({})
  const [savedRunErrors, setSavedRunErrors] = useState<Record<string, string>>({})
  const [savedTransfers, setSavedTransfers] = useState<SavedTransfer[]>([])
  const [transferValues, setTransferValues] = useState<Record<string, unknown>>({})
  const [transferErrors, setTransferErrors] = useState<Record<string, string>>({})
  // Console credentials to apply to a runnable-action run (secret injected from the
  // keychain at fire time). Same explicit-selection model as the Forms tab; only
  // the ids are persisted on the schedule, never a secret.
  const [credentialIds, setCredentialIds] = useState<string[]>([])
  const [creds, setCreds] = useState<Credential[]>([])
  // Stored eval fixtures (suite names), for the "Run evals" action's picker, plus
  // its run options. Loaded once per open.
  const [evalFixtures, setEvalFixtures] = useState<EvalFixtureMeta[]>([])
  const [evalFixture, setEvalFixture] = useState<string>('')
  const [evalSamples, setEvalSamples] = useState<number>(1)
  const [evalValidate, setEvalValidate] = useState<boolean>(false)
  const [evalTelemetry, setEvalTelemetry] = useState<boolean>(false)
  const isDark = useIsDark()
  const labelStyle = { color: textSecondary(isDark), fontSize: 13 }

  // Saved runbooks, for the "Run a runbook" action's picker. Loaded once per open.
  useEffect(() => {
    if (!open) return
    bridge.get_runbooks().then(setRunbooks).catch(console.error)
    // Saved runs, for the "Fire a saved run" action's picker.
    bridge.get_saved_runs().then(setSavedRuns).catch(() => setSavedRuns([]))
    // Saved transfers, for the "Saved transfer" action's picker.
    bridge.get_saved_transfers().then(setSavedTransfers).catch(() => setSavedTransfers([]))
    // Eval fixtures, for the "Run evals" action's picker.
    bridge.list_eval_fixtures().then(setEvalFixtures).catch(() => setEvalFixtures([]))
  }, [open])

  // All saved credentials, for the runnable-action credential picker. Loaded once
  // per open (mirrors the Forms run dialog).
  useEffect(() => {
    if (!open) return
    bridge.list_credentials().then(setCreds).catch(() => setCreds([]))
  }, [open])

  // Distinct host groups (from each host's `group` field), for group targeting.
  const groupNames = [...new Set(hosts.map(h => h.group).filter((g): g is string => !!g))].sort()
  const hostName: string = Form.useWatch('host', form) ?? defaultHost
  const groupName: string = Form.useWatch('group', form) ?? ''
  const hostsList: string[] = Form.useWatch('hosts', form) ?? []
  const selectedList: string[] = Form.useWatch('selected_hosts', form) ?? []
  // The chosen runbook (derived from the form's `runbook` id) drives the value
  // inputs, so it survives the async runbooks load with no extra state.
  const runbookId: string = Form.useWatch('runbook', form) ?? ''
  const selectedRunbook = runbooks.find(s => s.id === runbookId) ?? null
  // Same derivation for the chosen saved run — its open params drive the value inputs.
  const savedRunId: string = Form.useWatch('saved_run', form) ?? ''
  const selectedSavedRun = savedRuns.find(s => s.id === savedRunId) ?? null
  // …and for the chosen saved transfer's params.
  const savedTransferId: string = Form.useWatch('saved_transfer', form) ?? ''
  const selectedTransfer = savedTransfers.find(s => s.id === savedTransferId) ?? null
  // The runnable picker offers what the chosen target can run: a single host's
  // runnables, or (group / any-of-hosts mode) every runnable present on any of the
  // targeted hosts. Shared derivation with the saved-run editor.
  const hostRunnables = runnablesForTarget(runnables, hosts, target, {
    host: hostName, group: groupName, hostsList, selectedList,
  })
  // Subcommand handling, mirroring the saved-run editor: the field only exists
  // for runnables that declare subcommands (a dropdown of their paths, stored
  // space-joined in the form / as a path list on the schedule), and the picked
  // path shifts which args the rich block edits — the effective set at that
  // path (inherited globals + the command's own, child wins).
  const commandValue: string = Form.useWatch('command', form) ?? ''
  const commandPath = commandValue.trim() ? commandValue.trim().split(/\s+/) : []
  const commandOptions = selectedRunnable
    ? commandPathOptions(selectedRunnable, labelStyle)
    : []
  const effectiveRunnable = selectedRunnable
    ? { ...selectedRunnable, args: effectiveArgsForPath(selectedRunnable, commandPath) }
    : null
  const isEdit = !!editing

  const resetAll = () => {
    form.resetFields()
    setAction('runnable')
    setTarget('host')
    setWhenKind('cron')
    setCronPreset('')
    setCustomCron('')
    setSelectedRunnable(null)
    setArgValues({})
    setArgErrors({})
    setRunbookValues({})
    setRunbookErrors({})
    setSavedRunValues({})
    setSavedRunErrors({})
    setTransferValues({})
    setTransferErrors({})
    setCredentialIds([])
    setEvalFixture('')
    setEvalSamples(1)
    setEvalValidate(false)
    setEvalTelemetry(false)
  }

  // Prefill from the schedule being edited (or a clean slate for a new one)
  // whenever the dialog opens. Local state (action/whenKind/cron preset) lives
  // outside the Form, so it has to be seeded here too.
  useEffect(() => {
    if (!open) return
    if (!editing) {
      resetAll()
      form.setFieldsValue({ host: defaultHost, fields: [] })
      return
    }
    setAction(editing.action)
    setTarget(
      editing.group ? 'group'
      : editing.selected_hosts?.length ? 'selected'
      : editing.hosts?.length ? 'anyhost'
      : 'host',
    )
    const kind: WhenKind = editing.when.every ? 'every' : 'cron'
    setWhenKind(kind)
    if (kind === 'cron') {
      const cron = editing.when.cron ?? ''
      if (CRON_PRESETS.some(p => p.value === cron)) {
        setCronPreset(cron)
        setCustomCron('')
      } else {
        setCronPreset('__custom__')
        setCustomCron(cron)
      }
    } else {
      setCronPreset('')
      setCustomCron('')
    }
    const selectedNames = editing.selected_hosts ?? []
    const r = editing.group
      ? runnables.find(x => x.name === editing.runnable && hosts.some(h => h.group === editing.group && h.name === x.host)) ?? null
      : selectedNames.length
      ? runnables.find(x => x.name === editing.runnable && selectedNames.includes(x.host)) ?? null
      : runnables.find(x => x.host === editing.host && x.name === editing.runnable) ?? null
    setSelectedRunnable(r)
    // Seed rich arg values: spec defaults overlaid with the schedule's stored
    // args, against the effective set at the stored subcommand path.
    // `initialFormValues` (shared with Forms) preserves native types and drops
    // stale keys that no longer match a declared arg.
    const editArgs = r ? effectiveArgsForPath(r, editing.command ?? []) : []
    setArgValues(initialFormValues(editArgs, editing.args))
    setArgErrors({})
    setCredentialIds(editing.credential_ids ?? [])
    // Eval-action fields live outside the Form (like action/whenKind).
    setEvalFixture(editing.eval_fixture ?? '')
    setEvalSamples(editing.eval_samples ?? 1)
    setEvalValidate(editing.eval_validate ?? false)
    setEvalTelemetry(editing.eval_telemetry ?? false)
    // Runbook / saved-run value seeding is deferred to a second effect once the
    // respective list has loaded — their args come from the selected entry.
    setRunbookErrors({})
    setSavedRunErrors({})
    setTransferErrors({})
    const fv: Record<string, unknown> = {
      host: editing.host ?? defaultHost,
      group: editing.group ?? '',
      hosts: editing.hosts ?? [],
      selected_hosts: editing.selected_hosts ?? [],
      runnable: editing.runnable,
      command: (editing.command ?? []).join(' ') || undefined,
      timezone: editing.when.timezone ?? '',
      cron: editing.when.cron ?? '',
      every: editing.when.every ?? '',
      event: editing.event ?? '',
      prompt: editing.prompt ?? '',
      runbook: editing.runbook ?? undefined,
      saved_run: editing.saved_run ?? undefined,
      saved_transfer: editing.saved_transfer ?? undefined,
      transfer_dest: editing.transfer_dest ?? '',
      name: editing.name ?? '',
      sound: editing.sound ?? '',
      model: editing.model ?? '',
      fields: Object.entries(editing.fields ?? {}).map(([key, value]) => ({ key, value })),
    }
    form.setFieldsValue(fv)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing])

  // Seed the runbook value inputs whenever the chosen runbook resolves — this
  // handles both the async load on edit (runbooks list arrives after the modal
  // opens) and a manual switch of runbook in the picker. `initialFormValues`
  // overlays a saved schedule's values on top of the runbook's own defaults.
  useEffect(() => {
    if (!open) return
    if (!selectedRunbook) { setRunbookValues({}); return }
    const prefill = editing?.runbook === selectedRunbook.id ? editing.values : undefined
    setRunbookValues(initialFormValues(selectedRunbook.args ?? [], prefill))
    setRunbookErrors({})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedRunbook?.id])

  // Same seeding for the chosen saved run's open params.
  useEffect(() => {
    if (!open) return
    if (!selectedSavedRun) { setSavedRunValues({}); return }
    const prefill = editing?.saved_run === selectedSavedRun.id ? editing.values : undefined
    setSavedRunValues(initialFormValues(selectedSavedRun.params ?? [], prefill))
    setSavedRunErrors({})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedSavedRun?.id])

  // Same seeding for the chosen saved transfer's params.
  useEffect(() => {
    if (!open) return
    if (!selectedTransfer) { setTransferValues({}); return }
    const prefill = editing?.saved_transfer === selectedTransfer.id ? editing.values : undefined
    setTransferValues(initialFormValues(selectedTransfer.params ?? [], prefill))
    setTransferErrors({})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, selectedTransfer?.id])

  const handleRunnableChange = (name: string) => {
    const r = hostRunnables.find(x => x.name === name) ?? null
    setSelectedRunnable(r)
    // A subcommand carried over from the previous runnable doesn't apply.
    form.setFieldValue('command', undefined)
    // Reset the rich arg state to the runnable's spec defaults.
    setArgValues(initialFormValues(r?.args ?? []))
    setArgErrors({})
  }

  // Picking (or clearing) a subcommand switches which args the block edits —
  // reseed from the effective set at the new path.
  const handleCommandChange = (value?: string) => {
    if (!selectedRunnable) return
    const path = (value ?? '').trim() ? (value ?? '').trim().split(/\s+/) : []
    setArgValues(initialFormValues(effectiveArgsForPath(selectedRunnable, path)))
    setArgErrors({})
  }

  const handleHostChange = () => {
    form.setFieldValue('runnable', undefined)
    setSelectedRunnable(null)
    setArgValues({})
    setArgErrors({})
  }

  const handleTargetChange = (t: Target) => {
    setTarget(t)
    // Switching host↔group invalidates the chosen runnable (the available set
    // differs), so clear it.
    form.setFieldsValue({ runnable: undefined })
    setSelectedRunnable(null)
    setArgValues({})
    setArgErrors({})
  }

  const changeArg = (name: string, v: unknown) => {
    setArgValues(prev => ({ ...prev, [name]: v }))
    setArgErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const changeRunbookValue = (name: string, v: unknown) => {
    setRunbookValues(prev => ({ ...prev, [name]: v }))
    setRunbookErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const changeTransferValue = (name: string, v: unknown) => {
    setTransferValues(prev => ({ ...prev, [name]: v }))
    setTransferErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const changeSavedRunValue = (name: string, v: unknown) => {
    setSavedRunValues(prev => ({ ...prev, [name]: v }))
    setSavedRunErrors(prev => { const next = { ...prev }; delete next[name]; return next })
  }

  const handleSubmit = async () => {
    const values = await form.validateFields()

    // Timing — cron (preset or custom) or an interval.
    const when: ScheduleWhen = {}
    if (whenKind === 'every') {
      const every = String(values.every ?? '').trim()
      if (!every) { form.setFields([{ name: 'every', errors: ['Required'] }]); return }
      when.every = every
    } else {
      const cronValue = cronPreset === '__custom__' ? customCron.trim() : cronPreset
      if (!cronValue) { form.setFields([{ name: 'cron', errors: ['Required'] }]); return }
      when.cron = cronValue
    }
    const tz = String(values.timezone ?? '').trim()
    if (tz) when.timezone = tz

    const base: Schedule = { id: editing?.id ?? '', action, when, enabled: editing?.enabled ?? true }
    if (action === 'event') {
      const event = String(values.event ?? '').trim()
      if (!event) { form.setFields([{ name: 'event', errors: ['Required'] }]); return }
      const fields: Record<string, string> = {}
      for (const f of (values.fields ?? []) as { key?: string; value?: string }[]) {
        if (f?.key?.trim()) fields[f.key.trim()] = String(f.value ?? '').trim()
      }
      base.event = event
      base.fields = fields
      if (!base.id) base.id = `rs-${event}-${Date.now()}`
    } else if (action === 'prompt') {
      const prompt = String(values.prompt ?? '').trim()
      if (!prompt) { form.setFields([{ name: 'prompt', errors: ['Required'] }]); return }
      base.prompt = prompt
      if (!base.id) base.id = `rs-prompt-${Date.now()}`
    } else if (action === 'runbook') {
      const runbook = String(values.runbook ?? '').trim()
      if (!runbook) { form.setFields([{ name: 'runbook', errors: ['Required'] }]); return }
      const errs = validateArgs(selectedRunbook?.args ?? [], runbookValues)
      if (Object.keys(errs).length > 0) { setRunbookErrors(errs); return }
      const vals: Record<string, unknown> = {}
      for (const a of selectedRunbook?.args ?? []) {
        const v = runbookValues[a.name]
        if (v !== undefined && v !== '') vals[a.name] = v
      }
      base.runbook = runbook
      base.values = vals
      if (!base.id) base.id = `rs-${runbook}-${Date.now()}`
    } else if (action === 'saved_run') {
      const savedRun = String(values.saved_run ?? '').trim()
      if (!savedRun) { form.setFields([{ name: 'saved_run', errors: ['Required'] }]); return }
      const errs = validateArgs(selectedSavedRun?.params ?? [], savedRunValues)
      if (Object.keys(errs).length > 0) { setSavedRunErrors(errs); return }
      const vals: Record<string, unknown> = {}
      for (const a of selectedSavedRun?.params ?? []) {
        const v = savedRunValues[a.name]
        if (v !== undefined && v !== '') vals[a.name] = v
      }
      base.saved_run = savedRun
      base.values = vals
      if (!base.id) base.id = `rs-${savedRun}-${Date.now()}`
    } else if (action === 'saved_transfer') {
      const transfer = String(values.saved_transfer ?? '').trim()
      if (!transfer) { form.setFields([{ name: 'saved_transfer', errors: ['Required'] }]); return }
      const errs = validateArgs(selectedTransfer?.params ?? [], transferValues)
      if (Object.keys(errs).length > 0) { setTransferErrors(errs); return }
      const vals: Record<string, unknown> = {}
      for (const a of selectedTransfer?.params ?? []) {
        const v = transferValues[a.name]
        if (v !== undefined && v !== '') vals[a.name] = v
      }
      base.saved_transfer = transfer
      base.values = vals
      const dest = String(values.transfer_dest ?? '').trim()
      if (dest) base.transfer_dest = dest
      if (!base.id) base.id = `rs-${transfer}-${Date.now()}`
    } else if (action === 'eval') {
      if (!evalFixture) { message.error('Pick an eval fixture'); return }
      base.eval_fixture = evalFixture
      base.eval_samples = evalSamples
      base.eval_validate = evalValidate
      base.eval_telemetry = evalTelemetry
      if (!base.id) base.id = `rs-eval-${evalFixture}-${Date.now()}`
    } else {
      // A schedule is persisted to disk, so a typed secret would be written in
      // plaintext — never do that. Password-typed args aren't required at save and
      // aren't stored; supply them at fire time via a credential (below) or the
      // runnable's own env resolution. Everything else validates as on the Forms tab.
      const persistArgs = (effectiveRunnable?.args ?? []).filter(a => a.type !== 'password')
      const errs = validateArgs(persistArgs, argValues)
      if (Object.keys(errs).length > 0) { setArgErrors(errs); return }
      const args: Record<string, unknown> = {}
      for (const a of persistArgs) {
        const v = argValues[a.name]
        if (v !== undefined && v !== '') args[a.name] = v
      }
      if (commandPath.length > 0) base.command = commandPath
      if (credentialIds.length > 0) base.credential_ids = credentialIds
      if (target === 'group') {
        const group = String(values.group ?? '').trim()
        if (!group) { form.setFields([{ name: 'group', errors: ['Required'] }]); return }
        base.group = group
      } else if (target === 'anyhost') {
        const list = (values.hosts ?? []) as string[]
        if (list.length === 0) { form.setFields([{ name: 'hosts', errors: ['Pick at least one host'] }]); return }
        base.hosts = list
      } else if (target === 'selected') {
        const list = (values.selected_hosts ?? []) as string[]
        if (list.length === 0) { form.setFields([{ name: 'selected_hosts', errors: ['Pick at least one host'] }]); return }
        base.selected_hosts = list
      } else {
        base.host = values.host
      }
      base.runnable = values.runnable
      base.args = args
      if (!base.id) base.id = `rs-${values.runnable}-${Date.now()}`
    }

    // Optional display name for run cards and session chips. Omit when unset
    // (the console derives a label from the action instead of the id).
    const name = String(values.name ?? '').trim()
    if (name) base.name = name
    // Optional attention cue on fire (any action). Omit the key when unset.
    const sound = String(values.sound ?? '').trim()
    if (sound) base.sound = sound
    // Optional model for a prompt/runbook agent turn. Omit the key when unset.
    const model = String(values.model ?? '').trim()
    if (model && (action === 'prompt' || action === 'runbook')) base.model = model

    setSubmitting(true)
    try {
      if (isEdit) await bridge.update_schedule(base.id, { ...base } as unknown as Record<string, unknown>)
      else await bridge.create_schedule({ ...base } as unknown as Record<string, unknown>)
      message.success(isEdit ? 'Schedule updated' : 'Schedule created')
      onSaved()
      resetAll()
    } catch (e) {
      message.error(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = () => { resetAll(); onCancel() }

  return (
    <Modal
      title={isEdit ? 'Edit schedule' : 'New schedule'}
      open={open}
      onCancel={handleCancel}
      onOk={handleSubmit}
      okText={isEdit ? 'Save' : 'Create'}
      confirmLoading={submitting}
      width={620}
      destroyOnClose
    >
      <Form form={form} layout="vertical" initialValues={{ host: defaultHost, fields: [] }}>
        <Form.Item name="name" label="Name (optional)"
          extra="Shown on its runs and chat chips. Left blank, the console names it from what it does.">
          <Input placeholder="e.g. Morning disk check" maxLength={80} />
        </Form.Item>
        <Form.Item label="When it fires, it will…">
          {/* A dropdown, not a Segmented: six actions (and growing) overflow the
              modal width as a horizontal segmented control. */}
          <Select
            value={action}
            onChange={v => setAction(v as Action)}
            options={visibleScheduleActions([
              { label: 'Run a runnable', value: 'runnable' },
              { label: 'Saved run', value: 'saved_run', disabled: savedRuns.length === 0 },
              { label: 'Saved transfer', value: 'saved_transfer', disabled: savedTransfers.length === 0 },
              { label: 'Run a prompt', value: 'prompt' },
              { label: 'Run a runbook', value: 'runbook' },
              { label: 'Run evals', value: 'eval', disabled: evalFixtures.length === 0 },
              { label: 'Emit an event', value: 'event' },
            ], features)}
          />
          <div style={{ ...labelStyle, marginTop: 6 }}>
            {action === 'runnable'
              ? 'Runs the chosen runnable directly (no agent, no confirm gate).'
              : action === 'saved_run'
              ? 'Fires a saved run — a deterministic preset with its own stored targeting — directly (no agent, no confirm gate). Fill its open params below.'
              : action === 'saved_transfer'
              ? 'Pulls the files a saved transfer matches, directly (no agent, no confirm gate). A date param set to today / yesterday / -1d picks its day each time it fires.'
              : action === 'prompt'
              ? 'Runs an agent turn on a fixed instruction — schedule the assistant to check or do something later. It still confirms before anything destructive.'
              : action === 'runbook'
              ? 'Runs a saved runbook as an agent turn on this cadence. It still confirms before anything destructive.'
              : action === 'eval'
              ? 'Runs a tool-selection eval fixture against the live agent (a dry run — never executes a runnable). The report appears in the Evals tab; a nightly drift check.'
              : 'Emits a matchable event — set up Triggers (source = schedule) to react to it, like email.'}
          </div>
        </Form.Item>

        {/* ── Timing (shared) ── */}
        <Form.Item label="Schedule">
          <Segmented
            value={whenKind}
            onChange={v => setWhenKind(v as WhenKind)}
            options={[{ label: 'Cron', value: 'cron' }, { label: 'Interval', value: 'every' }]}
          />
        </Form.Item>

        {whenKind === 'cron' ? (
          <>
            <Form.Item name="cron" label="Cron" rules={[{ required: false }]}>
              <Select
                placeholder="Choose a preset or custom…"
                value={cronPreset || undefined}
                onChange={(v: string) => { setCronPreset(v); form.setFieldValue('cron', v) }}
              >
                {CRON_PRESETS.map(p => (
                  <Select.Option key={p.value} value={p.value}>{p.label}</Select.Option>
                ))}
              </Select>
            </Form.Item>
            {cronPreset === '__custom__' && (
              <Form.Item label="Cron expression" required>
                <Input
                  placeholder="e.g. 0 2 * * *"
                  value={customCron}
                  onChange={e => setCustomCron(e.target.value)}
                  style={{ fontFamily: MONO_FONT }}
                />
              </Form.Item>
            )}
          </>
        ) : (
          <Form.Item name="every" label="Every" rules={[{ required: true, whitespace: true, message: 'Required' }]}
            help="A number followed by s / m / h / d — e.g. 15m, 1h, 1d.">
            <Input placeholder="15m" style={{ fontFamily: MONO_FONT }} />
          </Form.Item>
        )}

        <Form.Item name="timezone" label="Timezone (optional)"
          help="IANA name, e.g. Europe/London. Blank = this PC's local time.">
          <Input placeholder="Europe/London" style={{ fontFamily: MONO_FONT }} />
        </Form.Item>

        {/* ── Event action ── */}
        {action === 'event' && (
          <>
            <Form.Item name="event" label="Event name" rules={[{ required: true, whitespace: true, message: 'Required' }]}
              help="The event type triggers match on (source = schedule).">
              <Input placeholder="nightly_report" style={{ fontFamily: MONO_FONT }} />
            </Form.Item>
            <div style={{ ...labelStyle, marginBottom: 8 }}>Fields (optional) — extra payload a trigger can match on</div>
            <Form.List name="fields">
              {(fields, { add, remove }) => (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 8 }}>
                  {fields.map(f => (
                    <div key={f.key} style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <Form.Item name={[f.name, 'key']} noStyle>
                        <Input size="small" placeholder="scope" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <span style={{ color: 'var(--rs-text-muted)' }}>=</span>
                      <Form.Item name={[f.name, 'value']} noStyle>
                        <Input size="small" placeholder="prod" style={{ fontFamily: MONO_FONT }} />
                      </Form.Item>
                      <MinusCircleOutlined onClick={() => remove(f.name)} style={{ color: 'var(--rs-text-muted)', cursor: 'pointer' }} />
                    </div>
                  ))}
                  <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => add()} style={{ width: 'fit-content' }}>
                    Add field
                  </Button>
                </div>
              )}
            </Form.List>
          </>
        )}

        {/* ── Prompt action ── */}
        {action === 'prompt' && (
          <Form.Item name="prompt" label="Prompt"
            rules={[{ required: true, whitespace: true, message: 'Required' }]}
            help="What the agent should do when this fires — written as a task, e.g. “Check disk usage on the web hosts and flag anything over 80%.”">
            <Input.TextArea rows={4} placeholder="Check disk usage and flag anything over 80%." />
          </Form.Item>
        )}

        {/* ── Runbook action ── */}
        {action === 'runbook' && (
          <>
            <Form.Item name="runbook" label="Runbook" rules={[{ required: true, message: 'Required' }]}
              help={runbooks.length === 0 ? 'No saved runbooks yet — create one from the Forms tab.' : undefined}>
              <Select placeholder="Select a runbook" showSearch
                filterOption={(input, opt) => (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())}>
                {runbooks.map(s => (
                  <Select.Option key={s.id} value={s.id}>
                    <code>{s.id}</code>
                    {s.title && <span style={{ ...labelStyle, marginLeft: 8 }}>{s.title}</span>}
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
            {selectedRunbook && selectedRunbook.args.length > 0 && (
              <>
                <div style={{ ...labelStyle, marginBottom: 8, marginTop: 4 }}>Parameters</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  {selectedRunbook.args.map(a => (
                    <ArgFieldRow key={a.name} arg={a} error={runbookErrors[a.name]}>
                      <RunbookField
                        arg={a}
                        hostNames={hosts.map(h => h.name)}
                        value={runbookValues[a.name]}
                        error={runbookErrors[a.name]}
                        onChange={v => changeRunbookValue(a.name, v)}
                      />
                    </ArgFieldRow>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {/* ── Saved-run action ── */}
        {action === 'saved_run' && (
          <>
            <Form.Item name="saved_run" label="Saved run" rules={[{ required: true, message: 'Required' }]}
              help={savedRuns.length === 0 ? 'No saved runs yet — create one from the Forms tab.' : undefined}>
              <Select placeholder="Select a saved run" showSearch
                filterOption={(input, opt) => (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())}>
                {savedRuns.map(s => (
                  <Select.Option key={s.id} value={s.id}>
                    <code>{s.id}</code>
                    <span style={{ ...labelStyle, marginLeft: 8 }}>
                      /{s.runnable} on {s.target || s.host}
                    </span>
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
            {selectedSavedRun && (selectedSavedRun.params ?? []).length > 0 && (
              <>
                <div style={{ ...labelStyle, marginBottom: 8, marginTop: 4 }}>Parameters</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  {selectedSavedRun.params.map(a => (
                    <ArgFieldRow key={a.name} arg={a} error={savedRunErrors[a.name]}>
                      <RunbookField
                        arg={a}
                        hostNames={hosts.map(h => h.name)}
                        value={savedRunValues[a.name]}
                        error={savedRunErrors[a.name]}
                        onChange={v => changeSavedRunValue(a.name, v)}
                      />
                    </ArgFieldRow>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {/* ── Saved-transfer action ── */}
        {action === 'saved_transfer' && (
          <>
            <Form.Item name="saved_transfer" label="Saved transfer" rules={[{ required: true, message: 'Required' }]}
              help={savedTransfers.length === 0 ? 'No saved transfers yet. Save one from the Files tab.' : undefined}>
              <Select placeholder="Select a saved transfer" showSearch
                filterOption={(input, opt) => (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())}>
                {savedTransfers.map(s => (
                  <Select.Option key={s.id} value={s.id} disabled={!!s.error}>
                    <code>{s.id}</code>
                    <span style={{ ...labelStyle, marginLeft: 8 }}>from {s.source}</span>
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
            {selectedTransfer && (selectedTransfer.params ?? []).length > 0 && (
              <>
                <div style={{ ...labelStyle, marginBottom: 8, marginTop: 4 }}>Parameters</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginBottom: 14 }}>
                  {(selectedTransfer.params ?? []).map(a => (
                    <ArgFieldRow key={a.name} arg={a} error={transferErrors[a.name]}>
                      <RunbookField
                        arg={a}
                        hostNames={hosts.map(h => h.name)}
                        value={transferValues[a.name]}
                        error={transferErrors[a.name]}
                        onChange={v => changeTransferValue(a.name, v)}
                      />
                    </ArgFieldRow>
                  ))}
                </div>
              </>
            )}
            <Form.Item name="transfer_dest" label="Save files to"
              extra={selectedTransfer?.dest_dir
                ? `Blank = its own folder (${selectedTransfer.dest_dir}).`
                : `Blank = documents/transfers/${selectedTransfer?.id || '<id>'}/ (a schedule has no chat workspace).`}>
              <Input placeholder="optional absolute folder, may use {{today}}" />
            </Form.Item>
          </>
        )}

        {/* ── Eval action ── */}
        {action === 'eval' && (
          <>
            <Form.Item label="Eval fixture"
              help={evalFixtures.length === 0 ? 'No fixtures yet — create one in the Evals tab.' : 'The suite of tool-selection cases to run against the live agent.'}>
              <Select
                placeholder="Select an eval fixture"
                value={evalFixture || undefined}
                onChange={setEvalFixture}
                showSearch
                options={evalFixtures.map(f => ({ value: f.id, label: `${f.id} (${f.cases} case${f.cases === 1 ? '' : 's'})` }))}
              />
            </Form.Item>
            <Space size="large" wrap>
              <Space>
                <span style={labelStyle}>Samples per case</span>
                <InputNumber min={1} max={20} value={evalSamples} onChange={v => setEvalSamples(Number(v) || 1)} />
              </Space>
              <Space>
                <Switch size="small" checked={evalValidate} onChange={setEvalValidate} />
                <Tooltip title="Also assert the model's args wouldn't bounce (preflight validation)">
                  <span style={labelStyle}>Validate args</span>
                </Tooltip>
              </Space>
              <Space>
                <Switch size="small" checked={evalTelemetry} onChange={setEvalTelemetry} />
                <Tooltip title="Emit one turn→llm→tool OTLP trace per case (needs [telemetry] enabled)">
                  <span style={labelStyle}>Emit telemetry</span>
                </Tooltip>
              </Space>
            </Space>
          </>
        )}

        {/* ── Runnable action ── */}
        {action === 'runnable' && (
          <>
            <TargetFormItems
              hosts={hosts}
              groupNames={groupNames}
              target={target}
              onTargetChange={handleTargetChange}
              onHostChange={handleHostChange}
              labelStyle={labelStyle}
            />

            <Form.Item name="runnable" label="Runnable" rules={[{ required: true }]}>
              <Select
                placeholder="Select a runnable"
                onChange={handleRunnableChange}
                showSearch
                optionLabelProp="value"
                filterOption={(input, opt) =>
                  (opt?.value as string ?? '').toLowerCase().includes(input.toLowerCase())
                }
                options={runnableSelectOptions(hostRunnables, labelStyle)}
              />
            </Form.Item>

            {/* Only runnables that declare subcommands get the field — a dropdown
                of their paths, mirroring the saved-run editor. */}
            {selectedRunnable && commandOptions.length > 0 && (
              <Form.Item
                name="command"
                label="Subcommand"
                rules={selectedRunnable.requireCommand
                  ? [{ required: true, message: 'This runnable requires a subcommand' }]
                  : undefined}
                help={selectedRunnable.requireCommand ? undefined : 'Optional — leave blank to run the root command.'}
              >
                <Select
                  placeholder="Select a subcommand…"
                  allowClear
                  optionLabelProp="value"
                  onChange={handleCommandChange}
                  options={commandOptions}
                />
              </Form.Item>
            )}

            {effectiveRunnable && effectiveRunnable.args.length > 0 && (
              <RunnableArgsBlock
                runnable={effectiveRunnable}
                values={argValues}
                errors={argErrors}
                onChange={changeArg}
                labelStyle={labelStyle}
              />
            )}

            {selectedRunnable && creds.length > 0 && (
              <CredentialsSection
                creds={creds}
                selected={credentialIds}
                onChange={setCredentialIds}
                declaredArgNames={new Set((effectiveRunnable?.args ?? []).map(a => a.name))}
                labelStyle={labelStyle}
              />
            )}
          </>
        )}

        {(action === 'prompt' || action === 'runbook') && (
          <Form.Item name="model" label="Model (optional)"
            help="Run this scheduled agent turn on a different model, e.g. a cheaper one for a routine check. Blank = the model set in Settings → Model / API.">
            <ModelSelect />
          </Form.Item>
        )}

        <Form.Item name="sound" label="Play a sound (optional)"
          help="An attention cue played when this schedule fires — a sound you named in Settings → Sounds, or a built-in cue.">
          <SoundSelect />
        </Form.Item>
      </Form>
    </Modal>
  )
}

// "in 2h 5m" / "in 30s" / "due" — a compact countdown to the next fire.
function relativeWhen(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now()
  if (Number.isNaN(ms)) return ''
  if (ms <= 0) return 'due'
  const mins = Math.round(ms / 60000)
  if (mins < 60) return `in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `in ${hours}h ${mins % 60}m`
  const days = Math.floor(hours / 24)
  return `in ${days}d ${hours % 24}h`
}

export function SchedulesView({ hosts, runnables, selectedHost, activeProfile, onOpenHistory }: SchedulesViewProps) {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Schedule | null>(null)
  // Run inbox (runnable-action schedule runs) + event-audit feed, mirroring the
  // Triggers tab. Cards arrive via runspec:schedule_card; their streamed output
  // (keyed by card_id) via runspec:output; events via runspec:schedule_event.
  const [cards, setCards] = useState<Record<string, RunCard>>({})
  const [streams, setStreams] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const [events, setEvents] = useState<TriggerEvent[]>([])
  // Which option card: All, this console's own schedules, or the profile's host
  // schedules by backend (cron / daemon, read-only). Remembered per viewer.
  const [scope, setScopeState] = useState<ScheduleScope>(readScheduleScope)
  const setScope = (v: ScheduleScope) => {
    setScopeState(v)
    try { localStorage.setItem(SCOPE_KEY, v) } catch { /* storage unavailable — fine */ }
  }

  // Every venv's runspec-scheduler status across the profile's connected hosts.
  // Read on open, when the profile's connected set changes, and on Refresh.
  const targets = useMemo(() => scheduleTargets(hosts), [hosts])
  const targetKey = targets.join(',')
  const [hostVenvs, setHostVenvs] = useState<SharedSchedulesVenv[]>(() => hostReadCache?.key === targetKey ? hostReadCache.venvs : [])
  const [hostsLoaded, setHostsLoaded] = useState(() => hostReadCache?.key === targetKey)
  const [hostsUpdatedAt, setHostsUpdatedAt] = useState<number | null>(() => hostReadCache?.key === targetKey ? hostReadCache.at : null)
  const [hostsLoading, setHostsLoading] = useState(false)
  const readSeq = useRef(0)
  const readHosts = useCallback(() => {
    const seq = ++readSeq.current
    const list = targetKey ? targetKey.split(',') : []
    setHostsLoading(true)
    Promise.resolve(bridge.shared_schedules_all(list))
      .then(venvs => {
        if (seq !== readSeq.current) return  // a newer read (profile switch) won
        const at = Date.now()
        hostReadCache = { key: targetKey, venvs: venvs ?? [], at }
        setHostVenvs(venvs ?? [])
        setHostsUpdatedAt(at)
      })
      .catch(e => { if (seq === readSeq.current) message.error(`Couldn't read host schedules: ${String(e)}`) })
      .finally(() => {
        if (seq !== readSeq.current) return
        setHostsLoading(false)
        setHostsLoaded(true)
      })
  }, [targetKey])
  // Hosts connect one by one after a profile switch; wait for the set to settle
  // before fanning out, rather than re-reading on every probe.
  useEffect(() => {
    if (hostReadCache?.key !== targetKey) {
      setHostVenvs([])
      setHostsLoaded(false)
      setHostsUpdatedAt(null)
    }
    const t = setTimeout(readHosts, hostReadCache?.key === targetKey ? 0 : 1200)
    return () => clearTimeout(t)
  }, [readHosts, targetKey])
  const isDark = useIsDark()
  const accent = useAccent()

  const reload = () => bridge.get_schedules().then(setSchedules)

  useEffect(() => { reload() }, [])

  // Seed + subscribe to the schedule run inbox and event log.
  useEffect(() => {
    const cardIds = new Set<string>()
    bridge.get_schedule_board().then(board => {
      board.forEach(c => cardIds.add(c.card_id))
      setCards(Object.fromEntries(board.map(c => [c.card_id, c])))
    }).catch(console.error)
    bridge.get_schedule_events().then(evs => setEvents([...evs].reverse())).catch(console.error)

    const onCard = (e: Event) => {
      const card = (e as CustomEvent).detail as RunCard
      cardIds.add(card.card_id)
      setCards(prev => ({ ...prev, [card.card_id]: card }))
    }
    const onOutput = (e: Event) => {
      const { id, line } = (e as CustomEvent).detail as { id: string; line: string }
      if (!cardIds.has(id)) return  // only schedule-run output
      setStreams(s => ({ ...s, [id]: (s[id] ?? '') + line + '\n' }))
    }
    const onEvent = (e: Event) => {
      const rec = (e as CustomEvent).detail as TriggerEvent
      setEvents(prev => [rec, ...prev].slice(0, 300))
    }
    window.addEventListener('runspec:schedule_card', onCard)
    window.addEventListener('runspec:output', onOutput)
    window.addEventListener('runspec:schedule_event', onEvent)
    return () => {
      window.removeEventListener('runspec:schedule_card', onCard)
      window.removeEventListener('runspec:output', onOutput)
      window.removeEventListener('runspec:schedule_event', onEvent)
    }
  }, [])

  const handleDelete = async (id: string) => {
    await bridge.delete_schedule(id)
    setSchedules(s => s.filter(x => x.id !== id))
  }

  const handleSaved = () => {
    setModalOpen(false)
    setEditing(null)
    reload()
  }

  const openNew = () => { setEditing(null); setModalOpen(true) }
  const openEdit = (s: Schedule) => { setEditing(s); setModalOpen(true) }

  const handleToggle = async (s: Schedule, enabled: boolean) => {
    // Optimistic flip; persist via update_schedule, then refresh for nextRun.
    setSchedules(prev => prev.map(x => (x.id === s.id ? { ...x, enabled } : x)))
    try {
      await bridge.update_schedule(s.id, { ...s, enabled } as unknown as Record<string, unknown>)
      reload()
    } catch (e) {
      message.error(String(e))
      reload()
    }
  }

  const handleRunNow = async (s: Schedule) => {
    try {
      const res = await bridge.run_schedule_now(s.id)
      if (res.action === 'event') message.success(`Emitted event “${res.event}”`)
      else if (res.action === 'prompt') message.success('Running the scheduled prompt…')
      else if (res.action === 'runbook') message.success(`Running runbook ${res.runnable}…`)
      else if (res.action === 'saved_run') message.success(`Running saved run ${res.runnable}…`)
      else if (res.action === 'saved_transfer') message.success(`Running saved transfer ${res.runnable}…`)
      else message.success(`Running /${res.runnable} on ${res.host}…`)
    } catch (e) {
      message.error(String(e))
    }
  }

  const dimCol = textMuted(isDark)
  const keyCol = accent
  const whereStyle = { fontFamily: MONO_FONT, fontSize: 13 }
  const whenText = (w: ScheduleWhen) => {
    const base = w.every ? `every ${w.every}` : (w.cron ?? '')
    return w.timezone ? `${base}  (${w.timezone})` : base
  }

  const detailFor = (s: Schedule): Record<string, unknown> =>
    s.action === 'event' ? (s.fields ?? {})
      : s.action === 'runbook' || s.action === 'saved_run' || s.action === 'saved_transfer' ? (s.values ?? {})
      : s.action === 'prompt' ? {}
      : (s.args ?? {})

  // Inline summary of the args/fields — first couple of entries as tiny tags,
  // with a "+N" overflow. Full set stays in the expandable row.
  const detailSummary = (s: Schedule) => {
    const entries = Object.entries(detailFor(s))
    if (entries.length === 0) return <span style={{ color: dimCol }}>—</span>
    const shown = entries.slice(0, 2)
    const prefix = s.action === 'runnable' ? '--' : ''
    return (
      <Space size={4} wrap>
        {shown.map(([k, v]) => (
          <span key={k} style={{ fontFamily: MONO_FONT, fontSize: 12 }}>
            <span style={{ color: keyCol }}>{prefix}{k}</span>
            <span style={{ color: dimCol }}>={String(v)}</span>
          </span>
        ))}
        {entries.length > shown.length && (
          <span style={{ color: dimCol, fontSize: 12 }}>+{entries.length - shown.length}</span>
        )}
      </Space>
    )
  }

  // The kind of action is a quiet label, not a colour each: colour is kept for
  // status, so a "runbook" chip can't read as a warning.
  const kind = (label: string) => <StatusTag tone="neutral">{label}</StatusTag>
  const actionCell = (s: Schedule) =>
    s.action === 'event'
      ? <Space size={6}>{kind('event')}<code>{s.event}</code></Space>
      : s.action === 'prompt'
          ? <Space size={6}>{kind('prompt')}<span style={{ color: dimCol, fontSize: 13 }}>{(s.prompt ?? '').slice(0, 48)}{(s.prompt ?? '').length > 48 ? '…' : ''}</span></Space>
          : s.action === 'runbook'
          ? <Space size={6}>{kind('runbook')}<code>{s.runbook}</code></Space>
          : s.action === 'saved_run'
          ? <Space size={6}>{kind('saved run')}<code>{s.saved_run}</code></Space>
          : s.action === 'saved_transfer'
          ? <Space size={6}>{kind('saved transfer')}<code>{s.saved_transfer}</code></Space>
          : s.action === 'eval'
          ? <Space size={6}>{kind('evals')}<code>{s.eval_fixture}</code></Space>
          : <Space size={6}>{kind('run')}<code>/{s.runnable}{s.command?.length ? ` ${s.command.join(' ')}` : ''}</code></Space>

  const columns = [
    {
      title: '',
      key: 'enabled',
      width: 56,
      render: (_: unknown, s: Schedule) => (
        <Tooltip title={s.enabled === false ? 'Disabled — won’t fire' : 'Enabled'}>
          <Switch size="small" checked={s.enabled !== false} onChange={v => handleToggle(s, v)} />
        </Tooltip>
      ),
    },
    {
      title: 'Action',
      key: 'action',
      render: (_: unknown, s: Schedule) => (
        <>
          {s.name && <div style={{ fontWeight: 600, marginBottom: 2 }}>{s.name}</div>}
          {actionCell(s)}
        </>
      ),
    },
    {
      title: 'Where',
      key: 'where',
      render: (_: unknown, s: Schedule) =>
        s.action === 'runnable'
          ? (s.group
              ? <span style={whereStyle}><span style={{ color: dimCol }}>group </span>{s.group}</span>
              : (s.hosts?.length
                  ? <span style={whereStyle}><span style={{ color: dimCol }}>first up of </span>{s.hosts.join(', ')}</span>
                  : (s.host ? <span style={whereStyle}>{s.host}</span> : <span style={{ color: dimCol }}>—</span>)))
          : (s.action === 'prompt' || s.action === 'runbook' || s.action === 'eval')
          ? <span style={{ color: dimCol, fontSize: 13 }}>the assistant</span>
          : s.action === 'saved_run'
          ? <span style={{ color: dimCol, fontSize: 13 }}>the saved run's target</span>
          : s.action === 'saved_transfer'
          ? <span style={{ color: dimCol, fontSize: 13 }}>the transfer's source</span>
          : <span style={{ color: dimCol }}>—</span>,
    },
        {
      title: 'Schedule',
      key: 'schedule',
      render: (_: unknown, s: Schedule) => s.when.every
        ? <code style={{ fontSize: 13 }}>{whenText(s.when)}</code>
        : <span>
            <WhenCell when={s.when.cron ?? ''} />
            {s.when.timezone && <span style={{ fontSize: 12, color: dimCol }}> ({s.when.timezone})</span>}
          </span>,
    },
    {
      title: 'Details',
      key: 'detail',
      render: (_: unknown, s: Schedule) => detailSummary(s),
    },
    {
      title: 'Next run',
      dataIndex: 'nextRun',
      key: 'nextRun',
      render: (v: string | undefined, s: Schedule) => {
        if (s.enabled === false) return <StatusTag tone="neutral">paused</StatusTag>
        if (!v) return <span style={{ color: dimCol }}>—</span>
        return (
          <Tooltip title={new Date(v).toLocaleString()}>
            <span style={{ color: dimCol }}>{relativeWhen(v)}</span>
          </Tooltip>
        )
      },
    },
    {
      title: '',
      key: 'actions',
      width: 132,
      render: (_: unknown, s: Schedule) => (
        <Space size={2}>
          <IconAction label={`Run ${s.name || 'schedule'} now`} tip="Run now" icon={<PlayCircleOutlined />} onClick={() => handleRunNow(s)} />
          <IconAction label={`Edit ${s.name || 'schedule'}`} tip="Edit" icon={<EditOutlined />} onClick={() => openEdit(s)} />
          <RowActions more={[{
            key: 'delete', label: 'Delete', icon: <DeleteOutlined />, danger: true,
            confirm: 'Delete this schedule?', onClick: () => handleDelete(s.id),
          }]} />
        </Space>
      ),
    },
  ]

  const scheduleTable = (
    <Table
      dataSource={schedules}
      columns={columns}
      rowKey="id"
      size="small"
      pagination={false}
      locale={{ emptyText: <EmptyState message="No schedules on this console yet." action={{ label: 'New schedule', icon: <PlusOutlined />, onClick: openNew }} /> }}
      rowClassName={s => (s.enabled === false ? 'rs-schedule-disabled' : '')}
      expandable={{
        rowExpandable: s => Object.keys(detailFor(s)).length > 0,
        expandedRowRender: s => (
          <div style={{ paddingLeft: 24, fontFamily: MONO_FONT, fontSize: 13, color: dimCol }}>
            {Object.entries(detailFor(s)).map(([k, v]) => (
              <span key={k} style={{ marginRight: 12 }}>
                <span style={{ color: keyCol }}>
                  {s.action === 'runnable' ? `--${k}` : k}
                </span>={String(v)}
              </span>
            ))}
          </div>
        ),
      }}
    />
  )

  const hostGroups = useMemo(
    () => Object.fromEntries(hosts.map(h => [h.name, h.group ?? ''])) as Record<string, string>,
    [hosts],
  )
  const collected = useMemo(
    () => collectRows(schedules, hostVenvs, { cards: Object.values(cards), hostGroups }),
    [schedules, hostVenvs, cards, hostGroups],
  )
  const counts = useMemo(() => scopeCounts(collected), [collected])

  const scopeToggle = (
    <ScheduleScopeCards
      value={scope}
      onChange={setScope}
      counts={counts}
      hostsPending={!hostsLoaded}
      noDaemons={collected.daemons.length === 0}
    />
  )

  if (scope !== 'console') {
    return (
      <div>
        {scopeToggle}
        <AllSchedulesView
          scope={scope}
          collected={collected}
          loading={hostsLoading}
          loaded={hostsLoaded}
          updatedAt={hostsUpdatedAt}
          hostCount={targets.length}
          activeProfile={activeProfile}
          onRefresh={readHosts}
          onOpenHistory={onOpenHistory}
          runnables={runnables}
        />
      </div>
    )
  }

  return (
    <div>
      {scopeToggle}
      {/* Schedules — collapsible table, mirroring the Triggers tab. Schedules are
          gated by the same working-hours rota (set on the Rota tab); fires outside
          it are skipped and recorded in the event log below. */}
      <Collapse
        size="small"
        defaultActiveKey={['schedules']}
        style={{ marginBottom: 20 }}
        items={[{
          key: 'schedules',
          label: (
            <span>
              <CalendarOutlined /> Schedules
              <span style={{ color: dimCol, fontSize: 13 }}>{' · '}{schedules.length} configured</span>
            </span>
          ),
          extra: (
            <Space size={8} onClick={e => e.stopPropagation()}>
              <IconAction label="Reload schedules" icon={<ReloadOutlined />} onClick={reload} />
              <Button type="primary" icon={<PlusOutlined />} size="small" onClick={openNew}>
                New schedule
              </Button>
            </Space>
          ),
          children: scheduleTable,
        }]}
      />

      {/* Run inbox — runnable-action schedule runs (no agent, no gate). */}
      <RunInbox
        cards={cards}
        selected={selected}
        onSelect={setSelected}
        onStop={id => bridge.cancel_invocation(id).catch(console.error)}
        streams={streams}
        emptyText="No schedule runs yet."
      />

      {/* Event log — every scheduled fire + its disposition (shared widget). */}
      <EventLog events={events} hint="every scheduled fire, run or skipped" />

      <ScheduleModal
        open={modalOpen}
        hosts={hosts}
        runnables={runnables}
        defaultHost={selectedHost}
        editing={editing}
        onCancel={() => { setModalOpen(false); setEditing(null) }}
        onSaved={handleSaved}
      />
    </div>
  )
}
