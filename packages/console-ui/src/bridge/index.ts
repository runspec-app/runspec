export interface ArgDef {
  name: string
  type: string
  required: boolean
  description?: string
  default?: unknown
}

export interface Runnable {
  name: string
  host: string
  description?: string
  args: ArgDef[]
}

export interface Host {
  name: string
  connected: boolean
  runnableCount: number
}

export interface HistoryRecord {
  id: string
  runnable: string
  host: string
  exitCode: number
  durationMs: number
  ts: string
}

export interface Schedule {
  id: string
  runnable: string
  host: string
  schedule: string
  nextRun: string
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
