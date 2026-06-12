/**
 * Discoverability audiences for runnables (the `discoverable` keyword).
 *
 * `discoverable` declares *which audiences* may discover a runnable, orthogonal
 * to `serve` (which declares *the context*). Metadata only — it adds no runtime
 * behaviour; the surfaces (`runspec serve`, `runspec local`, a future
 * self-service catalog) read it to decide what to expose to whom.
 *
 * Canonical form is a list of audience strings:
 *   absent / true     → ["operator"]   (default; operator-visible)
 *   false             → []             (hidden from all surfaces)
 *   ["operator", ...] → as written (validated)
 *
 * `self-service` is never implied — a runnable reaches a visitor-facing catalog
 * only by listing it explicitly, so the default never exposes it to untrusted
 * users. Mirrors packages/python/runspec/runspec/discovery.py.
 */

export const OPERATOR = 'operator';
export const SELF_SERVICE = 'self-service';
export const AUDIENCES: readonly string[] = [OPERATOR, SELF_SERVICE];

export type Discoverable = boolean | string[] | undefined;

export function normaliseDiscoverable(value: Discoverable): string[] {
  if (value === undefined || value === true) return [OPERATOR];
  if (value === false) return [];
  if (Array.isArray(value)) {
    const seen: string[] = [];
    for (const item of value) {
      if (typeof item === 'string' && !seen.includes(item)) seen.push(item);
    }
    return seen;
  }
  return [OPERATOR];
}

export function validateDiscoverable(value: Discoverable): string[] {
  if (value === undefined || typeof value === 'boolean') return [];
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return ['discoverable = [] is invalid — write false to hide everywhere, or omit to default to true'];
    }
    const unknown = value.filter((a) => !AUDIENCES.includes(a));
    if (unknown.length > 0) {
      return [`discoverable contains unknown audience(s) ${JSON.stringify(unknown)} — valid values are 'operator' and 'self-service'`];
    }
    return [];
  }
  return [`discoverable must be true, false, or a list of audiences ('operator', 'self-service'), got ${typeof value}`];
}

export function isDiscoverableTo(value: Discoverable, audience: string): boolean {
  return normaliseDiscoverable(value).includes(audience);
}
