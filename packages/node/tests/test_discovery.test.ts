import { normaliseDiscoverable, validateDiscoverable } from '../src/discovery';

describe('normaliseDiscoverable', () => {
  test('absent defaults to visible', () => {
    expect(normaliseDiscoverable(undefined)).toBe(true);
  });
  test('true is visible', () => {
    expect(normaliseDiscoverable(true)).toBe(true);
  });
  test('false is hidden', () => {
    expect(normaliseDiscoverable(false)).toBe(false);
  });
});

describe('validateDiscoverable', () => {
  test('undefined/true/false ok', () => {
    expect(validateDiscoverable(undefined)).toEqual([]);
    expect(validateDiscoverable(true)).toEqual([]);
    expect(validateDiscoverable(false)).toEqual([]);
  });
  test('list rejected', () => {
    const errs = validateDiscoverable(['operator', 'self-service']);
    expect(errs.length).toBe(1);
    expect(errs[0]).toContain('must be true or false');
  });
  test('wrong type rejected', () => {
    const errs = validateDiscoverable('operator');
    expect(errs.length).toBe(1);
    expect(errs[0]).toContain('must be true or false');
  });
});
