import { parse } from 'runspec-node';
import { connectPeer, certReport } from './tls';

async function main(): Promise<void> {
  const args = parse({ scriptName: 'cert-check' });
  const host = String(args.host);
  const port = Number(args.port);
  const warnDays = Number(args.warn_days);

  try {
    const peer = await connectPeer(host, port, Number(args.timeout) * 1000);
    const rep = certReport(peer.cert, warnDays);

    if (args.json) {
      console.log(JSON.stringify({ host, port, ...rep, authorized: peer.authorized, authorizationError: peer.authorizationError }));
    } else {
      console.log(`${host}:${port}`);
      console.log(`  subject:   ${rep.subject}`);
      console.log(`  issuer:    ${rep.issuer}`);
      console.log(`  valid to:  ${rep.validTo}`);
      console.log(`  remaining: ${rep.daysRemaining} day(s)  [${rep.status}]`);
      if (rep.altNames.length) console.log(`  SANs:      ${rep.altNames.join(', ')}`);
      if (!peer.authorized) console.log(`  trust:     NOT trusted (${peer.authorizationError})`);
    }

    // Non-zero exit on a problem so monitors / agents can branch on it.
    if (rep.status !== 'ok') process.exitCode = 1;
  } catch (err) {
    console.error(`✗  ${host}:${port}: ${(err as Error).message}`);
    process.exitCode = 2;
  }
}

main();
