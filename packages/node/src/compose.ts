/**
 * compose.ts — resolve `include` and `shared-args` in a parsed runspec.toml.
 *
 * Port of the Python core's `runspec/compose.py`; same rules, same messages.
 *
 * `include`: a runnable or command table may say `include = "path/file.toml"`.
 * The file holds that table's body and is merged into it. Paths are relative to
 * the file that names them and must stay inside the runspec.toml directory.
 * Included files may include others; a cycle is an error and the chain is capped
 * at MAX_INCLUDE_DEPTH files. A key set in both places is an error.
 *
 * `shared-args` / `use`: `[config.shared-args]`, `[<runnable>.shared-args]` and
 * `[<…>.commands.<c>.shared-args]` define args once; `project = {use = "project"}`
 * takes the nearest definition, and keys next to `use` override it.
 */
import * as fs from 'fs';
import * as path from 'path';
import { parse as parseTOML } from 'smol-toml';

/** Reads an included file by its package-relative POSIX path; `undefined` when missing. */
export type Reader = (rel: string) => string | undefined;

export const ROOT_FILE = 'runspec.toml';
export const MAX_INCLUDE_DEPTH = 8;
export const SHARED_ARGS_KEY = 'shared-args';

type Table = Record<string, unknown>;
type Defs = Record<string, Table>;

function isTable(v: unknown): v is Table {
  return typeof v === 'object' && v !== null && !Array.isArray(v) && !(v instanceof Date);
}

/** A deep copy of a parsed TOML value (tables, arrays, scalars, dates). */
function clone<T>(v: T): T {
  if (Array.isArray(v)) return v.map(clone) as unknown as T;
  if (isTable(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clone(x)])) as T;
  return v;
}

/**
 * Return a copy of `data` with every `include` and `use` resolved and every
 * `shared-args` table removed. A file using neither comes back deep-equal.
 */
export function resolve(data: Table, reader?: Reader, source: string = ROOT_FILE): Table {
  const out = clone(data);
  for (const [name, table] of Object.entries(out)) {
    if (name === 'config' || !isTable(table)) continue;
    resolveIncludes(table, `[${name}]`, source, reader, [source]);
  }
  resolveUses(out);
  return out;
}

/** Cheap textual check: could this runspec.toml text use `include`? */
export function hasInclude(text: string): boolean {
  return text.split(/\r?\n/).some((line) => /^\s*include\s*=/.test(line));
}

/** A reader over the real filesystem, rooted at the runspec.toml directory. */
export function fsReader(root: string): Reader {
  const base = fs.realpathSync(root);
  return (rel: string) => {
    const candidate = path.join(base, ...rel.split('/'));
    if (!fs.existsSync(candidate)) return undefined;
    const target = fs.realpathSync(candidate);
    const relToBase = path.relative(base, target);
    if (relToBase.startsWith('..') || path.isAbsolute(relToBase)) {
      throw new Error(`✗  include '${rel}' resolves outside the package directory (${base}).`);
    }
    if (!fs.statSync(target).isFile()) return undefined;
    return fs.readFileSync(target, 'utf-8');
  };
}

// ── include ────────────────────────────────────────────────────────────────────

function includePath(value: unknown, curFile: string, where: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`✗  ${where} include must be a path string, e.g. include = "commands/deploy.toml".`);
  }
  const raw = value.trim();
  if (raw.includes('\\')) throw new Error(`✗  ${where} include '${raw}': use '/' in include paths, not '\\'.`);
  if (raw.startsWith('/') || (raw.length > 1 && raw[1] === ':')) {
    throw new Error(`✗  ${where} include '${raw}' must be a relative path inside the package directory.`);
  }
  if (!raw.endsWith('.toml')) throw new Error(`✗  ${where} include '${raw}' must name a .toml file.`);
  const rel = path.posix.normalize(path.posix.join(path.posix.dirname(curFile), raw));
  if (rel === '..' || rel.startsWith('../')) {
    throw new Error(`✗  ${where} include '${raw}' (in ${curFile}) points outside the package directory.`);
  }
  return rel;
}

function readFragment(rel: string, reader: Reader | undefined, where: string, curFile: string): Table {
  if (!reader) throw new Error(`✗  ${where} include '${rel}' can't be read here (no file access).`);
  const text = reader(rel);
  if (text === undefined) {
    throw new Error(`✗  ${where} include '${rel}' (named in ${curFile}) was not found.\n   Is the file shipped in the package?`);
  }
  try {
    return parseTOML(text) as Table;
  } catch (e) {
    throw new Error(`✗  ${rel} is not valid TOML: ${(e as Error).message}`);
  }
}

function resolveIncludes(table: Table, where: string, curFile: string, reader: Reader | undefined, stack: string[]): void {
  const origin: Record<string, string> = Object.fromEntries(Object.keys(table).map((k) => [k, curFile]));
  let file = curFile;
  while ('include' in table) {
    const inc = table['include'];
    delete table['include'];
    const rel = includePath(inc, file, where);
    if (stack.includes(rel)) {
      const chain = [...stack.slice(stack.indexOf(rel)), rel].join(' → ');
      throw new Error(`✗  ${where} include cycle: ${chain}`);
    }
    if (stack.length > MAX_INCLUDE_DEPTH) {
      throw new Error(`✗  ${where} includes are nested more than ${MAX_INCLUDE_DEPTH} files deep: ${[...stack, rel].join(' → ')}`);
    }
    const frag = readFragment(rel, reader, where, file);
    const dupes = Object.keys(frag).filter((k) => k in table).sort();
    if (dupes.length) {
      throw new Error(`✗  ${where} sets ${dupes.join(', ')} both in ${origin[dupes[0]]} and in its include ${rel}.\n   Set each key in one place.`);
    }
    for (const [k, v] of Object.entries(frag)) {
      table[k] = v;
      origin[k] = rel;
    }
    stack = [...stack, rel];
    file = rel;
  }
  const commands = table['commands'];
  if (isTable(commands)) {
    const cmdFile = origin['commands'] ?? file;
    for (const [name, sub] of Object.entries(commands)) {
      if (isTable(sub)) resolveIncludes(sub, `${where.slice(0, -1)}.commands.${name}]`, cmdFile, reader, stack);
    }
  }
}

// ── shared-args / use ──────────────────────────────────────────────────────────

function sharedDefs(raw: unknown, where: string): Defs {
  if (raw === undefined || raw === null) return {};
  if (!isTable(raw)) throw new Error(`✗  ${where} must be a table of arg definitions.`);
  const defs: Defs = {};
  for (const [name, value] of Object.entries(raw)) {
    const def = isTable(value) ? value : { default: value };
    if ('use' in def) throw new Error(`✗  ${where} ${name}: a shared arg can't itself use another one.`);
    defs[name] = def;
  }
  return defs;
}

function resolveUses(data: Table): void {
  const scopes: Defs[] = [];
  const config = data['config'];
  if (isTable(config)) {
    scopes.push(sharedDefs(config[SHARED_ARGS_KEY], `[config.${SHARED_ARGS_KEY}]`));
    delete config[SHARED_ARGS_KEY];
  }
  for (const [name, table] of Object.entries(data)) {
    if (name === 'config' || !isTable(table)) continue;
    resolveUsesTable(table, `[${name}]`, scopes);
  }
}

function resolveUsesTable(table: Table, where: string, scopes: Defs[]): void {
  if (SHARED_ARGS_KEY in table) {
    scopes = [...scopes, sharedDefs(table[SHARED_ARGS_KEY], `${where.slice(0, -1)}.${SHARED_ARGS_KEY}]`)];
    delete table[SHARED_ARGS_KEY];
  }
  const args = table['args'];
  if (isTable(args)) {
    for (const [argName, value] of Object.entries(args)) {
      if (isTable(value) && 'use' in value) args[argName] = use(value, argName, where, scopes);
    }
  }
  const commands = table['commands'];
  if (isTable(commands)) {
    for (const [name, sub] of Object.entries(commands)) {
      if (isTable(sub)) resolveUsesTable(sub, `${where.slice(0, -1)}.commands.${name}]`, scopes);
    }
  }
}

function use(value: Table, argName: string, where: string, scopes: Defs[]): Table {
  const ref = value['use'];
  if (typeof ref !== 'string' || !ref) {
    throw new Error(`✗  ${where} arg ${argName}: use must name a shared arg, e.g. {use = "${argName}"}.`);
  }
  for (const defs of [...scopes].reverse()) {
    if (ref in defs) {
      const merged = clone(defs[ref]);
      for (const [k, v] of Object.entries(value)) if (k !== 'use') merged[k] = v;
      return merged;
    }
  }
  const known = [...new Set(scopes.flatMap((d) => Object.keys(d)))].sort();
  const close = closest(ref, known);
  let hint: string;
  if (close) hint = `\n   Did you mean '${close}'?`;
  else if (known.length) hint = `\n   Shared args in scope: ${known.join(', ')}`;
  else hint = `\n   No [${SHARED_ARGS_KEY}] table is in scope here.`;
  throw new Error(`✗  ${where} arg ${argName}: unknown shared arg '${ref}'.${hint}`);
}

/** The closest known name, if any is close enough (edit distance ≤ a third of the length). */
function closest(word: string, known: string[]): string | undefined {
  let best: string | undefined;
  let bestDist = Infinity;
  for (const k of known) {
    const d = levenshtein(word, k);
    if (d < bestDist) {
      best = k;
      bestDist = d;
    }
  }
  return best !== undefined && bestDist <= Math.max(1, Math.floor(Math.max(word.length, best.length) / 3)) ? best : undefined;
}

function levenshtein(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const tmp = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = tmp;
    }
  }
  return row[b.length];
}
