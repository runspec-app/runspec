export interface ArgDef {
  name: string
  type: string
  required: boolean
  description?: string
  default?: unknown
}

export interface Runnable {
  name: string
  group: string   // venv directory name — the execution environment identity
  host: string
  description?: string
  args: ArgDef[]
}

export interface Host {
  name: string
  connected: boolean
  runnableCount: number
  groups: string[]
}

export interface JumpHost {
  name: string        // identifier — becomes the key in jump_hosts.toml
  hostname: string
  user?: string       // falls back to global SSH default in settings
  region?: string
  datacenter?: string
  role: 'primary' | 'secondary'
  identityFile?: string
}

export interface HistoryLogLine {
  ts: string
  level: string
  message: string
}

export interface HistoryRecord {
  id: string
  runnable: string
  group: string
  host: string
  operator: string   // who triggered the run (console user or "Scheduled Task")
  runAs: string      // OS user the runnable executed as on the host (from run_as in spec)
  exitCode: number
  durationMs: number
  ts: string
  args: Record<string, unknown>      // arguments used for this invocation (from run_summary)
  logLines: HistoryLogLine[]         // log records belonging to this invocation
}

export interface Schedule {
  id: string
  runnable: string
  host: string
  schedule: string
  nextRun: string
}

export interface InFlightRecord {
  id: string
  runnable: string
  group: string
  host: string
  operator: string
  runAs: string
  startedAt: string
  args: Record<string, unknown>
}

export interface BridgeApi {
  get_hosts: () => Promise<Host[]>
  get_runnables: (host: string) => Promise<Runnable[]>
  get_history: (host: string, runnable?: string) => Promise<HistoryRecord[]>
  get_schedules: () => Promise<Schedule[]>
  get_config: () => Promise<Record<string, unknown>>
  save_config: (data: Record<string, unknown>) => Promise<void>
  create_schedule: (data: Record<string, unknown>) => Promise<void>
  delete_schedule: (id: string) => Promise<void>
  invoke_runnable: (host: string, runnable: string, args: Record<string, unknown>) => Promise<string>
  send_chat: (message: string, invocationId?: string) => Promise<string>
  get_in_flight: () => Promise<InFlightRecord[]>
}

declare global {
  interface Window {
    pywebview?: { api: BridgeApi }
  }
}

// Use real pywebview bridge in production, mock in dev
async function resolveBridge(): Promise<BridgeApi> {
  if (window.pywebview?.api) return window.pywebview.api
  const { mockApi } = await import('./mock')
  return mockApi
}

export const bridge: BridgeApi = new Proxy({} as BridgeApi, {
  get(_target, prop) {
    return async (...args: unknown[]) => {
      const api = await resolveBridge()
      return (api[prop as keyof BridgeApi] as (...a: unknown[]) => unknown)(...args)
    }
  },
})
