// Pure helpers for saved transfers (the Transfer dialog's "Save as scenario" and
// run dialogs). Kept free of React so they unit-test on their own.

import type { SavedTransfer } from '../bridge'

// One row of the params editor. A param with no default is required (the runspec
// inference rule), so there is no separate "required" switch. `extra` keeps any
// other keys the stored table had (options, pattern, …) so an edit round-trips.
export interface ParamRowState {
  name: string
  default: string
  description: string
  extra?: Record<string, unknown>
}

// A date written into a file name: 2026-10-29, 2026_10_29, 2026.10.29 or 20261029.
const DATE_RE = /(?<!\d)(20\d{2})([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])(?!\d)/g

// The distinct dates found in the given paths (as written).
export function detectDates(paths: string[]): string[] {
  const found = new Set<string>()
  for (const p of paths) for (const m of p.matchAll(DATE_RE)) found.add(m[0])
  return [...found]
}

// Replace every written date with a {{param:FORMAT}} placeholder that renders
// the same way (keeping the separator), so a saved path follows the date param.
export function templatizeDates(paths: string[], param: string): string[] {
  return paths.map(p => p.replace(DATE_RE, (_m, _y, sep: string) => `{{${param}:%Y${sep}%m${sep}%d}}`))
}

// Turn a picked file into a pattern that also matches its rotations, e.g.
// app-2026-10-29.log → app-2026-10-29*.log. Leaves a path with a wildcard alone.
export function widenToRotations(path: string): string {
  if (/[*?[]/.test(path)) return path
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  const dot = path.lastIndexOf('.')
  if (dot <= slash + 1) return `${path}*`
  return `${path.slice(0, dot)}*${path.slice(dot)}`
}

// The {{name}} / {{name:fmt}} placeholders a set of templates uses (names only).
export function placeholderNames(texts: string[]): string[] {
  const out = new Set<string>()
  for (const t of texts) for (const m of t.matchAll(/\{\{\s*([A-Za-z_][\w-]*)\s*(?::[^}]*)?\}\}/g)) out.add(m[1])
  return [...out]
}

export const BUILTIN_PLACEHOLDERS = ['today', 'yesterday', 'now', 'host']

// Params used in templates but not declared (and not built-ins) — offered as
// rows so the operator never has to type a name twice.
export function missingParams(texts: string[], rows: ParamRowState[]): string[] {
  const declared = new Set(rows.map(r => r.name.trim().replace(/-/g, '_')))
  return placeholderNames(texts).filter(n => !declared.has(n.replace(/-/g, '_')) && !BUILTIN_PLACEHOLDERS.includes(n))
}

// Stored params table → editor rows.
export function rowsFromParams(raw: Record<string, unknown> | undefined): ParamRowState[] {
  return Object.entries(raw ?? {}).map(([name, spec]) => {
    const s = (spec && typeof spec === 'object' ? spec : {}) as Record<string, unknown>
    const { default: def, description, ...extra } = s
    return {
      name,
      default: def === undefined || def === null ? '' : String(def),
      description: typeof description === 'string' ? description : '',
      ...(Object.keys(extra).length ? { extra } : {}),
    }
  })
}

// Editor rows → the params table saved in runspec_saved_transfers.toml.
export function paramsFromRows(rows: ParamRowState[]): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const r of rows) {
    const name = r.name.trim()
    if (!name) continue
    const spec: Record<string, unknown> = { ...(r.extra ?? {}) }
    if (r.default.trim()) spec.default = r.default.trim()
    else delete spec.default
    if (r.description.trim()) spec.description = r.description.trim()
    out[name] = spec
  }
  return out
}

// A safe id from a title: lowercase words joined by '-'.
export function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48)
}

// Literal param defaults seed the run form.
export function defaultValues(entry: SavedTransfer): Record<string, unknown> {
  const d: Record<string, unknown> = {}
  for (const a of entry.params ?? []) {
    if (a.default !== undefined && a.default !== null && a.default !== '') d[a.name] = a.default
  }
  return d
}
