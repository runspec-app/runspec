import { describe, it, expect, beforeEach } from 'vitest'
import type { Host } from '../../bridge'
import { emptyAdHoc } from '../AdHocHostFields'
import {
  planCopy, copyButtonLabel, fanoutCandidates, draftFromCopy, panesForSaved, nextSelection,
  joinPath, dirName, baseName, lastFilterFor, rememberFilter, targetKey, endpointOf, relaySource,
  type PaneTarget,
} from './filesModel'

const local: PaneTarget = { kind: 'local' }
const app1: PaneTarget = { kind: 'host', host: 'app-01' }
const app2: PaneTarget = { kind: 'host', host: 'app-02' }
const adhoc: PaneTarget = { kind: 'adhoc', adhoc: { ...emptyAdHoc(), target: 'old-box', jump: 'app-01', user: 'ops' } }

const HOSTS = [
  { name: 'local', connected: true },
  { name: 'app-01', connected: true, group: 'prod' },
  { name: 'app-02', connected: true, group: 'prod' },
  { name: 'app-03', connected: false, group: 'prod' },
  { name: 'web-01', connected: true, group: 'web' },
] as Host[]

describe('planCopy', () => {
  it('maps the two panes to the right copy', () => {
    expect(planCopy(app1, local)).toEqual({ kind: 'download' })
    expect(planCopy(local, app1)).toEqual({ kind: 'upload' })
    expect(planCopy(local, app1, ['app-02'])).toEqual({ kind: 'fanout' })
    expect(planCopy(local, adhoc)).toEqual({ kind: 'upload' })
    expect(planCopy(adhoc, local)).toEqual({ kind: 'download' })
    expect(planCopy(app1, app2)).toEqual({ kind: 'relay' })
    expect(planCopy(adhoc, app2)).toEqual({ kind: 'relay' })
  })
  it('refuses local to local and a remote copy into an ad-hoc host', () => {
    expect(planCopy(local, local).error).toMatch(/Both panes/)
    expect(planCopy(app1, adhoc).error).toMatch(/only from this computer/)
  })
  it('labels the button', () => {
    expect(copyButtonLabel('fanout', 3)).toBe('Copy to 3 hosts')
    expect(copyButtonLabel('relay', 1)).toBe('Relay')
    expect(copyButtonLabel('download', 1)).toBe('Copy')
  })
})

describe('endpoints', () => {
  it('builds bridge endpoints and relay sources', () => {
    expect(endpointOf(local)).toBe('local')
    expect(endpointOf(app1)).toBe('app-01')
    expect(endpointOf(adhoc)).toMatchObject({ kind: 'adhoc', host: 'old-box', jump: 'app-01', user: 'ops' })
    expect(relaySource(app1)).toEqual({ kind: 'host', host: 'app-01' })
    expect(relaySource(local)).toBeNull()
  })
  it('keys targets by machine', () => {
    expect(targetKey(app1)).toBe('host:app-01')
    expect(targetKey(adhoc)).toBe('adhoc:app-01>ops@old-box')
  })
})

describe('fanoutCandidates', () => {
  it('lists the other connected hosts in the group', () => {
    expect(fanoutCandidates('app-01', HOSTS)).toEqual(['app-02'])
    expect(fanoutCandidates('web-01', HOSTS)).toEqual([])
  })
})

describe('paths', () => {
  it('joins and splits POSIX and Windows paths', () => {
    expect(joinPath('/var/log', 'a.log')).toBe('/var/log/a.log')
    expect(joinPath('/', 'etc')).toBe('/etc')
    expect(joinPath('C:\\Users\\jason', 'x.txt')).toBe('C:\\Users\\jason\\x.txt')
    expect(dirName('/var/log/a.log')).toBe('/var/log')
    expect(dirName('/a.log')).toBe('/')
    expect(dirName('C:\\x.txt')).toBe('C:\\')
    expect(baseName('C:\\Users\\x.txt')).toBe('x.txt')
  })
})

describe('remembered filter', () => {
  beforeEach(() => localStorage.clear())
  it('is per machine and folder', () => {
    expect(lastFilterFor(app1, '/var/log')).toBe('')
    rememberFilter(app1, '/var/log', 'errors')
    expect(lastFilterFor(app1, '/var/log')).toBe('errors')
    expect(lastFilterFor(app2, '/var/log')).toBe('')
    rememberFilter(app1, '/var/log', '')
    expect(lastFilterFor(app1, '/var/log')).toBe('')
  })
})

describe('draftFromCopy', () => {
  it('drafts a pull with the filter and new name', () => {
    expect(draftFromCopy({
      src: app1, dst: local, paths: ['/var/log/app.log'], destDir: '/ws', hosts: HOSTS,
      filter: 'errors', filterArg: '', destName: 'app-errors.log', workspacePath: '/ws',
    })).toEqual({
      id: '', paths: ['/var/log/app.log'], host: 'app-01',
      filter: 'errors', filter_arg: undefined, dest_dir: undefined, dest_name: 'app-errors.log',
    })
  })
  it('keeps a local folder other than the workspace', () => {
    expect(draftFromCopy({ src: app1, dst: local, paths: ['/a', '/b'], destDir: 'D:\\logs', hosts: HOSTS, workspacePath: '/ws' }))
      .toMatchObject({ dest_dir: 'D:\\logs', dest_name: undefined })
  })
  it('drafts an upload to a whole group, and refuses a hand-picked subset', () => {
    expect(draftFromCopy({ src: local, dst: app1, paths: ['/x'], destDir: '/etc/app', hosts: HOSTS.filter(h => h.name !== 'app-03'), alsoTo: ['app-02'] }))
      .toMatchObject({ host: 'local', to_group: 'prod', dest_dir: '/etc/app' })
    expect(draftFromCopy({ src: local, dst: app1, paths: ['/x'], destDir: '/etc/app', hosts: HOSTS, alsoTo: ['app-02'] })).toBeNull()
  })
  it('drafts an ad-hoc source', () => {
    expect(draftFromCopy({ src: adhoc, dst: local, paths: ['/var/log/a'], destDir: '/ws', hosts: HOSTS, workspacePath: '/ws' }))
      .toMatchObject({ adhoc: 'old-box', jump: 'app-01', user: 'ops' })
  })
})

describe('panesForSaved', () => {
  it('opens a pull: source host on the right, local folder on the left', () => {
    const r = panesForSaved({ id: 'a', host: 'app-01', paths: ['/var/log/app.log.{{day}}'], dest_dir: 'D:/logs' }, HOSTS, '/ws')
    expect(r.right).toEqual({ target: app1, path: '/var/log' })
    expect(r.left).toEqual({ target: local, path: 'D:/logs' })
  })
  it('skips a templated folder and uses the workspace for a default destination', () => {
    const r = panesForSaved({ id: 'a', group: 'prod', paths: ['/var/log/{{day}}/app.log'] }, HOSTS, '/ws')
    expect(r.right).toEqual({ target: app1, path: '' })
    expect(r.left).toEqual({ target: local, path: '/ws' })
  })
  it('opens an upload with this computer on the left', () => {
    const r = panesForSaved({ id: 'a', host: 'local', to_host: 'web-01', dest_dir: '/etc/app', paths: ['/home/me/app.conf'] }, HOSTS, '/ws')
    expect(r.left).toEqual({ target: local, path: '/home/me' })
    expect(r.right).toEqual({ target: { kind: 'host', host: 'web-01' }, path: '/etc/app' })
  })
})

describe('nextSelection', () => {
  const names = ['a', 'b', 'c', 'd']
  it('click, ctrl-click and shift-click', () => {
    expect(nextSelection(['a'], names, 'c', 'a', {})).toEqual(['c'])
    expect(nextSelection(['a'], names, 'c', 'a', { ctrl: true })).toEqual(['a', 'c'])
    expect(nextSelection(['a', 'c'], names, 'c', 'a', { ctrl: true })).toEqual(['a'])
    expect(nextSelection(['b'], names, 'd', 'b', { shift: true })).toEqual(['b', 'c', 'd'])
    expect(nextSelection(['c'], names, 'a', 'c', { shift: true })).toEqual(['a', 'b', 'c'])
  })
})
