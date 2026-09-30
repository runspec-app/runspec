export interface LoggingConfig {
  rotate: string;
  keep: number;
  /** Write the structured run-summary record to the audit log (default true) —
   *  the data the console history/analytics tabs and `runspec logs` read. */
  summary: boolean;
  /** Also echo a one-line human-readable summary to stderr at exit (default
   *  false). Off by default so stdout stays clean for pipes and the line is
   *  never tagged ERROR by stream-classifying log processors (e.g. Rundeck). */
  summaryConsole?: boolean;
  /** Log file layout: 'single' (default, one rotating {runnable}.log) or
   *  'per-run' (one {runnable}.{utc-ts}.{run_id}.log per invocation). */
  store?: string;
}

export interface RawConfig {
  autonomyDefault: string;
  /** Default enforce_run_as for runnables that don't declare one. */
  enforceRunAs: string;
  /** Package default `run_as` (`[config] run_as`); absent ⇒ `"@owner"`. */
  runAs?: unknown;
  lang?: string;
  version: string;
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
  pattern?: string; // regex the value must fully match (str only)
  minLength?: number; // minimum string length (str only)
  maxLength?: number; // maximum string length (str only)
  multiple?: boolean;
  delimiter?: string;
  short?: string;
  position?: number;
  env?: string | string[];
  deprecated?: string;
  autonomy?: string;
  ui?: string;
  hint?: string; // UI placeholder/hint text (str/path/password types)
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
  discoverable?: boolean;
  /** The OS user the runnable must run as. String, $ENV ref, or a per-host /
   *  pattern table. Consumed by an executor for escalation; on the direct path
   *  it is the identity asserted by `enforceRunAs`. */
  runAs?: unknown;
  /** Reaction when the process is not running as the resolved `runAs` user on
   *  the direct/import path: 'error' (default), 'warn', or 'off'. */
  enforceRunAs?: string;
  requireCommand?: boolean;
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
