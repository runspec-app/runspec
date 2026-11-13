import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  GENESIS,
  AuditError,
  acquireLock,
  audit,
  readLedger,
  setRunContext,
  verifyLedger,
} from '../src/auditlog';
import { canonicalJson, codePointCompare, pyDumps, pyFloatRepr, pyParse } from '../src/pyjson';
import { parse } from '../src/parser';
import { _resetForTest, AUDIT_LOGGER } from '../src/logging_setup';

const FIXTURES = path.resolve(__dirname, '../../../tests/integration/fixtures/audit');

function tmp(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-audit-'));
}

beforeEach(() => {
  process.env.RUNSPEC_AUDIT = 'on';
  setRunContext(null);
});
afterAll(() => {
  process.env.RUNSPEC_AUDIT = 'off';
  setRunContext(null);
});

// ── canonical JSON (Python's json.dumps) ─────────────────────────────────────

test('pyFloatRepr matches Python repr(float)', () => {
  const cases: Array<[number, string]> = [
    [1, '1.0'], [0.1, '0.1'], [1e-5, '1e-05'], [0.0001, '0.0001'], [1e16, '1e+16'], [1e15, '1000000000000000.0'],
    [2.5e-7, '2.5e-07'], [-0, '-0.0'], [5e-324, '5e-324'], [1.5e300, '1.5e+300'], [123.456, '123.456'], [NaN, 'NaN'],
  ];
  for (const [x, want] of cases) expect(pyFloatRepr(x)).toBe(want);
});

test('pyDumps escapes like Python with ensure_ascii=False', () => {
  expect(pyDumps('a"b\\c\n\t\x01 é😀/')).toBe('"a\\"b\\\\c\\n\\t\\u0001 é😀/"');
  expect(pyDumps({ b: 1, a: [true, null, 0.5] })).toBe('{"b": 1, "a": [true, null, 0.5]}');
  expect(canonicalJson({ b: 1, a: 2 })).toBe('{"a":2,"b":1}');
});

test('keys sort by code point, as Python does', () => {
  expect(['\u{1F600}', 'ﬀ', 'z'].sort(codePointCompare)).toEqual(['z', 'ﬀ', '\u{1F600}']);
});

test('pyParse keeps int and float apart', () => {
  expect(canonicalJson(pyParse('{"a": 1.0, "b": 1, "c": 1e-05, "d": 12345678901234567890, "e": NaN}'))).toBe(
    '{"a":1.0,"b":1,"c":1e-05,"d":12345678901234567890,"e":NaN}',
  );
});

// ── the ledger ───────────────────────────────────────────────────────────────

test('audit() appends a hash-chained ledger that verifies', () => {
  const dir = tmp();
  const first = audit('payment.refund', 'succeeded', { target: { order: 'A-1' }, ledgerDir: dir, syslog: false })!;
  const second = audit('payment.refund', 'failed', { detail: { amount: 12.5 }, ledgerDir: dir, syslog: false })!;
  expect(first.prev).toBe(GENESIS);
  expect(second.prev).toBe(first.hash);
  const file = path.join(dir, 'audit.jsonl');
  expect(verifyLedger(file)).toMatchObject({ ok: true, records: 2, last_hash: second.hash });
  expect(readLedger(file).map((r) => r.outcome)).toEqual(['succeeded', 'failed']);
});

test('audit() detail with a circular object, a Set or a Map still records', () => {
  const dir = tmp();
  const loop: Record<string, unknown> = { a: 1 };
  loop['self'] = loop;
  const rec = audit('x.y', 'ok', { detail: { loop, tags: new Set(['a']), byType: new Map([['k', 1]]) }, ledgerDir: dir, syslog: false })!;
  expect(rec.detail).toEqual({ loop: { a: 1, self: '<circular>' }, tags: ['a'], byType: { k: 1 } });
  expect(verifyLedger(path.join(dir, 'audit.jsonl')).ok).toBe(true);
});

test('an edited, removed or garbled line is reported with its line number', () => {
  const dir = tmp();
  for (const o of ['a', 'b', 'c']) audit('x.y', o, { ledgerDir: dir, syslog: false });
  const file = path.join(dir, 'audit.jsonl');
  const lines = fs.readFileSync(file, 'utf8').split('\n');

  fs.writeFileSync(file, [lines[0], lines[1].replace('"b"', '"z"'), lines[2], ''].join('\n'));
  expect(verifyLedger(file)).toMatchObject({ ok: false, first_bad: 2, reason: expect.stringContaining('edited') });

  fs.writeFileSync(file, [lines[0], lines[2], ''].join('\n'));
  expect(verifyLedger(file)).toMatchObject({ ok: false, first_bad: 2, reason: expect.stringContaining('chain broken') });

  fs.writeFileSync(file, [lines[0], 'not json', ''].join('\n'));
  expect(verifyLedger(file)).toMatchObject({ ok: false, first_bad: 2, reason: 'not a JSON record' });
});

test('a missing ledger verifies as empty', () => {
  expect(verifyLedger(path.join(tmp(), 'audit.jsonl'))).toMatchObject({ ok: true, records: 0 });
});

test('secret-looking keys and values are redacted', () => {
  const rec = audit('creds.used', '', { detail: { password: 'hunter2', note: 'token=abc123', nested: { api_key: 'k' } }, ledgerDir: tmp(), syslog: false })!;
  expect(rec.detail).toEqual({ password: '[REDACTED]', note: 'token=[REDACTED]', nested: { api_key: '[REDACTED]' } });
});

test('invalid event or outcome throws TypeError', () => {
  expect(() => audit('Bad Name')).toThrow(TypeError);
  expect(() => audit('ok.name', 'Not A Word')).toThrow(TypeError);
});

test('RUNSPEC_AUDIT=off records nothing', () => {
  process.env.RUNSPEC_AUDIT = 'off';
  const dir = tmp();
  expect(audit('x.y', '', { ledgerDir: dir, syslog: false })).toBeNull();
  expect(fs.existsSync(path.join(dir, 'audit.jsonl'))).toBe(false);
});

test('strict throws AuditError when nothing took the event', () => {
  const file = path.join(tmp(), 'plain-file');
  fs.writeFileSync(file, '');
  expect(() => audit('x.y', '', { ledgerDir: path.join(file, 'sub'), syslog: false, strict: true })).toThrow(AuditError);
  expect(audit('x.y', '', { ledgerDir: path.join(file, 'sub'), syslog: false })).not.toBeNull();
});

// ── cross-language ledgers ───────────────────────────────────────────────────

test.each(['python.jsonl', 'node.jsonl'])('the %s fixture verifies', (name) => {
  expect(verifyLedger(path.join(FIXTURES, name))).toMatchObject({ ok: true, records: 4 });
});

test.each(['python.jsonl', 'node.jsonl'])('an edit to the %s fixture is caught', (name) => {
  const lines = fs.readFileSync(path.join(FIXTURES, name), 'utf8').split('\n');
  lines[2] = lines[2].replace('"checked"', '"changed"');
  const file = path.join(tmp(), 'audit.jsonl');
  fs.writeFileSync(file, lines.join('\n'));
  expect(verifyLedger(file)).toMatchObject({ ok: false, first_bad: 3 });
});

test('Node extends a ledger Python wrote', () => {
  const dir = tmp();
  fs.copyFileSync(path.join(FIXTURES, 'python.jsonl'), path.join(dir, 'audit.jsonl'));
  audit('fixture.append', 'succeeded', { detail: { n: 0.5 }, ledgerDir: dir, syslog: false });
  expect(verifyLedger(path.join(dir, 'audit.jsonl'))).toMatchObject({ ok: true, records: 5 });
});

// ── lock ─────────────────────────────────────────────────────────────────────

test('a lock left by a dead process is taken over; a live one makes us wait', () => {
  const lock = path.join(tmp(), 'audit.jsonl.lock');
  fs.writeFileSync(lock, `999999999 ${os.hostname()}\n`);
  const release = acquireLock(lock, 200);
  expect(fs.readFileSync(lock, 'utf8')).toContain(String(process.pid));
  expect(() => acquireLock(lock, 100)).toThrow(/held by another writer/);
  release();
  expect(fs.existsSync(lock)).toBe(false);
});

// ── inside a run ─────────────────────────────────────────────────────────────

test('inside parse() the event names the run and reaches the project ledger and run log', () => {
  const root = tmp();
  fs.writeFileSync(path.join(root, 'package.json'), '{"name":"t","version":"1.0.0"}');
  fs.writeFileSync(path.join(root, 'runspec.toml'), '[config.logging]\n[greet]\nrun_as = ""\n[greet.commands.hello]\n');
  _resetForTest();
  const writes: string[] = [];
  const spy = jest.spyOn(process.stdout, 'write').mockImplementation((c) => {
    writes.push(String(c));
    return true;
  });
  try {
    parse({ scriptName: 'greet', argv: ['hello'], configPath: path.join(root, 'runspec.toml') });
    audit('greeting.sent', 'succeeded', { syslog: false });
  } finally {
    spy.mockRestore();
  }
  const [rec] = readLedger(path.join(root, 'logs', 'audit.jsonl'));
  expect(rec).toMatchObject({ event: 'greeting.sent', runnable: 'greet', command: 'hello', venv: root });
  expect(String(rec.run_id)).toMatch(/[0-9a-f-]{36}/);
  expect(writes.join('')).not.toContain('greeting.sent'); // file-only
  const log = fs.readFileSync(path.join(root, 'logs', 'greet.log'), 'utf8');
  const runRec = log.trim().split('\n').map((l) => JSON.parse(l)).find((o) => o.logger === AUDIT_LOGGER);
  expect(runRec.extra.audit.hash).toBe(rec.hash);
  _resetForTest();
});
