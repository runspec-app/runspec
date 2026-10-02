"""
runspec — A language-agnostic, TOML-based interface specification
for anything runnable.

Public API:
    parse()         → RunSpec   Parse arguments for the calling script
    load_spec()     → RunSpec   Load spec without parsing argv
    register_type() → None      Register a custom type coercer
    audit()         → dict      Record an append-only audit event (see docs/audit.md)

Test helpers live in ``runspec.testing`` (imported explicitly from tests):
    from runspec.testing import ParseHarness
"""

from importlib.metadata import version as _version

from runspec.auditlog import AuditError, audit
from runspec.models import Arg, Group, RunSpec
from runspec.parser import load_spec, parse
from runspec.types import register_type

__version__ = _version("runspec")
__all__ = [
    "parse",
    "load_spec",
    "register_type",
    "audit",
    "AuditError",
    "RunSpec",
    "Arg",
    "Group",
]
