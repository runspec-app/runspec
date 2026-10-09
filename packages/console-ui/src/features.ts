/**
 * features.ts — console feature flags the frontend reads once at mount.
 *
 * Today there is one: `ai` — the `[console] ai` off-switch. With it off the
 * console is a non-AI fleet tool: the AI-only tabs and Settings tabs are hidden,
 * the command bar no longer routes plain text to the assistant, and the
 * Triggers/Schedules editors offer only rule / runnable actions. The backend
 * refuses every AI path on its own (see docs/design/console-ai-off-switch.md);
 * this context only keeps the UI honest about what will work.
 *
 * The flag is read via `bridge.ai_enabled()` in App.tsx and provided through
 * `FeaturesContext`; the default (`ai: true`) is what an older backend or a
 * failed call yields, so the default look is unchanged.
 */
import { createContext, useContext } from 'react'

export interface Features {
  ai: boolean
}

export const DEFAULT_FEATURES: Features = { ai: true }

export const FeaturesContext = createContext<Features>(DEFAULT_FEATURES)

export const useFeatures = () => useContext(FeaturesContext)

// Top-level views that exist only for the agent (App.tsx `navTabs` keys).
export const AI_ONLY_VIEWS: ReadonlySet<string> = new Set(['improve', 'evals'])

// Views hidden when AI is off but not *strictly* agent-only. Triggers lean on
// the agent for most actions (and a rule-only trigger doesn't earn a top-level
// tab here), and Rota is presence governance for reactive/agent behaviour —
// with no agent turns there is nothing for it to gate. This is UI curation, not
// a hard gate: the backend still runs any rule trigger a seed ships, and the
// rota gate goes inert when AI is off (see bridge_triggers `_build_*_ok`).
export const AI_OFF_HIDDEN_VIEWS: ReadonlySet<string> = new Set(['triggers', 'rota'])

// Settings tabs that configure the agent (SettingsDrawer `items` keys). The
// MCP *Server* tab stays — it gates an external coding agent's fleet calls with
// no model inside the console, the non-AI-inside demo. **Tool visibility stays
// too** — its globs filter the "agent" surface, which includes the MCP server's
// published tool list (and, via "hide everywhere", the Forms tab + catalogs), so
// an AI-off console hosting the MCP server still needs it to curate what an
// external agent sees. **Telemetry stays too**: metrics cover runnable runs
// (Forms / MCP server / schedules / triggers), not just agent activity, so an
// AI-off fleet console still exports them — the tab renders metrics-only when AI
// is off (tracing is agent-turn-only, so its controls hide). See TelemetryTab.
export const AI_ONLY_SETTINGS: ReadonlySet<string> = new Set([
  'llm', 'runbooks', 'mcp', 'chatHistory',
  // Dictation fills the chat prompt — with no assistant there is nothing to
  // dictate to (the backend refuses start_dictation too).
  'voice',
])

/** The nav tabs to render for the active feature set (order preserved). */
export function visibleNavTabs<T extends { key: string }>(tabs: T[], features: Features): T[] {
  return features.ai
    ? tabs
    : tabs.filter(t => !AI_ONLY_VIEWS.has(t.key) && !AI_OFF_HIDDEN_VIEWS.has(t.key))
}

// Settings tabs hidden when AI is off but not strictly agent-only — the Settings
// twin of AI_OFF_HIDDEN_VIEWS. Mailbox feeds the `new_mail` trigger source, and
// with no agent to react to mail (and the Triggers editor hidden) there is nothing
// to configure here. The MCP server doesn't change that: it only answers calls an
// external agent makes, it never pushes mail events out to one. UI curation, not
// a gate: a seeded [mailbox] still drives rule triggers and the helpdesk identity.
export const AI_OFF_HIDDEN_SETTINGS: ReadonlySet<string> = new Set(['mailbox'])

/** The Settings tabs to render for the active feature set (order preserved). */
export function visibleSettingsTabs<T extends { key: string }>(tabs: T[], features: Features): T[] {
  return features.ai
    ? tabs
    : tabs.filter(t => !AI_ONLY_SETTINGS.has(t.key) && !AI_OFF_HIDDEN_SETTINGS.has(t.key))
}

// Schedule actions that run an agent turn — mirrors the backend's
// `sources.schedule.AI_SCHEDULE_ACTIONS` (refused there when AI is off).
export const AI_SCHEDULE_ACTIONS: ReadonlySet<string> = new Set(['prompt', 'runbook', 'eval'])

// 'event' emits a matchable event with no agent involvement, so the backend
// never refuses it — but its only consumer is a source="schedule" Trigger, and
// Triggers are hidden when AI is off (AI_OFF_HIDDEN_VIEWS). A dead option, so we
// curate it out of the Schedules picker too. Frontend-only, not a backend gate.
export const AI_OFF_HIDDEN_SCHEDULE_ACTIONS: ReadonlySet<string> = new Set(['event'])

/** The schedule-action options to offer (order preserved). */
export function visibleScheduleActions<T extends { value: string }>(options: T[], features: Features): T[] {
  return features.ai
    ? options
    : options.filter(
        o => !AI_SCHEDULE_ACTIONS.has(o.value) && !AI_OFF_HIDDEN_SCHEDULE_ACTIONS.has(o.value),
      )
}

// What the command bar says instead of sending a chat turn when AI is off. It
// describes what the bar *does* accept — deliberately without naming AI or how
// to re-enable it (a locked-down console shouldn't advertise the switch).
export const AI_OFF_COMPOSER_HINT = 'Type / for runnables or > for saved runs.'
