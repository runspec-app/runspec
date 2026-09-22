import {
  moduleName,
  paramName,
  moduleFilename,
  argSpecEntry,
  argsMetadata,
  shortDescription,
  renderDocumentation,
  renderExamples,
  renderModule,
  renderRunnable,
} from '../src/ansiblegen';
import { ArgSpec, ScriptSpec } from '../src/models';

function arg(partial: Partial<ArgSpec>): ArgSpec {
  return { name: 'x', ...partial } as ArgSpec;
}
function script(partial: Partial<ScriptSpec>): ScriptSpec {
  return { name: 'r', args: {}, groups: {}, commands: {}, ...partial } as ScriptSpec;
}
function leaf(name = 'scan', args: Record<string, ArgSpec> = {}, commandPath: string[] = [], description = 'Scan things') {
  return { name, description, commandPath, args };
}

// ── name helpers ──────────────────────────────────────────────────────────────

test('module/param names sanitize hyphens', () => {
  expect(moduleName('cert-check')).toBe('cert_check');
  expect(moduleName('filesystem_read-text-file')).toBe('filesystem_read_text_file');
  expect(paramName('older-than')).toBe('older_than');
  expect(moduleFilename('cert-check')).toBe('cert_check.py');
});

// ── argument_spec ─────────────────────────────────────────────────────────────

test('type mapping', () => {
  expect(argSpecEntry(arg({ type: 'str' }))['type']).toBe('str');
  expect(argSpecEntry(arg({ type: 'int' }))['type']).toBe('int');
  expect(argSpecEntry(arg({ type: 'float' }))['type']).toBe('float');
  expect(argSpecEntry(arg({ type: 'flag' }))['type']).toBe('bool');
  expect(argSpecEntry(arg({ type: 'path' }))['type']).toBe('path');
});

test('choice carries choices', () => {
  const e = argSpecEntry(arg({ type: 'choice', options: ['text', 'json'] }));
  expect(e['type']).toBe('str');
  expect(e['choices']).toEqual(['text', 'json']);
});

test('rest is a list of str, no default', () => {
  expect(argSpecEntry(arg({ type: 'rest' }))).toEqual({ type: 'list', elements: 'str' });
});

test('password is no_log with no default', () => {
  const e = argSpecEntry(arg({ type: 'password', required: true }));
  expect(e['no_log']).toBe(true);
  expect(e['required']).toBe(true);
  expect('default' in e).toBe(false);
});

test('default passes through native (not stringified)', () => {
  expect(argSpecEntry(arg({ type: 'int', default: 7 }))['default']).toBe(7);
  expect(argSpecEntry(arg({ type: 'flag', default: false }))['default']).toBe(false);
});

// ── argv metadata ─────────────────────────────────────────────────────────────

test('args metadata categorizes and wires the secret env', () => {
  const rows = Object.fromEntries(
    argsMetadata('deploy', {
      target: arg({ type: 'str' }),
      verbose: arg({ type: 'flag', default: false }),
      name: arg({ type: 'str', position: 1 }),
      extra: arg({ type: 'rest' }),
      token: arg({ type: 'password' }),
    }).map((r) => [r[0], r]),
  );
  expect(rows['target'][1]).toBe('named');
  expect(rows['verbose'][1]).toBe('flag');
  expect(rows['name'][1]).toBe('positional');
  expect(rows['name'][3]).toBe(1);
  expect(rows['extra'][1]).toBe('rest');
  expect(rows['token'][1]).toBe('secret');
  expect(rows['token'][4]).toBe('RUNSPEC_DEPLOY_ARG_TOKEN');
  expect(rows['target'][2]).toBe('--target');
  expect(rows['verbose'][4]).toBeNull();
});

test('cli flag keeps hyphens while the param is underscored', () => {
  const rows = argsMetadata('t', { 'older-than': arg({ type: 'int' }) });
  expect(rows[0][0]).toBe('older_than');
  expect(rows[0][2]).toBe('--older-than');
});

// ── documentation ─────────────────────────────────────────────────────────────

test('short_description strips a trailing period', () => {
  expect(shortDescription('Scan a directory.')).toBe('Scan a directory');
  expect(shortDescription('')).toBe('Run the runspec runnable');
});

test('documentation lists options with type/choices/default and escapes colons', () => {
  const doc = renderDocumentation('scan', 'Do a thing: fast', {
    format: arg({ type: 'choice', options: ['text', 'json'], default: 'json', description: 'Fmt' }),
  });
  expect(doc).toContain('module: scan');
  expect(doc).toContain('"Do a thing: fast"');
  expect(doc).toContain('type: str');
  expect(doc).toContain('choices: ["text", "json"]');
  expect(doc).toContain('default: "json"');
});

test('documentation declares list elements for a rest arg', () => {
  const doc = renderDocumentation('m', 'd', { extra: arg({ type: 'rest' }) });
  expect(doc).toContain('type: list');
  expect(doc).toContain('elements: str');
});

test('examples omit password args', () => {
  const ex = renderExamples('deploy', { token: arg({ type: 'password', required: true }), env: arg({ type: 'str' }) });
  expect(ex).not.toContain('token');
  expect(ex).toContain('env');
});

// ── module assembly ───────────────────────────────────────────────────────────

test('render_module produces a complete Ansible module', () => {
  const src = renderModule(
    'scan',
    leaf('scan', {
      directory: arg({ type: 'path', default: '.', description: 'Dir' }),
      older_than: arg({ type: 'int', default: 7 }),
      format: arg({ type: 'choice', options: ['text', 'json'], default: 'json' }),
      delete: arg({ type: 'flag', default: false }),
      token: arg({ type: 'password', required: true }),
    }),
    '/opt/app/.venv/bin/scan',
  );
  expect(src).toContain('RUNNABLE = "/opt/app/.venv/bin/scan"');
  expect(src).toContain('from ansible.module_utils.basic import AnsibleModule');
  expect(src).toContain('supports_check_mode=True');
  expect(src).toContain('RUNSPEC_AUTONOMY');
  expect(src).toContain('"no_log": True');
});

test('secret rides the env channel, never argv', () => {
  const src = renderModule('deploy', leaf('deploy', { token: arg({ type: 'password', required: true }) }), 'deploy');
  expect(src).toContain('"RUNSPEC_DEPLOY_ARG_TOKEN"');
  expect(src).toContain('"no_log": True');
});

test('subcommand path is rendered', () => {
  const src = renderModule('deploy', leaf('deploy_start', {}, ['start']), '/v/bin/deploy');
  expect(src).toContain('COMMAND_PATH = [\n    "start",\n]');
});

test('render_runnable flattens subcommands and is deterministic', () => {
  const s = script({
    commands: {
      add: script({ name: 'add', args: { schedule: arg({ type: 'str', required: true }) } }),
      list: script({ name: 'list' }),
    },
  });
  const files = renderRunnable('cron', s, '/v/bin/cron');
  expect(new Set(Object.keys(files))).toEqual(new Set(['cron_add.py', 'cron_list.py']));
  expect(renderRunnable('cron', s, '/v/bin/cron')).toEqual(files);
});
