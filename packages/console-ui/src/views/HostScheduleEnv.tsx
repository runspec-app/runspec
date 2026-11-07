/**
 * HostScheduleEnv — in the host schedule dialog, the credentials the chosen
 * runnable reads from the environment and whether the venv's `.runspec_env` on
 * the host sets them. A host schedule never carries credentials (the fire runs
 * unattended on the host), so this file is where they have to come from; the
 * panel says which variables are missing and opens Env files on that venv.
 * Backed by `bridge.host_schedule_env_check` — variable names only, no values.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Alert, Button, Space, Spin, Typography } from 'antd'
import { CheckCircleOutlined, CodeOutlined, MinusCircleOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons'
import { bridge, type ArgDef, type HostScheduleEnvCheck, type HostScheduleEnvNeed } from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

export type NeedState = 'file' | 'schedule' | 'missing' | 'optional'

/** How one credential arg is covered: by the env file, by a value stored in the
 *  schedule (only the user half of a pair: a secret typed into the schedule
 *  would sit in its config, so it doesn't count), or not at all (missing when
 *  the arg is required, optional otherwise). */
export function needState(n: HostScheduleEnvNeed, arg: ArgDef | undefined, values: Record<string, unknown>): NeedState {
  if (n.set_by) return 'file'
  const v = values[n.arg]
  if (!n.secret && v !== undefined && v !== null && String(v) !== '') return 'schedule'
  if (arg && (!arg.required || arg.default !== undefined)) return 'optional'
  return 'missing'
}

/** The variable names to suggest: declared aliases first (friendlier), then the
 *  automatic RUNSPEC_<R>_ARG_<A>. */
export function suggestKeys(n: HostScheduleEnvNeed): string[] {
  return [...n.keys.filter(k => !k.startsWith('RUNSPEC_')), ...n.keys.filter(k => k.startsWith('RUNSPEC_'))]
}

/** Ask the app to open Env files on this venv. */
export function openEnvFiles(host: string, runspecPath: string) {
  window.dispatchEvent(new CustomEvent('runspec:open_env_files', { detail: { host, runspec_path: runspecPath } }))
}

const ICON: Record<NeedState, ReactNode> = {
  file: <CheckCircleOutlined style={{ color: 'var(--rs-success, #52c41a)' }} />,
  schedule: <CheckCircleOutlined style={{ color: 'var(--rs-success, #52c41a)' }} />,
  missing: <WarningOutlined style={{ color: 'var(--rs-warning, #faad14)' }} />,
  optional: <MinusCircleOutlined style={{ opacity: 0.55 }} />,
}

function Keys({ keys }: { keys: string[] }) {
  return <>{keys.map((k, i) => <span key={k}>{i > 0 && (i === keys.length - 1 ? ' or ' : ', ')}<Text code style={{ fontSize: 12 }}>{k}</Text></span>)}</>
}

export function HostScheduleEnv({ host, venv, runnable, command, args, values }: {
  host: string
  venv: string
  runnable: string
  command: string[]
  args: ArgDef[]
  values: Record<string, unknown>
}) {
  const [check, setCheck] = useState<HostScheduleEnvCheck | null>(null)
  const [loading, setLoading] = useState(false)
  const cmdKey = command.join(' ')

  const load = useCallback(() => {
    let live = true
    setLoading(true)
    Promise.resolve(bridge.host_schedule_env_check({ host, venv, runnable, command: cmdKey ? cmdKey.split(' ') : [] }))
      .then(r => { if (live) setCheck(r) })
      .catch(e => { if (live) setCheck({ ok: false, error: String(e), host, runspec_path: '', path: '', exists: false, needs: [], override: '' }) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [host, venv, runnable, cmdKey])

  useEffect(() => {
    setCheck(null)
    if (!host || !venv || !runnable) return
    return load()
  }, [host, venv, runnable, load])

  const hasPassword = args.some(a => a.type === 'password')
  if (!runnable) return null
  if (!check) {
    return loading && hasPassword
      ? <div style={{ marginBottom: 16 }}><Spin size="small" /> <Text type="secondary">Checking the venv's .runspec_env…</Text></div>
      : null
  }
  const needs = check.needs ?? []
  if (check.ok && needs.length === 0 && !hasPassword) return null

  const byName = new Map(args.map(a => [a.name, a]))
  const rows = needs.map(n => ({ n, state: needState(n, byName.get(n.arg), values) }))
  const missing = rows.filter(r => r.state === 'missing')
  const type = !check.ok ? 'warning' : missing.length ? 'warning' : rows.every(r => r.state === 'file' || r.state === 'schedule') ? 'success' : 'info'
  const title = !check.ok
    ? "Couldn't check the venv's .runspec_env"
    : missing.length
      ? `${missing.length} credential${missing.length === 1 ? '' : 's'} not set on the host`
      : 'Credentials come from the host'

  return (
    <Alert
      type={type}
      showIcon
      style={{ marginBottom: 16 }}
      message={title}
      action={
        <Space direction="vertical" size={4}>
          <Button size="small" icon={<CodeOutlined />} disabled={!check.runspec_path}
            onClick={() => openEnvFiles(check.host || host, check.runspec_path)}>Open Env files</Button>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => load()}>Check again</Button>
        </Space>
      }
      description={
        <div style={{ fontSize: 13 }}>
          <div style={{ marginBottom: 6 }}>
            A host schedule never carries credentials: each fire reads them on the host from{' '}
            <Text code style={{ fontSize: 12 }}>{check.path || '.runspec_env'}</Text>
            {check.ok && !check.exists && ' (no such file yet)'}.
          </div>
          {!check.ok && <div><Text type="secondary">{check.error}</Text></div>}
          {check.override && (
            <div style={{ marginBottom: 6 }}>
              <Text type="warning">This runnable names its own env file in runspec.toml (<Text code style={{ fontSize: 12 }}>{check.override}</Text>), so set them there instead.</Text>
            </div>
          )}
          {rows.map(({ n, state }) => (
            <div key={`${n.command}|${n.arg}`} style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginTop: 3 }}>
              <span>{ICON[state]}</span>
              <span>
                <span style={{ fontFamily: MONO_FONT }}>--{n.arg}</span>
                {n.command && <Text type="secondary"> ({n.command})</Text>}
                {': '}
                {state === 'file' && <>set by <Text code style={{ fontSize: 12 }}>{n.set_by}</Text></>}
                {state === 'schedule' && <>set in this schedule</>}
                {(state === 'missing' || state === 'optional') && (
                  <>{state === 'optional' ? 'optional, not set. To set it, add ' : 'add '}<Keys keys={suggestKeys(n)} /></>
                )}
                {n.blank.length > 0 && state !== 'file' && (
                  <Text type="secondary"> (<Keys keys={n.blank} /> {n.blank.length === 1 ? 'is' : 'are'} in the file but empty)</Text>
                )}
              </span>
            </div>
          ))}
          {check.ok && rows.length === 0 && hasPassword && (
            <div><Text type="secondary">Set its password arguments in the file before the first fire.</Text></div>
          )}
        </div>
      }
    />
  )
}
