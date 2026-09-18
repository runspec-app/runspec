/**
 * features.ts — the pure helpers behind the `[console] ai` off-switch UI gate.
 * With AI on every list is returned untouched (order and identity); with AI off
 * exactly the AI-only entries drop and the fleet ones (incl. the MCP Server
 * settings tab and the Console/Forms/History views) stay.
 */
import { describe, it, expect } from 'vitest'

import {
  visibleNavTabs, visibleSettingsTabs, visibleScheduleActions,
  AI_ONLY_VIEWS, AI_OFF_HIDDEN_VIEWS, AI_ONLY_SETTINGS, AI_SCHEDULE_ACTIONS,
  AI_OFF_HIDDEN_SCHEDULE_ACTIONS, DEFAULT_FEATURES,
} from './features'

const NAV = ['console', 'history', 'logs', 'analytics', 'improve', 'evals', 'specs', 'forms', 'schedules', 'triggers', 'rota', 'dev']
  .map(key => ({ key }))
const SETTINGS = ['ssh', 'llm', 'jumpHosts', 'localVenvs', 'agentTools', 'runbooks', 'savedRuns', 'selfService', 'filters', 'repos', 'credentials', 'denylist', 'mcp', 'mcpServer', 'configSync', 'mailbox', 'teams', 'sounds', 'voice', 'chatHistory', 'telemetry']
  .map(key => ({ key }))
const ACTIONS = ['runnable', 'saved_run', 'prompt', 'runbook', 'eval', 'event'].map(value => ({ value }))

describe('features — AI on (default)', () => {
  it('returns every list untouched', () => {
    expect(DEFAULT_FEATURES.ai).toBe(true)
    expect(visibleNavTabs(NAV, DEFAULT_FEATURES)).toBe(NAV)
    expect(visibleSettingsTabs(SETTINGS, DEFAULT_FEATURES)).toBe(SETTINGS)
    expect(visibleScheduleActions(ACTIONS, DEFAULT_FEATURES)).toBe(ACTIONS)
  })
})

describe('features — AI off', () => {
  const off = { ai: false }

  it('hides Improve, Evals, Triggers and Rota; the Console (run output) tab stays', () => {
    const keys = visibleNavTabs(NAV, off).map(t => t.key)
    expect(keys).toEqual(['console', 'history', 'logs', 'analytics', 'specs', 'forms', 'schedules', 'dev'])
    for (const k of ['improve', 'evals', 'triggers', 'rota']) expect(keys).not.toContain(k)
    expect([...AI_ONLY_VIEWS]).toEqual(['improve', 'evals'])
    expect([...AI_OFF_HIDDEN_VIEWS]).toEqual(['triggers', 'rota'])
  })

  it('hides the agent Settings tabs but keeps MCP Server, Telemetry, Tool visibility, and the fleet tabs', () => {
    const keys = visibleSettingsTabs(SETTINGS, off).map(t => t.key)
    for (const k of AI_ONLY_SETTINGS) expect(keys).not.toContain(k)
    // Telemetry stays — metrics cover runnable runs, so it renders metrics-only
    // when AI is off. Tool visibility (agentTools) stays too — its globs filter
    // the MCP server's published tool list (+ Forms/catalogs via "hide
    // everywhere"), so it is meaningful with no embedded agent.
    expect(keys).toEqual(['ssh', 'jumpHosts', 'localVenvs', 'agentTools', 'savedRuns', 'selfService', 'filters', 'repos', 'credentials', 'denylist', 'mcpServer', 'configSync', 'mailbox', 'teams', 'sounds', 'telemetry'])
    expect(AI_ONLY_SETTINGS.has('agentTools')).toBe(false)
    expect(AI_ONLY_SETTINGS.has('voice')).toBe(true)
  })

  it('drops the agent-turn schedule actions plus the dead event action', () => {
    const values = visibleScheduleActions(ACTIONS, off).map(o => o.value)
    // prompt/runbook/eval (agent turns) AND event (its only consumer, a
    // source="schedule" Trigger, is hidden when AI is off) all drop.
    expect(values).toEqual(['runnable', 'saved_run'])
    expect([...AI_SCHEDULE_ACTIONS].sort()).toEqual(['eval', 'prompt', 'runbook'])
    expect([...AI_OFF_HIDDEN_SCHEDULE_ACTIONS]).toEqual(['event'])
  })
})
