/**
 * Append-only audit events — `audit()`. Node port of Python's
 * `runspec/auditlog.py`; the record, the ledger chain and the canonical JSON the
 * hash is taken over are the same, so either pack verifies the other's ledger.
 *
 * Each event goes, best-effort, to up to three places:
 *
 * 1. **The ledger** `{project}/logs/audit.jsonl` — appended, never rotated or
 *    truncated, and hash-chained: each line carries the previous line's hash
 *    (`prev`) and its own (`hash`), so `verifyLedger()` / `runspec audit verify`
 *    names the first edited, removed or reordered line. Only the owner of
 *    `logs/` (or root) appends; anyone else writes `~/logs/audit.jsonl` (the
 *    Node pack has no inbox). Appends are serialised by a lock file.
 * 2. **The OS log** — syslog through `logger` on POSIX, the Application event
 *    log through `eventcreate` on Windows.
 * 3. **The run's own log** — a file-only record on the `runspec.audit` logger,
 *    when the runnable has `[config.logging]`.
 *
 * Tamper-evident, not tamper-proof: root can erase local records. Ship the OS
 * log off the machine for an immutable trail. See SPEC.md → "Audit Events".
 */

import { spawnSync } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { findConfig } from './finder';
import { AUDIT_LOGGER, getLogger, isConfigured, redact } from './logging_setup';
import { canonicalJson, pyDumps, pyParse, type PyValue } from './pyjson';
import type { AuditConfig } from './models';

export const LEDGER_NAME = 'audit.jsonl';
export const GENESIS = 'sha256:' + '0'.repeat(64);
export const AUDIT_ENV = 'RUNSPEC_AUDIT';
const RECORD_VERSION = 1;
const WINDOWS_EVENT_ID = 200;
const SYSLOG_TAG = 'runspec-audit';
const TAIL_BYTES = 65536;
const LOCK_WAIT_MS = 5000;
const LOCK_STALE_MS = 30000;

const EVENT_RE = /^[a-z0-9][a-z0-9_.-]{0,99}$/;
const OUTCOME_RE = /^[a-z0-9_.-]{0,40}$/;
const SENSITIVE_KEY_RE = /^(password|passwd|pwd|token|api[_-]?key|secret)$/i;
const FACILITIES = new Set(['auth', 'authpriv', 'user', 'daemon', 'local0', 'local1', 'local2', 'local3', 'local4', 'local5', 'local6', 'local7']);

/** `audit(..., { strict: true })` could not write the event anywhere. */
export class AuditError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AuditError';
  }
}

export type AuditRecord = Record<string, unknown>;

export interface AuditOptions {
  /** What was acted on (a JSON object; secret-looking keys/values redacted). */
  target?: Record<string, unknown>;
  /** Extra context (a JSON object; secret-looking keys/values redacted). */
  detail?: Record<string, unknown>;
  /** Throw `AuditError` when no sink took the event. */
  strict?: boolean;
  /** Write the ledger in this directory instead of the project's `logs/`. */
  ledgerDir?: string;
  /** Override `[config.audit] syslog` for this call. */
  syslog?: boolean;
}

// ── run context (set by parse()) ─────────────────────────────────────────────

export interface RunContext {
  runnable: string;
  commandPath: string[];
  runId: string;
  agent: boolean;
  trigger: string;
  auditCfg: AuditConfig;
  /** The project root (`{project}`): its `logs/` holds the ledger. */
  root: string;
}

let _ctx: RunContext | null = null;

/** Called by `parse()`: ties later `audit()` calls to this run. */
export function setRunContext(ctx: RunContext | null): void {
  _ctx = ctx;
}

export function currentRunContext(): RunContext | null {
  return _ctx;
}

export function auditDisabled(): boolean {
  return ['off', '0', 'false', 'no'].includes((process.env[AUDIT_ENV] ?? '').trim().toLowerCase());
}

// ── public API ───────────────────────────────────────────────────────────────

/**
 * Record one audit event. Returns the record as written (with `hash` when it
 * reached the ledger), or `null` when `RUNSPEC_AUDIT=off`.
 *
 * `event` is a dotted lowercase name you choose (`payment.refund`); `outcome`
 * a short word (`started`, `succeeded`, `failed`, `denied`…). Inside a parsed
 * run the event is tied to the run automatically.
 *
 * Throws `TypeError` for an invalid event or outcome (a programming error), and
 * `AuditError` with `strict` when no sink took the event; otherwise never throws.
 */
export function audit(event: string, outcome = '', opts: AuditOptions = {}): AuditRecord | null {
  if (typeof event !== 'string' || !EVENT_RE.test(event)) {
    throw new TypeError(`audit: event ${JSON.stringify(event)} must be lowercase letters, digits, '.', '_' or '-' (e.g. 'payment.refund')`);
  }
  const out = String(outcome ?? '').trim().toLowerCase();
  if (!OUTCOME_RE.test(out)) {
    throw new TypeError(`audit: outcome ${JSON.stringify(outcome)} must be a short lowercase word (e.g. 'succeeded')`);
  }
  if (auditDisabled()) return null;

  const ctx = _ctx;
  const root = ctx?.root ?? defaultRoot();
  let record = buildRecord(event, out, opts.target, opts.detail, ctx, root);
  const wrote: string[] = [];

  const ledger = ledgerTarget(opts.ledgerDir, root);
  if (ledger) {
    try {
      record = appendRecord(ledger, record);
      wrote.push('ledger');
    } catch {
      /* the OS log still gets it */
    }
  }

  const cfg = ctx?.auditCfg ?? { syslog: true, facility: 'auth' };
  const useOsLog = opts.syslog === undefined ? cfg.syslog : opts.syslog;
  if (useOsLog && osLog(record, cfg.facility)) wrote.push('os');

  if (ctx && isConfigured()) {
    try {
      getLogger(AUDIT_LOGGER).info(`audit ${event} ${out}`.trimEnd(), { event: 'audit', audit: record });
      wrote.push('run_log');
    } catch {
      /* never disrupt the caller */
    }
  }

  if (opts.strict && wrote.length === 0) {
    throw new AuditError(`audit: could not record '${event}' anywhere (ledger, OS log and run log all unavailable)`);
  }
  return record;
}

/** The ledger file in `logDir` (default: the project's `logs/`). */
export function ledgerPath(logDir?: string): string {
  return path.join(logDir ?? path.join(_ctx?.root ?? defaultRoot(), 'logs'), LEDGER_NAME);
}

/** The ledger's records in order (unparsable lines skipped — `verifyLedger` finds them). */
export function readLedger(file: string): AuditRecord[] {
  let text: string;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return [];
  }
  const out: AuditRecord[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      if (rec && typeof rec === 'object' && !Array.isArray(rec)) out.push(rec);
    } catch {
      /* skip */
    }
  }
  return out;
}

export interface VerifyResult {
  path: string;
  ok: boolean;
  records: number;
  first_bad: number | null;
  reason: string;
  last_hash: string;
}

/**
 * Check the ledger's hash chain. `first_bad` is the 1-based line number of the
 * first line that fails (`null` when intact). A missing file is ok with 0 records.
 */
export function verifyLedger(file: string): VerifyResult {
  const result: VerifyResult = { path: file, ok: true, records: 0, first_bad: null, reason: '', last_hash: GENESIS };
  let data: Buffer;
  try {
    data = fs.readFileSync(file);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') return result;
    return { ...result, ok: false, reason: `can't read: ${(e as Error).message}` };
  }
  const lines = data.toString('utf8').split('\n');
  let prev = GENESIS;
  for (let n = 1; n <= lines.length; n++) {
    const raw = lines[n - 1];
    if (!raw.trim()) continue;
    let rec: PyValue;
    try {
      rec = pyParse(raw.trim());
    } catch {
      return { ...result, ok: false, first_bad: n, reason: 'not a JSON record' };
    }
    if (!rec || typeof rec !== 'object' || Array.isArray(rec) || rec.constructor !== Object) {
      return { ...result, ok: false, first_bad: n, reason: 'not a JSON record' };
    }
    const obj = rec as Record<string, PyValue>;
    if (obj.prev !== prev) {
      return { ...result, ok: false, first_bad: n, reason: "chain broken — prev doesn't match the line before (a line was removed, inserted or reordered)" };
    }
    const { hash, ...body } = obj;
    if (hash !== hashBody(body)) {
      return { ...result, ok: false, first_bad: n, reason: 'hash mismatch — the line was edited' };
    }
    prev = hash as string;
    result.records += 1;
  }
  result.last_hash = prev;
  return result;
}

// ── internals ────────────────────────────────────────────────────────────────

/** Where an audit() outside a parsed run lands: the project found from the cwd, else home. */
function defaultRoot(): string {
  try {
    const { configPath } = findConfig(process.cwd());
    let dir = path.dirname(path.resolve(configPath));
    for (;;) {
      if (!dir.split(path.sep).includes('node_modules') && fs.existsSync(path.join(dir, 'package.json'))) return dir;
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
    return path.dirname(path.resolve(configPath));
  } catch {
    return os.homedir();
  }
}

export function hashBody(body: Record<string, unknown>): string {
  return 'sha256:' + crypto.createHash('sha256').update(canonicalJson(body), 'utf8').digest('hex');
}

function actor(): [string, number | null] {
  const uid = process.geteuid ? process.geteuid() : null;
  try {
    return [os.userInfo().username || String(uid ?? ''), uid];
  } catch {
    return [uid === null ? '' : String(uid), uid];
  }
}

function redactValue(v: unknown, key = ''): unknown {
  if (key && SENSITIVE_KEY_RE.test(key)) return '[REDACTED]';
  if (v === null || v === undefined) return null;
  if (Array.isArray(v)) return v.map((x) => redactValue(x));
  if (typeof v === 'string') return redact(v);
  if (typeof v === 'number' || typeof v === 'boolean') return v;
  if (typeof v === 'bigint') return v;
  if (v instanceof Date) return v.toISOString();
  if (typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) out[k] = redactValue(x, k);
    return out;
  }
  return redact(String(v));
}

/** UTC ISO-8601 the way Python's `datetime.now(timezone.utc).isoformat()` writes it. */
function isoNow(): string {
  return new Date().toISOString().replace(/\.(\d{3})Z$/, '.$1000+00:00');
}

function buildRecord(
  event: string,
  outcome: string,
  target: Record<string, unknown> | undefined,
  detail: Record<string, unknown> | undefined,
  ctx: RunContext | null,
  root: string,
): AuditRecord {
  const [who, uid] = actor();
  const envAgent = process.env.RUNSPEC_AGENT ?? '';
  return {
    v: RECORD_VERSION,
    ts: isoNow(),
    id: crypto.randomUUID(),
    event,
    outcome,
    actor: who,
    uid,
    host: os.hostname(),
    pid: process.pid,
    venv: root,
    runnable: ctx?.runnable ?? '',
    command: (ctx?.commandPath ?? []).join(' '),
    run_id: ctx?.runId ?? '',
    agent: ctx ? ctx.agent : !['', '0'].includes(envAgent),
    trigger: ctx?.trigger || process.env.RUNSPEC_TRIGGER || '',
    target: redactValue({ ...(target ?? {}) }),
    detail: redactValue({ ...(detail ?? {}) }),
  };
}

function writableDir(dir: string): boolean {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.accessSync(dir, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

/** The ledger has one writer: the owner of `logs/` (root too). */
function mayAppend(logDir: string, root: string): boolean {
  if (!process.geteuid) return writableDir(logDir);
  const euid = process.geteuid();
  try {
    if (!fs.existsSync(logDir)) {
      if (euid !== 0 && fs.statSync(root).uid !== euid) return false;
      if (!writableDir(logDir)) return false;
      if (euid === 0) chownLike(logDir, root);
      return true;
    }
    return euid === 0 || fs.statSync(logDir).uid === euid;
  } catch {
    return false;
  }
}

function ledgerTarget(ledgerDir: string | undefined, root: string): string | null {
  if (ledgerDir !== undefined) return path.join(ledgerDir, LEDGER_NAME);
  const projectLogs = path.join(root, 'logs');
  if (mayAppend(projectLogs, root)) return path.join(projectLogs, LEDGER_NAME);
  const homeLogs = path.join(os.homedir(), 'logs');
  if (path.resolve(homeLogs) !== path.resolve(projectLogs) && writableDir(homeLogs)) return path.join(homeLogs, LEDGER_NAME);
  return null;
}

function chownLike(file: string, like: string): void {
  try {
    const st = fs.statSync(like);
    fs.chownSync(file, st.uid, st.gid);
  } catch {
    /* best-effort */
  }
}

function sleepMs(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function pidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === 'EPERM';
  }
}

/** Take `lockPath` (exclusive create), taking over a stale one. Returns a release function. */
export function acquireLock(lockPath: string, waitMs = LOCK_WAIT_MS): () => void {
  const deadline = Date.now() + waitMs;
  for (;;) {
    try {
      const fd = fs.openSync(lockPath, 'wx', 0o644);
      try {
        fs.writeSync(fd, `${process.pid} ${os.hostname()}\n`);
      } finally {
        fs.closeSync(fd);
      }
      return () => {
        try {
          fs.unlinkSync(lockPath);
        } catch {
          /* already gone */
        }
      };
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e;
    }
    try {
      const st = fs.statSync(lockPath);
      const [pidText, host] = fs.readFileSync(lockPath, 'utf8').trim().split(/\s+/);
      const sameHost = !host || host === os.hostname();
      const dead = sameHost && pidText !== undefined && !pidAlive(parseInt(pidText, 10));
      if (dead || Date.now() - st.mtimeMs > LOCK_STALE_MS) {
        fs.unlinkSync(lockPath);
        continue;
      }
    } catch {
      continue; // released between our create and stat — try again
    }
    if (Date.now() >= deadline) throw new Error(`audit: ${lockPath} is held by another writer`);
    sleepMs(20);
  }
}

/** The `hash` of the ledger's last line (GENESIS when empty). An unparsable last
 *  line chains on the hash of its raw bytes, so verify still reports it. */
function lastHash(fd: number): string {
  const size = fs.fstatSync(fd).size;
  if (size === 0) return GENESIS;
  const start = Math.max(0, size - TAIL_BYTES);
  const buf = Buffer.alloc(size - start);
  fs.readSync(fd, buf, 0, buf.length, start);
  const lines: Buffer[] = [];
  let from = 0;
  for (let i = 0; i <= buf.length; i++) {
    if (i === buf.length || buf[i] === 0x0a) {
      const line = buf.subarray(from, i);
      if (line.toString('utf8').trim()) lines.push(line);
      from = i + 1;
    }
  }
  if (!lines.length) return GENESIS;
  const last = lines[lines.length - 1];
  try {
    const rec = JSON.parse(last.toString('utf8'));
    if (rec && typeof rec.hash === 'string' && rec.hash.startsWith('sha256:')) return rec.hash;
  } catch {
    /* fall through */
  }
  return 'sha256:' + crypto.createHash('sha256').update(last).digest('hex');
}

/** Append one chained line under the lock. Throws on failure (callers catch). */
export function appendRecord(file: string, record: AuditRecord): AuditRecord {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const created = !fs.existsSync(file);
  const release = acquireLock(`${file}.lock`);
  try {
    const flags = fs.constants.O_RDWR | fs.constants.O_APPEND | fs.constants.O_CREAT | (fs.constants.O_NOFOLLOW ?? 0);
    const fd = fs.openSync(file, flags, 0o644);
    try {
      if (created && process.geteuid && process.geteuid() === 0) chownLike(file, path.dirname(file));
      const body: AuditRecord = {};
      for (const [k, v] of Object.entries(record)) if (k !== 'prev' && k !== 'hash') body[k] = v;
      body.prev = lastHash(fd);
      body.hash = hashBody(body);
      fs.writeSync(fd, Buffer.from(pyDumps(body) + '\n', 'utf8'));
      try {
        fs.fsyncSync(fd);
      } catch {
        /* best-effort */
      }
      return body;
    } finally {
      fs.closeSync(fd);
    }
  } finally {
    release();
  }
}

/** Write the event to the OS log. True when a log daemon was there to take it. */
export function osLog(record: AuditRecord, facility = 'auth'): boolean {
  const line = `${SYSLOG_TAG} ${pyDumps(record, { compact: true })}`;
  if (process.platform === 'win32') return windowsEvent(line);
  if (!['/dev/log', '/var/run/syslog', '/var/run/log'].some(isSocket)) return false;
  const fac = FACILITIES.has(facility) ? facility : 'auth';
  const base = ['-t', `runspec[${process.pid}]`, '-p', `${fac}.notice`];
  try {
    // util-linux logger cuts messages at 1 KiB unless told otherwise; older and
    // BSD loggers don't know --size, so fall back to the plain form.
    let r = spawnSync('logger', [...base, '--size', '65536', '--', line], { stdio: 'ignore', timeout: 10000 });
    if (r.error) return false;
    if (r.status !== 0) r = spawnSync('logger', [...base, '--', line], { stdio: 'ignore', timeout: 10000 });
    return !r.error && r.status === 0;
  } catch {
    return false;
  }
}

function isSocket(p: string): boolean {
  try {
    return fs.statSync(p).isSocket();
  } catch {
    return false;
  }
}

function windowsEvent(message: string): boolean {
  try {
    const r = spawnSync(
      'eventcreate',
      ['/T', 'INFORMATION', '/ID', String(WINDOWS_EVENT_ID), '/L', 'APPLICATION', '/SO', 'runspec', '/D', message.slice(0, 1000)],
      { stdio: 'ignore', timeout: 10000, windowsHide: true },
    );
    return !r.error && r.status === 0;
  } catch {
    return false;
  }
}
