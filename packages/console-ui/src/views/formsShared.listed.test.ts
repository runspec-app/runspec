import { describe, it, expect } from 'vitest'
import { initialFormValues, parseListedText } from './formsShared'
import type { ArgDef } from '../bridge'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

// runspec core before 0.84.2 recorded a multiple choice as str(['Open']) ==
// "['Open']"; Edit & rerun prefilled that text as one pick and the run failed.
describe('parseListedText', () => {
  it("reads Python's str() of a list", () => {
    expect(parseListedText("['Open']")).toEqual(['Open'])
    expect(parseListedText("['Open', 'Closed']")).toEqual(['Open', 'Closed'])
    expect(parseListedText('["it\'s", \'a\\\\b\']')).toEqual(["it's", 'a\\b'])
    expect(parseListedText("['caf\\xe9']")).toEqual(['café'])
    expect(parseListedText('[1, 2]')).toEqual(['1', '2'])
    expect(parseListedText('[]')).toEqual([])
  })

  it('reads JSON lists', () => {
    expect(parseListedText('["Open","Closed"]')).toEqual(['Open', 'Closed'])
  })

  it('rejects anything that is not a list of scalars', () => {
    expect(parseListedText('Open')).toBeNull()
    expect(parseListedText("['Open'")).toBeNull()
    expect(parseListedText("['a' 'b']")).toBeNull()
    expect(parseListedText("[['a']]")).toBeNull()
    expect(parseListedText("[{'a': 1}]")).toBeNull()
    expect(parseListedText("['unterminated]")).toBeNull()
  })
})

describe('initialFormValues with listed text from History', () => {
  const args: ArgDef[] = [
    arg({ name: 'state', type: 'choice', options: ['Open', 'Closed', 'Pending'], multiple: true }),
    arg({ name: 'level', type: 'choice', options: ['low', 'high'] }),
    arg({ name: 'note', type: 'str' }),
  ]

  it('turns a multiple choice back into its picks', () => {
    expect(initialFormValues(args, { state: "['Open']" }).state).toEqual(['Open'])
    expect(initialFormValues(args, { state: "['Open', 'Pending']" }).state).toEqual(['Open', 'Pending'])
  })

  it('leaves real lists and plain values alone', () => {
    expect(initialFormValues(args, { state: ['Closed'] }).state).toEqual(['Closed'])
    expect(initialFormValues(args, { level: 'high' }).level).toBe('high')
  })

  it("unwraps a single choice written as a one-item list of an option", () => {
    expect(initialFormValues(args, { level: "['high']" }).level).toBe('high')
    expect(initialFormValues(args, { level: "['nope']" }).level).toBe("['nope']")
  })

  it('never touches a plain text arg', () => {
    expect(initialFormValues(args, { note: "['Open']" }).note).toBe("['Open']")
  })
})
