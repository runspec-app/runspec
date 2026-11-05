# Changelog — runspec-wireshark-core

## [0.1.1]

- **Works on Ubuntu 25.10 and later (incl. 26.04).** Ubuntu ships an AppArmor
  profile for `/usr/bin/tshark` that only lets it open files in `/tmp`, so
  every helper that read a capture failed with "You don't have permission to
  read the file". When tshark is confined (the profile is installed and
  AppArmor is on — new `client.confined()`), a capture is now piped in on
  stdin (`tshark -r -`); `save_filtered` and `export_objects` have tshark write
  into a private folder under `/tmp` (`client.staging()`) and move the result
  into place; and a TLS key log is read from a private owner-only copy under
  `/tmp`, deleted at exit (before, a confined tshark silently didn't decrypt).
  If tshark is refused a file this process can read anywhere else, the helper
  retries through the pipe and the rest of the process pipes too. Errors from
  a piped read still name the real file.

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
