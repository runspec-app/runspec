import * as fs from 'fs';
import * as path from 'path';

/**
 * Locate the governing `runspec.toml`.
 *
 * Resolution order mirrors the Python `parse()`:
 *   1. `RUNSPEC_CONFIG` env var — explicit override.
 *   2. an explicit `start` directory, when one is passed.
 *   3. **caller-relative** — walk up from the entry script's directory
 *      (`require.main` / `process.argv[1]`). This is what lets an installed
 *      runnable find its own config from *any* working directory — e.g. when a
 *      controller launches `bin/greet` over SSH with `cwd` = the login home,
 *      not the package. Mirrors Python resolving relative to the installed
 *      package, "what makes installed entry points work from any working
 *      directory".
 *   4. **cwd-relative** — walk up from `process.cwd()` (the historical
 *      behaviour, kept as the final fallback).
 *
 * A `runspec.toml` found *inside* a `node_modules` path is ignored: those
 * belong to installed dependencies (including runspec-node's own bundled CLI
 * spec), never the project being discovered.
 */
export function findConfig(start?: string): { configPath: string } {
  const env = process.env.RUNSPEC_CONFIG;
  if (env && fs.existsSync(env)) return { configPath: path.resolve(env) };

  // process.argv[1] over require.main.filename: the latter realpath-resolves
  // symlinks, which under `npm link` / workspaces / a local install jumps the
  // entry *out* of the project (to the linked source) and breaks the walk.
  // process.argv[1] preserves the path the binary was actually invoked at —
  // inside the project's node_modules — which is what we want to walk up from.
  const entry = process.argv[1] ?? require.main?.filename;
  const entryDir = entry ? path.dirname(entry) : undefined;

  for (const dir of searchStarts(start, entryDir, process.cwd())) {
    const found = walkUp(dir);
    if (found) return { configPath: found };
  }

  throw new Error(
    "No runspec configuration found.\nExpected runspec.toml inside your package directory.\n\nRun 'runspec init' to create one.",
  );
}

/**
 * Ordered, de-duplicated list of directories to walk up from, mirroring the
 * resolution order documented on `findConfig`. Pure (no I/O) so the priority
 * logic is unit-testable without a filesystem fixture.
 */
export function searchStarts(start: string | undefined, entryDir: string | undefined, cwd: string): string[] {
  const out: string[] = [];
  if (start) out.push(path.resolve(start));
  if (entryDir) out.push(path.resolve(entryDir));
  out.push(path.resolve(cwd));
  return [...new Set(out)];
}

/**
 * Walk up from `start` looking for a `runspec.toml`, skipping any found under a
 * `node_modules` path (a dependency's spec, not the project's). Returns the
 * path or null. Exported for testing.
 */
export function walkUp(start: string): string | null {
  let dir = path.resolve(start);
  while (true) {
    const runspecToml = path.join(dir, 'runspec.toml');
    if (fs.existsSync(runspecToml) && !isInNodeModules(dir)) {
      return runspecToml;
    }
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function isInNodeModules(dir: string): boolean {
  return dir.split(path.sep).includes('node_modules');
}
