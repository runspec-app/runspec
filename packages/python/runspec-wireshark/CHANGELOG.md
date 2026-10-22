# Changelog

All notable changes to `runspec-wireshark` are documented here.

## [0.1.0]

First release. One runnable, `pcap`, over Wireshark's tshark, with a verb per
tool of the open-source Wireshark MCP servers (mcp-wireshark, WireMCP,
sharkline, wireshark-mcp):

- **Reads** (`autonomous`): `check`, `list-interfaces`, `file-info`,
  `summary`, `packets`, `packet`, `fields`, `top`, `protocols`,
  `conversations`, `endpoints`, `expert`, `io`, `stats`, `follow`, `dns`,
  `http`, `tls`.
- **Files** (`confirm`): `capture` (bounded by duration, packet count and
  size), `save-filtered`, `merge`, `export-objects`. `capture` and
  `export-objects` record audit events; no verb overwrites a file unless
  `--overwrite`.
- Every reading verb takes a positional capture file and a `--filter` / `-Y`
  display filter. Global `--keylog-file` decrypts TLS, `--decode-as` maps a
  port to a protocol, `--resolve-names` turns name resolution on.
- Compact output: lists capped by `--limit` with `truncated`, paging with
  `--offset`; readable text for a person at a terminal, JSON for everyone else.
- Every environment variable the core reads is declared as an arg `env`
  (`TSHARK_PATH`/`WIRESHARK_TSHARK`, `WIRESHARK_KEYLOG_FILE`/`SSLKEYLOGFILE`,
  `WIRESHARK_TIMEOUT`, `WIRESHARK_CAPTURE_DIR`).
- Recorded mock responses for every verb (`runspec_wireshark/mocks/pcap.json`),
  so `RUNSPEC_MOCK=1 pcap …` answers without tshark; re-record with
  `scripts/record_mocks.py`.
- Not ported: credential extraction (`stats` refuses `-z credentials`),
  threat-intel and GeoIP lookups, YARA and beaconing detection, WPA
  decryption, editcap trimming and splitting.
