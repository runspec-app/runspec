/** What a run card shows as its args: the runnable's `password` args masked
 *  (globals and the chosen command's), and an MCP / rule-trigger card's
 *  `detail.args`. */
import { describe, it, expect, vi } from 'vitest'

import { cardCallArgs, secretArgNames } from './ConsoleView'
import { MASKED_ARG } from '../components/OutputPanel'
import type { Runnable, TriggerCard } from '../bridge'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

const arg = (name: string, type = 'str') => ({ name, type, required: false })

describe('secretArgNames', () => {
  const r: Runnable = {
    name: 'jira', group: 'venv', host: 'local',
    args: [arg('url'), arg('pw', 'password')],
    commands: {
      admin: {
        name: 'admin', group: 'venv', host: 'local', args: [arg('token2', 'password')],
        commands: { reset: { name: 'reset', group: 'venv', host: 'local', args: [arg('newpw', 'password'), arg('user')] } },
      },
    },
  }
  it('collects password args from every level of the command path', () => {
    expect(secretArgNames(r, ['admin', 'reset'])).toEqual(['pw', 'token2', 'newpw'])
    expect(secretArgNames(r)).toEqual(['pw'])
    expect(secretArgNames(r, ['nope'])).toEqual(['pw'])
  })
})

describe('cardCallArgs', () => {
  const card = (detail: Record<string, unknown>): TriggerCard => ({
    card_id: 'c', trigger: 'jira', source: 'mcp', event_type: 'request', summary: '/jira',
    column: 'running', detail, created_at: 0,
  })
  it('reads detail.args with secrets masked', () => {
    expect(cardCallArgs(card({ args: { issue: 'OPS-1', password: 'x' } })))
      .toEqual({ issue: 'OPS-1', password: MASKED_ARG })
  })
  it('is undefined when the card has no args', () => {
    expect(cardCallArgs(card({}))).toBeUndefined()
  })
})
