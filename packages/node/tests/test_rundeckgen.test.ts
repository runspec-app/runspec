import { execFileSync, spawnSync } from 'child_process';
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

test('multiple arg: multi-valued option with a delimiter-joined list default', () => {
  const opt = renderOption('states', arg({ type: 'choice', options: ['Missing', 'Stale', 'Ok'], multiple: true, default: ['Missing', 'Stale'] }));
  expect(opt.multivalued).toBe(true);
  expect(opt.delimiter).toBe(',');
  expect(opt.values).toEqual(['Missing', 'Stale', 'Ok']);
  expect(opt.enforced).toBe(true);
  expect(opt.value).toBe('Missing,Stale');
  expect(opt.description).toContain('several values separated by ","');
});

test("multiple arg uses the arg's delimiter and drops the numeric regex", () => {
  const opt = renderOption('ports', arg({ type: 'int', multiple: true, delimiter: ';', default: [80, 443] }));
  expect(opt.delimiter).toBe(';');
  expect(opt.value).toBe('80;443');
  expect(opt.regex).toBeUndefined();
  expect(renderOption('a', arg({ type: 'str', multiple: true, default: 'one' })).value).toBe('one');
  expect(renderOption('a', arg({ type: 'str', multiple: true, default: [] })).value).toBeUndefined();
  expect(renderOption('a', arg({ type: 'str', multiple: true, delimiter: '\n' })).delimiter).toBe(',');
  expect(renderOption('t', arg({ type: 'password', multiple: true })).multivalued).toBeUndefined();
  expect(renderOption('r', arg({ type: 'rest', multiple: true })).multivalued).toBeUndefined();
});

test('multiple arg default value containing the delimiter is refused', () => {
  expect(() => renderOption('a', arg({ type: 'str', multiple: true, default: ['x,y', 'z'] }))).toThrow(/delimiter/);
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
    expect(s).toContain(`IFS= read -r RUNSPEC_DEPLOY_ARG_${v} <<'RUNSPEC_VALUE'\nx@option.${name}@\nRUNSPEC_VALUE\n`);
    expect(s).toContain(`RUNSPEC_DEPLOY_ARG_${v}=\${RUNSPEC_DEPLOY_ARG_${v}#x}`);
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
  expect(s).toContain('rs_user=svc\n');
  expect(s).toContain('rs_frame RUNSPEC_DEPLOY_ARG_TOKEN | sudo -n -u "$rs_user" /opt/app/.venv/bin/python -c "$RS_PRELUDE" /opt/app/.venv/bin/deploy start');
  // The only export is the no-sudo branch taken when the Rundeck user already is the account.
  expect(s.split('export').length - 1).toBe(1);
  expect(s).toContain('if [ "$rs_user" = "$(id -un)" ]; then');
});

test('run_as without a venv uses the shell prelude', () => {
  const s = renderScript('deploy', [], { x: arg({ type: 'str' }) }, 'deploy', 'svc');
  expect(s).toContain('| sudo -n -u "$rs_user" sh -c "$RS_PRELUDE" sh deploy');
  expect(s).not.toContain('command -v');
});

test('run_as with no args just execs sudo', () => {
  const s = renderScript('deploy', [], {}, '/opt/v/bin/deploy', 'svc', '/opt/v');
  expect(s.trim().endsWith('exec sudo -n -u "$rs_user" /opt/v/bin/deploy')).toBe(true);
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

function showBinary(dir: string): string {
  fs.mkdirSync(path.join(dir, 'bin'), { recursive: true });
  const show = path.join(dir, 'bin', 'show');
  fs.writeFileSync(
    show,
    "#!/usr/bin/env python3\nimport json, os, sys\nprint(json.dumps([{k: v for k, v in os.environ.items() if k.startswith('RUNSPEC_SHOW_')}, sys.argv[1:]]))\n",
  );
  fs.chmodSync(show, 0o755);
  return show;
}

// Substitute tokens the way pre-5.20 Rundeck does (raw text, no escaping) and
// run the script from a file with /bin/bash, like the job's scriptInterpreter.
function runSubstituted(dir: string, script: string, values: Record<string, string>, env?: NodeJS.ProcessEnv) {
  const file = path.join(dir, 'rd-script.sh');
  fs.writeFileSync(file, script.replace(/@option\.([\w-]+)@/g, (_m, n: string) => values[n]));
  return spawnSync('bash', [file], { cwd: dir, encoding: 'utf8', env: env ?? process.env });
}

bashIt('multi-valued option values reach runspec newline-delimited', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const args = {
    states: arg({ type: 'choice', options: ['Missing', 'Stale'], multiple: true, default: ['Missing', 'Stale'] }),
    paths: arg({ type: 'str', multiple: true, delimiter: '/' }),
    globs: arg({ type: 'str', multiple: true, delimiter: '*' }),
    one: arg({ type: 'str' }),
  };
  const values = { states: String(renderOption('states', args.states).value), paths: "a/b'c/$(touch pwned)", globs: 'x*y', one: 'a,b' };
  for (const venv of [null, dir]) {
    const out = runSubstituted(dir, renderScript('show', [], args, show, null, venv), values);
    expect(out.status).toBe(0);
    const env = JSON.parse(out.stdout)[0];
    expect(env.RUNSPEC_SHOW_ARG_STATES).toBe('Missing\nStale');
    expect(env.RUNSPEC_SHOW_ARG_PATHS).toBe("a\nb'c\n$(touch pwned)");
    expect(env.RUNSPEC_SHOW_ARG_GLOBS).toBe('x\ny');
    expect(env.RUNSPEC_SHOW_ARG_ONE).toBe('a,b');
  }
  expect(fs.existsSync(path.join(dir, 'pwned'))).toBe(false);
});

bashIt('raw-substituted values reach the runnable env as data', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const values: Record<string, string> = {
    plain: '3',
    tricky: "a b'c\"$(touch pwned) `touch pwned`; touch pwned \\ ü  ",
    delim: 'RUNSPEC_VALUE',
    unset: '',
  };
  const args = Object.fromEntries(Object.keys(values).map((k) => [k, arg({ type: 'str' })]));
  for (const venv of [null, dir]) {
    const out = runSubstituted(dir, renderScript('show', ['sub'], args, show, null, venv), values);
    expect(out.status).toBe(0);
    const [env, argv] = JSON.parse(out.stdout);
    expect(env).toEqual({ RUNSPEC_SHOW_ARG_PLAIN: '3', RUNSPEC_SHOW_ARG_TRICKY: values.tricky, RUNSPEC_SHOW_ARG_DELIM: 'RUNSPEC_VALUE' });
    expect(argv).toEqual(['sub']);
  }
  expect(fs.existsSync(path.join(dir, 'pwned'))).toBe(false);
});

bashIt('a value with a line break is refused before anything runs', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const args = { a: arg({ type: 'str' }), b: arg({ type: 'password' }) };
  for (const attack of ['a\nRUNSPEC_VALUE\ntouch pwned\n', 'a\nRUNSPEC_VALUE\ntouch pwned', '\ntouch pwned', 'a\r\ntouch pwned']) {
    for (const runAs of [null, 'svc']) {
      const out = runSubstituted(dir, renderScript('show', [], args, show, runAs), { a: 'ok', b: attack });
      expect(out.status).toBe(64);
      expect(out.stderr).toContain('line break');
      expect(out.stdout).toBe('');
    }
  }
  expect(fs.existsSync(path.join(dir, 'pwned'))).toBe(false);
});

bashIt('the guard ignores blank lines', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const out = runSubstituted(dir, renderScript('show', [], { a: arg({ type: 'str' }) }, show) + '\n\n', { a: '\n' });
  expect(out.status).toBe(0);
  expect(JSON.parse(out.stdout)[0]).toEqual({});
});

bashIt('the script makes its own file private first', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const script = renderScript('show', [], { a: arg({ type: 'str' }) }, show);
  expect(script.split('\n')[1].startsWith('chmod 700 -- "${BASH_SOURCE[0]:-$0}"')).toBe(true);
  const out = runSubstituted(dir, script, { a: 'secret' });
  expect(out.status).toBe(0);
  expect(fs.statSync(path.join(dir, 'rd-script.sh')).mode & 0o777).toBe(0o700);
});

bashIt('a runnable missing from the venv stops with a clear message', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  fs.mkdirSync(path.join(dir, 'bin'));
  const out = runSubstituted(dir, renderScript('app', [], { a: arg({ type: 'str' }) }, path.join(dir, 'bin', 'app'), null, dir), { a: 'x' });
  expect(out.status).toBe(127);
  expect(out.stderr).toContain(`runspec: app not found at ${dir}/bin/app (checked as `);
  expect(out.stdout).toBe('');
});

bashIt('a runnable missing from PATH stops with a clear message', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const out = runSubstituted(dir, renderScript('rs-no-such-runnable', [], {}, 'rs-no-such-runnable'), {}, { ...process.env, PATH: '/usr/bin:/bin' });
  expect(out.status).toBe(127);
  expect(out.stderr).toContain('runspec: rs-no-such-runnable not found on PATH (checked as ');
});

bashIt('@owner runs directly when already the venv owner', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  const env = { ...process.env, PATH: `${dir}/bin:/usr/bin:/bin` };
  for (const venv of [dir, null]) {
    const script = renderScript('show', ['sub'], { a: arg({ type: 'str' }) }, venv ? show : 'show', '@owner', venv);
    const out = runSubstituted(dir, script, { a: 'v' }, env);
    expect(out.status).toBe(0);
    expect(JSON.parse(out.stdout)).toEqual([{ RUNSPEC_SHOW_ARG_A: 'v' }, ['sub']]);
  }
});

bashIt('@owner sudoes to the venv owner with values on stdin', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rd-'));
  const show = showBinary(dir);
  fs.symlinkSync(execFileSync('bash', ['-c', 'command -v python3']).toString().trim(), path.join(dir, 'bin', 'python'));
  const stubs = path.join(dir, 'stubs');
  fs.mkdirSync(stubs);
  fs.writeFileSync(path.join(stubs, 'stat'), '#!/bin/sh\ncase "$2" in %U) echo svc-owner;; *) echo 4242;; esac\n', { mode: 0o755 });
  fs.writeFileSync(path.join(stubs, 'sudo'), '#!/bin/sh\necho "$*" > "$SUDO_LOG"\nshift 3\nexec "$@"\n', { mode: 0o755 });
  const log = path.join(dir, 'sudo.log');
  const env = { ...process.env, PATH: `${stubs}:${dir}/bin:/usr/bin:/bin`, SUDO_LOG: log };
  const args = { a: arg({ type: 'str' }), b: arg({ type: 'password' }) };
  for (const venv of [dir, null]) {
    const out = runSubstituted(dir, renderScript('show', ['sub'], args, venv ? show : 'show', '@owner', venv), { a: 'v', b: 's3cret' }, env);
    expect(out.status).toBe(0);
    expect(JSON.parse(out.stdout)).toEqual([{ RUNSPEC_SHOW_ARG_A: 'v', RUNSPEC_SHOW_ARG_B: 's3cret' }, ['sub']]);
    const sudoArgv = fs.readFileSync(log, 'utf8');
    expect(sudoArgv.startsWith('-n -u svc-owner ')).toBe(true);
    expect(sudoArgv).not.toContain('s3cret');
    expect(sudoArgv).toContain(show);
  }
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

test('mergeSettings run_as precedence and forms a job cannot resolve', () => {
  expect(mergeSettings('app', { runAs: 'cli' }, { run_as: 'cfg' }, { run_as: 'run' }, 'own').runAs).toBe('cli');
  expect(mergeSettings('app', {}, { run_as: 'cfg' }, { run_as: 'run' }, 'own').runAs).toBe('run');
  expect(mergeSettings('app', {}, { run_as: 'cfg' }, {}, 'own').runAs).toBe('cfg');
  expect(mergeSettings('app', {}, {}, {}, 'own').runAs).toBe('own');
  for (const v of ['$SVC', { 'web-*': 'svc' }, '', '@other']) {
    expect(mergeSettings('app', {}, {}, {}, v).runAs).toBeUndefined();
  }
});

test('mergeSettings run_as: @owner, and unset defaults to @owner', () => {
  expect(mergeSettings('app', {}, {}, {}, '@owner').runAs).toBe('@owner');
  expect(mergeSettings('app', {}, {}, {}, undefined).runAs).toBe('@owner');
  expect(mergeSettings('app', {}, {}, {}).runAs).toBe('@owner');
  expect(mergeSettings('app', { runAs: '' }, {}, {}, 'own').runAs).toBeUndefined();
  expect(mergeSettings('app', {}, {}, { run_as: '' }, undefined).runAs).toBeUndefined();
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
