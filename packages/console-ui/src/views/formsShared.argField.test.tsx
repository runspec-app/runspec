/**
 * The run-dialog field header (ArgFieldHeader) and description (ArgDescription):
 * the arg name leads, the type is one muted word, "required" is the only marker
 * (optional is unmarked), and a description wraps to two lines instead of
 * clipping to one.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import type { ArgDef } from '../bridge'
import { ArgDescription, ArgError, ArgFieldHeader, argTypeLabel } from './formsShared'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'workers', type: 'int', ...over } as ArgDef)

describe('argTypeLabel', () => {
  it('uses the raw type by default', () => {
    expect(argTypeLabel(arg({ type: 'int' }))).toBe('int')
  })
  it('says "secret" for a password arg', () => {
    expect(argTypeLabel(arg({ type: 'password' }))).toBe('secret')
  })
  it('prefers a UI hint that says more than the type', () => {
    expect(argTypeLabel(arg({ type: 'path', ui: 'directory' }))).toBe('folder')
    expect(argTypeLabel(arg({ type: 'str', ui: 'date' }))).toBe('date')
    expect(argTypeLabel(arg({ type: 'str', ui: 'host' }))).toBe('host')
  })
})

describe('ArgFieldHeader', () => {
  it('shows the --name, the type word and a required marker', () => {
    render(<ArgFieldHeader arg={arg({ required: true })} />)
    expect(screen.getByText('--workers')).toBeTruthy()
    expect(screen.getByText('int')).toBeTruthy()
    expect(screen.getByText('required')).toBeTruthy()
  })

  it('leaves an optional arg unmarked', () => {
    render(<ArgFieldHeader arg={arg({ required: false, default: 4 })} />)
    expect(screen.queryByText('required')).toBeNull()
    expect(screen.queryByText('optional')).toBeNull()
  })

  it('names an env-var default', () => {
    render(<ArgFieldHeader arg={arg({ type: 'str', default: '$DEPLOY_TAG' })} />)
    expect(screen.getByText('= $DEPLOY_TAG')).toBeTruthy()
  })

  it('takes a bare prefix for runbook params', () => {
    render(<ArgFieldHeader arg={arg({})} prefix="" />)
    expect(screen.getByText('workers')).toBeTruthy()
  })
})

describe('ArgDescription', () => {
  it('clamps to two lines rather than one', () => {
    render(<ArgDescription text="Number of parallel workers to spawn." />)
    const el = screen.getByText('Number of parallel workers to spawn.') as HTMLElement
    expect(el.style.webkitLineClamp).toBe('2')
  })
})

describe('ArgError', () => {
  it('renders nothing without a message', () => {
    const { container } = render(<ArgError />)
    expect(container.textContent).toBe('')
  })
  it('renders the message', () => {
    render(<ArgError text="Required" />)
    expect(screen.getByText('Required')).toBeTruthy()
  })
})
