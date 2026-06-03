import * as path from 'path';
import type { ArgSpec } from './models';
import {
  formatInvalidChoice,
  formatInvalidPattern,
  formatTooShort,
  formatTooLong,
  formatInvalidItems,
} from './errors';

export type TypeCoercer = (value: unknown, spec: ArgSpec) => unknown;

const registry = new Map<string, TypeCoercer>();

export function registerType(name: string, coercer: TypeCoercer): void {
  registry.set(name, coercer);
}

export function coerce(value: unknown, spec: ArgSpec): unknown {
  const typeName = spec.type ?? 'str';
  const coercer = registry.get(typeName);
  if (!coercer) {
    throw new TypeError(
      `Unknown type '${typeName}' for argument '${spec.name}'. Registered types: ${[...registry.keys()].sort().join(', ')}\nRegister custom types with registerType().`,
    );
  }
  // Multiple-valued args coerce and validate each item independently and return
  // a list. The arg's type (and its pattern / length / range / choice checks)
  // applies per item. `rest` manages its own list, so it is excluded.
  if (spec.multiple && typeName !== 'rest') {
    const items = Array.isArray(value) ? value : [value];
    const out: unknown[] = [];
    const failures: Array<[number, unknown, string]> = [];
    items.forEach((item, i) => {
      try {
        out.push(coercer(item, spec));
      } catch (e) {
        failures.push([i + 1, item, (e as Error).message]);
      }
    });
    if (failures.length > 0) {
      throw new Error(formatInvalidItems(spec.name ?? '?', items.length, failures));
    }
    return out;
  }
  try {
    return coercer(value, spec);
  } catch (e) {
    throw new Error(
      `Cannot coerce value ${JSON.stringify(value)} to type '${typeName}' for argument '--${spec.name ?? '?'}': ${(e as Error).message}`,
    );
  }
}

export function listTypes(): string[] {
  return [...registry.keys()].sort();
}

function coerceStr(value: unknown, spec: ArgSpec): string {
  const coerced = String(value);
  checkLength(coerced, spec);
  checkPattern(coerced, spec);
  return coerced;
}

function coerceInt(value: unknown, spec: ArgSpec): number {
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n)) {
    throw new Error(`invalid integer: ${JSON.stringify(value)}`);
  }
  checkRange(n, spec);
  return n;
}

function coerceFloat(value: unknown, spec: ArgSpec): number {
  const n = Number(value);
  if (!Number.isFinite(n)) throw new Error(`invalid number: ${JSON.stringify(value)}`);
  checkRange(n, spec);
  return n;
}

function coerceBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    if (['true', '1', 'yes', 'on'].includes(value.toLowerCase())) return true;
    if (['false', '0', 'no', 'off'].includes(value.toLowerCase())) return false;
  }
  throw new Error(`Cannot interpret ${JSON.stringify(value)} as bool`);
}

function coerceFlag(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  return Boolean(value);
}

function coercePath(value: unknown): string {
  return path.resolve(String(value));
}

function coerceChoice(value: unknown, spec: ArgSpec): string {
  const v = String(value);
  const options = spec.options ?? [];
  if (options.length > 0 && !options.includes(v)) {
    throw new Error(formatInvalidChoice(v, options, spec.name ?? '?'));
  }
  return v;
}

function checkRange(value: number, spec: ArgSpec): void {
  if (spec.range) {
    const [min, max] = spec.range;
    if (value < min || value > max) {
      throw new Error(`Value ${value} is out of range [${min}, ${max}]`);
    }
  }
}

// String-only validation (str coercer). Mirrors Python's _check_length/_check_pattern.
function checkLength(value: string, spec: ArgSpec): void {
  if (spec.minLength !== undefined && value.length < spec.minLength) {
    throw new Error(formatTooShort(value, spec.minLength, spec.name ?? '?'));
  }
  if (spec.maxLength !== undefined && value.length > spec.maxLength) {
    throw new Error(formatTooLong(value, spec.maxLength, spec.name ?? '?'));
  }
}

function checkPattern(value: string, spec: ArgSpec): void {
  if (spec.pattern === undefined) return;
  // (?:…) anchoring reproduces Python's re.fullmatch even when the pattern
  // contains a top-level alternation (plain ^p$ would mis-bind `a|b`).
  const regex = new RegExp(`^(?:${spec.pattern})$`);
  if (!regex.test(value)) {
    throw new Error(formatInvalidPattern(value, spec.pattern, spec.name ?? '?'));
  }
}

registerType('str', coerceStr);
registerType('int', coerceInt);
registerType('float', coerceFloat);
registerType('bool', coerceBool);
registerType('flag', coerceFlag);
registerType('path', coercePath);
registerType('choice', coerceChoice);
registerType('rest', (v) => (Array.isArray(v) ? v : []));
