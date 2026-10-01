# runspec-linux-root

Linux admin tools that need **root**, as [runspec](https://runspec.app/) runnables:

| Tool | What it does | Autonomy |
|---|---|---|
| `list-upgrades` | List packages with an available upgrade (refreshes the index) | autonomous |
| `upgrade-packages` | Refresh the index and upgrade every package | confirm |
| `install-package` | Install packages by name | confirm |
| `remove-package` | Remove packages (`--purge` for config files too) | confirm |
| `autoremove-packages` | Remove packages nothing depends on (`--dry-run` to preview) | confirm |
| `set-sysctl` | Set a kernel parameter, optionally persisted under `/etc/sysctl.d` | confirm |
| `reboot-host` | Reboot, optionally after a delay | confirm |
| `enable-passwordless-sudo` | One-time bootstrap: a validated `/etc/sudoers.d` drop-in | confirm |

They work across apt, dnf, yum, zypper and pacman. The logic lives in
[`runspec-linux-core`](https://pypi.org/project/runspec-linux-core/). The
everyday tools that never need root are in
[`runspec-linux`](https://pypi.org/project/runspec-linux/).

## Install into a root-owned venv

Every tool here declares `run_as = "root"`: root runs this code. So install it
into a venv **only root can change**. A venv owned by a service account would
let that account edit code root later runs.

```bash
sudo python3 -m venv /opt/venvs/admin
sudo /opt/venvs/admin/bin/pip install runspec-linux-root
```

## Who can run them

sudoers decides. An admin runs a tool with sudo:

```bash
sudo /opt/venvs/admin/bin/upgrade-packages
```

runspec checks the process really is root (`enforce_run_as`) and refuses
otherwise, naming the account to `sudo -u` to.

**From runspec-console or the MCP gateway**, the tool is run with `sudo -n`,
which never prompts for a password, so the admin needs a passwordless sudo rule.
`enable-passwordless-sudo` installs one:

- `--scope scoped` allows only the programs in this venv's `bin/`. That covers
  direct runs (`sudo /opt/venvs/admin/bin/<tool>` from a shell or cron) and,
  with runspec-console / runspec-mcp built on runspec ≥ 0.70.0, console and
  gateway runs too. Those pass variables to the tool on stdin through a small
  prelude that runs on the venv's own `bin/python`, which the scoped rule covers.
  (Before runspec 0.70.0 the prelude ran on `sh`, which the scoped rule doesn't
  cover, so console runs that carried variables needed `--scope all`.)
- `--scope all` grants `NOPASSWD: ALL`.
