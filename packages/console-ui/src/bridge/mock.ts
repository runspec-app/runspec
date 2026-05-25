import type { BridgeApi, Host, JumpHost, Runnable, HistoryRecord, Schedule, InFlightRecord } from './index'

const MOCK_RUNNABLES: Runnable[] = [
  {
    name: 'backup',
    group: 'ops-tools',
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
    group: 'ops-tools',
    host: 'local',
    description: 'Pull active alerts from all configured sources',
    args: [
      { name: 'since', type: 'str', required: false, default: 'yesterday' },
    ],
  },
  {
    name: 'setup-keys',
    group: 'ops-tools',
    host: 'local',
    description: 'Copy SSH public key to a remote host',
    args: [
      { name: 'host', type: 'str', required: true, description: 'Remote hostname or IP' },
      { name: 'user', type: 'str', required: true, description: 'Remote username' },
    ],
  },
  {
    name: 'log-rotate',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Rotate and compress log files on the host',
    args: [
      { name: 'keep', type: 'int', required: false, default: 7, description: 'Days of logs to keep' },
    ],
  },
  {
    name: 'cache-purge',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Purge stale cache entries',
    args: [],
  },
  {
    // Same runnable name as ops-tools/backup — demonstrates the collision scenario
    name: 'backup',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Back up database snapshots to object storage',
    args: [
      { name: 'db', type: 'str', required: true, description: 'Database name' },
      { name: 'dry-run', type: 'flag', required: false, default: false },
    ],
  },
]

const MOCK_JUMP_HOSTS: JumpHost[] = [
  { name: 'eu-dc1-primary',   hostname: 'hostname1.company.com', user: 'jason', region: 'europe', datacenter: 'datacenter-1', role: 'primary' },
  { name: 'eu-dc2-secondary', hostname: 'hostname2.company.com', user: 'jason', region: 'europe', datacenter: 'datacenter-2', role: 'secondary' },
  { name: 'eu-dc1-primary-logs', hostname: 'hostname3.company.com', user: 'jason', region: 'europe', datacenter: 'datacenter-1', role: 'primary' },
]

const MOCK_HOSTS: Host[] = [
  { name: 'local',  connected: true,  runnableCount: 3, groups: ['ops-tools'] },
  { name: 'prod-1', connected: true,  runnableCount: 3, groups: ['platform-core'] },
  { name: 'prod-2', connected: false, runnableCount: 0, groups: [] },
]

const MOCK_HISTORY: HistoryRecord[] = [
  {
    id: '1', runnable: 'backup', group: 'ops-tools', host: 'local', operator: 'Jason Finestone', runAs: 'DESKTOP\\jason',
    exitCode: 0, durationMs: 3201, ts: new Date(Date.now() - 3600000).toISOString(),
    args: { source: 'C:/Users/jason/documents', dest: 'Z:/backups/documents', 'dry-run': false },
    logLines: [
      { ts: new Date(Date.now() - 3605000).toISOString(), level: 'INFO', message: 'backup starting' },
      { ts: new Date(Date.now() - 3604000).toISOString(), level: 'INFO', message: 'scanning source: C:/Users/jason/documents' },
      { ts: new Date(Date.now() - 3603000).toISOString(), level: 'INFO', message: '1,243 files found (2.1 GB)' },
      { ts: new Date(Date.now() - 3602000).toISOString(), level: 'INFO', message: 'copying to Z:/backups/documents' },
      { ts: new Date(Date.now() - 3601000).toISOString(), level: 'INFO', message: 'backup complete' },
    ],
  },
  {
    id: '2', runnable: 'log-rotate', group: 'platform-core', host: 'prod-1', operator: 'Scheduled Task', runAs: 'svc-runner',
    exitCode: 0, durationMs: 812, ts: new Date(Date.now() - 7200000).toISOString(),
    args: { keep: 7 },
    logLines: [
      { ts: new Date(Date.now() - 7205000).toISOString(), level: 'INFO', message: 'log-rotate starting, keep=7 days' },
      { ts: new Date(Date.now() - 7204000).toISOString(), level: 'INFO', message: 'rotating /var/log/app.log' },
      { ts: new Date(Date.now() - 7203000).toISOString(), level: 'INFO', message: 'deleted 3 files older than 7 days' },
    ],
  },
  {
    id: '3', runnable: 'get-alerts', group: 'ops-tools', host: 'local', operator: 'Jason Finestone', runAs: 'DESKTOP\\jason',
    exitCode: 1, durationMs: 450, ts: new Date(Date.now() - 10800000).toISOString(),
    args: { since: 'yesterday' },
    logLines: [
      { ts: new Date(Date.now() - 10805000).toISOString(), level: 'INFO',  message: 'get-alerts starting' },
      { ts: new Date(Date.now() - 10804000).toISOString(), level: 'INFO',  message: 'connecting to Datadog API' },
      { ts: new Date(Date.now() - 10803000).toISOString(), level: 'ERROR', message: 'Datadog API request failed: 401 Unauthorized' },
    ],
  },
  {
    id: '4', runnable: 'cache-purge', group: 'platform-core', host: 'prod-1', operator: 'Scheduled Task', runAs: 'svc-runner',
    exitCode: 0, durationMs: 201, ts: new Date(Date.now() - 86400000).toISOString(),
    args: {},
    logLines: [
      { ts: new Date(Date.now() - 86405000).toISOString(), level: 'INFO', message: 'cache-purge starting' },
      { ts: new Date(Date.now() - 86404000).toISOString(), level: 'INFO', message: 'purged 412 stale entries' },
    ],
  },
  {
    id: '5', runnable: 'backup', group: 'platform-core', host: 'prod-1', operator: 'Jason Finestone', runAs: 'svc-runner',
    exitCode: 0, durationMs: 2980, ts: new Date(Date.now() - 90000000).toISOString(),
    args: { db: 'postgres-main', 'dry-run': false },
    logLines: [
      { ts: new Date(Date.now() - 90005000).toISOString(), level: 'INFO', message: 'backup starting — db=postgres-main' },
      { ts: new Date(Date.now() - 90004000).toISOString(), level: 'INFO', message: 'creating snapshot' },
      { ts: new Date(Date.now() - 90003000).toISOString(), level: 'INFO', message: 'snapshot complete (1.8 GB)' },
      { ts: new Date(Date.now() - 90002000).toISOString(), level: 'INFO', message: 'uploading to s3://backups/postgres-main' },
      { ts: new Date(Date.now() - 90001000).toISOString(), level: 'INFO', message: 'backup complete' },
    ],
  },
  {
    id: '6', runnable: 'log-rotate', group: 'platform-core', host: 'prod-1', operator: 'Jason Finestone', runAs: 'svc-runner',
    exitCode: 0, durationMs: 930, ts: new Date(Date.now() - 172800000).toISOString(),
    args: { keep: 14 },
    logLines: [
      { ts: new Date(Date.now() - 172805000).toISOString(), level: 'INFO', message: 'log-rotate starting, keep=14 days' },
      { ts: new Date(Date.now() - 172804000).toISOString(), level: 'INFO', message: 'rotating /var/log/app.log' },
      { ts: new Date(Date.now() - 172803000).toISOString(), level: 'INFO', message: 'no files older than 14 days found' },
    ],
  },
]

const MOCK_SCHEDULES: Schedule[] = [
  { id: 'rs-backup-daily', runnable: 'backup', host: 'local', schedule: 'Daily at 02:00', nextRun: 'Tomorrow 02:00' },
  { id: 'rs-log-rotate-weekly', runnable: 'log-rotate', host: 'prod-1', schedule: 'Weekly Sun 03:00', nextRun: 'Sun 03:00' },
]

const MOCK_IN_FLIGHT: InFlightRecord[] = [
  {
    id: 'inf-1',
    runnable: 'log-rotate',
    group: 'platform-core',
    host: 'prod-1',
    operator: 'Scheduled Task',
    runAs: 'svc-runner',
    startedAt: new Date(Date.now() - 45000).toISOString(),
    args: { keep: 7 },
  },
  {
    id: 'inf-2',
    runnable: 'backup',
    group: 'platform-core',
    host: 'prod-1',
    operator: 'Jason Finestone',
    runAs: 'svc-runner',
    startedAt: new Date(Date.now() - 8000).toISOString(),
    args: { db: 'postgres-main' },
  },
]

let invocationCounter = 0

export const mockApi: BridgeApi = {
  get_hosts: async () => MOCK_HOSTS,

  get_runnables: async (_host) => MOCK_RUNNABLES,

  get_history: async (_host, _runnable) => MOCK_HISTORY,

  get_schedules: async () => MOCK_SCHEDULES,

  get_config: async () => ({
    ssh: { user: 'jason', identityFile: '~/.ssh/runspec_ed25519' },
    llm: { apiBaseUrl: '', model: 'claude-opus-4-7' },
    jumpHosts: MOCK_JUMP_HOSTS,
  }),

  save_config: async (data) => { console.log('[mock] save_config', data) },

  create_schedule: async (_data) => {},

  delete_schedule: async (id) => { console.log('[mock] delete_schedule', id) },

  import_jump_hosts: async (content) => {
    // Minimal TOML section parser — mirrors what Python's tomllib.loads() does on the real bridge
    const hosts: JumpHost[] = []
    let name = ''
    let current: Record<string, string> = {}
    for (const raw of content.split('\n')) {
      const line = raw.trim()
      const section = line.match(/^\[([^\]]+)\]$/)
      if (section) {
        if (name && current.hostname) hosts.push({ name, role: 'primary', ...current } as JumpHost)
        name = section[1].trim()
        current = {}
        continue
      }
      const kv = line.match(/^([\w-]+)\s*=\s*"([^"]*)"$/)
      if (kv && name) current[kv[1]] = kv[2]
    }
    if (name && current.hostname) hosts.push({ name, role: 'primary', ...current } as JumpHost)
    return hosts
  },

  invoke_runnable: async (host, runnable, args) => {
    const id = `inv-${++invocationCounter}`
    console.log('[mock] invoke_runnable', { host, runnable, args, id })

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

  get_in_flight: async () => MOCK_IN_FLIGHT,

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
