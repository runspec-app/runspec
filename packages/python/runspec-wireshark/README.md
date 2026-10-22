# runspec-wireshark

Packet captures as a [runspec](https://pypi.org/project/runspec/) runnable,
over Wireshark's **tshark**. One runnable — **`pcap`** — with a verb per tool,
chosen from the open-source Wireshark MCP servers (all of which wrap tshark):
[mcp-wireshark](https://glama.ai/mcp/servers/khuynh22/mcp-wireshark),
[WireMCP](https://github.com/0xKoda/WireMCP),
[sharkline](https://pkg.go.dev/github.com/inovacc/sharkline) and
[wireshark-mcp](https://pypi.org/project/wireshark-mcp/). `pip install` it into
a venv and it's discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`pcap_summary`,
`pcap_follow`, …), so a runspec-console agent can read a capture over SSH on
the host where it was taken, without copying the file anywhere.

The runnable is called `pcap`, not `wireshark` or `tshark`, so it never
shadows the real Wireshark binaries on PATH.

## Install

```
pip install runspec-wireshark
```

It needs tshark on the host (capinfos and mergecap come with it):

| OS | Install |
|---|---|
| Debian / Ubuntu | `sudo apt install tshark` |
| RHEL / Fedora | `sudo dnf install wireshark-cli` |
| Windows, macOS | the [Wireshark installer](https://www.wireshark.org/download.html) |

`pcap check` says whether tshark was found and which version. If it isn't on
PATH, set `TSHARK_PATH` (or `--tshark`) to it; capinfos and mergecap are looked
for in the same folder. The usual Windows and macOS install folders are tried
too.

## Verbs

Reading a capture is read-only and `autonomous`. The four verbs that create
files are `confirm`.

| Area | Verbs |
|---|---|
| Install and files | `check`, `list-interfaces`, `file-info` |
| First look | `summary` — protocols, busiest hosts and connections, TCP resets, expert warnings |
| Packets | `packets` (one row per packet, paged), `packet` (one packet decoded layer by layer), `fields` (any tshark fields), `top` (most common values of a field) |
| Statistics | `protocols`, `conversations`, `endpoints`, `expert`, `io` (packets and bytes per interval), `stats` (any `tshark -z` report) |
| Streams | `follow` — the text of a TCP, UDP, TLS, HTTP, HTTP/2 or QUIC stream, both directions in order |
| Applications | `dns` (lookups, failures first, slow answers), `http` (requests joined to responses, errors and slow ones), `tls` (handshakes with SNI, version, cipher, certificate names and alerts) |
| Files (`confirm`) | `capture` (live, bounded by `--duration` / `--count` / `--max-mb`), `save-filtered`, `merge`, `export-objects` |

Every reading verb takes a positional capture file and `--filter` / `-Y`, a
Wireshark display filter (`tcp.flags.reset == 1`, `http.response.code >= 400`,
`ip.addr == 10.0.0.5`). `capture` takes a BPF capture filter instead
(`-f 'host 10.0.0.5 and port 443'`).

```
pcap summary /tmp/outage.pcapng
pcap packets /tmp/outage.pcapng -Y 'tcp.flags.reset == 1'
pcap follow /tmp/outage.pcapng 3
pcap tls /tmp/outage.pcapng --failed
pcap capture -i eth0 --duration 30 -f 'host 10.0.0.5 and port 443'
```

## Output

Results are compact by default, because an agent re-reads every tool result on
each later step:

- Lists are capped by `--limit` and say `truncated: true`; `packets` and
  `fields` page with `--offset` (`next_offset` says where the next page starts).
- `packet` returns each layer's title and its fields as indented lines (up to
  40 per layer, `--depth` levels deep); `--layer http` details one layer,
  `--include bytes` adds the hex dump, `--raw` returns tshark's JSON for the
  packet.
- `follow` returns each direction's chunks in order and stops at
  `--max-bytes` (4000).
- `conversations`, `endpoints` and `io` are computed from one pass over the
  file, so bytes are exact and each TCP/UDP conversation carries its stream
  index for `follow`.

A person at a terminal gets readable text; agents, pipes, the console's Forms
tab and schedulers get JSON. Name resolution is off (no DNS lookup for every
address); `--resolve-names` turns it on.

## Capturing

`capture` needs capture rights: root, or on Linux an account in the
`wireshark` group (`sudo usermod -aG wireshark <user>`, after
`sudo dpkg-reconfigure wireshark-common` on Debian/Ubuntu); on Windows, Npcap.
It always stops — after `--duration` seconds (default 10, at most 3600),
`--count` packets or `--max-mb` megabytes (default 100) — and writes to
`--output`, or a `capture-<time>.pcapng` in `--capture-dir`
(`WIRESHARK_CAPTURE_DIR`, default the temp folder). No verb overwrites an
existing file unless `--overwrite`, and `export-objects` only writes into a new
or empty folder.

`capture` and `export-objects` are recorded as audit events (`pcap.capture`,
`pcap.export_objects`). Files exported from a capture came off the network:
treat them as untrusted.

## TLS decryption

With a TLS key log (the `SSLKEYLOGFILE` a browser, curl or a JVM agent can
write), `--keylog-file` decrypts TLS: then `follow --protocol tls` and `http`
show the payload. It reads `WIRESHARK_KEYLOG_FILE`, then `SSLKEYLOGFILE`.

## Settings — env vars

| Variable | Arg | Meaning |
|---|---|---|
| `TSHARK_PATH`, `WIRESHARK_TSHARK` | `--tshark` | tshark when it isn't on PATH |
| `WIRESHARK_KEYLOG_FILE`, `SSLKEYLOGFILE` | `--keylog-file` | TLS key log |
| `WIRESHARK_TIMEOUT` | `--timeout` | seconds before a tshark run is stopped (120) |
| `WIRESHARK_CAPTURE_DIR` | `capture --capture-dir` | where captures go without `--output` |

Each arg declares its variables as its `env`, so runspec-console's Env files
window lists them for a venv's `.runspec_env`.

## Mock responses

`runspec_wireshark/mocks/pcap.json` holds recorded answers for every verb, so
`RUNSPEC_MOCK=1 pcap …` (runspec 0.74.0+) answers without tshark — for trying
the runnable in the console or testing an agent. The recordings come from the
real code reading a sample capture of made-up traffic (DNS, HTTP 200/404/500, a
TCP reset, TLS 1.2 and 1.3, a failed `unknown_ca` handshake); file paths are
left out of the keys, so `pcap follow <any file> 2` gets the recording for
stream 2. Re-record with `scripts/record_mocks.py`.

## Not ported

Some of those MCP servers do more; these were left out on purpose:

- **Credential extraction** (`tshark -z credentials`, WireMCP's and others'
  password dumps) — `stats` refuses `credentials`; a tool result is the wrong
  place for secrets.
- Threat-intel and GeoIP lookups, YARA scanning and beaconing detection — they
  call outside services or need rule sets; build them on the core if you need
  them.
- WPA/WEP decryption, and editcap trimming and splitting.

## Building your own runnables

The logic lives in [`runspec-wireshark-core`](../runspec-wireshark-core),
which has no runspec dependency and surfaces no runnables. A private package
can depend on it alone and wrap the helpers as narrower runnables — for example
one that checks a team's service for resets and failed handshakes:

```python
from runspec_wireshark_core import config_from_env, packets, tls

cfg = config_from_env()
resets = packets(cfg, "/var/tmp/checkout.pcapng", filter="tcp.flags.reset == 1 and tcp.port == 8443")
failed = tls(cfg, "/var/tmp/checkout.pcapng", failed=True)
```
