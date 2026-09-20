import type { BridgeApi, ChatSessionInfo, MarketplaceRepo, MarketplaceEntry, MarketplaceTarget, MarketplaceInstalledTarget, MarketplaceOpRecord, MarketplaceOpPayload, MarketplaceOpResult, Host, JumpHost, Runnable, HistoryRecord, Schedule, InFlightRecord, TestResult, AnalyticsData, AnalyticsBucket, TriggerCard } from './index'

// 80 days ago — triggers the yellow warning state in dev mode
let MOCK_KEY_CREATED_AT: string = new Date(Date.now() - 80 * 24 * 60 * 60 * 1000).toISOString()

const MOCK_RUNNABLES: Runnable[] = [
  {
    name: 'backup',
    group: 'ops-tools',
    host: 'local',
    description: 'Back up a directory to remote storage',
    autonomy: 'confirm',
    runAs: 'jason',
    args: [
      { name: 'source', type: 'path', required: true, description: 'Directory to back up', short: 's', ui: 'directory' },
      { name: 'dest', type: 'str', required: false, default: '$BACKUP_DEST', description: 'Destination path', env: ['BACKUP_DEST', 'DEST'] },
      { name: 'label', type: 'str', required: false, description: 'Snapshot label', pattern: '^[a-z0-9-]+$', min_length: 3, max_length: 40, hint: 'lowercase-with-dashes' },
      { name: 'keep', type: 'int', required: false, default: 7, range: [1, 365], description: 'Retention days' },
      { name: 'tags', type: 'str', required: false, multiple: true, delimiter: ',', description: 'Comma-separated tags', meta: { team: 'ops' } },
      { name: 'dry-run', type: 'flag', required: false, default: false },
    ],
    rawSpec: {
      description: 'Back up a directory to remote storage',
      autonomy: 'confirm',
      run_as: 'jason',
      become_method: 'sudo',
      output: 'text',
      serve: true,
    },
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
    rawSpec: {
      description: 'Pull active alerts from all configured sources',
    },
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
    rawSpec: {
      description: 'Copy SSH public key to a remote host',
      runspec_env: '.runspec_env',
    },
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
    rawSpec: {
      description: 'Rotate and compress log files on the host',
      autonomy: 'autonomous',
      'autonomy-reason': 'Safe to run unattended — no destructive side-effects, logs compressed in-place',
      run_as: 'svc-runner',
      output: 'text',
      serve: true,
    },
  },
  {
    name: 'cache-purge',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Purge stale cache entries',
    args: [
      { name: 'redis-url', type: 'str', required: false, default: '$REDIS_URL', description: 'Redis connection string' },
    ],
    rawSpec: {
      description: 'Purge stale cache entries',
      autonomy: 'autonomous',
      'autonomy-reason': 'Idempotent cache eviction; worst case is a cache miss on next request',
      serve: true,
    },
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
    rawSpec: {
      description: 'Back up database snapshots to object storage',
      autonomy: 'confirm',
      run_as: 'svc-backup',
      output: 'json',
      serve: true,
    },
  },
  {
    // Same runnable on prod-1 AND prod-2 (identical group + name) — a fleet
    // search collapses these two into one "2 hosts" row with a host picker.
    name: 'health-check',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Probe service health endpoints and report status',
    args: [
      { name: 'endpoint', type: 'str', required: false, default: '/healthz', description: 'Health path to probe' },
      { name: 'timeout', type: 'int', required: false, default: 5, description: 'Per-probe timeout (seconds)' },
    ],
    rawSpec: { description: 'Probe service health endpoints and report status', autonomy: 'autonomous', output: 'json' },
  },
  {
    name: 'health-check',
    group: 'platform-core',
    host: 'prod-2',
    description: 'Probe service health endpoints and report status',
    args: [
      { name: 'endpoint', type: 'str', required: false, default: '/healthz', description: 'Health path to probe' },
      { name: 'timeout', type: 'int', required: false, default: 5, description: 'Per-probe timeout (seconds)' },
    ],
    rawSpec: { description: 'Probe service health endpoints and report status', autonomy: 'autonomous', output: 'json' },
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
    rawSpec: {
      description: 'Drain connections before failover to secondary datacenter',
      autonomy: 'supervised',
      'autonomy-reason': 'Irreversible during a failover window — operator must be present',
      run_as: 'svc-ops',
      become_method: 'sudo',
      output: 'stream',
      serve: false,
    },
  },
  {
    name: 'flush-dns',
    group: 'ops-tools',
    host: 'local',
    description: 'Flush the local DNS cache',
    args: [],
    rawSpec: {
      description: 'Flush the local DNS cache',
    },
  },
  {
    name: 'check-port',
    group: 'ops-tools',
    host: 'local',
    description: 'Test whether a TCP port is open on a host',
    autonomy: 'autonomous',
    args: [
      { name: 'host', type: 'str', required: true, description: 'Hostname or IP address' },
      { name: 'port', type: 'int', required: true, description: 'TCP port number to check' },
      { name: 'timeout', type: 'float', required: false, default: 3.0, description: 'Connection timeout in seconds' },
    ],
    rawSpec: {
      description: 'Test whether a TCP port is open on a host',
      autonomy: 'autonomous',
    },
  },
  {
    name: 'ping-host',
    group: 'ops-tools',
    host: 'local',
    description: 'Check network connectivity to a host',
    autonomy: 'autonomous',
    args: [
      { name: 'host', type: 'str', required: true, description: 'Hostname or IP address to ping' },
      { name: 'count', type: 'int', required: false, default: 4, description: 'Number of ping requests to send' },
    ],
    rawSpec: {
      description: 'Check network connectivity to a host',
      autonomy: 'autonomous',
    },
  },
  {
    name: 'generate-ssh-key',
    group: 'ops-tools',
    host: 'local',
    description: 'Generate or rotate the runspec-console SSH key pair',
    autonomy: 'confirm',
    args: [
      { name: 'key_path', type: 'str', required: false, default: '~/.ssh/runspec_ed25519', description: 'Path for the key pair (without extension)' },
    ],
    rawSpec: {
      description: 'Generate or rotate the runspec-console SSH key pair',
      autonomy: 'confirm',
    },
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
    rawSpec: {
      description: 'Switch the active environment context',
      autonomy: 'confirm',
      run_as: 'jason',
      runspec_env: '.runspec_env',
    },
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
    rawSpec: {
      description: 'Tune service resource limits — one example of every runspec arg type',
      autonomy: 'confirm',
      run_as: 'root',
      become_method: 'su',
      become_flags: '-m',
      output: 'json',
      runspec_env: '.runspec_env',
    },
  },
  {
    name: 'deploy',
    group: 'platform-core',
    host: 'prod-1',
    description: 'Deploy the application to production',
    autonomy: 'confirm',
    runAs: 'svc-deploy',
    requireCommand: true,
    args: [],
    rawSpec: {
      description: 'Deploy the application to production',
      autonomy: 'confirm',
      'autonomy-reason': 'Production deployments require explicit operator sign-off',
      run_as: 'svc-deploy',
      become_method: 'sudo',
      output: 'stream',
      serve: false,
      examples: [
        { description: 'Blue-green deploy to v2.4.1', args: { version: '2.4.1' } },
        { description: 'Canary at 10% traffic', args: { version: '2.4.1', 'initial-weight': 10 } },
      ],
    },
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
    rawSpec: {
      description: 'Run database migrations',
      autonomy: 'confirm',
      'autonomy-reason': 'Schema changes are irreversible without a separate rollback run',
      run_as: 'svc-migrations',
      output: 'stream',
      serve: false,
      examples: [
        { description: 'Apply all pending migrations', args: {} },
        { description: 'Dry-run next 3 steps', args: { steps: 3, 'dry-run': true } },
      ],
    },
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
  {
    name: 'web01', hostname: 'web01.company.com', user: 'ops', group: 'Production',
    room: 'ops', room_port: 8765, room_via: 'exec',
    room_queue: 'it-support', room_catalog: ['reset-password', 'request-vm'],
  },
]

const MOCK_HOSTS: Host[] = [
  { name: 'local',  connected: true,  runnableCount: 3, groups: ['ops-tools'] },
  { name: 'prod-1', connected: true,  runnableCount: 3, groups: ['platform-core'], role: 'primary',   group: 'Production', profiles: ['app-support', 'devops'] },
  { name: 'prod-2', connected: true,  runnableCount: 1, groups: ['platform-core'], role: 'secondary', group: 'Production', profiles: ['devops'] },
]

// Machine-local active host profile in dev mode ("" = All hosts). get_hosts
// filters MOCK_HOSTS by it so the sidebar profile switcher visibly scopes.
let MOCK_ACTIVE_PROFILE = ''
let MOCK_ACTIVE_ROOM = 'web01'

const MOCK_HISTORY: HistoryRecord[] = [
  {
    id: '1', runnable: 'backup', group: 'ops-tools', host: 'local', operator: 'Jason Finestone', runAs: 'DESKTOP\\jason',
    exitCode: 0, durationMs: 3201, ts: new Date(Date.now() - 3600000).toISOString(),
    initiatedBy: 'llm',
    args: { source: 'C:/Users/jason/documents', dest: 'Z:/backups/documents', 'dry-run': 'False' },
    argSources: { source: 'cli', dest: 'cli', 'dry-run': 'spec_default' },
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
    trigger: 'schedule',
    args: { keep: '7' },
    argSources: { keep: 'env' },
    logLines: [
      { ts: new Date(Date.now() - 7205000).toISOString(), level: 'INFO', message: 'log-rotate starting, keep=7 days' },
      { ts: new Date(Date.now() - 7204000).toISOString(), level: 'INFO', message: 'rotating /var/log/app.log' },
      { ts: new Date(Date.now() - 7203000).toISOString(), level: 'INFO', message: 'deleted 3 files older than 7 days' },
    ],
  },
  {
    id: '3', runnable: 'get-alerts', group: 'ops-tools', host: 'local', operator: 'Jason Finestone', runAs: 'DESKTOP\\jason',
    exitCode: 1, durationMs: 450, ts: new Date(Date.now() - 10800000).toISOString(),
    args: { since: 'yesterday', service: 'api-gateway' },
    argSources: { since: 'cli', service: 'runspec_env' },
    logLines: [
      { ts: new Date(Date.now() - 10805000).toISOString(), level: 'INFO',  message: 'get-alerts starting' },
      { ts: new Date(Date.now() - 10804000).toISOString(), level: 'INFO',  message: 'connecting to Datadog API' },
      { ts: new Date(Date.now() - 10803000).toISOString(), level: 'ERROR', message: 'Datadog API request failed: 401 Unauthorized' },
    ],
  },
  {
    id: '4', runnable: 'cache-purge', group: 'platform-core', host: 'prod-1', operator: 'Scheduled Task', runAs: 'svc-runner',
    exitCode: 0, durationMs: 201, ts: new Date(Date.now() - 86400000).toISOString(),
    trigger: 'schedule', initiatedBy: 'llm',
    args: {},
    argSources: {},
    logLines: [
      { ts: new Date(Date.now() - 86405000).toISOString(), level: 'INFO', message: 'cache-purge starting' },
      { ts: new Date(Date.now() - 86404000).toISOString(), level: 'INFO', message: 'purged 412 stale entries' },
    ],
  },
  {
    id: '5', runnable: 'backup', group: 'platform-core', host: 'prod-1', operator: 'Jason Finestone', runAs: 'svc-runner',
    exitCode: 0, durationMs: 2980, ts: new Date(Date.now() - 90000000).toISOString(),
    args: { db: 'postgres-main', 'dry-run': 'False' },
    argSources: { db: 'cli', 'dry-run': 'spec_default' },
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
    args: { keep: '14' },
    argSources: { keep: 'cli' },
    logLines: [
      { ts: new Date(Date.now() - 172805000).toISOString(), level: 'INFO', message: 'log-rotate starting, keep=14 days' },
      { ts: new Date(Date.now() - 172804000).toISOString(), level: 'INFO', message: 'rotating /var/log/app.log' },
      { ts: new Date(Date.now() - 172803000).toISOString(), level: 'INFO', message: 'no files older than 14 days found' },
    ],
  },
]

// ── Analytics mock ──────────────────────────────────────────────────────────
// Generate ~30 days of synthetic runs across the mock fleet so the Analytics
// tab — filters, charts and drill-down — behaves realistically in the browser
// (no pywebview). A small in-file reducer mirrors the Python aggregation shape.

interface SyntheticRun {
  ts: string; host: string; group: string; runnable: string
  operator: string; initiatedBy: 'user' | 'llm'; autonomy: string
  ok: boolean; durationMs: number
  events: Record<string, number>; exception?: string
}

function _seeded(seed: number): () => number {
  // Deterministic PRNG so dev reloads show a stable dataset.
  let s = seed
  return () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff }
}

const _ANALYTICS_RUNS: SyntheticRun[] = (() => {
  const rng = _seeded(42)
  const fleet = [
    { host: 'local',  group: 'ops-tools',      runnables: ['backup', 'get-alerts', 'log-rotate'] },
    { host: 'prod-1', group: 'platform-core',  runnables: ['backup', 'cache-purge', 'deploy', 'health-check'] },
    { host: 'prod-2', group: 'platform-core',  runnables: ['migrate', 'health-check'] },
  ]
  const operators = ['Jason Finestone', 'Scheduled Task', 'Priya Nair']
  const autonomies = ['confirm', 'autonomous', 'manual']
  const runs: SyntheticRun[] = []
  for (let day = 29; day >= 0; day--) {
    const base = Date.now() - day * 86400000
    const runsToday = 6 + Math.floor(rng() * 22)   // 6–27 runs/day
    for (let i = 0; i < runsToday; i++) {
      const f = fleet[Math.floor(rng() * fleet.length)]
      const runnable = f.runnables[Math.floor(rng() * f.runnables.length)]
      const operator = operators[Math.floor(rng() * operators.length)]
      const initiatedBy = rng() < 0.25 ? 'llm' : 'user'
      const fail = rng() < (runnable === 'get-alerts' ? 0.35 : runnable === 'deploy' ? 0.18 : 0.05)
      const durationMs = Math.round(150 + rng() * (runnable === 'backup' ? 6000 : 2500))
      const warn = Math.floor(rng() * 4)
      const errs = fail ? 1 + Math.floor(rng() * 3) : 0
      runs.push({
        ts: new Date(base - Math.floor(rng() * 86400000)).toISOString(),
        host: f.host, group: f.group, runnable, operator, initiatedBy,
        autonomy: autonomies[Math.floor(rng() * autonomies.length)],
        ok: !fail, durationMs,
        events: { DEBUG: Math.floor(rng() * 6), INFO: 4 + Math.floor(rng() * 30), WARNING: warn, ERROR: errs, CRITICAL: 0 },
        exception: fail
          ? (runnable === 'get-alerts' ? 'HTTPError: Datadog API request failed: 401 Unauthorized'
            : runnable === 'deploy' ? 'TimeoutError: health check did not pass within 60s'
            : 'RuntimeError: unexpected non-zero exit')
          : undefined,
      })
    }
  }
  return runs
})()

function _aggregateMock(runs: SyntheticRun[], sinceDays: number): AnalyticsData {
  const now = new Date()
  const sinceDate = new Date(now.getTime() - (sinceDays - 1) * 86400000)
  const since = sinceDate.toISOString().slice(0, 10)
  const until = now.toISOString().slice(0, 10)
  const inWindow = runs.filter(r => r.ts.slice(0, 10) >= since)

  const pct = (vals: number[], p: number): number => {
    if (vals.length === 0) return 0
    const s = [...vals].sort((a, b) => a - b)
    if (s.length === 1) return s[0]
    const rank = (p / 100) * (s.length - 1)
    const lo = Math.floor(rank), hi = Math.min(lo + 1, s.length - 1)
    return Math.round(s[lo] + (s[hi] - s[lo]) * (rank - lo))
  }

  const dailyMap = new Map<string, { total: number; success: number; failure: number; durs: number[] }>()
  const bucketMap = new Map<string, AnalyticsBucket>()
  const excMap = new Map<string, { exception: string; runnable: string; host: string; count: number; lastTs: string }>()
  const hosts = new Set<string>(), groups = new Set<string>(), runnables = new Set<string>(), operators = new Set<string>()
  const allDurs: number[] = []
  let total = 0, success = 0, failure = 0

  for (const r of inWindow) {
    const day = r.ts.slice(0, 10)
    total++; r.ok ? success++ : failure++
    allDurs.push(r.durationMs)
    hosts.add(r.host); groups.add(r.group); runnables.add(r.runnable); operators.add(r.operator)

    const d = dailyMap.get(day) ?? { total: 0, success: 0, failure: 0, durs: [] }
    d.total++; r.ok ? d.success++ : d.failure++; d.durs.push(r.durationMs)
    dailyMap.set(day, d)

    const bkey = [day, r.host, r.group, r.runnable, r.operator, r.initiatedBy, r.autonomy].join('|')
    const b = bucketMap.get(bkey) ?? {
      date: day, host: r.host, group: r.group, runnable: r.runnable, operator: r.operator,
      initiatedBy: r.initiatedBy, autonomy: r.autonomy, runbook: '',
      total: 0, success: 0, failure: 0, durationMsSum: 0,
      events: { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 } as Record<string, number>,
    }
    b.total++; r.ok ? b.success++ : b.failure++; b.durationMsSum += r.durationMs
    for (const k of Object.keys(b.events)) b.events[k] += r.events[k] ?? 0
    bucketMap.set(bkey, b)

    if (r.exception) {
      const ekey = [r.exception, r.runnable, r.host].join('|')
      const ex = excMap.get(ekey) ?? { exception: r.exception, runnable: r.runnable, host: r.host, count: 0, lastTs: '' }
      ex.count++; if (r.ts > ex.lastTs) ex.lastTs = r.ts
      excMap.set(ekey, ex)
    }
  }

  const daily: AnalyticsData['daily'] = []
  const start = new Date(since + 'T00:00:00Z')
  const end = new Date(until + 'T00:00:00Z')
  for (let t = start.getTime(); t <= end.getTime(); t += 86400000) {
    const key = new Date(t).toISOString().slice(0, 10)
    const d = dailyMap.get(key)
    daily.push(d
      ? { date: key, total: d.total, success: d.success, failure: d.failure, p50DurationMs: pct(d.durs, 50), p95DurationMs: pct(d.durs, 95) }
      : { date: key, total: 0, success: 0, failure: 0, p50DurationMs: 0, p95DurationMs: 0 })
  }

  return {
    since, until, totalRuns: total, successCount: success, failureCount: failure,
    durationMs: {
      p50: pct(allDurs, 50), p95: pct(allDurs, 95),
      max: allDurs.length ? Math.max(...allDurs) : 0,
      mean: allDurs.length ? Math.round(allDurs.reduce((a, b) => a + b, 0) / allDurs.length) : 0,
    },
    daily,
    buckets: [...bucketMap.values()],
    exceptions: [...excMap.values()].sort((a, b) => b.count - a.count).slice(0, 50),
    dimensions: {
      hosts: [...hosts].sort(), groups: [...groups].sort(), runnables: [...runnables].sort(),
      operators: [...operators].sort(), initiatedBy: ['user', 'llm'], runbooks: [],
    },
    partial: false, errors: [],
  }
}

let MOCK_SCHEDULES: Schedule[] = [
  { id: 'rs-backup-daily', action: 'runnable', runnable: 'backup', host: 'local', when: { cron: '0 2 * * *' }, args: { db: 'postgres-main' }, enabled: true, nextRun: new Date(Date.now() + 9 * 60 * 60 * 1000).toISOString() },
  { id: 'rs-log-rotate-weekly', action: 'runnable', runnable: 'log-rotate', host: 'prod-1', when: { cron: '0 3 * * 0' }, args: { keep: 7 }, enabled: false },
  { id: 'rs-nightly-report', action: 'event', event: 'nightly_report', when: { cron: '0 2 * * *', timezone: 'Europe/London' }, fields: { scope: 'prod', report: 'billing' }, enabled: true, nextRun: new Date(Date.now() + 11 * 60 * 60 * 1000).toISOString() },
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
    trigger: 'schedule',
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

// request_id → resolver, for the simulated autonomy-confirm round-trip.
const _pendingConfirms = new Map<string, (approved: boolean) => void>()

// Console sessions (see src/sessions.ts). Mirrors the backend registry: every
// conversation is a session; interactive ones are the send targets, automation
// ones (trigger / schedule fires) are read-only. In-memory, dev-only.
const _sessions = new Map<string, ChatSessionInfo>()
let _sessionCounter = 0
let _currentSession: string | null = null
const _dispatch = (event: string, detail: unknown) =>
  window.dispatchEvent(new CustomEvent(event, { detail }))
const _emitSession = (info: ChatSessionInfo) => _dispatch('runspec:session', { ...info })
function _newMockSession(id?: string, label = ''): ChatSessionInfo {
  const sid = id ?? `sess-${++_sessionCounter}`
  const info: ChatSessionInfo = {
    id: sid, kind: 'chat', label, interactive: true, created_at: Date.now() / 1000,
    state: 'idle', turn: '', turns: 0,
  }
  _sessions.set(sid, info)
  _currentSession = sid
  _emitSession(info)
  return info
}
function _patchSession(id: string, patch: Partial<ChatSessionInfo>): void {
  const cur = _sessions.get(id)
  if (!cur) return
  const next = { ...cur, ...patch }
  _sessions.set(id, next)
  _emitSession(next)
}
function _dropSession(id: string): void {
  if (!_sessions.delete(id)) return
  _dispatch('runspec:session', { id, state: 'closed' })
  if (_currentSession === id) {
    const rest = [..._sessions.values()].filter(s => s.interactive)
    _currentSession = rest.length ? rest[rest.length - 1].id : null
  }
}

// Chat turns currently streaming, keyed by chat id, so cancel_chat can stop one
// (including while its Approve/Deny prompt is up — the case the real bridge
// handles). Several run concurrently, one per session.
const _activeChats = new Map<string, { id: string; session: string; cancelled: boolean }>()


// ── Marketplace fixtures ──
const MOCK_MARKET_REPOS: MarketplaceRepo[] = [
  { id: 'nexus', name: 'Nexus', url: 'https://nexus.example.com/repository/pypi-all/simple/', enabled: true, scan: true, packages: ['acme-ops'], patterns: [], credential: 'svc-prod', pip_arg: 'auto', trusted_host: '', tags: ['internal'], source: 'config', pip_role: 'index', is_pypi: false },
  { id: 'pypi', name: 'PyPI', url: 'https://pypi.org/simple/', enabled: true, scan: true, packages: [], patterns: [], credential: '', pip_arg: 'auto', trusted_host: '', tags: ['pip-config'], source: 'pip', pip_role: 'extra', is_pypi: true },
]

const MOCK_MARKET_ENTRIES: MarketplaceEntry[] = [
  { name: 'runspec-linux', display_name: 'runspec-linux', version: '0.13.2', versions: ['0.13.2', '0.13.1', '0.13.0'], summary: 'Linux system admin runnables for runspec', description: '# runspec-linux\n\n31 Linux system-admin runnables — services, logs, network, packages, venvs, cron, filesystem and the cross-platform web/TLS probes.\n\n- `system-info`, `disk-usage`, `check-memory`\n- `restart-service`, `journalctl`\n- `create-venv`, `install-into-venv`', description_truncated: false, description_content_type: 'text/markdown', keywords: ['runspec', 'linux', 'ops'], links: { documentation: 'https://jasonfinestone.github.io/runspec/', source: 'https://github.com/jasonfinestone/runspec', changelog: 'https://github.com/jasonfinestone/runspec/blob/main/packages/python/runspec-linux/CHANGELOG.md' }, requires_python: '>=3.10', requires_dist: ['runspec>=0.50.1', 'runspec-linux-core>=0.6.2', 'runspec-webops-core>=0.3.0', 'runspec-fs-core>=0.3.0'], license: 'MIT', author: '', upload_time: '2026-09-17T23:06:09Z', has_runspec_toml: false, metadata_source: 'pypi-json', repo: 'pypi', repo_name: 'PyPI', repo_url: 'https://pypi.org/simple/', repos: ['pypi'] },
  { name: 'runspec-windows', display_name: 'runspec-windows', version: '0.9.0', versions: ['0.9.0', '0.8.0'], summary: 'Windows system admin + cross-platform web/TLS/API runnables for runspec', description: 'Windows system admin runnables (services, event log, registry, scheduled tasks) plus the cross-platform web/TLS probes.', description_truncated: false, description_content_type: 'text/markdown', keywords: ['runspec', 'windows'], links: { documentation: 'https://jasonfinestone.github.io/runspec/' }, requires_python: '>=3.10', requires_dist: ['runspec>=0.50.1', 'runspec-webops-core>=0.3.0'], license: 'MIT', author: '', upload_time: '2026-09-10T10:00:00Z', has_runspec_toml: false, metadata_source: 'pypi-json', repo: 'pypi', repo_name: 'PyPI', repo_url: 'https://pypi.org/simple/', repos: ['pypi'] },
  { name: 'runspec-jira', display_name: 'runspec-jira', version: '0.3.0', versions: ['0.3.0'], summary: 'Full-featured Jira runnable for runspec — issues, search, transitions, comments, service desk, attachments', description: '', description_truncated: false, description_content_type: '', keywords: ['runspec', 'jira', 'atlassian'], links: {}, requires_python: '>=3.10', requires_dist: ['runspec>=0.50.1', 'runspec-jira-core>=0.3.0'], license: '', author: '', upload_time: '2026-08-01T10:00:00Z', has_runspec_toml: false, metadata_source: 'pypi-json', repo: 'pypi', repo_name: 'PyPI', repo_url: 'https://pypi.org/simple/', repos: ['pypi'] },
  { name: 'acme-ops', display_name: 'acme-ops', version: '2.4.0', versions: ['2.4.0', '2.3.1'], summary: 'Acme internal ops runnables (deploy, rotate secrets, drain node)', description: 'Internal. Wraps runspec-linux-core with Acme defaults.', description_truncated: false, description_content_type: 'text/markdown', keywords: ['acme', 'internal'], links: { documentation: 'https://wiki.example.com/acme-ops' }, requires_python: '>=3.11', requires_dist: ['runspec>=0.50', 'runspec-linux-core'], license: 'Proprietary', author: 'Acme Platform', upload_time: '2026-09-18T08:00:00Z', has_runspec_toml: true, metadata_source: 'wheel', repo: 'nexus', repo_name: 'Nexus', repo_url: 'https://nexus.example.com/repository/pypi-all/simple/', repos: ['nexus'] },
]

const MOCK_MARKET_TARGETS: MarketplaceTarget[] = [
  { id: 'local::C:\\venvs\\console', host: 'local', venv: 'console', path: 'C:\\venvs\\console', python: 'C:\\venvs\\console\\Scripts\\python.exe', runspec_path: 'C:\\venvs\\console\\Scripts\\runspec.exe', local: true, connected: true },
  { id: 'local::C:\\venvs\\ops-tools', host: 'local', venv: 'ops-tools', path: 'C:\\venvs\\ops-tools', python: 'C:\\venvs\\ops-tools\\Scripts\\python.exe', runspec_path: 'C:\\venvs\\ops-tools\\Scripts\\runspec.exe', local: true, connected: true },
  { id: 'prod-1::/opt/venvs/ops', host: 'prod-1', venv: 'ops', path: '/opt/venvs/ops', python: '/opt/venvs/ops/bin/python', runspec_path: '/opt/venvs/ops/bin/runspec', local: false, connected: true },
  { id: 'prod-2::/opt/venvs/ops', host: 'prod-2', venv: 'ops', path: '/opt/venvs/ops', python: '/opt/venvs/ops/bin/python', runspec_path: '/opt/venvs/ops/bin/runspec', local: false, connected: false },
]

const MOCK_MARKET_INSTALLED: MarketplaceInstalledTarget[] = [
  { ...MOCK_MARKET_TARGETS[0], ok: true, error: null, owner: '', user: 'jason', python_version: '3.12.4', dists: { runspec: '0.51.0', 'runspec-windows': '0.9.0', 'runspec-jira': '0.2.0' } },
  { ...MOCK_MARKET_TARGETS[1], ok: true, error: null, owner: '', user: 'jason', python_version: '3.12.4', dists: { runspec: '0.50.1' } },
  { ...MOCK_MARKET_TARGETS[2], ok: true, error: null, owner: 'svc-ops', user: 'jason', python_version: '3.11.9', dists: { runspec: '0.50.1', 'runspec-linux': '0.13.1', 'acme-ops': '2.4.0' } },
  { ...MOCK_MARKET_TARGETS[3], ok: false, error: 'host not connected', owner: '', user: '', python_version: '', dists: {} },
]

const MOCK_MARKET_OPS = new Map<string, MarketplaceOpRecord>()

function mockMarketplaceOp(action: 'install' | 'uninstall', payload: MarketplaceOpPayload) {
  const op = Math.random().toString(16).slice(2, 14)
  const rec: MarketplaceOpRecord = { op, action, package: payload.package, status: 'running', results: [] }
  MOCK_MARKET_OPS.set(op, rec)
  console.log('[mock] marketplace_' + action, payload)
  let delay = 200
  for (const t of payload.targets) {
    const target = MOCK_MARKET_TARGETS.find(x => x.id === t.id)!
    setTimeout(() => window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op, phase: 'start', target: t.id, host: target.host, venv: target.venv, action } })), delay)
    const lines = action === 'install'
      ? [`Collecting ${payload.package}`, `  Downloading ${payload.package.replace(/-/g, '_')}-1.0.0-py3-none-any.whl (42 kB)`, `Installing collected packages: ${payload.package}`, `Successfully installed ${payload.package}-1.0.0`]
      : [`Found existing installation: ${payload.package} 1.0.0`, `Uninstalling ${payload.package}-1.0.0:`, `  Successfully uninstalled ${payload.package}-1.0.0`]
    lines.forEach((line, i) => setTimeout(() => window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op, phase: 'line', target: t.id, line } })), delay + 250 * (i + 1)))
    delay += 250 * (lines.length + 1)
    const ok = target.venv !== 'ops-tools'
    setTimeout(() => {
      const res: MarketplaceOpResult = { target: t.id, host: target.host, venv: target.venv, ok, exit_code: ok ? 0 : 1, summary: ok ? lines[lines.length - 1] : 'ERROR: No matching distribution found', skipped: false }
      rec.results.push(res)
      window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op, phase: 'done', target: t.id, ok, exit_code: res.exit_code, summary: res.summary } }))
    }, delay)
    delay += 100
  }
  setTimeout(() => {
    rec.status = 'finished'
    window.dispatchEvent(new CustomEvent('runspec:marketplace_done', { detail: { op, action, package: payload.package, status: 'finished', results: rec.results } }))
  }, delay + 100)
  return Promise.resolve({ op, targets: payload.targets.map(t => t.id) })
}

export const mockApi: BridgeApi = {
  get_hosts: async () => MOCK_ACTIVE_PROFILE
    ? MOCK_HOSTS.filter(h => h.name === 'local' || (h.profiles ?? []).includes(MOCK_ACTIVE_PROFILE))
    : MOCK_HOSTS,

  manage_denylist: async (payload) => {
    console.log('[mock] manage_denylist', payload)
    const targets = payload.targets?.length ? payload.targets : MOCK_HOSTS.map(h => h.name)
    return `runspec deny ${payload.action} — ${targets.length}/${targets.length} venv(s) ok across ${targets.length} host(s):\n` +
      targets.map(t => `✓ ${t}/app: ok`).join('\n')
  },

  denylist_status: async (targets) => {
    const names = targets?.length ? targets : MOCK_HOSTS.map(h => h.name)
    return names.map(host => ({
      host,
      venv: 'app',
      ok: true,
      present: host === 'prod-1',
      mode: 'enforce' as const,
      disabled: false,
      reason: '',
      rules: host === 'prod-1'
        ? [{ runnables: ['deploy'], users: ['*'], hosts: ['*'], reason: 'change freeze' }]
        : [],
    }))
  },

  get_runnables: async (_host) => MOCK_RUNNABLES,
  discovery_state: async () => ({ ready: true, done: 1, total: 1 }),
  list_dir: async (_host, path = '', _group, dirsOnly = false, _runnable, asServiceAccount = false) => {
    const home = asServiceAccount ? '/home/svc_app' : '/var/log/app'
    const cwd = path && path !== '/' ? path.replace(/\/$/, '') : home
    const entries = [
      { name: 'archive', type: 'directory' as const, size: 4096, mtime: 1717000000 },
      { name: 'sessions', type: 'directory' as const, size: 4096, mtime: 1717000100 },
      { name: 'fix.log', type: 'file' as const, size: 204800, mtime: 1717000500 },
      { name: 'exceptions.log', type: 'file' as const, size: 10240, mtime: 1717000400 },
      // Only visible under the service account — demonstrates the identity toggle.
      ...(asServiceAccount ? [{ name: 'svc-only.key', type: 'file' as const, size: 2048, mtime: 1717000600 }] : []),
    ].filter(e => !dirsOnly || e.type === 'directory')
    return { cwd, parent: cwd === '/' ? null : cwd.split('/').slice(0, -1).join('/') || '/', entries, truncated: false }
  },

  get_history: async (_host, _runnable) => MOCK_HISTORY,
  get_history_all: async () => MOCK_HISTORY,

  get_schedules: async () => MOCK_SCHEDULES,

  config_dir: async () => 'C:/Users/jason/AppData/Roaming/runspec-console',
  app_title: async () => 'runspec console',
  brand_theme: async () => ({ accent_color: '#b3001b', brand_name: 'MyCorp Ops', watermark_text: 'MyCorp', watermark_style: 'banner' as const }),
  // Dev mode: `?ai=0` on the URL previews the non-AI console.
  ai_enabled: async () => !(typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('ai') === '0'),
  // Dev mode: `?disclaimer=1` previews the blocking first-use disclaimer gate.
  disclaimer: async () => {
    const on = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('disclaimer') === '1'
    return {
      enabled: on,
      needs_ack: on,
      title: 'AI use notice',
      text: 'This tool uses AI. Verify important output before acting on it, and do not enter sensitive data unless authorised.\n\nSee the [AI use policy](https://example.com/ai-policy) for details.',
      acknowledge_label: 'I acknowledge',
      version: '1',
    }
  },
  acknowledge_disclaimer: async () => ({ ok: true, needs_ack: false }),
  app_version: async () => '0.19.0',
  check_for_update: async () => {
    await new Promise(r => setTimeout(r, 400))
    // Dev mode pretends a newer release exists so the update indicator is visible.
    return { ok: true, current: '0.19.0', latest: '0.20.0', update_available: true }
  },
  docs_url: async () => 'https://runspec.app/',
  open_docs: async () => { console.log('[mock] open_docs') },
  list_providers: async () => [
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'openai', label: 'OpenAI' },
    { value: 'bedrock', label: 'AWS Bedrock' },
    { value: 'langserve', label: 'LangServe gateway' },
  ],
  default_system_prompt: async () =>
    'You are an assistant embedded in runspec-console. You can run runspec runnables on local and remote hosts via tools. Use tools when they help, and confirm before anything destructive.\n\nWhen a required argument is missing or ambiguous — ask the operator one brief clarifying question instead of guessing.',
  refresh_hosts: async () => {
    window.dispatchEvent(new CustomEvent('runspec:discovery_state', { detail: { ready: false, done: 0, total: 3 } }))
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent('runspec:runnables_updated', { detail: {} }))
      window.dispatchEvent(new CustomEvent('runspec:discovery_state', { detail: { ready: true, done: 3, total: 3 } }))
    }, 600)
  },
  get_config: async () => ({
    ssh: { user: 'jason', identityFile: 'C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519', key_created_at: MOCK_KEY_CREATED_AT },
    llm: { apiBaseUrl: '', model: 'claude-opus-4-7' },
    improve: { developer_emails: 'dev@example.com, lead@example.com' },
  }),

  save_config: async (data) => { console.log('[mock] save_config', data) },

  send_test_trace: async () => ({
    ok: true,
    trace_id: '0123456789abcdef0123456789abcdef',
    spans: ['runspec.agent.turn', 'chat runspec-test-model', 'execute_tool run_runnable'],
    endpoint: 'http://127.0.0.1:6006/v1/traces',
    capture_content: true,
    flushed: true,
  }),

  send_test_metric: async () => ({
    ok: true,
    instruments: [
      'runspec.tool.runs',
      'runspec.tool.duration',
      'runspec.agent.turns',
      'runspec.agent.turn.duration',
      'runspec.agent.turn.tokens',
    ],
    endpoint: 'http://127.0.0.1:4318/v1/metrics',
    flushed: true,
  }),

  test_notification: async () => ({ toast_available: false, toast_reason: 'mock', sound_available: false, window_found: false }),
  test_sound: async () => false,
  sound_pack_info: async (path?: string) => ({ dir: path ?? '', exists: false, covered: {}, categories: [] }),
  get_sounds: async () => [
    { id: 'deploy-done', label: 'Deploy done', file: 'C:/sounds/deploy.mp3', exists: true },
    { id: 'pager', label: 'Pager (from another machine)', file: 'D:/sounds/pager.wav', exists: false },
  ],
  save_sounds: async (rows) => ({ ok: true, sounds: rows }),
  import_sound: async (_source_path, label) => ({ ok: true, sound: { id: (label || 'sound').toLowerCase().replace(/[^a-z0-9]+/g, '-'), label: label || 'Sound', file: 'C:/sounds/new.mp3', exists: true } }),
  delete_sound: async () => ({ ok: true, sounds: [] }),
  preview_sound: async () => true,

  get_mailbox: async () => ({ address: 'helpdesk@corp.com', folder: 'Tickets/Open', label: 'Helpdesk', once_per_thread: true }),

  save_mailbox: async (mailbox) => { console.log('[mock] save_mailbox', mailbox) },

  get_watches: async () => ([{ address: '', folder: 'Inbox', label: 'My inbox' }]),

  save_watches: async (watches) => { console.log('[mock] save_watches', watches) },

  get_jump_hosts: async () => MOCK_JUMP_HOSTS,

  save_jump_hosts: async (hosts) => { console.log('[mock] save_jump_hosts', hosts) },

  get_local_venv_config: async () => ({
    globs: ['C:/venvs/*'],
    own_venv: 'C:/venvs/console/Scripts/runspec.exe',
    resolved: [
      { path: 'C:/venvs/console/Scripts/runspec.exe', venv_name: 'console' },
      { path: 'C:/venvs/ops-tools/Scripts/runspec.exe', venv_name: 'ops-tools' },
    ],
  }),
  save_local_venv_config: async (globs) => {
    console.log('[mock] save_local_venv_config', globs)
    return { globs, own_venv: 'C:/venvs/console/Scripts/runspec.exe', resolved: [{ path: 'C:/venvs/console/Scripts/runspec.exe', venv_name: 'console' }] }
  },


  // ── Marketplace ──
  get_marketplace_config: async () => ({
    enabled: true,
    auto_pip: true,
    repos: [...MOCK_MARKET_REPOS],
    declared_repos: MOCK_MARKET_REPOS.filter(r => r.source === 'config'),
    patterns: ['runspec-*'],
    exclude: ['runspec', 'runspec-console', 'runspec-mcp', 'runspec-room', 'runspec-registry'],
    hide: [],
    require_runspec: true,
    include_prerelease: false,
    cache_ttl: 86400,
    timeout: 20,
    max_index_bytes: 134217728,
    max_wheel_bytes: 20971520,
    ca_bundle: null,
    tls_verify: true,
    pip_sources: ['C:\\Users\\jason\\AppData\\Roaming\\pip\\pip.ini'],
    credential_options: [{ id: 'svc-prod', label: 'Nexus', kind: 'userpass' }],
    cache_path: 'C:\\Users\\jason\\AppData\\Roaming\\runspec-console\\marketplace_cache.json',
  }),
  save_marketplace_config: async (section) => {
    console.log('[mock] save_marketplace_config', section)
    return mockApi.get_marketplace_config()
  },
  marketplace_catalog: async (refresh) => {
    console.log('[mock] marketplace_catalog', refresh)
    await new Promise(r => setTimeout(r, refresh ? 800 : 150))
    return {
      enabled: true,
      entries: MOCK_MARKET_ENTRIES,
      repos: MOCK_MARKET_REPOS.map(r => ({
        id: r.id, name: r.name, url: r.url, source: r.source, scan: r.scan, packages: r.packages,
        count: MOCK_MARKET_ENTRIES.filter(e => e.repos.includes(r.id)).length,
        fetched_at: Date.now() / 1000 - 3600, stale: false, error: r.id === 'nexus' ? null : null,
      })),
      errors: [],
    }
  },
  marketplace_targets: async () => MOCK_MARKET_TARGETS,
  marketplace_installed: async (refresh) => {
    console.log('[mock] marketplace_installed', refresh)
    await new Promise(r => setTimeout(r, 300))
    return { targets: MOCK_MARKET_INSTALLED, probed_at: Date.now() / 1000 }
  },
  marketplace_install: async (payload) => mockMarketplaceOp('install', payload),
  marketplace_uninstall: async (payload) => mockMarketplaceOp('uninstall', payload),
  marketplace_op: async (op) => MOCK_MARKET_OPS.get(op) ?? null,
  marketplace_cancel: async (op) => { console.log('[mock] marketplace_cancel', op); return true },
  marketplace_clear_cache: async () => { console.log('[mock] marketplace_clear_cache') },

  get_tool_visibility: async () => ({ hidden_from_agent: ['get-alerts'], hidden_from_all: ['flush-dns'] }),
  save_tool_visibility: async (payload) => {
    console.log('[mock] save_tool_visibility', payload)
    return { hidden_from_agent: payload.hidden_from_agent ?? [], hidden_from_all: payload.hidden_from_all ?? [] }
  },

  get_groups: async () => [
    { name: 'Production', description: 'Prod fleet' },
    { name: 'Staging', description: '' },
  ],
  save_groups: async (groups) => { console.log('[mock] save_groups', groups) },
  rename_group: async (oldName, newName) => { console.log('[mock] rename_group', oldName, newName) },

  get_profiles: async () => [
    { name: 'app-support', description: 'App-support fleet' },
    { name: 'devops', description: 'DevOps servers' },
  ],
  save_profiles: async (profiles) => { console.log('[mock] save_profiles', profiles) },
  rename_profile: async (oldName, newName) => { console.log('[mock] rename_profile', oldName, newName) },
  get_active_profile: async () => MOCK_ACTIVE_PROFILE,
  set_active_profile: async (name) => {
    MOCK_ACTIVE_PROFILE = name
    return { active: name }
  },

  get_room_hosts: async () => [
    { host: 'web01', room: 'helpdesk/helpdesk', queue: 'helpdesk' },
    { host: 'web02', room: 'helpdesk/helpdesk', queue: 'helpdesk' },
  ],
  get_active_room: async () => MOCK_ACTIVE_ROOM,
  set_active_room: async (host) => {
    MOCK_ACTIVE_ROOM = host
    return { active: host }
  },

  get_self_service: async () => [
    { name: 'reset-password', description: 'Password resets' },
    { name: 'request-vm', description: '' },
  ],
  save_self_service: async (entries) => { console.log('[mock] save_self_service', entries) },

  get_repos: async () => [
    { name: 'payments', url: 'https://git.example.com/team/payments.git', ref: 'main', credential_id: 'svc-prod', description: 'Payments service', checked_out: true, commit: 'a1b2c3d4e5' },
    { name: 'infra', url: 'git@git.example.com:ops/infra.git', ref: 'v2.1.0', path: 'C:\\dev\\infra', checkout_dir: 'C:\\dev\\infra', checked_out: false, commit: '' },
  ],
  save_repos: async (repos) => { console.log('[mock] save_repos', repos) },
  delete_repo: async (name) => { console.log('[mock] delete_repo', name); return { ok: true, removed_checkout: true } },
  sync_repo: async (name) => { console.log('[mock] sync_repo', name); return { ok: true, action: 'cloned', commit: 'a1b2c3d4e5' } },

  get_filters: async () => [
    { name: 'id-lines', pipeline: 'grep -F -- {{arg}} | cut -d, -f1,4', description: 'Lines containing the txn id, keep cols 1 & 4', param_label: 'transaction id' },
    { name: 'errors', pipeline: 'grep -i error' },
  ],

  save_filters: async (filters) => { console.log('[mock] save_filters', filters) },

  import_filters: async (toml) => { console.log('[mock] import_filters', toml); return [] },

  list_credentials: async () => [
    { id: 'svc-prod', label: 'Service account (prod)', kind: 'userpass', username: 'svc_runner', username_arg: 'service-username', secret_arg: 'user-password', scope_hosts: ['prod-*'], scope_runnables: ['deploy'], has_secret: true },
  ],
  credentials_for_runnable: async (host, runnable) => { console.log('[mock] credentials_for_runnable', host, runnable); return [{ id: 'svc-prod', label: 'Service account (prod)', has_secret: true, secret_arg: 'password', username_arg: '', username: '' }] },
  save_credential: async (entry) => { console.log('[mock] save_credential', { ...entry, secret: entry.secret ? '***' : undefined }); return { id: entry.id || 'mock-' + Date.now() } },
  delete_credential: async (id) => { console.log('[mock] delete_credential', id) },
  credentials_backend: async () => ({ available: true, backend: 'WinVaultKeyring' }),

  get_mcp_config: async () => ({
    enabled: true,
    call_timeout: 60,
    connect_timeout: 20,
    servers: [
      { name: 'files', transport: 'stdio', command: 'uvx', args: ['mcp-server-filesystem', '/srv'], enabled: true, autonomy: '' },
      { name: 'search', transport: 'http', url: 'https://mcp.example.com/mcp', credential: 'svc-prod', proxy: 'http://proxy.corp:8080', enabled: true, autonomy: 'autonomous', call_timeout: 120 },
      { name: 'atlassian', transport: 'http', url: 'https://mcp.atlassian.example/mcp', enabled: true, autonomy: 'confirm',
        header_credentials: [
          { header: 'X-Jira-PAT', credential: 'jira-pat' },
          { header: 'X-Confluence-PAT', credential: 'confluence-pat' },
        ] },
    ],
  }),
  save_mcp_config: async (payload) => { console.log('[mock] save_mcp_config', payload); return { available: true, started: true, tool_count: 3, servers: (payload.servers ?? []).map(s => ({ name: s.name, transport: s.transport, connected: true, tool_count: 2, error: '', credential: s.credential ?? '', proxy: s.proxy ?? '' })) } },
  mcp_status: async () => ({ available: true, started: true, tool_count: 3, servers: [
    { name: 'files', transport: 'stdio', connected: true, tool_count: 2, error: '', credential: '', proxy: '' },
    { name: 'search', transport: 'http', connected: false, tool_count: 0, error: 'connection refused', credential: 'svc-prod', proxy: 'http://proxy.corp:8080' },
  ] }),
  mcp_reload: async () => ({ available: true, started: true, tool_count: 3, servers: [] }),
  mcp_credential_options: async () => [
    { id: 'svc-prod', label: 'Service account (prod)', kind: 'userpass' },
    { id: 'jira-pat', label: 'Jira PAT', kind: 'token' },
    { id: 'confluence-pat', label: 'Confluence PAT', kind: 'token' },
  ],

  get_mcp_server_config: async () => ({
    enabled: false, host: '127.0.0.1', port: 8765, token: null,
    default_autonomy: null, confirm_wait: 50, tool_style: 'leaf', expose_builtins: false,
  }),
  save_mcp_server_config: async (payload) => {
    console.log('[mock] save_mcp_server_config', payload)
    return {
      running: !!payload.enabled, error: '', host: payload.host ?? '127.0.0.1',
      port: payload.port ?? 8765, url: payload.enabled ? `http://127.0.0.1:${payload.port ?? 8765}/mcp` : '',
      auth: !!payload.token, tools: payload.enabled ? 12 : 0, builtins: payload.expose_builtins ? 8 : 0,
      tool_style: payload.tool_style ?? 'leaf',
    }
  },
  mcp_server_status: async () => ({
    running: false, error: '', host: '127.0.0.1', port: 8765, url: '', auth: false, tools: 0, builtins: 0, tool_style: 'leaf',
  }),
  mcp_server_start: async () => ({
    running: false, error: '', host: '127.0.0.1', port: 8765, url: '', auth: false, tools: 0,
  }),

  get_sync_config: async () => ({
    url: '', ref: '', credential_id: '', subpath: '', layers: [], ca_bundle: '', tls_verify: true,
    files: ['config.toml', 'runspec_hosts.toml', 'runspec_triggers.toml', 'runspec_filters.toml', 'runspec_credentials.toml'],
    preserve: [],
    backups_keep: 5,
    last_ref: '', last_commit: '', last_synced: '',
    engine: 'dulwich',
    files_default: ['config.toml', 'runspec_hosts.toml', 'runspec_triggers.toml', 'runspec_filters.toml', 'runspec_credentials.toml'],
    preserve_default: ['config.toml:mailbox.folder', 'runspec_hosts.toml:*.room_user'],
  }),
  save_sync_config: async (data) => { console.log('[mock] save_sync_config', data) },
  list_sync_tags: async () => ({ tags: ['v1.2.0', 'v1.1.0', 'v1.0.0'] }),
  sync_pull: async () => { console.log('[mock] sync_pull'); return { ok: true, ref: 'v1.2.0', commit: 'a1b2c3d4e5f6', written: ['config.toml', 'runspec_hosts.toml'], skipped: [], backups: ['config.toml.bak-20260616-043000'], pruned: [] } },
  seed_status: async () => ({ available: true, stamp: { 'mycorp-console': '1.4.0' }, applied: { 'mycorp-console': '1.4.0' }, last_applied: '2026-06-29T09:15:00Z', pending: false, overrides: [
    { file: 'config.toml', path: 'llm.api_key_command', seed: 'get-key', local: 'C:/venv/Scripts/get-key.exe', preserved: false },
    { file: 'runspec_credentials.toml', path: 'svc-prod.username', seed: 'svc_runner', local: 'j.finestone', preserved: true },
  ] }),
  seed_apply: async (force) => { console.log('[mock] seed_apply', force); return { ok: true, applied: true, stamp: { 'mycorp-console': '1.4.0' }, written: ['config.toml'], skipped: [], backups: [], pruned: [] } },
  get_docs_config: async () => ({ dir: '', default: '/home/user/.config/runspec-console/docs' }),
  save_docs_config: async (dest_dir) => { console.log('[mock] save_docs_config', dest_dir) },
  generate_docs: async (dest_dir) => { console.log('[mock] generate_docs', dest_dir); return { ok: true, dir: dest_dir || '/home/user/.config/runspec-console/docs', written: ['README.md', 'hosts.md', 'runbooks/restart-web.md'] } },

  test_host: async (name): Promise<TestResult> => {
    await new Promise(r => setTimeout(r, 1200))
    if (name === 'prod-2') {
      return { connected: true, runspec_ok: false, runnable_count: 0,
               stdout: '', stderr: 'bash: /usr/local/bin/runspec: No such file or directory', exit_code: 127 }
    }
    return { connected: true, runspec_ok: true, runnable_count: 3,
             stdout: '[]', stderr: '', exit_code: 0 }
  },

  create_schedule: async (data) => {
    const s = data as unknown as Schedule
    const id = s.id || `rs-${s.runnable ?? s.event ?? 'sched'}-${Date.now()}`
    MOCK_SCHEDULES = [...MOCK_SCHEDULES, { ...s, id, nextRun: new Date(Date.now() + 60 * 60 * 1000).toISOString() }]
  },

  update_schedule: async (id, data) => {
    const s = data as unknown as Schedule
    MOCK_SCHEDULES = MOCK_SCHEDULES.map(x => (x.id === id ? { ...s, id } : x))
  },

  delete_schedule: async (id) => { MOCK_SCHEDULES = MOCK_SCHEDULES.filter(s => s.id !== id) },

  run_schedule_now: async (id) => {
    const s = MOCK_SCHEDULES.find(x => x.id === id)
    console.log('[mock] run_schedule_now', id)
    if (s?.action === 'event') return { action: 'event', event: s.event }
    return { action: 'runnable', inv_id: `inv-${Date.now()}`, host: s?.host, runnable: s?.runnable }
  },

  import_jump_hosts: async (content) => {
    // Minimal TOML section parser — mirrors what Python's tomllib.loads() does on the real bridge
    const hosts: JumpHost[] = []
    let name = ''
    let current: Record<string, string> = {}
    for (const raw of content.split('\n')) {
      const line = raw.trim()
      const section = line.match(/^\[([^\]]+)\]$/)
      if (section) {
        if (name && current.hostname) hosts.push({ name, role: 'primary', ...current } as unknown as JumpHost)
        name = section[1].trim()
        current = {}
        continue
      }
      const kv = line.match(/^([\w-]+)\s*=\s*"([^"]*)"$/)
      if (kv && name) current[kv[1]] = kv[2]
    }
    if (name && current.hostname) hosts.push({ name, role: 'primary', ...current } as unknown as JumpHost)
    return hosts
  },

  invoke_runnable: async (host, runnable, args, commandPath = [], _group?) => {
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

  cancel_invocation: async (invId) => {
    console.log('[mock] cancel_invocation', invId)
  },

  start_transfer: async (sourceHost, sourcePaths, destHost, destDir) => {
    console.log('[mock] start_transfer', { sourceHost, sourcePaths, destHost, destDir })
    if (!sourcePaths.length) return { error: 'no source files selected' }
    const id = `xfer-${++invocationCounter}`
    const file = sourcePaths[0].split(/[\\/]/).pop() || 'file'
    const total = 1_048_576
    let sent = 0
    const tick = () => {
      sent = Math.min(total, sent + 262_144)
      window.dispatchEvent(new CustomEvent('runspec:transfer_progress', {
        detail: { id, file, index: 0, count: sourcePaths.length, transferred: sent, total },
      }))
      if (sent < total) setTimeout(tick, 250)
      else window.dispatchEvent(new CustomEvent('runspec:transfer_end', {
        detail: { id, ok: true, files: sourcePaths.length, total: sourcePaths.length, error: null },
      }))
    }
    setTimeout(tick, 250)
    return { id }
  },
  start_fanout: async (sourcePaths, destHosts, destDir) => {
    console.log('[mock] start_fanout', { sourcePaths, destHosts, destDir })
    if (!sourcePaths.length) return { error: 'no source files selected' }
    if (!destHosts.length) return { error: 'no target hosts selected' }
    const id = `fan-${++invocationCounter}`
    const file = sourcePaths[0].split(/[\\/]/).pop() || 'file'
    const total = 1_048_576
    const sent: Record<string, number> = Object.fromEntries(destHosts.map(h => [h, 0]))
    const tick = () => {
      let allDone = true
      for (const h of destHosts) {
        sent[h] = Math.min(total, sent[h] + 262_144 * (0.5 + Math.random()))
        if (sent[h] < total) allDone = false
        window.dispatchEvent(new CustomEvent('runspec:transfer_progress', {
          detail: { id, host: h, file, index: 0, count: sourcePaths.length, transferred: Math.round(sent[h]), total },
        }))
      }
      if (!allDone) setTimeout(tick, 250)
      else window.dispatchEvent(new CustomEvent('runspec:transfer_end', {
        detail: { id, ok: true, files: sourcePaths.length, total: sourcePaths.length, error: null, hosts: destHosts.map(h => ({ host: h, ok: true, error: null })) },
      }))
    }
    setTimeout(tick, 250)
    return { id }
  },
  start_relay: async (source, sourcePaths, destHost, destDir) => {
    console.log('[mock] start_relay', { source: { ...source, password: source.password ? '***' : undefined }, sourcePaths, destHost, destDir })
    if (!sourcePaths.length) return { error: 'no source files selected' }
    const id = `relay-${++invocationCounter}`
    const file = sourcePaths[0].split(/[\\/]/).pop() || 'file'
    const total = 4_194_304
    let sent = 0
    const tick = () => {
      sent = Math.min(total, sent + 524_288)
      window.dispatchEvent(new CustomEvent('runspec:transfer_progress', {
        detail: { id, file, index: 0, count: sourcePaths.length, transferred: sent, total },
      }))
      if (sent < total) setTimeout(tick, 200)
      else window.dispatchEvent(new CustomEvent('runspec:transfer_end', {
        detail: { id, ok: true, files: sourcePaths.length, total: sourcePaths.length, error: null },
      }))
    }
    setTimeout(tick, 200)
    return { id }
  },
  list_dir_adhoc: async (jump, host, path = '', _user, _password, _port, dirsOnly = false, _credentialId?) => {
    console.log('[mock] list_dir_adhoc', { jump, host, path, dirsOnly })
    const cwd = path && path !== '/' ? path.replace(/\/$/, '') : '/var/log/app'
    const entries = [
      { name: 'archive', type: 'directory' as const, size: 4096, mtime: 1717000000 },
      { name: 'today.log', type: 'file' as const, size: 51200, mtime: 1717000500 },
      { name: 'errors.log', type: 'file' as const, size: 8192, mtime: 1717000400 },
    ].filter(e => !dirsOnly || e.type === 'directory')
    return { cwd, parent: cwd === '/' ? null : cwd.split('/').slice(0, -1).join('/') || '/', entries, truncated: false }
  },
  list_dir_unsaved: async (hostname, path = '', user, _port, _identityFile, dirsOnly = false) => {
    console.log('[mock] list_dir_unsaved', { hostname, path, user, dirsOnly })
    if (!hostname) return { cwd: '', parent: null, entries: [], truncated: false, error: 'no hostname specified' }
    const home = user ? `/home/${user}` : '/home/deploy'
    const cwd = path && path !== '/' ? path.replace(/\/$/, '') : home
    const entries = [
      { name: '.venv', type: 'directory' as const, size: 4096, mtime: 1717000000 },
      { name: 'venvs', type: 'directory' as const, size: 4096, mtime: 1717000100 },
      { name: 'bin', type: 'directory' as const, size: 4096, mtime: 1717000200 },
      { name: 'runspec', type: 'file' as const, size: 1024, mtime: 1717000500 },
    ].filter(e => !dirsOnly || e.type === 'directory')
    return { cwd, parent: cwd === '/' ? null : cwd.split('/').slice(0, -1).join('/') || '/', entries, truncated: false }
  },
  close_adhoc: async () => { console.log('[mock] close_adhoc') },
  check_dir_exists: async (hosts, path) => {
    console.log('[mock] check_dir_exists', { hosts, path })
    // Demo: pretend the folder is missing on any host whose name ends in '-2'.
    return Object.fromEntries(hosts.map(h => [h, { exists: !h.endsWith('-2'), error: null }]))
  },
  cancel_transfer: async (id) => {
    console.log('[mock] cancel_transfer', id)
  },
  current_workspace_path: async () => '/home/demo/.runspec-console/workspaces/chat-demo',

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

  get_analytics: async (hosts, sinceDays = 30, runnable) => {
    await new Promise(r => setTimeout(r, 250))   // simulate the heavier scan
    let runs = _ANALYTICS_RUNS
    if (hosts.length > 0 && !hosts.includes('all')) {
      runs = runs.filter(r => hosts.includes(r.host))
    }
    if (runnable) runs = runs.filter(r => r.runnable === runnable)
    return _aggregateMock(runs, sinceDays)
  },

  // ── logs management ─────────────────────────────────────────────────────
  logs_status: async (_host, _runnable) => {
    const iso = (daysAgo: number) => new Date(Date.now() - daysAgo * 86400000).toISOString().replace(/\.\d+Z$/, 'Z')
    return [
      {
        group: 'ops-tools', ok: true, dirs: ['/opt/venvs/ops-tools/logs'],
        total_bytes: 5_242_880, total_files: 142,
        runnables: [
          { runnable: 'backup', per_run_files: 118, archives: 2, total_bytes: 4_194_304, oldest: iso(96), newest: iso(0) },
          { runnable: 'get-alerts', per_run_files: 22, archives: 0, total_bytes: 1_048_576, oldest: iso(14), newest: iso(1) },
        ],
      },
      {
        group: 'platform-core', ok: true, dirs: ['/opt/venvs/platform-core/logs'],
        total_bytes: 786_432, total_files: 9,
        runnables: [
          { runnable: 'deploy', per_run_files: 9, archives: 0, total_bytes: 786_432, oldest: iso(20), newest: iso(2) },
        ],
      },
    ]
  },

  logs_view: async (_host, _group, runnable, _since, _user, _run) => ({
    ok: true,
    records: Array.from({ length: 6 }, (_v, i) => ({
      ts: new Date(Date.now() - (6 - i) * 600000).toISOString(),
      level: i === 4 ? 'ERROR' : 'INFO',
      message: i === 4 ? `${runnable}: upstream timeout` : `${runnable}: step ${i + 1} ok`,
      extra: { run_id: `r${Math.floor(i / 3)}`, user: 'alice' },
    })),
  }),

  logs_prune: async (_host, group, _runnable, _olderThan, _maxFiles, _maxTotalSize, dryRun = true) => ({
    group, ok: true, dry_run: dryRun, count: 12, freed_bytes: 1_572_864,
    deleted: Array.from({ length: 12 }, (_v, i) => ({ path: `/opt/venvs/${group}/logs/backup.2026010${i}T100000Z.${i}abcd.log`, bytes: 131_072 })),
  }),

  logs_compact: async (_host, group, _runnable, _olderThan, _gzip = true, dryRun = true) => ({
    group, ok: true, dry_run: dryRun, compacted: 30,
    archives: [{ archive: `/opt/venvs/${group}/logs/backup.archive.20260604.log.gz`, count: 30, sources: [] }],
  }),

  get_public_key: async () => ({
    ok: true,
    public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockPublicKeyForDevModeonlyrunspec-console',
    message: '',
  }),

  copy_ssh_id: async (host: string) => {
    await new Promise(r => setTimeout(r, 600))
    return { ok: true, connected: true, message: `Public key copied — ${host} is now connected.` }
  },

  copy_ssh_id_all: async (_password: string, targets?: string[]) => {
    await new Promise(r => setTimeout(r, 1000))
    const names = targets && targets.length ? targets : ['prod-1', 'prod-2']
    const per_host = names.map((host, i) => i === names.length - 1 && names.length > 1
      ? { host, ok: false, connected: false, error: 'Authentication failed — check the password.' }
      : { host, ok: true, connected: true, error: '' })
    const okN = per_host.filter(h => h.ok).length
    const failN = per_host.length - okN
    return {
      ok: okN > 0,
      per_host,
      message: failN === 0
        ? `Public key copied to all ${okN} host(s).`
        : `Public key copied to ${okN} of ${per_host.length} host(s) — ${failN} failed.`,
    }
  },

  generate_ssh_key: async () => {
    await new Promise(r => setTimeout(r, 600))
    MOCK_KEY_CREATED_AT = new Date().toISOString()
    return {
      ok: true, committed: true, per_host: [],
      public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockPublicKeyForDevModeonlyrunspec-console',
      key_path: 'C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519',
      message: 'Key generated at C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519',
    }
  },

  rotate_ssh_key: async () => {
    // Emit the same per-host progress events the real backend does, so the
    // rotation dialog's progress bar is exercisable in dev/mock mode.
    const total = 2
    window.dispatchEvent(new CustomEvent('runspec:rotate_progress', { detail: { done: 0, total, host: '', phase: 'start' } }))
    await new Promise(r => setTimeout(r, 900))
    window.dispatchEvent(new CustomEvent('runspec:rotate_progress', { detail: { done: 1, total, host: 'prod-1', phase: 'verified' } }))
    await new Promise(r => setTimeout(r, 900))
    window.dispatchEvent(new CustomEvent('runspec:rotate_progress', { detail: { done: 2, total, host: 'prod-2', phase: 'verified' } }))
    MOCK_KEY_CREATED_AT = new Date().toISOString()
    return {
      ok: true, committed: true,
      public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockPublicKeyForDevModeonlyrunspec-console',
      key_path: 'C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519',
      per_host: [
        { host: 'prod-1', pushed: true, verified: true, skipped: false, error: '' },
        { host: 'prod-2', pushed: false, verified: false, skipped: true, error: 'disconnected — new key not pushed; update manually' },
      ],
      message: 'Key rotated — verified on 1 host(s). 1 host(s) skipped (disconnected or per-host key) — update them manually.',
    }
  },

  puttygen_path: async () => 'C:/Program Files/PuTTY/puttygen.exe',
  launch_puttygen: async () => { console.log('mock: launch_puttygen') },
  open_putty_url: async (url: string) => { console.log('mock: open_putty_url', url) },
  browse_ssh_binary: async () => '',
  browse_key_file: async () => '',
  browse_folder: async () => '',
  browse_sound_file: async () => '',

  // Mirrors the Python bridge's gate round-trip: send_chat parks a resolver
  // keyed by request_id; resolve_tool_confirmation (called by the UI's
  // Approve/Deny buttons) wakes it. Lets the browser mock exercise the real
  // autonomy modal instead of pretending every tool is autonomous.
  resolve_tool_confirmation: async (requestId, approved, editedInput) => {
    console.log('[mock] resolve_tool_confirmation', requestId, approved, editedInput)
    const resolve = _pendingConfirms.get(requestId)
    if (resolve) { _pendingConfirms.delete(requestId); resolve(approved) }
  },

  // AI-off "Clear": the session is replaced by a fresh one (the old turn, if
  // any, is stopped). "New chat" uses new_chat_session instead.
  clear_chat: async (sessionId?: string) => {
    const replaced = sessionId ?? _currentSession ?? undefined
    if (replaced) {
      for (const c of _activeChats.values()) if (c.session === replaced) c.cancelled = true
      _dropSession(replaced)
    }
    const session = _newMockSession()
    _dispatch('runspec:chat_cleared', { session: session.id, replaced })
    return { ok: true, session, replaced }
  },
  compact_chat: async (_sessionId?: string) => ({ ok: true, compacted: 0 }),
  new_chat_session: async () => _newMockSession(),
  list_chat_sessions: async () => [..._sessions.values()].sort((a, b) => a.created_at - b.created_at),
  select_chat_session: async (id: string) => {
    const session = _sessions.get(id)
    if (!session) return { ok: false, error: `No such session: ${id}` }
    if (!session.interactive) return { ok: false, error: 'Automation sessions are read-only' }
    _currentSession = id
    return { ok: true, session }
  },
  close_chat_session: async (id: string) => {
    for (const c of _activeChats.values()) if (c.session === id) c.cancelled = true
    _dropSession(id)
    return { ok: true }
  },
  set_turn_included: async (_chatId: string, included: boolean) => ({ ok: true, included }),
  list_conversations: async () => [
    { id: 'mock-conv-1', title: 'Why did the nightly job fail?', created: '2026-06-27T20:00:00Z', updated: '2026-06-27T20:05:00Z', turn_count: 3, preview: 'Three jobs errored on host db1 — all OOM kills.' },
    { id: 'mock-conv-2', title: 'Draft the release notes', created: '2026-06-26T09:00:00Z', updated: '2026-06-26T09:12:00Z', turn_count: 5, preview: 'Here are the release notes for 0.145.0…' },
  ],
  load_conversation: async (id: string) => ({
    id, title: id === 'mock-conv-2' ? 'Draft the release notes' : 'Why did the nightly job fail?',
    turns: [
      { user: 'why did the nightly job fail?', assistant: 'Three jobs errored on host db1 — all OOM kills.', tools: [{ name: 'summarize-log', input: { path: '/var/log/app.log' }, output: '{"errors": 3}' }] },
      { user: 'can you restart them?', assistant: 'Restarted all three; they are healthy now.', tools: [] },
    ],
  }),
  // A resumed conversation becomes a live session whose id == the conversation
  // id; resuming one that's already open just reports `already_open`.
  resume_conversation: async (id: string) => {
    const already_open = _sessions.has(id)
    if (!already_open) _newMockSession(id, id === 'mock-conv-2' ? 'Draft the release notes' : 'Why did the nightly job fail?')
    else _currentSession = id
    return { ok: true, turns: 2, kept: 2, compacted: false, summarized: false, exact: false, session: id, already_open }
  },
  delete_conversation: async (_id: string) => {},
  rename_conversation: async (_id: string, _title: string) => {},
  run_improve_report: async (_windowDays?: number, comment?: string) => ({
    id: 'mock-improve-1',
    ts: '2026-09-03T10:00:00Z',
    kind: (comment ? 'problem_report' : 'evaluation') as 'evaluation' | 'problem_report',
    product_name: 'runspec console',
    developer_emails: ['dev@example.com'],
    comment: comment || '',
    window_days: 30,
    since_iso: '2026-08-04T10:00:00Z',
    metrics: { turns: 42, searches: 18, empty_searches: 2, runs: 15, arg_bounces: 3, stops: 1, durable_errors: 0, autonomy_denied: 1, autonomy_edited: 0, history_runs: 60, history_failures: 4 },
    scores: { arg_bounce_rate_pct: 20, search_before_run_pct: 87, empty_search_rate_pct: 11, stop_rate_pct: 2, history_failure_rate_pct: 6.7 },
    bounce_kinds: { unknown_arg: 2, bad_choice: 1 },
    autonomy_by_tool: { 'grp__wipe': { denied: 1, edited: 0 } },
    runnables: [{ runnable: 'drain', runs: 6, arg_bounces: 2, failures: 1, total_runs_history: 10, arg_bounce_rate_pct: 33.3, failure_rate_pct: 10 }],
    incidents: [
      { conversation_id: 'mock-conv-1', turn_index: 2, category: 'arg_bounce', runnable: 'drain', tool: 'run_runnable', bounce_kind: 'unknown_arg', evidence: 'Error: unknown argument(s) for drain: nodex' },
      { conversation_id: 'mock-conv-1', turn_index: 4, category: 'stop', evidence: 'paused after 25 tool steps' },
    ],
    incident_total: 2,
    incident_shown: 2,
    markdown: '# runspec console — Improve report\n\nHeadline metrics…',
    learnings_toml: '[[learning]]\ncategory = "runnable_args"\ntarget = "drain"\nwhy = "2 arg bounce(s)"\nfix = ""\nconfidence = "low"\n',
  }),
  list_improve_reports: async () => [
    { id: 'mock-improve-1', ts: '2026-09-03T10:00:00Z', kind: 'evaluation' as const, comment: '', product_name: 'runspec console', has_synthesis: false },
  ],
  load_improve_report: async (_id: string) => null,
  delete_improve_report: async (_id: string) => true,
  synthesize_improve_report: async (id: string) => ({
    ...(await mockApi.run_improve_report()),
    id,
    synthesis: { summary: 'The agent bounces on drain args; add a clearer node arg description.', correlation: '', recommendations: [{ category: 'runnable_args', target: 'drain', why: '33% arg-bounce rate on node', fix: 'Rename/clarify the node arg and add options.', confidence: 'high' }] },
  }),
  improve_mailto: async (_id: string) => 'mailto:dev@example.com?subject=Improve',
  run_evals: async (
    _fixtureToml?: string,
    _fixtureId?: string,
    _samples?: number,
    _validate?: boolean,
    _telemetry?: boolean,
  ) => ({
    ok: true as const,
    id: 'mock-eval-1',
    generated_at: '2026-09-11T09:00:00',
    provider: 'anthropic',
    model: 'claude-sonnet-4-6',
    tool_mode: 'dedup',
    samples: 1,
    summary: { total: 2, passed: 1, failed: 1, pass_rate: 0.5, by_category: { wrong_command: 1 } },
    cases: [
      { case_id: 'restart-nginx', passed: true, category: 'pass', reason: "tool + args as expected; would gate at 'confirm'", terminal: { name: 'restart-service', input: { service: 'nginx' } }, target: 'restart-service', command: '', autonomy: 'confirm', host: 'web-01', final_text: '', samples: 1, pass_count: 1, pass_rate: 1 },
      { case_id: 'cron-add', passed: false, category: 'wrong_command', reason: "expected subcommand 'add', got 'remove'", terminal: { name: 'cron', input: { command: 'remove' } }, target: 'cron', command: 'remove', autonomy: 'confirm', host: 'web-01', final_text: '', samples: 1, pass_count: 0, pass_rate: 0 },
    ],
  }),
  list_eval_reports: async () => [
    { id: 'mock-eval-1', generated_at: '2026-09-11T09:00:00', model: 'claude-sonnet-4-6', tool_mode: 'dedup', summary: { total: 2, passed: 1, failed: 1, pass_rate: 0.5, by_category: { wrong_command: 1 } } },
  ],
  load_eval_report: async (_id: string) => null,
  delete_eval_report: async (_id: string) => true,
  list_eval_fixtures: async () => [{ id: 'linux-fs', cases: 10 }],
  get_eval_fixture_text: async (_id: string) =>
    '[[eval]]\nid = "restart-nginx"\nprompt = "restart nginx on web-01"\nexpect_tool = "restart-service"\nexpect_args = { service = "nginx" }\n',
  save_eval_fixture: async (_id: string, _toml: string) => ({ ok: true, cases: 1 }),
  delete_eval_fixture: async (_id: string) => true,
  get_runbooks: async () => [],
  save_runbooks: async () => ({ ok: true, count: 0 }),
  save_runbook_entry: async () => ({ ok: true, id: '' }),
  delete_runbook: async () => ({ ok: true, removed: true }),
  run_runbook: async (runbookId, _values, _extra, _sessionId) => ({ chat_id: `mock-runbook-${runbookId}`, message: `$${runbookId}` }),
  get_saved_runs: async () => [
    {
      id: 'restart-web', title: 'Restart web tier', description: 'Rolling restart of nginx across the web hosts',
      runnable: 'service-restart', command: [],
      host: '', group: 'Production', hosts: [], selected_hosts: [],
      args: { service: 'nginx' }, credential_ids: [], target: 'group Production',
      params: [{ name: 'reason', type: 'str', required: true, description: 'Change ticket / reason' }],
    },
    {
      id: 'disk-check', title: 'Disk space check', description: 'Report free space on the primary host',
      runnable: 'df-report', command: [],
      host: 'prod-1', group: '', hosts: [], selected_hosts: [],
      args: { threshold: 80 }, credential_ids: [], target: 'prod-1',
      params: [],
    },
  ],
  save_saved_runs: async () => ({ ok: true, count: 0 }),
  run_saved_run: async (id, _values) => ({ id, runnable: 'mock', runs: [{ host: 'local', inv_id: `mock-saved-${id}` }] }),
  cancel_chat: async (chatId: string) => {
    const targets = chatId ? [_activeChats.get(chatId)].filter(Boolean) : [..._activeChats.values()]
    for (const active of targets) {
      if (!active) continue
      active.cancelled = true
      // Wake a pending confirm prompt for this turn, mirroring the real bridge.
      const resolve = _pendingConfirms.get(`mock-${active.id}`)
      if (resolve) { _pendingConfirms.delete(`mock-${active.id}`); resolve(false) }
    }
  },

  send_chat: async (message, _invocationId, sessionId) => {
    const id = `chat-${++invocationCounter}`
    const dispatch = _dispatch
    const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))
    const session = sessionId ?? _currentSession ?? _newMockSession().id
    const sinfo = _sessions.get(session)
    // Sending to an automation (read-only) session: an error token + a failed
    // run_end under the chat id, as the real bridge does.
    if (!sinfo || !sinfo.interactive) {
      ;(async () => {
        await delay(10)
        dispatch('runspec:token', { id, token: `⚠ Error: session ${session} is not an interactive chat.` })
        dispatch('runspec:run_end', { id, session, exit_code: 1, duration_ms: 1, stopped: false })
      })()
      return id
    }
    const active = { id, session, cancelled: false }
    _activeChats.set(id, active)
    _patchSession(session, { state: 'running', turn: id, label: sinfo.label || message.slice(0, 40) })
    const finish = (exit_code: number, duration_ms: number, stopped = false) => {
      dispatch('runspec:run_end', { id, session, exit_code, duration_ms, stopped })
      _activeChats.delete(id)
      const cur = _sessions.get(session)
      if (cur) _patchSession(session, { state: 'idle', turn: '', turns: cur.turns + 1 })
    }

    ;(async () => {
      await delay(10)  // yield so addBlock fires before first token
      // Phase 1: stream intro text
      const intro = `Sure, let me run the DNS flush for you.`
      for (const ch of intro) {
        dispatch('runspec:token', { id, token: ch })
        await delay(18)
      }
      await delay(200)

      // Simulate a tool call whose autonomy is `confirm` — the gate must
      // prompt the user before running it.
      const tool_name = 'local__flush-dns'
      const tool_input = { scope: 'system' }
      dispatch('runspec:tool_start', { id, tool_name, tool_input })

      const request_id = `mock-${id}`
      const approved = await new Promise<boolean>(resolve => {
        _pendingConfirms.set(request_id, resolve)
        dispatch('runspec:tool_confirm', {
          id, request_id, tool_name, tool_input, autonomy: 'confirm',
        })
      })

      // Stop pressed while the confirm dialog was up — refuse and halt.
      if (active.cancelled) {
        dispatch('runspec:tool_end', {
          id, tool_name,
          output: "⏹ 'flush-dns' was not run — you stopped the assistant before approving it.",
        })
        dispatch('runspec:token', { id, token: '\n⏹ Stopped.' })
        finish(0, 800, true)
        return
      }

      const output = approved
        ? 'flushing system DNS cache…\nDNS cache flushed'
        : "✗ The user declined to run 'flush-dns' (autonomy = confirm)."
      dispatch('runspec:tool_end', { id, tool_name, output })
      await delay(150)

      // Phase 2: stream follow-up text reflecting the decision
      const outro = approved
        ? `\n\nDone — the system DNS cache has been flushed.`
        : `\n\nNo problem, I won't run it. Just say the word when you're ready.`
      for (const ch of outro) {
        dispatch('runspec:token', { id, token: ch })
        await delay(14)
      }

      finish(approved ? 0 : 1, 1500)
      dispatch('runspec:chat_usage', {
        id, session, input_tokens: 1247, output_tokens: 342,
        cache_read_tokens: 8912, cache_creation_tokens: 0,
      })
      dispatch('runspec:context_estimate', {
        session, model: 'mock-model', window: 200000, total_tokens: 10501, cache_hit_pct: 84,
        buckets: [
          { key: 'system', tokens: 1200, est: true }, { key: 'tools', tokens: 6400, est: true },
          { key: 'history', tokens: 2901, est: true },
        ],
        tool_mode: 'search', history_msgs: 2 * (_sessions.get(session)?.turns ?? 1),
      })
    })()

    return id
  },
  launch_terminal: async (host: string): Promise<void> => {
    console.log('mock: launch_terminal', host)
  },
  launch_local_terminal: async (): Promise<void> => {
    console.log('mock: launch_local_terminal')
  },
  is_debug_enabled: async (): Promise<boolean> => true,
  open_devtools: async (): Promise<void> => {
    console.log('mock: open_devtools')
  },
  log_client_error: async (context, message): Promise<void> => {
    console.error('[mock] log_client_error', context, message)
  },

  // ── event triggers ─────────────────────────────────────────────────────────
  list_triggers: async () => ({
    triggers: [
      {
        name: 'vip-outage-triage',
        source: 'outlook',
        event: 'new_mail',
        mode: 'agent',
        enabled: true,
        autonomy: 'confirm',
        rules: 'If this reports an outage, run diagnostics and draft a reply.',
        match: { from: '.*@bigcustomer\\.com' },
        once_per_thread: true,
        reply: { draft: true, style: 'Concise, professional.' },
      },
      {
        name: 'invoice-reminder',
        source: 'outlook',
        event: 'reminder',
        mode: 'rule',
        enabled: false,
        run: 'local__notify',
        map: { message: '{{ subject }}' },
      },
    ],
    errors: [],
    sources: [
      { id: 'outlook', available: false, reason: 'Outlook source requires Windows', event_types: ['new_mail', 'reminder'] },
    ],
    rota: {
      enabled: true,
      source: 'console',
      shift_marker: 'shift',
      hold_during_meetings: false,
      lock_suspend: true,
      mon: { on: true, start: '09:00', end: '17:30' },
      tue: { on: true, start: '09:00', end: '17:30' },
      wed: { on: true, start: '09:00', end: '17:30' },
      thu: { on: true, start: '09:00', end: '17:30' },
      fri: { on: true, start: '09:00', end: '16:00' },
      sat: { on: false, start: '09:00', end: '17:00' },
      sun: { on: false, start: '09:00', end: '17:00' },
    },
    running: true,
    screen_state: 'unlocked',
  }),
  set_trigger_enabled: async (name: string, enabled: boolean) => {
    console.log('mock: set_trigger_enabled', name, enabled)
    return { ok: true }
  },
  save_trigger: async (trigger) => {
    console.log('mock: save_trigger', trigger)
    return { ok: true }
  },
  list_action_tools: async () => [
    { name: 'create_document', label: 'Create document' },
    { name: 'run_runbook', label: 'Run runbook (runbook)' },
    { name: 'create_schedule', label: 'Create schedule' },
    { name: 'emit_event', label: 'Emit event' },
  ],
  test_trigger: async (name: string, body: string, fields: Record<string, string>) => {
    console.log('mock: test_trigger', name, body, fields)
    return { ok: true }
  },
  submit_paste_input: async (name: string, text: string, fields: Record<string, string>) => {
    console.log('mock: submit_paste_input', name, text, fields)
    return { ok: true, skipped: undefined }
  },
  claim_email: async (cardId: string) => {
    console.log('mock: claim_email', cardId)
    return { claimed: true, owner: 'you' }
  },
  delete_trigger: async (name: string) => {
    console.log('mock: delete_trigger', name)
    return { ok: true }
  },
  save_rota: async (rota) => {
    console.log('mock: save_rota', rota)
    return { ok: true }
  },
  start_break: async (minutes) => {
    console.log('mock: start_break', minutes)
    const until = new Date(Date.now() + minutes * 60_000).toISOString().slice(0, 19)
    return { break_until: until, reminder_set: true }
  },
  cancel_break: async () => {
    console.log('mock: cancel_break')
    return { ok: true }
  },
  screen_state: async () => 'unlocked' as const,
  start_voice_dictation: async () => ({ ok: false }),
  stop_voice_dictation: async () => ({ ok: false }),
  dictation_status: async () => ({
    available: false, reason: 'Dictation is not available in the browser preview',
    listening: false, session_id: null, model: 'medium-streaming', language: 'en', vendored: false,
    silence_timeout: 8, keep_loaded: true, model_loaded: false,
  }),
  start_dictation: async () => ({ ok: false, error: 'Dictation is not available in the browser preview' }),
  stop_dictation: async () => ({ ok: true, session_id: null }),
  get_voice_config: async () => ({
    enabled: true, language: 'en', model: 'medium-streaming', model_dir: null, device: null,
    update_interval: 0.5, silence_timeout: 8, keep_loaded: true,
    available: false, reason: 'Dictation is not available in the browser preview', model_loaded: false,
    models: ['tiny-streaming', 'base-streaming', 'small-streaming', 'medium-streaming'],
  }),
  save_voice_config: async (cfg) => {
    console.log('[mock] save_voice_config', cfg)
    return {
      ...cfg, available: false, reason: 'Dictation is not available in the browser preview',
      model_loaded: false, models: ['tiny-streaming', 'base-streaming', 'small-streaming', 'medium-streaming'],
    }
  },
  list_voice_devices: async () => [{ index: 1, name: 'Built-in microphone', default: true }],
  get_trigger_board: async () => [
    {
      card_id: 'demo1', trigger: 'vip-outage-triage', source: 'outlook',
      event_type: 'new_mail', summary: 'Outage on web01 — site returning 503',
      column: 'done', detail: { result: '• check-memory → 87% used\n• disk-usage → /var 92%\n\nDrafted a reply summarising the findings.' },
      created_at: Date.now() / 1000 - 300,
      timeline: [
        { ts: Date.now() / 1000 - 300, text: 'triggered' },
        { ts: Date.now() / 1000 - 299, text: 'triage' },
        { ts: Date.now() / 1000 - 297, text: 'check-memory → 87% used' },
        { ts: Date.now() / 1000 - 295, text: 'disk-usage → /var 92%' },
        { ts: Date.now() / 1000 - 250, text: 'draft_reply → ✓ saved to Drafts' },
        { ts: Date.now() / 1000 - 250, text: 'done' },
      ],
    },
    {
      card_id: 'demo2', trigger: 'vip-outage-triage', source: 'outlook',
      event_type: 'new_mail', summary: 'Re: scheduled maintenance window',
      column: 'triage', detail: {}, created_at: Date.now() / 1000 - 30,
      timeline: [
        { ts: Date.now() / 1000 - 30, text: 'triggered' },
        { ts: Date.now() / 1000 - 29, text: 'triage' },
      ],
    },
  ],
  get_pending_confirms: async () => [
    {
      id: 'chat-main', request_id: 'req-demo-1', tool_name: 'prod-1__restart-service',
      tool_input: { service: 'nginx' }, autonomy: 'confirm', created_at: Date.now() / 1000 - 45,
    },
  ],
  get_trigger_events: async () => [
    { id: 'e1', source: 'outlook', type: 'new_mail', summary: 'Outage on web01 — site returning 503', matched: ['vip-outage-triage'], skipped: [], ts: Date.now() / 1000 - 300 },
    { id: 'e2', source: 'outlook', type: 'new_mail', summary: 'Lunch order for Friday', matched: [], skipped: [], ts: Date.now() / 1000 - 120 },
    { id: 'e3', source: 'outlook', type: 'new_mail', summary: 'URGENT: cert expiring', matched: [], skipped: ['vip-outage-triage'], skip_reasons: { 'vip-outage-triage': 'working hours' }, ts: Date.now() / 1000 - 90 },
    { id: 'e4', source: 'outlook', type: 'new_mail', summary: 'After-hours alert from monitoring', matched: [], skipped: ['vip-outage-triage'], skip_reasons: { 'vip-outage-triage': 'screen locked' }, ts: Date.now() / 1000 - 60 },
    { id: 'e5', source: 'outlook', type: 'new_mail', summary: 're: Outage on web01', matched: [], skipped: [], deduped: ['vip-outage-triage'], ts: Date.now() / 1000 - 30 },
  ],
  get_schedule_board: async () => [
    {
      card_id: 'sched-demo1', trigger: 'nightly-backup', source: 'schedule',
      event_type: 'runnable', summary: '/backup-db on db01',
      column: 'done', detail: { host: 'db01', runnable: 'backup-db', exit_code: 0, result: 'Backed up 3 databases (1.2 GB) in 42s.' },
      created_at: Date.now() / 1000 - 600,
      timeline: [
        { ts: Date.now() / 1000 - 600, text: 'running' },
        { ts: Date.now() / 1000 - 558, text: 'done (exit 0)' },
      ],
    },
    {
      card_id: 'sched-demo2', trigger: 'cert-check', source: 'schedule',
      event_type: 'runnable', summary: '/check-cert on web01',
      column: 'running', detail: { host: 'web01', runnable: 'check-cert' },
      created_at: Date.now() / 1000 - 5,
      timeline: [{ ts: Date.now() / 1000 - 5, text: 'running' }],
    },
  ],
  get_schedule_events: async () => [
    { id: 'se1', source: 'schedule', type: 'backup-db', summary: '/backup-db on db01', matched: ['nightly-backup'], skipped: [], ts: Date.now() / 1000 - 600 },
    { id: 'se2', source: 'schedule', type: 'nightly_report', summary: 'emit nightly_report', matched: [], skipped: ['weekend-report'], ts: Date.now() / 1000 - 200 },
  ],
  send_trigger_draft: async (entryId: string) => {
    console.log('mock: send_trigger_draft', entryId)
    return { ok: true, to: 'alice@bigcustomer.com', subject: 'Re: Outage on web01' }
  },
  get_room_state: async () => ({
    rooms: [
      { room: 'ops', host: 'web01', user: 'alice@web01', active: true, connected: true, queue: '' },
      { room: 'dba', host: 'db01', user: 'alice@db01', active: false, connected: false, queue: '' },
      {
        room: 'helpdesk/it-support', host: 'web01', user: 'alice@web01', active: true, connected: true, queue: 'it-support',
        threads: {
          'helpdesk/it-support/bob@corp': { state: 'open', assignee: null },
          'helpdesk/it-support/carol@corp': { state: 'assigned', assignee: 'alice@web01' },
        },
      },
    ],
  }),
  test_room_connection: async (host: string) => {
    console.log('mock: test_room_connection', host)
    return {
      ok: true,
      stage: 'ok' as const,
      host,
      room: 'helpdesk/it-support',
      queue: 'it-support',
      message: `Connected to the room on ${host} (port 8765, via exec). Queue it-support.`,
      hint: null,
    }
  },
  send_room_message: async (room: string, text: string) => {
    console.log('mock: send_room_message', room, text)
    // Echo into the transcript so the Rooms view is interactive in the browser
    // mock (the real server echoes a sender's own message back).
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent('runspec:room_message', {
        detail: { room, sender: 'alice@web01', text, id: 'mock-' + Date.now(), ts: Date.now() / 1000 },
      }))
    }, 80)
    return { ok: true }
  },

  // Interactive teams mock: the in-memory list drives the Teams tab in the browser
  // (the real bridge round-trips this through the helpdesk room).
  list_teams: async () => { setTimeout(emitMockTeams, 50); return { ok: true } },
  add_team: async (name: string) => {
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    if (slug && !MOCK_TEAMS.some(t => t.slug === slug)) MOCK_TEAMS = [...MOCK_TEAMS, { slug, name: name.trim() }]
    setTimeout(emitMockTeams, 50)
    return { ok: true }
  },
  remove_team: async (slug: string) => {
    if (slug !== 'default') MOCK_TEAMS = MOCK_TEAMS.filter(t => t.slug !== slug)
    setTimeout(emitMockTeams, 50)
    return { ok: true }
  },
}

let MOCK_TEAMS: { slug: string; name: string }[] = [
  { slug: 'default', name: 'Default' },
  { slug: 'it-support', name: 'IT Support' },
]
const emitMockTeams = () =>
  window.dispatchEvent(new CustomEvent('runspec:teams', { detail: { teams: MOCK_TEAMS } }))

// ── dev-only demo: a trigger fires a few seconds after load ───────────────────
// Exercises the Console's session chip row in `npm run dev`: a trigger card
// (triggered → running → done) streams a few tokens under its card id (== its
// session id == its chat id) alongside whatever chat is in flight, so a second
// chip appears, shows a spinner, gains an unread dot when another session is
// selected, and can be dismissed once done. Never runs under vitest.
if (import.meta.env.DEV && !import.meta.env.TEST && typeof window !== 'undefined') {
  const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))
  const demoCard = (over: Partial<TriggerCard>): TriggerCard => ({
    card_id: 'demo-live', trigger: 'vip-outage-triage', source: 'outlook', event_type: 'new_mail',
    summary: 'Outage on web02 — health check failing', column: 'triggered',
    detail: { mode: 'agent' }, created_at: Date.now() / 1000, ...over,
  })
  ;(async () => {
    await delay(4000)
    const id = 'demo-live'
    _dispatch('runspec:trigger_card', demoCard({}))
    await delay(600)
    _dispatch('runspec:trigger_card', demoCard({ column: 'running' }))
    const info: ChatSessionInfo = {
      id, kind: 'trigger', label: 'Trigger vip-outage-triage · new_mail', interactive: false,
      created_at: Date.now() / 1000, state: 'running', turn: id, turns: 0,
    }
    _sessions.set(id, info)
    _emitSession(info)
    for (const ch of 'Checking web02… health endpoint is returning 503. Restart queued for review.') {
      _dispatch('runspec:token', { id, token: ch })
      await delay(25)
    }
    _dispatch('runspec:run_end', { id, session: id, exit_code: 0, duration_ms: 2200, stopped: false })
    _dispatch('runspec:chat_usage', { id, session: id, input_tokens: 980, output_tokens: 60, cache_read_tokens: 0, cache_creation_tokens: 0 })
    _dispatch('runspec:trigger_card', demoCard({ column: 'done', detail: { mode: 'agent', result: 'Restart queued for review.' } }))
    _patchSession(id, { state: 'done', turn: '', turns: 1 })
  })()
}
