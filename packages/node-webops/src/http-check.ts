import { parse } from 'runspec-node';
import { probe, classify } from './http';

async function main(): Promise<void> {
  const args = parse({ scriptName: 'http-check' });
  const url = String(args.url);
  const expectStatus = args.expect_status == null ? null : Number(args.expect_status);

  try {
    const res = await probe(url, Number(args.timeout) * 1000);
    const klass = classify(res.status);

    if (args.json) {
      console.log(JSON.stringify({ ...res, class: klass }));
    } else {
      console.log(`${res.status} ${res.statusText}  [${klass}]  ${res.timeMs}ms  ${res.url}`);
      if (res.location) console.log(`  → ${res.location}`);
      if (res.server) console.log(`  server: ${res.server}`);
    }


    if (expectStatus !== null && res.status !== expectStatus) {
      console.error(`✗  expected ${expectStatus}, got ${res.status}`);
      process.exitCode = 1;
    }
  } catch (err) {
    console.error(`✗  ${url}: ${(err as Error).message}`);
    process.exitCode = 2;
  }
}

main();
