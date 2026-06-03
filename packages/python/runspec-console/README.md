# runspec-console

A desktop console for [runspec](https://pypi.org/project/runspec/) — run,
schedule, and monitor runnables from a graphical UI. Built on
[pywebview](https://pywebview.flowrl.com/) with a Vite/React frontend, it
discovers every runnable installed in its environment (via `runspec local`) and
can also reach runnables on remote hosts over SSH.

## Install

```
pip install runspec-console
runspec-console
```

On **Windows**, installing the console automatically pulls in
[`runspec-windows`](https://pypi.org/project/runspec-windows/) (with its
`[graph]` extra), so its Windows system-administration and Microsoft 365
runnables appear in the UI with no extra step. The dependency is gated to
`sys_platform == "win32"`, so it's skipped on macOS/Linux.

To add more runnables, just `pip install` the package that provides them into
the same environment (e.g. `runspec-linux`) — the console picks them up on the
next launch.

## Where runnables come from

The console lists whatever `runspec local` can discover: any installed package
that ships a `runspec.toml` and entry points. There's no registry and no
filesystem scanning — `pip install` (or `pip install -e .`) is how a runnable
becomes visible.

## Microsoft 365 runnables (Outlook · Teams · Calendar · OneDrive)

These ship in `runspec-windows` and authenticate as the signed-in user via the
Microsoft Graph **device-code** flow (no client secret). The full setup lives in
the [runspec-windows README](https://pypi.org/project/runspec-windows/); the
short version:

1. **Register an app** in your Entra (Azure AD) tenant
   (*App registrations → New registration*).
2. **Authentication → "Allow public client flows" → Yes** — device-code login
   needs this.
3. **API permissions → Microsoft Graph → Delegated**: add `User.Read`,
   `Mail.Read`, `Chat.Read`, `Calendars.Read`, `Files.Read.All`, then grant
   consent.
4. Copy the **Application (client) ID** and **Directory (tenant) ID**.
5. Drop them in `%APPDATA%\runspec-windows\graph.toml` (the client id is not a
   secret):

   ```toml
   client_id = "00000000-0000-0000-0000-000000000000"
   tenant    = "yourcompany.onmicrosoft.com"
   ```

   For a **personal** Microsoft account (e.g. trying it at home), register the
   app for "personal Microsoft accounts" and set `tenant = "consumers"` (or
   `"common"`) instead of a tenant domain.
6. Run `graph-login` once (it prints a URL + code to authenticate); the token
   caches and refreshes silently thereafter.

> **In a managed tenant:** `Chat.Read` (Teams) is typically **admin-consent-required**, so a
> tenant admin may need to grant consent on the app registration. The
> mail/calendar/files scopes are usually user-consentable.

## AI assistant (optional)

The console can drive runnables through an LLM. Install the provider extra you
want:

```
pip install "runspec-console[anthropic]"   # Claude
pip install "runspec-console[openai]"      # OpenAI
pip install "runspec-console[bedrock]"     # Claude via AWS Bedrock
```

## Usage

```
runspec-console              # production (bundled UI)
runspec-console --dev        # point pywebview at a running Vite dev server
runspec-console --devtools   # enable the Chromium inspector in a prod build
```

See the main [runspec docs](https://github.com/JasonFinestone/runspec) for the
runspec format, the SSH jump-host model, and logging.
