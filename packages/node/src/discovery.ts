/**
 * The `discoverable` keyword — whether a runnable is listed at all.
 *
 * `discoverable` is a plain boolean visibility gate, orthogonal to `serve`
 * (which declares *the context*). Metadata only — it adds no runtime behaviour;
 * the discovery surfaces (`runspec serve`, `runspec local`) read it to decide
 * whether to expose the runnable:
 *   absent / true   → visible (the default)
 *   false           → hidden from all discovery surfaces (an internal helper)
 *
 * A `false` runnable still runs when invoked directly; it simply never surfaces
 * as a discovered tool. Marking a runnable for a *self-service* audience is no
 * longer a format concern — that lives in the catalog consumer's configuration.
 * Mirrors packages/python/runspec/runspec/discovery.py.
 */

export type Discoverable = boolean | undefined;

export function normaliseDiscoverable(value: Discoverable): boolean {
  return value !== false;
}

export function validateDiscoverable(value: unknown): string[] {
  if (value === undefined || typeof value === 'boolean') return [];
  return [`discoverable must be true or false, got ${typeof value}`];
}
