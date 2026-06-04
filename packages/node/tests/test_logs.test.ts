import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as zlib from 'zlib';
import { randomUUID } from 'crypto';
import { parseDuration, parseSize, collectRecords, view, status, inventory, planPrune, prune, compact } from '../src/logs';

const DAY = 86_400_000;

function tmp(): string {
  return fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'runspec-logs-'));
}

function summary(runId: string, runnable: string, ts: string, user = 'alice'): string {
  return JSON.stringify({ ts, level: 'INFO', logger: 'runspec.runsummary', message: 'run completed', extra: { event: 'run_summary', run_id: runId, runnable, user, exit_code: 0 } });
}

function line(runId: string, message: string, ts: string, level = 'INFO'): string {
  return JSON.stringify({ ts, level, logger: 'app', message, extra: { run_id: runId } });
}

function writeRun(dir: string, runnable: string, tsName: string, lines: string[], ageDays?: number): string {
  // Real uuid token so isManaged() matches the per-run grammar.
  const p = path.join(dir, `${runnable}.${tsName}.${randomUUID()}.log`);
  fs.writeFileSync(p, lines.join('\n') + '\n');
  if (ageDays != null) {
    const t = Date.now() / 1000 - ageDays * 86400;
    fs.utimesSync(p, t, t);
  }
  return p;
}

const dirs: string[] = [];
afterEach(() => {
  while (dirs.length) fs.rmSync(dirs.pop()!, { recursive: true, force: true });
});
function dir(): string {
  const d = tmp();
  dirs.push(d);
  return d;
}

describe('parsers', () => {
  it('parseDuration', () => {
    expect(parseDuration('30m')).toBe(30 * 60_000);
    expect(parseDuration('7d')).toBe(7 * DAY);
    expect(parseDuration('2w')).toBe(14 * DAY);
    expect(() => parseDuration('soon')).toThrow();
  });
  it('parseSize', () => {
    expect(parseSize('500KB')).toBe(500 * 1024);
    expect(parseSize('10MB')).toBe(10 * 1024 ** 2);
    expect(() => parseSize('huge')).toThrow();
  });
});

describe('view / collectRecords', () => {
  it('merges and sorts by ts across files', () => {
    const d = dir();
    writeRun(d, 'deploy', '20260603T150000Z', [summary('r2', 'deploy', '2026-06-03T15:00:00+00:00')]);
    writeRun(d, 'deploy', '20260601T100000Z', [summary('r1', 'deploy', '2026-06-01T10:00:00+00:00')]);
    const ids = collectRecords([d], 'deploy').map((p) => p.rec.extra.run_id);
    expect(ids).toEqual(['r1', 'r2']);
  });

  it('user filter covers all of a run\'s lines', () => {
    const d = dir();
    writeRun(d, 'deploy', '20260601T100000Z', [line('r1', 'hello', '2026-06-01T10:00:00+00:00'), summary('r1', 'deploy', '2026-06-01T10:00:01+00:00', 'bob')]);
    writeRun(d, 'deploy', '20260601T110000Z', [summary('r2', 'deploy', '2026-06-01T11:00:00+00:00', 'alice')]);
    const recs = collectRecords([d], 'deploy', { user: 'bob' });
    expect(new Set(recs.map((p) => p.rec.extra.run_id))).toEqual(new Set(['r1']));
    expect(recs.length).toBe(2);
  });

  it('json passthrough emits the raw line', () => {
    const d = dir();
    const raw = summary('r1', 'deploy', '2026-06-01T10:00:00+00:00');
    writeRun(d, 'deploy', '20260601T100000Z', [raw]);
    let buf = '';
    view('deploy', { dirs: [d], asJson: true, out: (s) => (buf += s) });
    expect(buf.trim()).toBe(raw);
  });

  it('text line carries run + user columns', () => {
    const d = dir();
    writeRun(d, 'deploy', '20260601T100000Z', [line('r1', 'doing', '2026-06-01T10:00:00+00:00')]);
    let buf = '';
    view('deploy', { dirs: [d], out: (s) => (buf += s) });
    expect(buf).toContain('run=r1');
    expect(buf).toContain('doing');
  });
});

describe('status / inventory', () => {
  it('counts runs and archives, ignores single-mode file', () => {
    const d = dir();
    writeRun(d, 'deploy', '20260601T100000Z', [summary('r1', 'deploy', '2026-06-01T10:00:00+00:00')]);
    writeRun(d, 'deploy', '20260602T100000Z', [summary('r2', 'deploy', '2026-06-02T10:00:00+00:00')]);
    fs.writeFileSync(path.join(d, 'deploy.log'), summary('r0', 'deploy', '2026-06-01T00:00:00+00:00') + '\n'); // single-mode
    const inv = inventory([d]);
    expect(inv['deploy'].per_run_files).toBe(2);
    expect(inv['deploy'].archives).toBe(0);
  });

  it('json shape', () => {
    const d = dir();
    writeRun(d, 'deploy', '20260601T100000Z', [summary('r1', 'deploy', '2026-06-01T10:00:00+00:00')]);
    let buf = '';
    status(null, { dirs: [d], asJson: true, out: (s) => (buf += s) });
    const payload = JSON.parse(buf);
    expect(payload.total_files).toBe(1);
    expect(payload.runnables[0].runnable).toBe('deploy');
    expect(String(payload.runnables[0].newest)).toMatch(/Z$/);
  });

  it('text says nothing found when empty', () => {
    const d = dir();
    let buf = '';
    status(null, { dirs: [d], out: (s) => (buf += s) });
    expect(buf).toContain('No per-invocation logs');
  });
});

describe('prune', () => {
  it('older-than selects only old files', () => {
    const d = dir();
    const old = writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    const fresh = writeRun(d, 'x', '20260603T100000Z', [summary('r2', 'x', '2026-06-03T10:00:00+00:00')], 1);
    const doomed = planPrune([d], 'x', { olderThan: 7 * DAY });
    expect(doomed).toEqual([old]);
    expect(fs.existsSync(fresh)).toBe(true);
  });

  it('max-files keeps the newest N', () => {
    const d = dir();
    const files = [1, 2, 3, 4].map((i) => writeRun(d, 'x', `2026060${i}T100000Z`, [summary(`r${i}`, 'x', `2026-06-0${i}T10:00:00+00:00`)], 10 - i));
    const doomed = new Set(planPrune([d], 'x', { maxFiles: 2 }));
    expect(doomed).toEqual(new Set([files[0], files[1]])); // oldest two
  });

  it('requires a policy', () => {
    const d = dir();
    expect(() => planPrune([d], 'x', {})).toThrow();
  });

  it('never selects a single-mode {runnable}.log', () => {
    const d = dir();
    const single = path.join(d, 'x.log');
    fs.writeFileSync(single, '{}\n');
    const t = Date.now() / 1000 - 99 * 86400;
    fs.utimesSync(single, t, t);
    expect(planPrune([d], 'x', { olderThan: 7 * DAY })).toEqual([]);
    expect(fs.existsSync(single)).toBe(true);
  });

  it('dry-run deletes nothing; json reports the plan', () => {
    const d = dir();
    const f = writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    let buf = '';
    const { count } = prune('x', { dirs: [d], olderThan: 7 * DAY, dryRun: true, asJson: true, out: (s) => (buf += s) });
    expect(count).toBe(1);
    expect(fs.existsSync(f)).toBe(true);
    const payload = JSON.parse(buf);
    expect(payload.dry_run).toBe(true);
    expect(payload.deleted[0].path).toBe(f);
  });

  it('executes deletion', () => {
    const d = dir();
    const f = writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    prune('x', { dirs: [d], olderThan: 7 * DAY, out: () => {} });
    expect(fs.existsSync(f)).toBe(false);
  });
});

describe('compact', () => {
  it('rolls old runs into a gzip archive and view reads it back', () => {
    const d = dir();
    const old = writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    const recent = writeRun(d, 'x', '20260603T100000Z', [summary('r2', 'x', '2026-06-03T10:00:00+00:00')], 1);
    const n = compact('x', 7 * DAY, { dirs: [d], gzip: true, out: () => {} });
    expect(n).toBe(1);
    expect(fs.existsSync(old)).toBe(false);
    expect(fs.existsSync(recent)).toBe(true);
    const archives = fs.readdirSync(d).filter((f) => /x\.archive\.\d{8}\.log\.gz$/.test(f));
    expect(archives.length).toBe(1);
    // archive is readable + view merges it
    expect(zlib.gunzipSync(fs.readFileSync(path.join(d, archives[0]))).toString()).toContain('r1');
    // view merges the archive (r1) with the still-present recent file (r2), ts-sorted
    expect(collectRecords([d], 'x').map((p) => p.rec.extra.run_id)).toEqual(['r1', 'r2']);
  });

  it('does not re-compact an existing archive; json reports compacted count', () => {
    const d = dir();
    writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    let buf = '';
    compact('x', 7 * DAY, { dirs: [d], gzip: true, asJson: true, out: (s) => (buf += s) });
    expect(JSON.parse(buf).compacted).toBe(1);
    expect(compact('x', 7 * DAY, { dirs: [d], gzip: true, out: () => {} })).toBe(0);
  });

  it('dry-run keeps originals', () => {
    const d = dir();
    const old = writeRun(d, 'x', '20260101T100000Z', [summary('r1', 'x', '2026-01-01T10:00:00+00:00')], 30);
    compact('x', 7 * DAY, { dirs: [d], dryRun: true, out: () => {} });
    expect(fs.existsSync(old)).toBe(true);
    expect(fs.readdirSync(d).some((f) => f.includes('.archive.'))).toBe(false);
  });
});
