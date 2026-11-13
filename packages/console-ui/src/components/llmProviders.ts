/**
 * Settings → Model / API: the provider list and per-provider settings.
 *
 * The console runs one provider at a time from the top-level `[llm]` table. So
 * that switching the Provider dropdown (say from a company adapter plugin to
 * GitHub Copilot and back) doesn't lose the other provider's URL, token command
 * or model list, the settings that belong to a provider are parked under
 * `[llm.saved_providers.<name>]` while it isn't the active one, and brought back
 * when it is picked again. The backend only ever reads the top-level keys.
 */

export type ProviderKind = 'builtin' | 'plugin' | 'path'

export interface ProviderOption {
  value: string
  label: string
  // Older backends send only value/label; those are treated as built-ins.
  kind?: ProviderKind
  // Extra [llm] settings a plugin adapter declares (kept per provider too).
  keys?: string[]
}

export const SAVED_PROVIDERS_KEY = 'saved_providers'

// [llm] keys that describe one provider's connection.
export const PROVIDER_KEYS = [
  'api_key', 'api_key_command', 'api_key_ttl_ms',
  'model', 'models', 'utility_model',
  'base_url', 'aws_region', 'aws_access_key', 'aws_secret_key', 'aws_session_token',
  'auth_header', 'auth_scheme', 'input_messages_key', 'input_tools_key', 'tools_in_config',
  'max_tokens_key', 'max_tokens_in', 'effort_key', 'effort_in', 'cache_markers', 'stream_events',
  'cache_ttl', 'fallbacks', 'thinking_display',
  'cli_path',
]

// Console-wide [llm] settings: never parked with a provider, even when a
// plugin's adapter declares them.
const SHARED_KEYS = new Set([
  'provider', 'system', 'system_append', 'tool_mode', 'effort', 'max_tokens',
  'max_continuations', 'max_tool_rounds', 'max_network_retries', 'max_self_recoveries',
  'max_parallel_tools', 'tool_index', 'tool_index_limit', 'defer_builtins', 'model_limits',
  SAVED_PROVIDERS_KEY,
])

/** The [llm] keys that move with `provider` when it is switched. */
export function providerKeys(provider: string, options: ProviderOption[]): string[] {
  const extra = options.find(o => o.value === provider)?.keys ?? []
  return [...new Set([...PROVIDER_KEYS, ...extra])].filter(k => !SHARED_KEYS.has(k))
}

type Llm = Record<string, unknown>

function savedProviders(llm: Llm): Record<string, Llm> {
  const raw = llm[SAVED_PROVIDERS_KEY]
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: Record<string, Llm> = {}
  for (const [name, value] of Object.entries(raw as Record<string, unknown>)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) out[name] = { ...(value as Llm) }
  }
  return out
}

/**
 * The `[llm]` table after switching from `from` to `to`: `from`'s settings are
 * parked under `saved_providers.<from>`, `to`'s parked settings (if any) come
 * back to the top level, and `provider` is set (removed when `to` is blank).
 */
export function switchProvider(
  llm: Llm,
  from: string,
  to: string,
  keysFor: (provider: string) => string[],
): Llm {
  const out: Llm = { ...llm }
  const saved = savedProviders(llm)
  if (from && from !== to) {
    const park: Llm = {}
    for (const k of keysFor(from)) {
      if (k in out) {
        park[k] = out[k]
        delete out[k]
      }
    }
    if (Object.keys(park).length) saved[from] = park
    else delete saved[from]
  }
  if (to && from !== to) {
    Object.assign(out, saved[to] ?? {})
    delete saved[to]
  }
  if (to) out.provider = to
  else delete out.provider
  if (Object.keys(saved).length) out[SAVED_PROVIDERS_KEY] = saved
  else delete out[SAVED_PROVIDERS_KEY]
  return out
}

/** Dropdown groups: built-ins first, then providers installed packages add. */
export function groupProviderOptions(
  options: ProviderOption[],
): { label: string; options: ProviderOption[] }[] {
  const builtin = options.filter(o => (o.kind ?? 'builtin') === 'builtin')
  const plugin = options.filter(o => o.kind === 'plugin')
  const path = options.filter(o => o.kind === 'path')
  const groups: { label: string; options: ProviderOption[] }[] = []
  if (builtin.length) groups.push({ label: 'Built in', options: builtin })
  if (plugin.length) groups.push({ label: 'From installed packages', options: plugin })
  if (path.length) groups.push({ label: 'From config (module path)', options: path })
  return groups
}

export interface CopilotModel {
  id: string
  name: string
  enabled: boolean
  multiplier?: number | null
}

/** The model ids a Copilot sign-in check offers to add (enabled ones only). */
export function copilotModelIds(models: CopilotModel[] | undefined): string[] {
  return (models ?? []).filter(m => m.enabled).map(m => m.id)
}
