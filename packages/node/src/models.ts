export interface JumpHostConfig {
  host: string;
  user?: string;
  port?: number;
  sshKey?: string;
  bin?: string;
  useSshConfig?: boolean;
  sshOptions?: string[];
}

export interface LoggingConfig {
  rotate: string;
  keep: number;
  summary: boolean;
}

export interface RawConfig {
  autonomyDefault: string;
  lang?: string;
  version: string;
  jumpHosts: Record<string, JumpHostConfig>;
  logging?: LoggingConfig;
}

export interface ArgSpec {
  name: string;
  type?: string;
  required?: boolean;
  default?: unknown;
  description?: string;
  options?: string[];
  range?: [number, number];
  multiple?: boolean;
  delimiter?: string;
  short?: string;
  position?: number;
  env?: string | string[];
  deprecated?: string;
  autonomy?: string;
  ui?: string;
  meta?: Record<string, unknown>;
}

export interface GroupSpec {
  name: string;
  args: string[];
  exclusive?: boolean;
  inclusive?: boolean;
  atLeastOne?: boolean;
  exactlyOne?: boolean;
  condition?: string;
  requires?: string[];
}

export interface ScriptSpec {
  name: string;
  description?: string;
  autonomy?: string;
  autonomyReason?: string;
  output?: string;
  serve?: boolean | string[];
  args: Record<string, ArgSpec>;
  groups: Record<string, GroupSpec>;
  commands: Record<string, ScriptSpec>;
}

export interface RawSpec {
  config: RawConfig;
  runnables: Record<string, ScriptSpec>;
}

export interface ParsedArgs {
  [key: string]: unknown;
  readonly __runspec_agent__: boolean;
  readonly __runspec_script__: string;
  readonly __runspec_command_path__: string[];
  readonly __runspec_autonomy__: string;
  readonly __runspec_source__: string;
  readonly __runspec_spec__: ScriptSpec;
  readonly runspec_command: string | undefined;
  readonly runspec_command_path: string[];
  readonly runspec_prefix: string;
}
