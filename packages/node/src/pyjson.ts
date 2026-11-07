/**
 * JSON written the way Python's `json.dumps(..., ensure_ascii=False)` writes it.
 *
 * The audit ledger's hash is taken over Python's canonical JSON (sorted keys,
 * `(",", ":")` separators), so a ledger written by one language pack verifies
 * in the other. JavaScript differs from Python in three places, handled here:
 *
 * - **Numbers.** Python keeps integers and floats apart (`1` vs `1.0`) and
 *   writes floats with `repr` (`1e-05`, `1e+16`); JavaScript has one number
 *   type and writes `0.00001`, `10000000000000000`. Values built in JavaScript
 *   are written as integers when whole and as Python floats otherwise;
 *   numbers read back from a line keep the form they were written in
 *   ({@link PyNum}).
 * - **Key order.** Sorted by Unicode code point (JavaScript's default sort
 *   compares UTF-16 code units).
 * - **Escapes.** Only `"`, `\` and characters below U+0020 are escaped.
 *
 * See SPEC.md → "Audit Events" → "Canonical form".
 */

/** A number read from JSON text, kept in Python's canonical form. */
export class PyNum {
  constructor(public readonly text: string) {}
}

export type PyValue = null | boolean | number | bigint | string | PyNum | PyValue[] | { [key: string]: PyValue };

// ── numbers ──────────────────────────────────────────────────────────────────

/** Python's `repr(float(x))`. */
export function pyFloatRepr(x: number): string {
  if (Number.isNaN(x)) return 'NaN';
  if (x === Infinity) return 'Infinity';
  if (x === -Infinity) return '-Infinity';
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  const sign = x < 0 ? '-' : '';
  const [mant, expText] = Math.abs(x).toExponential().split('e');
  const digits = mant.replace('.', '');
  const decpt = parseInt(expText, 10) + 1; // value = 0.<digits> × 10^decpt
  let body: string;
  if (decpt > -4 && decpt <= 16) {
    if (decpt <= 0) body = '0.' + '0'.repeat(-decpt) + digits;
    else if (decpt >= digits.length) body = digits + '0'.repeat(decpt - digits.length) + '.0';
    else body = digits.slice(0, decpt) + '.' + digits.slice(decpt);
  } else {
    const exp = decpt - 1;
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    body = `${m}e${exp < 0 ? '-' : '+'}${String(Math.abs(exp)).padStart(2, '0')}`;
  }
  return sign + body;
}

/** A JavaScript number as Python would write it: whole ⇒ int, else float repr. */
export function pyNumber(x: number): string {
  if (Number.isFinite(x) && Number.isInteger(x)) {
    return Object.is(x, -0) ? '0' : BigInt(x).toString();
  }
  return pyFloatRepr(x);
}

/** A number token from JSON text in Python's canonical form (int stays int, float stays float). */
export function canonicalNumberToken(token: string): string {
  if (token === 'NaN' || token === 'Infinity' || token === '-Infinity') return token;
  if (/[.eE]/.test(token)) return pyFloatRepr(Number(token));
  return BigInt(token).toString();
}

// ── strings ──────────────────────────────────────────────────────────────────

const ESCAPES: Record<string, string> = { '"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t' };

export function pyString(s: string): string {
  // eslint-disable-next-line no-control-regex
  return '"' + s.replace(/[\x00-\x1f"\\]/g, (c) => ESCAPES[c] ?? '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')) + '"';
}

/** Compare by Unicode code point (Python's `str` ordering). */
export function codePointCompare(a: string, b: string): number {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const x = ia.next();
    const y = ib.next();
    if (x.done || y.done) return x.done && y.done ? 0 : x.done ? -1 : 1;
    const cx = x.value.codePointAt(0)!;
    const cy = y.value.codePointAt(0)!;
    if (cx !== cy) return cx < cy ? -1 : 1;
  }
}

// ── dumps ────────────────────────────────────────────────────────────────────

export interface DumpOptions {
  /** Sort object keys (canonical form). */
  sortKeys?: boolean;
  /** `(",", ":")` separators; otherwise Python's default `(", ", ": ")`. */
  compact?: boolean;
}

/** Serialise like Python's `json.dumps(value, ensure_ascii=False, ...)`. */
export function pyDumps(value: unknown, opts: DumpOptions = {}): string {
  const item = opts.compact ? ',' : ', ';
  const kv = opts.compact ? ':' : ': ';
  const out = (v: unknown): string => {
    if (v === null || v === undefined) return 'null';
    if (v instanceof PyNum) return v.text;
    switch (typeof v) {
      case 'boolean':
        return v ? 'true' : 'false';
      case 'number':
        return pyNumber(v);
      case 'bigint':
        return v.toString();
      case 'string':
        return pyString(v);
    }
    if (Array.isArray(v)) return '[' + v.map(out).join(item) + ']';
    if (v instanceof Date) return pyString(v.toISOString());
    if (typeof v === 'object') {
      const keys = Object.keys(v as object).filter((k) => (v as Record<string, unknown>)[k] !== undefined);
      if (opts.sortKeys) keys.sort(codePointCompare);
      return '{' + keys.map((k) => pyString(k) + kv + out((v as Record<string, unknown>)[k])).join(item) + '}';
    }
    return pyString(String(v));
  };
  return out(value);
}

/** The canonical form a ledger hash is taken over: sorted keys, no whitespace. */
export function canonicalJson(value: unknown): string {
  return pyDumps(value, { sortKeys: true, compact: true });
}

// ── parse ────────────────────────────────────────────────────────────────────

/**
 * Parse JSON text, keeping each number as a {@link PyNum} in Python's
 * canonical form. Accepts Python's `NaN` / `Infinity` / `-Infinity`. Throws
 * `SyntaxError` on anything else that isn't JSON.
 */
export function pyParse(text: string): PyValue {
  let i = 0;
  const ws = () => {
    while (i < text.length && ' \t\n\r'.includes(text[i])) i++;
  };
  const fail = (): never => {
    throw new SyntaxError(`Invalid JSON at position ${i}`);
  };
  const value = (): PyValue => {
    ws();
    const c = text[i];
    if (c === '{') {
      i++;
      const obj: { [key: string]: PyValue } = {};
      ws();
      if (text[i] === '}') {
        i++;
        return obj;
      }
      for (;;) {
        ws();
        if (text[i] !== '"') fail();
        const key = str();
        ws();
        if (text[i] !== ':') fail();
        i++;
        obj[key] = value();
        ws();
        if (text[i] === ',') {
          i++;
          continue;
        }
        if (text[i] === '}') {
          i++;
          return obj;
        }
        fail();
      }
    }
    if (c === '[') {
      i++;
      const arr: PyValue[] = [];
      ws();
      if (text[i] === ']') {
        i++;
        return arr;
      }
      for (;;) {
        arr.push(value());
        ws();
        if (text[i] === ',') {
          i++;
          continue;
        }
        if (text[i] === ']') {
          i++;
          return arr;
        }
        fail();
      }
    }
    if (c === '"') return str();
    for (const [word, v] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(word, i)) {
        i += word.length;
        return v;
      }
    }
    for (const word of ['NaN', 'Infinity', '-Infinity']) {
      if (text.startsWith(word, i)) {
        i += word.length;
        return new PyNum(word);
      }
    }
    const m = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i, i + 400));
    if (!m) fail();
    i += m![0].length;
    return new PyNum(canonicalNumberToken(m![0]));
  };
  const str = (): string => {
    const start = i;
    i++;
    while (i < text.length && text[i] !== '"') {
      if (text[i] === '\\') i++;
      i++;
    }
    if (i >= text.length) fail();
    i++;
    return JSON.parse(text.slice(start, i)) as string;
  };
  const v = value();
  ws();
  if (i !== text.length) fail();
  return v;
}
