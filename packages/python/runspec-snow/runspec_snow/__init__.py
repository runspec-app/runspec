"""runspec-snow — ServiceNow ITSM runnables for runspec.

Public API (parallels runspec-jsm's operation functions / runspec-linux's
``nc_send``): the operation functions below all take an explicit
:class:`SnowConfig` first argument, so a private (e.g. Nexus-hosted) wrapper
package can import them, inject credentials in code, and bake in a default
``default_table`` / ``env`` — building "one runnable per table" on top of this
package without re-implementing the REST plumbing.

``config_from_env(env=...)`` is the env-var path the bundled CLI runnables use;
``states.resolve_state_value`` maps a state label to the choice value
``update_state`` requires.
"""

from runspec_snow.catalog import get_catalog_item_variables, list_catalog_items
from runspec_snow.client import (
    SnowConfig,
    SnowError,
    config_from_env,
    resolve_table,
    snow_request,
)
from runspec_snow.comments import add_comment
from runspec_snow.fields import set_fields
from runspec_snow.records import create_record, get_record, resolve_sys_id
from runspec_snow.states import list_states, resolve_state_value, update_state

__all__ = [
    "SnowConfig",
    "SnowError",
    "config_from_env",
    "snow_request",
    "resolve_table",
    "create_record",
    "get_record",
    "resolve_sys_id",
    "add_comment",
    "list_states",
    "resolve_state_value",
    "update_state",
    "set_fields",
    "list_catalog_items",
    "get_catalog_item_variables",
]
