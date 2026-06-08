import { ParseHarness, harness } from '../src/testing';

// A spec with a required subcommand, a leaf with a required arg + a flag, and a
// leaf with only optional args — enough to exercise the whole smoke() walk.
const SPEC = `
[config]
autonomy-default = "autonomous"

[tool]
description = "A tool with subcommands"
require-command = true

[tool.commands.create]
description = "Create a thing"
[tool.commands.create.args]
name = {type = "str", required = true}
force = {type = "flag", default = false}

[tool.commands.show]
description = "Show a thing"
[tool.commands.show.args]
id = {type = "str", required = false, default = "x"}
`;

function h(): ParseHarness {
  return new ParseHarness({ scriptName: 'tool', toml: SPEC });
}

// ── core parse entry points ────────────────────────────────────────────────

test('expectExit(--help) returns 0 WITHOUT killing the process', () => {
  const harn = h();
  // If process.exit weren't intercepted, this would terminate the jest worker.
  const out = harn.expectExit(['create', '--help'], { code: 0 });
  expect(out).toContain('Create a thing');
  harn.close();
});

test('require-command: empty argv exits non-zero (RunSpecError mapped to code 1)', () => {
  const harn = h();
  harn.expectExit([]); // default code = 1
  harn.close();
});

test('missing required arg exits 1 and names the flag', () => {
  const harn = h();
  const out = harn.expectExit(['create'], { code: 1, contains: 'name' });
  expect(out).toContain('name');
  harn.close();
});

test('expectOk returns the parsed args with coercion applied', () => {
  const harn = h();
  const parsed = harn.expectOk(['create', '--name', 'widget', '--force']);
  expect(String(parsed.name)).toBe('widget');
  expect(parsed.force).toBe(true);
  harn.close();
});

// ── smoke() auto-coverage ───────────────────────────────────────────────────

test('smoke walks the whole tree and reports the checks performed', () => {
  const harn = h();
  const done = harn.smoke({ values: { name: 'widget' } });
  expect(done).toContain('--help');
  expect(done).toContain('(no command) -> require-command');
  expect(done).toContain('create --help');
  expect(done).toContain('create -> missing required');
  expect(done).toContain('create (valid)');
  expect(done).toContain('show (valid)');
  harn.close();
});

test('smoke without values still does the structural checks', () => {
  const harn = h();
  const done = harn.smoke();
  expect(done).toContain('--help');
  expect(done).toContain('create -> missing required');
  // show has no required args, so it still gets a (valid) check with empty argv
  expect(done).toContain('show (valid)');
  harn.close();
});

// ── introspection ───────────────────────────────────────────────────────────

test('command/leaf paths and arg introspection', () => {
  const harn = h();
  expect(harn.commandPaths()).toEqual([['create'], ['show']]);
  expect(harn.leafCommandPaths()).toEqual([['create'], ['show']]);
  expect(harn.requiredArgs(['create'])).toEqual(['name']);
  expect(harn.requiredArgs(['show'])).toEqual([]);
  expect(harn.requiresCommand()).toBe(true);
  expect(Object.keys(harn.argsFor(['create']))).toEqual(['name', 'force']);
  harn.close();
});

// ── lifecycle / API guards ──────────────────────────────────────────────────

test('factory helper mirrors the constructor', () => {
  const harn = harness('tool', { toml: SPEC });
  expect(harn.scriptName).toBe('tool');
  harn.close();
});

test('rejects passing both config and toml', () => {
  expect(() => new ParseHarness({ scriptName: 'tool', toml: SPEC, configPath: '/x/runspec.toml' })).toThrow(/exactly one/);
});

test('rejects passing neither config nor toml', () => {
  expect(() => new ParseHarness({ scriptName: 'tool' })).toThrow(/exactly one/);
});

test('unknown runnable throws with the available names', () => {
  expect(() => new ParseHarness({ scriptName: 'ghost', toml: SPEC })).toThrow(/ghost/);
});

// ── interception is fully restored ──────────────────────────────────────────

test('process.exit / stdout.write / console.log are restored after a run', () => {
  const harn = h();
  const exitBefore = process.exit;
  const stdoutBefore = process.stdout.write;
  const logBefore = console.log;

  harn.expectExit(['--help'], { code: 0 });
  harn.expectExit(['create']); // also exercise the throw path

  expect(process.exit).toBe(exitBefore);
  expect(process.stdout.write).toBe(stdoutBefore);
  expect(console.log).toBe(logBefore);
  harn.close();
});
