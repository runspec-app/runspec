import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';
import { execSync } from 'child_process';
import { findConfig } from './finder';
import { loadRaw } from './loader';
import { inferScript, effectiveAutonomy } from './inference';
import { coerce } from './types';
import { validateArgs, validateGroups, raiseIfErrors } from './validator';
import { resolveRunAs, verifyRunAsIdentity } from './become';
import { RunSpecError, RunAsMismatch, formatMissingCommand, formatRunAsMismatch } from './errors';
import { configureLogging, installExceptionHook } from './logging_setup';
import type { ParsedArgs, ScriptSpec, ArgSpec, GroupSpec } from './models';

export interface ParseOptions {
  scriptName?: string;
  argv?: string[];
  cwd?: string;
  configPath?: string;
  /** Internal: enforce `require-command` (real CLI parsing). loadSpec sets false. */
  _enforceRequiredCommand?: boolean;
  /** Internal: prompt for missing required password args. loadSpec sets false. */
  _promptSecrets?: boolean;
  /** Internal: verify the run_as identity (enforce_run_as). loadSpec sets false. */
  _checkRunAs?: boolean;
}

export function parse(opts: ParseOptions = {}): ParsedArgs {
  const { scriptName, argv: argvOverride, cwd, configPath: configPathOverride, _enforceRequiredCommand = true, _promptSecrets = true, _checkRunAs = true } = opts;

  const { configPath } = configPathOverride ? { configPath: configPathOverride } : findConfig(cwd);
  const raw = loadRaw(configPath);
  const config = raw.config;

  // Install the uncaught-exception handler now — before the rest of the
  // pipeline runs — so a failure inside runspec's own parse code (inference,
  // argv parsing, validation, coercion) is routed through the same one-liner /
  // --debug handler as a runtime exception rather than dumping a raw stack
  // trace. Without this the handler only goes up at the end of parse (via
  // configureLogging), leaving exceptions raised *within* runspec uncovered.
  // Gated on [config.logging] to match the feature's contract; full set-up
  // (file handler, summary, final --debug value) still happens once parsing
  // succeeds. Mirrors Python's early install in parser.py.
  if (config.logging) {
    const earlyArgv = argvOverride ?? process.argv.slice(2);
    installExceptionHook(earlyArgv.includes('--debug'));
  }

  const name = scriptName ?? inferFromArgv();
  if (!name) throw new RunSpecError('✗  Could not determine runnable name. Pass scriptName option.');
  if (name === 'config') throw new RunSpecError("✗  'config' is a reserved name in runspec.\n   Rename your runnable.");

  if (!(name in raw.runnables)) {
    const available = Object.keys(raw.runnables).join(', ') || '(none)';
    throw new RunSpecError(`✗  Runnable '${name}' not found.\n   Available: ${available}\n   Config: ${configPath}`);
  }

  let rawScript = inferScript(raw.runnables[name], config.autonomyDefault);

  // Auto-inject --debug flag when [config.logging] is present.
  // Without --debug: stdout = INFO+, stderr = WARNING+ (file always = DEBUG).
  // With --debug:    stdout also includes DEBUG records and tracebacks.
  if (config.logging && !('debug' in rawScript.args)) {
    rawScript = {
      ...rawScript,
      args: {
        ...rawScript.args,
        'debug': {
          name: 'debug',
          type: 'flag',
          default: false,
          required: false,
          description: 'Show DEBUG records and tracebacks on stdout.',
          multiple: false,
        },
      },
    };
  }

  let argv = argvOverride ?? process.argv.slice(2);
  let activeScript = rawScript;
  let commandPath: string[] = [];

  // Walk into nested subcommands as long as argv[0] matches a declared command.
  while (argv.length > 0) {
    const cmds = activeScript.commands ?? {};
    if (Object.keys(cmds).length === 0 || !(argv[0] in cmds)) break;
    commandPath.push(argv[0]);
    activeScript = cmds[argv[0]];
    argv = argv.slice(1);
  }

  if (argv.includes('--help') || argv.includes('-h')) {
    printHelp(name, activeScript, commandPath);
    process.exit(0);
  }

  // Verify run_as identity on the direct/import execution path. run_as is
  // consumed by an executor (sudo escalation) *before* the process starts, so
  // under one the effective user already matches and this passes silently; on
  // the direct path nothing escalates, so this catches running as the wrong
  // user. run_as/enforce_run_as are runnable-level, so they are read from the
  // root spec (not the merged subcommand leaf). Runs after --help so
  // `<name> --help` is always readable; skipped by loadSpec (introspection).
  if (_checkRunAs) {
    const rootSpec = raw.runnables[name];
    const detail = verifyRunAsIdentity(resolveRunAs(rootSpec.runAs, os.hostname()));
    if (detail) {
      const level = rootSpec.enforceRunAs ?? config.enforceRunAs ?? 'error';
      if (level === 'warn') {
        process.stderr.write(`⚠  ${name}: ${detail}. (enforce_run_as = "warn" — continuing)\n`);
      } else if (level !== 'off') {
        throw new RunAsMismatch(formatRunAsMismatch(name, detail));
      }
    }
  }

  // Enforce require-command on the resolved leaf: if the deepest matched node
  // still requires a command and has commands, none was chosen at that level.
  // Checking the leaf (not commandPath.length) enforces at every nesting depth.
  // Skipped by loadSpec — introspection/emit must not be blocked. Runs after
  // --help so `<name> --help` still lists the commands.
  if (
    _enforceRequiredCommand &&
    activeScript.requireCommand &&
    Object.keys(activeScript.commands ?? {}).length > 0
  ) {
    throw new RunSpecError(
      formatMissingCommand([name, ...commandPath].join(' '), Object.keys(activeScript.commands)),
    );
  }

  let parsedValues = parseArgv(argv, activeScript.args ?? {});
  parsedValues = applyEnv(parsedValues, activeScript.args ?? {}, name);
  parsedValues = applyDefaults(parsedValues, activeScript.args ?? {});

  // Prompt (no echo) for any still-unresolved required password arg when running
  // interactively. Passwords are refused on the command line, so a terminal user
  // supplies them here; agent / non-interactive runs (and loadSpec) fall through
  // to the env-var value or the required-arg error.
  const agentEarly = ['1', 'true', 'yes'].includes((process.env['RUNSPEC_AGENT'] ?? '').toLowerCase());
  if (_promptSecrets) {
    parsedValues = promptPasswords(parsedValues, activeScript.args ?? {}, agentEarly);
  }

  raiseIfErrors(validateArgs(parsedValues, activeScript.args ?? {}));
  raiseIfErrors(validateGroups(parsedValues, activeScript.groups ?? {}));

  const coercedValues = coerceValues(parsedValues, activeScript.args ?? {});

  const autonomy = effectiveAutonomy(
    activeScript.autonomy ?? config.autonomyDefault,
    parsedValues,
    activeScript.args ?? {},
  );

  const agent = ['1', 'true', 'yes'].includes((process.env['RUNSPEC_AGENT'] ?? '').toLowerCase());

  const debug = config.logging
    ? Boolean(coercedValues['debug'] ?? false)
    : false;

  try {
    configureLogging({
      logCfg: config.logging,
      runnableName: name,
      configPath,
      debug,
      autonomy,
      agent,
      commandPath,
    });
  } catch (e) {
    throw new RunSpecError((e as Error).message);
  }

  return {
    ...coercedValues,
    __runspec_agent__: agent,
    __runspec_script__: name,
    __runspec_command_path__: commandPath,
    __runspec_autonomy__: autonomy,
    __runspec_source__: configPath,
    __runspec_spec__: activeScript,
    get runspec_command() { return commandPath.length > 0 ? commandPath[commandPath.length - 1] : undefined; },
    get runspec_command_path() { return commandPath; },
    get runspec_prefix() { return path.dirname(configPath); },
  } as ParsedArgs;
}

export function loadSpec(opts: ParseOptions = {}): ParsedArgs {
  // Secret prompting is disabled — introspection must never block on a prompt.
  // The run_as identity check is likewise skipped: emitting schemas and listing
  // runnables must never depend on who is running them.
  return parse({ ...opts, argv: [], _enforceRequiredCommand: false, _promptSecrets: false, _checkRunAs: false });
}

function inferFromArgv(): string {
  const argv1 = process.argv[1] ?? '';
  return path.basename(argv1, path.extname(argv1)) || 'unknown';
}

function parseArgv(argv: string[], argSpecs: Record<string, ArgSpec>): Record<string, unknown> {
  const nameMap: Record<string, string> = {};
  const shortMap: Record<string, string> = {};

  for (const [name, spec] of Object.entries(argSpecs)) {
    const norm = name.replace(/-/g, '_');
    nameMap[`--${name}`] = norm;
    nameMap[`--${norm}`] = norm;
    if (spec.short) shortMap[spec.short] = norm;
  }

  // Positional args sorted by position index; rest arg (type='rest') collects post-'--' tokens
  const positionalArgs = Object.entries(argSpecs)
    .filter(([, s]) => s.position !== undefined)
    .sort(([, a], [, b]) => (a.position ?? 0) - (b.position ?? 0))
    .map(([name]) => name.replace(/-/g, '_'));
  const restArgNorm = Object.entries(argSpecs).find(([, s]) => s.type === 'rest')?.[0]?.replace(/-/g, '_');

  const result: Record<string, unknown> = {};
  for (const name of Object.keys(argSpecs)) {
    result[name.replace(/-/g, '_')] = undefined;
  }

  // A positional may also be given as --name (SPEC.md → Positional Arguments):
  // its slot is then skipped by bare tokens, and giving it both ways is an error.
  const positionalSet = new Set(positionalArgs);
  const byPosition = new Set<string>();
  const byName = new Set<string>();
  const claimByName = (norm: string): void => {
    if (!positionalSet.has(norm)) return;
    if (byPosition.has(norm)) {
      const flag = norm.replace(/_/g, '-');
      throw new RunSpecError(`✗  '${flag}' was given both as a positional value and as --${flag}; pass it one way.`);
    }
    byName.add(norm);
  };

  let positionalIndex = 0;
  let i = 0;
  while (i < argv.length) {
    const token = argv[i];

    // '--' separator: remaining tokens go to the rest arg
    if (token === '--') {
      if (restArgNorm !== undefined) result[restArgNorm] = argv.slice(i + 1);
      break;
    }

    if (token.startsWith('--') && token.includes('=')) {
      const eqIdx = token.indexOf('=');
      const key = token.slice(0, eqIdx);
      const value = token.slice(eqIdx + 1);
      const norm = nameMap[key];
      if (norm) {
        claimByName(norm);
        const hyphenName = norm.replace(/_/g, '-');
        const spec = argSpecs[hyphenName] ?? argSpecs[norm] ?? {};
        if ((spec as ArgSpec).type === 'password') refusePasswordOnCli(norm);
        result[norm] = appendOrSet(result[norm], value, spec);
      }
      i++;
      continue;
    }

    const norm = nameMap[token] ?? shortMap[token];
    if (norm) {
      claimByName(norm);
      const hyphenName = norm.replace(/_/g, '-');
      const spec = argSpecs[hyphenName] ?? argSpecs[norm] ?? {};
      const argType = spec.type ?? 'str';

      if (argType === 'password') refusePasswordOnCli(norm);

      if (argType === 'flag') {
        result[norm] = true;
        i++;
      } else if (i + 1 < argv.length && !argv[i + 1].startsWith('-')) {
        const raw = argv[i + 1];
        const delimiter = spec.delimiter;
        const parsed: string | string[] = delimiter ? raw.split(delimiter) : raw;
        result[norm] = appendOrSet(result[norm], parsed, spec);
        i += 2;
      } else {
        result[norm] = true;
        i++;
      }
      continue;
    }

    // Unrecognized non-flag token: assign to the next positional slot not
    // already filled by name
    while (positionalIndex < positionalArgs.length && byName.has(positionalArgs[positionalIndex])) positionalIndex++;
    if (!token.startsWith('-') && positionalIndex < positionalArgs.length) {
      result[positionalArgs[positionalIndex]] = token;
      byPosition.add(positionalArgs[positionalIndex]);
      positionalIndex++;
    }

    i++;
  }

  return result;
}

function appendOrSet(current: unknown, value: unknown, spec: ArgSpec): unknown {
  if (spec.multiple) {
    if (Array.isArray(value)) return [...((current as unknown[]) ?? []), ...value];
    return [...((current as unknown[]) ?? []), value];
  }
  return value;
}

// A `password` arg must never take its value from argv (it would leak into shell
// history and `ps`). Resolve it from the environment instead — the automatic
// RUNSPEC_<RUNNABLE>_ARG_<NAME> variable, a declared `env` alias — or let runspec
// prompt for it when run interactively in a terminal.
function refusePasswordOnCli(norm: string): never {
  const flag = norm.replace(/_/g, '-');
  const envHint = `RUNSPEC_<RUNNABLE>_ARG_${norm.toUpperCase()}`;
  throw new RunSpecError(
    `✗  --${flag} is a password and cannot be passed on the command line.\n` +
      `   Set it via the environment (${envHint} or an 'env' alias),\n` +
      `   or run the command interactively to be prompted.`,
  );
}

// Interactively prompt (no echo) for any required password arg still unresolved
// after env/defaults. Only when attached to a real terminal and not invoked by
// an agent. POSIX only (uses `stty`); Windows / non-TTY runs fall through to the
// normal required-arg error.
function promptPasswords(
  parsed: Record<string, unknown>,
  argSpecs: Record<string, ArgSpec>,
  agent: boolean,
): Record<string, unknown> {
  if (agent || process.platform === 'win32' || !(process.stdin.isTTY && process.stderr.isTTY)) {
    return parsed;
  }
  const result = { ...parsed };
  for (const [name, spec] of Object.entries(argSpecs)) {
    if (spec.type !== 'password') continue;
    const norm = name.replace(/-/g, '_');
    if (result[norm] !== undefined && result[norm] !== null) continue;
    if (!spec.required) continue;
    const value = readLineNoEcho(`${name}: `);
    if (value) result[norm] = value;
  }
  return result;
}

// Read one line from stdin with echo suppressed (getpass equivalent). Reads
// bytes so multibyte UTF-8 secrets decode correctly; restores echo on exit.
function readLineNoEcho(prompt: string): string {
  process.stderr.write(prompt);
  try {
    try {
      execSync('stty -echo', { stdio: 'inherit' });
    } catch {
      /* no stty available — proceed (input may echo) */
    }
    const bytes: number[] = [];
    const buf = Buffer.alloc(1);
    for (;;) {
      let n: number;
      try {
        n = fs.readSync(0, buf, 0, 1, null);
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === 'EAGAIN') continue;
        break;
      }
      if (n === 0) break;
      const b = buf[0];
      if (b === 0x0a) break; // \n
      if (b === 0x0d) continue; // \r
      bytes.push(b);
    }
    return Buffer.from(bytes).toString('utf8');
  } catch {
    return '';
  } finally {
    try {
      execSync('stty echo', { stdio: 'inherit' });
    } catch {
      /* ignore */
    }
    process.stderr.write('\n');
  }
}

// A `multiple` arg's env value is newline-delimited (the spec's env
// serialisation — what `runspec serve` writes): split it into a list, dropping
// empty lines. Every other type stays a string for its coercer.
function envValue(raw: string, spec: ArgSpec): unknown {
  if (spec.multiple && spec.type !== 'rest') return raw.split('\n').filter((item) => item !== '');
  return raw;
}

function applyEnv(parsed: Record<string, unknown>, argSpecs: Record<string, ArgSpec>, runnableName: string): Record<string, unknown> {
  const runnablePrefix = runnableName.toUpperCase().replace(/-/g, '_');
  const result = { ...parsed };
  for (const [name, spec] of Object.entries(argSpecs)) {
    const norm = name.replace(/-/g, '_');
    if (result[norm] !== null && result[norm] !== undefined) continue;
    // Tier 2a: automatic RUNSPEC_<RUNNABLE>_ARG_<ARGNAME>
    const autoKey = `RUNSPEC_${runnablePrefix}_ARG_` + name.toUpperCase().replace(/-/g, '_');
    const autoVal = process.env[autoKey];
    if (autoVal !== undefined) {
      result[norm] = envValue(autoVal, spec);
      continue;
    }
    // Tier 2b: developer-declared aliases
    const aliases = Array.isArray(spec.env) ? spec.env : (spec.env ? [spec.env] : []);
    for (const alias of aliases) {
      const envVal = process.env[alias];
      if (envVal !== undefined) {
        result[norm] = envValue(envVal, spec);
        break;
      }
    }
  }
  return result;
}

function applyDefaults(parsed: Record<string, unknown>, argSpecs: Record<string, ArgSpec>): Record<string, unknown> {
  const result = { ...parsed };
  for (const [name, spec] of Object.entries(argSpecs)) {
    const norm = name.replace(/-/g, '_');
    if ((result[norm] === null || result[norm] === undefined) && spec.default !== undefined && spec.default !== null) {
      result[norm] = spec.default;
    }
  }
  return result;
}

function coerceValues(parsed: Record<string, unknown>, argSpecs: Record<string, ArgSpec>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [name, spec] of Object.entries(argSpecs)) {
    const norm = name.replace(/-/g, '_');
    const value = parsed[norm];
    if (value === null || value === undefined) {
      result[norm] = undefined;
      continue;
    }
    try {
      result[norm] = coerce(value, spec);
    } catch (e) {
      throw new RunSpecError(`✗  ${(e as Error).message}`);
    }
  }
  return result;
}

// One-line human description of an argument group, for --help. Mirrors the
// constraint validateGroups enforces (and Python's _group_constraint_text), so
// --help advertises mutual-exclusivity / co-requirement.
function groupConstraintText(g: GroupSpec): string {
  const flags = (g.args ?? []).map((a) => `--${a}`).join(', ');
  if (g.exactlyOne) return `exactly one of: ${flags}`;
  if (g.exclusive) return `at most one of: ${flags}`;
  if (g.atLeastOne) return `at least one of: ${flags}`;
  if (g.inclusive) return `all or none of: ${flags}`;
  if (g.condition) {
    const req = (g.requires ?? []).map((a) => `--${a}`).join(', ');
    return `if --${g.condition} is set, also required: ${req}`;
  }
  return flags;
}

export function printHelp(name: string, script: ScriptSpec, commandPath: string[] = []): void {
  const fullName = [name, ...commandPath].join(' ');
  const args = script.args ?? {};
  const commands = script.commands ?? {};

  // Partition args into flags, positionals, and rest (mirrors Python's _print_help).
  const entries = Object.entries(args);
  const positionalArgs = entries
    .filter(([, s]) => s.position !== undefined)
    .sort(([, a], [, b]) => (a.position ?? 0) - (b.position ?? 0));
  const restArgs = entries.filter(([, s]) => s.type === 'rest');
  const flagArgs = entries.filter(([, s]) => s.position === undefined && s.type !== 'rest');

  // Choices render their options inline; other types render as <type>.
  const argToken = (spec: typeof args[string]): string =>
    spec.options ? `<${spec.options.join('|')}>` : `<${spec.type ?? 'str'}>`;

  // Usage line — order: name [flags] [positionals] [<command>] [-- <rest>...]
  // Rest stays last because '--' terminates argument parsing.
  const usageParts: string[] = [fullName];
  for (const [argName, spec] of flagArgs) {
    const flag = `--${argName}`;
    if (spec.type === 'flag') usageParts.push(`[${flag}]`);
    else if (spec.required) usageParts.push(`${flag} ${argToken(spec)}`);
    else usageParts.push(`[${flag} ${argToken(spec)}]`);
  }
  for (const [argName, spec] of positionalArgs) {
    usageParts.push(spec.required ? `<${argName}>` : `[<${argName}>]`);
  }
  if (Object.keys(commands).length > 0) usageParts.push('<command>');
  for (const [argName] of restArgs) {
    usageParts.push(`[-- <${argName}>...]`);
  }

  console.log(`Usage: ${usageParts.join(' ')}`);
  if (script.description) console.log(`\n${script.description}`);

  // Commands section
  if (Object.keys(commands).length > 0) {
    console.log(script.requireCommand ? '\nCommands (required):' : '\nCommands:');
    const cmdCol = Math.max(...Object.keys(commands).map((c) => c.length)) + 2;
    for (const [cmdName, cmdSpec] of Object.entries(commands)) {
      const desc = cmdSpec.description ?? '';
      console.log(`  ${cmdName.padEnd(cmdCol)} ${desc}`);
    }
  }

  if (Object.keys(args).length > 0) {
    console.log('\nArguments:');
    for (const [argName, spec] of entries) {
      const flag = `  --${argName}`;
      const parts: string[] = [];
      if (spec.type === 'flag') parts.push('flag');
      else parts.push(spec.type ?? 'str');
      if (spec.required) parts.push('required');
      else if (spec.default !== undefined && spec.default !== null) parts.push(`default: ${spec.default}`);
      if (spec.options) parts.push(`one of: ${spec.options.join(', ')}`);
      const meta = `(${parts.join(', ')})`;
      if (spec.description) console.log(`${flag.padEnd(24)} ${spec.description}  ${meta}`);
      else console.log(`${flag.padEnd(24)} ${meta}`);
    }
  }

  const groups = script.groups ?? {};
  if (Object.keys(groups).length > 0) {
    console.log('\nConstraints:');
    for (const g of Object.values(groups)) {
      console.log(`  ${groupConstraintText(g)}`);
    }
  }

  if (script.autonomy) {
    console.log(`\nAutonomy: ${script.autonomy}`);
    if (script.autonomyReason) console.log(`  ${script.autonomyReason}`);
  }

  if (Object.keys(commands).length > 0) {
    console.log(`\nRun '${fullName} <command> --help' for focused help on a command.`);
  }
  console.log('\n  -h, --help    Show this message and exit');
}
