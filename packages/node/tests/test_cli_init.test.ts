import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execFileSync } from 'child_process';

const CLI = path.resolve(__dirname, '../bin/runspec.js');

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-init-test-'));
}

function runCLI(dir: string, cliArgs: string[]): { stdout: string; stderr: string } {
  try {
    const stdout = execFileSync('node', [CLI, ...cliArgs], { cwd: dir, encoding: 'utf-8' });
    return { stdout, stderr: '' };
  } catch (e: any) {
    return { stdout: e.stdout ?? '', stderr: e.stderr ?? '' };
  }
}

function runInit(dir: string, extraArgs: string[] = []): { stdout: string; stderr: string } {
  return runCLI(dir, ['init', ...extraArgs]);
}

// ── init (basic) ──────────────────────────────────────────────────────────────

test('creates runspec.toml and .ts stub by default', () => {
  const dir = tmpDir();
  const { stdout } = runInit(dir, ['--name', 'greet']);
  expect(fs.existsSync(path.join(dir, 'runspec.toml'))).toBe(true);
  expect(fs.existsSync(path.join(dir, 'greet.ts'))).toBe(true);
  expect(stdout).toContain('Created runspec.toml');
  expect(stdout).toContain('Created greet.ts');
});

test('runspec.toml contains the runnable name', () => {
  const dir = tmpDir();
  runInit(dir, ['--name', 'deploy']);
  const toml = fs.readFileSync(path.join(dir, 'runspec.toml'), 'utf-8');
  expect(toml).toContain('[deploy]');
});

test('typescript stub imports parse from runspec-node', () => {
  const dir = tmpDir();
  runInit(dir, ['--name', 'greet']);
  const ts = fs.readFileSync(path.join(dir, 'greet.ts'), 'utf-8');
  expect(ts).toContain("import { parse } from 'runspec-node'");
  expect(ts).toContain('function main()');
  expect(ts).toContain('main();');
});

test('--lang javascript generates .js stub', () => {
  const dir = tmpDir();
  runInit(dir, ['--name', 'greet', '--lang', 'javascript']);
  expect(fs.existsSync(path.join(dir, 'greet.js'))).toBe(true);
  const js = fs.readFileSync(path.join(dir, 'greet.js'), 'utf-8');
  expect(js).toContain("require('runspec-node')");
  expect(js).toContain('main();');
});

test('--lang python generates .py stub', () => {
  const dir = tmpDir();
  runInit(dir, ['--name', 'greet', '--lang', 'python']);
  expect(fs.existsSync(path.join(dir, 'greet.py'))).toBe(true);
  const py = fs.readFileSync(path.join(dir, 'greet.py'), 'utf-8');
  expect(py).toContain('from runspec import parse');
  expect(py).toContain('if __name__ == "__main__"');
});

test('does not overwrite existing stub', () => {
  const dir = tmpDir();
  const stubPath = path.join(dir, 'greet.ts');
  fs.writeFileSync(stubPath, '// existing\n', 'utf-8');
  const { stdout } = runInit(dir, ['--name', 'greet']);
  expect(fs.readFileSync(stubPath, 'utf-8')).toBe('// existing\n');
  expect(stdout).toContain('already exists — skipped');
});

test('fails if runspec.toml already exists', () => {
  const dir = tmpDir();
  fs.writeFileSync(path.join(dir, 'runspec.toml'), '[greet]\n', 'utf-8');
  const { stdout } = runInit(dir, ['--name', 'greet']);
  expect(stdout).toContain('already exists');
  expect(fs.existsSync(path.join(dir, 'greet.ts'))).toBe(false);
});

test('unknown --lang exits with error', () => {
  const dir = tmpDir();
  const { stdout } = runInit(dir, ['--name', 'greet', '--lang', 'ruby']);
  expect(stdout).toContain('Unknown --lang');
});

// ── init --example ────────────────────────────────────────────────────────────

test('--example creates runspec.toml with clean and scan', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  const toml = fs.readFileSync(path.join(dir, 'runspec.toml'), 'utf-8');
  expect(toml).toContain('[clean]');
  expect(toml).toContain('[scan]');
});

test('--example creates clean.ts and scan.ts by default', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  expect(fs.existsSync(path.join(dir, 'clean.ts'))).toBe(true);
  expect(fs.existsSync(path.join(dir, 'scan.ts'))).toBe(true);
});

test('--example clean stub uses __runspec_agent__ check', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  const ts = fs.readFileSync(path.join(dir, 'clean.ts'), 'utf-8');
  expect(ts).toContain('__runspec_agent__');
  expect(ts).toContain('delete');
});

test('--example scan stub always outputs JSON', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  const ts = fs.readFileSync(path.join(dir, 'scan.ts'), 'utf-8');
  expect(ts).toContain('JSON.stringify');
  expect(ts).not.toContain('format');
});

test('--example scan toml has no format or delete arg', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  const toml = fs.readFileSync(path.join(dir, 'runspec.toml'), 'utf-8');
  const scanArgsSection = toml.split('[scan.args]')[1] ?? '';
  expect(scanArgsSection).not.toContain('format');
  expect(scanArgsSection).not.toContain('delete');
});

test('--example scan toml declares output = json', () => {
  const dir = tmpDir();
  runInit(dir, ['--example']);
  const toml = fs.readFileSync(path.join(dir, 'runspec.toml'), 'utf-8');
  expect(toml).toContain('output      = "json"');
});

test('--example shows demo prep commands', () => {
  const dir = tmpDir();
  const { stdout } = runInit(dir, ['--example']);
  expect(stdout).toContain('touch -t 202401010000 report.tmp cache.tmp session.tmp');
  expect(stdout).toContain('scan');
  expect(stdout).toContain('clean');
});

test('--example with --name warns name is ignored', () => {
  const dir = tmpDir();
  const { stdout } = runInit(dir, ['--example', '--name', 'myapp']);
  expect(stdout).toContain('--name is ignored');
});

test('--example --lang javascript creates clean.js and scan.js', () => {
  const dir = tmpDir();
  runInit(dir, ['--example', '--lang', 'javascript']);
  expect(fs.existsSync(path.join(dir, 'clean.js'))).toBe(true);
  expect(fs.existsSync(path.join(dir, 'scan.js'))).toBe(true);
});

// ── local command ─────────────────────────────────────────────────────────────

test('local command is recognized', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['local']);
  expect(stdout).not.toContain('Unknown command');
});

test('discover command is no longer available', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['discover']);
  expect(stdout).toContain('Unknown command');
});

test('check command is no longer available', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['check']);
  expect(stdout).toContain('Unknown command');
});

test('emit command is no longer available', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['emit']);
  expect(stdout).toContain('Unknown command');
});

test('jump command requires a host', () => {
  const dir = tmpDir();
  const { stderr } = runCLI(dir, ['jump']);
  expect(stderr).toContain('A host is required');
});

// ── help ──────────────────────────────────────────────────────────────────────

test('top-level help mentions local and jump', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['--help']);
  expect(stdout).toContain('local');
  expect(stdout).toContain('jump');
  expect(stdout).not.toContain('discover');
});

test('local --help shows focused help', () => {
  const dir = tmpDir();
  const { stdout } = runCLI(dir, ['local', '--help']);
  expect(stdout).toContain('--format');
  expect(stdout).toContain('--script');
});
