import type { BridgeApi, Host, Runnable, HistoryRecord, Schedule } from './index'

const MOCK_RUNNABLES: Runnable[] = [
  {
    name: 'backup',
    host: 'local',
    description: 'Back up a directory to remote storage',
    args: [
      { name: 'source', type: 'path', required: true, description: 'Directory to back up' },
      { name: 'dest', type: 'str', required: true, description: 'Destination path' },
      { name: 'dry-run', type: 'flag', required: false, default: false },
    ],
  },
  {
    name: 'get-alerts',
    host: 'local',
    description: 'Pull active alerts from all configured sources',
    args: [
      { name: 'since', type: 'str', required: false, default: 'yesterday' },
    ],
  },
  {
    name: 'setup-keys',
    host: 'local',
    description: 'Copy SSH public key to a remote host',
    args: [
      { name: 'host', type: 'str', required: true, description: 'Remote hostname or IP' },
      { name: 'user', type: 'str', required: true, description: 'Remote username' },
    ],
  },
  {
    name: 'log-rotate',
    host: 'prod-1',
    description: 'Rotate and compress log files on the host',
    args: [
      { name: 'keep', type: 'int', required: false, default: 7, description: 'Days of logs to keep' },
    ],
  },
  {
    name: 'cache-purge',
    host: 'prod-1',
    description: 'Purge stale cache entries',
    args: [],
  },
]

const MOCK_HOSTS: Host[] = [
  { name: 'local', connected: true, runnableCount: 3 },
  { name: 'prod-1', connected: true, runnableCount: 2 },
  { name: 'prod-2', connected: false, runnableCount: 0 },
]

const MOCK_HISTORY: HistoryRecord[] = [
  { id: '1', runnable: 'backup', host: 'local', exitCode: 0, durationMs: 3201, ts: new Date(Date.now() - 3600000).toISOString() },
  { id: '2', runnable: 'log-rotate', host: 'prod-1', exitCode: 0, durationMs: 812, ts: new Date(Date.now() - 7200000).toISOString() },
  { id: '3', runnable: 'get-alerts', host: 'local', exitCode: 1, durationMs: 450, ts: new Date(Date.now() - 10800000).toISOString() },
]

const MOCK_SCHEDULES: Schedule[] = [
  { id: 'rs-backup-daily', runnable: 'backup', host: 'local', schedule: 'Daily at 02:00', nextRun: 'Tomorrow 02:00' },
  { id: 'rs-log-rotate-weekly', runnable: 'log-rotate', host: 'prod-1', schedule: 'Weekly Sun 03:00', nextRun: 'Sun 03:00' },
]

let invocationCounter = 0

export const mockApi: BridgeApi = {
  get_hosts: async () => MOCK_HOSTS,

  get_runnables: async (_host) => MOCK_RUNNABLES,

  get_history: async (_host, _runnable) => MOCK_HISTORY,

  get_schedules: async () => MOCK_SCHEDULES,

  get_config: async () => ({ jumpHosts: MOCK_HOSTS.filter(h => h.name !== 'local').map(h => h.name) }),

  save_config: async (_data) => {},

  create_schedule: async (_data) => {},

  delete_schedule: async (id) => { console.log('[mock] delete_schedule', id) },

  invoke_runnable: async (host, runnable, args) => {
    const id = `inv-${++invocationCounter}`
    console.log('[mock] invoke_runnable', { host, runnable, args, id })

    // Simulate streaming output via CustomEvents after a short delay
    const lines = [
      `$ runspec ${runnable} ${Object.entries(args).map(([k, v]) => `--${k}=${v}`).join(' ')}`,
      `[${runnable}] starting on ${host}...`,
      `[${runnable}] processing step 1 of 3`,
      `[${runnable}] processing step 2 of 3`,
      `[${runnable}] processing step 3 of 3`,
      `[${runnable}] done.`,
    ]
    lines.forEach((line, i) => {
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('runspec:output', { detail: { id, line, stream: 'stdout' } }))
        if (i === lines.length - 1) {
          setTimeout(() => {
            window.dispatchEvent(new CustomEvent('runspec:run_end', { detail: { id, exit_code: 0, duration_ms: 1234 } }))
          }, 100)
        }
      }, i * 300)
    })

    return id
  },

  send_chat: async (message, _invocationId) => {
    const id = `chat-${++invocationCounter}`
    const response = `I can help you run runnables. You said: "${message}". Try typing / to see available commands.`
    response.split(' ').forEach((token, i) => {
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('runspec:token', { detail: { id, token: token + ' ' } }))
        if (i === response.split(' ').length - 1) {
          window.dispatchEvent(new CustomEvent('runspec:run_end', { detail: { id, exit_code: 0, duration_ms: 500 } }))
        }
      }, i * 80)
    })
    return id
  },
}
