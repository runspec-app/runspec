/**
 * Configure a lightweight logger from [config.logging]. Zero new deps — uses
 * only Node stdlib (fs, path, os). Call configureLogging() once from parse();
 * runnables call getLogger(name) to obtain a named Logger.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import type { LoggingConfig } from './models';

// ── internal state ────────────────────────────────────────────────────────────

let _configured = false;
const _loggers = new Map<string, Logger>();
const _handlers: Handler[] = [];

const RUN_SUMMARY_LOGGER = 'runspec.runsummary';

interface CapturedException {
  type: string;
  message: string;
  traceback: string;
}

interface SummaryState {
  counter: RunSummaryCounter;
  start: bigint;
  runnable: string;
  autonomy: string | undefined;
  agent: boolean;
  commandPath: string[];
  exception: CapturedException | null;
  exitCode: number;
  emitted: boolean;
  user: string;
  userTarget: string | undefined;
}

let _summaryState: SummaryState | null = null;
let _exitHooksInstalled = false;

// ── level map ─────────────────────────────────────────────────────────────────

const LEVEL_NUM: Record<string, number> = {
  debug: 10, info: 20, warning: 30, error: 40, critical: 50,
};

const LEVEL_LABEL: Record<number, string> = {
  10: 'DEBUG', 20: 'INFO', 30: 'WARNING', 40: 'ERROR', 50: 'CRITICAL',
};

// ── sensitive data redaction ──────────────────────────────────────────────────

const SENSITIVE_KEY_RE = /^(password|passwd|pwd|token|api[_-]?key|secret)$/i;

const SENSITIVE: Array<[RegExp, string]> = [
  [/(password|passwd|pwd)\s*[=:]\s*\S+/gi, '$1=[REDACTED]'],
  [/(token|api[_-]?key|secret)\s*[=:]\s*\S+/gi, '$1=[REDACTED]'],
  [/Authorization:\s*(Bearer|Basic)\s+\S+/gi, 'Authorization: $1 [REDACTED]'],
  [/https?:\/\/[^:@\s]+:[^@\s]+@/g, 'https://[REDACTED]@'],
  [/"(password|token|api_key|secret)"\s*:\s*"[^"]*"/gi, '"$1": "[REDACTED]"'],
  [/(password|passwd|token)=([^&\s"]+)/gi, '$1=[REDACTED]'],
];

function redact(msg: string): string {
  try {
    for (const [pattern, replacement] of SENSITIVE) {
      msg = msg.replace(pattern, replacement);
    }
  } catch {
    // never disrupt logging on redaction errors
  }
  return msg;
}

// ── log record & handler ──────────────────────────────────────────────────────

interface LogRecord {
  ts: Date;
  levelNum: number;
  loggerName: string;
  message: string;
  error?: Error;
  extra?: Record<string, unknown>;
}

interface Handler {
  level: number;
  emit(record: LogRecord): void;
}

// ── Logger ────────────────────────────────────────────────────────────────────

export class Logger {
  constructor(private readonly name: string) {}

  debug(msg: string, fields?: Record<string, unknown>): void { this._emit(10, msg, fields); }
  info(msg: string, fields?: Record<string, unknown>): void { this._emit(20, msg, fields); }
  warning(msg: string, fields?: Record<string, unknown>): void { this._emit(30, msg, fields); }
  warn(msg: string, fields?: Record<string, unknown>): void { this._emit(30, msg, fields); }
  error(msg: string, fields?: Record<string, unknown>): void { this._emit(40, msg, fields); }
  critical(msg: string, fields?: Record<string, unknown>): void { this._emit(50, msg, fields); }

  private _emit(levelNum: number, message: string, fields?: Record<string, unknown>): void {
    if (_handlers.length === 0) return;
    const error = fields?.['error'] instanceof Error ? (fields['error'] as Error) : undefined;
    const extra = fields ? _extractExtra(fields) : undefined;
    const record: LogRecord = { ts: new Date(), levelNum, loggerName: this.name, message: redact(message), error, extra };
    for (const h of _handlers) {
      if (levelNum >= h.level) h.emit(record);
    }
  }
}

function _extractExtra(fields: Record<string, unknown>): Record<string, unknown> | undefined {
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(fields)) {
    if (k === 'error') continue;
    if (typeof v === 'string') {
      result[k] = SENSITIVE_KEY_RE.test(k) ? '[REDACTED]' : redact(v);
    } else {
      result[k] = v;
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

export function getLogger(name: string): Logger {
  let logger = _loggers.get(name);
  if (!logger) {
    logger = new Logger(name);
    _loggers.set(name, logger);
  }
  return logger;
}

// ── formatters ────────────────────────────────────────────────────────────────

function formatJson(record: LogRecord): string {
  const obj: Record<string, unknown> = {
    ts: record.ts.toISOString(),
    level: LEVEL_LABEL[record.levelNum] ?? String(record.levelNum),
    logger: record.loggerName,
    message: record.message,
  };
  if (record.error) obj['exc'] = record.error.stack ?? record.error.message;
  if (record.extra) obj['extra'] = record.extra;
  return JSON.stringify(obj);
}

function formatConsole(record: LogRecord, showTracebacks: boolean): string {
  // INFO is plain print; higher levels get a prefix so they stand out.
  // DEBUG (when shown) prepends "DEBUG" so the level is unambiguous on stdout.
  let line: string;
  if (record.levelNum <= 10) {
    line = `DEBUG ${record.loggerName}: ${record.message}`;
  } else if (record.levelNum <= 20) {
    line = record.message;
  } else {
    line = `${LEVEL_LABEL[record.levelNum] ?? String(record.levelNum)}: ${record.message}`;
  }
  if (record.extra) {
    const pairs = Object.entries(record.extra).map(([k, v]) => `${k}=${v}`).join(' ');
    line += `  {${pairs}}`;
  }
  if (showTracebacks && record.error) line += `\n${record.error.stack ?? record.error.message}`;
  return line;
}

// ── console handlers ──────────────────────────────────────────────────────────

/**
 * Routes DEBUG/INFO records (i.e. below WARNING) to stdout.
 * Treated as the runnable's primary output — captured as the response body
 * when `runspec serve` invokes the runnable as a subprocess.
 *
 * Drops `runspec.runsummary` records — those are file-only; the human form
 * of the summary is written directly to stderr by the exit hook.
 */
class StdoutHandler implements Handler {
  constructor(public readonly level: number, private readonly showTracebacks: boolean) {}

  emit(record: LogRecord): void {
    if (record.levelNum >= 30) return; // WARNING+ belongs on stderr
    if (record.loggerName === RUN_SUMMARY_LOGGER) return;
    try {
      process.stdout.write(formatConsole(record, this.showTracebacks) + '\n');
    } catch {
      // never disrupt
    }
  }
}

/**
 * Routes WARNING+ records to stderr (Unix convention for diagnostics).
 * Floor is always WARNING regardless of the configured level — warnings and
 * errors must not be silenced even if the runnable raises the threshold above.
 */
class StderrHandler implements Handler {
  readonly level = 30; // WARNING

  constructor(private readonly showTracebacks: boolean) {}

  emit(record: LogRecord): void {
    if (record.levelNum < 30) return;
    if (record.loggerName === RUN_SUMMARY_LOGGER) return;
    try {
      process.stderr.write(formatConsole(record, this.showTracebacks) + '\n');
    } catch {
      // never disrupt
    }
  }
}

// ── run summary counter ───────────────────────────────────────────────────────

/**
 * Counts records by level. Emits nothing — read at process exit by the
 * summary hook. Always attached at level=DEBUG so every record is counted.
 */
class RunSummaryCounter implements Handler {
  readonly level = 10;
  readonly counts: Record<string, number> = {
    DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0,
  };

  emit(record: LogRecord): void {
    // Don't count the summary record itself.
    if (record.loggerName === RUN_SUMMARY_LOGGER) return;
    const label = LEVEL_LABEL[record.levelNum];
    if (label && label in this.counts) {
      this.counts[label]++;
    }
  }
}

// ── rotating file handlers ────────────────────────────────────────────────────

function doRotate(logPath: string, keep: number): void {
  for (let i = keep; i >= 1; i--) {
    const src = `${logPath}.${i}`;
    if (i === keep) {
      try { fs.unlinkSync(src); } catch { /* already gone */ }
    } else {
      try { fs.renameSync(src, `${logPath}.${i + 1}`); } catch { /* missing backup */ }
    }
  }
  try { fs.renameSync(logPath, `${logPath}.1`); } catch { /* current file missing */ }
}

class SizeRotatingFileHandler implements Handler {
  constructor(
    private readonly logPath: string,
    private readonly maxBytes: number,
    private readonly keep: number,
    readonly level: number,
  ) {}

  emit(record: LogRecord): void {
    try {
      this._rotateIfNeeded();
      fs.appendFileSync(this.logPath, formatJson(record) + '\n', 'utf-8');
    } catch {
      // never disrupt
    }
  }

  private _rotateIfNeeded(): void {
    try {
      if (fs.statSync(this.logPath).size < this.maxBytes) return;
    } catch {
      return; // file doesn't exist yet
    }
    doRotate(this.logPath, this.keep);
  }
}

function _periodForDate(d: Date, when: 'daily' | 'midnight' | 'weekly'): string {
  if (when === 'weekly') {
    const tmp = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    const day = tmp.getUTCDay() || 7;
    tmp.setUTCDate(tmp.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(tmp.getUTCFullYear(), 0, 1));
    const week = Math.ceil(((tmp.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
    return `${tmp.getUTCFullYear()}-W${week}`;
  }
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

class TimedRotatingFileHandler implements Handler {
  constructor(
    private readonly logPath: string,
    private readonly when: 'daily' | 'midnight' | 'weekly',
    private readonly keep: number,
    readonly level: number,
  ) {}

  emit(record: LogRecord): void {
    try {
      this._rotateIfNeeded();
      fs.appendFileSync(this.logPath, formatJson(record) + '\n', 'utf-8');
    } catch {
      // never disrupt
    }
  }

  private _rotateIfNeeded(): void {
    let filePeriod: string;
    try {
      filePeriod = _periodForDate(fs.statSync(this.logPath).mtime, this.when);
    } catch {
      return; // file doesn't exist yet
    }
    if (filePeriod === _periodForDate(new Date(), this.when)) return;
    doRotate(this.logPath, this.keep);
  }
}

// ── size/rotate parser ────────────────────────────────────────────────────────

const SIZE_RE = /^(\d+(?:\.\d+)?)\s*(KB|MB|GB)$/i;
const SIZE_MULT: Record<string, number> = { KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 };
const TIMED_KEYS = new Set(['daily', 'midnight', 'weekly']);

function makeFileHandler(logPath: string, rotate: string, keep: number, level: number): Handler {
  const sizeMatch = SIZE_RE.exec(rotate);
  if (sizeMatch) {
    const maxBytes = Math.round(parseFloat(sizeMatch[1]) * SIZE_MULT[sizeMatch[2].toUpperCase()]);
    return new SizeRotatingFileHandler(logPath, maxBytes, keep, level);
  }
  const when = rotate.toLowerCase();
  if (TIMED_KEYS.has(when)) {
    return new TimedRotatingFileHandler(logPath, when as 'daily' | 'midnight' | 'weekly', keep, level);
  }
  throw new Error(
    `✗  [config.logging] rotate ${JSON.stringify(rotate)} not recognised.\n` +
    `   Valid: '10 MB', '100 KB', '1 GB', 'daily', 'midnight', 'weekly'`,
  );
}

// ── log dir resolution ────────────────────────────────────────────────────────

/**
 * Walk up from `start` looking for a `package.json` that is NOT inside a
 * `node_modules` directory — that's the project root. Returns null if we
 * reach the filesystem root without finding one.
 *
 * Skipping node_modules is intentional: dependency packages bundle their
 * own package.json, but they're not the project root the user owns.
 */
function findProjectRoot(start: string): string | null {
  let dir = path.resolve(start);
  while (true) {
    if (!dir.split(path.sep).includes('node_modules')) {
      if (fs.existsSync(path.join(dir, 'package.json'))) return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) return null; // hit filesystem root
    dir = parent;
  }
}

/**
 * Resolve the log directory.
 *
 * Mirror of Python's `sys.prefix / "logs"`: pick the project's installation
 * root — the nearest ancestor `package.json` of the runnable's runspec.toml,
 * skipping anything under `node_modules`. Logs land at `{project_root}/logs/`,
 * so one logs directory per project, surviving reinstalls.
 *
 * Falls back to `~/logs/` when no project root is found or the chosen
 * directory is not writable (e.g. read-only volumes, system installs).
 */
function resolveLogDir(configPath: string): string {
  const projectRoot = findProjectRoot(path.dirname(configPath));
  if (projectRoot) {
    const candidate = path.join(projectRoot, 'logs');
    try {
      fs.mkdirSync(candidate, { recursive: true });
      const probe = path.join(candidate, '.wtest');
      fs.writeFileSync(probe, '');
      fs.unlinkSync(probe);
      return candidate;
    } catch {
      // fall through to home
    }
  }
  const fallback = path.join(os.homedir(), 'logs');
  fs.mkdirSync(fallback, { recursive: true });
  return fallback;
}

// ── run summary ──────────────────────────────────────────────────────────────

function _getInvoker(): [string, string | undefined] {
  const sudoUser = process.env['SUDO_USER'];
  if (sudoUser) {
    const target = process.env['USER'] || process.env['LOGNAME'] || 'root';
    return [sudoUser, target];
  }
  const user = process.env['USER'] || process.env['LOGNAME'] || process.env['USERNAME'] || 'unknown';
  return [user, undefined];
}

function formatSummaryLine(state: SummaryState, durationMs: number, exitCode: number): string {
  const counts = state.counter.counts;
  const total = (counts.DEBUG ?? 0) + (counts.INFO ?? 0) + (counts.WARNING ?? 0) + (counts.ERROR ?? 0) + (counts.CRITICAL ?? 0);
  const warnings = counts.WARNING ?? 0;
  const errors = (counts.ERROR ?? 0) + (counts.CRITICAL ?? 0);
  const secs = (durationMs / 1000).toFixed(2);
  const runnable = state.runnable;
  const wSuffix = warnings === 1 ? '' : 's';
  const eSuffix = errors === 1 ? '' : 's';
  const userPart = state.userTarget
    ? ` | user: ${state.user} → ${state.userTarget} (sudo)`
    : ` | user: ${state.user}`;
  if (state.exception || exitCode !== 0) {
    const excPart = state.exception ? `, ${state.exception.type}` : '';
    return `runspec: ${runnable} failed in ${secs}s — exit ${exitCode}${excPart} — ${total} events (${warnings} warning${wSuffix}, ${errors} error${eSuffix})${userPart}`;
  }
  return `runspec: ${runnable} completed in ${secs}s — ${total} events (${warnings} warning${wSuffix}, ${errors} error${eSuffix})${userPart}`;
}

/**
 * Emit one summary record to the file (via the standard logger pipeline —
 * picked up by the file handler, dropped by the console handlers) and one
 * formatted line directly to stderr. Idempotent — safe to call repeatedly.
 */
export function emitRunSummary(): void {
  const state = _summaryState;
  if (state === null || state.emitted) return;
  state.emitted = true;

  const durationMs = Number((process.hrtime.bigint() - state.start) / 1_000_000n);
  // Exit code: explicit capture (set by uncaughtException hook) or
  // process.exitCode if the user set it. Defaults to 0.
  const exitCode = state.exitCode !== 0 ? state.exitCode : (state.exception ? 1 : 0);

  try {
    getLogger(RUN_SUMMARY_LOGGER).info('run completed', {
      event: 'run_summary',
      runnable: state.runnable,
      command_path: state.commandPath,
      duration_ms: durationMs,
      exit_code: exitCode,
      agent: state.agent,
      autonomy: state.autonomy,
      exception: state.exception,
      events: { ...state.counter.counts },
      user: state.user,
      user_target: state.userTarget ?? null,
    });
  } catch {
    // never disrupt shutdown
  }

  try {
    process.stderr.write(formatSummaryLine(state, durationMs, exitCode) + '\n');
  } catch {
    // never disrupt shutdown
  }
}

function installExitHooks(): void {
  if (_exitHooksInstalled) return;
  _exitHooksInstalled = true;

  process.on('exit', (code) => {
    if (_summaryState && !_summaryState.emitted) {
      // process.exitCode wins over the explicit exception capture only if
      // it's non-zero — uncaughtException already set state.exitCode=1.
      if (code !== 0 && _summaryState.exitCode === 0) _summaryState.exitCode = code;
      emitRunSummary();
    }
  });

  // Skip the crash-handlers under jest — they call process.exit(1), which
  // would tear down the test runner if any test ever produced an unhandled
  // rejection. The 'exit' hook above is harmless and still runs.
  if (process.env['JEST_WORKER_ID'] !== undefined) return;

  process.on('uncaughtException', (err: Error) => {
    if (_summaryState) {
      _summaryState.exception = {
        type: err.name || 'Error',
        message: err.message || String(err),
        traceback: err.stack ?? '',
      };
      _summaryState.exitCode = 1;
    }
    // Preserve default Node behaviour: print and exit non-zero. The 'exit'
    // hook above will fire and run emitRunSummary().
    process.stderr.write((err.stack ?? String(err)) + '\n');
    process.exit(1);
  });

  process.on('unhandledRejection', (reason: unknown) => {
    if (_summaryState) {
      const err = reason instanceof Error ? reason : new Error(String(reason));
      _summaryState.exception = {
        type: err.name || 'Error',
        message: err.message || String(reason),
        traceback: err.stack ?? '',
      };
      _summaryState.exitCode = 1;
    }
    process.stderr.write(`Unhandled rejection: ${reason instanceof Error ? (reason.stack ?? reason.message) : String(reason)}\n`);
    process.exit(1);
  });
}

// ── public: configureLogging ──────────────────────────────────────────────────

export interface ConfigureLoggingOptions {
  logCfg: LoggingConfig | undefined;
  runnableName: string;
  configPath: string;
  debug?: boolean;
  noSummary?: boolean;
  autonomy?: string;
  agent?: boolean;
  commandPath?: string[];
}

/**
 * Configure handlers from normalised [config.logging]. No-op when logCfg is
 * undefined. Idempotent — second call is silently ignored.
 *
 * Console routing follows Unix stream conventions so a single `logger.X` call
 * works in both CLI mode (terminal output) and agent mode (captured by
 * `runspec serve` as the MCP tool response):
 *
 *   INFO     → stdout (plain message — reads like a print() call)
 *   WARNING+ → stderr (prefixed with the level name)
 *
 * DEBUG is suppressed by default on both stdout and the file. Pass
 * `debug: true` (set by the auto-added `--debug` flag / RUNSPEC_ARG_DEBUG env
 * var) to include DEBUG records (and tracebacks on stdout) everywhere.
 * One knob — stdout and file move together. Stderr stays pinned at
 * WARNING regardless.
 *
 * File handler is always JSON; level follows the same `--debug` toggle as
 * stdout (defaults to INFO — keeps third-party DEBUG noise out of the audit
 * log). Log files land under `{project_root}/logs/` — the nearest ancestor
 * `package.json` skipping `node_modules`, mirroring Python's venv-root
 * convention. Falls back to `~/logs/` when no project root is found.
 *
 * Run summary (when `logCfg.summary` is true and `noSummary` is false)
 * counts log events by level and emits a single record at process exit
 * with duration, exit code, exception class, and per-level counts.
 */
export function configureLogging(opts: ConfigureLoggingOptions): void {
  if (!opts.logCfg || _configured) return;

  const debug = opts.debug ?? false;
  const floor = debug ? LEVEL_NUM['debug'] : LEVEL_NUM['info'];

  _handlers.push(new StdoutHandler(floor, debug));
  _handlers.push(new StderrHandler(debug));

  const logDir = resolveLogDir(opts.configPath);
  const logPath = path.join(logDir, `${opts.runnableName}.log`);
  _handlers.push(makeFileHandler(logPath, opts.logCfg.rotate, opts.logCfg.keep, floor));

  // Always attach the counter — cost is one dict increment per log call.
  // Only the exit hook + state population are conditional on summary mode.
  const counter = new RunSummaryCounter();
  _handlers.push(counter);

  const runnablePrefix = opts.runnableName.toUpperCase().replace(/-/g, '_');
  const summaryEnabled =
    opts.logCfg.summary !== false &&
    !opts.noSummary &&
    !['1', 'true', 'yes'].includes((process.env[`RUNSPEC_${runnablePrefix}_ARG_NO_SUMMARY`] ?? '').toLowerCase());

  if (summaryEnabled) {
    const [user, userTarget] = _getInvoker();
    _summaryState = {
      counter,
      start: process.hrtime.bigint(),
      runnable: opts.runnableName,
      autonomy: opts.autonomy,
      agent: opts.agent ?? false,
      commandPath: opts.commandPath ?? [],
      exception: null,
      exitCode: 0,
      emitted: false,
      user,
      userTarget,
    };
    installExitHooks();
  }

  _configured = true;
}

// ── test helper ───────────────────────────────────────────────────────────────

export function _resetForTest(): void {
  _configured = false;
  _loggers.clear();
  _handlers.length = 0;
  _summaryState = null;
  // Note: process event listeners installed by installExitHooks() stay —
  // they no-op when _summaryState is null, which is the test-time state.
}

export { _periodForDate, RUN_SUMMARY_LOGGER };
