import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse, loadSpec } from '../src/parser';
import { loadRaw } from '../src/loader';
import { inferScript } from '../src/inference';
import { buildSchema } from '../src/cli';
import { coerce, listTypes } from '../src/types';
import type { ArgSpec } from '../src/models';

function makeTmpConfig(toml: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-pw-test-'));
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, toml);
  return file;
}

const TOML = `
[deploy]
description = "Deploy"
[deploy.args.host]
type = "str"
hint = "web-01.example.com"
required = true
[deploy.args.secret]
type = "password"
description = "DB password"
`;

// ── coercion ──────────────────────────────────────────────────────────────────

test('password coerces like str', () => {
  const spec = { name: 'secret', type: 'password' } as ArgSpec;
  expect(coerce('hunter2', spec)).toBe('hunter2');
});

test('password honours min-length', () => {
  const spec = { name: 'secret', type: 'password', minLength: 8 } as ArgSpec;
  expect(() => coerce('ab', spec)).toThrow();
});

test('password is a registered type', () => {
  expect(listTypes()).toContain('password');
});

// ── refused on the command line ─────────────────────────────────────────────────

test('password refused as --secret value', () => {
  const configPath = makeTmpConfig(TOML);
  expect(() =>
    parse({ scriptName: 'deploy', argv: ['--host', 'h', '--secret', 'x'], configPath }),
  ).toThrow(/cannot be passed on the command line/);
});

test('password refused as --secret=value', () => {
  const configPath = makeTmpConfig(TOML);
  expect(() =>
    parse({ scriptName: 'deploy', argv: ['--host', 'h', '--secret=x'], configPath }),
  ).toThrow(/cannot be passed on the command line/);
});

// ── resolved from the environment ────────────────────────────────────────────────

test('password resolves from RUNSPEC_<RUNNABLE>_ARG_<NAME>', () => {
  const configPath = makeTmpConfig(TOML);
  process.env['RUNSPEC_DEPLOY_ARG_SECRET'] = 'fromenv';
  try {
    const args = parse({ scriptName: 'deploy', argv: ['--host', 'h'], configPath });
    expect(args['secret']).toBe('fromenv');
  } finally {
    delete process.env['RUNSPEC_DEPLOY_ARG_SECRET'];
  }
});

// ── hint plumbing ───────────────────────────────────────────────────────────────

test('hint is carried onto the spec', () => {
  const configPath = makeTmpConfig(TOML);
  const raw = loadRaw(configPath);
  expect(raw.runnables['deploy'].args['host'].hint).toBe('web-01.example.com');
  expect(raw.runnables['deploy'].args['secret'].hint).toBeUndefined();
});

// ── omitted from emitted agent schemas ───────────────────────────────────────────

test('password arg is excluded from emitted MCP schema', () => {
  const configPath = makeTmpConfig(TOML);
  const raw = loadRaw(configPath);
  const script = inferScript(raw.runnables['deploy'], raw.config.autonomyDefault);
  const schema = buildSchema('deploy', script, 'mcp');
  const input = schema['inputSchema'] as Record<string, unknown>;
  const props = input['properties'] as Record<string, unknown>;
  expect('host' in props).toBe(true);
  expect('secret' in props).toBe(false);
  expect((input['required'] as string[] | undefined) ?? []).not.toContain('secret');
});

// ── loadSpec never prompts ───────────────────────────────────────────────────────

test('loadSpec does not prompt for a required password', () => {
  const configPath = makeTmpConfig(TOML);
  // loadSpec disables prompting; required args make it throw at validation, but
  // it must never block on a prompt. (No TTY in tests anyway, but the gate is
  // what guarantees it.)
  expect(() => loadSpec({ scriptName: 'deploy', configPath })).toThrow();
});
