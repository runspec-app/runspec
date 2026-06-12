import { normaliseDiscoverable, validateDiscoverable, isDiscoverableTo } from '../src/discovery';

describe('normaliseDiscoverable', () => {
  test('absent defaults to operator', () => {
    expect(normaliseDiscoverable(undefined)).toEqual(['operator']);
  });
  test('true is operator', () => {
    expect(normaliseDiscoverable(true)).toEqual(['operator']);
  });
  test('false is empty', () => {
    expect(normaliseDiscoverable(false)).toEqual([]);
  });
  test('list passthrough', () => {
    expect(normaliseDiscoverable(['operator', 'self-service'])).toEqual(['operator', 'self-service']);
  });
  test('dedupes preserving order', () => {
    expect(normaliseDiscoverable(['operator', 'operator', 'self-service'])).toEqual(['operator', 'self-service']);
  });
});

describe('isDiscoverableTo', () => {
  test('default reaches operator', () => {
    expect(isDiscoverableTo(undefined, 'operator')).toBe(true);
  });
  test('bare true never implies self-service', () => {
    expect(isDiscoverableTo(true, 'self-service')).toBe(false);
  });
  test('false reaches nobody', () => {
    expect(isDiscoverableTo(false, 'operator')).toBe(false);
    expect(isDiscoverableTo(false, 'self-service')).toBe(false);
  });
  test('explicit self-service', () => {
    expect(isDiscoverableTo(['operator', 'self-service'], 'self-service')).toBe(true);
  });
  test('self-service-only hides from operator', () => {
    expect(isDiscoverableTo(['self-service'], 'operator')).toBe(false);
  });
});

describe('validateDiscoverable', () => {
  test('undefined/true/false ok', () => {
    expect(validateDiscoverable(undefined)).toEqual([]);
    expect(validateDiscoverable(true)).toEqual([]);
    expect(validateDiscoverable(false)).toEqual([]);
  });
  test('known audiences ok', () => {
    expect(validateDiscoverable(['operator', 'self-service'])).toEqual([]);
  });
  test('empty list rejected', () => {
    const errs = validateDiscoverable([]);
    expect(errs.length).toBe(1);
    expect(errs[0]).toContain('write false');
  });
  test('unknown audience rejected', () => {
    const errs = validateDiscoverable(['operator', 'admins']);
    expect(errs.length).toBe(1);
    expect(errs[0]).toContain('unknown audience');
  });
});
