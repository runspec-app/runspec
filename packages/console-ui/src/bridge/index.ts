import { recordCallStart, recordCallEnd, recordCallError } from '../devbus'

export interface ArgDef {
  name: string
  type: string
  required: boolean
  description?: string
  default?: unknown
  options?: string[]      // populated when type === 'choice'
  range?: [number, number]
  multiple?: boolean
  short?: string          // short flag alias e.g. "-v"
  env?: string | string[] // env var aliases checked before spec default
  deprecated?: string
  autonomy?: string       // per-arg autonomy override
  position?: number       // 1-based positional index
  ui?: string             // form control hint
  hint?: string           // placeholder / hint text for the form field
  pattern?: string        // regex the value must fully match (str args)
  min_length?: number     // minimum string length (str args)
  max_length?: number     // maximum string length (str args)
  delimiter?: string      // splits a single value into a list
  meta?: Record<string, unknown>  // free-form developer metadata, passed through
  inherited?: boolean     // arg inherited from an ancestor (global / parent-command) — set by discovery
}

export interface Runnable {
  name: string
  group: string   // venv directory name — the execution environment identity
  host: string
  description?: string
  args: ArgDef[]
  commands?: Record<string, Runnable>  // subcommands, max two levels deep
  requireCommand?: boolean  // true when a command must be chosen at this level (core enforces require-command)
  autonomy?: string   // effective autonomy — 'confirm' | 'autonomous' | 'supervised' | 'manual'
  runAs?: string      // OS user the runnable executes as on the host
  rawSpec?: Record<string, unknown>  // full raw spec dict for the holistic Specs view
}

// A user-invocable, parameterized runbook (the `$` runbooks menu + Runbooks settings
// tab). Its `args` are a real runspec `[runbook.args]` table — parsed server-side to
// the same ArgDef shape a runnable form uses, so the run modal reuses ArgInput.
// One step of a runbook: free prose the agent follows (with {{name}} placeholders).
// Only text + optional title — a step never carries a machine condition/goto field
// (that would cross the "no workflow engine" cliff); skip/branch logic lives in the
// prose and is the agent's judgement.
export interface RunbookStep {
  title?: string
  text: string
}

export interface Runbook {
  id: string
  title: string
  symptom: string          // "when to use" — shown in the menu + editor
  steps: RunbookStep[]     // ordered steps; rendered to numbered prose at run time
  tags: string[]
  model?: string           // the runbook's own model for its agent turn ("" = the [llm] model)
  updated?: string
  args: ArgDef[]           // parsed param defs — the run modal renders these
  argsRaw?: Record<string, unknown>  // stored [runbook.args] table (faithful editor round-trip)
  argsError?: string       // set when a malformed args table failed to parse
}

// A saved run — a deterministic preset (runnable + target + fixed args + open
// params) stored in synced runspec_saved_runs.toml. The Forms tab lists them,
// the run modal renders only the open params, and schedules can fire them.
export interface SavedRun {
  id: string
  title: string
  description: string
  runnable: string
  command: string[]        // optional subcommand path
  // Exactly one of host / group / hosts / selected_hosts is set.
  host: string
  group: string
  hosts: string[]          // failover candidates — first connected wins
  selected_hosts: string[] // explicit fan-out list
  args: Record<string, unknown>       // fixed args, baked in
  credential_ids: string[]
  target?: string          // human summary of the targeting, computed bridge-side
  params: ArgDef[]         // parsed open-param defs — the run modal renders these
  paramsRaw?: Record<string, unknown> // stored params table (faithful editor round-trip)
  argsError?: string       // set when the entry failed to validate/parse
}

// A named sound in the runspec_sounds.toml registry — an operator-added audio
// file with a stable id, referenced by "sound:<id>" everywhere the console plays
// cues. `exists` is false for a synced entry whose file isn't on this machine.
export interface NamedSound {
  id: string
  label: string
  file: string
  exists?: boolean
}

export interface DirEntry {
  name: string
  type: 'directory' | 'file'
  size: number
  mtime: number   // epoch seconds
}

export interface DirListing {
  cwd: string
  parent: string | null   // null at a filesystem / drive root
  entries: DirEntry[]
  truncated: boolean
  error?: string
}

// runspec:transfer_progress event detail — one per throttled progress tick (or a
// per-file error when `error` is set).
export interface TransferProgress {
  id: string
  file: string
  index: number
  count: number
  transferred: number
  total: number
  // Set when a source-side filter is running: output size is unknown up front, so
  // `transferred` is bytes written (a counter) and `total` is only the input size.
  indeterminate?: boolean
  error?: string
  host?: string   // set for fan-out (which target host this tick is for)
}

export interface TransferHostResult {
  host: string
  ok: boolean
  error?: string | null
}

// A relay source: a configured host, or an ad-hoc host reached via a jump. The
// password (ad-hoc only) is used for the single connection and never stored.
export interface RelaySource {
  kind: 'host' | 'adhoc'
  host: string
  user?: string
  port?: number
  jump?: string       // configured host to tunnel through (ad-hoc)
  password?: string   // optional, used only if key/agent auth fails
  credential_id?: string  // optional, a saved credential whose secret is the password
}

// An ad-hoc transfer endpoint: an SSH host not in the fleet, reached via a
// configured jump — the same shape a RelaySource ad-hoc source uses. A transfer
// side is 'local', a configured host name, or this descriptor.
export interface AdHocEndpoint {
  kind: 'adhoc'
  host: string
  jump: string
  user?: string
  port?: number
  password?: string
  credential_id?: string
}
export type TransferEndpoint = string | AdHocEndpoint

// A named, reusable source-side stream filter (Settings → Filters). The pipeline
// runs on the file's host (file fed on stdin), so only matching output crosses
// the network. `{{arg}}` in the pipeline is a runtime value the dialog prompts
// for (labelled by `param_label`); `description` is free-text metadata.
export interface Filter {
  name: string
  pipeline: string
  description?: string
  param_label?: string
}

// A named credential (Settings → Credentials). Metadata only on the wire — the
// secret value lives in the OS keyring and is never read back; `has_secret` says
// whether one is stored. `secret` is write-only: send it to save/update a value,
// omit/blank it to leave the stored value untouched. `username_arg`/`secret_arg`
// bind the values to a runnable's flags; the scopes (fnmatch patterns) limit
// where the credential may be applied.
export interface Credential {
  id: string
  label: string
  kind: 'userpass' | 'password' | 'token' | 'ssh-key'
  username?: string
  username_arg?: string
  secret_arg?: string
  private_key?: string   // ssh-key: path to the private key file
  public_key?: string    // ssh-key: path to the public key file (optional)
  scope_hosts?: string[]
  scope_runnables?: string[]
  has_secret?: boolean   // ssh-key: a key passphrase is stored
  secret?: string   // write-only; never returned by list_credentials
}

export interface CredentialBackend {
  available: boolean
  backend: string
}

// Settings → MCP. An external MCP server the chat agent connects to. `credential`
// is a Credentials-tab id (keychain-backed; only the id is stored). `proxy` is a
// per-server HTTP CONNECT proxy (servers can live on different networks).
export interface McpServer {
  name: string
  transport: 'stdio' | 'http' | 'sse'
  command?: string
  args?: string[]
  env?: Record<string, string>
  cwd?: string
  url?: string
  headers?: Record<string, string>
  enabled?: boolean
  autonomy?: '' | 'autonomous' | 'confirm' | 'manual'
  credential?: string
  credential_header?: string
  // Extra secret headers, each from a keychain-backed credential (for a server
  // needing more than the single `credential` — e.g. a Jira PAT *and* a
  // Confluence PAT). Only the header name + credential id are stored; the secret
  // is resolved at connect time and never written to config.
  header_credentials?: McpHeaderCredential[]
  proxy?: string
  // Per-server overrides of the section's default timeouts (seconds). A positive
  // value overrides just this server's deadline; unset / 0 ⇒ inherit the [mcp]
  // call_timeout / connect_timeout that apply to all servers.
  call_timeout?: number | null
  connect_timeout?: number | null
}
export interface McpHeaderCredential {
  header: string
  credential: string
}
export interface McpConfig {
  enabled: boolean
  call_timeout: number
  connect_timeout: number
  servers: McpServer[]
}
export interface McpServerStatus {
  name: string
  transport: string
  connected: boolean
  tool_count: number
  error: string
  credential: string
  proxy: string
}
export interface McpStatus {
  available: boolean
  reason?: string
  started?: boolean
  tool_count: number
  servers: McpServerStatus[]
}
export interface McpCredentialOption {
  id: string
  label: string
  kind: string
}
// Result of mcp_install_venv (install a stdio MCP server into a dedicated venv).
export interface McpInstallResult {
  ok: boolean
  error?: string
  server?: string
  command?: string
  venv?: string
  status?: McpStatus
  scripts?: string[]  // when the launcher wasn't found: the venv's console scripts
  output?: string
}
// The console's *own* MCP server ([mcp_server]) — an external coding agent
// (Claude Code) connects to it. Off by default; localhost-only. (Named Own* to
// avoid clashing with the client-side McpServer* interfaces above.)
export interface OwnMcpServerConfig {
  enabled: boolean
  host: string
  port: number
  token: string | null
  default_autonomy: string | null
  confirm_wait: number
  tool_style: string        // 'leaf' (serve-identical) | 'runnable' (one tool per runnable + command arg)
  expose_builtins: boolean  // also publish the console's console-specific built-in agent tools
}
export interface OwnMcpServerStatus {
  running: boolean
  error: string
  host: string
  port: number
  url: string
  auth: boolean
  tools: number
  builtins?: number
  tool_style?: string
  // Tools dropped at the last tools/list because their inputSchema was invalid
  // draft 2020-12 (so the Anthropic API would 400 the whole list). Surfaced so a
  // drop is visible in the UI, not just in runspec-console.log.
  invalid_tools?: { name: string; reason: string }[]
}

// Settings → Config Sync. Where to pull the console's config files from (a git
// repo, at a tag/ref), which credential authenticates, and the last successful
// pull. The secret is never here — it lives in the keyring under credential_id.
// `engine` is the active git engine (null = the `git-sync` extra isn't installed);
// `files_default` is the file set the UI offers when none are chosen. `preserve`
// is the user's keep-local paths ("<file>:<dotted.path>"); `preserve_default` is
// the built-in set always applied on top (mailbox.folder, *.room_user).
export interface SyncConfig {
  url: string
  ref: string
  credential_id: string
  subpath: string
  layers: string[]
  ca_bundle: string
  tls_verify: boolean
  files: string[]
  preserve: string[]
  backups_keep: number
  last_ref: string
  last_commit: string
  last_synced: string
  engine: string | null
  files_default: string[]
  preserve_default: string[]
}

export interface SyncPullResult {
  ok: boolean
  ref?: string
  commit?: string
  written?: string[]
  skipped?: string[]
  backups?: string[]
  pruned?: string[]
  error?: string
}

// Settings → Config Sync → Config seed. Read-only status of the baseline config
// bundled inside the deployment package. `stamp`/`applied` map distribution name
// → version; `pending` is true when what's bundled differs from what's applied
// (a re-assert will run on next launch, or via the Re-apply button now).
// One seeded leaf key whose current *local* value differs from the seed's — what
// a re-assert (or the next package upgrade) would silently revert. `path` is the
// dotted key (mapping files) or `<id>.<field>` (collection files); `preserved` is
// true when a `preserve` rule pins it local, so a re-assert would NOT override it.
export interface SeedOverride {
  file: string
  path: string
  seed: string
  local: string
  preserved: boolean
}

export interface SeedStatus {
  available: boolean
  stamp: Record<string, string>
  applied: Record<string, string>
  last_applied: string
  pending: boolean
  overrides?: SeedOverride[]
  // The last seed-apply error (a malformed bundled file aborts the whole seed);
  // empty when the last apply succeeded. Shown in the Config seed panel.
  error?: string
}

// Validated white-label theme (`[console.theme]`). Every field optional; an
// empty object means no theme (the default runspec look). `accent_color` is a
// hex colour → Ant `colorPrimary`; `watermark_logo` is an http(s)/data:image URL.
export interface BrandTheme {
  accent_color?: string
  brand_name?: string
  watermark_text?: string
  watermark_logo?: string
  watermark_style?: 'tiled' | 'banner' | 'center'  // default 'tiled'
}

// The first-use AI-use disclaimer's state for the gate modal (see bridge.disclaimer).
export interface Disclaimer {
  enabled: boolean
  needs_ack: boolean
  title: string
  text: string           // markdown
  acknowledge_label: string
  version: string
}

export interface SeedApplyResult {
  ok: boolean
  applied?: boolean
  reason?: string
  stamp?: Record<string, string>
  written?: string[]
  skipped?: string[]
  backups?: string[]
  pruned?: string[]
  error?: string
}

// Settings → Config Sync → Runbook docs. Where the deterministic runbook wiki is
// written (blank = the `default`, app_dir()/docs).
export interface DocsConfig {
  dir: string
  default: string
}

export interface DocsGenResult {
  ok: boolean
  dir?: string
  written?: string[]
  error?: string
}

// The single [mailbox] the Outlook source monitors (Settings → Triggers).
// `address` blank = the default account; `folder` blank = the Inbox; `label` is
// an optional display name. null = no mailbox configured (monitoring off).
// See docs/design/teams-and-email-monitoring.md.
export interface Mailbox {
  address: string
  // The folders to monitor under this mailbox (each a /-separated path). Empty =
  // the Inbox only. The legacy single `folder` is still read as a one-element
  // list; `folder` is kept as folders[0] for single-folder callers.
  folders?: string[]
  folder?: string
  label: string
  // Mailbox-wide default for suppressing replies (once per thread). A trigger
  // inherits this unless it sets its own override. Default false.
  once_per_thread?: boolean
}

// An additional watch-only mailbox/folder the Outlook source monitors, on top of
// the primary [mailbox] — e.g. the operator's own Inbox alongside a shared
// helpdesk mailbox. Same shape minus once_per_thread: these feed new_mail
// triggers only and carry no room/verify-from role, so reply suppression is left
// to each trigger's own once_per_thread. See read_watches() in config.py.
export interface Watch {
  address: string
  folders?: string[]
  folder?: string
  label: string
}

// A customer team (department) a visitor picks at sign-up. Lives in the helpdesk
// room DB, managed from the console (Settings → Teams). `slug` is the stable id.
export interface Team {
  slug: string
  name: string
}

// runspec:teams event detail — the room's reply to list/add/remove_team.
export interface TeamsEvent {
  teams: Team[]
}

// runspec:transfer_end event detail — fired once when a transfer finishes.
export interface TransferEnd {
  id: string
  ok: boolean
  files: number   // files completed
  total: number
  error?: string | null
  hosts?: TransferHostResult[]   // per-host results for a fan-out
}

export interface Host {
  name: string
  connected: boolean
  runnableCount: number
  groups: string[]
  role?: 'primary' | 'secondary'  // undefined = local machine, always included
  group?: string                  // sidebar display group label, joined from JumpHost config
  profiles?: string[]             // host-profile (working-set) tags this host belongs to
}

// An entry in the canonical host-group registry (runspec_groups.toml). The
// registry pins the legal group NAMES (shared via Config Sync); membership lives
// on each host's `group` field.
export interface HostGroup {
  name: string
  description?: string
}

// An entry in the canonical host-profile registry (runspec_profiles.toml). A
// profile is a named host working-set (which remotes are in scope / connected).
// The registry pins the legal profile NAMES (shared via Config Sync); membership
// lives on each host's `profiles` array; the *active* profile is machine-local.
// See docs/design/host-profiles.md.
export interface HostProfile {
  name: string
  description?: string
}

// A runnable designated as self-service (Settings → Self-Service,
// runspec_self_service.toml). The registry names which discovered runnables are
// offered to visitors in the helpdesk room — replacing the runspec.toml
// `discoverable = ["self-service"]` marking. Identity key is `name` (synced).
export interface SelfServiceEntry {
  name: string
  description?: string
}

// A git repo the agent may read and locally edit (Settings → Repos,
// runspec_repos.toml). The registry is the agent's allowlist — only repos listed
// here are reachable by the repo_* tools. `checked_out`/`commit` are status fields
// returned by get_repos (the local working-copy state); omit them on save.
export interface Repo {
  name: string
  url: string
  ref: string
  credential_id?: string
  description?: string
  path?: string          // machine-local custom checkout dir ("" / absent = default AppData location)
  checkout_dir?: string  // effective absolute checkout dir (read-only; from get_repos)
  checked_out?: boolean
  commit?: string
}

export interface RepoSyncResult {
  ok: boolean
  action?: string   // "cloned" | "updated"
  commit?: string   // short HEAD sha after the sync
  error?: string
}

export interface JumpHost {
  name: string        // identifier — displayed and used as SSH host alias
  hostname: string
  runspec_paths?: string[]  // paths to runspec binaries (one per venv) on the remote host
  user?: string       // falls back to SSH default in settings
  port?: number       // falls back to SSH default (22)
  identityFile?: string
  group?: string      // sidebar display group label (e.g. "Production", "Staging")
  jump?: string       // reach this host via system-ssh ProxyJump (-J) so ~/.ssh/config legacy algorithms apply (CentOS 6 ssh-rsa/ssh-dss)
  profiles?: string[] // host-profile (working-set) tags — which profiles load this host
  room?: string       // chat room hosted on this host (runspec-room) — opt-in
  room_port?: number  // the room server's localhost port on this host
  room_user?: string  // display name in the room (defaults to the SSH login)
  room_via?: string   // "exec" to join over an SSH exec channel (no port-forward)
  room_exec_command?: string  // exec-mode relay command on the host (e.g. socat / console-room connect)
  room_queue?: string  // helpdesk queue this console staffs on the host's room server (agent mode)
  room_catalog?: string[]  // optional per-queue catalog allowlist (bare self-service runnable names)
}

// The local machine's venv auto-discovery config (Settings → Local venvs). The
// `globs` convention lives in config.toml so it rides Config Sync / the config
// seed (one versioned glob works fleet-wide); `resolved` is what those globs
// actually match on THIS machine (machine-local), with the console's own venv
// (`own_venv`) always first.
export interface LocalVenvConfig {
  globs: string[]
  own_venv: string
  resolved: { path: string; venv_name: string }[]
}

// The [tool_visibility] hide lists (Settings → Agent tools). Glob patterns
// matched against a runnable name; a "group:"/"host:" prefix hides a whole
// group/host. hidden_from_agent drops runnables from the agent toolset only;
// hidden_from_all also drops them from the Forms tab + catalogs. Curation, not
// a safety block — hidden runnables stay directly runnable.
export interface ToolVisibility {
  hidden_from_agent: string[]
  hidden_from_all: string[]
}

export interface HistoryLogLine {
  ts: string
  level: string
  message: string
}

export interface HistoryRecord {
  id: string
  runId?: string     // raw run_id (id is a hash of run_id+runnable) — drives the "View full log" drill
  runnable: string
  group: string      // venv the run's logs live in — needed to resolve logs_view; "" when unknown (some remote)
  host: string
  operator: string   // who triggered the run (console user or "Scheduled Task")
  runAs: string      // OS user the runnable executed as on the host (from run_as in spec)
  exitCode: number
  durationMs: number
  ts: string
  args: Record<string, unknown>      // arguments used for this invocation (from run_summary)
  argSources?: Record<string, string> // provenance of each arg: "cli" | "env" | "runspec_env" | "spec_default" | "not_set"
  commandPath?: string[]             // subcommand path ([] for a plain runnable) — drives rerun reconstruction
  logLines: HistoryLogLine[]         // log records belonging to this invocation
  initiatedBy?: 'llm' | 'user'      // 'llm' when triggered by the agentic chat loop
  trigger?: string                   // event source that caused the run ("schedule"/…), "" = interactive/manual
  runbook?: string                     // saved runbook that drove the run (RUNSPEC_RUNBOOK), "" = not a runbook run
  kind?: 'runnable' | 'agent' | 'tool' // 'runnable' run, whole 'agent' turn, or a single built-in 'tool' call
  // §8.1 log-prune provenance (from the runs index, runspec 0.58.0): when/by-whom
  // this run's log file was pruned. "" = log present. A DB row outlives its log
  // file on purpose, so a set logPrunedAt turns a bare "no log lines" on expand
  // into "log pruned by X on Y — record retained".
  logPrunedAt?: string
  logPrunedBy?: string
  // From the runs index only: the row's outcome ('skipped' = a `runspec exec` run
  // that never started — lock held / already running; exitCode is 0 but it isn't
  // a success) and its exec label ("sched:<id>" for a shared cron schedule).
  status?: string
  label?: string
}

// One in-flight run from the per-venv runs index — the History tab's live
// "Running now" strip (get_running_runs). Fleet-wide and index-sourced: every
// connected host's running rows, whoever launched them (this console, a bare CLI
// run, the scheduler, another console), liveness-filtered so a crashed row never
// shows. Distinct from InFlightRecord, which is only what this console launched
// (and carries a Cancel). A running run has no exit/duration yet, so it carries
// startTs + pid and the strip ticks the elapsed time from startTs client-side.
export interface RunningRun {
  id: string
  runId: string
  runnable: string
  group: string      // venv the run's index lives in
  host: string
  operator: string   // who launched it ("" when unknown)
  runAs: string      // OS user it executes as
  startTs: string    // ISO start — the strip ticks elapsed from this
  pid: number
  args: Record<string, unknown>
  commandPath?: string[]             // subcommand path ([] for a plain runnable)
  initiatedBy?: 'llm' | 'user'      // 'llm' when launched by the agentic loop
  trigger?: string                   // event source that caused the run ("schedule"/…), "" = interactive
  runbook?: string                   // saved runbook that drove the run, "" otherwise
  autonomy?: string
}

// A schedule (Schedules tab). When it fires it either emits a matchable event
// (action 'event') that source="schedule" triggers pick up like mail, runs a
// runnable directly (action 'runnable'), runs an agent turn on a fixed prompt
// (action 'prompt'), or runs a saved runbook as an agent turn (action 'runbook').
// Timing lives in `when` (cron or every).
export interface ScheduleWhen {
  cron?: string
  every?: string
  timezone?: string
}
export interface Schedule {
  id: string
  enabled?: boolean
  action: 'runnable' | 'event' | 'prompt' | 'runbook' | 'saved_run' | 'eval'
  when: ScheduleWhen
  // action === 'runnable' — exactly one of host / group / hosts / selected_hosts.
  // group fans the run out across every member of the host group; hosts is a
  // candidate list and the run goes to the first connected one (failover);
  // selected_hosts is an explicit list and the run fans out to every host in it.
  host?: string
  group?: string
  hosts?: string[]
  selected_hosts?: string[]
  runnable?: string
  args?: Record<string, unknown>
  // action === 'runnable' — the subcommand path to descend into (empty/absent =
  // the root command). Mirrors a saved run's `command`.
  command?: string[]
  // action === 'runnable' — ids of console credentials to apply at fire time.
  // Only ids are stored (never secrets); the secret is injected from the keychain
  // when the schedule fires, exactly like the Forms tab's explicit selection.
  credential_ids?: string[]
  // action === 'event'
  event?: string
  fields?: Record<string, string>
  // action === 'prompt' — the instruction the agent turn runs on.
  prompt?: string
  // action === 'runbook' — the saved runbook id + values for its {{placeholders}}.
  runbook?: string
  // Shared with action === 'saved_run' (there it fills the preset's open params).
  values?: Record<string, unknown>
  // action === 'saved_run' — the saved-run (preset) id; targeting lives on the preset.
  saved_run?: string
  // action === 'eval' — the eval fixture (suite name) to run against the live
  // agent, plus the run options (a dry run — never executes a runnable).
  eval_fixture?: string
  eval_samples?: number
  eval_validate?: boolean
  eval_telemetry?: boolean
  // Optional attention cue played when the schedule fires (a named sound id or a
  // built-in cue). Empty/absent = no sound.
  sound?: string
  // action "prompt" / "runbook": optional model for the agent turn (blank = [llm] model).
  model?: string
  nextRun?: string
}

export interface ConversationMeta {
  id: string
  title: string
  created: string
  updated: string
  turn_count: number
  preview: string
}

// ── Improve (effectiveness + problem report) ──────────────────────────────────
export interface ImproveIncident {
  conversation_id: string
  turn_index: number
  category: string
  evidence: string
  tool?: string
  runnable?: string
  bounce_kind?: string
  run_id?: string
  host?: string
  ts?: string
}

export interface ImproveRecommendation {
  category: string
  target: string
  why: string
  fix: string
  confidence: string
}

// A saved Improve report. `synthesis` (+ `markdown`/`learnings_toml` regenerated
// with it) is attached only after the optional LLM-judge pass runs.
export interface ImproveReport {
  id: string
  ts: string
  kind: 'evaluation' | 'problem_report'
  product_name: string
  developer_emails: string[]
  comment: string
  window_days: number
  since_iso: string
  metrics: Record<string, number>
  scores: Record<string, number>
  bounce_kinds: Record<string, number>
  autonomy_by_tool: Record<string, { denied: number; edited: number }>
  runnables: Record<string, unknown>[]
  incidents: ImproveIncident[]
  incident_total: number
  incident_shown: number
  markdown?: string
  learnings_toml?: string
  synthesis?: { summary?: string; correlation?: string; recommendations?: ImproveRecommendation[] }
  synthesis_error?: string
}

export interface ImproveReportMeta {
  id: string
  ts: string
  kind: 'evaluation' | 'problem_report'
  comment: string
  product_name: string
  has_synthesis: boolean
}

// ── Evals (tool-selection eval suites; mirrors bridge_evals.py) ──────────────
export interface EvalToolInvocation {
  name: string
  input: Record<string, unknown>
}

export interface EvalCaseResult {
  case_id: string
  passed: boolean
  category: string
  reason: string
  terminal: EvalToolInvocation | null
  target: string
  command: string    // observed subcommand/verb (space-joined for a nested path)
  autonomy: string   // level the call would gate at: autonomous / confirm / manual
  host: string       // host (or host GROUP) the call targeted
  final_text: string
  samples: number
  pass_count: number
  pass_rate: number
}

export interface EvalSummary {
  total: number
  passed: number
  failed: number
  pass_rate: number
  by_category: Record<string, number>
}

export interface EvalReport {
  id: string
  generated_at: string
  provider: string
  model: string
  tool_mode: string
  samples: number
  summary: EvalSummary
  cases: EvalCaseResult[]
}

export interface EvalReportMeta {
  id: string
  generated_at: string
  model: string
  tool_mode: string
  summary: EvalSummary
}

// run_evals returns { ok, reason } on failure, or { ok: true, ...EvalReport }.
export type EvalRunResult = ({ ok: true } & EvalReport) | { ok: false; reason: string }

export interface EvalFixtureMeta {
  id: string
  cases: number
}

export interface EvalFixtureSaveResult {
  ok: boolean
  reason?: string
  cases?: number
}

export interface ConversationTurn {
  // Absent ⇒ a chat exchange; 'operator_run' is an operator-initiated manual
  // runnable run, rendered for the operator but never replayed to the agent.
  kind?: 'chat' | 'operator_run'
  user: string
  assistant: string
  tools: {
    name: string
    input: Record<string, unknown>
    output: string
    // Operator's confirm-gate decision (when the call was gated) + the agent's
    // original args if the operator edited them. Operator-only annotations.
    operator_decision?: 'approved' | 'edited' | 'denied'
    proposed_input?: Record<string, unknown>
  }[]
  usage?: Record<string, number> | null
  model?: string
  ts?: string
  // operator_run fields
  runnable?: string
  host?: string
  args?: Record<string, unknown>
  output?: string
  exit_code?: number
  duration_ms?: number
  operator?: string
}

export interface ChatConversation {
  id: string
  title: string
  created?: string
  updated?: string
  turn_count?: number
  provider?: string
  model?: string
  turns: ConversationTurn[]
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
  trigger?: string                   // event source that caused the run ("schedule"/…), "" = interactive/manual
  kind?: string                      // "builtin_tool" for a built-in agent tool (repo_*, outlook_*, …); absent for a runnable run
  tool?: string                      // the raw built-in tool name (when kind === "builtin_tool")
  chatId?: string                    // the chat turn this built-in tool belongs to
}

export interface TodayByRunnable {
  runnable: string
  host: string
  count: number
  lastExitCode: number
  lastRun: string
}

export interface TodayUpcoming {
  scheduleId: string
  runnable: string
  host: string
  nextRun: string
}

export interface TodaySummary {
  date: string           // YYYY-MM-DD
  generatedAt: string    // ISO timestamp of last digest run
  totalRuns: number
  successCount: number
  failureCount: number
  byRunnable: TodayByRunnable[]
  upcomingToday: TodayUpcoming[]
}

export interface AnalyticsDaily {
  date: string          // YYYY-MM-DD
  total: number
  success: number
  failure: number
  p50DurationMs: number
  p95DurationMs: number
}

export interface AnalyticsBucket {
  date: string
  host: string
  group: string
  runnable: string
  operator: string
  initiatedBy: 'user' | 'llm'
  autonomy: string
  runbook: string                     // saved runbook that drove the run, "" = not a runbook run
  total: number
  success: number
  failure: number
  durationMsSum: number
  events: Record<string, number>   // per-level log counts (DEBUG…CRITICAL)
}

export interface AnalyticsException {
  exception: string
  runnable: string
  host: string
  count: number
  lastTs: string
}

export interface AnalyticsDimensions {
  hosts: string[]
  groups: string[]
  runnables: string[]
  operators: string[]
  initiatedBy: string[]
  runbooks: string[]
}

export interface AnalyticsDurationStats {
  p50: number
  p95: number
  max: number
  mean: number
}

export interface AnalyticsData {
  since: string          // YYYY-MM-DD inclusive lower bound (UTC)
  until: string          // YYYY-MM-DD upper bound (today, UTC)
  totalRuns: number
  successCount: number
  failureCount: number
  durationMs: AnalyticsDurationStats
  daily: AnalyticsDaily[]
  buckets: AnalyticsBucket[]       // pre-aggregated drill grain — filtered client-side
  exceptions: AnalyticsException[]
  dimensions: AnalyticsDimensions
  partial: boolean                 // true if any host scan failed/timed out
  errors: { host: string; message: string }[]
  // Hosts with cached runs whose log-event counts / exception aren't in the runs
  // index (recorded by runspec < 0.64) — the Events/Exceptions panels are partial.
  eventsUnknown?: string[]
}

// ── logs management (Logs tab) ──────────────────────────────────────────────
// One uniform shape per `runspec logs <verb> --json`, driven per venv.

export interface LogStatusRunnable {
  runnable: string
  per_run_files: number
  archives: number
  total_bytes: number
  oldest: string | null
  newest: string | null
}

export interface LogStatusVenv {
  group: string                    // venv directory name
  ok: boolean
  error?: string                   // set when ok === false
  dirs?: string[]
  runnables?: LogStatusRunnable[]
  total_bytes?: number
  total_files?: number
}

// "Make logs shared": ran the runspec-linux share-venv-logs runnable to set the
// venv's logs/ dir 2775 + setgid (+ optional chgrp), so the runs DB + audit logs
// are group-writable. as_user is "" for a direct run, else the owner/root the
// repair escalated to over SSH.
export interface ShareLogsResult {
  ok: boolean
  venv: string
  group?: string
  as_user?: string
  output?: string                  // set when ok
  error?: string                   // set when !ok
  // Set (ok=true) when the share applied but the console's own user still can't
  // write the dir — its runs' logs will fall back to ~/logs until that user joins
  // the dir's group. Names the fix.
  warning?: string
}

// Runs index (runspec_runs.db) health — `runspec history stats` for one venv's
// logs dir. write_error is non-empty when the login user can't write the index
// (the "Run as the venv owner" case). Needs runspec >= 0.62.0 on the target.
/** One remote host's History-cache freshness (see `history_sync_status`). */
export interface HistorySyncState {
  cached: boolean
  synced_at: string | null
  error: string
}

export interface HistoryDbStats {
  ok: boolean
  group: string
  error?: string
  path?: string
  exists?: boolean
  size_bytes?: number
  rows?: number
  by_status?: Record<string, number>
  oldest?: string | null
  newest?: string | null
  running?: number
  stale_running?: number           // dead-process `running` rows a reap would crash
  log_pruned?: number              // rows that outlive their pruned log file
  schema_version?: number | null
  write_error?: string
}

export type HistoryDbAction = 'prune' | 'reap' | 'reindex'

export interface HistoryDbResult {
  ok: boolean
  group: string
  verb?: HistoryDbAction
  count?: number
  error?: string
}

// Shared (host cron) schedules — read-only, from `runspec-scheduler status` on
// each host venv (runspec-scheduler >= 0.3.0). Managed on the host, not here.
export interface SharedScheduleRun {
  run_id: string
  status: string                   // completed / failed / crashed / skipped / running
  exit_code: number | null
  start_ts: string
  end_ts?: string | null
  duration_ms?: number | null
  trigger?: string
  user?: string
  log_file?: string | null
  log_pruned_at?: string | null
}

export interface SharedScheduleOutput {
  stdout: string
  stderr: string
  truncated: boolean
  result: { message?: string; status?: string; exit_code?: number; duration_ms?: number; reason?: string } | null
  errors: string[]                 // the runnable's own ERROR records for that run
}

export interface SharedSchedule {
  id: string
  runnable: string
  command?: string[]               // subcommand path for a runnable with verbs
  enabled: boolean
  when: { cron?: string; every?: string; timezone?: string }
  args?: Record<string, unknown>
  run_as?: string
  installable: boolean
  not_installable_reason: string | null
  installed: boolean | null        // null = the account's crontab couldn't be read
  line: 'exec' | 'legacy' | null   // legacy = pre-0.3 line (unrecorded output/skips)
  next_run: string | null
  runs: SharedScheduleRun[]
  last_output: SharedScheduleOutput | null
}

export interface SharedSchedulesStatus {
  ok: boolean
  venv: string
  config: string
  backend: string
  schedules: SharedSchedule[]
  stale: { id: string; user: string }[]
  errors: string[]
}

export interface SharedSchedulesVenv {
  group: string
  available: boolean               // false = no runspec-scheduler in this venv
  ok: boolean
  error?: string
  status?: SharedSchedulesStatus
}

export interface LogPruneResult {
  group: string
  ok: boolean
  error?: string
  dry_run?: boolean
  count?: number
  freed_bytes?: number
  deleted?: { path: string; bytes: number }[]
}

export interface LogCompactResult {
  group: string
  ok: boolean
  error?: string
  dry_run?: boolean
  compacted?: number
  archives?: { archive: string; count: number; sources: string[] }[]
}

export interface LogViewResult {
  ok: boolean
  error?: string
  records: Record<string, unknown>[]   // parsed JSON log lines, timestamp-sorted
}

export interface RotateHostResult {
  host: string
  pushed: boolean
  verified: boolean
  skipped: boolean
  error: string
}

export interface TestResult {
  connected: boolean
  runspec_ok: boolean
  runnable_count: number
  stdout: string
  stderr: string
  exit_code: number
}

// ── event triggers (Triggers tab) ───────────────────────────────────────────

// runspec:trigger_card event detail — one run card (an event instance moving
// through its run inbox). Lifecycle statuses: triggered | triage | awaiting_human
// | running | done | failed.
export interface TimelineEntry {
  ts: number      // epoch seconds
  text: string
}

export interface TriggerCard {
  card_id: string
  trigger: string
  source: string
  event_type: string
  summary: string
  column: string
  // Free-form per-stage data. Notable keys: `mode` ('agent' | 'rule', stamped at
  // dispatch — lets the Console pick a chat/transcript vs a run/output block),
  // `tool`/`args` (rule run target), `result`/`error` (terminal output).
  detail: Record<string, unknown>
  created_at: number
  timeline?: TimelineEntry[]   // timestamped audit trail of stage changes + steps
}

// A run card in the shared run inbox (RunInbox). Schedule runs reuse the exact
// TriggerCard shape, so one type serves both the Triggers and Schedules inboxes.
export type RunCard = TriggerCard

// ── console sessions ─────────────────────────────────────────────────────────
// One live conversation on the backend: an interactive chat, or the read-only
// conversation behind a trigger / schedule fire (kind 'trigger' / 'schedule').
// The `runspec:session` event carries this shape on create / turn start
// (state 'running', `turn` = the chat id) / turn end ('idle' for a chat, 'done'
// for automation) / close; an eviction or close may arrive as the partial
// `{id, state: 'closed'}`. For an automation session `id` == its run card's
// card_id == its turn's chat id.
export interface ChatSessionInfo {
  id: string
  kind: 'chat' | 'trigger' | 'schedule'
  label: string
  interactive: boolean
  created_at: number   // epoch seconds
  state: 'idle' | 'running' | 'done' | 'closed'
  turn: string         // chat id of the turn in flight ('' when idle)
  turns: number
}

// A parked autonomy-gate confirm — the runspec:tool_confirm event's shape, also
// returned by get_pending_confirms for cold-start. `id` is the chat/card id; a
// confirm whose id matches a live trigger card belongs to that card (the work
// inbox folds it there, the same id-match the Triggers tab badge uses).
export interface PendingConfirm {
  id: string
  request_id: string
  tool_name: string                     // raw tool name — matches the tool segment's entry.name
  tool_input: Record<string, unknown>
  // Human-readable view of what's being approved (unwrapped runnable + args for
  // the search-mode `run_runnable` wrapper; equal to tool_name/_input otherwise).
  display_name?: string
  display_input?: Record<string, unknown>
  autonomy: string
  // The gated tool call's id — pairs the gate with exactly its card (several
  // same-name cards can be running). Absent on older backends.
  call_id?: string
  created_at?: number
}

// runspec:trigger_event detail — every event the engine saw, with its
// disposition: which triggers fired (matched), which matched but were gated
// (skipped, with per-trigger reasons in skip_reasons — "screen locked" /
// "working hours"), which were a once_per_thread repeat (deduped), or neither
// (no match). skip_reasons/deduped are optional for backward compatibility with
// older persisted records.
export interface TriggerEvent {
  id: string
  source: string
  type: string
  summary: string
  matched: string[]
  skipped: string[]
  skip_reasons?: Record<string, string>
  deduped?: string[]
  ts: number
}

// One weekday of the working-hours rota.
export interface RotaDay {
  on?: boolean
  start?: string   // "HH:MM"
  end?: string     // "HH:MM"
}

// The working-hours rota. `source` picks the authority: "console" (the manual
// grid of RotaDay per mon..sun) or "outlook" (calendar shift-marker events).
export interface Rota {
  enabled?: boolean
  source?: string                 // "console" | "outlook"
  shift_marker?: string           // outlook: subject keyword marking a shift
  hold_during_meetings?: boolean  // outlook: also pause while in a meeting/OOO
  lock_suspend?: boolean          // pause reactive triggers while the screen is locked (default on)
  [day: string]: boolean | string | RotaDay | undefined
}

export interface TriggerSourceInfo {
  id: string
  available: boolean
  reason: string
  event_types: string[]
}

export interface TriggerListResult {
  triggers: Record<string, unknown>[]   // raw [triggers.<name>] tables from config
  errors: string[]                        // per-table parse errors (skipped)
  sources: TriggerSourceInfo[]
  rota: Rota                              // working-hours schedule ({} when unset)
  running: boolean
  break_until?: string | null             // active "Take a break" return time (ISO), or null
  screen_state?: LockState                // current screen-lock state (live presence indicator)
}

// Lock auto-suspend presence state. 'unknown' = the probe couldn't tell (treated
// as locked / fail-closed by the gate when lock_suspend is on).
export type LockState = 'locked' | 'unlocked' | 'unknown'

// ── In-app dictation (the composer's mic button; `[voice]` extra) ─────────────
export interface DictationStatus {
  available: boolean
  reason: string            // why unavailable (extra not installed, PortAudio missing, disabled)
  listening: boolean        // a session is live right now
  session_id: string | null
  model: string             // streaming model size, e.g. 'medium-streaming'
  language: string
  vendored: boolean         // model loaded from [voice] model_dir (never downloads)
  silence_timeout: number   // seconds of no recognised speech before the mic turns itself off (0 = never)
  keep_loaded: boolean      // engine stays resident between sessions (second press is instant)
  model_loaded: boolean     // the engine is resident right now
}
// Settings → Voice: the `[voice]` section plus live availability. `model` is the
// normalised streaming arch; `models` the selectable names; `device` an input
// index, a device name, or null for the system default.
export interface VoiceConfig {
  enabled: boolean
  language: string
  model: string
  model_dir: string | null
  device: number | string | null
  update_interval: number
  silence_timeout: number
  keep_loaded: boolean
  available: boolean
  reason: string
  model_loaded: boolean
  models: string[]
}
export interface VoiceDevice { index: number; name: string; default: boolean }
// `runspec:dictation_state` detail. `loading` = the model is opening (first run
// may download it — `progress` 0..1 + `file`); `listening` = mic open; `ended`
// = over (`reason`: 'stopped' by the operator, 'silence' after
// `silence_timeout` seconds with nothing recognised, else an error message).
export interface DictationStateEvent {
  session_id: string
  state: 'loading' | 'listening' | 'ended'
  reason?: string
  progress?: number
  file?: string
  model?: string
}
// `runspec:dictation_partial` (in-progress line, replaced as it firms up) and
// `runspec:dictation_final` (a finished line to append) detail.
export interface DictationTextEvent {
  session_id: string
  text: string
}


// ── chat room (Rooms view) ───────────────────────────────────────────────────
// The room is a "dumb message pipe with presence" the console connects to over
// the SSH tunnel; the bridge fans its events to the UI as runspec:room_* events.

// A helpdesk conversation's lifecycle (the room server's `conversation` frames).
export interface ThreadState {
  state: 'open' | 'assigned' | 'resolved' | 'closed' | string
  assignee: string | null
}

// One configured room (declared on a fleet host entry) + its live state.
// `queue` marks a helpdesk agent entry (room_queue on the host): the console is
// subscribed to that queue's namespace and `room` is the namespace key
// (helpdesk/<queue>); `threads` seeds the inbox (conversation room → state).
export interface RoomInfo {
  room: string         // the room name (or queue namespace in agent mode)
  host: string         // the fleet host it's hosted on
  user: string         // this console's display name in the room
  connected: boolean   // currently joined (per-host accurate; only the active host)
  active: boolean      // the single host this console is currently selected to staff
  queue: string        // bare queue name in agent mode; '' for group chat
  threads?: Record<string, ThreadState>
}

// All configured rooms (bridge.get_room_state). Empty when none are configured.
export interface RoomsState {
  rooms: RoomInfo[]
}

// A room-configured host offered in the single-room selector (bridge.get_room_hosts).
export interface RoomHost {
  host: string
  room: string
  queue: string
}

// bridge.test_room_connection — an isolated, on-demand diagnosis of a room's
// connection over the real paramiko path (proxy/key/channel). `stage` localises
// a failure: ssh (reach/auth the host), channel (tunnel/exec relay), room (relay
// up but room server silent), config, or ok.
export interface RoomConnTest {
  ok: boolean
  stage: 'ssh' | 'channel' | 'room' | 'config' | 'ok'
  message: string
  hint?: string | null
  host?: string
  room?: string
  queue?: string
}

// runspec:room_connection — a room's live connection flipped (connect / drop).
export interface RoomConnection {
  room: string
  host: string
  connected: boolean
  queue?: string
}

// runspec:room_message — a sent message (server-stamped id/ts/sender). A
// visitor's catalog submission arrives as kind="request" with the machine
// payload in `data`; plain chat is kind="chat" (older servers send neither).
export interface RoomMessage {
  room: string
  sender: string
  text: string
  id: string
  ts: number
  kind?: string
  data?: { tool: string; args: Record<string, unknown> }
}

// runspec:room_conversation — a helpdesk thread's lifecycle state changed
// (open on first visitor message, assigned on claim, resolved/closed/reopened).
export interface RoomConversation {
  room: string
  state: string
  assignee: string | null
}

// runspec:room_presence — a participant's live presence in a room.
export interface RoomPresence {
  room: string
  user: string
  status: 'available' | 'away'
}

// runspec:room_draft — an agent-proposed reply pre-filling the composer (the
// "speak" checkpoint: the human edits and sends; the agent never sends).
export interface RoomDraft {
  id: string
  room: string
  text: string
}


// Denylist kill switch (Settings → Denylist). `targets` empty ⇒ all hosts;
// `runnable`/`user`/`host` are globs used by action 'add'; `setting` is for
// action 'mode'. Returns the aggregated per-host/per-venv summary string.
export interface DenylistPayload {
  action: 'list' | 'add' | 'all' | 'clear' | 'mode'
  targets?: string[]
  runnable?: string[]
  user?: string[]
  host?: string[]
  reason?: string
  setting?: 'enforce' | 'warn' | 'off'
}

export interface DenylistRule {
  runnables: string[]
  users: string[]
  hosts: string[]
  reason: string
}

// One row per venv on a host, from denylist_status(). When `ok` is false the
// host/venv couldn't be read (see `error`); otherwise the denylist fields apply.
export interface DenylistHostStatus {
  host: string
  venv: string
  ok: boolean
  error?: string
  path?: string
  present?: boolean
  mode?: 'enforce' | 'warn' | 'off'
  disabled?: boolean
  reason?: string
  rules?: DenylistRule[]
}


// ── Marketplace (runspec runnable packages from package indexes) ─────────────
// One package index the marketplace reads. `source: "pip"` rows are derived
// from the console venv's pip configuration (index-url / extra-index-url) and
// are read-only in the UI; `source: "config"` rows are [[marketplace.repos]].
export interface MarketplaceRepo {
  id: string
  name: string
  url: string
  enabled: boolean
  scan: boolean
  packages: string[]
  patterns: string[]
  credential: string
  pip_arg: 'auto' | 'none' | 'index-url' | 'extra-index-url'
  trusted_host: string
  tags: string[]
  source: 'config' | 'pip'
  pip_role: '' | 'index' | 'extra'
  is_pypi: boolean
}

export interface MarketplaceConfig {
  enabled: boolean
  auto_pip: boolean
  repos: MarketplaceRepo[]           // effective: declared + pip-derived
  declared_repos: MarketplaceRepo[]  // the [[marketplace.repos]] entries only
  patterns: string[]
  exclude: string[]
  hide: string[]                     // name globs of runnable packages to hide from the catalogue
  require_runspec: boolean
  include_prerelease: boolean
  cache_ttl: number
  timeout: number
  max_index_bytes: number
  max_wheel_bytes: number
  ca_bundle: string | null
  tls_verify: boolean
  pip_sources: string[]
  credential_options: McpCredentialOption[]
  cache_path: string
}

// What save_marketplace_config takes: the editable subset (declared repos only).
export interface MarketplaceConfigInput {
  enabled?: boolean
  auto_pip?: boolean
  repos?: Partial<MarketplaceRepo>[]
  patterns?: string[]
  exclude?: string[]
  hide?: string[]
  require_runspec?: boolean
  include_prerelease?: boolean
  cache_ttl?: number
  timeout?: number
  ca_bundle?: string | null
  tls_verify?: boolean
}

export interface MarketplaceEntry {
  name: string
  display_name: string
  version: string
  versions: string[]
  summary: string
  description: string
  description_truncated: boolean
  description_content_type: string
  keywords: string[]
  links: { documentation?: string; homepage?: string; source?: string; changelog?: string; issues?: string }
  requires_python: string
  requires_dist: string[]
  license: string
  author: string
  upload_time: string
  has_runspec_toml: boolean
  metadata_source: string
  repo: string        // the repo id the entry was read from
  repo_name: string
  repo_url: string
  repos: string[]     // every repo id that carries the package
  newest_elsewhere?: { repo: string; version: string }
}

export interface MarketplaceRepoStatus {
  id: string
  name: string
  url: string
  source: 'config' | 'pip'
  scan: boolean
  packages: string[]
  count: number
  fetched_at: number | null
  stale: boolean
  error: string | null
}

export interface MarketplaceCatalog {
  enabled: boolean
  entries: MarketplaceEntry[]
  repos: MarketplaceRepoStatus[]
  errors: string[]
}

// A venv a package can be installed into: the console's own venv / a local
// venv_globs match (`local: true`) or a fleet host's runspec_paths venv.
export interface MarketplaceTarget {
  id: string          // `${host}::${path}`
  host: string
  venv: string
  path: string
  python: string
  runspec_path: string
  local: boolean
  connected: boolean
}

export interface MarketplaceInstalledTarget extends MarketplaceTarget {
  ok: boolean
  error: string | null
  owner: string          // POSIX owner of the venv (blank on Windows / unknown)
  user: string           // the user the probe ran as
  python_version: string
  dists: Record<string, string>  // normalised name → installed version
}

export interface MarketplaceInstalled {
  targets: MarketplaceInstalledTarget[]
  probed_at: number
}

export interface MarketplaceOpTarget {
  id: string
  run_as?: string
  become_method?: 'sudo' | 'su'
}

export interface MarketplaceOpPayload {
  package: string
  targets: MarketplaceOpTarget[]
  upgrade?: boolean
  reinstall?: boolean
  pre?: boolean
  repo?: string
}

export interface MarketplaceOpResult {
  target: string
  host: string
  venv: string
  ok: boolean
  exit_code: number | null
  summary: string
  lines?: string[]
  skipped: boolean
}

export interface MarketplaceOpRecord {
  op: string
  action: 'install' | 'uninstall'
  package: string
  status: 'running' | 'finished' | 'cancelled'
  results: MarketplaceOpResult[]
}

export interface BridgeApi {
  get_hosts: () => Promise<Host[]>
  // Disable / re-enable runnables across fleet hosts (a kill switch). Edits each
  // host's venv denylist via the core `runspec deny` over SSH.
  manage_denylist: (payload: DenylistPayload) => Promise<string>
  // Read-only structured denylist state per host/venv (Settings → Denylist table).
  denylist_status: (targets?: string[]) => Promise<DenylistHostStatus[]>
  get_runnables: (host: string) => Promise<Runnable[]>
  // Fleet-discovery readiness for the "discovering N/M hosts…" indicator. `ready`
  // flips true after the first full refresh cycle; live updates arrive on the
  // `runspec:discovery_state` window event (read this on mount in case it predates
  // the listener).
  discovery_state: () => Promise<{ ready: boolean; done: number; total: number }>
  get_history: (host: string, runnable?: string) => Promise<HistoryRecord[]>
  // History across every host (local + connected remotes) — the History tab uses
  // this so a run shows up regardless of which host it ran on. `since_days` bounds
  // the read to the last N days (default 7 backend-side; 0 = all); only real
  // runnable runs are returned and the heavy logLines are dropped (fetch them on
  // expand via get_run_detail) so the fleet-wide payload stays small.
  get_history_all: (since_days?: number) => Promise<HistoryRecord[]>
  // The hosts the History tab reads, in order: `local` first, then every
  // connected remote (disconnected remotes skipped). The tab fetches these one at
  // a time (get_history_slim) so it can render each host as it arrives and show a
  // per-host fetch time — one slow/offline host no longer blocks the whole view.
  history_targets: () => Promise<string[]>
  // One host's slim, runnable-only history rows (same shape/filtering as
  // get_history_all, scoped to a single host) — the per-host unit the History tab
  // fetches. Heavy logLines are dropped; fetch them on expand via get_run_detail.
  get_history_slim: (host: string, since_days?: number) => Promise<HistoryRecord[]>
  // Per remote host whose History comes from the console's local cache (pulled
  // incrementally from each venv's runs index — runspec >= 0.63.0): when it was
  // last pulled and the last pull's error. A host absent from the map is read
  // directly. The backend emits `runspec:history_updated {host}` when a pull
  // changed a host's cached runs.
  history_sync_status: () => Promise<Record<string, HistorySyncState>>
  // Heavy per-run detail (the recorded log lines) for one run, fetched on demand
  // when a History row is expanded — the bulk read ships slim rows without them.
  get_run_detail: (host: string, run_id: string) => Promise<{ logLines: HistoryLogLine[] }>
  // Live "what's running" across the fleet — the in-flight runs the per-venv runs
  // index holds, newest-started first, for the History tab's "Running now" strip.
  // Fleet-wide and index-sourced (every connected host's running rows, whoever
  // launched them), liveness-filtered so a crashed run never shows; index-only, so
  // a venv without the index contributes nothing (a running run has no run_summary
  // to log-parse yet). Distinct from get_in_flight (this console's own launches).
  get_running_runs: () => Promise<RunningRun[]>
  get_schedules: () => Promise<Schedule[]>
  get_config: () => Promise<Record<string, unknown>>
  config_dir: () => Promise<string>
  // The single monitored mailbox (Settings → Triggers), or null when unset.
  // Saving restarts the trigger engine so the Outlook source re-resolves the
  // watched folder; pass null to stop monitoring.
  get_mailbox: () => Promise<Mailbox | null>
  save_mailbox: (mailbox: Mailbox | null) => Promise<void>
  // Additional watch-only mailboxes monitored on top of the primary [mailbox]
  // (Settings → Mailbox). Saving restarts the trigger engine; pass [] to clear.
  get_watches: () => Promise<Watch[]>
  save_watches: (watches: Watch[]) => Promise<void>
  // ── version / docs ─────────────────────────────────────────────────────────
  app_title: () => Promise<string>
  // Validated [console.theme] for white-label rendering (read-only; set by a
  // config seed or by hand). Empty object = the default look.
  brand_theme: () => Promise<BrandTheme>
  // The `[console] ai` off-switch (default true). Read once at bring-up by the
  // backend; false hides the AI surfaces here (see src/features.ts) while the
  // backend refuses every agent path. Restart to apply a config change.
  ai_enabled: () => Promise<boolean>
  // Blocking first-use AI-use disclaimer ([console.disclaimer]). `needs_ack` is
  // true only when it's enabled AND the current wording hasn't been acknowledged
  // on this machine; false for a vanilla install, when unset/empty, or when
  // [console] ai = false. Read-only from config (no Settings editor — an operator
  // can't click their own compliance notice off). See docs/design/console-disclaimer.md.
  disclaimer: () => Promise<Disclaimer>
  // Record acknowledgement of the *current* wording (writes the machine-local ack
  // + a console audit line + a Windows event-log entry; telemetry carries it too
  // when configured). Returns { ok, needs_ack } — needs_ack stays true if the
  // write failed, so the gate stays up.
  acknowledge_disclaimer: () => Promise<{ ok: boolean; needs_ack: boolean }>
  app_version: () => Promise<string>
  check_for_update: () => Promise<{ ok: boolean; current: string; latest: string | null; update_available: boolean }>
  docs_url: () => Promise<string>
  open_docs: () => Promise<void>
  list_providers: () => Promise<{ value: string; label: string }[]>
  save_config: (data: Record<string, unknown>) => Promise<void>
  // The built-in base system prompt (what runs when `[llm] system` is unset).
  // Settings → Model / API pre-fills the field with it; a saved value equal to
  // it is dropped from config.toml so the install keeps tracking the default.
  default_system_prompt: () => Promise<string>
  // Probe every in-scope host and re-discover its runnables now (a forced
  // refresh cycle — discovery otherwise only re-runs on reconnect / every
  // `[refresh] discovery_interval`). Returns immediately; progress arrives via
  // `runspec:discovery_state` and the result via `runspec:runnables_updated`.
  refresh_hosts: () => Promise<void>
  // Emit one representative agent-turn trace (turn → LLM call → tool call) to the
  // configured OTLP backend and flush it immediately, so the operator can verify
  // the [telemetry] pipeline without running a real turn. Reconfigures the tracer
  // from the saved config first (picks up a just-saved endpoint). ok=false carries
  // a reason; on success, trace_id / spans / endpoint / capture_content / flushed.
  send_test_trace: () => Promise<{
    ok: boolean
    reason?: string
    trace_id?: string
    spans?: string[]
    endpoint?: string
    capture_content?: boolean
    flushed?: boolean
  }>
  // Emit one representative tool-run + turn metric to the configured
  // [telemetry.metrics] OTLP endpoint and flush it immediately — the metrics twin
  // of send_test_trace. Reconfigures the meter from the saved config first.
  // ok=false carries a reason; on success, instruments / endpoint / flushed.
  send_test_metric: () => Promise<{
    ok: boolean
    reason?: string
    instruments?: string[]
    endpoint?: string
    flushed?: boolean
  }>
  // Fire a test toast + taskbar flash + confirm sound; reports which channels are
  // available (Settings → Sounds & Notifications). window_found surfaces the
  // flash/focus title-match prerequisite.
  test_notification: () => Promise<{ toast_available: boolean; toast_reason: string; sound_available: boolean; window_found: boolean }>
  // Preview one sound category (Settings row ▶). token plays the row's unsaved
  // selection ("synth:…" / "system:…" / "file:…"); omitted → the saved cue.
  test_sound: (category: string, token?: string | null) => Promise<boolean>
  // Coverage report for a sound-pack folder: which categories it has files for.
  sound_pack_info: (path?: string) => Promise<{ dir: string; exists: boolean; covered: Record<string, string>; categories: string[] }>
  // The named-sound registry (runspec_sounds.toml): operator-added audio files
  // given a stable id + label, referenced by a "sound:<id>" token in the per-
  // category picks, a trigger/schedule action, and the agent's play_sound tool.
  // `exists` is false for a synced entry whose file isn't on this machine.
  get_sounds: () => Promise<NamedSound[]>
  // Replace the whole registry (Settings edit). Validates every entry first.
  save_sounds: (rows: NamedSound[]) => Promise<{ ok: boolean; error?: string; sounds?: NamedSound[] }>
  // Add a named sound: copy the picked .wav/.mp3 into the app-data store and
  // register it under a slug id derived from `label` (or the file name).
  import_sound: (source_path: string, label?: string) => Promise<{ ok: boolean; error?: string; sound?: NamedSound }>
  // Remove a named sound (and its imported copy, if console-owned).
  delete_sound: (sound_id: string) => Promise<{ ok: boolean; error?: string; sounds?: NamedSound[] }>
  // Audition a named/built-in sound by id or token (forced past the master toggle).
  preview_sound: (identifier: string) => Promise<boolean>
  create_schedule: (data: Record<string, unknown>) => Promise<void>
  // Replace a schedule in place (edit dialog, inline enable/disable). Keeps the id.
  update_schedule: (id: string, data: Record<string, unknown>) => Promise<void>
  delete_schedule: (id: string) => Promise<void>
  // Fire a schedule immediately — runs the runnable (returns inv_id) or emits the
  // event (returns event), regardless of the schedule's enabled flag.
  run_schedule_now: (id: string) => Promise<{ action: 'runnable' | 'event' | 'prompt' | 'runbook' | 'saved_run'; inv_id?: string; event?: string; host?: string; runnable?: string }>
  get_jump_hosts: () => Promise<JumpHost[]>
  save_jump_hosts: (hosts: JumpHost[]) => Promise<void>
  import_jump_hosts: (tomlContent: string) => Promise<JumpHost[]>
  // Local machine venv auto-discovery ([local] venv_globs in config.toml). get
  // returns the saved globs + a live preview of what they resolve to here; save
  // validates + persists them (raises on bad input), then rediscovers.
  get_local_venv_config: () => Promise<LocalVenvConfig>
  save_local_venv_config: (globs: string[]) => Promise<LocalVenvConfig>
  // Agent-tool visibility ([tool_visibility] in config.toml). get returns the
  // two hide lists; save validates + persists them (raises on bad input) — the
  // lists ride Config Sync / the config seed.
  // Marketplace — runspec runnable packages from package indexes (pip config +
  // [[marketplace.repos]]), installable into local + fleet venvs. Long ops
  // return {op} and stream runspec:marketplace_progress / _done events.
  get_marketplace_config: () => Promise<MarketplaceConfig>
  save_marketplace_config: (section: MarketplaceConfigInput) => Promise<MarketplaceConfig>
  marketplace_catalog: (refresh?: boolean) => Promise<MarketplaceCatalog>
  marketplace_targets: () => Promise<MarketplaceTarget[]>
  marketplace_installed: (refresh?: boolean) => Promise<MarketplaceInstalled>
  marketplace_install: (payload: MarketplaceOpPayload) => Promise<{ op: string; targets: string[] }>
  marketplace_uninstall: (payload: MarketplaceOpPayload) => Promise<{ op: string; targets: string[] }>
  marketplace_op: (op: string) => Promise<MarketplaceOpRecord | null>
  marketplace_cancel: (op: string) => Promise<boolean>
  marketplace_clear_cache: () => Promise<void>
  get_tool_visibility: () => Promise<ToolVisibility>
  save_tool_visibility: (payload: ToolVisibility) => Promise<ToolVisibility>
  // Canonical host-group registry (runspec_groups.toml) — the controlled
  // vocabulary the host editor's group dropdown is populated from (synced).
  get_groups: () => Promise<HostGroup[]>
  save_groups: (groups: HostGroup[]) => Promise<void>
  rename_group: (oldName: string, newName: string) => Promise<void>
  // Host-profile registry + the machine-local active profile (working sets).
  get_profiles: () => Promise<HostProfile[]>
  save_profiles: (profiles: HostProfile[]) => Promise<void>
  rename_profile: (oldName: string, newName: string) => Promise<void>
  get_active_profile: () => Promise<string>
  set_active_profile: (name: string) => Promise<{ active: string }>
  // Self-service catalog registry (runspec_self_service.toml) — which discovered
  // runnables are offered to visitors in the helpdesk room (Settings →
  // Self-Service). Replaces the runspec.toml `discoverable = ["self-service"]`
  // marking; saving republishes every connected queue's catalog.
  get_self_service: () => Promise<SelfServiceEntry[]>
  save_self_service: (entries: SelfServiceEntry[]) => Promise<void>
  // Machine-local single-room selector (Settings → Self-Service): which
  // room-configured host this console connects to. get_room_hosts lists the
  // candidates; get_active_room returns the effectively-connected host ("" =
  // none); set_active_room picks one ("" = no room) and live re-joins.
  get_room_hosts: () => Promise<RoomHost[]>
  get_active_room: () => Promise<string>
  set_active_room: (host: string) => Promise<{ active: string }>
  // Git-repo registry (runspec_repos.toml) — the agent's read/edit allowlist
  // (Settings → Repos). sync_repo clones/fetches a repo on operator demand.
  get_repos: () => Promise<Repo[]>
  save_repos: (repos: Repo[]) => Promise<void>
  // Remove a repo from the registry and delete its AppData checkout from disk; a
  // custom dev-folder checkout (Repo.path) is never deleted.
  delete_repo: (name: string) => Promise<{ ok: boolean; removed_checkout: boolean }>
  sync_repo: (name: string) => Promise<RepoSyncResult>
  // Named source-side stream filters (Settings → Filters), selectable by name in
  // the Relay and Download dialogs.
  get_filters: () => Promise<Filter[]>
  save_filters: (filters: Filter[]) => Promise<void>
  import_filters: (tomlContent: string) => Promise<Filter[]>
  // Named credentials (Settings → Credentials). Metadata is returned by
  // list_credentials; the secret value rides only on save (write-only) and is
  // stored in the OS keyring, never read back.
  list_credentials: () => Promise<Credential[]>
  // In-scope credentials for a runnable (id + label) — for the Forms run dialog's
  // optional credential override. In-scope credentials auto-apply on run anyway.
  credentials_for_runnable: (host: string, runnable: string) => Promise<{ id: string; label: string; has_secret: boolean; secret_arg: string; username_arg: string; username: string }[]>
  save_credential: (entry: Credential) => Promise<{ id?: string; error?: string }>
  delete_credential: (id: string) => Promise<void>
  credentials_backend: () => Promise<CredentialBackend>
  // Settings → MCP (external MCP servers the chat agent connects to). get/save the
  // [mcp] section; save validates, writes config.toml, reconnects, and returns the
  // fresh status. mcp_status/mcp_reload report/refresh live connection state;
  // mcp_credential_options feeds the per-server credential dropdown (no secrets).
  get_mcp_config: () => Promise<McpConfig>
  save_mcp_config: (payload: McpConfig) => Promise<McpStatus>
  mcp_status: () => Promise<McpStatus>
  mcp_reload: () => Promise<McpStatus>
  mcp_credential_options: () => Promise<McpCredentialOption[]>
  // One-click: install a pip-distributed stdio MCP server into a dedicated venv
  // (under app-data mcp-venvs/, not the console's own) and wire it as a
  // [[mcp.servers]] stdio entry. On a launcher-not-found result, `scripts` lists
  // the venv's console scripts so the operator can retry with the right command.
  mcp_install_venv: (payload: { name: string; package: string; command?: string; autonomy?: string }) => Promise<McpInstallResult>
  // The console's own MCP server ([mcp_server]) — get/save the settings and read
  // live status (running/url/auth/tool count). save restarts the server to apply.
  get_mcp_server_config: () => Promise<OwnMcpServerConfig>
  save_mcp_server_config: (payload: Partial<OwnMcpServerConfig>) => Promise<OwnMcpServerStatus>
  mcp_server_status: () => Promise<OwnMcpServerStatus>
  mcp_server_start: () => Promise<OwnMcpServerStatus>
  // Config Sync (Settings → Config Sync): pull config files from a git repo by
  // tag. get/save the settings, list the repo's tags for the ref dropdown, and
  // run a pull (overwrites local config, backing up first; hot-reloads after).
  get_sync_config: () => Promise<SyncConfig>
  save_sync_config: (data: Partial<SyncConfig>) => Promise<void>
  list_sync_tags: () => Promise<{ tags?: string[]; error?: string }>
  sync_pull: () => Promise<SyncPullResult>
  // Package config seed (Settings → Config Sync): baseline config bundled inside
  // the deployment package, applied on launch when its version changed. Read-only
  // status, and a force re-apply button.
  seed_status: () => Promise<SeedStatus>
  seed_apply: (force?: boolean) => Promise<SeedApplyResult>
  // Runbook docs: a deterministic markdown wiki generated from the local runbooks
  // + fleet, for humans to follow during an outage. get/save the output dir and
  // (re)generate the files.
  get_docs_config: () => Promise<DocsConfig>
  save_docs_config: (dest_dir: string) => Promise<void>
  generate_docs: (dest_dir?: string) => Promise<DocsGenResult>
  test_host: (name: string) => Promise<TestResult>
  // `credentialIds` (optional) are saved credentials the operator chose to pass to
  // this run as name-derived plain env vars (e.g. JIRA_USERNAME/JIRA_PASSWORD).
  // Credentials are never copied into args on this human/form path.
  invoke_runnable: (host: string, runnable: string, args: Record<string, unknown>, commandPath?: string[], group?: string, credentialIds?: string[]) => Promise<string>
  // List a directory on a host for the Forms path-picker. With asServiceAccount +
  // a runnable, the remote listing runs as that runnable's run_as (sudo/become).
  list_dir: (host: string, path?: string, group?: string, dirsOnly?: boolean, runnable?: string, asServiceAccount?: boolean) => Promise<DirListing>
  cancel_invocation: (invId: string) => Promise<void>
  // Copy files between the local machine and a remote host over SFTP (exactly one
  // side is 'local'). The remote side is a configured host name or an AdHocEndpoint
  // (an SSH host not in the fleet, reached via a jump). Returns an id, then streams
  // runspec:transfer_progress / transfer_end. filterName/filterArg select a saved
  // source-side filter (download: filtered at the source; upload: filtered locally).
  // destName optionally renames the (single) file on the destination (bare filename).
  start_transfer: (sourceHost: TransferEndpoint, sourcePaths: string[], destHost: TransferEndpoint, destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
  // Fan-out: upload the same local source file(s) to dest_dir on many remote hosts.
  // filterName/filterArg apply a saved filter locally (once) before fanning out.
  // destName optionally renames the (single) file on every target (bare filename).
  start_fanout: (sourcePaths: string[], destHosts: string[], destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
  // Relay: stream file(s) from one host to another through the console. Source is a
  // configured host, or an ad-hoc host reached via a configured jump (key first,
  // then an optional password — used once, never stored/logged).
  // filterName/filterArg select a saved source-side filter run on the source host,
  // so only matching output is relayed (handy for pulling a few lines from a huge log).
  // destName optionally renames the (single) file on the destination (bare filename).
  start_relay: (source: RelaySource, sourcePaths: string[], destHost: string, destDir: string, filterName?: string, filterArg?: string, destName?: string) => Promise<{ id?: string; error?: string }>
  // Browse a directory on an ad-hoc relay source (reached via a jump). Password
  // is used only for this listing connection, never stored.
  list_dir_adhoc: (jump: string, host: string, path?: string, user?: string, password?: string, port?: number, dirsOnly?: boolean, credentialId?: string) => Promise<DirListing>
  // Browse a directory on a host that isn't saved yet (Settings → Jump Hosts add/
  // edit form) so the runspec-path picker can pick where runspec lives. Connects
  // straight to the host from the form's connection details (no jump).
  list_dir_unsaved: (hostname: string, path?: string, user?: string, port?: number, identityFile?: string, dirsOnly?: boolean) => Promise<DirListing>
  // Close cached ad-hoc browse connections (call when the relay dialog closes).
  close_adhoc: () => Promise<void>
  // Pre-flight: does `path` exist as a directory on each host? (fan-out dest check)
  check_dir_exists: (hosts: string[], path: string) => Promise<Record<string, { exists: boolean; error?: string | null }>>
  cancel_transfer: (id: string) => Promise<void>
  // Absolute path of the current chat session's agent workspace, or '' if none.
  // The Transfer dialog defaults a local destination to it so downloads land
  // where the agent works. Creates the directory if missing.
  current_workspace_path: () => Promise<string>
  // Start a chat turn in `sessionId` (the selected interactive session; omitted
  // ⇒ the backend's current one). Returns the turn's chat id (== the block id).
  // Sending to an automation session yields an error token + run_end exit 1
  // under that chat id — the UI never does that (the composer is read-only).
  send_chat: (message: string, invocationId?: string, sessionId?: string) => Promise<string>
  cancel_chat: (chatId: string) => Promise<void>
  // Replace `sessionId` (an interactive session) with a fresh, empty one — the
  // AI-off "Clear". "New chat" uses new_chat_session instead, so the previous
  // conversation keeps running. Also dispatches runspec:chat_cleared.
  clear_chat: (sessionId?: string) => Promise<{ ok: boolean; session?: ChatSessionInfo; replaced?: string }>
  // Condense the live chat history in place (drop the older prefix, fold a note,
  // keep the recent tail) — frees context without losing the thread. Returns the
  // number of messages dropped. See docs/design/console-context-indicator.md.
  compact_chat: (sessionId?: string) => Promise<{ ok: boolean; compacted: number }>
  // ── sessions ───────────────────────────────────────────────────────────────
  // Open a fresh interactive session and make it the backend's send target; the
  // previous session keeps running (concurrent turns).
  new_chat_session: () => Promise<ChatSessionInfo>
  // Every live session (interactive + automation), oldest first — cold-start seed.
  list_chat_sessions: () => Promise<ChatSessionInfo[]>
  // Make an interactive session the send target. Interactive ids only — the UI
  // selects automation sessions locally and never calls this for them.
  select_chat_session: (id: string) => Promise<{ ok: boolean; session?: ChatSessionInfo; error?: string }>
  // Stop an in-flight turn and drop the session (an automation session: dismiss
  // its chip). The saved conversation (History) is untouched.
  close_chat_session: (id: string) => Promise<{ ok: boolean; error?: string }>
  // Toggle whether a completed chat turn stays in the agent's context (the
  // Console card "in context" checkbox). Reversible and non-destructive — the
  // backend keeps the messages and only filters, so re-including restores the
  // exchange. Returns the resulting in/out state.
  set_turn_included: (chatId: string, included: boolean) => Promise<{ ok: boolean; included: boolean }>
  // ── saved conversations (chat history) ────────────────────────────────────────
  // Metadata for every saved chat, newest first (for the Conversations browser).
  list_conversations: () => Promise<ConversationMeta[]>
  // A saved conversation's full transcript, for rendering it read-only.
  load_conversation: (id: string) => Promise<ChatConversation>
  // Make a saved conversation the active rolling chat so new sends continue it
  // (history rebuilt from the transcript as plain text; exact=false). On a long
  // chat the middle is condensed on resume (compacted=true; summarized=true when
  // an LLM summary was used) — the saved transcript itself is never changed.
  // `session` is the live session's id (== the conversation id); `already_open`
  // means that conversation is already an open session — just select it, don't
  // re-hydrate its blocks.
  resume_conversation: (id: string) => Promise<{ ok: boolean; turns?: number; kept?: number; compacted?: boolean; summarized?: boolean; exact?: boolean; error?: string; session?: string; already_open?: boolean }>
  delete_conversation: (id: string) => Promise<void>
  rename_conversation: (id: string, title: string) => Promise<void>
  // ── Improve (effectiveness + problem report) ──────────────────────────────────
  // Mine this install's saved conversations + run history into an effectiveness
  // report. A non-empty comment makes it a problem report (carried into the
  // report for correlation). window_days 0 ⇒ the [improve] config default.
  run_improve_report: (windowDays?: number, comment?: string) => Promise<ImproveReport>
  // Saved reports, newest first.
  list_improve_reports: () => Promise<ImproveReportMeta[]>
  load_improve_report: (reportId: string) => Promise<ImproveReport | null>
  delete_improve_report: (reportId: string) => Promise<boolean>
  // Run the LLM-judge pass over a saved report → ranked recommendations (+ a
  // comment↔evidence correlation for a problem report). Best-effort: returns the
  // report with `synthesis_error` when no provider is configured or the call fails.
  synthesize_improve_report: (reportId: string) => Promise<ImproveReport>
  // Build a mailto: to the configured developer_emails with the report body, open
  // the operator's default mail client, and return the URL (also usable as a link).
  improve_mailto: (reportId: string) => Promise<string>
  // ── Evals (proactive tool-selection eval suites) ─────────────────────────────
  // Drive the configured agent over a fixture's [[eval]] cases (inline `fixtureToml`
  // or a stored `fixtureId`) and score which runnable it WOULD pick, with which
  // args, without executing. `samples` runs each case N times (stochastic model);
  // `validate` also asserts the args wouldn't bounce (preflight); `telemetry` emits
  // one OTLP trace per case. Returns the scored report, or {ok:false,reason}.
  run_evals: (
    fixtureToml?: string,
    fixtureId?: string,
    samples?: number,
    validate?: boolean,
    telemetry?: boolean,
  ) => Promise<EvalRunResult>
  list_eval_reports: () => Promise<EvalReportMeta[]>
  load_eval_report: (reportId: string) => Promise<EvalReport | null>
  delete_eval_report: (reportId: string) => Promise<boolean>
  // Stored fixture suites (machine-local .toml files).
  list_eval_fixtures: () => Promise<EvalFixtureMeta[]>
  get_eval_fixture_text: (fixtureId: string) => Promise<string>
  save_eval_fixture: (fixtureId: string, tomlText: string) => Promise<EvalFixtureSaveResult>
  delete_eval_fixture: (fixtureId: string) => Promise<boolean>
  // ── runbooks (user-invocable, parameterized runbooks) ─────────────────────────
  // All runbooks with their [runbook.args] parsed to ArgDef[] — feeds the `$` menu,
  // the run modal, and the Settings editor.
  get_runbooks: () => Promise<Runbook[]>
  // Persist the whole collection from the Settings editor (validates each args
  // table; rejects with {ok:false,error} if one is malformed).
  save_runbooks: (runbooks: Runbook[]) => Promise<{ ok: boolean; count?: number; error?: string }>
  // Upsert a single runbook via backend read-modify-write — the editor's save path.
  // Leaves other runbooks (incl. ones changed on disk since load) untouched and
  // preserves each one's `discoverable` audience. Rejects with {ok:false,error}.
  save_runbook_entry: (entry: Runbook) => Promise<{ ok: boolean; id?: string; error?: string }>
  // Delete a single runbook by id via backend read-modify-write (others untouched).
  delete_runbook: (runbook_id: string) => Promise<{ ok: boolean; removed?: boolean; error?: string }>
  // Render a runbook's body with the operator's form values (validated against its
  // args) + optional extra instructions, then start a chat turn tagged with the
  // runbook. Returns {chat_id, message} (the rendered text the UI shows) or {error}.
  run_runbook: (
    runbookId: string,
    values?: Record<string, unknown>,
    extraInstructions?: string,
    sessionId?: string,
  ) => Promise<{ chat_id?: string; message?: string; error?: string }>
  // ── saved runs (deterministic presets) ─────────────────────────────────────
  // All saved runs with their params parsed to ArgDef[] — feeds the Forms lane,
  // the run modal, the Schedules picker, and the Settings editor.
  get_saved_runs: () => Promise<SavedRun[]>
  // Persist the whole collection from the Settings editor (each entry validated;
  // rejected with {ok:false,error} if one is invalid).
  save_saved_runs: (savedRuns: SavedRun[]) => Promise<{ ok: boolean; count?: number; error?: string }>
  // Fire a saved run now: validates `values` against its open params, merges
  // them over the fixed args, resolves the stored targeting bridge-side, and
  // invokes — one {host, inv_id} per host actually run (the UI renders one
  // Console run block each, streaming via runspec:output / runspec:run_end).
  run_saved_run: (
    id: string,
    values?: Record<string, unknown>,
  ) => Promise<{ id?: string; runnable?: string; runs?: { host: string; inv_id: string }[]; error?: string }>
  // Resolve a parked autonomy-gate confirm. `editedInput` (optional) replaces the
  // agent's proposed tool_input when approving — the operator can tweak the args
  // before the call runs (editable confirmations). Ignored on a deny.
  resolve_tool_confirmation: (
    requestId: string,
    approved: boolean,
    editedInput?: Record<string, unknown>,
  ) => Promise<void>
  // ── event triggers ─────────────────────────────────────────────────────────
  list_triggers: () => Promise<TriggerListResult>
  // The built-in action tools an agent-mode trigger may be allowed to use, for the
  // Triggers editor's per-trigger allowlist ({name, label}; Outlook entries appear
  // only when Outlook COM is reachable on the host).
  list_action_tools: () => Promise<{ name: string; label: string }[]>
  set_trigger_enabled: (name: string, enabled: boolean) => Promise<{ ok: boolean }>
  // Add/update one [triggers.<name>] table (upsert by name) or remove it; both
  // restart the trigger engine. Re-read list_triggers after to refresh the view.
  save_trigger: (trigger: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>
  delete_trigger: (name: string) => Promise<{ ok: boolean }>
  save_rota: (rota: Rota) => Promise<{ ok: boolean }>
  // Rota "Take a break": suspend automation for N minutes (auto-resumes; held
  // while the screen is locked) + a best-effort Outlook reminder at the return time.
  start_break: (minutes: number) => Promise<{ break_until: string | null; reminder_set: boolean }>
  cancel_break: () => Promise<{ ok: boolean }>
  // Lock auto-suspend live indicator: current screen-lock state (cheap probe).
  screen_state: () => Promise<LockState>
  // Wake/sleep Windows Voice access listening (injects Alt+Shift+B host-side —
  // same chord both ways; the UI tracks state). Speak button wakes after focusing
  // the box; sending sleeps. {ok:false} off Windows / on failure (operator can
  // still drive Voice access from its own bar).
  start_voice_dictation: () => Promise<{ ok: boolean }>
  stop_voice_dictation: () => Promise<{ ok: boolean }>
  // In-app dictation (Moonshine Voice, the `[voice]` extra): the composer's mic
  // button. `dictation_status` is the mount-time availability probe; start/stop
  // run the on-device engine host-side, which streams text back as
  // `runspec:dictation_state` / `runspec:dictation_partial` /
  // `runspec:dictation_final` window events (DictationStateEvent /
  // DictationTextEvent). Finals are appended into the prompt box; nothing sends.
  dictation_status: () => Promise<DictationStatus>
  start_dictation: () => Promise<{ ok: boolean; session_id?: string; error?: string }>
  stop_dictation: () => Promise<{ ok: boolean; session_id: string | null }>
  // Settings → Voice. save validates (rejects the promise with the reason),
  // writes `[voice]` atomically, releases the resident engine when the model
  // selection changed or keep_loaded turned off, and returns the fresh config.
  get_voice_config: () => Promise<VoiceConfig>
  save_voice_config: (cfg: Omit<VoiceConfig, 'available' | 'reason' | 'model_loaded' | 'models'>) => Promise<VoiceConfig>
  list_voice_devices: () => Promise<VoiceDevice[]>
  get_trigger_board: () => Promise<TriggerCard[]>
  get_trigger_events: () => Promise<TriggerEvent[]>
  // Fire a trigger now with a synthetic event (the Triggers table's "Trigger now"
  // button) — test a trigger without waiting for a real event. `body` is the
  // pasted message (lands under the `body` payload key); `fields` are key=value
  // payload overrides. Bypasses match/rota/dedup for the named trigger.
  test_trigger: (name: string, body: string, fields: Record<string, string>) => Promise<{ ok: boolean; error?: string }>
  // Run a `source = "paste"` trigger now with `text` as its input — a REAL run
  // through the trigger's match/rota/dedup gates (unlike test_trigger). `fields`
  // are extra key=value payload. `skipped` explains a gated no-run.
  submit_paste_input: (name: string, text: string, fields: Record<string, string>) => Promise<{ ok: boolean; skipped?: string; error?: string }>
  // Claim the parked shared-mailbox email behind a trigger card (the Claim
  // button). Moves the item into the trigger's claim folder — the winning console
  // runs the action; a loser sees `owner`; a config error leaves it parked.
  claim_email: (cardId: string) => Promise<{ claimed: boolean; owner?: string; error?: string }>
  // Schedules mirror the trigger run inbox + event log with their own feeds.
  get_schedule_board: () => Promise<RunCard[]>
  get_schedule_events: () => Promise<TriggerEvent[]>
  // Cold-start snapshot for the work inbox: autonomy-gate confirms parked before
  // the view mounted (the live path only emits runspec:tool_confirm for later ones).
  get_pending_confirms: () => Promise<PendingConfirm[]>
  // ─────────────────────────────────────────────────────────────────────────
  // Send a draft an agent-mode trigger saved (the card's Send button).
  send_trigger_draft: (entryId: string) => Promise<{ ok: boolean; to?: string; subject?: string; error?: string }>
  // ── chat room (Rooms view) ─────────────────────────────────────────────────
  get_room_state: () => Promise<RoomsState>
  // Diagnose a room/queue's connection on demand (the real paramiko path), so
  // the operator sees the actual blocker instead of a buried traceback.
  test_room_connection: (host: string) => Promise<RoomConnTest>
  // The composer's one outbound path. The agent never reaches this — a proposed
  // reply only pre-fills the composer (runspec:room_draft); the human sends.
  // For a helpdesk thread, pass the conversation room (helpdesk/<queue>/<visitor>);
  // the bridge routes it via the agent connection staffing that queue.
  send_room_message: (room: string, text: string) => Promise<{ ok: boolean; error?: string }>
  // Customer teams (Settings → Teams) — managed over the live helpdesk room
  // connection. Fire-and-forget: the room's reply arrives as a runspec:teams event
  // (TeamsEvent). ok=false (with a reason) when no helpdesk room is connected.
  list_teams: () => Promise<{ ok: boolean; error?: string }>
  add_team: (name: string) => Promise<{ ok: boolean; error?: string }>
  remove_team: (slug: string) => Promise<{ ok: boolean; error?: string }>
  get_in_flight: () => Promise<InFlightRecord[]>
  get_today: (host: string, group: string) => Promise<TodaySummary | null>
  get_analytics: (hosts: string[], sinceDays?: number, runnable?: string) => Promise<AnalyticsData>
  // ── logs management ──────────────────────────────────────────────────────
  logs_status: (host: string, runnable?: string) => Promise<LogStatusVenv[]>
  logs_view: (host: string, group: string, runnable: string, since?: string, user?: string, run?: string) => Promise<LogViewResult>
  logs_prune: (host: string, group: string, runnable?: string, olderThan?: string, maxFiles?: number, maxTotalSize?: string, dryRun?: boolean, runAs?: string) => Promise<LogPruneResult>
  logs_compact: (host: string, group: string, runnable?: string, olderThan?: string, gzip?: boolean, dryRun?: boolean, runAs?: string) => Promise<LogCompactResult>
  share_venv_logs: (host: string, venv: string, group?: string, runAs?: string) => Promise<ShareLogsResult>
  // Runs index (runspec_runs.db) maintenance for one venv — drives core's
  // `runspec history stats|prune|reap|reindex --scope … --json`.
  // Shared (host cron) schedules on a host — one entry per venv (read-only).
  shared_schedules: (host: string) => Promise<SharedSchedulesVenv[]>
  history_db_stats: (host: string, group: string) => Promise<HistoryDbStats>
  history_db_maintain: (host: string, group: string, action: HistoryDbAction, olderThan?: string, maxRows?: number, runAs?: string) => Promise<HistoryDbResult>
  // Owner of a venv's dir — pre-fills the "Run as" field on the destructive Logs
  // actions (prune/compact/share). `self` is the effective (login/SSH) user, so the
  // UI can tell whether escalation is even needed. Empty owner for the ~/logs scope.
  venv_owner: (host: string, group: string) => Promise<{ ok: boolean; owner: string; self: string; error?: string }>
  generate_ssh_key: () => Promise<{ ok: boolean; public_key: string; key_path: string; committed: boolean; per_host: RotateHostResult[]; message: string }>
  rotate_ssh_key: () => Promise<{ ok: boolean; committed: boolean; public_key: string; key_path: string; per_host: RotateHostResult[]; message: string }>
  get_public_key: () => Promise<{ ok: boolean; public_key: string; message: string }>
  copy_ssh_id: (host: string, password: string) => Promise<{ ok: boolean; connected: boolean; message: string }>
  copy_ssh_id_all: (password: string, targets?: string[]) => Promise<{ ok: boolean; per_host: { host: string; ok: boolean; connected: boolean; error: string }[]; message: string }>
  puttygen_path: () => Promise<string>
  launch_puttygen: () => Promise<void>
  open_putty_url: (url: string) => Promise<void>
  browse_ssh_binary: () => Promise<string>
  browse_key_file: () => Promise<string>
  browse_folder: () => Promise<string>
  browse_sound_file: () => Promise<string>
  // ── terminal sessions ──────────────────────────────────────────────────────
  launch_terminal: (host: string) => Promise<void>
  launch_local_terminal: () => Promise<void>
  // ── dev tools ────────────────────────────────────────────────────────────
  is_debug_enabled: () => Promise<boolean>
  open_devtools: () => Promise<void>
  // Record a frontend error in the Python log file (so it isn't trapped in the
  // web view console). Best-effort; the proxy forwards uncaught bridge errors here.
  log_client_error: (context: string, message: string, detail?: unknown) => Promise<void>
  // Signal (once React has mounted) that first paint is done, so the backend can
  // start its heavy telemetry / MCP / sandbox bring-up without contending with the
  // webview loading the app bundle. Fire-and-forget from App's mount effect.
  notify_frontend_ready: () => Promise<void>
}

declare global {
  interface Window {
    pywebview?: { api: BridgeApi }
  }
}

// Use real pywebview bridge in the desktop app, mock in a plain browser.
// pywebview injects window.pywebview.api asynchronously — wait for the
// 'pywebviewready' event with a short timeout to fall back to mock when
// running directly in a browser (no pywebview present).
let _bridge: Promise<BridgeApi> | null = null

function resolveBridge(): Promise<BridgeApi> {
  if (_bridge) return _bridge
  _bridge = new Promise<BridgeApi>(resolve => {
    if (window.pywebview?.api) {
      resolve(window.pywebview.api)
      return
    }
    let settled = false
    const settle = (api: BridgeApi) => { if (!settled) { settled = true; resolve(api) } }

    window.addEventListener('pywebviewready', () => settle(window.pywebview!.api), { once: true })

    // Poll — pywebview may inject the api without firing the event on some platforms
    const poll = setInterval(() => {
      if (window.pywebview?.api) { clearInterval(poll); settle(window.pywebview.api) }
    }, 50)

    // Fall back to the mock only in a dev build (a plain browser with no pywebview,
    // e.g. `vite dev` opened in Chrome). In the packaged desktop app (production
    // build) pywebview is ALWAYS present — it can just be slow to inject
    // window.pywebview.api on a cold, AV-starved launch (the gui-scripts shortcut
    // can take 7–10s). Falling back to the mock there binds the whole UI to fake,
    // empty data — a blank screen while the real backend runs — so in production we
    // keep polling for the real api instead of ever settling to the mock.
    if (import.meta.env.DEV) {
      setTimeout(async () => {
        clearInterval(poll)
        if (!settled) {
          settled = true
          const { mockApi } = await import('./mock')
          resolve(mockApi)
        }
      }, 2500)
    }
  })
  return _bridge
}

// Forward a bridge error into the Python log file (best-effort). Guarded so the
// very failure we're surfacing — an older Python that doesn't expose
// `log_client_error` — can't recurse or throw; if it's missing we just skip, and
// the descriptive error below still shows in the Dev tab.
let _forwardingClientError = false
function forwardClientError(name: string, err: unknown): void {
  if (_forwardingClientError || name === 'log_client_error') return
  const fn = (window.pywebview?.api as Partial<BridgeApi> | undefined)?.log_client_error
  if (typeof fn !== 'function') return
  _forwardingClientError = true
  try {
    void fn('bridge-call', `${name}: ${err instanceof Error ? err.message : String(err)}`)
  } catch { /* logging must never break the UI */ }
  finally { _forwardingClientError = false }
}

// Resolve a bridge method, tolerating pywebview's async startup.
//
// pywebview 6.x injects `window.pywebview.api` and then attaches its methods
// **asynchronously** — so a mount-time call can land *before* a given method
// exists on the api object yet (the failures cluster at startup and vary in
// count run-to-run, the signature of a race, not a missing method). We read the
// **live** api each time (the object may be replaced/extended after the first
// ready-probe) and, on the real pywebview path, briefly retry while the method
// is still being attached. A method that's still absent after the api has had
// time to settle is a genuine "not exposed" (a real version mismatch).
const _BRIDGE_READY_RETRIES = 120  // × 75ms ≈ 9s max wait, only while a method is
                                   // still attaching (pywebview 6.x async wiring);
                                   // generous for a cold, AV-starved shortcut launch
const _BRIDGE_READY_DELAY_MS = 75

async function resolveMethod(
  name: string,
  resolved: BridgeApi,
): Promise<(...a: unknown[]) => unknown> {
  const fromLive = (): ((...a: unknown[]) => unknown) | undefined => {
    // Plain browser (mock): no window.pywebview — use the resolved mock, which
    // always has every method. Real desktop: read the live pywebview api.
    const api =
      (window.pywebview?.api as Record<string, unknown> | undefined) ??
      (resolved as unknown as Record<string, unknown>)
    const fn = api[name]
    return typeof fn === 'function' ? (fn as (...a: unknown[]) => unknown) : undefined
  }
  let fn = fromLive()
  // Only retry on the real pywebview path (the mock has every method already).
  for (let i = 0; !fn && window.pywebview?.api && i < _BRIDGE_READY_RETRIES; i++) {
    await new Promise(r => setTimeout(r, _BRIDGE_READY_DELAY_MS))
    fn = fromLive()
  }
  if (!fn) {
    throw new Error(
      `Bridge method "${name}" is not exposed by the Python API (still missing after ` +
      `waiting for the bridge to settle) — likely a version mismatch between the ` +
      `installed console backend and this UI bundle.`,
    )
  }
  return fn
}

export const bridge: BridgeApi = new Proxy({} as BridgeApi, {
  get(_target, prop) {
    // Symbol probes (thenable checks, etc.) must not be mistaken for methods —
    // returning a function here would make the Proxy look like a Promise.
    if (typeof prop === 'symbol') return undefined
    const name = String(prop)
    return async (...args: unknown[]) => {
      const entryId = recordCallStart(name, args)
      const t0 = performance.now()
      try {
        const resolved = await resolveBridge()
        const fn = await resolveMethod(name, resolved)
        const result = await fn(...args)
        recordCallEnd(entryId, result, performance.now() - t0)
        return result
      } catch (err) {
        recordCallError(entryId, err, performance.now() - t0)
        forwardClientError(name, err)
        throw err
      }
    }
  },
})
