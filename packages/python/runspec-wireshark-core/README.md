# runspec-wireshark-core

Pure-Python helpers over Wireshark's **tshark**, capinfos and mergecap — the
logic behind [`runspec-wireshark`](../runspec-wireshark). Stdlib only, no
runspec dependency, no `runspec.toml`, no entry points: installing it surfaces
no runnables, so a private package can import the helpers and build narrower
runnables on top.

```python
from runspec_wireshark_core import config_from_env, summary, follow

cfg = config_from_env()
print(summary(cfg, "/var/tmp/outage.pcapng"))
print(follow(cfg, "/var/tmp/outage.pcapng", stream=3))
```

Every helper takes a `WiresharkConfig` first and returns plain data; failures
raise `WiresharkError` (`ToolNotFoundError` when tshark isn't installed).
