/**
 * Installed-code integrity — is a runnable's installed code what was installed?
 *
 * Node port of Python's `runspec/integrity.py`. npm keeps no per-file hashes,
 * so the known-good hashes come from (1) a `runspec-files.json` list packed
 * into the package by `runspec manifest`, else (2) a snapshot taken on the
 * first run and kept in `{project}/logs/.integrity-baseline.json`.
 *
 * `parse()` checks the project (the folder holding the nearest `package.json`
 * above `runspec.toml`, outside `node_modules`) and each of its direct
 * dependencies. A changed or missing file makes the run `modified`: a stderr
 * warning and an `integrity` field on the run summary. Nothing is checked in a
 * git work tree (a developer's checkout); symlinked dependencies (`npm link`,
 * `npm install ../dir`) are skipped.
 *
 * Tamper-evident only: whoever can edit an installed file can also edit the
 * list, the snapshot, or runspec-node. Never throws into the caller.
 * See SPEC.md → "Installed-Code Integrity" → "Node pack".
 */

import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { audit } from './auditlog';

export const INTEGRITY_ENV = 'RUNSPEC_INTEGRITY';
export const MANIFEST_NAME = 'runspec-files.json';
export const BASELINE_NAME = '.integrity-baseline.json';
/** How many changed files a run record carries (the full list is `runspec verify`'s job). */
export const MAX_FILES = 20;
/** A package with more files than this is not snapshotted (reported `unverifiable`). */
export const MAX_SNAPSHOT_FILES = 5000;
const WARN_FILES = 3;

export type Source = 'index' | 'linked' | 'archive' | 'vcs';
export type Status = 'ok' | 'modified' | 'skipped' | 'unverifiable';
export type Baseline = 'list' | 'snapshot' | 'none';

const LOCAL_SOURCES = new Set<Source>(['archive', 'vcs']);

export interface PackageCheck {
  name: string;
  version: string;
  dir: string;
  source: Source;
  baseline: Baseline;
  status: Status;
  checked: number;
  modified: string[];
  missing: string[];
  /** A snapshot was taken by this check (first run for this name@version). */
  snapshotTaken?: boolean;
  /** Current hash of each changed file (`missing` when deleted) — the fingerprint of this change. */
  digests?: Record<string, string>;
}

export interface Report {
  packages: PackageCheck[];
  /** The project root checked (its `logs/` holds the dedupe state). */
  root?: string;
}

export interface Manifest {
  version: 1;
  algorithm: 'sha256';
  name?: string;
  packageVersion?: string;
  files: Record<string, string>;
}

interface SnapshotEntry {
  version: string;
  taken: string;
  files: Record<string, string>;
}

type BaselineFile = Record<string, SnapshotEntry>;

export function enabled(): boolean {
  const v = (process.env[INTEGRITY_ENV] ?? '').trim().toLowerCase();
  return !['off', '0', 'false', 'no'].includes(v);
}

// ── Report helpers ───────────────────────────────────────────────────────────

export function changedFiles(p: PackageCheck): string[] {
  return [...p.modified, ...p.missing];
}

export function modifiedPackages(report: Report): PackageCheck[] {
  return report.packages.filter((p) => p.status === 'modified');
}

/** `modified` / `local` / `null` — the run record's `integrity`. */
export function reportToken(report: Report | null): string | null {
  if (!report) return null;
  if (modifiedPackages(report).length) return 'modified';
  if (report.packages.some((p) => LOCAL_SOURCES.has(p.source) && p.status !== 'skipped')) return 'local';
  return null;
}

/** `"<package>: <path>"` for each changed file, capped. */
export function reportFiles(report: Report | null, limit = MAX_FILES): string[] {
  if (!report) return [];
  const out: string[] = [];
  for (const p of modifiedPackages(report)) for (const f of changedFiles(p)) out.push(`${p.name}: ${f}`);
  return out.slice(0, limit);
}

// ── Hashing ──────────────────────────────────────────────────────────────────

const STREAM_ABOVE = 8 << 20;
let _chunk: Buffer | null = null;

export function hashFile(file: string): string {
  const h = crypto.createHash('sha256');
  const fd = fs.openSync(file, 'r');
  try {
    // Most package files are small: one read. Bigger ones go through a reused chunk.
    if (fs.fstatSync(fd).size <= STREAM_ABOVE) {
      h.update(fs.readFileSync(fd));
    } else {
      _chunk ??= Buffer.allocUnsafe(1 << 20);
      for (;;) {
        const n = fs.readSync(fd, _chunk, 0, _chunk.length, null);
        if (n === 0) break;
        h.update(_chunk.subarray(0, n));
      }
    }
  } finally {
    fs.closeSync(fd);
  }
  return h.digest('hex');
}

function toPosix(p: string): string {
  return p.split(path.sep).join('/');
}

/**
 * The files a snapshot covers: every regular file under `dir`, skipping
 * symlinks, dot-entries and nested `node_modules`, plus `exclude` (top-level
 * names). Returns `null` past `MAX_SNAPSHOT_FILES`.
 */
export function snapshotFileList(dir: string, exclude: Set<string> = new Set()): string[] | null {
  const out: string[] = [];
  const walk = (abs: string, rel: string): boolean => {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(abs, { withFileTypes: true });
    } catch {
      return true;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const e of entries) {
      if (e.name.startsWith('.') || e.name === 'node_modules') continue;
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (!rel && exclude.has(e.name)) continue;
      if (e.isDirectory()) {
        if (!walk(path.join(abs, e.name), r)) return false;
      } else if (e.isFile()) {
        out.push(r);
        if (out.length > MAX_SNAPSHOT_FILES) return false;
      }
    }
    return true;
  };
  return walk(dir, '') ? out : null;
}

/** Hash `files` (posix relative paths under `dir`). */
export function hashFiles(dir: string, files: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of files) {
    try {
      out[f] = hashFile(path.join(dir, ...f.split('/')));
    } catch {
      /* vanished / unreadable between listing and hashing */
    }
  }
  return out;
}

/** Compare `dir`'s files against `expected` hashes, filling `result`. */
function compare(dir: string, expected: Record<string, string>, result: PackageCheck): void {
  for (const [rel, want] of Object.entries(expected)) {
    if (rel.split('/').includes('..') || path.isAbsolute(rel)) continue;
    let actual: string;
    try {
      actual = hashFile(path.join(dir, ...rel.split('/')));
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === 'ENOENT') {
        result.missing.push(rel);
        (result.digests ??= {})[rel] = 'missing';
      }
      continue; // unreadable — can't judge
    }
    result.checked += 1;
    if (actual !== String(want).toLowerCase()) {
      result.modified.push(rel);
      (result.digests ??= {})[rel] = actual;
    }
  }
  if (result.modified.length || result.missing.length) result.status = 'modified';
  else if (result.checked === 0) result.status = 'unverifiable';
}

// ── Package metadata ─────────────────────────────────────────────────────────

function readJson(file: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function packageJson(dir: string): Record<string, unknown> | null {
  const pj = readJson(path.join(dir, 'package.json'));
  return isObject(pj) ? pj : null;
}

export function readManifest(dir: string): Manifest | null {
  const m = readJson(path.join(dir, MANIFEST_NAME));
  if (!isObject(m) || m.algorithm !== 'sha256' || !isObject(m.files)) return null;
  const files: Record<string, string> = {};
  for (const [k, v] of Object.entries(m.files)) if (typeof v === 'string') files[k] = v;
  return { version: 1, algorithm: 'sha256', name: m.name as string | undefined, packageVersion: m.packageVersion as string | undefined, files };
}

/** Source of the dependency installed at `node_modules/<name>`, from npm's hidden lockfile. */
export function sourceOf(root: string, name: string, lock: Record<string, unknown> | null): Source {
  const dir = path.join(root, 'node_modules', name);
  try {
    if (fs.lstatSync(dir).isSymbolicLink()) return 'linked';
  } catch {
    /* fall through */
  }
  const pkgs = lock && isObject(lock.packages) ? lock.packages : null;
  const entry = pkgs ? pkgs[`node_modules/${name}`] : null;
  if (!isObject(entry)) return 'index';
  if (entry.link === true) return 'linked';
  const resolved = typeof entry.resolved === 'string' ? entry.resolved : '';
  return classifyResolved(resolved);
}

/** Classify an npm `resolved` value. Pure. */
export function classifyResolved(resolved: string): Source {
  if (!resolved) return 'index';
  if (resolved.startsWith('file:')) return 'archive';
  if (/^(git\+|git:|github:|gitlab:|bitbucket:|gist:)/.test(resolved) || /\.git(#|$)/.test(resolved)) return 'vcs';
  return 'index';
}

/** The project root for a `runspec.toml`: nearest `package.json` above it, outside node_modules. */
export function projectRoot(configPath: string): string | null {
  let dir = path.dirname(path.resolve(configPath));
  for (;;) {
    if (!dir.split(path.sep).includes('node_modules') && fs.existsSync(path.join(dir, 'package.json'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** True when `dir` is inside a git work tree (a `.git` in it or an ancestor). */
export function inGitWorkTree(dir: string): boolean {
  let d = path.resolve(dir);
  for (;;) {
    if (fs.existsSync(path.join(d, '.git'))) return true;
    const parent = path.dirname(d);
    if (parent === d) return false;
    d = parent;
  }
}

/** Direct dependency names (dependencies + optionalDependencies), in declaration order. */
export function directDependencies(pj: Record<string, unknown>): string[] {
  const out: string[] = [];
  for (const key of ['dependencies', 'optionalDependencies']) {
    const deps = pj[key];
    if (!isObject(deps)) continue;
    for (const name of Object.keys(deps)) if (!out.includes(name)) out.push(name);
  }
  return out;
}

/** Every package installed at the top of `node_modules` (incl. `@scope/name`). */
export function installedPackages(root: string): string[] {
  const nm = path.join(root, 'node_modules');
  const out: string[] = [];
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(nm, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    if (e.name.startsWith('@')) {
      try {
        for (const s of fs.readdirSync(path.join(nm, e.name))) if (!s.startsWith('.')) out.push(`${e.name}/${s}`);
      } catch {
        /* unreadable scope */
      }
    } else {
      out.push(e.name);
    }
  }
  return out.sort();
}

// ── Baseline (snapshot) file ─────────────────────────────────────────────────

export function baselinePath(root: string): string {
  return path.join(root, 'logs', BASELINE_NAME);
}

function readBaseline(file: string): BaselineFile {
  const data = readJson(file);
  if (!isObject(data)) return {};
  const out: BaselineFile = {};
  for (const [k, v] of Object.entries(data)) {
    if (isObject(v) && typeof v.version === 'string' && isObject(v.files)) {
      out[k] = { version: v.version, taken: String(v.taken ?? ''), files: v.files as Record<string, string> };
    }
  }
  return out;
}

function writeBaseline(file: string, data: BaselineFile): boolean {
  const tmp = `${file}.${process.pid}.tmp`;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(tmp, JSON.stringify(data, null, 1) + '\n', { encoding: 'utf8' });
    fs.renameSync(tmp, file);
    return true;
  } catch {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
    return false;
  }
}

// ── Checking ─────────────────────────────────────────────────────────────────

/** Top-level names left out of the project's own snapshot. */
const PROJECT_SNAPSHOT_EXCLUDE = new Set(['logs', 'bin', 'package-lock.json', MANIFEST_NAME]);

interface CheckContext {
  root: string;
  lock: Record<string, unknown> | null;
  baseline: BaselineFile;
  baselineDirty: boolean;
  /** Take a snapshot when a package has no list and no current snapshot. */
  takeSnapshots: boolean;
  /** Replace existing snapshots (`runspec verify --snapshot`). */
  retake: boolean;
}

function checkPackage(ctx: CheckContext, name: string, dir: string, source: Source, isProject: boolean): PackageCheck {
  const pj = packageJson(dir) ?? {};
  const version = typeof pj.version === 'string' ? pj.version : '';
  const pkgName = typeof pj.name === 'string' && pj.name ? pj.name : name;
  const result: PackageCheck = {
    name: pkgName, version, dir, source, baseline: 'none', status: 'ok', checked: 0, modified: [], missing: [],
  };
  if (source === 'linked') {
    result.status = 'skipped';
    return result;
  }
  const manifest = readManifest(dir);
  if (manifest) {
    result.baseline = 'list';
    compare(dir, manifest.files, result);
    return result;
  }
  const key = isProject ? `${pkgName} (project)` : pkgName;
  const existing = ctx.baseline[key];
  if (existing && existing.version === version && !ctx.retake) {
    result.baseline = 'snapshot';
    compare(dir, existing.files, result);
    return result;
  }
  if (!ctx.takeSnapshots) {
    result.status = 'unverifiable';
    return result;
  }
  const files = snapshotFileList(dir, isProject ? PROJECT_SNAPSHOT_EXCLUDE : new Set());
  if (files === null) {
    result.status = 'unverifiable';
    return result;
  }
  const hashes = hashFiles(dir, files);
  ctx.baseline[key] = { version, taken: new Date().toISOString(), files: hashes };
  ctx.baselineDirty = true;
  result.baseline = 'snapshot';
  result.snapshotTaken = true;
  result.checked = Object.keys(hashes).length;
  if (result.checked === 0) result.status = 'unverifiable';
  return result;
}

export interface CheckOptions {
  /** Package names to check (default: the project + its direct dependencies). */
  names?: string[];
  /** Check every package in node_modules (plus the project). */
  all?: boolean;
  /** Take a snapshot for packages with no list and none yet (default true). */
  takeSnapshots?: boolean;
  /** Replace existing snapshots. */
  retake?: boolean;
  /** Check even when switched off / in a git work tree (`runspec verify`). */
  force?: boolean;
  /** The project is a git checkout: report it `skipped` (its dependencies are still checked). */
  checkout?: boolean;
}

/**
 * Check the project that owns `configPath` and its direct dependencies (or the
 * packages `opts` names). `null` when nothing is checked: switched off, no
 * `package.json`, or a git work tree. Never throws.
 */
export function checkProject(configPath: string, opts: CheckOptions = {}): Report | null {
  try {
    if (!opts.force && !enabled()) return null;
    const root = projectRoot(configPath);
    if (!root) return null;
    if (!opts.force && inGitWorkTree(root)) return null;
    return checkRoot(root, opts);
  } catch {
    return null;
  }
}

/** Check the project at `root` (a folder with a `package.json`). */
export function checkRoot(root: string, opts: CheckOptions = {}): Report {
  const pj = packageJson(root) ?? {};
  const projectName = typeof pj.name === 'string' && pj.name ? pj.name : path.basename(root);
  const lockRaw = readJson(path.join(root, 'node_modules', '.package-lock.json'));
  const bfile = baselinePath(root);
  const ctx: CheckContext = {
    root,
    lock: isObject(lockRaw) ? lockRaw : null,
    baseline: readBaseline(bfile),
    baselineDirty: false,
    takeSnapshots: opts.takeSnapshots ?? true,
    retake: opts.retake ?? false,
  };

  let deps = opts.all ? installedPackages(root) : directDependencies(pj);
  let includeProject = true;
  if (opts.names && opts.names.length) {
    const wanted = new Set(opts.names);
    includeProject = wanted.has(projectName);
    const pool = [...new Set([...directDependencies(pj), ...installedPackages(root)])];
    deps = pool.filter((n) => wanted.has(n));
  }

  const packages: PackageCheck[] = [];
  if (includeProject) {
    if (opts.checkout) {
      const version = typeof pj.version === 'string' ? pj.version : '';
      packages.push({ name: projectName, version, dir: root, source: 'index', baseline: 'none', status: 'skipped', checked: 0, modified: [], missing: [] });
    } else {
      packages.push(checkPackage(ctx, projectName, root, 'index', true));
    }
  }
  for (const name of deps) {
    const dir = path.join(root, 'node_modules', name);
    if (!fs.existsSync(dir)) continue;
    packages.push(checkPackage(ctx, name, dir, sourceOf(root, name, ctx.lock), false));
  }
  if (ctx.baselineDirty && !writeBaseline(bfile, ctx.baseline)) {
    // Couldn't keep the snapshot: nothing it found can be relied on next run.
    for (const p of packages) {
      if (p.snapshotTaken) {
        p.snapshotTaken = false;
        p.baseline = 'none';
        p.status = 'unverifiable';
        p.checked = 0;
      }
    }
  }
  return { packages, root };
}

// ── Acting on a result ───────────────────────────────────────────────────────

export function warningText(name: string, report: Report): string {
  const mods = modifiedPackages(report);
  const files = mods.flatMap(changedFiles);
  const shown = files.slice(0, WARN_FILES).join(', ') + (files.length > WARN_FILES ? ` and ${files.length - WARN_FILES} more` : '');
  const pkgs = mods.map((p) => `${p.name} ${p.version}`.trim()).join(', ');
  const noun = files.length === 1 ? 'file differs' : 'files differ';
  return `⚠  ${name}: ${files.length} installed ${noun} from the package (${pkgs}): ${shown}. This run is recorded as modified. Reinstall the package to restore it; "runspec verify" lists the changes.\n`;
}

export const STATE_NAME = '.integrity-state.json';

function fingerprint(p: PackageCheck): string {
  const lines = Object.entries(p.digests ?? {}).map(([f, h]) => `${f}=${h}`).sort();
  return crypto.createHash('sha256').update([p.name, p.version, ...lines].join('\n'), 'utf8').digest('hex');
}

function readState(file: string): Record<string, string> {
  const data = readJson(file);
  const out: Record<string, string> = {};
  if (isObject(data)) for (const [k, v] of Object.entries(data)) out[k] = String(v);
  return out;
}

function writeState(file: string, state: Record<string, string>): void {
  const tmp = `${file}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(state), 'utf8');
    fs.renameSync(tmp, file);
  } catch {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
  }
}

/**
 * Warn on stderr for a `modified` report and record the `runnable.modified`
 * audit event — once per distinct set of changes, remembered in
 * `{project}/logs/.integrity-state.json`. Never throws.
 */
export function reportRun(name: string, report: Report | null, opts: { statePath?: string } = {}): void {
  if (!report) return;
  const mods = modifiedPackages(report);
  if (mods.length) {
    try {
      process.stderr.write(warningText(name, report));
    } catch {
      /* never disrupt a run */
    }
  }
  try {
    const file = opts.statePath ?? (report.root ? path.join(report.root, 'logs', STATE_NAME) : null);
    if (!file) return;
    const state = mods.length || fs.existsSync(file) ? readState(file) : {};
    let changed = false;
    // A package that is clean again forgets its last change, so the same edit
    // made again later is recorded again.
    for (const p of report.packages) {
      if (p.status !== 'modified' && p.name in state) {
        delete state[p.name];
        changed = true;
      }
    }
    for (const p of mods) {
      const fp = fingerprint(p);
      if (state[p.name] === fp) continue;
      try {
        audit('runnable.modified', 'detected', {
          target: { package: p.name, version: p.version },
          detail: { files: changedFiles(p).slice(0, MAX_FILES), count: changedFiles(p).length, source: p.source, baseline: p.baseline },
        });
      } catch {
        /* never disrupt a run */
      }
      state[p.name] = fp;
      changed = true;
    }
    if (changed) writeState(file, state);
  } catch {
    /* never disrupt a run */
  }
}

// ── Writing a list (`runspec manifest`) ──────────────────────────────────────

/** Build a manifest for `dir` from the files `npm pack` would publish. Pure apart from hashing. */
export function buildManifest(dir: string, packedFiles: string[]): Manifest {
  const pj = packageJson(dir) ?? {};
  const files = [...new Set(packedFiles.map(toPosix))].filter((f) => f !== MANIFEST_NAME).sort();
  return {
    version: 1,
    algorithm: 'sha256',
    name: typeof pj.name === 'string' ? pj.name : undefined,
    packageVersion: typeof pj.version === 'string' ? pj.version : undefined,
    files: hashFiles(dir, files),
  };
}

/**
 * Whether `package.json` `files` would publish `runspec-files.json`. `true`
 * when there is no `files` field (npm then publishes everything not ignored).
 */
export function manifestIsPublished(pj: Record<string, unknown>): boolean {
  const files = pj.files;
  if (!Array.isArray(files)) return true;
  return files.some((f) => {
    const s = String(f).replace(/^\.\//, '').replace(/\/$/, '');
    return s === MANIFEST_NAME || s === '*' || s === '**' || s === '*.json' || s === '**/*';
  });
}
