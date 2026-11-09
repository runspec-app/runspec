// filesModel — the Files tab's rules, kept pure so they unit-test without a
// bridge: what a pane shows, which copy a drag between two panes means (and
// which bridge call runs it), the per-folder remembered filter, and the
// saved-transfer drafts / "Open in panes" layouts.
import type { Host, SavedTransfer, TransferEndpoint, RelaySource } from '../../bridge'
import { adHocToEndpoint, type AdHocState } from '../AdHocHostFields'
import { remoteTarget } from '../savedTransfers'

export const LOCAL = 'local'

// What a pane browses: this computer, a configured host, or an ad-hoc SSH host.
export type PaneTarget =
  | { kind: 'local' }
  | { kind: 'host'; host: string }
  | { kind: 'adhoc'; adhoc: AdHocState }

export type Side = 'left' | 'right'
export const otherSide = (s: Side): Side => (s === 'left' ? 'right' : 'left')

// The drag payload's MIME type, so a pane only lights up for our own rows.
export const DRAG_MIME = 'application/x-runspec-files'

export interface DragPayload {
  side: Side
  paths: string[]
}

export function targetLabel(t: PaneTarget): string {
  if (t.kind === 'local') return 'this computer'
  if (t.kind === 'host') return t.host
  return t.adhoc.target.trim() || 'ad-hoc host'
}

// The host name the list_dir / make_dir / view_file / launch_terminal calls take
// ('' for an ad-hoc host, which only lists through list_dir_adhoc).
export function targetHost(t: PaneTarget): string {
  return t.kind === 'local' ? LOCAL : t.kind === 'host' ? t.host : ''
}

// A stable key for "the same machine", used to refresh the right pane when a
// copy lands and to remember filters per folder.
export function targetKey(t: PaneTarget): string {
  if (t.kind === 'adhoc') return `adhoc:${t.adhoc.jump}>${t.adhoc.user.trim()}@${t.adhoc.target.trim()}`
  return t.kind === 'local' ? LOCAL : `host:${t.host}`
}

export function sameTarget(a: PaneTarget, b: PaneTarget): boolean {
  return targetKey(a) === targetKey(b)
}

export function endpointOf(t: PaneTarget): TransferEndpoint {
  if (t.kind === 'local') return LOCAL
  if (t.kind === 'host') return t.host
  return adHocToEndpoint(t.adhoc)
}

// ── paths ──────────────────────────────────────────────────────────────────

// A Windows path (C:\…) uses '\'; everything else '/'.
export function sepOf(path: string): string {
  return path.includes('\\') && !path.includes('/') ? '\\' : '/'
}

export function joinPath(dir: string, name: string): string {
  const sep = sepOf(dir || '/')
  return dir.endsWith(sep) ? dir + name : dir + sep + name
}

export function baseName(path: string): string {
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] || path
}

export function dirName(path: string): string {
  const sep = sepOf(path)
  const i = path.lastIndexOf(sep)
  if (i < 0) return ''
  if (i === 0) return sep
  // Keep the drive root's separator (C:\).
  if (sep === '\\' && /^[A-Za-z]:$/.test(path.slice(0, i))) return path.slice(0, i + 1)
  return path.slice(0, i)
}

// ── which copy a drag means ──────────────────────────────────────────────────

export type CopyKind = 'download' | 'upload' | 'fanout' | 'relay'

export interface CopyPlan {
  kind?: CopyKind
  // Set when this drag can't be a copy; shown in place of the Copy button.
  error?: string
}

export function planCopy(src: PaneTarget, dst: PaneTarget, alsoTo: string[] = []): CopyPlan {
  if (src.kind === 'local' && dst.kind === 'local') {
    return { error: 'Both panes show this computer. Pick a host in one of them to copy.' }
  }
  if (src.kind === 'local') {
    return { kind: dst.kind === 'host' && alsoTo.length > 0 ? 'fanout' : 'upload' }
  }
  if (dst.kind === 'local') return { kind: 'download' }
  if (dst.kind === 'adhoc') {
    return { error: 'Copying to an ad-hoc host works only from this computer. Put this computer in the source pane.' }
  }
  return { kind: 'relay' }
}

export function copyButtonLabel(kind: CopyKind | undefined, hostCount: number): string {
  if (kind === 'fanout') return `Copy to ${hostCount} hosts`
  if (kind === 'relay') return 'Relay'
  return 'Copy'
}

// The other hosts a local file can also be pushed to: the connected members of
// the destination host's group.
export function fanoutCandidates(dstHost: string, hosts: Host[]): string[] {
  const group = hosts.find(h => h.name === dstHost)?.group
  if (!group) return []
  return hosts
    .filter(h => h.name !== LOCAL && h.name !== dstHost && h.group === group && h.connected)
    .map(h => h.name)
}

export function relaySource(t: PaneTarget): RelaySource | null {
  if (t.kind === 'host') return { kind: 'host', host: t.host }
  if (t.kind === 'adhoc') return adHocToEndpoint(t.adhoc)
  return null
}

// ── remembered filter per source folder ──────────────────────────────────────

const FILTER_KEY = 'runspec.files.lastFilter'
const FILTER_CAP = 200

function readFilterMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(FILTER_KEY)
    const v = raw ? JSON.parse(raw) : {}
    return v && typeof v === 'object' ? v : {}
  } catch { return {} }
}

export function lastFilterFor(src: PaneTarget, dir: string): string {
  return readFilterMap()[`${targetKey(src)}|${dir}`] ?? ''
}

export function rememberFilter(src: PaneTarget, dir: string, filter: string): void {
  const map = readFilterMap()
  const key = `${targetKey(src)}|${dir}`
  delete map[key]
  map[key] = filter
  const keys = Object.keys(map)
  for (const k of keys.slice(0, Math.max(0, keys.length - FILTER_CAP))) delete map[k]
  try { localStorage.setItem(FILTER_KEY, JSON.stringify(map)) } catch { /* private window */ }
}

// ── saved transfers ──────────────────────────────────────────────────────────

// A saved-transfer draft from a copy between the panes: the source pane's
// machine + picked files, the destination pane's machine + folder, and the
// drop dialog's filter / new name. `null` when a scenario can't name the
// destination (a hand-picked subset of a group).
export function draftFromCopy(opts: {
  src: PaneTarget
  dst: PaneTarget
  paths: string[]
  destDir: string
  hosts: Host[]
  alsoTo?: string[]
  filter?: string
  filterArg?: string
  destName?: string
  workspacePath?: string
}): SavedTransfer | null {
  const { src, dst, paths, destDir, hosts } = opts
  let to: Partial<SavedTransfer> = {}
  if (dst.kind === 'host') {
    const picked = remoteTarget([dst.host, ...(opts.alsoTo ?? [])], hosts)
    if (!picked) return null
    to = picked
  } else if (dst.kind === 'adhoc') {
    to = {
      to_adhoc: dst.adhoc.target.trim(), to_jump: dst.adhoc.jump || undefined,
      to_user: dst.adhoc.user.trim() || undefined, to_credential_id: dst.adhoc.credentialId || undefined,
    }
  }
  const from: Partial<SavedTransfer> = src.kind === 'local' ? { host: LOCAL }
    : src.kind === 'host' ? { host: src.host }
      : {
          adhoc: src.adhoc.target.trim(), jump: src.adhoc.jump || undefined,
          user: src.adhoc.user.trim() || undefined, credential_id: src.adhoc.credentialId || undefined,
        }
  return {
    id: '',
    paths,
    ...from,
    ...to,
    filter: opts.filter || undefined,
    filter_arg: opts.filter ? (opts.filterArg || undefined) : undefined,
    // The chat workspace is the default local destination and follows the chat,
    // so it isn't pinned; any other local folder is.
    dest_dir: dst.kind !== 'local' ? destDir : destDir && destDir !== opts.workspacePath ? destDir : undefined,
    dest_name: paths.length === 1 ? (opts.destName?.trim() || undefined) : undefined,
  }
}

export interface PaneOpen {
  target: PaneTarget
  path: string
}

// A folder worth opening: one with no placeholder or wildcard in it.
function plainDir(path: string | undefined): string {
  if (!path) return ''
  const dir = dirName(path)
  return /\{\{|[*?[]/.test(dir) ? '' : dir
}
function plainFolder(dir: string | undefined): string {
  return dir && !/\{\{|[*?[]/.test(dir) ? dir : ''
}

// "Open in panes": the saved transfer's source folder in the right pane and its
// destination folder in the left one (an upload: the other way round), so you
// can look before running it. A group opens its first connected member; an
// ad-hoc side leaves that pane as it is (null).
export function panesForSaved(t: SavedTransfer, hosts: Host[], workspacePath: string): { left: PaneOpen | null; right: PaneOpen | null } {
  const groupMember = (g?: string) => {
    const members = hosts.filter(h => h.name !== LOCAL && h.group === g)
    return (members.find(h => h.connected) ?? members[0])?.name ?? ''
  }
  const srcHost = t.host === LOCAL ? LOCAL : t.host || (t.group ? groupMember(t.group) : '')
  const source: PaneOpen | null = !srcHost ? null : {
    target: srcHost === LOCAL ? { kind: 'local' } : { kind: 'host', host: srcHost },
    path: plainDir(t.paths?.[0]),
  }
  const dstHost = t.to_host || (t.to_group ? groupMember(t.to_group) : '')
  const remoteDest = !!(t.to_host || t.to_group || t.to_adhoc)
  const dest: PaneOpen | null = !remoteDest
    ? { target: { kind: 'local' }, path: plainFolder(t.dest_dir) || workspacePath }
    : dstHost ? { target: { kind: 'host', host: dstHost }, path: plainFolder(t.dest_dir) } : null
  // Keep a remote side on the right, this computer on the left.
  if (source?.target.kind === 'local') return { left: source, right: dest }
  return { left: dest, right: source }
}

// ── selection ────────────────────────────────────────────────────────────────

// Click / Ctrl-click / Shift-click over `names` (the pane's visible order).
export function nextSelection(
  current: string[], names: string[], clicked: string, anchor: string | null,
  mods: { ctrl?: boolean; shift?: boolean },
): string[] {
  if (mods.shift && anchor && names.includes(anchor)) {
    const a = names.indexOf(anchor)
    const b = names.indexOf(clicked)
    const [lo, hi] = a < b ? [a, b] : [b, a]
    return names.slice(lo, hi + 1)
  }
  if (mods.ctrl) return current.includes(clicked) ? current.filter(n => n !== clicked) : [...current, clicked]
  return [clicked]
}

export function humanSize(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`
}

export function humanTime(epochSeconds: number, now = Date.now()): string {
  if (!epochSeconds) return ''
  const d = new Date(epochSeconds * 1000)
  const today = new Date(now)
  const sameDay = d.toDateString() === today.toDateString()
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `today ${hm}`
  if (d.getFullYear() === today.getFullYear()) return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${hm}`
  // An older year: the date is enough.
  return d.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' })
}
