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
    delete process.env['RUNSPEC_ARG_QUALITY'];
    delete process.env['CI_QUALITY'];
  });

  test('RUNSPEC_ARG_* provides auto default when no CLI arg', () => {
    const configPath = makeTmpConfig(QUALITY_TOML);
    process.env['RUNSPEC_ARG_QUALITY'] = '95';
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(95);
  });

  test('CLI arg wins over RUNSPEC_ARG_*', () => {
    const configPath = makeTmpConfig(QUALITY_TOML);
    process.env['RUNSPEC_ARG_QUALITY'] = '95';
    const args = parse({ scriptName: 'compress', argv: ['--quality', '80'], configPath });
    expect(args['quality']).toBe(80);
  });

  test('developer alias fallback when RUNSPEC_ARG_* not set', () => {
    const configPath = makeTmpConfig(QUALITY_ALIAS_TOML);
    process.env['CI_QUALITY'] = '70';
    const args = parse({ scriptName: 'compress', argv: [], configPath });
    expect(args['quality']).toBe(70);
  });

  test('RUNSPEC_ARG_* wins over developer alias', () => {
    const configPath = makeTmpConfig(QUALITY_ALIAS_TOML);
    process.env['RUNSPEC_ARG_QUALITY'] = '95';
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
