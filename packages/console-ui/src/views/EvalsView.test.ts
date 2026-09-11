import { describe, it, expect } from 'vitest'
import { evalCatMeta, EVAL_CAT_META } from './EvalsView'

describe('evalCatMeta', () => {
  it('maps a known verdict category to a colour + label', () => {
    expect(evalCatMeta('wrong_tool')).toEqual({ color: 'red', label: 'Wrong tool' })
    expect(evalCatMeta('pass')).toEqual({ color: 'green', label: 'Pass' })
  })

  it('covers the arg-bounce buckets shared with improve.classify_bounce', () => {
    for (const cat of ['missing_required', 'unknown_arg', 'bad_choice', 'out_of_range', 'bad_command']) {
      expect(EVAL_CAT_META[cat]).toBeDefined()
      expect(EVAL_CAT_META[cat].label).toBeTruthy()
    }
  })

  it('falls back to the raw category for an unknown bucket', () => {
    expect(evalCatMeta('something_new')).toEqual({ color: 'default', label: 'something_new' })
  })
})
