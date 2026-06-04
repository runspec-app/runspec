import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { scaffoldBin, resolveScript, readBinMap, shimContent, cmdShimContent } from '../src/cli';

function tmp(): string {
  return fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'runspec-bin-'));
}

function project(files: Record<string, string>): string {
  const dir = tmp();
  for (const [rel, content] of Object.entries(files)) {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, content);
  }
  return dir;
}

const cleanups: string[] = [];
afterEach(() => {
  while (cleanups.length) fs.rmSync(cleanups.pop()!, { recursive: true, force: true });
});
function track(dir: string): string {
  cleanups.push(dir);
  return dir;
}

describe('shimContent', () => {
  it('is an executable POSIX shim that resolves its own root and execs node', () => {
    const s = shimContent('greet.js');
    expect(s.startsWith('#!/bin/sh')).toBe(true);
    expect(s).toContain('dirname');
    expect(s).toContain('exec node "$DIR/greet.js" "$@"');
  });
});

describe('cmdShimContent', () => {
  it('is a Windows batch shim that resolves its own root and runs node', () => {
    const s = cmdShimContent('cmd/greet.js');
    expect(s.startsWith('@echo off')).toBe(true);
    expect(s).toContain('node "%~dp0..\\cmd\\greet.js" %*'); // forward slashes → backslashes
    expect(s).toContain('\r\n'); // CRLF line endings
  });
});

describe('readBinMap', () => {
  it('reads an object bin map', () => {
    const dir = track(project({ 'package.json': JSON.stringify({ name: 'x', bin: { greet: './g.js' } }) }));
    expect(readBinMap(dir)).toEqual({ greet: './g.js' });
  });

  it('maps the package name for a string bin', () => {
    const dir = track(project({ 'package.json': JSON.stringify({ name: 'mytool', bin: './m.js' }) }));
    expect(readBinMap(dir)).toEqual({ mytool: './m.js' });
  });

  it('returns {} when no package.json', () => {
    const dir = track(tmp());
    expect(readBinMap(dir)).toEqual({});
  });
});

describe('resolveScript', () => {
  it('prefers package.json bin when the target exists', () => {
    const dir = track(project({ 'src/greet.js': '' }));
    expect(resolveScript(dir, 'greet', { greet: './src/greet.js' })).toBe(path.join(dir, 'src/greet.js'));
  });

  it('returns null when a bin entry points at a missing file', () => {
    const dir = track(tmp());
    expect(resolveScript(dir, 'greet', { greet: './nope.js' })).toBeNull();
  });

  it('falls back to ./<name>.{js,cjs,mjs} by convention', () => {
    const dir = track(project({ 'greet.cjs': '' }));
    expect(resolveScript(dir, 'greet', {})).toBe(path.join(dir, 'greet.cjs'));
  });

  it('returns null when no script can be found', () => {
    const dir = track(tmp());
    expect(resolveScript(dir, 'greet', {})).toBeNull();
  });
});

describe('scaffoldBin', () => {
  const TOML = '[greet]\ndescription = "g"\n[greet.args]\nname = {type="str", default="world"}\n[backup]\ndescription = "b"\n';

  it('writes bin/runspec + one bin/<runnable> per runnable, executable', () => {
    const dir = track(
      project({
        'runspec.toml': TOML,
        'greet.js': 'x',
        'backup.js': 'x',
        'node_modules/runspec-node/bin/runspec.js': '#!/usr/bin/env node\n',
      }),
    );
    const res = scaffoldBin(dir);
    expect(res.warnings).toEqual([]);
    expect(res.written.map((w) => w.name).sort()).toEqual(['backup', 'greet', 'runspec']);

    for (const name of ['runspec', 'greet', 'backup']) {
      const p = path.join(dir, 'bin', name);
      expect(fs.existsSync(p)).toBe(true);
      expect(fs.statSync(p).mode & 0o111).toBeTruthy(); // executable bit
    }
    // bin/runspec points at the installed CLI; bin/greet at the runnable script.
    expect(fs.readFileSync(path.join(dir, 'bin', 'runspec'), 'utf-8')).toContain('node_modules/runspec-node/bin/runspec.js');
    expect(fs.readFileSync(path.join(dir, 'bin', 'greet'), 'utf-8')).toContain('exec node "$DIR/greet.js"');
    // Windows .cmd shims sit alongside each POSIX shim.
    for (const name of ['runspec', 'greet', 'backup']) {
      expect(fs.existsSync(path.join(dir, 'bin', name + '.cmd'))).toBe(true);
    }
    expect(fs.readFileSync(path.join(dir, 'bin', 'greet.cmd'), 'utf-8')).toContain('node "%~dp0..\\greet.js" %*');
    // venv shape includes logs/
    expect(fs.existsSync(path.join(dir, 'logs'))).toBe(true);
  });

  it('warns (and skips) when a runnable has no script, and when runspec-node is absent', () => {
    const dir = track(project({ 'runspec.toml': TOML, 'greet.js': 'x' })); // no backup.js, no node_modules
    const res = scaffoldBin(dir);
    expect(res.written.map((w) => w.name)).toEqual(['greet']);
    expect(res.warnings.some((w) => w.includes('backup'))).toBe(true);
    expect(res.warnings.some((w) => w.includes('runspec-node is not installed'))).toBe(true);
  });

  it('honours a package.json bin path over the convention', () => {
    const dir = track(
      project({
        'runspec.toml': '[greet]\ndescription = "g"\n',
        'package.json': JSON.stringify({ name: 'x', bin: { greet: './cmd/greet.js' } }),
        'cmd/greet.js': 'x',
        'node_modules/runspec-node/bin/runspec.js': '#!/usr/bin/env node\n',
      }),
    );
    const res = scaffoldBin(dir);
    expect(res.warnings).toEqual([]);
    expect(fs.readFileSync(path.join(dir, 'bin', 'greet'), 'utf-8')).toContain('exec node "$DIR/cmd/greet.js"');
  });
});
