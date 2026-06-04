import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { findConfig, searchStarts, walkUp } from '../src/finder';

function tmpdir(): string {
  return fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'runspec-finder-'));
}

function writeToml(dir: string): string {
  fs.mkdirSync(dir, { recursive: true });
  const p = path.join(dir, 'runspec.toml');
  fs.writeFileSync(p, '[greet]\ndescription = "x"\n');
  return p;
}

describe('searchStarts — resolution priority', () => {
  it('orders explicit start, then entry dir, then cwd', () => {
    expect(searchStarts('/a', '/b', '/c')).toEqual([path.resolve('/a'), path.resolve('/b'), path.resolve('/c')]);
  });

  it('omits a missing explicit start / entry dir', () => {
    expect(searchStarts(undefined, undefined, '/c')).toEqual([path.resolve('/c')]);
    expect(searchStarts(undefined, '/b', '/c')).toEqual([path.resolve('/b'), path.resolve('/c')]);
  });

  it('de-duplicates while preserving order (cwd === entry dir)', () => {
    expect(searchStarts(undefined, '/same', '/same')).toEqual([path.resolve('/same')]);
  });
});

describe('walkUp', () => {
  let dir: string;
  afterEach(() => {
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it('finds runspec.toml walking up from a nested dir', () => {
    dir = tmpdir();
    const toml = writeToml(dir);
    const nested = path.join(dir, 'a', 'b', 'c');
    fs.mkdirSync(nested, { recursive: true });
    expect(walkUp(nested)).toBe(toml);
  });

  it('ignores a runspec.toml that lives under node_modules', () => {
    dir = tmpdir();
    const projectToml = writeToml(dir); // {root}/runspec.toml — the project's
    // A dependency ships its own runspec.toml (e.g. runspec-node's CLI spec).
    const depDir = path.join(dir, 'node_modules', 'runspec-node', 'dist');
    writeToml(depDir);
    // Walking up from inside node_modules must skip the dep's toml and resolve
    // the project's — this is the installed-CLI discovery case.
    expect(walkUp(depDir)).toBe(projectToml);
  });

  it('returns null when no runspec.toml exists up the tree', () => {
    dir = tmpdir();
    const nested = path.join(dir, 'x', 'y');
    fs.mkdirSync(nested, { recursive: true });
    expect(walkUp(nested)).toBeNull();
  });
});

describe('findConfig', () => {
  let dir: string;
  const savedEnv = process.env.RUNSPEC_CONFIG;
  afterEach(() => {
    if (savedEnv === undefined) delete process.env.RUNSPEC_CONFIG;
    else process.env.RUNSPEC_CONFIG = savedEnv;
    if (dir) fs.rmSync(dir, { recursive: true, force: true });
  });

  it('honours RUNSPEC_CONFIG when set', () => {
    dir = tmpdir();
    const toml = writeToml(path.join(dir, 'custom'));
    process.env.RUNSPEC_CONFIG = toml;
    // cwd has no toml, but the env var wins regardless.
    expect(findConfig(dir).configPath).toBe(path.resolve(toml));
  });

  it('resolves from an explicit start dir', () => {
    delete process.env.RUNSPEC_CONFIG;
    dir = tmpdir();
    const toml = writeToml(dir);
    expect(findConfig(path.join(dir, 'sub', 'dir')).configPath).toBe(toml);
  });

  it('throws a helpful error when nothing is found', () => {
    delete process.env.RUNSPEC_CONFIG;
    dir = tmpdir(); // empty, no runspec.toml anywhere up the chain inside the tmp root
    expect(() => findConfig(dir)).toThrow(/No runspec configuration found/);
  });
});
