import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse, loadSpec } from '../src/parser';
import { effectiveRunAs, environmentOwner, environmentRoot, resolveAccount, resolveRunAs, verifyRunAsIdentity } from '../src/become';

function makeTmpConfig(toml: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-run-as-test-'));
  const configPath = path.join(dir, 'runspec.toml');
  fs.writeFileSync(configPath, toml);
  return configPath;
}

// POSIX-only: the uid comparison needs process.geteuid + `id`.
const posixIt = process.geteuid ? test : test.skip;

// ── resolveRunAs ──────────────────────────────────────────────────────────────

describe('resolveRunAs', () => {
  test('null/undefined/empty → no escalation', () => {
    expect(resolveRunAs(undefined, 'host1')).toBe('');
    expect(resolveRunAs(null, 'host1')).toBe('');
    expect(resolveRunAs('', 'host1')).toBe('');
  });

  test('simple string', () => {
    expect(resolveRunAs('oracle', 'anything')).toBe('oracle');
  });

  test('$ENV reference', () => {
    process.env['ORACLE_RUN_AS'] = 'orasvc';
    expect(resolveRunAs('$ORACLE_RUN_AS', 'h')).toBe('orasvc');
    delete process.env['ORACLE_RUN_AS'];
    expect(resolveRunAs('$ORACLE_RUN_AS', 'h')).toBe('');
  });

  test('per-host exact match, then default', () => {
    const spec = { default: 'oracle', hosts: { 'box-01': 'dba', 'box-02': '' } };
    expect(resolveRunAs(spec, 'box-01')).toBe('dba');
    expect(resolveRunAs(spec, 'box-02')).toBe(''); // explicit no-escalation
    expect(resolveRunAs(spec, 'other')).toBe('oracle');
  });

  test('patterns, full-match, first wins', () => {
    const spec = { default: 'oracle', patterns: { '[lg]pexp[0-9]*': 'orasvc', 'prod[0-9]*': 'produser' } };
    expect(resolveRunAs(spec, 'lpexp01')).toBe('orasvc');
    expect(resolveRunAs(spec, 'prod7')).toBe('produser');
    expect(resolveRunAs(spec, 'lpexp')).toBe('orasvc');
    expect(resolveRunAs(spec, 'xlpexp01')).toBe('oracle'); // not a full match
  });
});

// ── verifyRunAsIdentity ───────────────────────────────────────────────────────

describe('verifyRunAsIdentity', () => {
  test('empty target is a no-op', () => {
    expect(verifyRunAsIdentity('')).toBeNull();
  });

  posixIt('current uid (by number) matches', () => {
    expect(verifyRunAsIdentity(String(process.geteuid!()))).toBeNull();
  });

  posixIt('nonexistent user is reported', () => {
    const detail = verifyRunAsIdentity('nope-user-xyz');
    expect(detail).toContain('does not exist');
  });

  posixIt('uid mismatch is reported', () => {
    const realUid = process.geteuid!();
    const spy = jest.spyOn(process, 'geteuid').mockReturnValue(999999);
    try {
      const detail = verifyRunAsIdentity(String(realUid));
      expect(detail).toContain('999999');
      expect(detail).toContain(`uid ${realUid}`);
    } finally {
      spy.mockRestore();
    }
  });

  test('no POSIX uid model (Windows) is a no-op', () => {
    const orig = process.geteuid;
    (process as { geteuid?: () => number }).geteuid = undefined;
    try {
      expect(verifyRunAsIdentity('root')).toBeNull();
    } finally {
      (process as { geteuid?: () => number }).geteuid = orig;
    }
  });
});

// ── enforce_run_as wired into the parser ──────────────────────────────────────

function body(opts: { enforce?: string; configEnforce?: string } = {}): string {
  return [
    '[config]',
    opts.configEnforce ?? '',
    '',
    '[tool]',
    'description = "x"',
    'run_as = "nope-user-xyz"',
    opts.enforce ?? '',
    '',
  ].join('\n');
}

function doParse(opts: { enforce?: string; configEnforce?: string } = {}) {
  return parse({ scriptName: 'tool', argv: [], configPath: makeTmpConfig(body(opts)) });
}

describe('enforce_run_as gate', () => {
  posixIt('error is the default', () => {
    expect(() => doParse()).toThrow(/Refusing to run 'tool'/);
  });

  posixIt('explicit error throws', () => {
    expect(() => doParse({ enforce: 'enforce_run_as = "error"' })).toThrow(/Refusing to run/);
  });

  posixIt('warn continues and writes stderr', () => {
    const spy = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
    try {
      const args = doParse({ enforce: 'enforce_run_as = "warn"' });
      expect(args.__runspec_script__).toBe('tool');
      const written = spy.mock.calls.map((c) => String(c[0])).join('');
      expect(written).toContain('⚠');
      expect(written).toContain('nope-user-xyz');
    } finally {
      spy.mockRestore();
    }
  });

  posixIt('off is silent', () => {
    const spy = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
    try {
      const args = doParse({ enforce: 'enforce_run_as = "off"' });
      expect(args.__runspec_script__).toBe('tool');
      expect(spy.mock.calls.length).toBe(0);
    } finally {
      spy.mockRestore();
    }
  });

  posixIt('config default applies when runnable omits enforce_run_as', () => {
    const spy = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
    try {
      doParse({ configEnforce: 'enforce_run_as = "warn"' });
      const written = spy.mock.calls.map((c) => String(c[0])).join('');
      expect(written).toContain('⚠');
    } finally {
      spy.mockRestore();
    }
  });

  posixIt('runnable value overrides a permissive config default', () => {
    expect(() =>
      doParse({ enforce: 'enforce_run_as = "error"', configEnforce: 'enforce_run_as = "off"' }),
    ).toThrow(/Refusing to run/);
  });

  test('no run_as passes for the environment owner', () => {
    // No run_as → "@owner"; the test process owns (or is root over) the tmp dir.
    const args = parse({ scriptName: 'tool', argv: [], configPath: makeTmpConfig('[tool]\ndescription = "x"\n') });
    expect(args.__runspec_script__).toBe('tool');
  });

  posixIt('loadSpec skips the check', () => {
    const args = loadSpec({ scriptName: 'tool', configPath: makeTmpConfig(body({ enforce: 'enforce_run_as = "error"' })) });
    expect(args.__runspec_script__).toBe('tool');
  });
});

// ── the "@owner" default ──────────────────────────────────────────────────────

describe('@owner', () => {
  test('effectiveRunAs order', () => {
    expect(effectiveRunAs('svc', 'other')).toBe('svc');
    expect(effectiveRunAs(undefined, 'other')).toBe('other');
    expect(effectiveRunAs(undefined, undefined)).toBe('@owner');
    expect(effectiveRunAs('', 'other')).toBe('');
    expect(effectiveRunAs(undefined, '')).toBe('');
  });

  posixIt('environmentOwner never resolves to root', () => {
    expect(environmentOwner('/')).toBe('');
    expect(environmentOwner('/nope/not/here')).toBe('');
  });

  test('environmentRoot is the nearest package.json dir', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-envroot-'));
    fs.writeFileSync(path.join(dir, 'package.json'), '{}');
    fs.mkdirSync(path.join(dir, 'pkg'));
    expect(environmentRoot(path.join(dir, 'pkg', 'runspec.toml'))).toBe(dir);
  });

  test('resolveAccount only touches @owner', () => {
    expect(resolveAccount('alice', '/')).toBe('alice');
    expect(resolveAccount('', '/')).toBe('');
  });

  // Needs root to hand the project dir to another uid.
  const rootIt = process.geteuid && process.geteuid() === 0 ? test : test.skip;

  function foreignOwnedConfig(toml: string): string {
    const cfg = makeTmpConfig(toml);
    fs.writeFileSync(path.join(path.dirname(cfg), 'package.json'), '{}');
    fs.chownSync(path.dirname(cfg), 424242, 424242);
    return cfg;
  }

  rootIt('default refuses anyone but the owner', () => {
    const cfg = foreignOwnedConfig('[tool]\ndescription = "x"\n');
    expect(() => parse({ scriptName: 'tool', argv: [], configPath: cfg })).toThrow(/owns its environment/);
  });

  rootIt('[config] run_as = "" opts the package out', () => {
    const cfg = foreignOwnedConfig('[config]\nrun_as = ""\n\n[tool]\ndescription = "x"\n');
    expect(parse({ scriptName: 'tool', argv: [], configPath: cfg }).__runspec_script__).toBe('tool');
  });

  posixIt('[config] run_as is the package default', () => {
    const cfg = makeTmpConfig('[config]\nrun_as = "nope-user-xyz"\n\n[tool]\ndescription = "x"\n');
    expect(() => parse({ scriptName: 'tool', argv: [], configPath: cfg })).toThrow(/Refusing to run/);
  });
});
