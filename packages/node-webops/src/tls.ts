import * as tls from 'tls';
import type { PeerCertificate, DetailedPeerCertificate } from 'tls';

export interface TlsPeer {
  cert: DetailedPeerCertificate;
  protocol: string | null;
  cipher: { name: string; version: string };
  authorized: boolean;
  authorizationError: string | null;
}

/**
 * Open a TLS connection and capture the peer certificate (full chain) plus the
 * negotiated protocol/cipher. `rejectUnauthorized` is false on purpose — a cert
 * *checker* must still connect to an expired / self-signed / untrusted endpoint
 * to report on it; the trust result is surfaced via `authorized` /
 * `authorizationError` rather than throwing. I/O only — the reporting logic
 * below is pure so it can be unit-tested without a network.
 */
export function connectPeer(host: string, port: number, timeoutMs: number, servername?: string): Promise<TlsPeer> {
  return new Promise<TlsPeer>((resolve, reject) => {
    const socket = tls.connect(
      { host, port, servername: servername ?? host, rejectUnauthorized: false, timeout: timeoutMs },
      () => {
        const peer: TlsPeer = {
          cert: socket.getPeerCertificate(true) as DetailedPeerCertificate,
          protocol: socket.getProtocol(),
          cipher: socket.getCipher() as { name: string; version: string },
          authorized: socket.authorized,
          authorizationError: socket.authorizationError ? String(socket.authorizationError) : null,
        };
        socket.end();
        resolve(peer);
      },
    );
    socket.once('timeout', () => {
      socket.destroy();
      reject(new Error(`connection to ${host}:${port} timed out after ${timeoutMs}ms`));
    });
    socket.once('error', (err) => reject(err));
  });
}

// ── pure reporting ────────────────────────────────────────────────────────────

export type CertStatus = 'ok' | 'expiring' | 'expired';

export interface CertReport {
  subject: string;
  issuer: string;
  validFrom: string;
  validTo: string;
  daysRemaining: number;
  status: CertStatus;
  altNames: string[];
  serialNumber: string;
  fingerprint256: string;
}

export interface ChainLink {
  subject: string;
  issuer: string;
  validTo: string;
  daysRemaining: number;
  selfSigned: boolean;
}

/** Whole days until `validTo` (Node's `"Jun  4 23:59:59 2026 GMT"` form). Negative = expired. */
export function daysUntil(validTo: string, now: number = Date.now()): number {
  const expiry = Date.parse(validTo.replace(/\s+/g, ' '));
  if (Number.isNaN(expiry)) return NaN;
  return Math.floor((expiry - now) / 86_400_000);
}

/** A certificate name field (`CN`/`O`) may be typed `string | string[]`; flatten it. */
function nameField(v: string | string[] | undefined): string {
  if (v === undefined) return '';
  return Array.isArray(v) ? v.join(', ') : v;
}

/** Strip the `DNS:` prefixes from a `subjectaltname` string into a list. */
export function parseAltNames(san: string | undefined): string[] {
  if (!san) return [];
  return san
    .split(',')
    .map((s) => s.trim().replace(/^DNS:/, ''))
    .filter(Boolean);
}

/** Build the leaf-certificate report. Pure. */
export function certReport(cert: PeerCertificate, warnDays: number, now: number = Date.now()): CertReport {
  const days = daysUntil(cert.valid_to, now);
  const status: CertStatus = Number.isNaN(days) ? 'expired' : days < 0 ? 'expired' : days <= warnDays ? 'expiring' : 'ok';
  return {
    subject: nameField(cert.subject?.CN),
    issuer: nameField(cert.issuer?.CN) || nameField(cert.issuer?.O),
    validFrom: cert.valid_from,
    validTo: cert.valid_to,
    daysRemaining: days,
    status,
    altNames: parseAltNames(cert.subjectaltname),
    serialNumber: cert.serialNumber ?? '',
    fingerprint256: cert.fingerprint256 ?? '',
  };
}

/** Walk leaf → issuer → … → root. Pure; de-dups by fingerprint to stop at a self-signed root. */
export function chainReport(leaf: DetailedPeerCertificate, now: number = Date.now()): ChainLink[] {
  const out: ChainLink[] = [];
  const seen = new Set<string>();
  let cur: DetailedPeerCertificate | undefined = leaf;
  while (cur && cur.fingerprint256 && !seen.has(cur.fingerprint256)) {
    seen.add(cur.fingerprint256);
    const selfSigned = !!cur.issuerCertificate && cur.issuerCertificate.fingerprint256 === cur.fingerprint256;
    out.push({
      subject: nameField(cur.subject?.CN),
      issuer: nameField(cur.issuer?.CN) || nameField(cur.issuer?.O),
      validTo: cur.valid_to,
      daysRemaining: daysUntil(cur.valid_to, now),
      selfSigned,
    });
    if (selfSigned) break;
    cur = cur.issuerCertificate;
  }
  return out;
}
