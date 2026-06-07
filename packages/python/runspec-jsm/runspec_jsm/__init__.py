"""runspec-jsm — Jira Service Management (Data Center) service-desk runnables.

Public API (parallels runspec-linux's ``nc_send`` / runspec-windows' ``graph_get``):
the operation functions below all take an explicit :class:`JsmConfig` first
argument, so a private (e.g. Nexus-hosted) wrapper package can import them,
inject credentials in code, and bake in a default ``service_desk_id`` /
``project_key`` / ``env`` — building "one runnable per project" on top of this
package without re-implementing the REST plumbing.

``config_from_env(env=...)`` is the env-var path the bundled CLI runnables use;
``forms.resolve_request_fields`` maps friendly field names to the ``fieldId``
keys ``create_request`` requires.
"""

from runspec_jsm.client import (
    JsmConfig,
    JsmError,
    config_from_env,
    jsm_request,
)
from runspec_jsm.comments import add_comment
from runspec_jsm.forms import (
    get_request_type_fields,
    list_request_types,
    resolve_request_fields,
)
from runspec_jsm.properties import set_issue_property
from runspec_jsm.tickets import create_request, get_request
from runspec_jsm.transitions import (
    list_transitions,
    resolve_transition_id,
    transition_issue,
)

__all__ = [
    "JsmConfig",
    "JsmError",
    "config_from_env",
    "jsm_request",
    "create_request",
    "get_request",
    "add_comment",
    "list_transitions",
    "resolve_transition_id",
    "transition_issue",
    "set_issue_property",
    "list_request_types",
    "get_request_type_fields",
    "resolve_request_fields",
]
