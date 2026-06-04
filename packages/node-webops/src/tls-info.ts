import { parse } from 'runspec-node';
import { connectPeer } from './tls';

async function main(): Promise<void> {
  const args = parse({ scriptName: 'tls-info' });
  const host = String(args.host);
  const port = Number(args.port);

  try {
    const peer = await connectPeer(host, port, Number(args.timeout) * 1000);
    const info = {
      host,
      port,
      protocol: peer.protocol,
      cipher: peer.cipher.name,
      cipherVersion: peer.cipher.version,
      authorized: peer.authorized,
      authorizationError: peer.authorizationError,
    };

    if (args.json) {
      console.log(JSON.stringify(info));
    } else {
      console.log(`${host}:${port}`);
      console.log(`  protocol: ${info.protocol}`);
      console.log(`  cipher:   ${info.cipher} (${info.cipherVersion})`);
      console.log(`  trust:    ${info.authorized ? 'trusted' : `NOT trusted (${info.authorizationError})`}`);
    }

  } catch (err) {
    console.error(`✗  ${host}:${port}: ${(err as Error).message}`);
    process.exitCode = 2;
  }
}

main();
