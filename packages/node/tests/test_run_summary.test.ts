import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
  configureLogging,
  getLogger,
  emitRunSummary,
  _resetForTest,
  RUN_SUMMARY_LOGGER,
} from '../src/logging_setup';

function tmpDir(): string {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-summary-test-'));
  fs.writeFileSync(path.join(d, 'package.json'), '{"name":"test","version":"0.0.0"}');
  return d;
}

function makeCfg(dir: string, overrides: Record<string, unknown> = {}): Parameters<typeof configureLogging>[0] {
  const { debug, noSummary, autonomy, agent, commandPath, ...logOverrides } = overrides as {
    debug?: boolean; noSummary?: boolean; autonomy?: string; agent?: boolean; commandPath?: string[];
  } & Record<string, unknown>;
  return {
    logCfg: { rotate: 'midnight', keep: 7, summary: true, ...logOverrides },
    runnableName: 'myscript',
    configPath: path.join(dir, 'runspec.toml'),
    debug,
    noSummary,
    autonomy,
    agent,
    commandPath,
  };
}

function captureStderr() {
  const lines: string[] = [];
  const spy = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => {
    lines.push(String(chunk));
    return true;
  });
  return { lines, restore() { spy.mockRestore(); } };
}

beforeEach(() => {
  _resetForTest();
  delete process.env['RUNSPEC_MYSCRIPT_ARG_NO_SUMMARY'];
});

afterAll(() => {
  // Final cleanup so jest's own process-exit doesn't trigger a summary
  // emit against a stale state pointing at a tmp dir.
  _resetForTest();
});

// ── counter ──────────────────────────────────────────────────────────────────

test('counter increments per level', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  const log = getLogger('test.counter');
  log.info('one');
  log.info('two');
  log.warning('careful');
  log.error('broke');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const summary = content.trim().split('\n').map(l => JSON.parse(l)).find(o => o.logger === RUN_SUMMARY_LOGGER);
  expect(summary.extra.events).toEqual({ DEBUG: 0, INFO: 2, WARNING: 1, ERROR: 1, CRITICAL: 0 });
});

test('counter ignores the summary logger itself', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  // Calling the summary logger directly must not inflate counts.
  getLogger(RUN_SUMMARY_LOGGER).info('should not count');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const lines = content.trim().split('\n').map(l => JSON.parse(l));
  const summary = lines.find(o => o.logger === RUN_SUMMARY_LOGGER && o.extra?.event === 'run_summary');
  expect(summary.extra.events).toEqual({ DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 });
});

// ── emit ─────────────────────────────────────────────────────────────────────

test('summary writes one record to the audit file', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { autonomy: 'confirm', agent: false }));
  getLogger('test.emit').info('did work');
  getLogger('test.emit').warning('a warning');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const summaries = content.trim().split('\n').map(l => JSON.parse(l)).filter(o => o.logger === RUN_SUMMARY_LOGGER);
  expect(summaries).toHaveLength(1);
  const s = summaries[0];
  expect(s.message).toBe('run completed');
  expect(s.extra.event).toBe('run_summary');
  expect(s.extra.runnable).toBe('myscript');
  expect(s.extra.events.INFO).toBe(1);
  expect(s.extra.events.WARNING).toBe(1);
  expect(s.extra.exit_code).toBe(0);
  expect(s.extra.exception).toBeNull();
  expect(typeof s.extra.duration_ms).toBe('number');
});

test('summary writes a one-line summary to stderr', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('test.stderr').info('ran');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  const joined = cap.lines.join('');
  expect(joined).toContain('runspec: myscript completed');
  expect(joined).toContain('events');
});

test('summary is not echoed to stdout or stderr via console handlers', () => {
  const dir = tmpDir();
  const stdoutLines: string[] = [];
  const stderrLines: string[] = [];
  const o = jest.spyOn(process.stdout, 'write').mockImplementation((chunk) => { stdoutLines.push(String(chunk)); return true; });
  const e = jest.spyOn(process.stderr, 'write').mockImplementation((chunk) => { stderrLines.push(String(chunk)); return true; });
  configureLogging(makeCfg(dir));
  getLogger('test.console').info('hi');
  emitRunSummary();
  o.mockRestore();
  e.mockRestore();
  // The JSON form of the summary record must not appear on either stream.
  expect(stdoutLines.join('')).not.toContain('"logger":"runspec.runsummary"');
  expect(stderrLines.join('').match(/"logger":"runspec.runsummary"/)).toBeNull();
});

test('summary emit is idempotent', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  emitRunSummary();
  cap.restore();
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const summaries = content.trim().split('\n').map(l => JSON.parse(l)).filter(o => o.logger === RUN_SUMMARY_LOGGER);
  expect(summaries).toHaveLength(1);
});

// ── disable switches ─────────────────────────────────────────────────────────

test('noSummary option disables summary', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { noSummary: true }));
  const cap = captureStderr();
  emitRunSummary(); // no-op because state is null
  cap.restore();
  expect(cap.lines.join('')).not.toContain('runspec: ');
  expect(fs.existsSync(path.join(dir, 'logs', 'myscript.log'))).toBe(false);
});

test('summary=false in config disables summary', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir, { summary: false }));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  expect(cap.lines.join('')).not.toContain('runspec: ');
});

test('RUNSPEC_MYSCRIPT_ARG_NO_SUMMARY=1 disables summary', () => {
  process.env['RUNSPEC_MYSCRIPT_ARG_NO_SUMMARY'] = '1';
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  expect(cap.lines.join('')).not.toContain('runspec: ');
});

// ── stderr line shape ────────────────────────────────────────────────────────

test('success line uses singular "warning" for 1 and plural "errors" for 0', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('t').warning('one');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  const joined = cap.lines.join('');
  expect(joined).toContain('1 warning,');
  expect(joined).toContain('0 errors)');
});

test('success line uses plural for 2 warnings', () => {
  const dir = tmpDir();
  configureLogging(makeCfg(dir));
  getLogger('t').warning('a');
  getLogger('t').warning('b');
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  expect(cap.lines.join('')).toContain('2 warnings,');
});

// ── invoker capture ──────────────────────────────────────────────────────────

test('user appended to stderr line (no sudo)', () => {
  const dir = tmpDir();
  const origSudo = process.env['SUDO_USER'];
  delete process.env['SUDO_USER'];
  process.env['USER'] = 'alice';
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  if (origSudo !== undefined) process.env['SUDO_USER'] = origSudo;
  expect(cap.lines.join('')).toContain('| user: alice');
});

test('sudo user shown with arrow and target', () => {
  const dir = tmpDir();
  process.env['SUDO_USER'] = 'alice';
  process.env['USER'] = 'root';
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  delete process.env['SUDO_USER'];
  expect(cap.lines.join('')).toContain('| user: alice → root (sudo)');
});

test('user and user_target written to audit record', () => {
  const dir = tmpDir();
  const origSudo = process.env['SUDO_USER'];
  delete process.env['SUDO_USER'];
  process.env['USER'] = 'alice';
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  if (origSudo !== undefined) process.env['SUDO_USER'] = origSudo;
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const summary = content.trim().split('\n').map(l => JSON.parse(l)).find(o => o.logger === RUN_SUMMARY_LOGGER);
  expect(summary.extra.user).toBe('alice');
  expect(summary.extra.user_target).toBeNull();
});

test('sudo user_target written to audit record', () => {
  const dir = tmpDir();
  process.env['SUDO_USER'] = 'alice';
  process.env['USER'] = 'root';
  configureLogging(makeCfg(dir));
  const cap = captureStderr();
  emitRunSummary();
  cap.restore();
  delete process.env['SUDO_USER'];
  const content = fs.readFileSync(path.join(dir, 'logs', 'myscript.log'), 'utf-8');
  const summary = content.trim().split('\n').map(l => JSON.parse(l)).find(o => o.logger === RUN_SUMMARY_LOGGER);
  expect(summary.extra.user).toBe('alice');
  expect(summary.extra.user_target).toBe('root');
});
