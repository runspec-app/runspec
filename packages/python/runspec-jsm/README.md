# runspec-jsm

Jira Service Management (Data Center) service-desk runnables for
[runspec](https://pypi.org/project/runspec/) — for service-desk **agents and
customers**, not Jira product admins. `pip install` it into a venv and the
runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

Pure HTTP via `httpx` over a mixed REST surface (`/rest/servicedeskapi` for
raise/comment/discovery, `/rest/api/2` for transitions/properties), so it runs
unchanged on Windows and Linux. All runnables emit JSON on stdout.

## Install

```
pip install runspec-jsm
```

## Runnables

| Kind | Runnables |
|---|---|
| Requests | `jsm-raise-request`, `jsm-get-request`, `jsm-add-comment` (public or internal) |
| Transitions | `jsm-list-transitions`, `jsm-transition-issue` (by name or id) |
| Properties | `jsm-set-property` |
| Discovery | `jsm-list-request-types`, `jsm-list-fields` |

## Auth — env vars only

Set the base URL and one credential; a Personal Access Token wins over Basic:

```
export JSM_BASE_URL=https://jsm.example.com
export JSM_PAT=<personal-access-token>          # Bearer
# …or Basic:
export JSM_USER=svc-desk
export JSM_PASSWORD=<password>
```

Every runnable takes `--env dev|uat|prod`, which selects `JSM_<ENV>_*` prefixed
vars (e.g. `JSM_PROD_BASE_URL`) so one install serves several environments.

**Identity is deploy-determined — there is no sudo flag.** Run it as yourself
locally, or as the service account on a server (`sudo -u <svc> -E`, systemd, or
a login shell); it makes no OS calls.

```
jsm-list-request-types --env prod
jsm-raise-request --request-type-id 42 --summary "VPN down" --env prod
jsm-add-comment --issue SD-1234 --body "Investigating" --internal
jsm-transition-issue --issue SD-1234 --transition "Resolve this issue"
```

## Public Python API

Every operation takes a `JsmConfig` first arg, so a private (e.g. Nexus-hosted)
wrapper can inject credentials in code and bake in a default `service_desk_id` /
`project_key` / `env` to build "one runnable per project" on top:

```python
from runspec_jsm import config_from_env, create_request, add_comment, transition_issue

cfg = config_from_env(env="prod")
create_request(cfg, request_type_id=42, summary="VPN down")
```

Operations raise `JsmError` on failure.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
