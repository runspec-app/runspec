# Changelog — runspec-wireshark-core

## [0.1.0]

First release. Pure-Python helpers over Wireshark's tshark, capinfos and
mergecap (stdlib only, no runspec dependency, no runnables):

- **Setup:** `WiresharkConfig`, `config_from_env` (`TSHARK_PATH` /
  `WIRESHARK_TSHARK`, `WIRESHARK_KEYLOG_FILE` / `SSLKEYLOGFILE`,
  `WIRESHARK_TIMEOUT`), `find_tool` (explicit path, its folder, PATH, then
  the usual Windows and macOS install folders), `check`, `list_interfaces`,
  `file_info`.
- **Packets:** `packets` (paged rows with stream indexes), `packet` (one packet
  decoded layer by layer), `fields`, `top`.
- **Statistics:** `summary`, `protocols`, `conversations`, `endpoints`,
  `expert`, `io` (one fields pass, exact bytes), `stats` (any `-z` report
  except `credentials`).
- **Streams and applications:** `follow` (TCP/UDP/TLS/HTTP/HTTP2/QUIC/…),
  `dns`, `http` (requests joined to responses), `tls` (SNI, version, cipher,
  certificate names, alerts).
- **Files:** `capture` (bounded by duration, count and size; files go in
  `WIRESHARK_CAPTURE_DIR` or the temp folder without an output path),
  `save_filtered`, `merge`, `export_objects`. None overwrites a file unless
  asked.
- Failures raise `WiresharkError` (`ToolNotFoundError` when tshark is
  missing), with tshark's error cut to the useful line.
