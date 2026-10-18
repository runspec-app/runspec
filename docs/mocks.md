# Mock responses

A runnable can be run in **mock mode**: it checks its arguments as usual, then
prints a response recorded earlier and exits with that response's exit code.
**None of the runnable's own code runs.** Nothing is contacted, written or
changed, and no credentials are needed.

Use it to:

- try an agent, a prompt or a runbook against realistic output without touching
  a real system,
- let an eval run several tool calls in a row and produce a full trace,
- give someone a working demo of a runnable that talks to a system they can't
  reach.

The format is defined in [SPEC → Mock Playback](https://github.com/jasonfinestone/runspec/blob/main/spec/SPEC.md#mock-playback).
Mock playback needs runspec 0.74.0 or later; the Node pack does not support it yet.

## Play back

Set `RUNSPEC_MOCK=1`:

```bash
RUNSPEC_MOCK=1 jira get-issue --key OPS-101
```

or use the CLI, which does the same thing:

```bash
runspec mock play jira -- get-issue --key OPS-101
```

`runspec serve` and `runspec exec` pass the variable on to the runnables they
run. So does the console (0.351.5 or later): start it with `RUNSPEC_MOCK=1` and
every run is mocked, including `run_as` runs and runs on remote hosts.
`RUNSPEC_MOCK_DIR` reaches only runs on the console's own machine, because it
names a local directory.

Argument checking still happens, so `jira get-issue` with no `--key` fails with
the usual error. A required `password` arg is not needed, and the `run_as`
check is skipped, because nothing runs.

### Which response is used

Each response is recorded for a subcommand and a set of arguments. A call gets
the response whose arguments **all** appear in the call with the same values.
When several match, the one with the most arguments wins. So:

| Recorded args | Answers |
|---|---|
| `{"key": "OPS-101"}` | `get-issue --key OPS-101`, and `get-issue --key OPS-101 --include comments` |
| `{"key": "OPS-101", "include": "comments"}` | only the second of those, because it is more specific |
| `{}` | any other `get-issue` call: the command's fallback |

Only arguments the caller gave count (on the command line or through the
environment). Defaults don't count, so changing a default doesn't break a
recording.

`RUNSPEC_MOCK=strict` uses only a response recorded for exactly the same
arguments. Use it in tests.

### When nothing matches

Mock mode never falls back to a real run. If no response matches, the
runnable exits with code **69** and prints what *is* recorded for that
command:

```json
{
  "error": "no mock response recorded for these arguments",
  "kind": "MockNotFound",
  "runnable": "jira",
  "command": ["get-issue"],
  "args": {"key": "OPS-2"},
  "recorded": [{"key": "OPS-101"}, {"key": "OPS-102"}]
}
```

### How a mock run is recorded

The run still appears in the logs and History like any other run. Its
`run_summary` record carries `"mock": true` and the recorded exit code, so a
mock run can always be told apart from a real one.

## Record

`runspec mock record` runs the runnable **for real**, as an agent would
(`RUNSPEC_AGENT=1`, so you get the output an agent sees), and saves what it
printed and its exit code:

```bash
runspec mock record jira -- get-issue --key OPS-101
```

```text
✓  added jira get-issue {"key": "OPS-101"} → exit 0, 656 bytes
   …/runspec_jira/mocks/jira.json
```

The response is saved under the arguments you typed. Recording the same call
again replaces it. A failed call (exit 1, a "not found" error) is recorded too,
and is worth having.

Where it is saved:

| Where | When |
|---|---|
| `mocks/<runnable>.json` next to the package's `runspec.toml` | The package is a source checkout (`pip install -e`). This is the copy you commit and ship. |
| `{venv}/runspec_mocks/<runnable>.json` | The package is installed from a wheel. Mocks collected on this machine, never part of a package. |

`--to package`, `--to venv` or `--to <directory>` chooses where.

Playback reads all of these, plus the directory in `RUNSPEC_MOCK_DIR`. That
directory comes first, then the venv folder, then the package's own `mocks/`.

### Keep real data out

Mocks in a package are shipped with it. **Don't commit responses recorded from a
production system** unless they have been scrubbed:

- `--redact REGEX` (repeatable) replaces matches with `«redacted»` before saving:
  `runspec mock record jira --redact '[a-z.]+@acme\.com' -- search --jql 'project = OPS'`
- `runspec mock record` and `runspec mock check` warn about anything that looks
  like a credential (tokens, keys, `password=`).
- `password` arguments are never saved.

The cleanest way to get shippable mocks is to record against a **fake server
with made-up data**. That way the responses come from your real code and contain
nothing private. `runspec-jira` does this: `scripts/record_mocks.py` starts a
small fake Jira and records each call. Keep mocks made from real data in the
venv folder or in a private package.

You can also edit the file by hand, for example to widen a response's arguments
so it answers more calls.

## Check

```bash
runspec mock list            # what is recorded, where, and how big
runspec mock check           # exit 1 if any response no longer fits its runnable
```

`check` reports a response whose subcommand or argument no longer exists, a
value the argument would now reject, a `password` argument in a key, a
credential-looking response, and a response over 256 KB. Run it in CI after the
spec changes. In a package's own tests, the same check is
`runspec.mock.check_entries`:

```python
from pathlib import Path

from runspec import mock

SPEC = Path(__file__).parent.parent / "mypkg" / "runspec.toml"


def test_shipped_mocks_fit_the_spec() -> None:
    assert mock.check_entries("mytool", SPEC, mock.load_candidates("mytool", SPEC)) == []
```

## The file

```json
{
  "runspec_mock": 1,
  "runnable": "jira",
  "entries": [
    {
      "command": ["get-issue"],
      "args": {"key": "OPS-101"},
      "exit_code": 0,
      "stdout": {"key": "OPS-101", "summary": "Disk full on web-01: /var at 97%", "status": "Open"},
      "recorded": "2026-10-18T10:49:30Z",
      "source": "record"
    },
    {
      "command": ["get-issue"],
      "args": {},
      "exit_code": 1,
      "stdout": {"error": "Jira 404: Issue Does Not Exist", "kind": "JiraError"},
      "note": "any other key: not found"
    }
  ]
}
```

A JSON response is stored as JSON, so changes are easy to review in a merge
request. A text response is stored as a string and printed exactly as it was.
