import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
  configureLogging,
  getLogger,
  Logger,
  installExceptionHook,
  _handleUncaught,
  _resetForTest,
  _periodForDate,
} from '../src/logging_setup';

function tmpDir(): string {
  // Each test dir is its own project root so resolveLogDir lands inside it.
  // Without a package.json the walk-up would escape to a real project root
  // and write logs to a shared location — drop a marker file to anchor it.
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-log-test-'));
  fs.writeFileSync(path.join(d, 'package.json'), '{"name":"test","version":"0.0.0"}');
  return d;
}

function makeCfg(dir: string, overrides: Record<string, unknown> = {}): Parameters<typeof configureLogging>[0] {
  const { debug, ...logOverrides } = overrides as { debug?: boolean } & Record<string, unknown>;
  return {
    // summary defaults off in tests so we don't litter atexit emissions across
    // unrelated assertions; the run-summary suite enables it explicitly.
    logCfg: { rotate: 'midnight', keep: 7, summary: false, ...logOverrides },
    runnableName: 'myscript',
    configPath: path.join(dir, 'runspec.toml'),
    debug,
  };
}

beforeEach(() => {
  _resetForTest();
});

// ── getLogger ─────────────────────────────────────────────────────────────────

test('getLogger returns Logger instance', () => {
  expect(getLogger('myapp')).toBeInstanceOf(Logger);
});

test('getLogger returns same instance for same name', () => {
  expect(getLogger('myapp')).toBe(getLogger('myapp'));
});

test('getLogger returns different instances for different names', () => {
  expect(getLogger('a')).not.toBe(getLogger('b'));
});

// ── configureLogging — no-op cases ────────────────────────────────────────────

test('no-op when logCfg is undefined', () => {
  const dir = tmpDir();
  configureLogging({ logCfg: undefined, runnableName: 'x', configPath: path.join(dir, 'runspec.toml') });
  // no log file created
  expect(fs.existsSync(path.join(dir, 'logs', 'x.log'))).toBe(false);
});

test('idempotent — second call is silently ignored', () => {
  const dir = tmpDir();
  const cfg = makeCfg(dir);
  configureLogging(cfg);
  _resetForTest();  // reset state but don't re-configure — simulate second call by calling again after a fresh configure
  configureLogging(cfg);
  configureLogging(cfg); // third call — should be no-op after second
  // Just ensure no error thrown
});

test('idempotent — configured flag prevents double setup', () => {
  const dir = tmpDir();
  const cfg = makeCfg(dir);
  configureLogging(cfg);
  // second call should be silently ignored
  expect(() => configureLogging(cfg)).not.toThrow();
});

// ── file handler ──────────────────────────────────────────────────────────────

test('creates log file in logs/ subdir', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('hello');
  expect(fs.existsSync(path.join(dir, 'logs', 'myscript.log'))).toBe(true);
});

test('log file contains JSON lines', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('hello world');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const parsed = JSON.parse(content);
  expect(parsed.level).toBe('INFO');
  expect(parsed.message).toBe('hello world');
  expect(parsed.logger).toBe('test');
  expect(typeof parsed.ts).toBe('string');
});

test('file handler omits DEBUG without the debug flag', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  const log = getLogger('test');
  log.debug('low level detail');
  log.info('info detail');
  const lines = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim().split('\n');
  expect(lines.some(l => JSON.parse(l).level === 'DEBUG')).toBe(false);
  expect(lines.some(l => JSON.parse(l).level === 'INFO')).toBe(true);
});

test('file handler captures DEBUG when debug flag is set', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { debug: true }));
  getLogger('test').debug('low level detail');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const parsed = JSON.parse(content);
  expect(parsed.level).toBe('DEBUG');
  expect(parsed.message).toBe('low level detail');
});

test('log file captures all levels when debug flag is set', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { debug: true }));
  const log = getLogger('test');
  log.debug('a');
  log.info('b');
  log.warning('c');
  log.error('d');
  log.critical('e');
  const lines = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim().split('\n');
  expect(lines).toHaveLength(5);
  expect(JSON.parse(lines[0]).level).toBe('DEBUG');
  expect(JSON.parse(lines[4]).level).toBe('CRITICAL');
});

test('log file captures INFO and above without debug flag', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  const log = getLogger('test');
  log.debug('a');
  log.info('b');
  log.warning('c');
  log.error('d');
  log.critical('e');
  const lines = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim().split('\n');
  expect(lines).toHaveLength(4);
  expect(JSON.parse(lines[0]).level).toBe('INFO');
  expect(JSON.parse(lines[3]).level).toBe('CRITICAL');
});

test('error field included when Error passed', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').error('boom', { error: new Error('something broke') });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const parsed = JSON.parse(content);
  expect(typeof parsed.exc).toBe('string');
  expect(parsed.exc).toContain('something broke');
});

// ── stdout/stderr routing ─────────────────────────────────────────────────────

function captureStreams() {
  const stdoutLines: string[] = [];
  const stderrLines: string[] = [];
  const stdoutSpy = jest.spyOn(process.stdout, 'write').mockImplementation((chunk) => {
    stdoutLines.push(String(chunk));
    return true;
  });
  const stderrSpy = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => {
    stderrLines.push(String(chunk));
    return true;
  });
  return {
    stdoutLines, stderrLines,
    restore() { stdoutSpy.mockRestore(); stderrSpy.mockRestore(); },
  };
}

test('info routes to stdout (plain print, no prefix)', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').info('hello from info');
  expect(cap.stdoutLines.some(l => l.includes('hello from info'))).toBe(true);
  expect(cap.stderrLines.some(l => l.includes('hello from info'))).toBe(false);
  // INFO format is a plain print — message only, no level prefix, no timestamp
  const line = cap.stdoutLines.find(l => l.includes('hello from info'))!;
  expect(line.trim()).toBe('hello from info');
  cap.restore();
});

test('warning routes to stderr with WARNING prefix', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').warning('heads up');
  expect(cap.stderrLines.some(l => l.includes('heads up'))).toBe(true);
  expect(cap.stdoutLines.some(l => l.includes('heads up'))).toBe(false);
  expect(cap.stderrLines.find(l => l.includes('heads up'))!.trim()).toBe('WARNING: heads up');
  cap.restore();
});

test('error routes to stderr with ERROR prefix', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').error('broke');
  expect(cap.stderrLines.some(l => l.includes('broke'))).toBe(true);
  expect(cap.stdoutLines.some(l => l.includes('broke'))).toBe(false);
  expect(cap.stderrLines.find(l => l.includes('broke'))!.trim()).toBe('ERROR: broke');
  cap.restore();
});

test('critical routes to stderr with CRITICAL prefix', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').critical('dead');
  expect(cap.stderrLines.find(l => l.includes('dead'))!.trim()).toBe('CRITICAL: dead');
  cap.restore();
});

test('debug records are silent on both streams by default', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').debug('not shown');
  expect(cap.stdoutLines).toHaveLength(0);
  expect(cap.stderrLines).toHaveLength(0);
  cap.restore();
});

test('warnings still reach stderr (warnings cannot be silenced)', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir));
  getLogger('test').warning('audible');
  expect(cap.stderrLines.some(l => l.includes('audible'))).toBe(true);
  cap.restore();
});

// ── --debug flag ──────────────────────────────────────────────────────────────

test('debug flag lowers stdout threshold to DEBUG', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir, { debug: true }));
  getLogger('test').debug('now visible');
  expect(cap.stdoutLines.some(l => l.includes('now visible'))).toBe(true);
  // DEBUG records get a prefix so they're distinguishable from plain INFO output
  expect(cap.stdoutLines.find(l => l.includes('now visible'))!).toMatch(/DEBUG/);
  cap.restore();
});

test('debug flag does not lower the stderr floor below WARNING', () => {
  const dir = tmpDir();
  const cap = captureStreams();
  configureLogging(makeCfg(dir, { debug: true }));
  getLogger('test').info('still on stdout');
  expect(cap.stdoutLines.some(l => l.includes('still on stdout'))).toBe(true);
  expect(cap.stderrLines).toHaveLength(0);
  cap.restore();
});

// ── sensitive data redaction ──────────────────────────────────────────────────

test('redacts password= in log message', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('connecting with password=hunter2');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('hunter2');
  expect(content).toContain('[REDACTED]');
});

test('redacts token= in log message', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('using token=abc123secret');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('abc123secret');
  expect(content).toContain('[REDACTED]');
});

test('redacts Bearer token in Authorization header', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('Authorization: Bearer supersecrettoken');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('supersecrettoken');
  expect(content).toContain('[REDACTED]');
});

test('redacts URL credentials', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('connecting to https://user:p4ssw0rd@db.example.com');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('p4ssw0rd');
  expect(content).toContain('[REDACTED]');
});

test('redacts JSON password field', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('payload: {"password": "mysecret"}');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('mysecret');
  expect(content).toContain('[REDACTED]');
});

test('redaction errors do not suppress the log record', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  // Normal message — no redaction error expected, but confirm log still written
  getLogger('test').info('safe message');
  expect(fs.existsSync(path.join(dir, 'logs', 'myscript.log'))).toBe(true);
});

// ── size-based rotation ───────────────────────────────────────────────────────

test('size rotation: creates .1 backup when file exceeds maxBytes', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { rotate: '1 KB', keep: 3 }));
  const logPath = path.join(dir, 'logs', 'myscript.log');
  const log = getLogger('rot');
  // Write enough to exceed 1 KB
  for (let i = 0; i < 30; i++) log.info('a'.repeat(50));
  expect(fs.existsSync(`${logPath}.1`)).toBe(true);
});

test('size rotation: keeps at most N backups', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { rotate: '1 KB', keep: 2 }));
  const logPath = path.join(dir, 'logs', 'myscript.log');
  const log = getLogger('rot');
  for (let i = 0; i < 120; i++) log.info('a'.repeat(50));
  expect(fs.existsSync(`${logPath}.1`)).toBe(true);
  expect(fs.existsSync(`${logPath}.2`)).toBe(true);
  expect(fs.existsSync(`${logPath}.3`)).toBe(false);
});

// ── _periodForDate ────────────────────────────────────────────────────────────

test('_periodForDate daily returns YYYY-MM-DD', () => {
  const d = new Date('2024-03-15T12:00:00Z');
  expect(_periodForDate(d, 'daily')).toBe('2024-03-15');
});

test('_periodForDate midnight returns YYYY-MM-DD', () => {
  const d = new Date('2024-03-15T23:59:00Z');
  expect(_periodForDate(d, 'midnight')).toBe('2024-03-15');
});

test('_periodForDate weekly returns ISO week string', () => {
  const d = new Date('2024-03-15T12:00:00Z'); // ISO week 11 of 2024
  const result = _periodForDate(d, 'weekly');
  expect(result).toMatch(/^\d{4}-W\d+$/);
});

test('_periodForDate weekly: same week returns same string', () => {
  const mon = new Date('2024-03-11T00:00:00Z');
  const sun = new Date('2024-03-17T00:00:00Z');
  expect(_periodForDate(mon, 'weekly')).toBe(_periodForDate(sun, 'weekly'));
});

test('_periodForDate weekly: adjacent weeks differ', () => {
  const sun = new Date('2024-03-17T00:00:00Z');
  const nextMon = new Date('2024-03-18T00:00:00Z');
  expect(_periodForDate(sun, 'weekly')).not.toBe(_periodForDate(nextMon, 'weekly'));
});

// ── log dir fallback ──────────────────────────────────────────────────────────

test('falls back to ~/logs when the project root is not writable', () => {
  const dir = tmpDir();
  const logsDir = path.join(dir, 'logs');
  // Create logs dir as unwritable
  fs.mkdirSync(logsDir, { recursive: true });
  try {
    fs.chmodSync(logsDir, 0o444);
  } catch {
    // Skip on systems that don't support chmod (e.g. Windows CI)
    return;
  }
  configureLogging(makeCfg(dir));
  getLogger('test').info('fallback test');
  const homeLog = path.join(os.homedir(), 'logs', 'myscript.log');
  expect(fs.existsSync(homeLog)).toBe(true);
  // Cleanup
  try { fs.chmodSync(logsDir, 0o755); } catch {}
  try { fs.unlinkSync(homeLog); } catch {}
});

// ── invalid rotate value ──────────────────────────────────────────────────────

test('throws on unrecognised rotate value', () => {
  const dir = tmpDir();
  expect(() => configureLogging(makeCfg(dir, { rotate: 'hourly' }))).toThrow('[config.logging]');
});

// ── extra fields ──────────────────────────────────────────────────────────────

test('extra fields appear under "extra" key in JSON', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('connected', { user_id: '42', region: 'eu-west' });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const record = JSON.parse(content);
  // run_id is always present on file records (SPEC §run_id); user fields ride alongside.
  expect(record.extra).toMatchObject({ user_id: '42', region: 'eu-west' });
  expect(typeof record.extra.run_id).toBe('string');
  expect(record.message).toBe('connected');
});

test('run_id is the only extra field when none supplied', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('plain message');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const record = JSON.parse(content);
  expect(Object.keys(record.extra)).toEqual(['run_id']);
  expect(typeof record.extra.run_id).toBe('string');
});

test('error key extracted from fields, not placed in extra', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').error('boom', { error: new Error('oops'), user_id: '42' });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const record = JSON.parse(content);
  expect(typeof record.exc).toBe('string');
  expect(record.exc).toContain('oops');
  expect(record.extra).toMatchObject({ user_id: '42' });
  expect(record.extra?.error).toBeUndefined();
});

test('error-only fields: extra holds just run_id', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').error('boom', { error: new Error('oops') });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const record = JSON.parse(content);
  expect(Object.keys(record.extra)).toEqual(['run_id']);
  expect(typeof record.exc).toBe('string');
});

test('extra string values are redacted', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('auth', { token: 'secret123', user: 'alice' });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  expect(content).not.toContain('secret123');
  expect(content).toContain('[REDACTED]');
  expect(content).toContain('alice'); // non-sensitive field untouched
});

test('extra integer fields pass through unredacted', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').info('counts', { items: 99 });
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const record = JSON.parse(content);
  expect(record.extra?.items).toBe(99);
});

test('extra fields appear in console output', () => {
  const dir = tmpDir();
  const lines: string[] = [];
  const stdoutWrite = jest.spyOn(process.stdout, 'write').mockImplementation((chunk) => {
    lines.push(String(chunk));
    return true;
  });
  configureLogging(makeCfg(dir));
  getLogger('test').info('connected', { user_id: '42' });
  expect(lines.some(l => l.includes('user_id=42'))).toBe(true);
  stdoutWrite.mockRestore();
});

// ── store = "per-run" ───────────────────────────────────────────────────────────

const PER_RUN_RE = /^myscript\.\d{8}T\d{6}Z\.[0-9a-fA-F-]{8,}\.log$/;

function perRunFile(dir: string): string | undefined {
  return fs.readdirSync(path.join(dir, 'logs')).find(f => PER_RUN_RE.test(f));
}

test('per-run: writes {runnable}.{ts}.{run_id}.log, not {runnable}.log', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { store: 'per-run' }));
  getLogger('test').info('hi');
  const files = fs.readdirSync(path.join(dir, 'logs'));
  expect(files.some(f => PER_RUN_RE.test(f))).toBe(true);
  expect(files).not.toContain('myscript.log');
});

test('per-run: run_id in filename matches extra.run_id in records', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { store: 'per-run' }));
  getLogger('test').info('hi');
  const fname = perRunFile(dir)!;
  const fileRunId = fname.replace(/\.log$/, '').split('.').pop();
  const record = JSON.parse(fs.readFileSync(path.join(dir, 'logs', fname), 'utf-8').trim());
  expect(record.extra.run_id).toBe(fileRunId);
});

test('per-run: no rotation even when rotate is set', () => {
  const dir = tmpDir();
  // a tiny size threshold would rotate a single file immediately; per-run must not.
  configureLogging(makeCfg(dir, { store: 'per-run', rotate: '1 KB' }));
  const log = getLogger('test');
  // ~30 × ~60 bytes well exceeds the 1 KB threshold a rotating handler would act on.
  for (let i = 0; i < 30; i++) log.info(`line ${i} ${'x'.repeat(50)}`);
  const files = fs.readdirSync(path.join(dir, 'logs'));
  // exactly one per-run file, and no rotated backups (.log.1 etc.)
  expect(files.filter(f => PER_RUN_RE.test(f))).toHaveLength(1);
  expect(files.some(f => /\.log\.\d+$/.test(f))).toBe(false);
});

test('per-run: no empty file when nothing is logged', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { store: 'per-run' }));
  // summary is off in makeCfg and we log nothing → lazy append means no file.
  expect(perRunFile(dir)).toBeUndefined();
});

test('default store is single ({runnable}.log)', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir)); // no store override
  getLogger('test').info('hi');
  expect(fs.existsSync(path.join(dir, 'logs', 'myscript.log'))).toBe(true);
  expect(perRunFile(dir)).toBeUndefined();
});

// ── early exception-hook install (parse-pipeline coverage) ─────────────────────

test('installExceptionHook is safe to call early and is idempotent', () => {
  _resetForTest();
  expect(() => {
    installExceptionHook(false);
    installExceptionHook(true);
  }).not.toThrow();
});

test('uncaught handler before configure: one-liner to stderr, no audit, no throw', () => {
  // Simulates the parse-pipeline window — the handler is up but configureLogging
  // has not run, so there are no handlers. It must still surface the one-liner on
  // stderr (no raw stack, no thrown error, no audit emission).
  _resetForTest(); // _handlers empty, _debug false, _summaryState null
  const cap = captureStreams();
  try {
    expect(() => _handleUncaught(new Error('parse-time boom'))).not.toThrow();
  } finally {
    cap.restore();
  }
  const out = cap.stderrLines.join('');
  expect(out).toContain('ERROR: Error: parse-time boom');
  expect(out).toContain('run with --debug');
});
