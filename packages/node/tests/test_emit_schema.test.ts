import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { loadRaw } from '../src/loader';
import { inferScript } from '../src/inference';
import { buildSchema } from '../src/cli';

function schemaFor(toml: string, name: string): Record<string, unknown> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runspec-emit-test-'));
  const file = path.join(dir, 'runspec.toml');
  fs.writeFileSync(file, toml);
  const raw = loadRaw(file);
  const script = inferScript(raw.runnables[name], raw.config.autonomyDefault);
  return buildSchema(name, script, 'mcp');
}

test('string validation fields emit to JSON Schema', () => {
  const schema = schemaFor(
    `
[greet]
[greet.args]
slug = {type = "str", pattern = "[a-z]+-[0-9]+", min-length = 3, max-length = 10}
`,
    'greet',
  );
  const props = (schema['inputSchema'] as Record<string, Record<string, unknown>>)['properties'];
  const slug = (props as Record<string, Record<string, unknown>>)['slug'];
  expect(slug['type']).toBe('string');
  expect(slug['pattern']).toBe('[a-z]+-[0-9]+');
  expect(slug['minLength']).toBe(3);
  expect(slug['maxLength']).toBe(10);
});
