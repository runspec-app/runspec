import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { loadRaw } from '../src/loader';

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-test-'));
}

afterEach(() => {
  // tmp dirs cleaned up by OS
});

// ── runspec.toml format ───────────────────────────────────────────────────────

test('loads simple runspec.toml', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[greet]
description = "Greet someone"
autonomy = "autonomous"

[greet.args]
name = {type = "str"}
loud = {default = false}
`);
  const raw = loadRaw(file);
  expect(raw.runnables['greet']).toBeDefined();
  expect(raw.runnables['greet'].description).toBe('Greet someone');
  expect(raw.runnables['greet'].args['name'].type).toBe('str');
  expect(raw.runnables['greet'].args['loud'].default).toBe(false);
});

test('normalises hyphenated field names', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[deploy]
autonomy-reason = "Irreversible"
`);
  const raw = loadRaw(file);
  expect(raw.runnables['deploy'].autonomyReason).toBe('Irreversible');
});

test('parses config section', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[config]
autonomy-default = "autonomous"
version = "1"

[greet]
description = "hi"
`);
  const raw = loadRaw(file);
  expect(raw.config.autonomyDefault).toBe('autonomous');
  expect(raw.config.version).toBe('1');
});

test('config excluded from runnables', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[config]
autonomy-default = "confirm"

[greet]
description = "hi"
`);
  const raw = loadRaw(file);
  expect('config' in raw.runnables).toBe(false);
  expect('greet' in raw.runnables).toBe(true);
});

// ── arg normalisation ─────────────────────────────────────────────────────────

test('normalises bare value shorthand', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[greet]
description = "hi"

[greet.args]
loud = false
times = 1
`);
  const raw = loadRaw(file);
  expect(raw.runnables['greet'].args['loud'].default).toBe(false);
  expect(raw.runnables['greet'].args['times'].default).toBe(1);
});

test('normalises range field', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[greet]
description = "hi"

[greet.args]
workers = {default = 4, range = [1, 32]}
`);
  const raw = loadRaw(file);
  expect(raw.runnables['greet'].args['workers'].range).toEqual([1, 32]);
});

test('normalises group fields', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[pipeline]
description = "hi"

[pipeline.groups.formats]
exclusive = true
args = ["json", "csv"]
`);
  const raw = loadRaw(file);
  const group = raw.runnables['pipeline'].groups['formats'];
  expect(group.exclusive).toBe(true);
  expect(group.args).toEqual(['json', 'csv']);
});

// ── defaults ──────────────────────────────────────────────────────────────────

test('autonomy-default falls back to confirm', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `[greet]\ndescription = "hi"\n`);
  const raw = loadRaw(file);
  expect(raw.config.autonomyDefault).toBe('confirm');
});

// ── [config.logging] ──────────────────────────────────────────────────────────

test('normalises [config.logging] with defaults', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[config.logging]

[greet]
description = "hi"
`);
  const raw = loadRaw(file);
  expect(raw.config.logging).toEqual({ rotate: 'midnight', keep: 7 });
});

test('normalises [config.logging] all fields', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `
[config.logging]
rotate = "10 MB"
keep   = 3

[greet]
description = "hi"
`);
  const raw = loadRaw(file);
  expect(raw.config.logging).toEqual({ rotate: '10 MB', keep: 3 });
});

test('logging is undefined when section absent', () => {
  const dir = tmpDir();
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, `[greet]\ndescription = "hi"\n`);
  const raw = loadRaw(file);
  expect(raw.config.logging).toBeUndefined();
});
