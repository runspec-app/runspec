import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import * as readline from 'readline';
import * as path from 'path';

const VALID_BIN_NAMES = new Set(['runspec', 'runspec.exe']);

export function resolveBinRaw(binFlag: string | undefined): string {
  return binFlag ?? process.env['RUNSPEC_JUMP_BIN'] ?? 'runspec';
}

export function resolveBin(binFlag: string | undefined): string {
  const binPath = resolveBinRaw(binFlag);
  validateBinPath(binPath);
  return binPath;
}

function validateBinPath(binPath: string): void {
  const name = path.basename(binPath);
  if (!VALID_BIN_NAMES.has(name)) {
    process.stderr.write(
      `✗  Jump \`bin\` must point at a runspec executable.\n` +
        `   Got: ${JSON.stringify(binPath)} (basename ${JSON.stringify(name)})\n` +
        `   Expected basename: 'runspec' (or 'runspec.exe' on Windows).\n` +
        `   This field is locked to the runspec CLI; it cannot be redirected.\n`,
    );
    process.exit(1);
  }
}

export function sshCmd(host: string, binPath: string): string[] {
  return ['ssh', '-o', 'BatchMode=yes', host, binPath, 'serve'];
}

interface Session {
  send: (msg: Record<string, unknown>) => void;
  recv: () => Promise<Record<string, unknown>>;
  close: () => void;
  proc: ChildProcess;
  binPath: string;
}

function openSession(host: string, binPath: string): Session {
  const cmd = sshCmd(host, binPath);
  const proc = spawn(cmd[0], cmd.slice(1), { stdio: ['pipe', 'pipe', 'inherit'] });

  proc.on('error', (err: NodeJS.ErrnoException) => {
    if (err.code === 'ENOENT') {
      process.stderr.write('✗  ssh not found — install OpenSSH\n');
    } else {
      process.stderr.write(`✗  Failed to launch ssh: ${err.message}\n`);
    }
    process.exit(1);
  });

  const rl = readline.createInterface({ input: proc.stdout!, crlfDelay: Infinity });
  const iter = rl[Symbol.asyncIterator]();

  const send = (msg: Record<string, unknown>): void => {
    proc.stdin!.write(JSON.stringify(msg) + '\n');
  };

  const recv = async (): Promise<Record<string, unknown>> => {
    const { value, done } = await iter.next();
    if (done) {
      await reportRemoteFailure(proc, binPath);
      throw new Error('unreachable');
    }
    return JSON.parse(value as string) as Record<string, unknown>;
  };

  const close = (): void => {
    rl.close();
    if (proc.stdin) proc.stdin.destroy();
  };

  return { send, recv, close, proc, binPath };
}

async function reportRemoteFailure(proc: ChildProcess, binPath: string): Promise<never> {
  const exitCode =
    proc.exitCode ??
    (await new Promise<number | null>((resolve) => {
      const timer = setTimeout(() => resolve(null), 1000);
      proc.once('exit', (code) => {
        clearTimeout(timer);
        resolve(code);
      });
    }));

  if (exitCode === 255) {
    process.stderr.write('✗  SSH connection failed (see error above for details).\n');
  } else if (exitCode !== null && exitCode !== 0) {
    const prefix = `✗  Remote command failed (exit ${exitCode}) before the MCP handshake completed.\n`;
    if (binPath.includes('/')) {
      process.stderr.write(
        prefix +
          "   If the error above doesn't explain it, verify the path exists on the remote:\n" +
          `     ${binPath}\n` +
          '   Common causes:\n' +
          '     - the venv path differs between local and remote\n' +
          "     - runspec isn't installed in that venv on the remote\n" +
          '     - typo in the --bin / RUNSPEC_JUMP_BIN value\n',
      );
    } else {
      process.stderr.write(
        prefix +
          `   \`${binPath}\` is not on the remote shell's PATH.\n` +
          '   Fix: pass --bin /full/path/to/runspec to runspec jump,\n' +
          '   or export RUNSPEC_JUMP_BIN in your local shell.\n' +
          "   (SSH commands run in a non-login shell and don't source ~/.bashrc / ~/.profile.)\n",
      );
    }
  } else {
    process.stderr.write('✗  Remote MCP server closed stdout unexpectedly\n');
  }
  process.exit(1);
}

async function initialize(session: Session): Promise<void> {
  session.send({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'runspec-jump', version: '1.0' },
    },
  });
  await session.recv();
  session.send({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} });
}

export async function listTools(host: string, binPath: string): Promise<Array<Record<string, unknown>>> {
  const session = openSession(host, binPath);
  try {
    await initialize(session);
    session.send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
    const resp = await session.recv();
    return ((resp['result'] as Record<string, unknown>)?.['tools'] as Array<Record<string, unknown>>) ?? [];
  } finally {
    session.close();
  }
}

export async function callTool(host: string, binPath: string, toolName: string, toolArgv: string[]): Promise<void> {
  const session = openSession(host, binPath);
  try {
    await initialize(session);
    session.send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} });
    const toolsResp = await session.recv();
    const tools =
      ((toolsResp['result'] as Record<string, unknown>)?.['tools'] as Array<Record<string, unknown>>) ?? [];
    const schema = tools.find((t) => t['name'] === toolName);
    if (!schema) {
      process.stderr.write(`✗  Tool '${toolName}' not found on remote\n`);
      process.exit(1);
    }

    const arguments_ = parseToolArgv(toolArgv, schema);
    session.send({
      jsonrpc: '2.0',
      id: 3,
      method: 'tools/call',
      params: { name: toolName, arguments: arguments_ },
    });
    const callResp = await session.recv();

    if ('error' in callResp) {
      const err = callResp['error'] as Record<string, unknown>;
      process.stderr.write(`✗  ${(err['message'] as string | undefined) ?? 'Remote error'}\n`);
      process.exit(1);
    }

    const result = (callResp['result'] as Record<string, unknown>) ?? {};
    for (const block of (result['content'] as Array<Record<string, unknown>>) ?? []) {
      if (block['type'] === 'text') {
        const text = block['text'] as string;
        process.stdout.write(text);
        if (!text.endsWith('\n')) process.stdout.write('\n');
      }
    }

    if (result['isError']) {
      process.exit(1);
    }
  } finally {
    session.close();
  }
}

export function parseToolArgv(argv: string[], schema: Record<string, unknown>): Record<string, unknown> {
  const props =
    ((schema['inputSchema'] as Record<string, unknown>)?.['properties'] as Record<
      string,
      Record<string, unknown>
    >) ?? {};
  const result: Record<string, unknown> = {};
  let i = 0;
  while (i < argv.length) {
    const token = argv[i];
    if (!token.startsWith('--')) {
      i++;
      continue;
    }
    const argName = token.slice(2);
    const prop = props[argName] ?? {};
    if (prop['type'] === 'boolean') {
      result[argName] = true;
      i++;
    } else if (i + 1 < argv.length) {
      result[argName] = argv[i + 1];
      i += 2;
    } else {
      process.stderr.write(`✗  --${argName} requires a value\n`);
      process.exit(1);
    }
  }
  return result;
}
