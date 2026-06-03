import { inferArg, inferScript, effectiveAutonomy, isMoreRestrictive, tsTypeOf } from '../src/inference';
import { RunSpecError } from '../src/errors';
import type { ArgSpec, ScriptSpec } from '../src/models';

function makeArg(overrides: Partial<ArgSpec> = {}): ArgSpec {
  return { name: 'test', args: [], ...overrides } as unknown as ArgSpec;
}

// ── inferArg — type inference ─────────────────────────────────────────────────

test('infers flag from boolean default', () => {
  const result = inferArg(makeArg({ default: false }));
  expect(result.type).toBe('flag');
});

test('infers flag from true default', () => {
  const result = inferArg(makeArg({ default: true }));
  expect(result.type).toBe('flag');
});

test('infers int from integer default', () => {
  const result = inferArg(makeArg({ default: 42 }));
  expect(result.type).toBe('int');
});

test('infers float from float default', () => {
  const result = inferArg(makeArg({ default: 3.14 }));
  expect(result.type).toBe('float');
});

test('infers str from string default', () => {
  const result = inferArg(makeArg({ default: 'json' }));
  expect(result.type).toBe('str');
});

test('infers choice when options present', () => {
  const result = inferArg(makeArg({ options: ['a', 'b'] }));
  expect(result.type).toBe('choice');
});

test('choice wins over type inference when options present', () => {
  const result = inferArg(makeArg({ options: ['a', 'b'], default: 'a' }));
  expect(result.type).toBe('choice');
});

test('defaults to str when no clues', () => {
  const result = inferArg(makeArg({}));
  expect(result.type).toBe('str');
});

test('preserves explicit type', () => {
  const result = inferArg(makeArg({ type: 'path' }));
  expect(result.type).toBe('path');
});

// ── inferArg — required inference ────────────────────────────────────────────

test('required=true when no default and not flag', () => {
  const result = inferArg(makeArg({ name: 'input' }));
  expect(result.required).toBe(true);
});

test('required=false when default present', () => {
  const result = inferArg(makeArg({ default: 'hello' }));
  expect(result.required).toBe(false);
});

test('required=false for flag with no default', () => {
  const result = inferArg(makeArg({ type: 'flag' }));
  expect(result.required).toBe(false);
});

test('required=false for flag with false default', () => {
  const result = inferArg(makeArg({ default: false }));
  expect(result.required).toBe(false);
});

test('preserves explicit required=false', () => {
  const result = inferArg(makeArg({ required: false }));
  expect(result.required).toBe(false);
});

// ── inferArg — errors ─────────────────────────────────────────────────────────

test('throws when type=choice but no options', () => {
  expect(() => inferArg(makeArg({ type: 'choice' }))).toThrow(RunSpecError);
});

// ── inferScript ───────────────────────────────────────────────────────────────

test('fills autonomy from config default', () => {
  const script: ScriptSpec = { name: 'test', args: {}, groups: {}, commands: {} };
  const result = inferScript(script, 'confirm');
  expect(result.autonomy).toBe('confirm');
});

test('preserves explicit autonomy', () => {
  const script: ScriptSpec = { name: 'test', autonomy: 'autonomous', args: {}, groups: {}, commands: {} };
  const result = inferScript(script, 'confirm');
  expect(result.autonomy).toBe('autonomous');
});

test('infers all args', () => {
  const script: ScriptSpec = {
    name: 'test',
    args: { verbose: { name: 'verbose', default: false }, workers: { name: 'workers', default: 4 } },
    groups: {},
    commands: {},
  };
  const result = inferScript(script, 'confirm');
  expect(result.args['verbose'].type).toBe('flag');
  expect(result.args['workers'].type).toBe('int');
});

test('recurses into subcommands', () => {
  const script: ScriptSpec = {
    name: 'test',
    args: {},
    groups: {},
    commands: {
      run: { name: 'run', args: { input: { name: 'input', type: 'path' } }, groups: {}, commands: {} },
    },
  };
  const result = inferScript(script, 'confirm');
  expect(result.commands['run'].autonomy).toBe('confirm');
  expect(result.commands['run'].args['input'].required).toBe(true);
});

// ── effectiveAutonomy ─────────────────────────────────────────────────────────

test('returns script autonomy when no arg overrides', () => {
  expect(effectiveAutonomy('confirm', {}, {})).toBe('confirm');
});

test('escalates to more restrictive arg autonomy', () => {
  const argSpecs = { apiKey: { name: 'api-key', autonomy: 'manual' } };
  expect(effectiveAutonomy('confirm', { apiKey: 'abc' }, argSpecs)).toBe('manual');
});

test('does not de-escalate', () => {
  const argSpecs = { verbose: { name: 'verbose', autonomy: 'autonomous' } };
  expect(effectiveAutonomy('confirm', { verbose: true }, argSpecs)).toBe('confirm');
});

// ── isMoreRestrictive ─────────────────────────────────────────────────────────

test('manual is more restrictive than confirm', () => {
  expect(isMoreRestrictive('manual', 'confirm')).toBe(true);
});

test('autonomous is not more restrictive than confirm', () => {
  expect(isMoreRestrictive('autonomous', 'confirm')).toBe(false);
});

// ── tsTypeOf (derived value type, computed on demand) ──────────────────────────

test('tsTypeOf: scalar types', () => {
  expect(tsTypeOf(makeArg({ type: 'str' }))).toBe('string');
  expect(tsTypeOf(makeArg({ type: 'int' }))).toBe('number');
  expect(tsTypeOf(makeArg({ type: 'flag' }))).toBe('boolean');
  expect(tsTypeOf(makeArg({ type: 'path' }))).toBe('string');
});

test('tsTypeOf: multiple suffixes []', () => {
  expect(tsTypeOf(makeArg({ type: 'str', multiple: true }))).toBe('string[]');
  expect(tsTypeOf(makeArg({ type: 'int', multiple: true }))).toBe('number[]');
});

test('tsTypeOf: rest is string[]', () => {
  expect(tsTypeOf(makeArg({ type: 'rest' }))).toBe('string[]');
});

test('tsTypeOf: custom type falls back to unknown', () => {
  expect(tsTypeOf(makeArg({ type: 'json-file', multiple: true }))).toBe('unknown[]');
});

test('tsTypeOf works on an inferred ArgSpec; type stays the item type', () => {
  const arg = inferArg(makeArg({ type: 'str', multiple: true }));
  expect(arg.type).toBe('str');
  expect(tsTypeOf(arg)).toBe('string[]');
});
