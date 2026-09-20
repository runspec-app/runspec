# runspec-snow

ServiceNow ITSM runnables for [runspec](https://pypi.org/project/runspec/) — the
ServiceNow counterpart to [`runspec-jsm`](../runspec-jsm), for teams migrating
service-desk tasks to ServiceNow. `pip install` it into a venv and the runnables
are discoverable by `runspec local`, `runspec serve` (MCP), and runspec-console.

Pure HTTP via `httpx` over the **Table API** (`/api/now/table/{table}`) for CRUD
and the **Service Catalog API** (`/api/sn_sc/servicecatalog`) for discovery. All
runnables emit JSON on stdout. Tuned for `incident`, `sc_task` and `sc_req_item`,
but `--table` accepts any table.

## Install

```
pip install runspec-snow
```

## Runnables

| Kind | Runnables |
|---|---|
| Records | `snow-create-record`, `snow-get-record` (by sys_id or number), `snow-set-field` |
| Comments | `snow-add-comment` (`--internal` → `work_notes`, else `comments`) |
| State | `snow-list-states`, `snow-update-state` (validated `state` choice, label→value via `sys_choice`) |
| Catalog discovery | `snow-list-catalog-items`, `snow-list-variables` |

## Auth — env vars only

Set the instance and one credential; an OAuth/bearer token wins over Basic:

```
export SNOW_BASE_URL=https://acme.service-now.com   # or SNOW_INSTANCE=acme
export SNOW_TOKEN=<oauth-token>                      # Bearer
# …or Basic:
export SNOW_USER=svc-itsm
export SNOW_PASSWORD=<password>
```

Every runnable takes `--env dev|uat|prod`, selecting `SNOW_<ENV>_*` prefixed
vars so one install serves several instances. Same **deploy-determined identity
— no sudo flag** model as `runspec-jsm`.

```
snow-create-record --table incident --field short_description="Printer offline" --env prod
snow-get-record --table incident --number INC0012345
snow-add-comment --table incident --sys-id <id> --body "On it" --internal
snow-update-state --table incident --number INC0012345 --state "In Progress"
```

## Public Python API

Every operation takes a `SnowConfig` first arg, so a private package can inject
credentials and bake in a default `default_table` / `env` to build "one runnable
per table" on top:

```python
from runspec_snow import config_from_env, create_record, add_comment, update_state

cfg = config_from_env(env="prod")
create_record(cfg, table="incident", fields={"short_description": "Printer offline"})
```

Operations raise `SnowError` on failure.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
