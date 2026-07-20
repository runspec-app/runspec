import { describe, it, expect } from 'vitest'
import { emptyAdHoc, adHocValid, adHocToEndpoint } from './AdHocHostFields'

describe('AdHocHostFields helpers', () => {
  it('requires only a target — a jump is optional (jumpless direct connect)', () => {
    expect(adHocValid(emptyAdHoc())).toBe(false)
    expect(adHocValid({ ...emptyAdHoc(), jump: 'bastion' })).toBe(false)  // jump alone isn't a host
    expect(adHocValid({ ...emptyAdHoc(), target: 'db.internal' })).toBe(true)  // target alone is enough
    expect(adHocValid({ ...emptyAdHoc(), jump: 'bastion', target: 'db.internal' })).toBe(true)
  })

  it('builds an ad-hoc endpoint descriptor, trimming and dropping empties', () => {
    const ep = adHocToEndpoint({ jump: 'bastion', target: '  db.internal ', user: ' svc ', credentialId: '', password: '' })
    expect(ep).toEqual({ kind: 'adhoc', host: 'db.internal', jump: 'bastion', user: 'svc', password: undefined, credential_id: undefined })
  })

  it('carries a credential id and a one-shot password when present', () => {
    const ep = adHocToEndpoint({ jump: 'bastion', target: 'db', user: '', credentialId: 'cred-1', password: 'pw' })
    expect(ep.credential_id).toBe('cred-1')
    expect(ep.password).toBe('pw')
  })
})
