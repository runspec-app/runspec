import type { ArgSpec, ScriptSpec } from './models';
import { RunSpecError } from './errors';

export const AUTONOMY_LEVELS = ['autonomous', 'confirm', 'supervised', 'manual'] as const;
export const AUTONOMY_RANK = Object.fromEntries(AUTONOMY_LEVELS.map((l, i) => [l, i]));

// Item-type name → its TypeScript type. Mirrors the JSON-Schema type map used
// by emit; custom (register_type) names fall back to `unknown`.
const TS_TYPE_NAMES: Record<string, string> = {
  str: 'string',
  int: 'number',
  float: 'number',
  bool: 'boolean',
  flag: 'boolean',
  path: 'string',
  choice: 'string',
  rest: 'string[]',
};

/** The TypeScript type of an arg's parsed value: the item type, suffixed `[]`
 *  when the arg is `multiple` (`rest` is always `string[]`). `arg.type` itself
 *  stays the item type — it drives per-item coercion. Pure helper, computed on
 *  demand from a (typically inferred) ArgSpec; custom registered types fall
 *  back to `"unknown"`. e.g. `tsTypeOf({type:"str", multiple:true})` → `"string[]"`. */
export function tsTypeOf(arg: ArgSpec): string {
  const type = arg.type ?? 'str';
  if (type === 'rest') return 'string[]';
  const base = TS_TYPE_NAMES[type] ?? 'unknown';
  return arg.multiple ? `${base}[]` : base;
}

export function inferArg(raw: ArgSpec): ArgSpec {
  const result = { ...raw };
  const def = result.default;
  const options = result.options;

  if (!result.type) {
    if (options !== undefined) {
      result.type = 'choice';
    } else if (typeof def === 'boolean') {
      result.type = 'flag';
    } else if (typeof def === 'number' && Number.isInteger(def)) {
      result.type = 'int';
    } else if (typeof def === 'number') {
      result.type = 'float';
    } else if (typeof def === 'string') {
      result.type = 'str';
    } else {
      result.type = 'str';
    }
  }

  if (result.type === 'rest') {
    if (result.default === undefined || result.default === null) result.default = [];
    if (result.required === undefined) result.required = false;
  } else if (result.required === undefined) {
    const hasNoDefault = def === undefined || def === null;
    result.required = hasNoDefault && result.type !== 'flag';
  }

  if (result.type === 'choice' && (!options || options.length === 0)) {
    throw new RunSpecError(`Argument '${raw.name}' has type 'choice' but no 'options' list was provided.`);
  }

  return result;
}

export function inferScript(raw: ScriptSpec, configAutonomy: string): ScriptSpec {
  const result = { ...raw };

  if (!result.autonomy) {
    result.autonomy = configAutonomy;
  }

  result.args = Object.fromEntries(Object.entries(result.args ?? {}).map(([name, arg]) => [name, inferArg(arg)]));

  result.commands = Object.fromEntries(
    Object.entries(result.commands ?? {}).map(([name, cmd]) => [name, inferScript(cmd, configAutonomy)]),
  );

  return result;
}

export function effectiveAutonomy(
  scriptAutonomy: string,
  providedArgs: Record<string, unknown>,
  argSpecs: Record<string, ArgSpec>,
): string {
  let effective = scriptAutonomy;

  for (const [argName, value] of Object.entries(providedArgs)) {
    if (value === null || value === undefined) continue;
    const spec = argSpecs[argName];
    const argAutonomy = spec?.autonomy;
    if (argAutonomy && isMoreRestrictive(argAutonomy, effective)) {
      effective = argAutonomy;
    }
  }

  return effective;
}

export function isMoreRestrictive(candidate: string, current: string): boolean {
  return (AUTONOMY_RANK[candidate] ?? 0) > (AUTONOMY_RANK[current] ?? 0);
}
