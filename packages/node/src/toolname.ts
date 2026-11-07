/**
 * MCP tool name length: the 128-character limit and a stable way to fit it.
 *
 * Mirrors packages/python/runspec/runspec/toolname.py byte-for-byte: a name over
 * the limit keeps its start (trailing `_`/`-` dropped), then `_` and the first six
 * hex characters of the SHA-1 of the whole name. Never cut silently: cutting can
 * make two tools share a name.
 */
import { createHash } from 'crypto';

/** Longest tool name the MCP specification allows. */
export const MCP_TOOL_NAME_MAX = 128;

const HASH_CHARS = 6;

/** `name` unchanged when it fits `limit`, else a shortened, stable, unique name. */
export function fitToolName(name: string, limit: number = MCP_TOOL_NAME_MAX): string {
  if (name.length <= limit) return name;
  const digest = createHash('sha1').update(Buffer.from(name, 'utf-8')).digest('hex').slice(0, HASH_CHARS);
  const keep = Math.max(limit - HASH_CHARS - 1, 0);
  const head = name.slice(0, keep).replace(/[_-]+$/, '');
  return head ? `${head}_${digest}` : digest.slice(0, limit);
}

/** One line explaining that `name` was shortened to `fitted`. */
export function tooLongMessage(name: string, fitted: string, limit: number = MCP_TOOL_NAME_MAX): string {
  return `tool name '${name}' is ${name.length} characters, over the MCP limit of ${limit}; serving it as '${fitted}'. Shorten the runnable or command names to keep the full name.`;
}
