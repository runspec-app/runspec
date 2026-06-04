import type { PeerCertificate, DetailedPeerCertificate } from 'tls';
import { daysUntil, parseAltNames, certReport, chainReport } from '../src/tls';

const DAY = 86_400_000;
const NOW = Date.parse('2026-06-04T00:00:00Z');

function cert(overrides: Partial<PeerCertificate> = {}): PeerCertificate {
  return {
    subject: { CN: 'example.com' },
    issuer: { CN: 'Test CA', O: 'Test Org' },
    valid_from: 'Mar  1 00:00:00 2026 GMT',
    valid_to: 'Sep  1 00:00:00 2026 GMT',
    subjectaltname: 'DNS:example.com, DNS:www.example.com',
    serialNumber: 'ABCD',
    fingerprint256: 'AA:BB',
    ...overrides,
  } as PeerCertificate;
}

describe('daysUntil', () => {
  it('counts whole days to a future GMT date (double-space form)', () => {
    expect(daysUntil('Jun 14 00:00:00 2026 GMT', NOW)).toBe(10);
    expect(daysUntil('Jun  14 00:00:00 2026 GMT', NOW)).toBe(10); // odd spacing tolerated
  });

  it('is negative for an expired cert', () => {
    expect(daysUntil('Jun 1 00:00:00 2026 GMT', NOW)).toBe(-3);
  });

  it('returns NaN for an unparseable date', () => {
    expect(Number.isNaN(daysUntil('not a date', NOW))).toBe(true);
  });
});

describe('parseAltNames', () => {
  it('strips DNS: prefixes', () => {
    expect(parseAltNames('DNS:a.com, DNS:b.com')).toEqual(['a.com', 'b.com']);
  });
  it('returns [] for undefined', () => {
    expect(parseAltNames(undefined)).toEqual([]);
  });
});

describe('certReport status', () => {
  it('ok when comfortably in the future', () => {
    const r = certReport(cert({ valid_to: 'Sep 1 00:00:00 2026 GMT' }), 30, NOW);
    expect(r.status).toBe('ok');
    expect(r.subject).toBe('example.com');
    expect(r.issuer).toBe('Test CA');
    expect(r.altNames).toEqual(['example.com', 'www.example.com']);
  });

  it('expiring inside the warn window', () => {
    const r = certReport(cert({ valid_to: 'Jun 20 00:00:00 2026 GMT' }), 30, NOW); // 16 days
    expect(r.status).toBe('expiring');
    expect(r.daysRemaining).toBe(16);
  });

  it('expired when past validity', () => {
    const r = certReport(cert({ valid_to: 'Jun 1 00:00:00 2026 GMT' }), 30, NOW);
    expect(r.status).toBe('expired');
    expect(r.daysRemaining).toBeLessThan(0);
  });

  it('falls back to issuer.O when issuer.CN is absent', () => {
    const r = certReport(cert({ issuer: { O: 'Org Only' } as PeerCertificate['issuer'] }), 30, NOW);
    expect(r.issuer).toBe('Org Only');
  });
});

describe('chainReport', () => {
  function link(fp: string, cn: string, issuerCn: string): DetailedPeerCertificate {
    return {
      subject: { CN: cn },
      issuer: { CN: issuerCn },
      valid_to: 'Sep 1 00:00:00 2026 GMT',
      fingerprint256: fp,
    } as DetailedPeerCertificate;
  }

  it('walks leaf → intermediate → self-signed root and stops', () => {
    const root = link('ROOT', 'Root CA', 'Root CA');
    (root as { issuerCertificate: DetailedPeerCertificate }).issuerCertificate = root; // self-signed
    const inter = link('INT', 'Intermediate CA', 'Root CA');
    (inter as { issuerCertificate: DetailedPeerCertificate }).issuerCertificate = root;
    const leaf = link('LEAF', 'example.com', 'Intermediate CA');
    (leaf as { issuerCertificate: DetailedPeerCertificate }).issuerCertificate = inter;

    const chain = chainReport(leaf, NOW);
    expect(chain.map((c) => c.subject)).toEqual(['example.com', 'Intermediate CA', 'Root CA']);
    expect(chain[2].selfSigned).toBe(true);
    expect(chain[0].selfSigned).toBe(false);
  });

  it('terminates on a cycle without a self-signed flag', () => {
    const a = link('A', 'a', 'b');
    const b = link('B', 'b', 'a');
    (a as { issuerCertificate: DetailedPeerCertificate }).issuerCertificate = b;
    (b as { issuerCertificate: DetailedPeerCertificate }).issuerCertificate = a;
    const chain = chainReport(a, NOW);
    expect(chain.map((c) => c.subject)).toEqual(['a', 'b']); // de-dup by fingerprint halts it
  });
});
