import * as fs from 'fs';
import { parse as parseTOML } from 'smol-toml';
import type { RawConfig, RawSpec, ScriptSpec, ArgSpec, GroupSpec, JumpHostConfig, LoggingConfig } from './models';

export function loadRaw(configPath: string): RawSpec {
  const content = fs.readFileSync(configPath, 'utf-8');
  const raw = parseTOML(content) as Record<string, unknown>;

  const runnablesRaw: Record<string, Record<string, unknown>> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (key !== 'config' && typeof value === 'object' && value !== null && !Array.isArray(value)) {
      runnablesRaw[key] = value as Record<string, unknown>;
    }
  }

  return {
    config: normaliseConfig((raw['config'] ?? {}) as Record<string, unknown>),
    runnables: normaliseRunnables(runnablesRaw),
  };
}

function normaliseConfig(raw: Record<string, unknown>): RawConfig {
  const rawHosts = (raw['jump-hosts'] ?? {}) as Record<string, Record<string, unknown>>;
  const jumpHosts: Record<string, JumpHostConfig> = {};
  for (const [name, cfg] of Object.entries(rawHosts)) {
    jumpHosts[name] = normaliseJumpHost(cfg);
  }
  return {
    autonomyDefault: (raw['autonomy-default'] as string | undefined) ?? 'confirm',
    lang: raw['lang'] as string | undefined,
    version: String(raw['version'] ?? '1'),
    jumpHosts,
    logging: normaliseLogging(raw['logging'] as Record<string, unknown> | undefined),
  };
}

const VALID_LOG_LEVELS = new Set(['debug', 'info', 'warning', 'error', 'critical']);

function normaliseLogging(raw: Record<string, unknown> | undefined): LoggingConfig | undefined {
  if (raw === undefined) return undefined;
  const level = String(raw['level'] ?? 'info').toLowerCase();
  if (!VALID_LOG_LEVELS.has(level)) {
    const sorted = [...VALID_LOG_LEVELS].sort().join(', ');
    throw new Error(`✗  [config.logging] level must be one of: ${sorted}. Got: ${JSON.stringify(level)}`);
  }
  return {
    level,
    rotate: String(raw['rotate'] ?? 'midnight'),
    keep: Number(raw['keep'] ?? 7),
  };
}

function normaliseJumpHost(raw: Record<string, unknown>): JumpHostConfig {
  return {
    host: raw['host'] as string,
    user: raw['user'] as string | undefined,
    port: raw['port'] as number | undefined,
    sshKey: raw['ssh-key'] as string | undefined,
    bin: raw['bin'] as string | undefined,
    useSshConfig: raw['use-ssh-config'] as boolean | undefined,
    sshOptions: raw['ssh-options'] as string[] | undefined,
  };
}

function normaliseRunnables(raw: Record<string, Record<string, unknown>>): Record<string, ScriptSpec> {
  return Object.fromEntries(Object.entries(raw).map(([name, data]) => [name, normaliseScript(name, data)]));
}

function normaliseScript(name: string, raw: Record<string, unknown>): ScriptSpec {
  return {
    name,
    description: raw['description'] as string | undefined,
    autonomy: raw['autonomy'] as string | undefined,
    autonomyReason: raw['autonomy-reason'] as string | undefined,
    output: raw['output'] as string | undefined,
    args: normaliseArgs((raw['args'] ?? {}) as Record<string, unknown>),
    groups: normaliseGroups((raw['groups'] ?? {}) as Record<string, unknown>),
    commands: normaliseCommands((raw['commands'] ?? {}) as Record<string, Record<string, unknown>>),
  };
}

function normaliseArgs(raw: Record<string, unknown>): Record<string, ArgSpec> {
  return Object.fromEntries(
    Object.entries(raw).map(([name, value]) => {
      if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
        return [name, normaliseArg(name, value as Record<string, unknown>)];
      }
      return [name, normaliseArg(name, { default: value })];
    }),
  );
}

function normaliseArg(name: string, raw: Record<string, unknown>): ArgSpec {
  return {
    name,
    type: (raw['type'] as string | undefined) ?? undefined,
    required: raw['required'] as boolean | undefined,
    default: raw['default'],
    description: raw['description'] as string | undefined,
    options: raw['options'] as string[] | undefined,
    range: raw['range'] as [number, number] | undefined,
    multiple: (raw['multiple'] as boolean | undefined) ?? false,
    delimiter: raw['delimiter'] as string | undefined,
    short: raw['short'] as string | undefined,
    position: raw['position'] as number | undefined,
    env: raw['env'] as string | undefined,
    deprecated: raw['deprecated'] as string | undefined,
    autonomy: raw['autonomy'] as string | undefined,
    ui: raw['ui'] as string | undefined,
    meta: raw['meta'] as Record<string, unknown> | undefined,
  };
}

function normaliseGroups(raw: Record<string, unknown>): Record<string, GroupSpec> {
  return Object.fromEntries(
    Object.entries(raw).map(([name, data]) => {
      const g = data as Record<string, unknown>;
      return [
        name,
        {
          name,
          args: (g['args'] as string[]) ?? [],
          exclusive: (g['exclusive'] as boolean | undefined) ?? false,
          inclusive: (g['inclusive'] as boolean | undefined) ?? false,
          atLeastOne: (g['at-least-one'] as boolean | undefined) ?? false,
          exactlyOne: (g['exactly-one'] as boolean | undefined) ?? false,
          condition: g['if'] as string | undefined,
          requires: (g['requires'] as string[] | undefined) ?? [],
        } satisfies GroupSpec,
      ];
    }),
  );
}

function normaliseCommands(raw: Record<string, Record<string, unknown>>): Record<string, ScriptSpec> {
  return Object.fromEntries(Object.entries(raw).map(([name, data]) => [name, normaliseScript(name, data)]));
}

