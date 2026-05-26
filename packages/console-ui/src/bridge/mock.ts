import type { BridgeApi, Host, JumpHost, Runnable, HistoryRecord, Schedule, InFlightRecord, TestResult } from './index'

const MOCK_RUNNABLES: Runnable[] = [
  {
    name: 'backup',
    group: 'ops-tools',
    host: 'local',
    description: 'Back up a directory to remote storage',
    autonomy: 'confirm',
    runAs: 'jason',
    args: [
      { name: 'source', type: 'path', required: true, description: 'Directory to back up' },
      { name: 'dest', type: 'str', required: false, default: '$BACKUP_DEST', description: 'Destination path' },
      { name: 'dry-run', type: 'flag', required: false, default: false },
    ],
  },
  {
    name: 'get-alerts',
    group: 'ops-tools',
    host: 'local',
    description: 'Pull active alerts from all configured sources',
    args: [
      { name: 'endpoint', type: 'str', required: false, default: '$ALERT_ENDPOINT', description: 'Alert source URL' },
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
      { name: 'user', type: 'str', required: false, default: '$DEPLOY_USER', description: 'Remote username' },
    ],
  },
  {
    name: 'log-rotate',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Rotate and compress log files on the host',
    autonomy: 'autonomous',
    runAs: 'svc-runner',
    args: [
      { name: 'log-dir', type: 'path', required: false, default: '$LOG_DIR', description: 'Directory containing log files' },
      { name: 'keep', type: 'int', required: false, default: 7, description: 'Days of logs to keep' },
    ],
  },
  {
    name: 'cache-purge',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Purge stale cache entries',
    args: [
      { name: 'redis-url', type: 'str', required: false, default: '$REDIS_URL', description: 'Redis connection string' },
    ],
  },
  {
    // Same runnable name as ops-tools/backup — demonstrates the collision scenario
    name: 'backup',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Back up database snapshots to object storage',
    args: [
      { name: 'db', type: 'str', required: true, description: 'Database name' },
      { name: 'bucket', type: 'str', required: false, default: '$S3_BUCKET', description: 'S3 bucket name' },
      { name: 'dry-run', type: 'flag', required: false, default: false },
    ],
  },
  {
    // Secondary-only runnable — only visible when Secondary or All is selected
    name: 'failover-drain',
    group: 'platform-core',
    host: 'prod-2',
    description: 'Drain connections before failover to secondary datacenter',
    args: [
      { name: 'timeout', type: 'int', required: false, default: 30, description: 'Drain timeout in seconds' },
      { name: 'notify-url', type: 'str', required: false, default: '$SLACK_WEBHOOK', description: 'Webhook to notify on drain' },
    ],
  },
  {
    name: 'flush-dns',
    group: 'ops-tools',
    host: 'local',
    description: 'Flush the local DNS cache',
    args: [],
  },
  {
    name: 'set-env',
    group: 'ops-tools',
    host: 'local',
    description: 'Switch the active environment context',
    autonomy: 'confirm',
    runAs: 'jason',
    args: [
      { name: 'env', type: 'choice', required: true, options: ['dev', 'staging', 'prod'], description: 'Target environment' },
      { name: 'confirm', type: 'flag', required: false, default: false, description: 'Skip confirmation prompt' },
    ],
  },
  {
    name: 'tune',
    group: 'ops-tools',
    host: 'local',
    description: 'Tune service resource limits — one example of every runspec arg type',
    autonomy: 'confirm',
    runAs: 'root',
    args: [
      { name: 'target',     type: 'choice', required: true,  options: ['web', 'worker', 'scheduler'], description: 'Service to tune' },
      { name: 'config',     type: 'path',   required: false, default: '/etc/app/config.yaml', description: 'Config file path' },
      { name: 'cpu-limit',  type: 'float',  required: false, default: 0.8,  description: 'CPU limit as a fraction (0.0–1.0)' },
      { name: 'max-memory', type: 'int',    required: false, default: 512,  description: 'Memory limit in MB' },
      { name: 'label',      type: 'str',    required: false, default: '$DEPLOY_TAG', description: 'Deployment label tag' },
      { name: 'verbose',    type: 'flag',   required: false, default: false },
    ],
  },
  {
    name: 'deploy',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Deploy the application to production',
    autonomy: 'confirm',
    runAs: 'svc-deploy',
    args: [],
    commands: {
      'blue-green': {
        name: 'blue-green',
        group: 'platform-core',
        host: 'prod-1',
        description: 'Blue-green deployment with full traffic cutover',
        args: [
          { name: 'version', type: 'str', required: true, description: 'Image tag to deploy' },
          { name: 'weight', type: 'int', required: false, default: 100, description: 'Traffic weight % to shift (0–100)' },
        ],
        commands: {
          'canary': {
            name: 'canary',
            group: 'platform-core',
            host: 'prod-1',
            description: 'Canary release — shift a small % of traffic first',
            args: [
              { name: 'version', type: 'str', required: true, description: 'Image tag to deploy' },
              { name: 'initial-weight', type: 'int', required: false, default: 10, description: 'Initial canary traffic %' },
            ],
          },
        },
      },
      'rolling': {
        name: 'rolling',
        group: 'platform-core',
        host: 'prod-1',
        description: 'Rolling update — replace instances one batch at a time',
        args: [
          { name: 'version', type: 'str', required: true, description: 'Image tag to deploy' },
          { name: 'batch-size', type: 'int', required: false, default: 2, description: 'Instances to update at once' },
        ],
      },
    },
  },
  {
    name: 'migrate',
    group: 'platform-core',
    host: 'prod-2',
    description: 'Run database migrations',
    args: [],
    commands: {
      'up': {
        name: 'up',
        group: 'platform-core',
        host: 'prod-2',
        description: 'Apply pending migrations',
        args: [
          { name: 'steps', type: 'int', required: false, default: 0, description: 'Steps to apply (0 = all pending)' },
          { name: 'dry-run', type: 'flag', required: false, default: false },
        ],
      },
      'down': {
        name: 'down',
        group: 'platform-core',
        host: 'prod-2',
        description: 'Roll back migrations',
        args: [
          { name: 'steps', type: 'int', required: false, default: 1, description: 'Steps to roll back' },
        ],
      },
    },
  },
]

const MOCK_JUMP_HOSTS: JumpHost[] = [
  { name: 'prod-1', hostname: 'hostname1.company.com', user: 'jason', group: 'Production' },
  { name: 'prod-2', hostname: 'hostname2.company.com', user: 'jason', group: 'Production' },
  { name: 'logs-1', hostname: 'hostname3.company.com', user: 'jason', group: 'Production' },
]

const MOCK_HOSTS: Host[] = [
  { name: 'local',  connected: true,  runnableCount: 3, groups: ['ops-tools'] },
  { name: 'prod-1', connected: true,  runnableCount: 3, groups: ['platform-core'], role: 'primary',   group: 'Production' },
  { name: 'prod-2', connected: true,  runnableCount: 1, groups: ['platform-core'], role: 'secondary', group: 'Production' },
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
  }),

  save_config: async (data) => { console.log('[mock] save_config', data) },

  get_jump_hosts: async () => MOCK_JUMP_HOSTS,

  save_jump_hosts: async (hosts) => { console.log('[mock] save_jump_hosts', hosts) },

  test_host: async (name): Promise<TestResult> => {
    await new Promise(r => setTimeout(r, 1200))
    if (name === 'prod-2') {
      return { connected: true, runspec_ok: false, runnable_count: 0,
               stdout: '', stderr: 'bash: /usr/local/bin/runspec: No such file or directory', exit_code: 127 }
    }
    return { connected: true, runspec_ok: true, runnable_count: 3,
             stdout: '[]', stderr: '', exit_code: 0 }
  },

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

  invoke_runnable: async (host, runnable, args, commandPath = []) => {
    const id = `inv-${++invocationCounter}`
    console.log('[mock] invoke_runnable', { host, runnable, args, commandPath, id })
    const cmd = [runnable, ...commandPath].join(' ')

    const lines = [
      `$ runspec ${cmd} ${Object.entries(args).map(([k, v]) => `--${k}=${v}`).join(' ')}`,
      `[${cmd}] starting on ${host}...`,
      `[${cmd}] processing step 1 of 3`,
      `[${cmd}] processing step 2 of 3`,
      `[${cmd}] processing step 3 of 3`,
      `[${cmd}] done.`,
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

  get_today: async (_host, _group) => {
    const today = new Date().toISOString().slice(0, 10)
    return {
      date: today,
      generatedAt: new Date(Date.now() - 3 * 60 * 1000).toISOString(), // 3 min ago
      totalRuns: 14,
      successCount: 12,
      failureCount: 2,
      byRunnable: [
        { runnable: 'backup',      host: 'local',  count: 4, lastExitCode: 0, lastRun: new Date(Date.now() - 18 * 60 * 1000).toISOString() },
        { runnable: 'log-rotate',  host: 'local',  count: 3, lastExitCode: 0, lastRun: new Date(Date.now() - 42 * 60 * 1000).toISOString() },
        { runnable: 'deploy',      host: 'prod-1', count: 2, lastExitCode: 1, lastRun: new Date(Date.now() - 67 * 60 * 1000).toISOString() },
        { runnable: 'migrate',     host: 'prod-2', count: 2, lastExitCode: 0, lastRun: new Date(Date.now() - 90 * 60 * 1000).toISOString() },
        { runnable: 'set-env',     host: 'local',  count: 2, lastExitCode: 0, lastRun: new Date(Date.now() - 4 * 60 * 60 * 1000).toISOString() },
        { runnable: 'health-check',host: 'prod-1', count: 1, lastExitCode: 1, lastRun: new Date(Date.now() - 5 * 60 * 60 * 1000).toISOString() },
      ],
      upcomingToday: [
        { scheduleId: 'sched-1', runnable: 'backup',       host: 'local',  nextRun: new Date(Date.now() + 42 * 60 * 1000).toISOString() },
        { scheduleId: 'sched-2', runnable: 'log-rotate',   host: 'local',  nextRun: new Date(Date.now() + 78 * 60 * 1000).toISOString() },
        { scheduleId: 'sched-3', runnable: 'health-check', host: 'prod-1', nextRun: new Date(Date.now() + 15 * 60 * 1000).toISOString() },
      ],
    }
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
