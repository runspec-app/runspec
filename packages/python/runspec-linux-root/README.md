# runspec-linux-root

Linux admin tools that need **root**, as [runspec](https://runspec.app/) runnables:

| Tool | What it does | Autonomy |
|---|---|---|
| `list-upgrades` | List packages with an available upgrade (refreshes the index) | autonomous |
| `upgrade-packages` | Refresh the index and upgrade every package | confirm |
| `install-package` | Install packages by name | confirm |
| `remove-package` | Remove packages (`--purge` for config files too) | confirm |
| `autoremove-packages` | Remove packages nothing depends on (`--dry-run` to preview) | confirm |
| `package-repo list` | List the package repositories configured on the host | autonomous |
| `package-repo add` | Add or update a repository and its signing key, then refresh it (undone if that fails) | confirm |
| `package-repo remove` | Remove a repository and its key file | confirm |
| `system-service <action>` | `start`, `stop`, `restart`, `reload`, `enable` or `disable` a systemd service (`--now` with enable/disable) | confirm |
| `set-sysctl` | Set a kernel parameter, optionally persisted under `/etc/sysctl.d` | confirm |
| `reboot-host` | Reboot, optionally after a delay | confirm |
| `enable-passwordless-sudo` | One-time bootstrap: a validated `/etc/sudoers.d` drop-in | confirm |

They work across apt, dnf, yum, zypper and pacman (`package-repo`: all but pacman). The logic lives in
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

## Adding a package repository

Vendor install guides (Grafana Alloy, Docker, PostgreSQL, ...) start with "add
our key and repository". `package-repo add` does that without a shell:

```bash
package-repo add grafana \
  --apt-url https://apt.grafana.com --apt-key https://apt.grafana.com/gpg-full.key \
  --rpm-url https://rpm.grafana.com --rpm-key https://rpm.grafana.com/gpg.key
install-package alloy
system-service enable alloy --now
```

- One call carries the **apt** pair and the **rpm** pair (dnf, yum and zypper).
  Each host uses the pair for its own package manager, so the same call works
  on every host in a mixed group. A missing pair is an error naming it.
- **apt**: the key goes to `/etc/apt/keyrings/<name>.asc` and
  `/etc/apt/sources.list.d/<name>.list` names it with `signed-by=`, so the key
  is trusted for that repository only. `--apt-suite` (default `stable`) and
  `--apt-components` (default `main`) fill in the rest of the `deb` line.
- **dnf / yum / zypper**: the key goes to `/etc/pki/rpm-gpg/RPM-GPG-KEY-<name>`
  and is imported with `rpm --import`; `/etc/yum.repos.d/<name>.repo` (or
  `/etc/zypp/repos.d/`) has `gpgcheck=1`, `repo_gpgcheck=1` and
  `gpgkey=file://` pointing at that file. `--unsigned-metadata` sets
  `repo_gpgcheck=0` for a repository whose metadata isn't signed.
- **The key** is an `https://` URL or a file already on the host (`http://`
  only with `--key-fingerprint`). The output lists every key's fingerprint.
  `--key-fingerprint` (repeat it, or separate with commas) refuses a key file
  holding any key you didn't list. Check them with the vendor first. A key file
  can hold several keys: Grafana's apt file has three.
- **Then it refreshes just that repository.** If the refresh fails (wrong key,
  bad URL) the files are put back and newly imported rpm keys removed, so a
  host is never left with a repository that breaks `apt-get update`. On
  dnf/yum, a new key also clears dnf's cached copy of the old one, so the
  refresh really checks it.
- **Running it again** with the same values changes nothing (no refresh). A
  same-named file it didn't write (one you set up by hand) needs `--replace`.
  On apt, the same URL in another file is refused: apt won't accept one
  repository with two keys. Remove the old one first.
- The key download uses the proxy environment variables, or `--proxy`, and
  `--ca-bundle` for a private CA. The package manager uses its own proxy
  settings.

`package-repo remove <name>` deletes the repository file and its key file
(unless another repository uses that key), including one set up by hand
following the vendor's guide. Keys already in the rpm database stay.

## Across a group of hosts

From runspec-console, ask the agent to run it with a **host group** as the
target: it runs on every host in the group behind one approval, and each host
picks its own apt or rpm details. A saved runbook can hold the whole sequence
(add the repository, `install-package`, then `system-service enable --now`).
