import { fitToolName, MCP_TOOL_NAME_MAX, tooLongMessage } from '../src/toolname';

describe('fitToolName', () => {
  it('leaves names within the limit alone', () => {
    expect(fitToolName('gitlab')).toBe('gitlab');
    expect(fitToolName('x'.repeat(MCP_TOOL_NAME_MAX))).toBe('x'.repeat(MCP_TOOL_NAME_MAX));
  });

  it('shortens long names stably', () => {
    const name = 'a'.repeat(200);
    const fitted = fitToolName(name);
    expect(fitted.length).toBeLessThanOrEqual(MCP_TOOL_NAME_MAX);
    expect(fitToolName(name)).toBe(fitted);
  });

  it('keeps names that share a long start distinct', () => {
    const base = 'acme-department-tools_' + 'raise-'.repeat(30);
    expect(fitToolName(base + 'laptop')).not.toBe(fitToolName(base + 'desktop'));
  });

  it('matches the Python pack byte-for-byte', () => {
    // Same vector as packages/python/runspec/tests/test_toolname.py::test_fixed_vector.
    expect(fitToolName('abcdefghij_klmnop', 12)).toBe('abcde_10b41d');
  });

  it('explains the rename', () => {
    expect(tooLongMessage('a'.repeat(130), 'short')).toContain('130 characters');
  });
});
