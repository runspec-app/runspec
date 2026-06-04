/**
 * runspec logs — view / status / prune / compact per-invocation audit logs.
 *
 * The Node port of the Python `runspec/logs.py` engine. The per-run store
 * (`[config.logging] store = "per-run"`) writes one
 * `{runnable}.{utc-ts}.{run_id}.log` per invocation with no in-process rotation;
 * this is the read + maintenance side. Pure functions (collectRecords,
 * inventory, planPrune, planCompact) are kept separate from I/O so they can be
 * unit-tested without a filesystem fixture.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import * as zlib from 'zlib';
import { findConfig } from './finder';
import { findProjectRoot } from './logging_setup';

// Filename grammar (under {logs}/), matching the Node + Python write side:
//   {runnable}.log                              single-mode active file
//   {runnable}.{YYYYMMDDThhmmssZ}.{run_id}.log  per-run invocation file
//   {runnable}.archive.{YYYYMMDD}.log[.gz]      compacted archive
const PER_RUN_RE = /\.\d{8}T\d{6}Z\.[0-9a-fA-F-]{8,}\.log$/;
const ARCHIVE_RE = /\.archive\.\d{8}\.log(?:\.gz)?$/;

const DUR_MS: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
const SIZE_MULT: Record<string, number> = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 };

// ── parsing helpers ────────────────────────────────────────────────────────────

/** '30m' / '24h' / '7d' / '2w' → milliseconds. Throws on a bad value. */
export function parseDuration(s: string): number {
  const m = /^\s*(\d+)\s*([smhdw])\s*$/i.exec(s);
  if (!m) throw new Error(`invalid duration ${JSON.stringify(s)} — use e.g. 30m, 24h, 7d, 2w`);
  return parseInt(m[1], 10) * DUR_MS[m[2].toLowerCase()];
}

/** '500KB' / '10MB' / '5GB' → bytes. Throws on a bad value. */
export function parseSize(s: string): number {
  const m = /^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)?\s*$/i.exec(s);
  if (!m) throw new Error(`invalid size ${JSON.stringify(s)} — use e.g. 500KB, 10MB, 5GB`);
  return Math.round(parseFloat(m[1]) * SIZE_MULT[(m[2] ?? 'B').toUpperCase()]);
}

function iso(mtimeMs: number | null): string | null {
  if (mtimeMs === null) return null;
  return new Date(mtimeMs).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function humanSize(n: number): string {
  let size = n;
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (size < 1024 || unit === 'TB') return unit === 'B' ? `${Math.round(size)} B` : `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} TB`;
}

// ── discovery ──────────────────────────────────────────────────────────────────

/** Existing logs directories, in priority order: the project's logs/, then ~/logs. */
export function logDirs(): string[] {
  const candidates: string[] = [];
  try {
    const { configPath } = findConfig();
    const root = findProjectRoot(path.dirname(configPath)) ?? path.dirname(configPath);
    candidates.push(path.join(root, 'logs'));
  } catch {
    // no project context (no runspec.toml) — fall back to ~/logs only
  }
  candidates.push(path.join(os.homedir(), 'logs'));

  const seen = new Set<string>();
  const out: string[] = [];
  for (const d of candidates) {
    const r = path.resolve(d);
    if (seen.has(r)) continue;
    seen.add(r);
    try {
      if (fs.statSync(d).isDirectory()) out.push(d);
    } catch {
      // not present — skip
    }
  }
  return out;
}

function matchesRunnable(name: string, runnable: string | null): boolean {
  if (!(name.endsWith('.log') || name.endsWith('.log.gz'))) return false;
  if (!runnable) return true;
  return name === `${runnable}.log` || name.startsWith(`${runnable}.`);
}

export function discover(dirs: string[], runnable: string | null): string[] {
  const seen = new Set<string>();
  const files: string[] = [];
  for (const d of dirs) {
    let entries: string[];
    try {
      entries = fs.readdirSync(d);
    } catch {
      continue;
    }
    for (const name of entries) {
      if (!matchesRunnable(name, runnable)) continue;
      const full = path.join(d, name);
      if (seen.has(full)) continue;
      try {
        if (!fs.statSync(full).isFile()) continue;
      } catch {
        continue;
      }
      seen.add(full);
      files.push(full);
    }
  }
  return files;
}

/** True only for per-run files and archives — never a single {runnable}.log. */
export function isManaged(p: string): boolean {
  const n = path.basename(p);
  return PER_RUN_RE.test(n) || ARCHIVE_RE.test(n);
}

function isArchive(p: string): boolean {
  return ARCHIVE_RE.test(path.basename(p));
}

function runnableOf(p: string): string {
  const n = path.basename(p);
  for (const rx of [PER_RUN_RE, ARCHIVE_RE]) {
    const m = rx.exec(n);
    if (m) return n.slice(0, m.index);
  }
  return n.endsWith('.log.gz') ? n.slice(0, -7) : n.slice(0, -4);
}

function readLines(p: string): string[] {
  let text: string;
  try {
    text = p.endsWith('.gz') ? zlib.gunzipSync(fs.readFileSync(p)).toString('utf-8') : fs.readFileSync(p, 'utf-8');
  } catch {
    return [];
  }
  return text.split('\n').filter((l) => l.trim() !== '');
}

// ── view ────────────────────────────────────────────────────────────────────────

export interface RecPair {
  rec: Record<string, any>;
  raw: string;
}

export interface CollectOpts {
  since?: number | null; // milliseconds
  run?: string | null;
  user?: string | null;
}

/** Merge a runnable's files into one timestamp-sorted list of {rec, raw}. Pure. */
export function collectRecords(dirs: string[], runnable: string, opts: CollectOpts = {}): RecPair[] {
  const raws: string[] = [];
  for (const f of discover(dirs, runnable)) raws.push(...readLines(f));

  const parsed: RecPair[] = [];
  const userOfRun: Record<string, string> = {};
  for (const raw of raws) {
    let rec: Record<string, any>;
    try {
      rec = JSON.parse(raw);
    } catch {
      continue;
    }
    const extra = rec && typeof rec.extra === 'object' && rec.extra ? rec.extra : {};
    if (extra.event === 'run_summary' && extra.run_id) userOfRun[extra.run_id] = extra.user ?? '';
    parsed.push({ rec, raw });
  }

  const cutoff = opts.since != null ? Date.now() - opts.since : null;
  const keep = (rec: Record<string, any>): boolean => {
    const extra = rec.extra && typeof rec.extra === 'object' ? rec.extra : {};
    const rid = extra.run_id ?? '';
    if (opts.run && rid !== opts.run) return false;
    if (opts.user && (userOfRun[rid] ?? extra.user ?? '') !== opts.user) return false;
    if (cutoff != null) {
      const t = Date.parse(rec.ts ?? '');
      if (Number.isNaN(t) || t < cutoff) return false;
    }
    return true;
  };

  return parsed.filter((p) => keep(p.rec)).sort((a, b) => String(a.rec.ts ?? '').localeCompare(String(b.rec.ts ?? '')));
}

function formatLine(rec: Record<string, any>, userOfRun: Record<string, string>): string {
  const extra = rec.extra && typeof rec.extra === 'object' ? rec.extra : {};
  const rid = extra.run_id ?? '';
  const user = extra.user || userOfRun[rid] || '';
  const ts = rec.ts ?? '';
  const level = String(rec.level ?? '').padEnd(8);
  const msg = rec.message ?? '';
  return `${ts} ${level} run=${(rid || '-').slice(0, 8)} user=${user || '-'} ${msg}`;
}

type Writer = (s: string) => void;

function emit(records: RecPair[], asJson: boolean, out: Writer): void {
  const userOfRun: Record<string, string> = {};
  for (const { rec } of records) {
    const extra = rec.extra && typeof rec.extra === 'object' ? rec.extra : {};
    if (extra.event === 'run_summary' && extra.run_id) userOfRun[extra.run_id] = extra.user ?? '';
  }
  for (const { rec, raw } of records) {
    out(asJson ? raw + '\n' : formatLine(rec, userOfRun) + '\n');
  }
}

export interface ViewOpts extends CollectOpts {
  dirs?: string[];
  asJson?: boolean;
  follow?: boolean;
  out?: Writer;
  pollMs?: number;
}

/** Write the merged stream to `out` (default stdout). Supports `--follow` (poll). */
export function view(runnable: string, opts: ViewOpts = {}): void {
  const dirs = opts.dirs ?? logDirs();
  const out = opts.out ?? ((s: string) => process.stdout.write(s));
  const co: CollectOpts = { since: opts.since, run: opts.run, user: opts.user };
  const records = collectRecords(dirs, runnable, co);
  emit(records, !!opts.asJson, out);

  if (!opts.follow) return;
  const key = (r: Record<string, any>) => `${r.ts ?? ''}|${(r.extra && r.extra.run_id) ?? ''}|${r.message ?? ''}`;
  const seen = new Set(records.map((p) => key(p.rec)));
  const interval = setInterval(() => {
    const fresh = collectRecords(dirs, runnable, co).filter((p) => !seen.has(key(p.rec)));
    if (fresh.length) {
      emit(fresh, !!opts.asJson, out);
      for (const p of fresh) seen.add(key(p.rec));
    }
  }, opts.pollMs ?? 1000);
  // Don't keep the event loop alive solely for the poll if the process is done.
  if (typeof interval.unref === 'function') interval.unref();
}

// ── status ───────────────────────────────────────────────────────────────────────

export interface InvRow {
  runnable: string;
  per_run_files: number;
  archives: number;
  total_bytes: number;
  oldest: number | null; // mtime epoch ms
  newest: number | null;
}

/** Per-runnable inventory of managed files. Pure (stat only). */
export function inventory(dirs: string[], runnable: string | null = null): Record<string, InvRow> {
  const per: Record<string, InvRow> = {};
  for (const f of discover(dirs, runnable)) {
    if (!isManaged(f)) continue;
    let st: fs.Stats;
    try {
      st = fs.statSync(f);
    } catch {
      continue;
    }
    const name = runnableOf(f);
    const row = (per[name] ??= { runnable: name, per_run_files: 0, archives: 0, total_bytes: 0, oldest: null, newest: null });
    if (isArchive(f)) row.archives++;
    else row.per_run_files++;
    row.total_bytes += st.size;
    if (row.oldest === null || st.mtimeMs < row.oldest) row.oldest = st.mtimeMs;
    if (row.newest === null || st.mtimeMs > row.newest) row.newest = st.mtimeMs;
  }
  return per;
}

export interface StatusOpts {
  dirs?: string[];
  asJson?: boolean;
  out?: Writer;
}

export function status(runnable: string | null = null, opts: StatusOpts = {}): void {
  const dirs = opts.dirs ?? logDirs();
  const out = opts.out ?? ((s: string) => process.stdout.write(s));
  const rows = Object.values(inventory(dirs, runnable)).sort((a, b) => a.runnable.localeCompare(b.runnable));
  const totalBytes = rows.reduce((s, r) => s + r.total_bytes, 0);
  const totalFiles = rows.reduce((s, r) => s + r.per_run_files + r.archives, 0);

  if (opts.asJson) {
    out(
      JSON.stringify({
        dirs,
        runnables: rows.map((r) => ({
          runnable: r.runnable,
          per_run_files: r.per_run_files,
          archives: r.archives,
          total_bytes: r.total_bytes,
          oldest: iso(r.oldest),
          newest: iso(r.newest),
        })),
        total_bytes: totalBytes,
        total_files: totalFiles,
      }) + '\n',
    );
    return;
  }

  if (rows.length === 0) {
    out('No per-invocation logs found.\n');
    return;
  }
  const nameW = Math.max('RUNNABLE'.length, ...rows.map((r) => r.runnable.length));
  out(`${'RUNNABLE'.padEnd(nameW)}  ${'RUNS'.padStart(6)}  ${'ARCHIVES'.padStart(8)}  ${'SIZE'.padStart(10)}  NEWEST\n`);
  for (const r of rows) {
    out(`${r.runnable.padEnd(nameW)}  ${String(r.per_run_files).padStart(6)}  ${String(r.archives).padStart(8)}  ${humanSize(r.total_bytes).padStart(10)}  ${iso(r.newest) ?? '-'}\n`);
  }
  out(`\n${totalFiles} file(s), ${humanSize(totalBytes)} total across ${rows.length} runnable(s).\n`);
}

// ── prune ──────────────────────────────────────────────────────────────────────

export interface PrunePolicy {
  olderThan?: number | null; // ms
  maxFiles?: number | null;
  maxTotalSize?: number | null; // bytes
}

/** Return the managed files to delete. Pure — touches nothing. Throws if no policy. */
export function planPrune(dirs: string[], runnable: string | null, policy: PrunePolicy, now: number = Date.now()): string[] {
  const { olderThan = null, maxFiles = null, maxTotalSize = null } = policy;
  if (olderThan == null && maxFiles == null && maxTotalSize == null) {
    throw new Error('prune needs at least one of --older-than / --max-files / --max-total-size');
  }
  const files = discover(dirs, runnable).filter(isManaged);
  const stat = new Map<string, fs.Stats>();
  for (const f of files) {
    try {
      stat.set(f, fs.statSync(f));
    } catch {
      // skip vanished file
    }
  }
  const live = files.filter((f) => stat.has(f));
  const doomed = new Set<string>();

  if (olderThan != null) {
    const cut = now - olderThan;
    for (const f of live) if (stat.get(f)!.mtimeMs < cut) doomed.add(f);
  }

  if (maxFiles != null) {
    const per: Record<string, string[]> = {};
    for (const f of live) (per[runnableOf(f)] ??= []).push(f);
    for (const group of Object.values(per)) {
      group.sort((a, b) => stat.get(b)!.mtimeMs - stat.get(a)!.mtimeMs); // newest first
      for (const f of group.slice(maxFiles)) doomed.add(f);
    }
  }

  if (maxTotalSize != null) {
    const ordered = [...live].sort((a, b) => stat.get(b)!.mtimeMs - stat.get(a)!.mtimeMs);
    let total = 0;
    for (const f of ordered) {
      total += stat.get(f)!.size;
      if (total > maxTotalSize) doomed.add(f);
    }
  }

  return [...doomed].sort((a, b) => stat.get(a)!.mtimeMs - stat.get(b)!.mtimeMs);
}

export interface PruneOpts extends PrunePolicy {
  dirs?: string[];
  dryRun?: boolean;
  asJson?: boolean;
  out?: Writer;
}

export function prune(runnable: string | null, opts: PruneOpts = {}): { count: number; freed: number } {
  const dirs = opts.dirs ?? logDirs();
  const out = opts.out ?? ((s: string) => process.stdout.write(s));
  const targets = planPrune(dirs, runnable, opts);
  let freed = 0;
  const deleted: Array<{ path: string; bytes: number }> = [];
  for (const f of targets) {
    let size = 0;
    try {
      size = fs.statSync(f).size;
    } catch {
      // already gone
    }
    if (!opts.dryRun) {
      try {
        fs.unlinkSync(f);
      } catch {
        // ignore
      }
    }
    freed += size;
    deleted.push({ path: f, bytes: size });
    if (!opts.asJson) out(`${opts.dryRun ? 'would delete' : 'deleted'}  ${f}  (${size} bytes)\n`);
  }
  if (opts.asJson) {
    out(JSON.stringify({ dry_run: !!opts.dryRun, count: targets.length, freed_bytes: freed, deleted }) + '\n');
  } else {
    out(`${opts.dryRun ? 'Would free' : 'Freed'} ${freed} bytes across ${targets.length} file(s).\n`);
  }
  return { count: targets.length, freed };
}

// ── compact ──────────────────────────────────────────────────────────────────────

function lineTs(line: string): string {
  try {
    return String(JSON.parse(line).ts ?? '');
  } catch {
    return '';
  }
}

/** Map each per-runnable archive target → the per-run files to fold in. Pure. */
export function planCompact(dirs: string[], runnable: string | null, olderThan: number, now: number = Date.now()): Map<string, string[]> {
  const cut = now - olderThan;
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const plan = new Map<string, string[]>();
  for (const f of discover(dirs, runnable)) {
    if (!isManaged(f) || isArchive(f)) continue;
    let st: fs.Stats;
    try {
      st = fs.statSync(f);
    } catch {
      continue;
    }
    if (st.mtimeMs >= cut) continue;
    const archive = path.join(path.dirname(f), `${runnableOf(f)}.archive.${today}.log`);
    (plan.get(archive) ?? plan.set(archive, []).get(archive)!).push(f);
  }
  return plan;
}

export interface CompactOpts {
  dirs?: string[];
  gzip?: boolean;
  dryRun?: boolean;
  asJson?: boolean;
  out?: Writer;
}

export function compact(runnable: string | null, olderThan: number, opts: CompactOpts = {}): number {
  const dirs = opts.dirs ?? logDirs();
  const out = opts.out ?? ((s: string) => process.stdout.write(s));
  const plan = planCompact(dirs, runnable, olderThan);
  let compacted = 0;
  const archives: Array<{ archive: string; count: number; sources: string[] }> = [];

  for (const [archive, sources] of plan) {
    const target = opts.gzip ? archive + '.gz' : archive;
    if (opts.dryRun) {
      archives.push({ archive: target, count: sources.length, sources });
      compacted += sources.length;
      if (!opts.asJson) out(`would compact ${sources.length} file(s) → ${target}\n`);
      continue;
    }

    const lines: string[] = [];
    for (const existing of [archive, archive + '.gz']) {
      if (fs.existsSync(existing)) lines.push(...readLines(existing));
    }
    for (const s of sources) lines.push(...readLines(s));
    lines.sort((a, b) => lineTs(a).localeCompare(lineTs(b)));

    const body = lines.join('\n') + '\n';
    if (opts.gzip) {
      fs.writeFileSync(target, zlib.gzipSync(Buffer.from(body, 'utf-8')));
      if (fs.existsSync(archive) && archive !== target) {
        try {
          fs.unlinkSync(archive);
        } catch {
          // ignore
        }
      }
    } else {
      fs.writeFileSync(target, body, 'utf-8');
    }
    for (const s of sources) {
      try {
        fs.unlinkSync(s);
      } catch {
        // ignore
      }
    }
    archives.push({ archive: target, count: sources.length, sources });
    compacted += sources.length;
    if (!opts.asJson) out(`compacted ${sources.length} file(s) → ${target}\n`);
  }

  if (opts.asJson) out(JSON.stringify({ dry_run: !!opts.dryRun, compacted, archives }) + '\n');
  else if (plan.size === 0) out('nothing to compact.\n');
  return compacted;
}
