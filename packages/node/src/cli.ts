import * as fs from 'fs';
import * as path from 'path';
import * as readline from 'readline';
import { findConfig } from './finder';
import { loadRaw } from './loader';
import { inferScript } from './inference';
import { parse } from './parser';
import type { ScriptSpec, ArgSpec } from './models';

const _CLI_CONFIG = path.join(__dirname, 'runspec.toml');

// ── Entry point ───────────────────────────────────────────────────────────────

export function main(): void {
  const args = process.argv.slice(2);

  const command = args[0];
  const rest = args.slice(1);

  // Per-subcommand help
  const preHelpArgs = args.includes('--') ? args.slice(0, args.indexOf('--')) : args;
  if (command && command !== '-h' && command !== '--help' && (preHelpArgs.includes('-h') || preHelpArgs.includes('--help'))) {
    printCommandHelp(command);
    return;
  }

  if (!args.length || command === '-h' || command === '--help') {
    printHelp();
    return;
  }

  const commands: Record<string, (args: string[]) => void | Promise<void>> = {
    init: cmdInit,
    local: cmdLocal,
    jump: cmdJump,
    serve: cmdServe,
  };

  if (!(command in commands)) {
    console.log(`✗  Unknown command: ${command}`);
    console.log(`   Available commands: ${Object.keys(commands).join(', ')}`);
    process.exit(1);
  }

  const result = commands[command](rest);
  if (result instanceof Promise) {
    result.catch((err: unknown) => {
      process.stderr.write(`✗  ${err instanceof Error ? err.message : String(err)}\n`);
      process.exit(1);
    });
  }
}

// ── Commands ──────────────────────────────────────────────────────────────────

function cmdInit(args: string[]): void {
  const nameFlag = getFlag(args, '--name');
  const langFlag = getFlag(args, '--lang') ?? 'typescript';
  const example = args.includes('--example');

  const cwd = process.cwd();
  const runspecToml = path.join(cwd, 'runspec.toml');

  if (example) {
    if (nameFlag) {
      console.log('  ℹ  --name is ignored with --example (runnables are always clean and scan)');
    }
    initExampleToml(runspecToml);
    initExampleStubs(cwd, langFlag);
    printNextSteps(cwd, true);
    return;
  }

  const runnableName = nameFlag ?? sanitizeName(path.basename(cwd));
  initRunspecToml(runspecToml, runnableName);
  initCodeStub(cwd, runnableName, langFlag);
  printNextSteps(cwd, false);
}

function cmdLocal(args: string[]): void {
  const fmt = getFlag(args, '--format') ?? 'text';
  const scriptName = getFlag(args, '--script');

  const discovered = discoverLocal();

  if (!discovered.length) {
    console.log('No runspec-aware runnables found in this environment.');
    console.log("Create a runspec.toml inside your package directory and run 'runspec init' to get started.");
    return;
  }

  // Filter to single runnable if --script given
  const filtered = scriptName
    ? discovered.filter((d) => d.runnable === scriptName)
    : discovered;

  if (scriptName && !filtered.length) {
    console.log(`✗  Runnable '${scriptName}' not found`);
    process.exit(1);
  }

  if (fmt === 'text') {
    printLocalText(filtered);
  } else if (fmt === 'json') {
    console.log(JSON.stringify(filtered, null, 2));
  } else if (['mcp', 'openai', 'anthropic'].includes(fmt)) {
    console.log(JSON.stringify(emitAll(filtered, fmt), null, 2));
  } else {
    console.log(`✗  Unknown format: ${fmt}`);
    console.log('   Available formats: text, json, mcp, openai, anthropic');
    process.exit(1);
  }
}

async function cmdJump(args: string[]): Promise<void> {
  const parsed = parse({ scriptName: 'runspec', argv: ['jump', ...args], configPath: _CLI_CONFIG });
  const fmt = String(parsed['format'] ?? 'text');

  const host = parsed['host'] as string | undefined;
  if (!host) {
    process.stderr.write('✗  A host is required\n');
    process.stderr.write('   Usage: runspec jump <host> [<tool>] [-- tool-args...]\n');
    process.stderr.write('   Example: runspec jump user@prod.example.com\n');
    process.exit(1);
  }

  const binFlag = parsed['bin'] as string | undefined;
  const toolName = parsed['tool'] as string | undefined;
  const toolArgv = (parsed['tool_args'] as string[] | undefined) ?? [];

  const { resolveBin, listTools, callTool } = await import('./jump');
  const binPath = resolveBin(binFlag);

  if (!toolName) {
    const tools = await listTools(host, binPath);
    if (!tools.length) {
      console.log(`No tools found on ${host}.`);
      return;
    }
    if (fmt === 'json') {
      console.log(JSON.stringify(tools, null, 2));
      return;
    }
    console.log(`Tools on ${host}:\n`);
    for (const tool of tools) {
      const name = String(tool['name'] ?? '').padEnd(24);
      const desc = String(tool['description'] ?? '');
      console.log(`  ${name} ${desc}`);
    }
    return;
  }

  await callTool(host, binPath, toolName, toolArgv);
}

function cmdServe(_args: string[]): void {
  if (process.stdin.isTTY) {
    console.log('runspec serve is an MCP stdio server — it is not run directly from a terminal.');
    console.log('Configure it as an MCP server in your MCP host (Claude Desktop, VS Code, PyCharm, etc.)');
    console.log();
    console.log('To test manually:');
    console.log('  echo \'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\' | runspec serve');
    return;
  }
  const { serve } = require('./serve') as { serve: () => void };
  serve();
}

// ── Schema builder ────────────────────────────────────────────────────────────

export function buildSchema(name: string, script: ScriptSpec, fmt: string): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const requiredArgs: string[] = [];

  for (const [argName, arg] of Object.entries(script.args ?? {})) {
    properties[argName] = argToJsonSchema(arg);
    if (arg.required) requiredArgs.push(argName);
  }

  const schema: Record<string, unknown> = {
    name,
    description: script.description ?? '',
    'x-autonomy': script.autonomy ?? 'confirm',
    'x-output': script.output ?? 'text',
    inputSchema: { type: 'object', properties },
  };

  if (requiredArgs.length) (schema['inputSchema'] as Record<string, unknown>)['required'] = requiredArgs;
  if (script.autonomyReason) schema['x-autonomy-reason'] = script.autonomyReason;

  return schema;
}

function argToJsonSchema(arg: ArgSpec): Record<string, unknown> {
  const typeMap: Record<string, string> = {
    str: 'string',
    int: 'integer',
    float: 'number',
    bool: 'boolean',
    flag: 'boolean',
    path: 'string',
    choice: 'string',
  };

  let prop: Record<string, unknown> = { type: typeMap[arg.type ?? 'str'] ?? 'string' };
  if (arg.description) prop['description'] = arg.description;
  if (arg.default !== undefined && arg.default !== null) prop['default'] = arg.default;
  if (arg.options) prop['enum'] = arg.options;
  if (arg.range) {
    prop['minimum'] = arg.range[0];
    prop['maximum'] = arg.range[1];
  }
  if (arg.multiple) prop = { type: 'array', items: prop };

  return prop;
}

// ── Discovery ─────────────────────────────────────────────────────────────────

function discoverLocal(): Array<{ source: string; runnable: string; spec: ScriptSpec }> {
  try {
    const { configPath } = findConfig(process.cwd());
    const raw = loadRaw(configPath);
    return Object.entries(raw.runnables).map(([name, spec]) => ({ source: configPath, runnable: name, spec }));
  } catch {
    return [];
  }
}

function emitAll(discovered: Array<{ source: string; runnable: string; spec: ScriptSpec }>, fmt: string): Record<string, unknown> {
  const tools = discovered.map((item) => buildSchema(item.runnable, item.spec, fmt));
  if (fmt === 'mcp') return { tools };
  return Object.fromEntries(tools.map((t) => [t['name'], t]));
}

function printLocalText(discovered: Array<{ source: string; runnable: string; spec: ScriptSpec }>): void {
  const bySource: Record<string, Array<{ runnable: string; spec: ScriptSpec }>> = {};
  for (const item of discovered) {
    (bySource[item.source] ??= []).push({ runnable: item.runnable, spec: item.spec });
  }

  const warnings: string[] = [];
  const errors: string[] = [];

  console.log(`Found ${discovered.length} runspec runnable(s):\n`);
  for (const [source, items] of Object.entries(bySource)) {
    console.log(`  ${source}`);
    for (const { runnable: name, spec } of items) {
      const desc = spec.description ?? '';
      const autonomy = spec.autonomy ?? 'confirm';
      const truncated = desc.length > 48 ? desc.slice(0, 48) : desc;
      console.log(`    ${name.padEnd(24)} ${truncated.padEnd(50)}  [${autonomy}]`);

      if (!spec.description) warnings.push(`'${name}' has no description — agents won't know what it does`);
      if (!spec.autonomy) warnings.push(`'${name}' autonomy not declared — defaulting to 'confirm'`);
      for (const [argName, arg] of Object.entries(spec.args ?? {})) {
        if (!arg.description && arg.required) {
          warnings.push(`'${name}.${argName}' is required but has no description`);
        }
      }
    }
    console.log();
  }

  if (warnings.length || errors.length) {
    console.log('Issues:\n');
    for (const msg of warnings) console.log(`  ℹ  ${msg}`);
    for (const msg of errors) console.log(`  ✗  ${msg}`);
    console.log();
  }

  console.log("Run 'runspec local --format mcp' to emit MCP tool schemas.");
}

// ── Init helpers ──────────────────────────────────────────────────────────────

function sanitizeName(raw: string): string {
  const s = raw.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return s || 'myscript';
}

function initRunspecToml(filePath: string, name: string): void {
  if (fs.existsSync(filePath)) {
    console.log(`✗  ${path.basename(filePath)} already exists — already initialized`);
    console.log(`   Edit ${path.basename(filePath)} directly to add more runnables.`);
    process.exit(1);
  }
  const content = `#:schema https://raw.githubusercontent.com/JasonFinestone/runspec/main/schema/runspec.schema.json\n\n[${name}]\ndescription = "Describe what ${name} does"\nautonomy    = "confirm"\n\n[${name}.args]\n# example = {type = "str", description = "An example argument"}\n`;
  writeAndVerify(filePath, content, null);
  console.log(`  ✓  Created runspec.toml with [${name}] runnable`);
}

function initExampleToml(filePath: string): void {
  if (fs.existsSync(filePath)) {
    console.log(`✗  ${path.basename(filePath)} already exists — already initialized`);
    console.log(`   Edit ${path.basename(filePath)} directly to add more runnables.`);
    process.exit(1);
  }
  const content = [
    '#:schema https://raw.githubusercontent.com/JasonFinestone/runspec/main/schema/runspec.schema.json',
    '',
    '[clean]',
    'description = "Find and optionally delete stale temporary files in a directory"',
    'autonomy    = "confirm"',
    '',
    '[clean.args]',
    'directory  = {type = "path",   description = "Directory to scan",                            default = "."}',
    'pattern    = {type = "str",    description = "Glob pattern to match",                        default = "*.tmp"}',
    'older_than = {type = "int",    description = "Only match files older than N days",           default = 7}',
    'format     = {type = "choice", description = "Output format", options = ["text", "json"],    default = "text"}',
    'delete     = {type = "flag",   description = "Delete matched files (asks for confirmation)", default = false}',
    '',
    '[scan]',
    'description = "Scan for stale temporary files and report what clean would delete"',
    'autonomy    = "autonomous"',
    'output      = "json"',
    '',
    '[scan.args]',
    'directory  = {type = "path", description = "Directory to scan",                default = "."}',
    'pattern    = {type = "str",  description = "Glob pattern to match",             default = "*.tmp"}',
    'older_than = {type = "int",  description = "Only match files older than N days", default = 7}',
    '',
  ].join('\n');
  writeAndVerify(filePath, content, null);
  console.log('  ✓  Created runspec.toml with [clean] and [scan] runnables');
}

function initExampleStubs(dir: string, lang: string): void {
  if (lang === 'typescript') {
    writeStubIfMissing(path.join(dir, 'clean.ts'), CLEAN_TS_STUB);
    writeStubIfMissing(path.join(dir, 'scan.ts'), SCAN_TS_STUB);
  } else if (lang === 'javascript') {
    writeStubIfMissing(path.join(dir, 'clean.js'), CLEAN_JS_STUB);
    writeStubIfMissing(path.join(dir, 'scan.js'), SCAN_JS_STUB);
  } else if (lang === 'python') {
    writeStubIfMissing(path.join(dir, 'clean.py'), CLEAN_PY_STUB);
    writeStubIfMissing(path.join(dir, 'scan.py'), SCAN_PY_STUB);
  } else {
    console.log(`✗  Unknown --lang: ${lang}`);
    console.log('   Supported: typescript (default), javascript, python');
    process.exit(1);
  }
}

function writeStubIfMissing(filePath: string, content: string): void {
  if (fs.existsSync(filePath)) {
    console.log(`  ℹ  ${path.basename(filePath)} already exists — skipped`);
  } else {
    fs.writeFileSync(filePath, content, 'utf-8');
    console.log(`  ✓  Created ${path.basename(filePath)}`);
  }
}

function initCodeStub(dir: string, name: string, lang: string): void {
  const templates: Record<string, { ext: string; content: string }> = {
    typescript: { ext: '.ts', content: `import { parse } from 'runspec-node';\n\nfunction main(): void {\n  const args = parse();\n  // your logic here\n}\n\nmain();\n` },
    javascript: { ext: '.js', content: `const { parse } = require('runspec-node');\n\nfunction main() {\n  const args = parse();\n  // your logic here\n}\n\nmain();\n` },
    python:     { ext: '.py', content: `from runspec import parse\n\n\ndef main():\n    args = parse()\n    # your logic here\n\n\nif __name__ == "__main__":\n    main()\n` },
  };

  const template = templates[lang];
  if (!template) {
    console.log(`✗  Unknown --lang: ${lang}`);
    console.log('   Supported: typescript, javascript, python');
    process.exit(1);
  }

  const filePath = path.join(dir, name + template.ext);
  writeStubIfMissing(filePath, template.content);
}

function printNextSteps(cwd: string, example: boolean): void {
  const pkgJson = path.join(cwd, 'package.json');
  const hasPkg = fs.existsSync(pkgJson);

  console.log('\nNext steps:\n');
  if (!hasPkg) {
    console.log('  1. npm init -y');
    console.log('  2. npm install runspec-node');
  } else {
    console.log('  1. npm install runspec-node');
  }
  console.log(`  ${hasPkg ? '2' : '3'}. runspec local`);

  if (example) {
    console.log('\nDemo prep — pre-date some files to trigger the example:');
    console.log('  touch -t 202401010000 report.tmp cache.tmp session.tmp');
    console.log('\nThen try:');
    console.log('  npx ts-node scan.ts                 # agent-ready JSON output');
    console.log('  npx ts-node clean.ts --delete       # prompts for confirmation');
    console.log('  npx ts-node clean.ts --format json  # structured output');
  }
}

function writeAndVerify(filePath: string, content: string, original: string | null): void {
  fs.writeFileSync(filePath, content, 'utf-8');
  try {
    const { parse } = require('smol-toml') as { parse: (s: string) => unknown };
    parse(content);
  } catch (e) {
    if (original !== null) fs.writeFileSync(filePath, original, 'utf-8');
    else fs.unlinkSync(filePath);
    console.log('✗  Generated invalid TOML — this is a bug, please report it');
    console.log(`   ${(e as Error).message}`);
    process.exit(1);
  }
}

// ── Example stubs ─────────────────────────────────────────────────────────────

const CLEAN_TS_STUB = `import * as fs from 'fs';
import * as path from 'path';
import * as readline from 'readline';
import { parse } from 'runspec-node';

async function main(): Promise<void> {
  const args = parse();

  const cutoff = Date.now() - Number(args.older_than) * 86400 * 1000;
  const dir = String(args.directory);
  const pattern = String(args.pattern).replace('*', '');
  const matches = fs.readdirSync(dir)
    .map((f) => path.join(dir, f))
    .filter((f) => f.endsWith(pattern) && fs.statSync(f).isFile() && fs.statSync(f).mtimeMs < cutoff);

  if (!matches.length) {
    console.log(\`No '\${args.pattern}' files older than \${args.older_than} days found in \${args.directory}.\`);
    return;
  }

  if (String(args.format) === 'json') {
    const data = matches.map((f) => ({ path: f, size: fs.statSync(f).size, days_old: Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000) }));
    console.log(JSON.stringify(data, null, 2));
  } else {
    console.log(\`Found \${matches.length} file(s) matching '\${args.pattern}' older than \${args.older_than} days:\`);
    console.log();
    for (const f of matches) {
      const days = Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000);
      console.log(\`  \${f}  (\${fs.statSync(f).size.toLocaleString()} bytes, \${days}d old)\`);
    }
  }

  if (args.delete) {
    if (!args.__runspec_agent__) {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      const answer = await new Promise<string>((resolve) => rl.question(\`\\nDelete \${matches.length} file(s)? [y/N] \`, resolve));
      rl.close();
      if (answer.trim().toLowerCase() !== 'y') {
        console.log('Aborted.');
        return;
      }
    }
    for (const f of matches) fs.unlinkSync(f);
    console.log(\`\\nDeleted \${matches.length} file(s).\`);
  }
}

main();
`;

const SCAN_TS_STUB = `import * as fs from 'fs';
import * as path from 'path';
import { parse } from 'runspec-node';

function main(): void {
  const args = parse();

  const cutoff = Date.now() - Number(args.older_than) * 86400 * 1000;
  const dir = String(args.directory);
  const pattern = String(args.pattern).replace('*', '');
  const matches = fs.readdirSync(dir)
    .map((f) => path.join(dir, f))
    .filter((f) => f.endsWith(pattern) && fs.statSync(f).isFile() && fs.statSync(f).mtimeMs < cutoff);

  const data = matches.map((f) => ({ path: f, size: fs.statSync(f).size, days_old: Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000) }));
  console.log(JSON.stringify(data, null, 2));
}

main();
`;

const CLEAN_JS_STUB = `const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { parse } = require('runspec-node');

async function main() {
  const args = parse();

  const cutoff = Date.now() - Number(args.older_than) * 86400 * 1000;
  const dir = String(args.directory);
  const pattern = String(args.pattern).replace('*', '');
  const matches = fs.readdirSync(dir)
    .map((f) => path.join(dir, f))
    .filter((f) => f.endsWith(pattern) && fs.statSync(f).isFile() && fs.statSync(f).mtimeMs < cutoff);

  if (!matches.length) {
    console.log(\`No '\${args.pattern}' files older than \${args.older_than} days found in \${args.directory}.\`);
    return;
  }

  if (String(args.format) === 'json') {
    const data = matches.map((f) => ({ path: f, size: fs.statSync(f).size, days_old: Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000) }));
    console.log(JSON.stringify(data, null, 2));
  } else {
    console.log(\`Found \${matches.length} file(s) matching '\${args.pattern}' older than \${args.older_than} days:\`);
    for (const f of matches) {
      const days = Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000);
      console.log(\`  \${f}  (\${fs.statSync(f).size.toLocaleString()} bytes, \${days}d old)\`);
    }
  }

  if (args.delete) {
    if (!args.__runspec_agent__) {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      const answer = await new Promise((resolve) => rl.question(\`\\nDelete \${matches.length} file(s)? [y/N] \`, resolve));
      rl.close();
      if (answer.trim().toLowerCase() !== 'y') { console.log('Aborted.'); return; }
    }
    for (const f of matches) fs.unlinkSync(f);
    console.log(\`\\nDeleted \${matches.length} file(s).\`);
  }
}

main();
`;

const SCAN_JS_STUB = `const fs = require('fs');
const path = require('path');
const { parse } = require('runspec-node');

function main() {
  const args = parse();

  const cutoff = Date.now() - Number(args.older_than) * 86400 * 1000;
  const dir = String(args.directory);
  const pattern = String(args.pattern).replace('*', '');
  const matches = fs.readdirSync(dir)
    .map((f) => path.join(dir, f))
    .filter((f) => f.endsWith(pattern) && fs.statSync(f).isFile() && fs.statSync(f).mtimeMs < cutoff);

  const data = matches.map((f) => ({ path: f, size: fs.statSync(f).size, days_old: Math.floor((Date.now() - fs.statSync(f).mtimeMs) / 86400000) }));
  console.log(JSON.stringify(data, null, 2));
}

main();
`;

const CLEAN_PY_STUB = `import json
import sys
import time

from runspec import parse


def main():
    args = parse()

    cutoff = time.time() - args.older_than * 86400
    matches = [p for p in args.directory.glob(args.pattern) if p.is_file() and p.stat().st_mtime < cutoff]

    if not matches:
        print(f"No '{args.pattern}' files older than {args.older_than} days found in {args.directory}.")
        return

    if args.format == "json":
        data = [{"path": str(p), "size": p.stat().st_size, "days_old": int((time.time() - p.stat().st_mtime) / 86400)} for p in matches]
        print(json.dumps(data, indent=2))
    else:
        print(f"Found {len(matches)} file(s) matching '{args.pattern}' older than {args.older_than} days:")
        print()
        for p in matches:
            days = int((time.time() - p.stat().st_mtime) / 86400)
            print(f"  {p}  ({p.stat().st_size:,} bytes, {days}d old)")

    if args.delete:
        if not args.__runspec_agent__:
            print()
            confirm = input(f"Delete {len(matches)} file(s)? [y/N] ")
            if confirm.strip().lower() != "y":
                print("Aborted.")
                return
        for p in matches:
            p.unlink()
        print()
        print(f"Deleted {len(matches)} file(s).")


if __name__ == "__main__":
    main()
`;

const SCAN_PY_STUB = `import json
import time

from runspec import parse


def main():
    args = parse()

    cutoff = time.time() - args.older_than * 86400
    matches = [p for p in args.directory.glob(args.pattern) if p.is_file() and p.stat().st_mtime < cutoff]

    data = [{"path": str(p), "size": p.stat().st_size, "days_old": int((time.time() - p.stat().st_mtime) / 86400)} for p in matches]
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
`;

// ── Arg parser helper ─────────────────────────────────────────────────────────

function getFlag(args: string[], flag: string): string | undefined {
  const idx = args.indexOf(flag);
  if (idx !== -1 && idx + 1 < args.length) return args[idx + 1];
  return undefined;
}

// ── Help ──────────────────────────────────────────────────────────────────────

function printHelp(): void {
  console.log(`runspec — interface specification for anything runnable

Usage:
  runspec <command> [options]

Commands:
  init        Create runspec.toml and a code stub
  local       List runnables and emit tool schemas
  jump        Execute a runnable on a remote host via SSH
  serve       Start the MCP stdio server for local runnables

Run 'runspec <command> --help' for focused help on each command.

Examples:
  runspec init
  runspec init --example
  runspec local
  runspec local --format mcp
  runspec serve`);
}

function printCommandHelp(command: string): void {
  const help: Record<string, string> = {
    init: `runspec init — Create runspec.toml and a code stub

Options:
  --name      Runnable name (default: current directory name)
  --lang      Language for stub: typescript (default), javascript, python
  --example   Generate dual clean + scan example runnables

Examples:
  runspec init
  runspec init --name myapp
  runspec init --name myapp --lang javascript
  runspec init --example`,

    local: `runspec local — List runnables and emit tool schemas

Options:
  --format    Output format: text (default), json, mcp, openai, anthropic
  --script    Target a single runnable by name (use with --format)

Examples:
  runspec local
  runspec local --format mcp
  runspec local --format mcp --script deploy
  runspec local --format json`,

    jump: `runspec jump — Execute a runnable on a remote host via SSH

  Not yet implemented in the Node package.
  Use the Python package (pip install runspec) for SSH execution.`,

    serve: `runspec serve — Start the MCP stdio server for local runnables

  Reads JSON-RPC messages from stdin, writes responses to stdout.
  Configure as an MCP server in Claude Desktop, VS Code, PyCharm, etc.

  To test manually:
    echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | runspec serve`,
  };

  if (command in help) {
    console.log(help[command]);
  } else {
    console.log(`No help available for '${command}'.`);
    printHelp();
  }
}
