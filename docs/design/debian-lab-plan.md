# Lab plan: five Debian machines

A step-by-step test of the run-as model on five Debian testing machines. Do the
parts in order. Every step tells you:

- **where** to type: your **PC** (PowerShell or the console app), or an SSH
  window on one of the Debian machines;
- **who** you are there (the prompt shows it: `jason@deb1:~$`);
- **what to type**, exactly;
- **what you should see**.

If what you see differs from **You should see**, write it down in the results
table at the end. That's a finding, not a mistake.

Throughout, the five machines are called **deb1** … **deb5**. Replace those with
their real hostnames or IP addresses.

---

## How to "be" alice, bob or svc-ops

You don't need their passwords. Alice, bob and svc-ops have none (Part 1.1
creates them that way), so nobody can log in as them with a password. **You
become them from your own jason session with `sudo`:**

1. On your PC, open **PowerShell** and connect as yourself:

   ```powershell
   ssh jason@deb1
   ```

   The prompt is `jason@deb1:~$`.

2. Become alice:

   ```bash
   sudo -iu alice
   ```

   Type **your own** (jason's) sudo password if asked. The prompt changes to
   `alice@deb1:~$`. Check with:

   ```bash
   whoami
   ```

   It prints `alice`.

3. When you're done as alice, go back to being jason:

   ```bash
   exit
   ```

   The prompt is `jason@deb1:~$` again.

The same works for bob (`sudo -iu bob`) and the service account
(`sudo -iu svc-ops`).

> **Why this is fair for the tests:** `sudo -iu alice` gives you a real login
> shell as alice: her uid, her groups, her home, her sudo rights. Anything you
> run there behaves exactly as if alice had logged in herself.

The **console** is different: it logs in over SSH as the account in its host
entry, so for the console tests alice and bob need your PC's SSH key. Part 1.2
sets that up.

---

## Part 1: one-time set-up

### What each machine is for

| Machine | Used in | What runs there |
|---|---|---|
| deb1 | Scenario A | alice's personal venv |
| deb2 | Scenario B | the shared `svc-ops` venv |
| deb3 | Scenario C | the root admin venv |
| deb4 | Scenario D | all three kinds on one machine |
| deb5 | Scenario E | an older runspec version |

Not every machine needs every account, but to keep things simple **every
machine gets the same set-up**. The extra accounts don't hurt, and every check
then gives the same result everywhere.

### 1.1 The same accounts on every machine (deb1 … deb5)

Do this on **each** of the five machines. It's safe to run more than once, and
safe where you've already done some of it: anything that already exists is
skipped.

On your PC, in PowerShell:

```powershell
ssh jason@deb1
```

As **jason** on the Debian machine, paste this whole block:

```bash
sudo apt install -y python3 python3-venv sudo openssh-server
for u in alice bob; do
  id "$u" >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash "$u"
done
id svc-ops >/dev/null 2>&1 || sudo useradd --system --user-group --create-home \
  --home-dir /var/lib/svc-ops --shell /bin/bash svc-ops
getent group ops >/dev/null || sudo groupadd ops
sudo usermod -aG ops alice
echo '%ops ALL=(svc-ops) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/runspec-ops >/dev/null
sudo chmod 0440 /etc/sudoers.d/runspec-ops
sudo visudo -c
```

Then check it:

```bash
id alice; id bob; id svc-ops
getent group ops
sudo cat /etc/sudoers.d/runspec-ops
```

**You should see, on every machine:**

- three lines starting `uid=`: for alice (her groups include `ops`), bob, and
  svc-ops;
- `ops:x:<number>:alice`;
- `%ops ALL=(svc-ops) NOPASSWD: ALL`.

`visudo -c` should also have printed `parsed OK` for every file.

What this sets up, and why:

- **alice**: an operator allowed to run the shared tools (member of `ops`).
- **bob**: an operator who isn't (not in `ops`).
- **svc-ops**: the service account that owns shared venvs.
- **the sudo rule**: members of `ops` may run commands as `svc-ops` without a
  password. That's safe because svc-ops is an ordinary, unprivileged account.

None of the three new accounts has a password, so nobody can log in as them
with one. You become them with `sudo -iu` (above), and the console logs in as
them with an SSH key (next step).

Type `exit` to close the SSH session. Repeat on deb2 … deb5.

### 1.2 Let the console log in as alice and bob (deb1 … deb5)

The console doesn't use your usual SSH key. It has its own, which it created
itself:

```text
%APPDATA%\runspec-console\runspec_ed25519
```

(Its path is also in the console's `%APPDATA%\runspec-console\config.toml`,
under `[ssh]` as `identityFile`. If yours is different, use that path in the
steps below.) Every host entry uses it unless the entry names another key.

The console already logs in as **jason** with that key, so jason's
`~/.ssh/authorized_keys` on each machine contains it. These steps give alice and
bob the same authorised keys, and make your PowerShell `ssh` use the console's
key too, so you can check it.

**a) On your PC: let `ssh` use the console's key for the lab machines.**

In PowerShell:

```powershell
notepad $HOME\.ssh\config
```

(Say **Yes** if Notepad offers to create the file.) Add these lines at the end,
with your machines' hostnames in place of `deb1 deb2 deb3 deb4 deb5`, then save:

```text
Host deb1 deb2 deb3 deb4 deb5
    IdentityFile ~/AppData/Roaming/runspec-console/runspec_ed25519
```

From now on, `ssh alice@deb1` (and any other account on those machines) also
tries the console's key. Your usual key keeps working as before.

Check the console's key reaches jason:

```powershell
ssh jason@deb1 whoami
```

**You should see:** `jason`. If you're asked for a password instead, the
console's key isn't on deb1 yet: in the console, **Settings → SSH → Copy key to
all hosts…** (it asks for jason's password once), then try again.

**b) On each Debian machine: give alice and bob jason's authorised keys.**

As **jason** on the Debian machine:

```bash
for u in alice bob; do
  sudo install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  sudo install -m 600 -o "$u" -g "$u" ~/.ssh/authorized_keys "/home/$u/.ssh/authorized_keys"
done
```

**c) Check it**, from your PC in PowerShell:

```powershell
ssh alice@deb1 whoami
ssh bob@deb1 whoami
```

**You should see:** `alice` and then `bob`, with no password prompt. Repeat for
deb2 … deb5.

### 1.3 The console on your PC

In the venv where you run the console, in PowerShell:

```powershell
pip install -U runspec-console
pip show runspec-console runspec | Select-String "^(Name|Version)"
```

**You should see:** runspec-console **0.306.0** or later and runspec **0.68.0**
or later.

### 1.4 The lab host entries

The console keeps its hosts in `%APPDATA%\runspec-console\runspec_hosts.toml`.

1. **Close the console.**
2. Open that file in Notepad:

   ```powershell
   notepad $env:APPDATA\runspec-console\runspec_hosts.toml
   ```

3. Paste the entries below **at the end of the file**. Replace `deb1` …
   `deb5` with your hostnames. They need no key line: they use the console's
   own key, like your other hosts.

```toml
[[host]]
name          = "deb1-alice"
ssh           = "alice@deb1"
runspec_paths = ["/home/alice/venvs/tools/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb2-alice"
ssh           = "alice@deb2"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb2-bob"
ssh           = "bob@deb2"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb3-jason"
ssh           = "jason@deb3"
runspec_paths = ["/opt/venvs/admin/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb4-jason"
ssh           = "jason@deb4"
runspec_paths = ["/home/jason/venvs/tools/bin/runspec", "/opt/venvs/ops/bin/runspec", "/opt/venvs/admin/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb4-alice"
ssh           = "alice@deb4"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
profiles      = ["lab"]

[[host]]
name          = "deb5-alice"
ssh           = "alice@deb5"
runspec_paths = ["/opt/venvs/old/bin/runspec"]
profiles      = ["lab"]
```

4. Save, close Notepad, and start the console.
5. In the sidebar's profile switcher, choose **lab**. Only these hosts show.

Hosts whose venvs don't exist yet will show no runnables (or an error) until you
reach their scenario. That's expected.

---

## Scenario A: personal venvs (deb1)

**What this tests:** a user without service-account rights keeps their own venv
in their home, and nothing escalates.

### A.0 Create alice's venv

On your PC, in PowerShell:

```powershell
ssh jason@deb1
```

As jason, become alice:

```bash
sudo -iu alice
```

As **alice** (prompt `alice@deb1:~$`):

```bash
python3 -m venv ~/venvs/tools
~/venvs/tools/bin/pip install runspec-linux
~/venvs/tools/bin/pip show runspec runspec-linux | grep -E '^(Name|Version)'
```

**You should see:** runspec **0.68.0** or later and runspec-linux **0.19.0** or
later.

Stay as alice for A1.

### A1. Alice runs her own tool

As **alice**:

```bash
~/venvs/tools/bin/system-info
```

**You should see:** a line of JSON (hostname, kernel, uptime …) and no password
prompt.

**This proves:** a personal venv runs as its owner, with no sudo.

Now go back to jason:

```bash
exit
```

### A2. Jason tries to run alice's tool

As **jason** (prompt `jason@deb1:~$`):

```bash
ls -ld /home/alice
/home/alice/venvs/tools/bin/system-info
```

**You should see one of:**

- `Permission denied`, if the first line shows `drwx------` (Debian's default:
  nobody else can enter alice's home); or
- `✗  Refusing to run 'system-info': …` naming `alice`, if her home is readable.

**This proves:** one user can't run another's personal tools as themselves.

Type `exit` to close the SSH session.

### A3. The console as alice

In the **console** on your PC:

1. Sidebar → **lab** profile → host **deb1-alice**. Wait for the runnables to
   appear.
2. **Forms** tab → **system-info**.

   **You should see:** no "as …" label next to the runnable name.
3. Click **Run**.

   **You should see:** the JSON output, and a success status.
4. **History** tab.

   **You should see:** the run, with operator **alice**.

**This proves:** the console only escalates when the venv's owner isn't the
account it logs in as.

### A4. The run history lives in the venv

On your PC, in PowerShell:

```powershell
ssh alice@deb1
```

As **alice**:

```bash
~/venvs/tools/bin/runspec history list
ls -l ~/venvs/tools/logs
exit
```

**You should see:** the A1 and A3 runs in the list, and log files owned by
`alice`.

---

## Scenario B: shared service-account venv (deb2)

**What this tests:** the main model. `svc-ops` owns the venv, members of `ops`
(alice) may `sudo -u svc-ops`, and bob may not.

### B.0 Create the shared venv

On your PC, in PowerShell:

```powershell
ssh jason@deb2
```

As **jason**:

```bash
sudo install -d -m 0755 /opt/venvs
sudo install -d -o svc-ops -g svc-ops -m 0755 /opt/venvs/ops
sudo -u svc-ops python3 -m venv /opt/venvs/ops
sudo -u svc-ops /opt/venvs/ops/bin/pip install runspec-linux
stat -c '%U' /opt/venvs/ops
```

**You should see:** the install finishes, and the last line prints `svc-ops`.

Stay logged in as jason; the next steps switch users from here.

### B1. Alice runs a shared tool directly

As jason, become alice:

```bash
sudo -iu alice
```

As **alice** (prompt `alice@deb2:~$`):

```bash
/opt/venvs/ops/bin/system-info
echo "exit code: $?"
```

**You should see:** `✗  Refusing to run 'system-info': …`, naming `svc-ops`
and suggesting `sudo -u svc-ops …`, then `exit code: 1` (anything but 0).

**This proves:** nobody can bypass sudoers by running the venv's `bin/` directly.

### B2. Alice runs it through sudo

Still as **alice**:

```bash
sudo -u svc-ops /opt/venvs/ops/bin/system-info
```

**You should see:** the JSON output, with **no** password prompt.

### B3. `--run-as` naming the account you already are

Still as **alice**:

```bash
sudo -u svc-ops /opt/venvs/ops/bin/tail-log --file /etc/hostname --run-as svc-ops
```

**You should see:** the machine's hostname in JSON. No "password is required"
error.

### B4. `--run-as root` is refused

Still as **alice**:

```bash
sudo -u svc-ops /opt/venvs/ops/bin/tail-log --file /etc/hostname --run-as root
echo "exit code: $?"
```

**You should see:** `{"error": "--run-as root: runspec-linux does not run
anything as root — root tools live in runspec-linux-root"}` and `exit code: 2`.

Go back to jason:

```bash
exit
```

### B5. Bob is stopped by sudo

As jason, become bob:

```bash
sudo -iu bob
```

As **bob** (prompt `bob@deb2:~$`):

```bash
sudo -u svc-ops /opt/venvs/ops/bin/system-info
```

**You should see:** sudo asks for **bob's** password. bob has none, so press
**Ctrl+C**. (If instead you see `Sorry, user bob is not allowed to execute …`,
that's the same result.)

**This proves:** who may run the shared tools is decided by sudoers alone.

Go back to jason and close the session:

```bash
exit
exit
```

### B6. The console sees the owner

In the **console**:

1. Sidebar → host **deb2-alice** → **Forms** → **system-info**.

   **You should see:** **as svc-ops** next to the runnable name.

### B7. A console run as alice escalates

In the **console**, on **deb2-alice**:

1. Run **system-info**.

   **You should see:** success.
2. **History** tab.

   **You should see:** operator **alice**, run as **svc-ops**.

Then on your PC, in PowerShell:

```powershell
ssh jason@deb2
```

As **jason**:

```bash
sudo journalctl _COMM=sudo --since "-10min" | tail -5
```

**You should see:** a line like
`alice : PWD=/home/alice ; USER=svc-ops ; COMMAND=/opt/venvs/ops/bin/system-info`.
sudo records **alice** as the person and **svc-ops** as the account. There's no
`TTY=` because the console ran it over SSH, not from a terminal. (When a run
carries variables, such as a credential, the command is `…/sh -c …` instead:
you'll see that in B12.)

### B8. A console run as bob fails cleanly

In the **console**, on **deb2-bob**:

1. Run **system-info**.

   **You should see:** a quick failure (seconds, not a hang). sudo's own lines
   ("a terminal is required … a password is required") are followed by the
   console's explanation (runspec-console 0.307.0 or later):

   > ✗  Couldn't run as svc-ops on deb2-bob: sudo didn't let bob run this venv's
   > tools as svc-ops without a password, and the console can't type one. Ask an
   > administrator for a passwordless sudo rule letting bob run commands as
   > svc-ops, usually by joining the group that has one.
2. **Write down** whether that tells you what to do. This is what an assessor
   would see.

### B9. The agent path

**a) A read-only question.** In the **console** chat, type:

> what's the uptime on deb2-alice?

**You should see:** the answer, with **no** confirm card. `system-info` is
read-only, so runspec-linux lets the agent run it without asking. It still runs
as `svc-ops`, the same as B7.

**b) Something that changes the machine.** In the chat, type:

> add a cron job on deb2-alice that runs `echo hello` every hour

**You should see:** a **confirm card** for `cron add`, showing the schedule and
the command. Adding a cron job changes the machine, so the agent has to ask.
Click **Deny**: nothing is added. (If you approve it by mistake, B11's
`cron list` shows it; remove it with `cron remove`.)

### B10. One writer for the logs

In your jason SSH session on deb2:

```bash
ls -l /opt/venvs/ops/logs
ls -l /opt/venvs/ops/logs/inbox 2>/dev/null || echo "no inbox"
```

**You should see:** every file owned by `svc-ops`, and either "no inbox" or an
empty inbox.

**This proves:** the shared venv's run history needs no group or ACL set-up.

### B11. Cron shows the service account's crontab

In the **console**, on **deb2-alice**: **Forms** → **cron** → **list** → **Run**.

**You should see:** svc-ops's crontab (empty at first), not alice's.

### B12. A secret never appears on a command line

1. In the **console**: **Settings → Credentials → Add**.
   - Label `Lab`, kind **token**, secret `lab-7f3a91` (any made-up value).
   - Scope it to host `deb2-alice`.
2. **Forms** on **deb2-alice** → **ping-host** with host `127.0.0.1` and count
   `15`. The credential rides along as `LAB_TOKEN`. Click **Run**. It takes
   about 15 seconds.
3. **While it's running**, in your jason SSH session on deb2:

   ```bash
   ps -ww -eo user,args | grep lab-7f3a91 | grep -v grep
   sudo journalctl _COMM=sudo --since "-5min" | grep lab-7f3a91
   ```

**You should see:** **nothing** printed by either command.

Then look at the sudo line for this run:

```bash
sudo journalctl _COMM=sudo --since "-5min" | grep alice | tail -2
```

**You should see:** `COMMAND=/usr/bin/sh -c while IFS= read -r l; …` followed
by the tool: the fixed prelude that reads the variables from stdin. The secret
isn't in it.

**This proves:** secrets travel on stdin, not on a command line anyone can read
with `ps` or in the sudo log.

### B13. Marketplace installs as the owner

In the **console**: **Marketplace** tab → find **runspec-logops** → **Install**
→ tick the venv on **deb2-alice**.

**You should see:** the **Run as** column pre-filled with `svc-ops`, then a
successful install.

Check in your jason SSH session on deb2:

```bash
ls -l /opt/venvs/ops/bin/summarize-log
```

**You should see:** the file owned by `svc-ops`.

### B14. The kill switch

1. In the **console**: the **Denylist** button at the bottom of the hosts panel.
   - **Target hosts:** pick **deb2-alice**. This is what sends the rule to that
     host.
   - Under **Disable specific runnables**: **Runnable names** = `system-info`.
     Leave **Usernames** and **Only on machines whose hostname matches** empty.
     That last field is matched against the machine's own hostname
     (`little-girl`), not the console's name `deb2-alice`. Typing `deb2-alice`
     there makes a rule that never fires, and the modal now warns about it.
   - **Add deny rule.**

   **You should see:** the result says `✓ deb2-alice/ops (as svc-ops): ✓ Added
   deny rule`. In the table, the deb2-alice/ops row shows `little-girl` under
   the host name and one rule line with a remove (🗑) button. Other venvs on the
   same hosts that aren't created yet (Scenarios C–E) show a grey *not
   installed* tag, which is fine at this stage.
2. **Forms** on **deb2-alice** → run **system-info**.

   **You should see:** `✗ Refusing to run 'system-info': disabled by the
   runspec denylist.`
3. Back in **Denylist**, click the rule's 🗑 → **Remove**. That removes just
   that rule on that venv. (**Re-enable all (clear)** also works, but drops
   every rule.)
4. Run **system-info** again.

   **You should see:** it works.
5. Optional, all-disabled: **Target hosts** = deb2-alice → **Disable all** →
   the row shows **ALL** with an **Enable** button. Click **Enable** → the row
   goes back to its rules (or *none*).

Needs `runspec >= 0.69.0` in `/opt/venvs/ops` for the hostname column, the
warning and per-rule remove. Upgrade it on deb2, as **jason**:

```bash
sudo -u svc-ops /opt/venvs/ops/bin/pip install -U runspec
```

With an older runspec there, steps 1, 2 and 4 still work. Step 3's 🗑 then
replies "Unknown deny action", so use **Re-enable all (clear)** instead.

Type `exit` to close your deb2 SSH session.

---

## Scenario C: the root admin venv (deb3)

**What this tests:** root tools kept apart in a venv only root can change, and
how an admin reaches them. This scenario decides "step 3".

### C.0 Create the admin venv

On your PC, in PowerShell:

```powershell
ssh jason@deb3
```

As **jason**:

```bash
sudo python3 -m venv /opt/venvs/admin
sudo /opt/venvs/admin/bin/pip install runspec-linux-root
stat -c '%U %a' /opt/venvs/admin
```

**You should see:** `root 755`.

### C1. Jason uses sudo with his password

As **jason**:

```bash
sudo -k
sudo /opt/venvs/admin/bin/list-upgrades
```

(`sudo -k` makes sudo forget your recent password, so it asks again.)

**You should see:** a password prompt, then JSON listing upgradable packages.

### C2. Jason without sudo

As **jason**:

```bash
/opt/venvs/admin/bin/list-upgrades
```

**You should see:** `✗  Refusing to run 'list-upgrades': it must run as 'root'
(uid 0), but you are 'jason' (uid 1000).` With runspec ≥ 0.69.0 the next line
gives the command to use instead: `Run it as root instead:  sudo
/opt/venvs/admin/bin/list-upgrades`, which is what C1 did.

### C3. Alice can't use them

As jason, become alice:

```bash
sudo -iu alice
```

As **alice**:

```bash
sudo /opt/venvs/admin/bin/list-upgrades
```

**You should see:** sudo asks for alice's password. She has none, so press
**Ctrl+C**.

Go back to jason:

```bash
exit
```

### Before C4: does jason already have passwordless sudo?

C4–C6 show what each sudo rule changes, so they need jason to start with
**no** passwordless sudo. In your jason SSH session on deb3:

```bash
sudo -k; sudo -n true && echo "PASSWORDLESS ALREADY" || echo "needs a password"
```

- **needs a password:** do C4–C6 as written.
- **PASSWORDLESS ALREADY:** an existing rule, often
  `jason ALL=(ALL) NOPASSWD: ALL` in `/etc/sudoers` or `/etc/sudoers.d/`
  (`sudo -l` shows which), already covers everything. With that rule, C4–C6
  all just work, and that is the correct result for such a host. Either:
  - **Skip C4–C6** and note "passwordless sudo already present" in the results
    table, or
  - **Park the rule for the test.** Find the file with
    `sudo grep -rl NOPASSWD /etc/sudoers /etc/sudoers.d/`. If it's a file in
    `/etc/sudoers.d/`, move it aside with
    `sudo mv /etc/sudoers.d/<file> /root/<file>.parked`, then put it back after
    C6 with `sudo mv /root/<file>.parked /etc/sudoers.d/<file>`. Keep a second
    root shell open (`sudo -i`) until it's back. If the rule is inside
    `/etc/sudoers` itself, skip rather than edit that file.

### C4. The console, with no passwordless rule

In the **console**: host **deb3-jason** → **Forms** → **list-upgrades** → **Run**.

**You should see:** a quick failure that says in plain English that jason has
no passwordless sudo to root (console 0.307.0+), not a hang.

### C5. The "scoped" passwordless rule

In your jason SSH session on deb3, **without** `sudo` in front:

```bash
/opt/venvs/admin/bin/enable-passwordless-sudo --scope scoped
```

It asks for your (jason's) password. That's piped to sudo, never put on a
command line.

**You should see:** JSON with `"installed": true`.

Test it from the shell:

```bash
sudo -k
sudo -n /opt/venvs/admin/bin/list-upgrades
```

**You should see:** JSON, with no password prompt.

Now test it from the **console**: host **deb3-jason** → run **list-upgrades**.

**You should see:** it **works**. A Forms run with no environment variables is
`sudo -n -u root /opt/venvs/admin/bin/list-upgrades` directly, and the scoped
rule covers that binary. (The `sh -c` env wrapper, which a scoped rule does
*not* cover, is only used when the run carries variables, e.g. a selected
credential. To see that limit, pick any credential in the run dialog's
credential section and run again: it fails, and the console adds its "Ask an
administrator for a passwordless sudo rule…" line.)

### C6. The "all" passwordless rule

In your jason SSH session on deb3:

```bash
/opt/venvs/admin/bin/enable-passwordless-sudo --scope all
```

In the **console**: host **deb3-jason** → run **list-upgrades**, this time with
a credential selected as at the end of C5.

**You should see:** it works. The "all" rule covers the `sh` env wrapper too.

Now remove the rule again (and put back any rule you parked before C4):

```bash
sudo rm /etc/sudoers.d/runspec
```

### C7. Only root can change the admin code

In your jason SSH session on deb3:

```bash
stat -c '%U' /opt/venvs/admin/lib/python3*/site-packages/runspec_linux_root/runspec.toml
```

**You should see:** `root`.

Type `exit` to close the session.

### Decide step 3

After C4–C6, pick one way for admins to reach root from the console:

1. **Command line only.** Admins type their sudo password in a terminal; the
   console handles everything else. Nothing to build.
2. **`NOPASSWD: ALL` for admin accounts** (what C6 did). Simple, but broad.
3. **A small launcher**, so the scoped rule from C5 works from the console too.
   Only needed if (1) and (2) are both unacceptable.

---

## Scenario D: everything on one host, with audit (deb4)

**What this tests:** a realistic production host with a personal venv, the
shared venv and the admin venv side by side, and an audit trail that names the
real person.

### D.0 Set up all three venvs

On your PC, in PowerShell:

```powershell
ssh jason@deb4
```

As **jason** (jason's personal venv):

```bash
python3 -m venv ~/venvs/tools
~/venvs/tools/bin/pip install runspec-linux
```

Still as **jason** (the shared venv, as in B.0):

```bash
sudo install -d -m 0755 /opt/venvs
sudo install -d -o svc-ops -g svc-ops -m 0755 /opt/venvs/ops
sudo -u svc-ops python3 -m venv /opt/venvs/ops
sudo -u svc-ops /opt/venvs/ops/bin/pip install runspec-linux
```

Still as **jason** (the admin venv, as in C.0):

```bash
sudo python3 -m venv /opt/venvs/admin
sudo /opt/venvs/admin/bin/pip install runspec-linux-root
```

Turn on auditing of the shared and admin tools:

```bash
sudo apt install -y auditd
sudo auditctl -w /opt/venvs/ops/bin -p x -k runspec-ops
sudo auditctl -w /opt/venvs/admin/bin -p x -k runspec-admin
```

### D1. Three venvs, three identities

In the **console**: host **deb4-jason** → **Forms**.

**You should see:** runnables from three venvs:

- jason's own venv: no "as …" label;
- `ops`: **as svc-ops**;
- `admin`: **as root**.

### D2. The audit trail names alice

1. In the **console**: host **deb4-alice** → run **system-info**.
2. In your jason SSH session on deb4:

   ```bash
   sudo ausearch -k runspec-ops -i | tail -20
   ```

**You should see:** `auid=alice` on the same record as `uid=svc-ops`.

**This proves:** even though the tool ran as the service account, the audit log
records the person who started it.

### D3. The same tool in two venvs

`system-info` exists in jason's venv and in `ops`.

In the **console**, on **deb4-jason**, run **system-info** from each venv (Forms
shows them separately).

**You should see:** jason's copy runs with no escalation; the ops copy runs as
`svc-ops` (check **History**).

### D4. The agent picks the right venv

In the **console** chat:

> show disk usage on deb4 using the ops tools

**You should see:** a confirm card naming the ops venv; after approval it runs
as `svc-ops`.

### D5. History across venvs

**History** tab.

**You should see:** the D1–D4 runs, each with the right operator and account.

Type `exit` to close your deb4 SSH session.

---

## Scenario E: an older host, and the MCP gateway (deb5)

### E1. A host still on runspec 0.67.0

On your PC, in PowerShell:

```powershell
ssh jason@deb5
```

As **jason**:

```bash
sudo install -d -m 0755 /opt/venvs
sudo install -d -o svc-ops -g svc-ops -m 0755 /opt/venvs/old
sudo -u svc-ops python3 -m venv /opt/venvs/old
sudo -u svc-ops /opt/venvs/old/bin/pip install "runspec==0.67.0" "runspec-linux==0.18.0"
```

In the **console**: host **deb5-alice**.

**You should see:** the runnables, with **no** "as …" label (an older runspec
doesn't report an owner), and no errors in the **Dev** tab.

Run **system-info** on **deb5-alice**.

**You should see:** it runs as **alice**, the old behaviour. A half-upgraded
fleet keeps working.

Type `exit` to close the session.

### E2. The MCP gateway, from Claude Code and Copilot

The gateway can reuse the console's own settings folder: the same hosts
(including the `deb…` lab entries), the same credentials, the same SSH key.
Nothing new to write. This needs **runspec-mcp 0.1.7 or later**.

On your **PC**, in PowerShell:

```powershell
python -m venv $HOME\venvs\gateway
& $HOME\venvs\gateway\Scripts\pip install -U "runspec-mcp[credentials]"
& $HOME\venvs\gateway\Scripts\pip show runspec-mcp | Select-String "^Version"
```

**You should see:** `Version: 0.1.7` or later.

Check it can reach your hosts using the console's settings:

```powershell
& $HOME\venvs\gateway\Scripts\runspec-mcp check --config-dir $env:APPDATA\runspec-console
```

**You should see:** your hosts listed, the `deb…` ones reachable.

(The gateway doesn't know about console profiles, so it offers tools from
**all** your console hosts, not just the lab ones. Ask for a lab host by name.)

- [ ] **E2a: Claude Code.**

  ```powershell
  & $HOME\venvs\gateway\Scripts\runspec-mcp setup claude --config-dir $env:APPDATA\runspec-console
  ```

  Run the `claude mcp add …` line it prints. Then in Claude Code ask *"what's
  the uptime on deb2-alice?"*.

  **You should see:** it runs, as `svc-ops` (the same as B7).
- [ ] **E2b: Copilot (VS Code).**

  ```powershell
  & $HOME\venvs\gateway\Scripts\runspec-mcp setup vscode --config-dir $env:APPDATA\runspec-console
  ```

  Paste the entry it prints into `.vscode/mcp.json` in any folder you open in VS
  Code, then ask Copilot the same question.

  **You should see:** the same result. **Write down** how each client asks you
  to confirm a tool.
- [ ] **E2c: root through the gateway.** Ask either client for **list-upgrades
  on deb3-jason**.

  **You should see:** "a password is required", the same as C4 (you removed the
  passwordless rule in C6). If you skipped C4–C6 because jason already had
  passwordless sudo, it works instead, which is also correct.

---

## Results

| Test | Pass / fail / surprise | Notes |
|---|---|---|
| A1–A4 | | |
| B1–B5 (command line) | | |
| B6–B11 (console) | | |
| B12 (secrets) | | |
| B13–B14 | | |
| C1–C7 | | |
| Step-3 decision | | |
| D1–D5 | | |
| E1 | | |
| E2a–E2c | | |
| Anything else | | |

---

## Clean up afterwards

On each Debian machine, as **jason**:

```bash
sudo rm -rf /opt/venvs/ops /opt/venvs/admin /opt/venvs/old
sudo rm -f /etc/sudoers.d/runspec
sudo auditctl -D 2>/dev/null        # deb4: removes the audit watches
rm -rf ~/venvs/tools                 # jason's personal venv (deb4)
sudo rm -rf /home/alice/venvs        # alice's personal venv (deb1)
```

Keep the accounts, the `ops` group and `/etc/sudoers.d/runspec-ops` if you want
to run the lab again. To remove those too:

```bash
sudo deluser --remove-home bob; sudo deluser --remove-home alice
sudo deluser --system svc-ops; sudo groupdel ops
sudo rm -f /etc/sudoers.d/runspec-ops
```

In the console, delete the `deb…` entries from `runspec_hosts.toml` (console
closed) and the `Lab` credential.
