import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  BASELINE_NAME,
  MANIFEST_NAME,
  buildManifest,
  checkProject,
  checkRoot,
  classifyResolved,
  directDependencies,
  hashFile,
  installedPackages,
  manifestIsPublished,
  reportFiles,
  reportRun,
  reportToken,
  snapshotFileList,
  warningText,
} from '../src/integrity';
import { parse } from '../src/parser';
import { configureLogging, emitRunSummary, _resetForTest, RUN_SUMMARY_LOGGER } from '../src/logging_setup';

// ── fixtures ─────────────────────────────────────────────────────────────────

function write(file: string, content: string): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

/** A deployed project folder (not a git checkout) with one dependency. */
function makeProject(opts: { depManifest?: boolean; projectManifest?: boolean; resolved?: string } = {}): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-integrity-'));
  write(path.join(root, 'package.json'), JSON.stringify({
    name: 'my-tools', version: '1.0.0', dependencies: { 'dep-a': '^1.0.0' },
  }));
  write(path.join(root, 'runspec.toml'), '[greet]\ndescription = "Greet"\n\n[greet.args]\nname = {type = "str", default = "world"}\n');
  write(path.join(root, 'greet.js'), 'console.log("hi");\n');
  write(path.join(root, 'lib', 'util.js'), 'module.exports = 1;\n');
  write(path.join(root, 'bin', 'greet'), '#!/bin/sh\n');
  write(path.join(root, 'logs', 'greet.log'), '{}\n');
  write(path.join(root, '.runspec_env'), 'X=1\n');
  write(path.join(root, 'package-lock.json'), '{}\n');
  const dep = path.join(root, 'node_modules', 'dep-a');
  write(path.join(dep, 'package.json'), JSON.stringify({ name: 'dep-a', version: '1.2.3' }));
  write(path.join(dep, 'index.js'), 'module.exports = "a";\n');
  write(path.join(dep, 'node_modules', 'nested', 'index.js'), 'nested\n');
  if (opts.depManifest) {
    fs.writeFileSync(path.join(dep, MANIFEST_NAME), JSON.stringify(buildManifest(dep, ['package.json', 'index.js'])));
  }
  if (opts.projectManifest) {
    fs.writeFileSync(path.join(root, MANIFEST_NAME), JSON.stringify(buildManifest(root, ['package.json', 'runspec.toml', 'greet.js'])));
  }
  if (opts.resolved !== undefined) {
    write(path.join(root, 'node_modules', '.package-lock.json'), JSON.stringify({
      packages: { 'node_modules/dep-a': { version: '1.2.3', resolved: opts.resolved } },
    }));
  }
  return root;
}

const cfg = (root: string) => path.join(root, 'runspec.toml');
const byName = (r: { packages: { name: string }[] }, n: string) => r.packages.find((p) => p.name === n) as any;

beforeEach(() => {
  process.env.RUNSPEC_INTEGRITY = 'on';
});
afterAll(() => {
  process.env.RUNSPEC_INTEGRITY = 'off';
});

// ── pure helpers ─────────────────────────────────────────────────────────────

test('classifyResolved maps npm resolved values to a source', () => {
  expect(classifyResolved('')).toBe('index');
  expect(classifyResolved('https://registry.npmjs.org/a/-/a-1.0.0.tgz')).toBe('index');
  expect(classifyResolved('file:../a-1.0.0.tgz')).toBe('archive');
  expect(classifyResolved('git+ssh://git@github.com/x/y.git#abc')).toBe('vcs');
  expect(classifyResolved('github:x/y')).toBe('vcs');
});

test('directDependencies reads dependencies + optionalDependencies, not devDependencies', () => {
  expect(directDependencies({
    dependencies: { a: '1', b: '1' }, optionalDependencies: { c: '1', a: '1' }, devDependencies: { d: '1' },
  })).toEqual(['a', 'b', 'c']);
});

test('manifestIsPublished follows the package.json files field', () => {
  expect(manifestIsPublished({})).toBe(true);
  expect(manifestIsPublished({ files: ['dist', 'runspec.toml'] })).toBe(false);
  expect(manifestIsPublished({ files: ['dist', 'runspec-files.json'] })).toBe(true);
});

test('snapshotFileList skips dot-entries, nested node_modules and the project excludes', () => {
  const root = makeProject();
  const files = snapshotFileList(root, new Set(['logs', 'bin', 'package-lock.json']));
  expect(files).toEqual(['greet.js', 'lib/util.js', 'package.json', 'runspec.toml']);
  const dep = snapshotFileList(path.join(root, 'node_modules', 'dep-a'));
  expect(dep).toEqual(['index.js', 'package.json']);
});

test('installedPackages lists scoped and plain packages', () => {
  const root = makeProject();
  write(path.join(root, 'node_modules', '@scope', 'pkg', 'package.json'), '{"name":"@scope/pkg"}');
  expect(installedPackages(root)).toEqual(['@scope/pkg', 'dep-a']);
});

// ── snapshot baseline ────────────────────────────────────────────────────────

test('first run takes a snapshot; an unchanged second run is ok', () => {
  const root = makeProject();
  const first = checkProject(cfg(root))!;
  expect(first.packages.map((p) => [p.name, p.baseline, p.status, p.snapshotTaken])).toEqual([
    ['my-tools', 'snapshot', 'ok', true],
    ['dep-a', 'snapshot', 'ok', true],
  ]);
  expect(fs.existsSync(path.join(root, 'logs', BASELINE_NAME))).toBe(true);
  const second = checkProject(cfg(root))!;
  expect(second.packages.every((p) => p.status === 'ok' && !p.snapshotTaken)).toBe(true);
  expect(reportToken(second)).toBeNull();
});

test('an edited or deleted file after the snapshot is modified', () => {
  const root = makeProject();
  checkProject(cfg(root));
  fs.appendFileSync(path.join(root, 'greet.js'), 'console.log(process.env);\n');
  fs.unlinkSync(path.join(root, 'node_modules', 'dep-a', 'index.js'));
  const r = checkProject(cfg(root))!;
  expect(byName(r, 'my-tools').modified).toEqual(['greet.js']);
  expect(byName(r, 'dep-a').missing).toEqual(['index.js']);
  expect(reportToken(r)).toBe('modified');
  expect(reportFiles(r)).toEqual(['my-tools: greet.js', 'dep-a: index.js']);
});

test('logs, bin shims, .runspec_env and package-lock.json can change freely', () => {
  const root = makeProject();
  checkProject(cfg(root));
  fs.appendFileSync(path.join(root, 'logs', 'greet.log'), '{}\n');
  fs.writeFileSync(path.join(root, 'bin', 'greet'), '#!/bin/sh\nexec node greet.js\n');
  fs.writeFileSync(path.join(root, '.runspec_env'), 'X=2\n');
  fs.writeFileSync(path.join(root, 'package-lock.json'), '{"x":1}\n');
  expect(reportToken(checkProject(cfg(root)))).toBeNull();
});

test('a new version of a package takes a new snapshot', () => {
  const root = makeProject();
  checkProject(cfg(root));
  const dep = path.join(root, 'node_modules', 'dep-a');
  fs.writeFileSync(path.join(dep, 'package.json'), JSON.stringify({ name: 'dep-a', version: '1.3.0' }));
  fs.writeFileSync(path.join(dep, 'index.js'), 'module.exports = "a2";\n');
  const r = checkProject(cfg(root))!;
  expect(byName(r, 'dep-a')).toMatchObject({ version: '1.3.0', status: 'ok', snapshotTaken: true });
});

test('a read-only logs dir leaves snapshot-only packages unverifiable', () => {
  if (process.getuid && process.getuid() === 0) return; // root ignores permissions
  const root = makeProject();
  fs.chmodSync(path.join(root, 'logs'), 0o555);
  try {
    const r = checkProject(cfg(root))!;
    expect(r.packages.every((p) => p.status === 'unverifiable')).toBe(true);
  } finally {
    fs.chmodSync(path.join(root, 'logs'), 0o755);
  }
});

// ── packed list ──────────────────────────────────────────────────────────────

test('a packed runspec-files.json is used instead of a snapshot', () => {
  const root = makeProject({ depManifest: true });
  const r = checkProject(cfg(root))!;
  expect(byName(r, 'dep-a')).toMatchObject({ baseline: 'list', status: 'ok', checked: 2 });
  fs.appendFileSync(path.join(root, 'node_modules', 'dep-a', 'index.js'), '// edit\n');
  expect(byName(checkProject(cfg(root))!, 'dep-a')).toMatchObject({ status: 'modified', modified: ['index.js'] });
});

test('an edit made before the first run is caught by a list but not by a snapshot', () => {
  const listed = makeProject({ projectManifest: true });
  fs.appendFileSync(path.join(listed, 'greet.js'), '// edit\n');
  expect(byName(checkProject(cfg(listed))!, 'my-tools').status).toBe('modified');

  const snap = makeProject();
  fs.appendFileSync(path.join(snap, 'greet.js'), '// edit\n');
  expect(byName(checkProject(cfg(snap))!, 'my-tools').status).toBe('ok');
});

test('buildManifest hashes the listed files and never lists itself', () => {
  const root = makeProject();
  const m = buildManifest(root, ['greet.js', MANIFEST_NAME, 'package.json']);
  expect(Object.keys(m.files)).toEqual(['greet.js', 'package.json']);
  expect(m.files['greet.js']).toBe(hashFile(path.join(root, 'greet.js')));
  expect(m).toMatchObject({ name: 'my-tools', packageVersion: '1.0.0', algorithm: 'sha256' });
});

// ── what is skipped ──────────────────────────────────────────────────────────

test('nothing is checked in a git work tree', () => {
  const root = makeProject();
  fs.mkdirSync(path.join(root, '.git'));
  expect(checkProject(cfg(root))).toBeNull();
  expect(fs.existsSync(path.join(root, 'logs', BASELINE_NAME))).toBe(false);
});

test('RUNSPEC_INTEGRITY=off checks nothing', () => {
  const root = makeProject();
  process.env.RUNSPEC_INTEGRITY = 'off';
  expect(checkProject(cfg(root))).toBeNull();
});

test('a symlinked dependency (npm link) is skipped', () => {
  const root = makeProject();
  const src = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-linked-'));
  write(path.join(src, 'package.json'), JSON.stringify({ name: 'dep-a', version: '9.9.9' }));
  fs.rmSync(path.join(root, 'node_modules', 'dep-a'), { recursive: true });
  fs.symlinkSync(src, path.join(root, 'node_modules', 'dep-a'), 'dir');
  expect(byName(checkProject(cfg(root))!, 'dep-a')).toMatchObject({ source: 'linked', status: 'skipped' });
});

test('a file: or git dependency makes the run local', () => {
  expect(reportToken(checkProject(cfg(makeProject({ resolved: 'file:../dep-a-1.2.3.tgz' }))))).toBe('local');
  expect(reportToken(checkProject(cfg(makeProject({ resolved: 'git+https://example.com/dep-a.git#abc' }))))).toBe('local');
  expect(reportToken(checkProject(cfg(makeProject({ resolved: 'https://registry.npmjs.org/dep-a/-/dep-a-1.2.3.tgz' }))))).toBeNull();
});

test('verify-style checks: names, checkout and no snapshot taking', () => {
  const root = makeProject();
  const r = checkRoot(root, { names: ['dep-a'], takeSnapshots: false });
  expect(r.packages.map((p) => [p.name, p.status])).toEqual([['dep-a', 'unverifiable']]);
  expect(fs.existsSync(path.join(root, 'logs', BASELINE_NAME))).toBe(false);
  const c = checkRoot(root, { checkout: true, takeSnapshots: true });
  expect(byName(c, 'my-tools').status).toBe('skipped');
  expect(byName(c, 'dep-a').status).toBe('ok');
});

// ── run integration ──────────────────────────────────────────────────────────

test('warning names the package and files', () => {
  const root = makeProject();
  checkProject(cfg(root));
  fs.appendFileSync(path.join(root, 'greet.js'), '// edit\n');
  const r = checkProject(cfg(root))!;
  expect(warningText('greet', r)).toContain('greet: 1 installed file differs from the package (my-tools 1.0.0): greet.js.');
  const lines: string[] = [];
  const spy = jest.spyOn(process.stderr, 'write').mockImplementation((c) => { lines.push(String(c)); return true; });
  reportRun('greet', r);
  spy.mockRestore();
  expect(lines.join('')).toContain('recorded as modified');
});

test('parse() warns and the run summary carries integrity', () => {
  const root = makeProject();
  fs.appendFileSync(path.join(root, 'runspec.toml'), '\n[config.logging]\n');
  checkProject(cfg(root));
  fs.appendFileSync(path.join(root, 'greet.js'), '// edit\n');
  _resetForTest();
  const lines: string[] = [];
  const spy = jest.spyOn(process.stderr, 'write').mockImplementation((c) => { lines.push(String(c)); return true; });
  try {
    parse({ scriptName: 'greet', argv: [], configPath: cfg(root), _checkRunAs: false });
    emitRunSummary();
  } finally {
    spy.mockRestore();
  }
  expect(lines.join('')).toContain('greet: 1 installed file differs');
  const log = fs.readFileSync(path.join(root, 'logs', 'greet.log'), 'utf8');
  const summary = log.trim().split('\n').map((l) => JSON.parse(l)).find((o) => o.logger === RUN_SUMMARY_LOGGER);
  expect(summary.extra.integrity).toBe('modified');
  expect(summary.extra.integrity_files).toEqual(['my-tools: greet.js']);
  _resetForTest();
});

test('a clean run summary has no integrity fields', () => {
  const root = makeProject();
  _resetForTest();
  configureLogging({ logCfg: { rotate: 'midnight', keep: 7, summary: true }, runnableName: 'greet', configPath: cfg(root), integrity: null, integrityFiles: [] });
  emitRunSummary();
  const log = fs.readFileSync(path.join(root, 'logs', 'greet.log'), 'utf8');
  const summary = log.trim().split('\n').map((l) => JSON.parse(l)).find((o) => o.logger === RUN_SUMMARY_LOGGER);
  expect(summary.extra).not.toHaveProperty('integrity');
  _resetForTest();
});

test('runnable.modified is audited once per distinct change', () => {
  const { readLedger, setRunContext } = require('../src/auditlog');
  process.env.RUNSPEC_AUDIT = 'on';
  const root = makeProject();
  setRunContext({ runnable: 'greet', commandPath: [], runId: '', agent: false, trigger: '', auditCfg: { syslog: false, facility: 'auth' }, root });
  const spy = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
  try {
    checkProject(cfg(root));
    fs.appendFileSync(path.join(root, 'greet.js'), '// edit 1\n');
    reportRun('greet', checkProject(cfg(root)));
    reportRun('greet', checkProject(cfg(root))); // same change: no new event
    fs.appendFileSync(path.join(root, 'greet.js'), '// edit 2\n');
    reportRun('greet', checkProject(cfg(root))); // a further edit: a new event
  } finally {
    spy.mockRestore();
    setRunContext(null);
    process.env.RUNSPEC_AUDIT = 'off';
  }
  const events = readLedger(path.join(root, 'logs', 'audit.jsonl'));
  expect(events.map((e: any) => [e.event, e.outcome, e.target.package, e.detail.files])).toEqual([
    ['runnable.modified', 'detected', 'my-tools', ['greet.js']],
    ['runnable.modified', 'detected', 'my-tools', ['greet.js']],
  ]);
});
