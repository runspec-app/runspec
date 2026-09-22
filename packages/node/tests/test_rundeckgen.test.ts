import {
  rdEnvVar,
  jobFilename,
  renderOption,
  renderScript,
  renderNotifications,
  mergeSettings,
  renderJob,
  renderRunnable,
  dumps,
  RundeckSettings,
} from '../src/rundeckgen';
import { secretEnvVar, expandLeaves } from '../src/emitutil';
import { ArgSpec, ScriptSpec } from '../src/models';

function arg(partial: Partial<ArgSpec>): ArgSpec {
  return { name: 'x', ...partial } as ArgSpec;
}
function script(partial: Partial<ScriptSpec>): ScriptSpec {
  return { name: 'r', args: {}, groups: {}, commands: {}, ...partial } as ScriptSpec;
}

// ── name helpers ──────────────────────────────────────────────────────────────

test('rdEnvVar uppercases and underscores', () => {
  expect(rdEnvVar('target')).toBe('RD_OPTION_TARGET');
  expect(rdEnvVar('node-filter')).toBe('RD_OPTION_NODE_FILTER');
  expect(rdEnvVar('older_than')).toBe('RD_OPTION_OLDER_THAN');
});

test('secretEnvVar keys on the runnable, not the leaf', () => {
  expect(secretEnvVar('deploy', 'token')).toBe('RUNSPEC_DEPLOY_ARG_TOKEN');
  expect(secretEnvVar('runspec-jira', 'jira-pat')).toBe('RUNSPEC_RUNSPEC_JIRA_ARG_JIRA_PAT');
});

test('jobFilename', () => {
  expect(jobFilename('cron_add')).toBe('cron_add.json');
});

// ── options ─────────────────────────────────────────────────────────────────

test('string option with default', () => {
  expect(renderOption('pattern', arg({ type: 'str', default: '*.tmp', description: 'Glob' }))).toEqual({
    name: 'pattern',
    description: 'Glob',
    value: '*.tmp',
  });
});

test('int option gets a validating regex + stringified default', () => {
  const opt = renderOption('depth', arg({ type: 'int', default: 3 }));
  expect(opt['value']).toBe('3');
  expect(opt['regex']).toBe('^-?\\d+$');
});

test('choice option is enforced values', () => {
  const opt = renderOption('format', arg({ type: 'choice', options: ['text', 'json'], default: 'json' }));
  expect(opt['values']).toEqual(['text', 'json']);
  expect(opt['enforced']).toBe(true);
  expect(opt['value']).toBe('json');
});

test('flag option is true/false enforced', () => {
  const opt = renderOption('verbose', arg({ type: 'flag', default: false }));
  expect(opt['values']).toEqual(['true', 'false']);
  expect(opt['enforced']).toBe(true);
  expect(opt['value']).toBe('false');
});

test('password option is secure + exposed with no value', () => {
  const opt = renderOption('token', arg({ type: 'password', required: true, description: 'API token' }));
  expect(opt['secure']).toBe(true);
  expect(opt['valueExposed']).toBe(true);
  expect(opt['required']).toBe(true);
  expect('value' in opt).toBe(false);
});

test('option description notes constraints', () => {
  const opt = renderOption('id', arg({ type: 'str', required: true, position: 1, description: 'Host' }));
  expect(String(opt['description'])).toContain('positional #1');
});

// ── script ────────────────────────────────────────────────────────────────────

test('script guards named args, conditions flags, orders positionals', () => {
  const s = renderScript(
    'app',
    [],
    { target: arg({ type: 'str' }), verbose: arg({ type: 'flag', default: false }), b: arg({ type: 'str', position: 2 }), a: arg({ type: 'str', position: 1 }) },
    '/opt/app/.venv/bin/app',
  );
  expect(s).toContain('if [ -n "${RD_OPTION_TARGET:-}" ]; then args+=(--target "${RD_OPTION_TARGET:-}"); fi');
  expect(s).toContain('if [ "${RD_OPTION_VERBOSE:-}" = "true" ]; then args+=(--verbose); fi');
  expect(s.indexOf('RD_OPTION_A')).toBeLessThan(s.indexOf('RD_OPTION_B'));
});

test('rest arg word-splits after --', () => {
  const s = renderScript('app', [], { extra: arg({ type: 'rest' }) }, 'app');
  expect(s).toContain('args+=(-- ${RD_OPTION_EXTRA:-})');
});

test('secret is exported, never on the command line', () => {
  const s = renderScript('deploy', [], { token: arg({ type: 'password' }) }, 'deploy');
  expect(s).toContain('export RUNSPEC_DEPLOY_ARG_TOKEN="${RD_OPTION_TOKEN:-}"');
  expect(s).not.toContain('--token');
});

test('invocation includes the command path', () => {
  const s = renderScript('deploy', ['start'], {}, '/opt/app/.venv/bin/app');
  expect(s.trim().endsWith('exec /opt/app/.venv/bin/app start "${args[@]}"')).toBe(true);
});

// ── leaf expansion ────────────────────────────────────────────────────────────

test('no commands → a single leaf', () => {
  const leaves = expandLeaves('tool', script({ description: 'd', args: { x: arg({ type: 'str' }) } }));
  expect(leaves.length).toBe(1);
  expect(leaves[0].name).toBe('tool');
  expect(leaves[0].commandPath).toEqual([]);
});

test('subcommands flatten with inherited globals, child overrides parent', () => {
  const s = script({
    args: { mode: arg({ type: 'str', default: 'root' }) },
    commands: {
      run: script({ name: 'run', args: { mode: arg({ type: 'str', default: 'child' }), extra: arg({ type: 'str' }) } }),
      list: script({ name: 'list' }),
    },
  });
  const byName = Object.fromEntries(expandLeaves('cron', s).map((l) => [l.name, l]));
  expect(Object.keys(byName).sort()).toEqual(['cron_list', 'cron_run']);
  expect(new Set(Object.keys(byName['cron_run'].args))).toEqual(new Set(['mode', 'extra']));
  expect(byName['cron_run'].args['mode'].default).toBe('child');
  expect(byName['cron_run'].commandPath).toEqual(['run']);
});

// ── settings ──────────────────────────────────────────────────────────────────

test('mergeSettings precedence CLI > runnable > config', () => {
  const s = mergeSettings('app', { group: 'cli' }, { group: 'config' }, { group: 'runnable' });
  expect(s.group).toBe('cli');
  const s2 = mergeSettings('app', {}, { schedule: 'cfg' }, { schedule: 'run' });
  expect(s2.schedule).toBe('run');
  expect(mergeSettings('app', { venv: '/opt/v/' }, {}, {}).invocation).toBe('/opt/v/bin/app');
});

test('orchestrator defaults subset with a node filter, none without', () => {
  expect(mergeSettings('app', { nodeFilter: 'tags: web' }, {}, {}).orchestrator).toBe('subset');
  expect(mergeSettings('app', {}, {}, {}).orchestrator).toBe('none');
});

// ── notifications ─────────────────────────────────────────────────────────────

test('notifications render email + webhook', () => {
  const out = renderNotifications({ onfailure: { email: 'ops@x' }, onsuccess: { webhook: 'https://h/x' } });
  expect((out['onfailure'] as any)['email']['recipients']).toBe('ops@x');
  expect((out['onsuccess'] as any)['webhook']['urls']).toBe('https://h/x');
});

// ── job assembly ──────────────────────────────────────────────────────────────

function settings(over: Partial<RundeckSettings> = {}): RundeckSettings {
  return { ...mergeSettings('app', {}, {}, {}), ...over };
}
function leaf(name = 'app', args: Record<string, ArgSpec> = {}) {
  return { name, description: 'd', commandPath: [], args };
}

test('renderJob core shape', () => {
  const job = renderJob('app', leaf('app', { x: arg({ type: 'str', required: true }) }), settings());
  expect(job['name']).toBe('app');
  expect(job['loglevel']).toBe('INFO');
  expect((job['sequence'] as any)['strategy']).toBe('node-first');
  expect((job['sequence'] as any)['commands'][0]['scriptInterpreter']).toBe('/bin/bash');
  expect((job['options'] as any[])[0]['name']).toBe('x');
});

test('renderJob omits options when there are none', () => {
  expect('options' in renderJob('app', leaf('app', {}), settings())).toBe(false);
});

test('node filter adds orchestrator subset', () => {
  const job = renderJob('app', leaf(), settings({ nodeFilter: 'tags: web', orchestrator: 'subset', threadcount: 1 }));
  expect((job['nodefilters'] as any)['filter']).toBe('tags: web');
  expect(job['orchestrator']).toEqual({ type: 'subset', configuration: { count: '1' } });
});

test('no node filter → no orchestrator', () => {
  const job = renderJob('app', leaf(), settings());
  expect('nodefilters' in job).toBe(false);
  expect('orchestrator' in job).toBe(false);
});

test('schedule renders a crontab', () => {
  const job = renderJob('app', leaf(), settings({ schedule: '0 0 2 * * ?' }));
  expect(job['schedule']).toEqual({ crontab: '0 0 2 * * ?' });
  expect(job['scheduleEnabled']).toBe(true);
});

test('uuid is deterministic and name-dependent', () => {
  const a = renderJob('app', leaf(), settings())['uuid'];
  const b = renderJob('app', leaf(), settings())['uuid'];
  expect(a).toBe(b);
  expect(renderJob('app', leaf('other'), settings())['uuid']).not.toBe(a);
});

// ── serialization ─────────────────────────────────────────────────────────────

test('dumps is stable and a valid JSON array', () => {
  const jobs = renderRunnable(
    'cron',
    script({
      commands: {
        add: script({ name: 'add', args: { schedule: arg({ type: 'str', required: true }) } }),
        list: script({ name: 'list' }),
      },
    }),
    settings(),
  );
  const t1 = dumps(jobs);
  expect(dumps(jobs)).toBe(t1);
  const parsed = JSON.parse(t1);
  expect(Array.isArray(parsed)).toBe(true);
  expect(new Set(parsed.map((j: any) => j.name))).toEqual(new Set(['cron_add', 'cron_list']));
  expect(t1.endsWith('\n')).toBe(true);
});
