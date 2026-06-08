import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { cmdTest, testOne } from '../src/cli';

const GOOD_TOML = `
[config]
autonomy-default = "autonomous"

[greet]
description = "Greet"
[greet.args]
name = {type = "str", required = false, default = "world"}

[farewell]
description = "Bye"
[farewell.args]
name = {type = "str", required = false, default = "world"}
`;

const cleanups: string[] = [];
afterEach(() => {
  while (cleanups.length) fs.rmSync(cleanups.pop()!, { recursive: true, force: true });
  jest.restoreAllMocks();
});

/** Build a venv-shaped folder: runspec.toml + node_modules/.bin/<name> shims
 * exiting with the given code (a shim of `null` means: don't create it). */
function folder(toml: string, shims: Record<string, number | null>): string {
  const dir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'runspec-test-'));
  cleanups.push(dir);
  fs.writeFileSync(path.join(dir, 'runspec.toml'), toml);
  const binDir = path.join(dir, 'node_modules', '.bin');
  fs.mkdirSync(binDir, { recursive: true });
  for (const [name, code] of Object.entries(shims)) {
    if (code === null) continue;
    const p = path.join(binDir, name);
    fs.writeFileSync(p, `#!/bin/sh\nexit ${code}\n`);
    fs.chmodSync(p, 0o755);
  }
  return dir;
}

function item(dir: string, runnable: string) {
  return { source: path.join(dir, 'runspec.toml'), runnable, spec: {} as any };
}

// ── testOne (the per-runnable unit) ─────────────────────────────────────────

test('passes when spec smoke ok and entry point exits 0', () => {
  const dir = folder(GOOD_TOML, { greet: 0 });
  const r = testOne(item(dir, 'greet'));
  expect(r.ok).toBe(true);
  expect(r.checks.spec_smoke.ok).toBe(true);
  expect(r.checks.entry_point.ok).toBe(true);
});

test('fails when the entry-point binary exits non-zero', () => {
  const dir = folder(GOOD_TOML, { greet: 1 });
  const r = testOne(item(dir, 'greet'));
  expect(r.ok).toBe(false);
  expect(r.checks.spec_smoke.ok).toBe(true); // spec is fine
  expect(r.checks.entry_point.ok).toBe(false);
  expect(r.checks.entry_point.error).toContain('exit 1');
});

test('fails when the entry point is missing', () => {
  const dir = folder(GOOD_TOML, { greet: null });
  const r = testOne(item(dir, 'greet'));
  expect(r.ok).toBe(false);
  expect(r.checks.entry_point.error).toContain('entry point not found');
});

test('fails spec smoke when the runnable is not in the TOML', () => {
  const dir = folder(GOOD_TOML, { ghost: 0 });
  const r = testOne(item(dir, 'ghost'));
  expect(r.ok).toBe(false);
  expect(r.checks.spec_smoke.ok).toBe(false);
  expect(r.checks.spec_smoke.error).toContain('ghost');
});

// ── cmdTest (orchestration: exit code, filter, json) ────────────────────────

function runCmd(dir: string, args: string[]): { exitCode: number | undefined; out: string } {
  const cwd = process.cwd();
  let exitCode: number | undefined;
  const exitSpy = jest.spyOn(process, 'exit').mockImplementation(((c?: number) => {
    exitCode = c;
    return undefined as never;
  }) as never);
  let out = '';
  const logSpy = jest.spyOn(console, 'log').mockImplementation((...a: unknown[]) => {
    out += a.map(String).join(' ') + '\n';
  });
  try {
    process.chdir(dir);
    cmdTest(args);
  } finally {
    process.chdir(cwd);
    logSpy.mockRestore();
    exitSpy.mockRestore();
  }
  return { exitCode, out };
}

test('all pass → no exit-1, summary reports passed', () => {
  const dir = folder(GOOD_TOML, { greet: 0, farewell: 0 });
  const { exitCode, out } = runCmd(dir, []);
  expect(exitCode).toBeUndefined();
  expect(out).toContain('2 passed, 0 failed');
});

test('one broken runnable → exit 1', () => {
  const dir = folder(GOOD_TOML, { greet: 1, farewell: 0 });
  const { exitCode, out } = runCmd(dir, []);
  expect(exitCode).toBe(1);
  expect(out).toContain('1 passed, 1 failed');
});

test('--runnable filters to a single runnable', () => {
  const dir = folder(GOOD_TOML, { greet: 0, farewell: 0 });
  const { out } = runCmd(dir, ['--runnable', 'greet']);
  expect(out).toContain('1 passed');
  expect(out).not.toContain('farewell');
});

test('--format json emits results + summary', () => {
  const dir = folder(GOOD_TOML, { greet: 0, farewell: 0 });
  const { out } = runCmd(dir, ['--format', 'json']);
  const payload = JSON.parse(out);
  expect(payload.summary).toEqual({ total: 2, passed: 2, failed: 0 });
  expect(payload.results).toHaveLength(2);
  expect(Object.keys(payload.results[0].checks)).toEqual(['spec_smoke', 'entry_point']);
});
