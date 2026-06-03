import { coerce, registerType, listTypes } from '../src/types';
import type { ArgSpec } from '../src/models';

function spec(overrides: Partial<ArgSpec> = {}): ArgSpec {
  return { name: 'test', ...overrides } as ArgSpec;
}

// ── str ───────────────────────────────────────────────────────────────────────

test('coerces string', () => {
  expect(coerce('hello', spec({ type: 'str' }))).toBe('hello');
});

test('coerces number to string', () => {
  expect(coerce(42, spec({ type: 'str' }))).toBe('42');
});

// ── str validation: pattern / min-length / max-length (str only) ───────────────

test('str matches pattern', () => {
  expect(coerce('PROJ-123', spec({ type: 'str', pattern: '[A-Z]+-[0-9]+' }))).toBe('PROJ-123');
});

test('str violates pattern', () => {
  expect(() => coerce('proj-123', spec({ type: 'str', pattern: '[A-Z]+-[0-9]+' }))).toThrow();
});

test('pattern is anchored (fullmatch, not substring)', () => {
  expect(() => coerce('PROJ-123-extra', spec({ type: 'str', pattern: '[A-Z]+-[0-9]+' }))).toThrow();
});

test('anchored pattern wraps top-level alternation correctly', () => {
  // ^(?:a|b)$ accepts only "a" or "b" exactly, not a string ending in "b".
  expect(coerce('a', spec({ type: 'str', pattern: 'a|b' }))).toBe('a');
  expect(() => coerce('xb', spec({ type: 'str', pattern: 'a|b' }))).toThrow();
});

test('str respects minLength', () => {
  expect(() => coerce('ab', spec({ type: 'str', minLength: 3 }))).toThrow();
});

test('str respects maxLength', () => {
  expect(() => coerce('abcdef', spec({ type: 'str', maxLength: 5 }))).toThrow();
});

test('str within length bounds passes', () => {
  expect(coerce('hello', spec({ type: 'str', minLength: 3, maxLength: 10 }))).toBe('hello');
});

// ── int ───────────────────────────────────────────────────────────────────────

test('coerces integer string', () => {
  expect(coerce('42', spec({ type: 'int' }))).toBe(42);
});

test('coerces number to int', () => {
  expect(coerce(10, spec({ type: 'int' }))).toBe(10);
});

test('rejects float string as int', () => {
  expect(() => coerce('3.14', spec({ type: 'int' }))).toThrow();
});

test('int respects range', () => {
  expect(() => coerce('50', spec({ type: 'int', range: [1, 32] }))).toThrow();
});

test('int within range passes', () => {
  expect(coerce('4', spec({ type: 'int', range: [1, 32] }))).toBe(4);
});

// ── float ─────────────────────────────────────────────────────────────────────

test('coerces float string', () => {
  expect(coerce('3.14', spec({ type: 'float' }))).toBeCloseTo(3.14);
});

test('rejects invalid float', () => {
  expect(() => coerce('abc', spec({ type: 'float' }))).toThrow();
});

// ── bool ─────────────────────────────────────────────────────────────────────

test('coerces true string', () => {
  expect(coerce('true', spec({ type: 'bool' }))).toBe(true);
});

test('coerces yes string', () => {
  expect(coerce('yes', spec({ type: 'bool' }))).toBe(true);
});

test('coerces false string', () => {
  expect(coerce('false', spec({ type: 'bool' }))).toBe(false);
});

test('coerces boolean directly', () => {
  expect(coerce(true, spec({ type: 'bool' }))).toBe(true);
});

test('rejects invalid bool', () => {
  expect(() => coerce('maybe', spec({ type: 'bool' }))).toThrow();
});

// ── flag ─────────────────────────────────────────────────────────────────────

test('flag true when boolean true', () => {
  expect(coerce(true, spec({ type: 'flag' }))).toBe(true);
});

test('flag false when boolean false', () => {
  expect(coerce(false, spec({ type: 'flag' }))).toBe(false);
});

// ── path ─────────────────────────────────────────────────────────────────────

test('coerces path to absolute', () => {
  const result = coerce('/tmp/data', spec({ type: 'path' })) as string;
  expect(result).toBe('/tmp/data');
  expect(result.startsWith('/')).toBe(true);
});

// ── choice ───────────────────────────────────────────────────────────────────

test('accepts valid choice', () => {
  expect(coerce('json', spec({ type: 'choice', options: ['json', 'csv'] }))).toBe('json');
});

test('rejects invalid choice', () => {
  expect(() => coerce('xml', spec({ type: 'choice', options: ['json', 'csv'] }))).toThrow();
});

// ── multiple (per-item coercion) ───────────────────────────────────────────────

test('multiple returns a list of coerced items', () => {
  expect(coerce(['a', 'b', 'c'], spec({ type: 'str', multiple: true }))).toEqual(['a', 'b', 'c']);
});

test('multiple int coerces each item', () => {
  expect(coerce(['1', '2', '3'], spec({ type: 'int', multiple: true }))).toEqual([1, 2, 3]);
});

test('multiple applies pattern per item', () => {
  const s = spec({ type: 'str', multiple: true, pattern: '[a-z]+' });
  expect(coerce(['ab', 'cd'], s)).toEqual(['ab', 'cd']);
});

test('multiple wraps a scalar value into a one-item list', () => {
  expect(coerce('solo', spec({ type: 'str', multiple: true }))).toEqual(['solo']);
});

test('multiple collects all failing items with index and value', () => {
  const s = spec({ name: 'tag', type: 'str', multiple: true, pattern: '[a-z]+' });
  let msg = '';
  try {
    coerce(['ab', 'XY', 'z9', 'de'], s);
  } catch (e) {
    msg = (e as Error).message;
  }
  expect(msg).toContain('2 of 4 item(s) failed');
  expect(msg).toContain('item 2 ("XY")');
  expect(msg).toContain('item 3 ("z9")');
});

test('multiple range applies per item', () => {
  const s = spec({ name: 'n', type: 'int', multiple: true, range: [1, 10] });
  expect(() => coerce(['5', '99'], s)).toThrow(/item 2 \("99"\)/);
});

test('rest type is not treated as per-item', () => {
  expect(coerce(['--flag', 'value'], spec({ type: 'rest' }))).toEqual(['--flag', 'value']);
});

// ── custom types ──────────────────────────────────────────────────────────────

test('registerType adds custom coercer', () => {
  registerType('upper', (v) => String(v).toUpperCase());
  expect(coerce('hello', spec({ type: 'upper' }))).toBe('HELLO');
  expect(listTypes()).toContain('upper');
});

test('unknown type throws', () => {
  expect(() => coerce('x', spec({ type: 'nonexistent-xyz' }))).toThrow();
});
