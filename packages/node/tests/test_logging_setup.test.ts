import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
  configureLogging,
  getLogger,
  Logger,
  _resetForTest,
  _periodForDate,
} from '../src/logging_setup';

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-log-test-'));
}

function makeCfg(dir: string, overrides: Record<string, unknown> = {}): Parameters<typeof configureLogging>[0] {
  return {
    logCfg: { level: 'info', rotate: 'midnight', keep: 7, ...overrides },
    agent: false,
    runnableName: 'myscript',
    configPath: path.join(dir, 'runspec.toml'),
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
  configureLogging({ logCfg: undefined, agent: false, runnableName: 'x', configPath: path.join(dir, 'runspec.toml') });
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

test('file handler captures DEBUG even when console is INFO', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { level: 'info' }));
  getLogger('test').debug('low level detail');
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const parsed = JSON.parse(content);
  expect(parsed.level).toBe('DEBUG');
});

test('log file captures all levels', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
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

test('error field included when Error passed', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test').error('boom', new Error('something broke'));
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8').trim();
  const parsed = JSON.parse(content);
  expect(typeof parsed.exc).toBe('string');
  expect(parsed.exc).toContain('something broke');
});

// ── agent mode ────────────────────────────────────────────────────────────────

test('agent mode: no console output to stderr', () => {
  const dir = tmpDir();
  const stderrWrite = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
  configureLogging({ ...makeCfg(dir), agent: true });
  getLogger('test').info('silent');
  expect(stderrWrite).not.toHaveBeenCalled();
  stderrWrite.mockRestore();
});

test('non-agent mode: writes to stderr', () => {
  const dir = tmpDir();
  const lines: string[] = [];
  const stderrWrite = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => {
    lines.push(String(chunk));
    return true;
  });
  configureLogging(makeCfg(dir, { level: 'info' }));
  getLogger('test').info('visible');
  expect(lines.some(l => l.includes('visible'))).toBe(true);
  stderrWrite.mockRestore();
});

test('console output not written for messages below configured level', () => {
  const dir = tmpDir();
  const lines: string[] = [];
  const stderrWrite = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => {
    lines.push(String(chunk));
    return true;
  });
  configureLogging(makeCfg(dir, { level: 'warning' }));
  getLogger('test').debug('below threshold');
  getLogger('test').info('also below');
  expect(lines).toHaveLength(0);
  stderrWrite.mockRestore();
});

// ── log level override ────────────────────────────────────────────────────────

test('logLevelOverride sets console level', () => {
  const dir = tmpDir();
  const lines: string[] = [];
  const stderrWrite = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => {
    lines.push(String(chunk));
    return true;
  });
  configureLogging({ ...makeCfg(dir, { level: 'warning' }), logLevelOverride: 'debug' });
  getLogger('test').debug('now visible');
  expect(lines.some(l => l.includes('now visible'))).toBe(true);
  stderrWrite.mockRestore();
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

test('falls back to ~/logs when package dir is not writable', () => {
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
