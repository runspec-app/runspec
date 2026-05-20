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

function formatHuman(record: LogRecord, showTracebacks: boolean): string {
  const hh = String(record.ts.getHours()).padStart(2, '0');
  const mm = String(record.ts.getMinutes()).padStart(2, '0');
  const ss = String(record.ts.getSeconds()).padStart(2, '0');
  const time = `${hh}:${mm}:${ss}`;
  const label = (LEVEL_LABEL[record.levelNum] ?? String(record.levelNum)).padEnd(8);
  let line = `${time} ${label} ${record.loggerName}: ${record.message}`;
  if (record.extra) {
    const pairs = Object.entries(record.extra).map(([k, v]) => `${k}=${v}`).join(' ');
    line += `  {${pairs}}`;
  }
  if (showTracebacks && record.error) line += `\n${record.error.stack ?? record.error.message}`;
  return line;
}

// ── console handler ───────────────────────────────────────────────────────────

class ConsoleHandler implements Handler {
  constructor(public readonly level: number, private readonly showTracebacks: boolean) {}

  emit(record: LogRecord): void {
    try {
      process.stderr.write(formatHuman(record, this.showTracebacks) + '\n');
    } catch {
      // never disrupt
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
  readonly level = 10; // always DEBUG

  constructor(
    private readonly logPath: string,
    private readonly maxBytes: number,
    private readonly keep: number,
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
  readonly level = 10;

  constructor(
    private readonly logPath: string,
    private readonly when: 'daily' | 'midnight' | 'weekly',
    private readonly keep: number,
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

function makeFileHandler(logPath: string, rotate: string, keep: number): Handler {
  const sizeMatch = SIZE_RE.exec(rotate);
  if (sizeMatch) {
    const maxBytes = Math.round(parseFloat(sizeMatch[1]) * SIZE_MULT[sizeMatch[2].toUpperCase()]);
    return new SizeRotatingFileHandler(logPath, maxBytes, keep);
  }
  const when = rotate.toLowerCase();
  if (TIMED_KEYS.has(when)) {
    return new TimedRotatingFileHandler(logPath, when as 'daily' | 'midnight' | 'weekly', keep);
  }
  throw new Error(
    `✗  [config.logging] rotate ${JSON.stringify(rotate)} not recognised.\n` +
    `   Valid: '10 MB', '100 KB', '1 GB', 'daily', 'midnight', 'weekly'`,
  );
}

// ── log dir resolution ────────────────────────────────────────────────────────

function resolveLogDir(configPath: string): string {
  const candidate = path.join(path.dirname(configPath), 'logs');
  try {
    fs.mkdirSync(candidate, { recursive: true });
    const probe = path.join(candidate, '.wtest');
    fs.writeFileSync(probe, '');
    fs.unlinkSync(probe);
    return candidate;
  } catch {
    const fallback = path.join(os.homedir(), 'logs');
    fs.mkdirSync(fallback, { recursive: true });
    return fallback;
  }
}

// ── public: configureLogging ──────────────────────────────────────────────────

export interface ConfigureLoggingOptions {
  logCfg: LoggingConfig | undefined;
  agent: boolean;
  runnableName: string;
  configPath: string;
  logLevelOverride?: string;
}

export function configureLogging(opts: ConfigureLoggingOptions): void {
  if (!opts.logCfg || _configured) return;

  const effectiveLevelName = opts.logLevelOverride ?? opts.logCfg.level;
  const effectiveLevel = LEVEL_NUM[effectiveLevelName] ?? LEVEL_NUM['info'];

  if (!opts.agent) {
    _handlers.push(new ConsoleHandler(effectiveLevel, effectiveLevelName === 'debug'));
  }

  const logDir = resolveLogDir(opts.configPath);
  const logPath = path.join(logDir, `${opts.runnableName}.log`);
  _handlers.push(makeFileHandler(logPath, opts.logCfg.rotate, opts.logCfg.keep));

  _configured = true;
}

// ── test helper ───────────────────────────────────────────────────────────────

export function _resetForTest(): void {
  _configured = false;
  _loggers.clear();
  _handlers.length = 0;
}

export { _periodForDate };
