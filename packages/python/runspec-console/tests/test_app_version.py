"""
test_app_version.py — Bridge.app_version() never returns blank.

A white-label install points branding at its adapter distribution; if that
distribution's version can't be read (renamed/editable install, an untraceable
provider) the footer must still show a version — runspec-console's own — rather
than going empty. (The empty footer is what made the version "disappear" on a
real white-label deploy.)
"""

from __future__ import annotations

from runspec_console.branding import Branding
from runspec_console.bridge import Bridge


def _bridge_with_branding(branding_or_exc):
    class _B:
        app_version = Bridge.app_version

        def _get_branding(self):
            if isinstance(branding_or_exc, Exception):
                raise branding_or_exc
            return branding_or_exc

    return _B()


def _branding(version_package: str) -> Branding:
    return Branding(
        title="X", docs_url="https://example/docs", version_package=version_package
    )


def test_falls_back_when_branded_package_unresolvable():
    v = _bridge_with_branding(_branding("nonexistent-pkg-zzz")).app_version()
    assert v  # non-empty — runspec-console's own version


def test_uses_runspec_console_when_branded_is_itself():
    v = _bridge_with_branding(_branding("runspec-console")).app_version()
    assert v


def test_survives_branding_error():
    # A broken branding resolution must not blank the footer.
    v = _bridge_with_branding(RuntimeError("boom")).app_version()
    assert v


def test_empty_version_package_falls_back():
    v = _bridge_with_branding(_branding("")).app_version()
    assert v
