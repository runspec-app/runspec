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
import type { ParsedArgs, ScriptSpec, ArgSpec } from './models';

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

  private buildValidArgv(p: string[], values: Record<string, unknown>): string[] | null {
    const argv: string[] = [];
    for (const [name, a] of Object.entries(this.mergedArgs(p))) {
      const supplied = values[name];
      if (supplied === undefined || supplied === null) {
        if (a.required) return null;
        continue;
      }
      const flag = `--${name}`;
      if (a.type === 'flag') {
        if (supplied) argv.push(flag);
      } else {
        argv.push(flag, String(supplied));
      }
    }
    return argv;
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
