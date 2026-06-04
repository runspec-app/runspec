import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { configureLogging, emitRunSummary, getLogger, _resetForTest } from '../src/logging_setup';

// NOTE: these tests write via `process.stdout.write` directly rather than
// `console.log`, because jest hijacks `console.*` to add source annotations —
// which is what the tee would (correctly) capture under jest. In production
// `console.log` writes the plain line straight to process.stdout, which is what
// we exercise here.

function tmpDir(): string {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-print-test-'));
  fs.writeFileSync(path.join(d, 'package.json'), '{"name":"test","version":"0.0.0"}');
  return d;
}

function cfg(dir: string): Parameters<typeof configureLogging>[0] {
  return {
    logCfg: { rotate: 'midnight', keep: 7, summary: true, store: 'per-run' },
    runnableName: 'myscript',
    configPath: path.join(dir, 'runspec.toml'),
  };
}

function readPerRun(dir: string): Array<Record<string, any>> {
  const file = fs.readdirSync(path.join(dir, 'logs')).find((f) => f.startsWith('myscript.'))!;
  return fs
    .readFileSync(path.join(dir, 'logs', file), 'utf-8')
    .trim()
    .split('\n')
    .map((l) => JSON.parse(l));
}

beforeEach(() => _resetForTest());
afterAll(() => _resetForTest());

test('printed output is captured into the audit file as a runspec.print record', () => {
  const dir = tmpDir();
  configureLogging(cfg(dir));
  process.stdout.write('hello from the runnable\n');
  emitRunSummary();

  const printRec = readPerRun(dir).find((r) => r.logger === 'runspec.print');
  expect(printRec).toBeTruthy();
  expect(printRec!.message).toBe('hello from the runnable');
  expect(printRec!.extra.run_id).toBeTruthy(); // tagged like every file record
});

test('captured output reaches the real stdout exactly once (no double-print)', () => {
  const dir = tmpDir();
  // Spy BEFORE configureLogging so the tee writes through this spy as the raw stream.
  const spy = jest.spyOn(process.stdout, 'write').mockImplementation(() => true);
  configureLogging(cfg(dir));
  process.stdout.write('once please\n');
  const rawCalls = spy.mock.calls.filter((c) => String(c[0]).includes('once please')).length;
  spy.mockRestore();
  expect(rawCalls).toBe(1);
});

test('captured lines are not counted as log events in the run summary', () => {
  const dir = tmpDir();
  configureLogging(cfg(dir));
  process.stdout.write('line one\n');
  process.stdout.write('line two\n');
  getLogger('app').info('a real event'); // this one SHOULD count
  emitRunSummary();

  const summary = readPerRun(dir).find((r) => r.extra?.event === 'run_summary')!;
  expect(summary.extra.events.INFO).toBe(1); // the two printed lines didn't inflate the count
});

test('a partial (newline-less) trailing write is flushed on the next newline', () => {
  const dir = tmpDir();
  configureLogging(cfg(dir));
  process.stdout.write('partial '); // buffered, not yet a complete line
  process.stdout.write('then rest\n'); // completes the line
  emitRunSummary();

  const printRec = readPerRun(dir).find((r) => r.logger === 'runspec.print');
  expect(printRec!.message).toBe('partial then rest');
});
