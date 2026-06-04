import { parse } from 'runspec-node';
import { connectPeer, chainReport } from './tls';

async function main(): Promise<void> {
  const args = parse({ scriptName: 'cert-chain' });
  const host = String(args.host);
  const port = Number(args.port);

  try {
    const peer = await connectPeer(host, port, Number(args.timeout) * 1000);
    const chain = chainReport(peer.cert);

    if (args.json) {
      console.log(JSON.stringify({ host, port, chain }));
    } else {
      console.log(`${host}:${port} — ${chain.length} cert(s) in chain`);
      chain.forEach((link, i) => {
        const tag = link.selfSigned ? ' (self-signed root)' : '';
        console.log(`  [${i}] ${link.subject}${tag}`);
        console.log(`      issuer:    ${link.issuer}`);
        console.log(`      valid to:  ${link.validTo}  (${link.daysRemaining}d)`);
      });
    }

  } catch (err) {
    console.error(`✗  ${host}:${port}: ${(err as Error).message}`);
    process.exitCode = 2;
  }
}

main();
