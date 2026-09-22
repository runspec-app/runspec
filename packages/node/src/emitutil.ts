/**
 * emitutil.ts — Shared helpers for the `runspec emit` target generators.
 *
 * Both `rundeckgen` (Rundeck jobs) and `ansiblegen` (Ansible modules) turn a
 * runnable's inferred spec into one artefact per **leaf command** and pass
 * secrets through the runspec env channel. Those two pieces are
 * platform-agnostic, so they live here and are re-exported by each generator.
 *
 * Ports `runspec/emitutil.py`.
 */

import { inferArg } from './inference';
import { ArgSpec, ScriptSpec } from './models';

/** One leaf command flattened out of a runnable's subcommand tree. */
export interface Leaf {
  /** Serve-style leaf id, e.g. `cron_add` (or the runnable name at the root). */
  name: string;
  description: string;
  /** Tokens to append after the runnable binary, e.g. `["add"]`. */
  commandPath: string[];
  args: Record<string, ArgSpec>;
}

/**
 * The runspec secret channel var for an arg — `RUNSPEC_<RUNNABLE>_ARG_<NAME>`.
 *
 * Mirrors `parser.applyEnv` / `serve.envKey`: keyed on the top-level runnable
 * name (the entry point), not the leaf command, with hyphens → underscores and
 * uppercased.
 */
export function secretEnvVar(runnable: string, name: string): string {
  const r = runnable.toUpperCase().replace(/-/g, '_');
  const n = name.toUpperCase().replace(/-/g, '_');
  return `RUNSPEC_${r}_ARG_${n}`;
}

/**
 * Flatten a runnable's subcommand tree into one entry per leaf command.
 *
 * A runnable with no `commands` yields a single leaf (itself). Otherwise one
 * leaf per fully-pathed command, with args = the root (global) args merged with
 * each command's own args along the path, child overriding parent on a name
 * clash — the same inheritance `parser.resolveSubcommand` applies at runtime.
 * Args are run through `inferArg` so inferred types are reflected.
 */
export function expandLeaves(runnable: string, script: ScriptSpec): Leaf[] {
  const rootDesc = script.description ?? '';
  const leaves: Leaf[] = [];

  const walk = (node: ScriptSpec, path: string[], merged: Record<string, ArgSpec>): void => {
    const args: Record<string, ArgSpec> = { ...merged, ...(node.args ?? {}) };
    const commands = node.commands ?? {};
    if (Object.keys(commands).length === 0) {
      const name = path.length ? [runnable, ...path].join('_') : runnable;
      const inferred: Record<string, ArgSpec> = {};
      for (const [k, v] of Object.entries(args)) {
        inferred[k] = inferArg(v);
      }
      leaves.push({
        name,
        description: node.description || rootDesc,
        commandPath: [...path],
        args: inferred,
      });
      return;
    }
    for (const [cmdName, cmdNode] of Object.entries(commands)) {
      walk(cmdNode, [...path, cmdName], args);
    }
  };

  walk(script, [], {});
  return leaves;
}
