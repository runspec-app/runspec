import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse as parseTOML } from 'smol-toml';
import { hasInclude, resolve } from '../src/compose';
import { loadRaw, loadText, normalise, readResolved } from '../src/loader';

const REPO = path.resolve(__dirname, '../../..');
const FIXTURES = path.join(REPO, 'tests/integration/fixtures');
const COMPOSED = path.join(FIXTURES, 'composed');

function write(files: Record<string, string>): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-compose-'));
  for (const [rel, text] of Object.entries(files)) {
    const p = path.join(root, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, text);
  }
  return path.join(root, 'runspec.toml');
}

function repoSpecs(): string[] {
  const out: string[] = [];
  const skip = new Set(['node_modules', '.venv', 'dist', 'build', '.git']);
  const walk = (dir: string) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (skip.has(e.name)) continue;
      const p = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (p !== COMPOSED) walk(p);
      } else if (e.name === 'runspec.toml') out.push(p);
    }
  };
  walk(path.join(REPO, 'packages'));
  walk(path.join(REPO, 'tests'));
  return out.sort();
}

describe('existing specs are untouched', () => {
  const specs = repoSpecs();

  test('found the repo specs', () => {
    expect(specs.length).toBeGreaterThan(20);
  });

  test.each(specs.map((p) => [path.relative(REPO, p), p]))('%s resolves to itself', (_name, p) => {
    const data = parseTOML(fs.readFileSync(p, 'utf-8')) as Record<string, unknown>;
    expect(resolve(data)).toEqual(data);
  });
});

describe('compliance fixture', () => {
  test('composed equals flat', () => {
    expect(loadRaw(path.join(COMPOSED, 'runspec.toml'))).toEqual(loadRaw(path.join(FIXTURES, 'composed_flat.toml')));
  });
});

describe('include', () => {
  test('relative to the including file', () => {
    const cfg = write({
      'runspec.toml': '[r]\n[r.commands.a]\ninclude = "a/a.toml"\n',
      'a/a.toml': '[commands.b]\ninclude = "../b/b.toml"\n',
      'b/b.toml': 'description = "from b"\n',
    });
    expect(loadRaw(cfg).runnables['r'].commands['a'].commands['b'].description).toBe('from b');
  });

  test('meta survives for emit', () => {
    const cfg = write({ 'runspec.toml': '[r]\ninclude = "r.toml"\n', 'r.toml': '[meta.rundeck]\nschedule = "0 2 * * *"\n' });
    const raw = readResolved(cfg) as { r: { meta: { rundeck: { schedule: string } } } };
    expect(raw.r.meta.rundeck.schedule).toBe('0 2 * * *');
  });

  test('cycle', () => {
    const cfg = write({
      'runspec.toml': '[r]\n[r.commands.a]\ninclude = "a.toml"\n',
      'a.toml': '[commands.b]\ninclude = "b.toml"\n',
      'b.toml': '[commands.c]\ninclude = "a.toml"\n',
    });
    expect(() => loadRaw(cfg)).toThrow('cycle: a.toml → b.toml → a.toml');
  });

  test('depth cap', () => {
    const files: Record<string, string> = { 'runspec.toml': '[r]\ninclude = "f1.toml"\n' };
    for (let i = 1; i <= 10; i++) files[`f${i}.toml`] = `include = "f${i + 1}.toml"\n`;
    files['f11.toml'] = 'description = "deep"\n';
    expect(() => loadRaw(write(files))).toThrow('more than 8 files deep');
  });

  test('duplicate key', () => {
    const cfg = write({ 'runspec.toml': '[r]\ndescription = "here"\ninclude = "r.toml"\n', 'r.toml': 'description = "there"\n' });
    expect(() => loadRaw(cfg)).toThrow('sets description both in runspec.toml and in its include r.toml');
  });

  test('missing file', () => {
    expect(() => loadRaw(write({ 'runspec.toml': '[r]\ninclude = "nope.toml"\n' }))).toThrow(/nope\.toml.*not found/);
  });

  test.each([
    ['../outside.toml', 'outside the package'],
    ['/etc/x.toml', 'relative path'],
    ['notes.txt', 'must name a .toml file'],
  ])('bad path %s', (p, message) => {
    expect(() => loadRaw(write({ 'runspec.toml': `[r]\ninclude = "${p}"\n` }))).toThrow(message);
  });

  test('symlink out of the package is refused', () => {
    const outsideDir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-outside-'));
    const outside = path.join(outsideDir, 'x.toml');
    fs.writeFileSync(outside, 'description = "x"\n');
    const cfg = write({ 'runspec.toml': '[r]\ninclude = "link.toml"\n' });
    try {
      fs.symlinkSync(outside, path.join(path.dirname(cfg), 'link.toml'));
    } catch {
      return;
    }
    expect(() => loadRaw(cfg)).toThrow('outside the package directory');
  });

  test('loadText with and without a reader', () => {
    const files: Record<string, string> = { 'cmd/x.toml': 'description = "x"\n' };
    const text = '[r]\n[r.commands.x]\ninclude = "cmd/x.toml"\n';
    expect(loadText(text, (rel) => files[rel]).runnables['r'].commands['x'].description).toBe('x');
    expect(() => loadText(text)).toThrow('no file access');
  });

  test('hasInclude', () => {
    expect(hasInclude('[r]\n  include="x.toml"\n')).toBe(true);
    expect(hasInclude('[r]\ndescription = "include = x"\n')).toBe(false);
  });
});

describe('shared-args', () => {
  test('use with overrides', () => {
    const spec = loadText(`
[gitlab.shared-args]
project = {type = "str", required = false, env = "GITLAB_PROJECT", description = "Project id or path"}

[gitlab.commands.create-issue.args]
project = {use = "project", required = true}
source-project = {use = "project"}
`).runnables['gitlab'].commands['create-issue'].args;
    expect(spec['project'].required).toBe(true);
    expect(spec['project'].description).toBe('Project id or path');
    expect(spec['source-project'].env).toEqual(['GITLAB_PROJECT']);
  });

  test('nearest definition wins', () => {
    const cmds = loadText(`
[config.shared-args]
limit = 10
[r.shared-args]
limit = 20
[r.commands.a.shared-args]
limit = 30
[r.commands.a.args]
limit = {use = "limit"}
[r.commands.b.args]
limit = {use = "limit"}
`).runnables['r'].commands;
    expect(cmds['a'].args['limit'].default).toBe(30);
    expect(cmds['b'].args['limit'].default).toBe(20);
  });

  test('shared-args tables are removed', () => {
    const out = resolve(parseTOML('[config.shared-args]\nx = 1\n[r.args]\nx = {use = "x"}\n') as Record<string, unknown>);
    expect(out).toEqual({ config: {}, r: { args: { x: { default: 1 } } } });
    expect(normalise(out).runnables['r'].args['x'].default).toBe(1);
  });

  test('unknown name suggests', () => {
    expect(() => loadText('[r.shared-args]\nproject = "x"\n[r.args]\np = {use = "projet"}\n')).toThrow(
      /unknown shared arg 'projet'[\s\S]*Did you mean 'project'/,
    );
  });

  test('sibling scope is not visible', () => {
    expect(() => loadText('[r.commands.a.shared-args]\nx = 1\n[r.commands.b.args]\nx = {use = "x"}\n')).toThrow(
      "unknown shared arg 'x'",
    );
  });

  test('a definition cannot use', () => {
    expect(() => loadText('[r.shared-args]\na = 1\nb = {use = "a"}\n')).toThrow("can't itself use");
  });
});
