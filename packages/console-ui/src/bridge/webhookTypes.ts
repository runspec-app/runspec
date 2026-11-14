// Webhooks a host's runspec-scheduler receives, as the console follows them
// (runspec_console/bridge_webhooks.py; console 0.438.0).

/** One delivery (`hooks follow` / `hooks deliveries`). Times are ISO strings. */
export interface WebhookDelivery {
  id: number
  hook: string
  received: string | null
  source: string
  state: string
  fields: Record<string, unknown>
  rule: string
  runnable: string
  command: string[]
  args: Record<string, unknown>
  autonomy: string
  reason: string
  count?: number
  dup_of?: number | null
  expires?: string | null
  decided_by?: string
  decided?: string | null
  note?: string
  started?: string | null
  finished?: string | null
  exit_code?: number | null
  emailed?: string | null
  mail?: string
  item?: number | null
  request?: string
  content_type?: string
  body?: string
  body_truncated?: boolean
}

export interface WebhookMute {
  who: string
  hook: string
  until: string
  reason: string
}

export interface WebhookRule {
  name: string
  action: string
  run: string
  command: string[]
  match: Record<string, string>
}

/** One hook as `hooks list` shows it (never a hash or a secret). */
export interface WebhookHook {
  name: string
  enabled: boolean
  description: string
  source: 'console' | 'deployed' | string
  file: string
  managed_by: string
  auth: string[]
  token_header: string
  allow: string[]
  fields: Record<string, string>
  split: string
  rules: WebhookRule[]
  dedup: string
  require_approval: boolean
  transform: string
  overridden?: boolean
  raw?: Record<string, unknown>
  toml?: string
  url?: string | null
}

/** `[hooks]` as the scheduler reports it. */
export interface WebhookSettings {
  enabled?: boolean
  port?: number
  bind?: string
  base_path?: string
  cert?: string
  key?: string
  tls?: boolean
  max_body?: number
  keep?: string | number
  max_connections?: number
  rate_per_minute?: number
  source_rate_per_minute?: number
  trusted_proxies?: string[]
  approve_within?: string | number
  expect_claim_within?: string | number
  max_runs?: number
  problem?: string | null
  [key: string]: unknown
}

export interface WebhookSmtp {
  relay?: string
  port?: number
  from?: string
  to?: string[]
  starttls?: boolean
  timeout?: number
}

/** One followed host + venv. */
export interface WebhookVenv {
  key: string
  host: string
  venv: string
  follows: string[]
  connected: boolean
  error: string
  /** Why this venv takes no webhooks at all (no runspec-scheduler, one older
   *  than 0.11.0, root-owned): not a fault. '' / absent when it can. */
  unavailable?: string
  paused: string
  who: string
  settings: WebhookSettings
  hooks: { name: string; enabled?: boolean; description?: string }[]
  mutes: WebhookMute[]
  open: WebhookDelivery[]
  dismissed: number
  recent: WebhookDelivery[]
  away: string
}

export interface WebhooksStatus {
  configured: boolean
  away: string
  venvs: WebhookVenv[]
  /** host name → the hooks its entry follows (`*` = all). */
  follows: Record<string, string[]>
}

export interface WebhookTarget { host: string; venv: string }
export interface WebhookResult { ok: boolean; error?: string }

export interface WebhookHooksList extends WebhookResult {
  host?: string
  config?: string
  settings?: WebhookSettings
  smtp?: WebhookSmtp
  hooks?: WebhookHook[]
  errors?: string[]
  pending?: WebhookDelivery[]
  mutes?: WebhookMute[]
}

/** What a dry run of one item would do. */
export interface WebhookTestItem {
  state: string
  fields: Record<string, unknown>
  rule?: string
  runnable?: string
  command?: string[]
  args?: Record<string, unknown>
  autonomy?: string
  reason?: string
}

export interface WebhookTestResult extends WebhookResult {
  source?: string
  items: WebhookTestItem[]
}

export interface WebhookSaveResult extends WebhookResult {
  hook?: string
  url?: string | null
  replaced?: boolean
  /** A made token / password, shown once. */
  secrets?: { token?: string; username?: string; password?: string }
  note?: string
}

export interface WebhookEditPayload extends WebhookTarget {
  /** TOML: a bare table or one `[[hook]]`. */
  text?: string
  hook?: Record<string, unknown>
}
