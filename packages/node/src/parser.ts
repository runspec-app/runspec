import * as path from 'path';
import { findConfig } from './finder';
import { loadRaw } from './loader';
import { inferScript, effectiveAutonomy } from './inference';
import { coerce } from './types';
import { validateArgs, validateGroups, raiseIfErrors } from './validator';
import { RunSpecError } from './errors';
import { configureLogging } from './logging_setup';
import type { ParsedArgs, ScriptSpec, ArgSpec } from './models';

export interface ParseOptions {
  scriptName?: string;
  argv?: string[];
  cwd?: string;
  configPath?: string;
}

export function parse(opts: ParseOptions = {}): ParsedArgs {
  const { scriptName, argv: argvOverride, cwd, configPath: configPathOverride } = opts;

  const { configPath } = configPathOverride ? { configPath: configPathOverride } : findConfig(cwd);
  const raw = loadRaw(configPath);
  const config = raw.config;

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

  // Auto-inject --no-summary when [config.logging] is present. Suppresses
  // the per-run summary record and stderr line for that one invocation.
  if (config.logging && !('no-summary' in rawScript.args)) {
    rawScript = {
      ...rawScript,
      args: {
        ...rawScript.args,
        'no-summary': {
          name: 'no-summary',
          type: 'flag',
          default: false,
          required: false,
          description: 'Suppress the per-run summary record and stderr line.',
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

  let parsedValues = parseArgv(argv, activeScript.args ?? {});
  parsedValues = applyEnv(parsedValues, activeScript.args ?? {}, name);
  parsedValues = applyDefaults(parsedValues, activeScript.args ?? {});

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
  const noSummary = config.logging
    ? Boolean(coercedValues['no_summary'] ?? false)
    : false;

  try {
    configureLogging({
      logCfg: config.logging,
      runnableName: name,
      configPath,
      debug,
      noSummary,
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
  return parse({ ...opts, argv: [] });
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
        const hyphenName = norm.replace(/_/g, '-');
        const spec = argSpecs[hyphenName] ?? argSpecs[norm] ?? {};
        result[norm] = appendOrSet(result[norm], value, spec);
      }
      i++;
      continue;
    }

    const norm = nameMap[token] ?? shortMap[token];
    if (norm) {
      const hyphenName = norm.replace(/_/g, '-');
      const spec = argSpecs[hyphenName] ?? argSpecs[norm] ?? {};
      const argType = spec.type ?? 'str';

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

    // Unrecognized non-flag token: assign to next positional arg
    if (!token.startsWith('-') && positionalIndex < positionalArgs.length) {
      result[positionalArgs[positionalIndex]] = token;
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
      result[norm] = autoVal;
      continue;
    }
    // Tier 2b: developer-declared aliases
    const aliases = Array.isArray(spec.env) ? spec.env : (spec.env ? [spec.env] : []);
    for (const alias of aliases) {
      const envVal = process.env[alias];
      if (envVal !== undefined) {
        result[norm] = envVal;
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
    console.log('\nCommands:');
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

  if (script.autonomy) {
    console.log(`\nAutonomy: ${script.autonomy}`);
    if (script.autonomyReason) console.log(`  ${script.autonomyReason}`);
  }

  if (Object.keys(commands).length > 0) {
    console.log(`\nRun '${fullName} <command> --help' for focused help on a command.`);
  }
  console.log('\n  -h, --help    Show this message and exit');
}
