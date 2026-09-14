/**
 * features.ts — the pure helpers behind the `[console] ai` off-switch UI gate.
 * With AI on every list is returned untouched (order and identity); with AI off
 * exactly the AI-only entries drop and the fleet ones (incl. the MCP Server
 * settings tab and the Console/Forms/History views) stay.
 */
import { describe, it, expect } from 'vitest'

import {
  visibleNavTabs, visibleSettingsTabs, visibleScheduleActions,
  AI_ONLY_VIEWS, AI_ONLY_SETTINGS, AI_SCHEDULE_ACTIONS, DEFAULT_FEATURES,
} from './features'

const NAV = ['console', 'history', 'logs', 'analytics', 'improve', 'evals', 'specs', 'forms', 'schedules', 'triggers', 'rota', 'dev']
  .map(key => ({ key }))
const SETTINGS = ['ssh', 'llm', 'jumpHosts', 'localVenvs', 'agentTools', 'runbooks', 'savedRuns', 'selfService', 'filters', 'repos', 'credentials', 'denylist', 'mcp', 'mcpServer', 'configSync', 'mailbox', 'teams', 'sounds', 'chatHistory', 'telemetry']
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

  it('hides only Improve and Evals; the Console (run output) tab stays', () => {
    const keys = visibleNavTabs(NAV, off).map(t => t.key)
    expect(keys).toEqual(['console', 'history', 'logs', 'analytics', 'specs', 'forms', 'schedules', 'triggers', 'rota', 'dev'])
    expect(keys).not.toContain('improve')
    expect(keys).not.toContain('evals')
    expect([...AI_ONLY_VIEWS]).toEqual(['improve', 'evals'])
  })

  it('hides the agent Settings tabs but keeps MCP Server and the fleet tabs', () => {
    const keys = visibleSettingsTabs(SETTINGS, off).map(t => t.key)
    for (const k of AI_ONLY_SETTINGS) expect(keys).not.toContain(k)
    expect(keys).toEqual(['ssh', 'jumpHosts', 'localVenvs', 'savedRuns', 'selfService', 'filters', 'repos', 'credentials', 'denylist', 'mcpServer', 'configSync', 'mailbox', 'teams', 'sounds'])
  })

  it('drops the agent-turn schedule actions (mirrors the backend set)', () => {
    const values = visibleScheduleActions(ACTIONS, off).map(o => o.value)
    expect(values).toEqual(['runnable', 'saved_run', 'event'])
    expect([...AI_SCHEDULE_ACTIONS].sort()).toEqual(['eval', 'prompt', 'runbook'])
  })
})
