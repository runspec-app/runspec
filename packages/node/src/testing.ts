/**
 * runspec-node/testing — test helpers for runspec runnables.
 *
 * The Node port of Python's `runspec.testing`. Import it from the subpath
 * export so it stays out of the main entry:
 *
 *     import { ParseHarness } from 'runspec-node/testing';
 *
 * `parse()` already takes `argv` and `configPath`, so testing a runnable never
 * requires monkeypatching `process.argv` or mocking the parser. `ParseHarness`
 * removes the remaining boilerplate — pointing `parse()` at a spec, asserting on
 * clean exits without the process actually exiting, and walking the subcommand
 * tree to auto-cover every command.
 *
 * Node specifics this handles for you: `parse()` calls `process.exit(0)` on
 * `--help` and *throws* `RunSpecError` on validation/require-command errors. The
 * harness intercepts `process.exit` (so `--help` doesn't kill your test runner)
 * and maps a thrown `RunSpecError` to a non-zero "exit", unifying both under one
 * exit-code abstraction that mirrors Python's `SystemExit`-based helpers.
 */

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { parse } from './parser';
import { loadRaw } from './loader';
import { inferScript } from './inference';
import { RunSpecError } from './errors';
import * as become from './become';
import type { ParsedArgs, ScriptSpec, ArgSpec, GroupSpec } from './models';

export interface ParseHarnessOptions {
  scriptName: string;
  /** An existing runspec.toml (a fixture, or your package's real spec). */
  configPath?: string;
  /** An inline TOML string written to a throwaway temp file. */
  toml?: string;
  /** Pin the per-run summary off during a parse (default true). */
  suppressSummary?: boolean;
}

export interface ExpectExitOptions {
  /** Expected exit code; `null` accepts any non-zero. Default 1; use 0 for --help. */
  code?: number | null;
  /** Substring(s) the captured output must include. */
  contains?: string | string[];
}

export interface SmokeOptions {
  /** Arg-name → value map used to build valid invocations for each leaf. */
  values?: Record<string, unknown>;
  /** Assert `--help` exits 0 for the runnable and every subcommand (default true). */
  checkHelp?: boolean;
}

/** Thrown in place of `process.exit()` so a parse that exits can be caught. */
class ExitSignal extends Error {
  constructor(public exitCode: number) {
    super(`process.exit(${exitCode})`);
  }
}

export class ParseHarness {
  readonly scriptName: string;
  readonly configPath: string;
  private readonly suppressSummary: boolean;
  private tmpDir: string | null = null;
  private readonly spec: ScriptSpec;

  constructor(opts: ParseHarnessOptions) {
    const { scriptName, configPath, toml, suppressSummary = true } = opts;
    if ((configPath == null) === (toml == null)) {
      throw new Error('pass exactly one of configPath or toml');
    }

    this.scriptName = scriptName;
    this.suppressSummary = suppressSummary;

    let cfgPath = configPath;
    if (toml != null) {
      this.tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-test-'));
      cfgPath = path.join(this.tmpDir, 'runspec.toml');
      fs.writeFileSync(cfgPath, toml, 'utf-8');
    }
    this.configPath = path.resolve(cfgPath as string);

    try {
      const raw = loadRaw(this.configPath);
      if (!(scriptName in raw.runnables)) {
        const have = Object.keys(raw.runnables).sort().join(', ') || '(none)';
        throw new Error(`runnable '${scriptName}' not in ${this.configPath} (have: ${have})`);
      }
      // Infer so `required` is resolved from defaults/types (raw leaves it unset).
      this.spec = inferScript(raw.runnables[scriptName], raw.config.autonomyDefault);
    } catch (e) {
      // Tear down the temp dir synchronously on a failed construction.
      this.close();
      throw e;
    }
  }

  // ── lifecycle ─────────────────────────────────────────────────────────────

  close(): void {
    if (this.tmpDir != null) {
      fs.rmSync(this.tmpDir, { recursive: true, force: true });
      this.tmpDir = null;
    }
  }

  // ── core parse entry points ───────────────────────────────────────────────

  parse(argv: string[]): ParsedArgs {
    const { code, output, parsed } = this.runParse(argv);
    if (parsed === null) {
      throw new Error(`expected \`${this.scriptName} ${argv.join(' ')}\` to parse, but it exited ${code}:\n${output}`);
    }
    return parsed;
  }

  expectOk(argv: string[]): ParsedArgs {
    return this.parse(argv);
  }

  expectExit(argv: string[], opts: ExpectExitOptions = {}): string {
    const { code = 1, contains } = opts;
    const res = this.runParse(argv);
    if (res.code === null) {
      throw new Error(`expected \`${this.scriptName} ${argv.join(' ')}\` to exit, but it parsed cleanly`);
    }
    if (code !== null && res.code !== code) {
      throw new Error(`\`${this.scriptName} ${argv.join(' ')}\`: expected exit ${code}, got ${res.code}:\n${res.output}`);
    }
    if (contains != null) {
      const needles = Array.isArray(contains) ? contains : [contains];
      for (const needle of needles) {
        if (!res.output.includes(needle)) {
          throw new Error(`\`${this.scriptName} ${argv.join(' ')}\`: ${JSON.stringify(needle)} not found in output:\n${res.output}`);
        }
      }
    }
    return res.output;
  }

  // ── spec introspection ────────────────────────────────────────────────────

  commandPaths(): string[][] {
    const paths: string[][] = [];
    const walk = (node: ScriptSpec, prefix: string[]): void => {
      for (const [name, sub] of Object.entries(node.commands ?? {})) {
        const here = [...prefix, name];
        paths.push(here);
        walk(sub, here);
      }
    };
    walk(this.spec, []);
    return paths;
  }

  leafCommandPaths(): string[][] {
    return this.commandPaths().filter((p) => Object.keys(this.node(p).commands ?? {}).length === 0);
  }

  argsFor(p: string[] = []): Record<string, ArgSpec> {
    return this.node(p).args ?? {};
  }

  requiredArgs(p: string[] = []): string[] {
    return Object.entries(this.argsFor(p))
      .filter(([, a]) => a.required)
      .map(([name]) => name);
  }

  requiresCommand(p: string[] = []): boolean {
    return Boolean(this.node(p).requireCommand);
  }

  // ── auto coverage ─────────────────────────────────────────────────────────

  smoke(opts: SmokeOptions = {}): string[] {
    const { values = {}, checkHelp = true } = opts;
    const done: string[] = [];

    if (checkHelp) {
      this.expectExit(['--help'], { code: 0 });
      done.push('--help');
    }
    if (this.requiresCommand()) {
      this.expectExit([]);
      done.push('(no command) -> require-command');
    }

    for (const p of this.commandPaths()) {
      const label = p.join(' ');
      const node = this.node(p);

      if (checkHelp) {
        this.expectExit([...p, '--help'], { code: 0 });
        done.push(`${label} --help`);
      }

      if (node.requireCommand) {
        this.expectExit(p);
        done.push(`${label} -> require-command`);
        continue;
      }
      if (Object.keys(node.commands ?? {}).length > 0) {
        continue; // intermediate group without require-command
      }

      if (this.requiredArgs(p).length > 0) {
        this.expectExit(p);
        done.push(`${label} -> missing required`);
      }

      const built = this.buildValidArgv(p, values);
      if (built !== null) {
        this.expectOk([...p, ...built]);
        done.push(`${label} (valid)`);
      }
    }

    return done;
  }

  // ── internals ─────────────────────────────────────────────────────────────

  private runParse(argv: string[]): { code: number | null; output: string; parsed: ParsedArgs | null } {
    // Capture the *original* (unbound) handles so restoration preserves identity.
    const realExit = process.exit;
    const realOut = process.stdout.write;
    const realErr = process.stderr.write;
    const realLog = console.log;
    const realErrLog = console.error;
    let captured = '';

    (process as unknown as { exit: (c?: number) => never }).exit = (c?: number): never => {
      throw new ExitSignal(c ?? 0);
    };
    process.stdout.write = ((chunk: unknown): boolean => {
      captured += String(chunk);
      return true;
    }) as typeof process.stdout.write;
    process.stderr.write = ((chunk: unknown): boolean => {
      captured += String(chunk);
      return true;
    }) as typeof process.stderr.write;
    console.log = (...a: unknown[]): void => {
      captured += a.map(String).join(' ') + '\n';
    };
    console.error = (...a: unknown[]): void => {
      captured += a.map(String).join(' ') + '\n';
    };

    return this.withSummaryPinned(() => {
      try {
        const parsed = parse({
          scriptName: this.scriptName,
          argv,
          configPath: this.configPath,
          _promptSecrets: false,
          // The run_as identity gate (enforceRunAs) is an execution-time
          // concern, not a wiring one. A test asserting that a runnable's args
          // parse must not depend on *who* runs the suite, so a runnable that
          // declares `runAs = "svc"` stays testable as your own user. Mirrors
          // _promptSecrets above and Python's ParseHarness.
          _checkRunAs: false,
        });
        return { code: null, output: captured, parsed };
      } catch (e) {
        if (e instanceof ExitSignal) {
          return { code: e.exitCode, output: captured, parsed: null };
        }
        if (e instanceof RunSpecError) {
          // Validation / require-command throw rather than exit; fold the
          // message into the captured output so `contains:` can match on it.
          return { code: 1, output: `${captured}${e.message}\n`, parsed: null };
        }
        throw e;
      } finally {
        process.exit = realExit;
        process.stdout.write = realOut;
        process.stderr.write = realErr;
        console.log = realLog;
        console.error = realErrLog;
      }
    });
  }

  private node(p: string[]): ScriptSpec {
    let node = this.spec;
    for (const part of p) node = (node.commands ?? {})[part];
    return node;
  }

  private mergedArgs(p: string[]): Record<string, ArgSpec> {
    const merged: Record<string, ArgSpec> = { ...this.argsFor([]) };
    const acc: string[] = [];
    for (const part of p) {
      acc.push(part);
      Object.assign(merged, this.argsFor(acc));
    }
    return merged;
  }

  private mergedGroups(p: string[]): Record<string, GroupSpec> {
    let node: ScriptSpec = this.spec;
    const merged: Record<string, GroupSpec> = { ...(node.groups ?? {}) };
    for (const part of p) {
      node = (node.commands ?? {})[part];
      Object.assign(merged, node.groups ?? {});
    }
    return merged;
  }

  // Build argv covering every required arg from `values`; null if a leaf can't
  // be satisfied (a required non-choice arg has no value, or a group constraint
  // needs a member that can't be fabricated). Group-aware across every constraint
  // the group validator enforces:
  //   * exclusive / exactly-one (capping): at most one member is included even
  //     when `values` supplies several, so a populated values map doesn't build a
  //     mutually-exclusive call the group would reject; a required member is
  //     preferred when choosing which to keep. An exactly-one group with no
  //     supplied member can't be built (null).
  //   * at-least-one / inclusive / if->requires (presence): a member the
  //     constraint needs is added even when `values` supplies none — a
  //     fabricatable one (a flag, or a choice filled from its options). An
  //     at-least-one group with only non-fabricatable members (non-choice,
  //     non-flag args with no value) can't be built cleanly, so the leaf is
  //     skipped (null) rather than building a call the group rejects.
  // Choice-aware: `values` is one flat map keyed by arg name, but the same name
  // can declare different `options` on different subcommands; for a choice arg the
  // value is validated against THIS leaf's options (falling back to its first
  // option), so one value — or none — works across the whole tree.
  private buildValidArgv(p: string[], values: Record<string, unknown>): string[] | null {
    const merged = this.mergedArgs(p);
    const groups = Object.values(this.mergedGroups(p));

    // Phase 1 — capping groups: pick members to drop so a fully-populated
    // `values` map can't build a call an exclusive/exactly-one group rejects.
    const drop = new Set<string>();
    for (const g of groups) {
      if (!(g.exclusive || g.exactlyOne)) continue;
      const supplied = (g.args ?? []).filter((m) => values[m] !== undefined && values[m] !== null);
      if (g.exactlyOne && supplied.length === 0) return null;
      if (supplied.length > 1) {
        // Keep a required member if there is one; drop the rest.
        supplied.sort((a, b) => Number(!!merged[b]?.required) - Number(!!merged[a]?.required));
        for (const m of supplied.slice(1)) drop.add(m);
      }
    }

    // Phase 2 — build argv from supplied values plus required-arg fills.
    const argv: string[] = [];
    const included = new Set<string>();
    for (const [name, a] of Object.entries(merged)) {
      if (drop.has(name)) continue;
      const frag = this.argFragment(name, a, values[name]);
      if (frag === null) return null; // required non-choice with no value -> skip leaf
      if (frag.length > 0) {
        argv.push(...frag);
        included.add(name);
      }
    }

    // Phase 3 — presence groups: add fabricatable members so the built argv
    // satisfies each constraint (or skip the leaf if it can't be).
    for (const g of groups) {
      const fill = this.presenceFill(g, merged, included, drop);
      if (fill === null) return null;
      argv.push(...fill);
    }
    return argv;
  }

  // The argv fragment for one arg, or a signal for omit / can't-build: the
  // ["--flag", value] (or ["--flag"]) fragment; [] when the arg should be
  // omitted (optional, unsupplied, not forced); or null when it must appear but
  // can't be fabricated (a required/forced non-choice, non-flag arg with no
  // value — the caller then skips the leaf). Choice-aware: a value invalid for
  // THIS leaf's options falls back to its first option; a needed choice with no
  // value is filled from its options.
  private argFragment(name: string, spec: ArgSpec, value: unknown, force = false): string[] | null {
    const options = spec.options;
    const isFlag = spec.type === 'flag';
    let v = value;
    if (v === undefined || v === null) {
      if (!(force || spec.required)) return []; // optional + unsupplied -> omit
      if (isFlag) {
        v = true;
      } else if (options && options.length > 0) {
        v = options[0]; // fill a needed choice from its own options
      } else {
        return null; // non-choice with no value -> can't fabricate
      }
    } else if (options && options.length > 0 && !options.includes(v as string)) {
      v = options[0]; // shared value invalid for THIS leaf -> a valid option
    }
    const flag = `--${name}`;
    if (isFlag) return v ? [flag] : [];
    return [flag, String(v)];
  }

  // Extra argv fragments so a presence group is satisfied by the built call, or
  // null when a needed member can't be fabricated. at-least-one: if no member is
  // included yet, add one fabricatable member. inclusive (all-or-none): if any
  // member is included, add every other member; none is already valid. if +
  // requires: if the condition arg is included, add every required arg not
  // already present. Returns [] when nothing needs adding. `included` is updated
  // in place so members added for one group count toward a later one.
  private presenceFill(
    group: GroupSpec,
    merged: Record<string, ArgSpec>,
    included: Set<string>,
    drop: Set<string>,
  ): string[] | null {
    const members = group.args ?? [];
    const fabricate = (names: string[]): string[] | null => {
      const frags: string[] = [];
      for (const m of names) {
        if (drop.has(m)) return null; // a capping group already excluded this member
        const frag = this.argFragment(m, merged[m] ?? {}, undefined, true);
        if (frag === null) return null;
        frags.push(...frag);
        included.add(m);
      }
      return frags;
    };

    if (group.atLeastOne) {
      if (members.some((m) => included.has(m))) return [];
      for (const m of members) {
        const frag = fabricate([m]);
        if (frag !== null) return frag;
      }
      return null;
    }
    if (group.inclusive) {
      if (!members.some((m) => included.has(m))) return []; // all-or-none: none is valid
      return fabricate(members.filter((m) => !included.has(m)));
    }
    if (group.condition) {
      if (!included.has(group.condition)) return [];
      return fabricate((group.requires ?? []).filter((m) => !included.has(m)));
    }
    return [];
  }

  // `suppressSummary` is retained for back-compat but is now a no-op: the
  // run-summary terminal echo is opt-in (`summary_console`, default off), so a
  // parse never leaks the closing line into test output, and the audit record
  // is written to the log file only — never the console.
  private withSummaryPinned<T>(fn: () => T): T {
    return fn();
  }
}

export function harness(scriptName: string, opts: Omit<ParseHarnessOptions, 'scriptName'> = {}): ParseHarness {
  return new ParseHarness({ scriptName, ...opts });
}

/**
 * Run `fn` with the `enforceRunAs` identity gate disabled, then restore it.
 *
 * `ParseHarness` already skips the gate, but only for parses it makes itself. A
 * test that drives a runnable's `main()` directly is different: `main()` calls
 * `parse()` *itself*, which is the real parse, so the gate fires there and a
 * `run_as` runnable throws `RunAsMismatch` on a developer's machine. Wrap the
 * `main()` call:
 *
 *     import { withoutRunAsCheck } from 'runspec-node/testing';
 *
 *     const code = withoutRunAsCheck(() => myPkg.main());
 *
 * This lives in `runspec-node/testing` (a test-only import) and swaps the
 * parser's view of the identity check for the duration of the callback, so no
 * production code path can bypass the gate — only code run inside `fn`.
 * Everything else about the parse (validation, coercion, require-command) is
 * unchanged. To *run* (not test) a `run_as` runnable by hand, set
 * `enforce_run_as = "warn"` / `"off"` in the spec instead.
 */
export function withoutRunAsCheck<T>(fn: () => T): T {
  const mutable = become as { verifyRunAsIdentity: typeof become.verifyRunAsIdentity };
  const original = mutable.verifyRunAsIdentity;
  mutable.verifyRunAsIdentity = () => null;
  try {
    return fn();
  } finally {
    mutable.verifyRunAsIdentity = original;
  }
}
