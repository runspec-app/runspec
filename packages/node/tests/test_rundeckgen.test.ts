import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  rdOptionToken,
  shellQuote,
  ENV_PRELUDE,
  PY_ENV_PRELUDE,
  FRAME_FUNC,
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

test('rdOptionToken is the inline-script substitution token', () => {
  expect(rdOptionToken('target')).toBe('@option.target@');
  expect(rdOptionToken('node-filter')).toBe('@option.node-filter@');
});

test('shellQuote matches Python shlex.quote', () => {
  expect(shellQuote('/opt/app/.venv/bin/app')).toBe('/opt/app/.venv/bin/app');
  expect(shellQuote('')).toBe("''");
  expect(shellQuote("a b'c")).toBe(`'a b'"'"'c'`);
});

test('preludes are byte-identical to spec/SPEC.md', () => {
  const spec = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'spec', 'SPEC.md'), 'utf-8');
  expect(spec).toContain(ENV_PRELUDE);
  expect(spec).toContain(PY_ENV_PRELUDE);
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

test('script assigns every arg from its option token, no RD_OPTION_*', () => {
  const s = renderScript(
    'deploy',
    [],
    {
      target: arg({ type: 'str' }),
      verbose: arg({ type: 'flag', default: false }),
      first: arg({ type: 'str', position: 1 }),
      extra: arg({ type: 'rest' }),
      token: arg({ type: 'password' }),
    },
    '/opt/app/.venv/bin/app',
  );
  for (const [name, v] of [['target', 'TARGET'], ['verbose', 'VERBOSE'], ['first', 'FIRST'], ['extra', 'EXTRA'], ['token', 'TOKEN']]) {
    expect(s).toContain(`RUNSPEC_DEPLOY_ARG_${v}=@option.${name}@`);
  }
  expect(s).not.toContain('RD_OPTION');
  expect(s).toContain('if [ -n "${!rs_var}" ]; then export "$rs_var"; else unset "$rs_var"; fi');
});

test('no value goes on the command line', () => {
  const s = renderScript('deploy', ['start'], { target: arg({ type: 'str' }), token: arg({ type: 'password' }) }, '/opt/app/.venv/bin/app');
  expect(s).not.toContain('--target');
  expect(s).not.toContain('--token');
  expect(s.trim().endsWith('exec /opt/app/.venv/bin/app start')).toBe(true);
});

test('run_as sends the env frame on stdin to the venv python', () => {
  const s = renderScript('deploy', ['start'], { token: arg({ type: 'password' }) }, '/opt/app/.venv/bin/deploy', 'svc', '/opt/app/.venv');
  expect(s).toContain('rs_frame RUNSPEC_DEPLOY_ARG_TOKEN | sudo -n -u svc /opt/app/.venv/bin/python -c "$RS_PRELUDE" /opt/app/.venv/bin/deploy start');
  expect(s).not.toContain('export');
});

test('run_as without a venv uses the shell prelude', () => {
  const s = renderScript('deploy', [], { x: arg({ type: 'str' }) }, 'deploy', 'svc');
  expect(s).toContain('| sudo -n -u svc sh -c "$RS_PRELUDE" sh deploy');
});

test('run_as with no args just execs sudo', () => {
  const s = renderScript('deploy', [], {}, '/opt/v/bin/deploy', 'svc', '/opt/v');
  expect(s.trim().endsWith('exec sudo -n -u svc /opt/v/bin/deploy')).toBe(true);
});

test('run_as rejects shell metacharacters', () => {
  expect(() => renderScript('deploy', [], {}, 'deploy', 'svc; id')).toThrow();
});

function hasTool(name: string): boolean {
  try {
    execFileSync('bash', ['-c', `command -v ${name}`]);
    return true;
  } catch {
    return false;
  }
}

const bashIt = hasTool('bash') && hasTool('od') && hasTool('python3') ? test : test.skip;

bashIt('values round-trip through bash into the runnable env', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  fs.mkdirSync(path.join(dir, 'bin'));
  const show = path.join(dir, 'bin', 'show');
  fs.writeFileSync(
    show,
    "#!/usr/bin/env python3\nimport json, os, sys\nprint(json.dumps([{k: v for k, v in os.environ.items() if k.startswith('RUNSPEC_SHOW_')}, sys.argv[1:]]))\n",
  );
  fs.chmodSync(show, 0o755);
  const values: Record<string, string> = { plain: '3', tricky: "a b'c\"$(id) `x`;\nline2 ü", unset: '' };
  const args = Object.fromEntries(Object.keys(values).map((k) => [k, arg({ type: 'str' })]));
  let script = renderScript('show', ['sub'], args, show);
  script = script.replace(/@option\.([\w-]+)@/g, (_m, n: string) => shellQuote(values[n]));
  const [env, argv] = JSON.parse(execFileSync('bash', ['-c', script]).toString());
  expect(env).toEqual({ RUNSPEC_SHOW_ARG_PLAIN: '3', RUNSPEC_SHOW_ARG_TRICKY: values.tricky });
  expect(argv).toEqual(['sub']);
});

bashIt('rs_frame writes the SPEC env frame', () => {
  const value = "x y'z\nü$HOME";
  const out = execFileSync('bash', ['-c', `${FRAME_FUNC}\nA="$1"\nB=\nrs_frame A B`, 'bash', value]);
  const esc = Array.from(Buffer.from(value, 'utf-8'))
    .map((b) => '\\0' + b.toString(8).padStart(3, '0'))
    .join('');
  expect(out.toString()).toBe(`A=${esc}\n.\n`);
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

test('mergeSettings run_as precedence and node-resolved forms', () => {
  expect(mergeSettings('app', { runAs: 'cli' }, { run_as: 'cfg' }, { run_as: 'run' }, 'own').runAs).toBe('cli');
  expect(mergeSettings('app', {}, { run_as: 'cfg' }, { run_as: 'run' }, 'own').runAs).toBe('run');
  expect(mergeSettings('app', {}, { run_as: 'cfg' }, {}, 'own').runAs).toBe('cfg');
  expect(mergeSettings('app', {}, {}, {}, 'own').runAs).toBe('own');
  for (const v of ['@owner', '$SVC', { 'web-*': 'svc' }, '', undefined]) {
    expect(mergeSettings('app', {}, {}, {}, v).runAs).toBeUndefined();
  }
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
