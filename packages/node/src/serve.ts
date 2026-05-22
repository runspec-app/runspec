import * as fs from 'fs';
import * as path from 'path';
import * as readline from 'readline';
import { spawnSync } from 'child_process';
import { findConfig } from './finder';
import { loadRaw } from './loader';
import { inferScript } from './inference';
import { buildSchema } from './cli';

const MCP_PROTOCOL_VERSION = '2024-11-05';
const ERR_PARSE = -32700;
const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_PARAMS = -32602;

const SHELL_EXTS = ['', '.sh', '.ksh', '.bash', '.zsh'];

export function serve(): void {
  let configPath: string;

  try {
    ({ configPath } = findConfig(process.cwd()));
  } catch (e) {
    process.stderr.write(`runspec serve: ${(e as Error).message}\n`);
    process.exit(1);
  }

  const raw = loadRaw(configPath);
  const config = raw.config;

  const tools: Record<string, Record<string, unknown>> = {};
  const argSpecs: Record<string, Record<string, unknown>> = {};
  const execSpecs: Record<string, { command: string | null }> = {};

  const binDir = path.join(path.dirname(configPath), 'node_modules', '.bin');

  for (const [name, runnable] of Object.entries(raw.runnables)) {
    const inferred = inferScript(runnable, config.autonomyDefault);
    tools[name] = buildSchema(name, inferred, 'mcp');
    argSpecs[name] = inferred.args ?? {};
    execSpecs[name] = { command: findScript(name, binDir) };
  }

  const serverName = serverNameFromConfig(config as unknown as Record<string, unknown>);

  mcpLoop(tools, argSpecs, execSpecs, serverName);
}

function mcpLoop(
  tools: Record<string, Record<string, unknown>>,
  argSpecs: Record<string, Record<string, unknown>>,
  execSpecs: Record<string, { command: string | null }>,
  serverName: string,
): void {
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    let request: Record<string, unknown>;
    try {
      request = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      writeMsg({ jsonrpc: '2.0', id: null, error: { code: ERR_PARSE, message: 'Parse error' } });
      return;
    }

    const response = dispatch(request, tools, argSpecs, execSpecs, serverName);
    if (response !== null) writeMsg(response);
  });
}

function dispatch(
  request: Record<string, unknown>,
  tools: Record<string, Record<string, unknown>>,
  argSpecs: Record<string, Record<string, unknown>>,
  execSpecs: Record<string, { command: string | null }>,
  serverName: string,
): Record<string, unknown> | null {
  const method = (request['method'] as string) ?? '';
  const reqId = request['id'];

  if (reqId === undefined || reqId === null) return null;

  if (method === 'initialize') return handleInitialize(reqId, serverName);
  if (method === 'tools/list') return handleToolsList(reqId, tools);
  if (method === 'tools/call') {
    return handleToolsCall(reqId, (request['params'] ?? {}) as Record<string, unknown>, tools, argSpecs, execSpecs);
  }

  return { jsonrpc: '2.0', id: reqId, error: { code: ERR_METHOD_NOT_FOUND, message: `Method not found: ${method}` } };
}

function handleInitialize(reqId: unknown, serverName: string): Record<string, unknown> {
  const version = '0.6.0';
  return {
    jsonrpc: '2.0',
    id: reqId,
    result: {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: { tools: {} },
      serverInfo: { name: serverName, version },
    },
  };
}

function handleToolsList(reqId: unknown, tools: Record<string, Record<string, unknown>>): Record<string, unknown> {
  return { jsonrpc: '2.0', id: reqId, result: { tools: Object.values(tools) } };
}

function handleToolsCall(
  reqId: unknown,
  params: Record<string, unknown>,
  tools: Record<string, Record<string, unknown>>,
  argSpecs: Record<string, Record<string, unknown>>,
  execSpecs: Record<string, { command: string | null }>,
): Record<string, unknown> {
  const name = (params['name'] as string) ?? '';
  const args = (params['arguments'] as Record<string, unknown>) ?? {};

  if (!(name in tools)) {
    return { jsonrpc: '2.0', id: reqId, error: { code: ERR_INVALID_PARAMS, message: `Unknown tool: ${name}` } };
  }

  const cmd = execSpecs[name]?.command ?? null;
  if (!cmd) {
    return {
      jsonrpc: '2.0',
      id: reqId,
      result: {
        content: [{ type: 'text', text: `Script '${name}' not found. Place it alongside runspec.toml or in a bin/ subdirectory.` }],
        isError: true,
      },
    };
  }

  const toolArgSpecs = argSpecs[name] ?? {};
  const argv = argsToArgv(args, toolArgSpecs);
  const runspecEnv = argsToRunspecEnv(args, toolArgSpecs);
  const env = { ...process.env, RUNSPEC_AGENT: '1', ...runspecEnv };

  const start = process.hrtime.bigint();
  const result = spawnSync(cmd, argv, { encoding: 'utf-8', env });
  const durationMs = Number((process.hrtime.bigint() - start) / 1_000_000n);

  // _meta is the MCP-standard extension point; clients that don't understand
  // it ignore the block. Same envelope on success and failure so callers can
  // rely on it being present.
  const meta = {
    runspec: { tool: name, duration_ms: durationMs, exit_code: result.status ?? null },
  };

  if (result.status === 0) {
    return {
      jsonrpc: '2.0',
      id: reqId,
      result: {
        content: [{ type: 'text', text: result.stdout ?? '' }],
        isError: false,
        _meta: meta,
      },
    };
  }

  const parts = [`exit_code: ${result.status ?? 'unknown'}`];
  if (result.stdout) parts.push(`stdout:\n${result.stdout.trimEnd()}`);
  if (result.stderr) parts.push(`stderr:\n${result.stderr.trimEnd()}`);

  return {
    jsonrpc: '2.0',
    id: reqId,
    result: {
      content: [{ type: 'text', text: parts.join('\n') }],
      isError: true,
      _meta: meta,
    },
  };
}

function argsToArgv(args: Record<string, unknown>, argSpecs: Record<string, unknown>): string[] {
  const argv: string[] = [];

  for (const [argName, spec] of Object.entries(argSpecs)) {
    const s = spec as Record<string, unknown>;
    const value = args[argName] ?? args[argName.replace(/-/g, '_')];
    if (value === null || value === undefined) continue;

    const flag = `--${argName}`;
    const argType = (s['type'] as string) ?? 'str';

    if (argType === 'flag') {
      if (value) argv.push(flag);
    } else if (s['multiple'] && Array.isArray(value)) {
      for (const item of value) argv.push(flag, String(item));
    } else {
      argv.push(flag, String(value));
    }
  }

  return argv;
}

function argsToRunspecEnv(args: Record<string, unknown>, argSpecs: Record<string, unknown>): Record<string, string> {
  const env: Record<string, string> = {};

  for (const [argName, spec] of Object.entries(argSpecs)) {
    const s = spec as Record<string, unknown>;
    let value = args[argName] ?? args[argName.replace(/-/g, '_')];
    if (value === null || value === undefined) value = s['default'];
    if (value === null || value === undefined) continue;

    const envKey = 'RUNSPEC_ARG_' + argName.toUpperCase().replace(/-/g, '_').replace(/\./g, '_');
    const argType = (s['type'] as string) ?? 'str';

    if (argType === 'flag' || argType === 'bool') {
      env[envKey] = value ? '1' : '0';
    } else if (s['multiple'] && Array.isArray(value)) {
      env[envKey] = (value as unknown[]).map(String).join('\n');
    } else {
      env[envKey] = String(value);
    }
  }

  return env;
}

function findScript(name: string, binDir: string): string | null {
  // 1. node_modules/.bin (Node entry points; also .exe on Windows)
  for (const ext of [...SHELL_EXTS, '.exe']) {
    const candidate = path.join(binDir, name + ext);
    if (fs.existsSync(candidate)) return candidate;
  }

  // 2. cwd/ and cwd/bin/
  const cwd = process.cwd();
  for (const dir of [cwd, path.join(cwd, 'bin')]) {
    for (const ext of SHELL_EXTS) {
      const candidate = path.join(dir, name + ext);
      if (fs.existsSync(candidate)) return candidate;
    }
  }

  return null;
}

function serverNameFromConfig(config: Record<string, unknown>): string {
  const name = config['name'];
  if (name && typeof name === 'string') return name;
  return path.basename(path.dirname(process.execPath));
}

function writeMsg(response: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify(response) + '\n');
}
