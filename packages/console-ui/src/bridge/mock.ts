import type { BridgeApi, Host, JumpHost, Runnable, HistoryRecord, Schedule, InFlightRecord, TestResult, AnalyticsData, AnalyticsBucket } from './index'

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
      { name: 'source', type: 'path', required: true, description: 'Directory to back up' },
      { name: 'dest', type: 'str', required: false, default: '$BACKUP_DEST', description: 'Destination path' },
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
      initiatedBy: r.initiatedBy, autonomy: r.autonomy,
      total: 0, success: 0, failure: 0, durationMsSum: 0,
      events: { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 },
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
      operators: [...operators].sort(), initiatedBy: ['user', 'llm'],
    },
    partial: false, errors: [],
  }
}

let MOCK_SCHEDULES: Schedule[] = [
  { id: 'rs-backup-daily', runnable: 'backup', host: 'local', schedule: '0 2 * * *', args: { db: 'postgres-main' } },
  { id: 'rs-log-rotate-weekly', runnable: 'log-rotate', host: 'prod-1', schedule: '0 3 * * 0', args: { keep: 7 } },
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

// request_id → resolver, for the simulated autonomy-confirm round-trip.
const _pendingConfirms = new Map<string, (approved: boolean) => void>()

// The chat turn currently streaming, so cancel_chat can stop it (including
// while its Approve/Deny prompt is up — the case the real bridge handles).
let _activeChat: { id: string; cancelled: boolean } | null = null

export const mockApi: BridgeApi = {
  get_hosts: async () => MOCK_HOSTS,

  get_runnables: async (_host) => MOCK_RUNNABLES,

  get_history: async (_host, _runnable) => MOCK_HISTORY,

  get_schedules: async () => MOCK_SCHEDULES,

  config_dir: async () => 'C:/Users/jason/AppData/Roaming/runspec-console',
  list_providers: async () => [
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'openai', label: 'OpenAI' },
    { value: 'bedrock', label: 'AWS Bedrock' },
    { value: 'langserve', label: 'LangServe gateway' },
  ],
  get_config: async () => ({
    ssh: { user: 'jason', identityFile: 'C:/Users/jason/AppData/Roaming/runspec-console/runspec_ed25519', key_created_at: MOCK_KEY_CREATED_AT },
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

  create_schedule: async (data) => {
    const s = data as unknown as Schedule
    MOCK_SCHEDULES = [...MOCK_SCHEDULES, { ...s, id: s.id || `rs-${s.runnable}-${Date.now()}` }]
  },

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

  minimize_window: async () => { console.log('[mock] minimize_window') },
  toggle_maximize_window: async () => { console.log('[mock] toggle_maximize_window') },
  close_window: async () => { console.log('[mock] close_window') },
  resize_window: async (w: number, h: number) => { console.log('[mock] resize_window', w, h) },
  move_window: async (x: number, y: number) => { console.log('[mock] move_window', x, y) },

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
    await new Promise(r => setTimeout(r, 1800))
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

  // Mirrors the Python bridge's gate round-trip: send_chat parks a resolver
  // keyed by request_id; resolve_tool_confirmation (called by the UI's
  // Approve/Deny buttons) wakes it. Lets the browser mock exercise the real
  // autonomy modal instead of pretending every tool is autonomous.
  resolve_tool_confirmation: async (requestId, approved) => {
    console.log('[mock] resolve_tool_confirmation', requestId, approved)
    const resolve = _pendingConfirms.get(requestId)
    if (resolve) { _pendingConfirms.delete(requestId); resolve(approved) }
  },

  clear_chat: async () => {},
  cancel_chat: async (chatId: string) => {
    if (_activeChat && (!chatId || _activeChat.id === chatId)) {
      _activeChat.cancelled = true
      // Wake a pending confirm prompt for this turn, mirroring the real bridge.
      const resolve = _pendingConfirms.get(`mock-${_activeChat.id}`)
      if (resolve) { _pendingConfirms.delete(`mock-${_activeChat.id}`); resolve(false) }
    }
  },

  send_chat: async (message, _invocationId) => {
    const id = `chat-${++invocationCounter}`
    _activeChat = { id, cancelled: false }
    const dispatch = (event: string, detail: unknown) =>
      window.dispatchEvent(new CustomEvent(event, { detail }))
    const delay = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

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
      if (_activeChat?.cancelled) {
        dispatch('runspec:tool_end', {
          id, tool_name,
          output: "⏹ 'flush-dns' was not run — you stopped the assistant before approving it.",
        })
        dispatch('runspec:token', { id, token: '\n⏹ Stopped.' })
        dispatch('runspec:run_end', { id, exit_code: 0, duration_ms: 800 })
        _activeChat = null
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

      dispatch('runspec:run_end', { id, exit_code: approved ? 0 : 1, duration_ms: 1500 })
      dispatch('runspec:chat_usage', {
        id, input_tokens: 1247, output_tokens: 342,
        cache_read_tokens: 8912, cache_creation_tokens: 0,
      })
      _activeChat = null
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

}
