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

// Settings tabs that configure the agent (SettingsDrawer `items` keys). The
// MCP *Server* tab stays — it gates an external coding agent's fleet calls with
// no model inside the console, the non-AI-inside demo.
export const AI_ONLY_SETTINGS: ReadonlySet<string> = new Set([
  'llm', 'agentTools', 'runbooks', 'mcp', 'chatHistory', 'telemetry',
])

/** The nav tabs to render for the active feature set (order preserved). */
export function visibleNavTabs<T extends { key: string }>(tabs: T[], features: Features): T[] {
  return features.ai ? tabs : tabs.filter(t => !AI_ONLY_VIEWS.has(t.key))
}

/** The Settings tabs to render for the active feature set (order preserved). */
export function visibleSettingsTabs<T extends { key: string }>(tabs: T[], features: Features): T[] {
  return features.ai ? tabs : tabs.filter(t => !AI_ONLY_SETTINGS.has(t.key))
}

// Schedule actions that run an agent turn — mirrors the backend's
// `sources.schedule.AI_SCHEDULE_ACTIONS` (refused there when AI is off).
export const AI_SCHEDULE_ACTIONS: ReadonlySet<string> = new Set(['prompt', 'runbook', 'eval'])

/** The schedule-action options to offer (order preserved). */
export function visibleScheduleActions<T extends { value: string }>(options: T[], features: Features): T[] {
  return features.ai ? options : options.filter(o => !AI_SCHEDULE_ACTIONS.has(o.value))
}

// What the command bar says instead of sending a chat turn when AI is off.
export const AI_OFF_COMPOSER_HINT = 'AI is off in this console — type / for runnables or > for saved runs.'
