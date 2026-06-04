import * as http from 'http';
import * as https from 'https';
import { URL } from 'url';

export interface HttpResult {
  url: string;
  status: number;
  statusText: string;
  timeMs: number;
  server: string | null;
  location: string | null;
}

/**
 * Issue a single request and capture the status, timing, and a couple of useful
 * headers. Body is drained, not buffered. I/O only — `classify` is the pure bit.
 */
export function probe(rawUrl: string, timeoutMs: number, method = 'GET'): Promise<HttpResult> {
  const u = new URL(rawUrl);
  const mod = u.protocol === 'https:' ? https : http;
  const start = Date.now();
  return new Promise<HttpResult>((resolve, reject) => {
    const req = mod.request(u, { method, timeout: timeoutMs }, (res) => {
      res.resume(); // drain so the socket can close
      resolve({
        url: rawUrl,
        status: res.statusCode ?? 0,
        statusText: res.statusMessage ?? '',
        timeMs: Date.now() - start,
        server: (res.headers['server'] as string | undefined) ?? null,
        location: (res.headers['location'] as string | undefined) ?? null,
      });
    });
    req.once('timeout', () => {
      req.destroy();
      reject(new Error(`request to ${rawUrl} timed out after ${timeoutMs}ms`));
    });
    req.once('error', reject);
    req.end();
  });
}

export type HttpClass = 'ok' | 'redirect' | 'client-error' | 'server-error' | 'unknown';

/** Bucket an HTTP status code. Pure. */
export function classify(status: number): HttpClass {
  if (status >= 200 && status < 300) return 'ok';
  if (status >= 300 && status < 400) return 'redirect';
  if (status >= 400 && status < 500) return 'client-error';
  if (status >= 500 && status < 600) return 'server-error';
  return 'unknown';
}
