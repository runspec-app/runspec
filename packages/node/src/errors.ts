export class RunSpecError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RunSpecError';
  }
}

export class MissingRequiredArg extends RunSpecError {}
export class InvalidChoice extends RunSpecError {}
export class OutOfRange extends RunSpecError {}
export class UnknownArg extends RunSpecError {}
export class GroupViolation extends RunSpecError {}
export class AutonomyViolation extends RunSpecError {}

export function formatMissingRequired(name: string, spec: Record<string, unknown>): string {
  const lines = [
    `✗  Missing required argument: --${name}`,
    `   Type: ${spec['type'] ?? 'str'}`,
  ];
  if (spec['description']) lines.push(`   Description: ${spec['description']}`);
  if (spec['env']) lines.push(`   Tip: set environment variable ${spec['env']} as an alternative`);
  return lines.join('\n');
}

export function formatInvalidChoice(value: string, options: string[], name: string): string {
  const lines = [
    `✗  Invalid value for --${name}: ${JSON.stringify(value)}`,
    `   Expected one of: ${options.join(', ')}`,
    `   Got: ${JSON.stringify(value)}`,
  ];
  const s = suggest(value, options);
  if (s) lines.push(`\n   Did you mean: ${s}?`);
  return lines.join('\n');
}

export function formatOutOfRange(value: number, range: [number, number], name: string): string {
  return [
    `✗  Value out of range for --${name}: ${value}`,
    `   Expected: between ${range[0]} and ${range[1]}`,
    `   Got: ${value}`,
  ].join('\n');
}

export function formatInvalidPattern(value: string, pattern: string, name: string): string {
  return [
    `✗  Invalid value for --${name}: ${JSON.stringify(value)}`,
    `   Expected: a value matching pattern ${JSON.stringify(pattern)}`,
    `   Got: ${JSON.stringify(value)}`,
  ].join('\n');
}

export function formatTooShort(value: string, minLength: number, name: string): string {
  return [
    `✗  Value too short for --${name}: ${JSON.stringify(value)}`,
    `   Expected: at least ${minLength} character${minLength === 1 ? '' : 's'}`,
    `   Got: ${value.length}`,
  ].join('\n');
}

export function formatTooLong(value: string, maxLength: number, name: string): string {
  return [
    `✗  Value too long for --${name}: ${JSON.stringify(value)}`,
    `   Expected: at most ${maxLength} character${maxLength === 1 ? '' : 's'}`,
    `   Got: ${value.length}`,
  ].join('\n');
}

export function formatMissingCommand(runnablePath: string, availableCommands: string[]): string {
  return [
    `✗  '${runnablePath}' requires a command.`,
    `   Available commands: ${availableCommands.join(', ')}`,
    `\n   Run '${runnablePath} <command> --help' for details.`,
  ].join('\n');
}

export function formatUnknownArg(name: string, knownArgs: string[]): string {
  const lines = [
    `✗  Unknown argument: --${name}`,
    `   Known arguments: ${[...knownArgs].sort().map((a) => `--${a}`).join(', ')}`,
  ];
  const s = suggest(name, knownArgs);
  if (s) lines.push(`\n   Did you mean: --${s}?`);
  return lines.join('\n');
}

export function formatGroupExclusive(groupName: string, provided: string[]): string {
  return [
    `✗  Conflicting arguments in group '${groupName}'`,
    `   --${provided[0]} and --${provided[1]} cannot be used together`,
    `   Choose one or the other`,
  ].join('\n');
}

export function formatGroupInclusive(groupName: string, missing: string[]): string {
  return [
    `✗  Incomplete argument group '${groupName}'`,
    `   Providing one of these args requires all of them`,
    `   Also provide: ${missing.map((m) => `--${m}`).join(' and ')}`,
  ].join('\n');
}

export function formatGroupAtLeastOne(groupName: string, args: string[]): string {
  return [
    `✗  Group '${groupName}' requires at least one argument`,
    `   Provide at least one of: ${args.map((a) => `--${a}`).join(', ')}`,
  ].join('\n');
}

export function formatGroupExactlyOne(groupName: string, args: string[], provided: string[]): string {
  if (!provided.length) {
    return [
      `✗  Group '${groupName}' requires exactly one argument`,
      `   Provide exactly one of: ${args.map((a) => `--${a}`).join(', ')}`,
    ].join('\n');
  }
  return [
    `✗  Group '${groupName}' requires exactly one argument`,
    `   Got ${provided.length}: ${provided.map((a) => `--${a}`).join(', ')}`,
    `   Provide exactly one of: ${args.map((a) => `--${a}`).join(', ')}`,
  ].join('\n');
}

export function formatDeprecated(name: string, message: string): string {
  return `⚠  --${name} is deprecated: ${message}`;
}

function suggest(value: string, candidates: string[]): string | undefined {
  let best: string | undefined;
  let bestScore = 0;
  for (const c of candidates) {
    const score = diceSimilarity(value, c);
    if (score > bestScore && score >= 0.6) {
      bestScore = score;
      best = c;
    }
  }
  return best;
}

function diceSimilarity(a: string, b: string): number {
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const bigrams = new Map<string, number>();
  for (let i = 0; i < a.length - 1; i++) {
    const bg = a.slice(i, i + 2);
    bigrams.set(bg, (bigrams.get(bg) ?? 0) + 1);
  }
  let intersect = 0;
  for (let i = 0; i < b.length - 1; i++) {
    const bg = b.slice(i, i + 2);
    const count = bigrams.get(bg) ?? 0;
    if (count > 0) {
      bigrams.set(bg, count - 1);
      intersect++;
    }
  }
  return (2 * intersect) / (a.length + b.length - 2);
}
