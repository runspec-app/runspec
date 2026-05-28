import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse } from '../src/parser';

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
