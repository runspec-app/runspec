import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse, loadSpec } from '../src/parser';

function makeTmpConfig(toml: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-parser-test-'));
  const configPath = path.join(dir, 'runspec.toml');
  fs.writeFileSync(configPath, toml);
  return configPath;
}

const QUALITY_TOML = `
[compress]
[compress.args]
quality = {default = 85, range = [1, 100]}
`;

const QUALITY_ALIAS_TOML = `
[compress]
[compress.args]
quality = {default = 85, range = [1, 100], env = "CI_QUALITY"}
`;

// ── env resolution tier ───────────────────────────────────────────────────────

describe('env resolution', () => {
  afterEach(() => {
    delete process.env['RUNSPEC_COMPRESS_ARG_QUALITY'];
    delete process.env['CI_QUALITY'];
  });

  test('RUNSPEC_<RUNNABLE>_ARG_* provides auto default when no CLI arg', () => {
    const configPath = makeTmpConfig(QUALITY_TOML);
    process.env['RUNSPEC_COMPRESS_ARG_QUALITY'] = '95';
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(95);
  });

  test('CLI arg wins over RUNSPEC_<RUNNABLE>_ARG_*', () => {
    const configPath = makeTmpConfig(QUALITY_TOML);
    process.env['RUNSPEC_COMPRESS_ARG_QUALITY'] = '95';
    const args = parse({ scriptName: 'compress', argv: ['--quality', '80'], configPath });
    expect(args['quality']).toBe(80);
  });

  test('developer alias fallback when RUNSPEC_<RUNNABLE>_ARG_* not set', () => {
    const configPath = makeTmpConfig(QUALITY_ALIAS_TOML);
    process.env['CI_QUALITY'] = '70';
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(70);
  });

  test('RUNSPEC_<RUNNABLE>_ARG_* wins over developer alias', () => {
    const configPath = makeTmpConfig(QUALITY_ALIAS_TOML);
    process.env['RUNSPEC_COMPRESS_ARG_QUALITY'] = '95';
    process.env['CI_QUALITY'] = '70';
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(95);
  });

  test('no env vars set falls back to spec default', () => {
    const configPath = makeTmpConfig(QUALITY_TOML);
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(85);
  });
});

// ── help display ──────────────────────────────────────────────────────────────

describe('help display', () => {
  let logSpy: jest.SpyInstance;
  let exitSpy: jest.SpyInstance;
  let output: string[];

  beforeEach(() => {
    output = [];
    logSpy = jest.spyOn(console, 'log').mockImplementation((...args) => {
      output.push(args.join(' '));
    });
    exitSpy = jest.spyOn(process, 'exit').mockImplementation((() => {
      throw new Error('__exit__');
    }) as never);
  });

  afterEach(() => {
    logSpy.mockRestore();
    exitSpy.mockRestore();
  });

  const runHelp = (toml: string, name: string, argv: string[]): string => {
    const configPath = makeTmpConfig(toml);
    try {
      parse({ scriptName: name, argv, configPath });
    } catch (e) {
      if ((e as Error).message !== '__exit__') throw e;
    }
    return output.join('\n');
  };

  test('usage line renders <command> after parent args', () => {
    const toml = `
[pipeline]
description = "Run a pipeline"
[pipeline.args]
verbose = {type = "flag"}
config  = {type = "path"}
[pipeline.commands.run]
description = "Run it"
`;
    const out = runHelp(toml, 'pipeline', ['--help']);
    const usage = out.split('\n').find((l) => l.startsWith('Usage:'))!;
    expect(usage.indexOf('--verbose')).toBeLessThan(usage.indexOf('<command>'));
    expect(usage.indexOf('--config')).toBeLessThan(usage.indexOf('<command>'));
    expect(usage.trimEnd().endsWith('<command>')).toBe(true);
    expect(out).toContain('Commands:');
    expect(out).toContain('run');
  });

  test('nested subcommands resolve to full path in usage', () => {
    const toml = `
[outer]
[outer.commands.inner]
[outer.commands.inner.commands.deep]
description = "Deepest"
[outer.commands.inner.commands.deep.args]
bar = {type = "str", required = true}
`;
    const out = runHelp(toml, 'outer', ['inner', 'deep', '--help']);
    expect(out).toContain('Usage: outer inner deep');
    expect(out).toContain('--bar');
    expect(out).toContain('Deepest');
  });

  test('choice options render inline in usage', () => {
    const toml = `
[greet]
[greet.args]
fmt = {type = "choice", options = ["text", "json", "xml"], default = "text"}
`;
    const out = runHelp(toml, 'greet', ['--help']);
    expect(out).toContain('[--fmt <text|json|xml>]');
  });

  test('usage places <command> before -- <rest>', () => {
    const toml = `
[multi]
[multi.args]
host  = {type = "str", position = 1}
extra = {type = "rest"}
[multi.commands.run]
description = "Run it"
`;
    const out = runHelp(toml, 'multi', ['--help']);
    const usage = out.split('\n').find((l) => l.startsWith('Usage:'))!;
    expect(usage.indexOf('<host>')).toBeLessThan(usage.indexOf('<command>'));
    expect(usage.indexOf('<command>')).toBeLessThan(usage.indexOf('-- <extra>'));
  });
});

// ── require-command ─────────────────────────────────────────────────────────────

describe('require-command', () => {
  const DB_TOML = `
[db]
require-command = true
[db.commands.migrate]
[db.commands.seed]
`;

  test('no command errors and lists commands', () => {
    const configPath = makeTmpConfig(DB_TOML);
    let msg = '';
    try {
      parse({ scriptName: 'db', argv: [], configPath });
      throw new Error('expected parse to throw');
    } catch (e) {
      msg = (e as Error).message;
    }
    expect(msg).toContain('requires a command');
    expect(msg).toContain('migrate');
    expect(msg).toContain('seed');
  });

  test('valid command passes', () => {
    const configPath = makeTmpConfig(DB_TOML);
    const args = parse({ scriptName: 'db', argv: ['migrate'], configPath });
    expect(args.runspec_command_path).toEqual(['migrate']);
    expect(args.runspec_command).toBe('migrate');
  });

  test('nested requirement enforced at each depth', () => {
    const toml = `
[app]
require-command = true
[app.commands.db]
require-command = true
[app.commands.db.commands.migrate]
`;
    const configPath = makeTmpConfig(toml);
    // Choosing the intermediate command isn't enough — db itself requires one.
    expect(() => parse({ scriptName: 'app', argv: ['db'], configPath })).toThrow(/requires a command/);
    // Going all the way down satisfies it.
    const args = parse({ scriptName: 'app', argv: ['db', 'migrate'], configPath });
    expect(args.runspec_command_path).toEqual(['db', 'migrate']);
  });

  test('backward compatible: commands without require-command run at root', () => {
    const toml = `
[tool]
[tool.commands.run]
`;
    const configPath = makeTmpConfig(toml);
    const args = parse({ scriptName: 'tool', argv: [], configPath });
    expect(args.runspec_command_path).toEqual([]);
  });

  test('loadSpec bypasses enforcement', () => {
    const configPath = makeTmpConfig(DB_TOML);
    const spec = loadSpec({ scriptName: 'db', configPath });
    expect(spec.runspec_command_path).toEqual([]);
  });

  test('--help still works at a required level', () => {
    const configPath = makeTmpConfig(DB_TOML);
    const output: string[] = [];
    const logSpy = jest.spyOn(console, 'log').mockImplementation((...a) => { output.push(a.join(' ')); });
    const exitSpy = jest.spyOn(process, 'exit').mockImplementation((() => { throw new Error('__exit__'); }) as never);
    try {
      parse({ scriptName: 'db', argv: ['--help'], configPath });
    } catch (e) {
      if ((e as Error).message !== '__exit__') throw e;
    } finally {
      logSpy.mockRestore();
      exitSpy.mockRestore();
    }
    const out = output.join('\n');
    expect(out).toContain('Commands (required):');
    expect(out).toContain('migrate');
  });
});

// ── positional by name + env flag/list values (SPEC.md → Positional Arguments,
//    Serialisation) — parity with the Python pack ────────────────────────────────

const POS_TOML = `
[demo]
description = "d"
[demo.args]
target  = {type = "str", position = 1, description = "t"}
release = {type = "str", position = 2, required = false, description = "r"}
dry-run = {type = "flag", default = true, description = "d"}
verbose = {type = "flag", default = false, description = "v"}
tags    = {type = "str", multiple = true, required = false, description = "t"}
`;

describe('positional args by name', () => {
  test('by position', () => {
    const args = parse({ scriptName: 'demo', argv: ['prod', 'v1'], configPath: makeTmpConfig(POS_TOML) });
    expect(args['target']).toBe('prod');
    expect(args['release']).toBe('v1');
  });

  test('by name, incl. =form', () => {
    const args = parse({ scriptName: 'demo', argv: ['--target', 'prod', '--release=v1'], configPath: makeTmpConfig(POS_TOML) });
    expect(args['target']).toBe('prod');
    expect(args['release']).toBe('v1');
  });

  test('a bare token skips a slot already filled by name', () => {
    const configPath = makeTmpConfig(POS_TOML);
    let args = parse({ scriptName: 'demo', argv: ['--target', 'prod', 'v1'], configPath });
    expect([args['target'], args['release']]).toEqual(['prod', 'v1']);
    args = parse({ scriptName: 'demo', argv: ['--release', 'v1', 'prod'], configPath });
    expect([args['target'], args['release']]).toEqual(['prod', 'v1']);
  });

  test('both ways is an error', () => {
    expect(() => parse({ scriptName: 'demo', argv: ['prod', '--target', 'x'], configPath: makeTmpConfig(POS_TOML) })).toThrow(/both as a positional value and as --target/);
  });
});

describe('flag + multiple values from the environment', () => {
  afterEach(() => {
    for (const k of ['RUNSPEC_DEMO_ARG_DRY_RUN', 'RUNSPEC_DEMO_ARG_VERBOSE', 'RUNSPEC_DEMO_ARG_TAGS']) delete process.env[k];
  });

  test("'0'/'false' switch a flag off (was: any non-empty string read as true)", () => {
    const configPath = makeTmpConfig(POS_TOML);
    for (const v of ['0', 'false', 'no', 'off']) {
      process.env['RUNSPEC_DEMO_ARG_DRY_RUN'] = v;
      process.env['RUNSPEC_DEMO_ARG_VERBOSE'] = v;
      const args = parse({ scriptName: 'demo', argv: ['prod'], configPath });
      expect(args['dry_run']).toBe(false);
      expect(args['verbose']).toBe(false);
    }
    process.env['RUNSPEC_DEMO_ARG_VERBOSE'] = '1';
    expect(parse({ scriptName: 'demo', argv: ['prod'], configPath })['verbose']).toBe(true);
  });

  test('a multiple arg splits its newline-delimited env value', () => {
    process.env['RUNSPEC_DEMO_ARG_TAGS'] = 'a\nb\n';
    const args = parse({ scriptName: 'demo', argv: ['prod'], configPath: makeTmpConfig(POS_TOML) });
    expect(args['tags']).toEqual(['a', 'b']);
  });
});
