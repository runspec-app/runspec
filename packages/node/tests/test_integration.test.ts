import * as path from 'path';
import { loadRaw } from '../src/loader';
import { inferArg, inferScript } from '../src/inference';
import { RunSpecError } from '../src/errors';

const FIXTURES = path.resolve(__dirname, '../../../tests/integration/fixtures');
const SIMPLE = path.join(FIXTURES, 'simple.toml');
const COMPLEX = path.join(FIXTURES, 'complex.toml');

// ── simple.toml ───────────────────────────────────────────────────────────────

describe('simple.toml', () => {
  test('loads config section', () => {
    const raw = loadRaw(SIMPLE);
    expect(raw.config.autonomyDefault).toBe('confirm');
  });

  test('greet runnable present', () => {
    const raw = loadRaw(SIMPLE);
    expect(raw.runnables['greet']).toBeDefined();
    expect(raw.runnables['greet'].description).toBe('Greet someone from the command line');
    expect(raw.runnables['greet'].autonomy).toBe('autonomous');
  });

  test('greet args: name is str and required', () => {
    const raw = loadRaw(SIMPLE);
    const inferred = inferScript(raw.runnables['greet'], raw.config.autonomyDefault);
    expect(inferred.args['name'].type).toBe('str');
    expect(inferred.args['name'].required).toBe(true);
  });

  test('greet args: loud inferred as flag', () => {
    const raw = loadRaw(SIMPLE);
    const inferred = inferScript(raw.runnables['greet'], raw.config.autonomyDefault);
    expect(inferred.args['loud'].type).toBe('flag');
    expect(inferred.args['loud'].required).toBe(false);
    expect(inferred.args['loud'].default).toBe(false);
  });

  test('greet args: times inferred as int', () => {
    const raw = loadRaw(SIMPLE);
    const inferred = inferScript(raw.runnables['greet'], raw.config.autonomyDefault);
    expect(inferred.args['times'].type).toBe('int');
    expect(inferred.args['times'].default).toBe(1);
  });
});

// ── complex.toml ──────────────────────────────────────────────────────────────

describe('complex.toml', () => {
  test('loads config section', () => {
    const raw = loadRaw(COMPLEX);
    expect(raw.config.autonomyDefault).toBe('confirm');
    expect(raw.config.lang).toBe('python');
    expect(raw.config.version).toBe('1');
  });

  test('pipeline runnable present', () => {
    const raw = loadRaw(COMPLEX);
    expect(raw.runnables['pipeline']).toBeDefined();
    expect(raw.runnables['pipeline'].description).toBe('Process and validate data pipeline files');
  });

  test('pipeline has run and validate subcommands', () => {
    const raw = loadRaw(COMPLEX);
    const cmds = raw.runnables['pipeline'].commands;
    expect(cmds['run']).toBeDefined();
    expect(cmds['validate']).toBeDefined();
  });

  test('run subcommand: input is path and required', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['input'].type).toBe('path');
    expect(run.args['input'].required).toBe(true);
  });

  test('run subcommand: format is choice with default', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['format'].type).toBe('choice');
    expect(run.args['format'].options).toEqual(['json', 'csv', 'parquet']);
    expect(run.args['format'].default).toBe('json');
    expect(run.args['format'].required).toBe(false);
  });

  test('run subcommand: workers inferred as int with range', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['workers'].type).toBe('int');
    expect(run.args['workers'].default).toBe(4);
    expect(run.args['workers'].range).toEqual([1, 32]);
  });

  test('run subcommand: dry-run inferred as flag', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['dry-run'].type).toBe('flag');
    expect(run.args['dry-run'].default).toBe(false);
  });

  test('run subcommand: tag is multiple', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['tag'].multiple).toBe(true);
    expect(run.args['tag'].type).toBe('str');
  });

  test('run subcommand: fields has delimiter', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['fields'].delimiter).toBe(',');
    expect(run.args['fields'].multiple).toBe(true);
  });

  test('run subcommand: api-key has env and autonomy', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['api-key'].env).toEqual(['PIPELINE_API_KEY']);
    expect(run.args['api-key'].autonomy).toBe('manual');
  });

  test('run subcommand: verbose has short flag', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['verbose'].short).toBe('-v');
  });

  test('run subcommand: threads has deprecated field', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.args['threads'].deprecated).toBe('use --workers instead');
  });

  test('run subcommand: groups defined', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.groups['input-format']).toBeDefined();
    expect(run.groups['input-format'].exclusive).toBe(true);
    expect(run.groups['api-auth']).toBeDefined();
    expect(run.groups['api-auth'].inclusive).toBe(true);
  });

  test('validate subcommand: is autonomous', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const validate = inferred.commands['validate'];
    expect(validate.autonomy).toBe('autonomous');
  });

  test('validate subcommand: format is choice', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const validate = inferred.commands['validate'];
    expect(validate.args['format'].type).toBe('choice');
  });

  test('run subcommand: autonomy-reason preserved', () => {
    const raw = loadRaw(COMPLEX);
    const inferred = inferScript(raw.runnables['pipeline'], raw.config.autonomyDefault);
    const run = inferred.commands['run'];
    expect(run.autonomyReason).toBe('Writes output files and may call external APIs');
  });
});

// ── cross-fixture: inference rules ────────────────────────────────────────────

test('bool checked before int in inference (bool is not int)', () => {
  const arg = inferArg({ name: 'verbose', default: false } as any);
  expect(arg.type).toBe('flag');
  expect(typeof arg.default).toBe('boolean');
});

test('type=path with no default is required', () => {
  const arg = inferArg({ name: 'input', type: 'path' } as any);
  expect(arg.required).toBe(true);
});

test('choice type with options is not required when default present', () => {
  const arg = inferArg({ name: 'format', options: ['json', 'csv'], default: 'json' } as any);
  expect(arg.required).toBe(false);
  expect(arg.type).toBe('choice');
});

test('choice type with no options throws', () => {
  expect(() => inferArg({ name: 'fmt', type: 'choice' } as any)).toThrow(RunSpecError);
});
