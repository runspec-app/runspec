"""
test_branding.py — white-label identity derived from the active LLM plugin or the
config-seed package.

The window title, docs link, and update-check package follow whichever installed
distribution supplies the configured provider, else the single package that
contributes a config seed (the AI-off / adapter-less white-label case). The title
is the prettified distribution name and is independent of
``[console.theme] brand_name`` (which titles the sidebar). A built-in / unset
provider with no seed package falls back to runspec's own identity. Resolution
must never raise into the UI.
"""

from __future__ import annotations

import unittest
from unittest import mock

from runspec_console import branding
from runspec_console.config import DEFAULT_DOCS_URL
from runspec_console.configseed import ENTRY_POINT_GROUP as CONFIG_SEED_GROUP


class _FakeMetadata:
    """Stands in for ``Distribution.metadata`` (an email.message-like object)."""

    def __init__(self, project_urls: list[str] | None = None, home_page=None) -> None:
        self._project_urls = project_urls
        self._home_page = home_page

    def get_all(self, key: str):
        if key == "Project-URL":
            return self._project_urls
        return None

    def __getitem__(self, key: str):
        if key == "Home-page" and self._home_page is not None:
            return self._home_page
        raise KeyError(key)


class _FakeDist:
    def __init__(
        self, name, version="1.0.0", project_urls=None, home_page=None
    ) -> None:
        self.name = name
        self.version = version
        self.metadata = _FakeMetadata(project_urls, home_page)


class _FakeEP:
    def __init__(self, name, dist) -> None:
        self.name = name
        self.dist = dist


def _resolve(
    config,
    *,
    entry_points=(),
    seed_entry_points=(),
    packages_distributions=None,
    distribution=None,
):
    """Resolve branding with importlib.metadata stubbed inside branding.py.

    ``entry_points`` are advertised in the LLM-adapter group, ``seed_entry_points``
    in the config-seed group — the mock dispatches on the ``group`` kwarg, just as
    the real ``importlib.metadata.entry_points`` filters by group.
    """
    pd = packages_distributions or (lambda: {})

    def fake_dist(name):
        if distribution is None:
            raise branding.meta.PackageNotFoundError(name)
        return distribution(name)

    def fake_entry_points(*, group=None):
        if group == CONFIG_SEED_GROUP:
            return list(seed_entry_points)
        return list(entry_points)

    with (
        mock.patch.object(branding.meta, "entry_points", fake_entry_points),
        mock.patch.object(branding.meta, "packages_distributions", pd),
        mock.patch.object(branding.meta, "distribution", fake_dist),
    ):
        return branding.resolve_branding(config)


class TestPrettifyDistName(unittest.TestCase):
    def test_hyphenated(self) -> None:
        self.assertEqual(
            branding._prettify_dist_name("mycorp-console"), "Mycorp Console"
        )

    def test_underscored(self) -> None:
        self.assertEqual(branding._prettify_dist_name("acme_ops"), "Acme Ops")

    def test_single_token(self) -> None:
        self.assertEqual(branding._prettify_dist_name("runspec"), "Runspec")

    def test_empty(self) -> None:
        self.assertEqual(branding._prettify_dist_name(""), "")


class TestEntryPointProvider(unittest.TestCase):
    def test_full_branding_from_dist(self) -> None:
        dist = _FakeDist(
            "mycorp-console",
            project_urls=[
                "Documentation, https://docs.mycorp.com",
                "Homepage, https://mycorp.com",
            ],
        )
        b = _resolve(
            {"llm": {"provider": "mycorp"}},
            entry_points=[_FakeEP("mycorp", dist)],
        )
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.docs_url, "https://docs.mycorp.com")
        self.assertEqual(b.version_package, "mycorp-console")

    def test_homepage_when_no_documentation(self) -> None:
        dist = _FakeDist(
            "mycorp-console", project_urls=["Homepage, https://mycorp.com"]
        )
        b = _resolve(
            {"llm": {"provider": "mycorp"}}, entry_points=[_FakeEP("mycorp", dist)]
        )
        self.assertEqual(b.docs_url, "https://mycorp.com")

    def test_legacy_home_page_field(self) -> None:
        dist = _FakeDist("mycorp-console", home_page="https://legacy.mycorp.com")
        b = _resolve(
            {"llm": {"provider": "mycorp"}}, entry_points=[_FakeEP("mycorp", dist)]
        )
        self.assertEqual(b.docs_url, "https://legacy.mycorp.com")

    def test_no_urls_falls_back_to_default_docs(self) -> None:
        dist = _FakeDist("mycorp-console")
        b = _resolve(
            {"llm": {"provider": "mycorp"}}, entry_points=[_FakeEP("mycorp", dist)]
        )
        self.assertEqual(b.docs_url, DEFAULT_DOCS_URL)
        # title / version still come from the dist.
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.version_package, "mycorp-console")

    def test_multiple_plugins_active_provider_wins(self) -> None:
        a = _FakeDist(
            "acme-console", project_urls=["Documentation, https://docs.acme.com"]
        )
        m = _FakeDist(
            "mycorp-console", project_urls=["Documentation, https://docs.mycorp.com"]
        )
        b = _resolve(
            {"llm": {"provider": "mycorp"}},
            entry_points=[_FakeEP("acme", a), _FakeEP("mycorp", m)],
        )
        self.assertEqual(b.version_package, "mycorp-console")
        self.assertEqual(b.docs_url, "https://docs.mycorp.com")


class TestBuiltinAndUnset(unittest.TestCase):
    def _assert_defaults(self, b) -> None:
        self.assertEqual(b.title, "runspec console")
        self.assertEqual(b.docs_url, DEFAULT_DOCS_URL)
        self.assertEqual(b.version_package, "runspec-console")

    def test_builtin_provider_uses_defaults(self) -> None:
        dist = _FakeDist("should-not-be-used")
        b = _resolve(
            {"llm": {"provider": "anthropic"}}, entry_points=[_FakeEP("mycorp", dist)]
        )
        self._assert_defaults(b)

    def test_unset_provider_uses_defaults(self) -> None:
        self._assert_defaults(_resolve({}))

    def test_blank_provider_uses_defaults(self) -> None:
        self._assert_defaults(_resolve({"llm": {"provider": "   "}}))


class TestConfigDocsUrlTier1(unittest.TestCase):
    def test_config_docs_url_wins_over_dist(self) -> None:
        dist = _FakeDist(
            "mycorp-console", project_urls=["Documentation, https://docs.mycorp.com"]
        )
        b = _resolve(
            {
                "llm": {"provider": "mycorp"},
                "console": {"docs_url": "https://internal.example/docs"},
            },
            entry_points=[_FakeEP("mycorp", dist)],
        )
        self.assertEqual(b.docs_url, "https://internal.example/docs")
        # title/version still derive from the dist.
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.version_package, "mycorp-console")

    def test_config_docs_url_wins_with_default_provider(self) -> None:
        b = _resolve({"console": {"docs_url": "https://internal.example/docs"}})
        self.assertEqual(b.docs_url, "https://internal.example/docs")


class TestDottedPathProvider(unittest.TestCase):
    def test_resolves_via_packages_distributions(self) -> None:
        dist = _FakeDist(
            "mycorp-console", project_urls=["Documentation, https://docs.mycorp.com"]
        )
        b = _resolve(
            {"llm": {"provider": "mypkg.mod:Cls"}},
            packages_distributions=lambda: {"mypkg": ["mycorp-console"]},
            distribution=lambda name: dist,
        )
        self.assertEqual(b.version_package, "mycorp-console")
        self.assertEqual(b.docs_url, "https://docs.mycorp.com")

    def test_unresolvable_dotted_path_uses_defaults(self) -> None:
        b = _resolve(
            {"llm": {"provider": "mypkg.mod:Cls"}},
            packages_distributions=lambda: {},
        )
        self.assertEqual(b.version_package, "runspec-console")
        self.assertEqual(b.title, "runspec console")


class TestConfigSeedBranding(unittest.TestCase):
    """An AI-off / adapter-less white-label package brands from the config seed
    it contributes, since there is no active LLM provider to trace."""

    def test_seed_dist_brands_when_no_provider(self) -> None:
        dist = _FakeDist(
            "mycorp-console", project_urls=["Documentation, https://docs.mycorp.com"]
        )
        b = _resolve(
            {"console": {"ai": False}},  # no [llm] provider
            seed_entry_points=[_FakeEP("mycorp", dist)],
        )
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.docs_url, "https://docs.mycorp.com")
        self.assertEqual(b.version_package, "mycorp-console")

    def test_active_provider_dist_wins_over_seed(self) -> None:
        adapter = _FakeDist(
            "acme-console", project_urls=["Documentation, https://docs.acme.com"]
        )
        seed = _FakeDist("seed-only")
        b = _resolve(
            {"llm": {"provider": "acme"}},
            entry_points=[_FakeEP("acme", adapter)],
            seed_entry_points=[_FakeEP("seed", seed)],
        )
        self.assertEqual(b.title, "Acme Console")
        self.assertEqual(b.version_package, "acme-console")

    def test_ambiguous_seed_dists_use_defaults(self) -> None:
        a = _FakeDist("acme-console")
        m = _FakeDist("mycorp-console")
        b = _resolve(
            {},
            seed_entry_points=[_FakeEP("acme", a), _FakeEP("mycorp", m)],
        )
        self.assertEqual(b.title, "runspec console")
        self.assertEqual(b.version_package, "runspec-console")

    def test_seed_dist_that_is_runspec_console_is_ignored(self) -> None:
        # A seed contributed by runspec-console itself is not white-label branding.
        dist = _FakeDist("runspec-console")
        b = _resolve({}, seed_entry_points=[_FakeEP("self", dist)])
        self.assertEqual(b.title, "runspec console")
        self.assertEqual(b.version_package, "runspec-console")


class TestBrandNameDoesNotTitleWindow(unittest.TestCase):
    """[console.theme] brand_name titles the *sidebar* (frontend), never the
    window title — the window title is the prettified distribution name, so the
    two aren't duplicated."""

    def test_brand_name_does_not_set_title_without_dist(self) -> None:
        b = _resolve({"console": {"ai": False, "theme": {"brand_name": "Acme Fleet"}}})
        self.assertEqual(b.title, "runspec console")  # not "Acme Fleet"
        self.assertEqual(b.version_package, "runspec-console")

    def test_title_is_prettified_dist_name_ignoring_brand_name(self) -> None:
        dist = _FakeDist(
            "mycorp-console", project_urls=["Documentation, https://docs.mycorp.com"]
        )
        b = _resolve(
            {
                "llm": {"provider": "mycorp"},
                "console": {"theme": {"brand_name": "Acme Fleet"}},
            },
            entry_points=[_FakeEP("mycorp", dist)],
        )
        # brand_name is ignored for the title; the package name is prettified.
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.version_package, "mycorp-console")
        self.assertEqual(b.docs_url, "https://docs.mycorp.com")

    def test_seed_only_white_label_title_is_prettified_dist_name(self) -> None:
        # The reported case: adapter-less package with brand_name set. The window
        # title must be the package name (Jason Console), not the brand_name.
        dist = _FakeDist("jason-console")
        b = _resolve(
            {"console": {"ai": False, "theme": {"brand_name": "Jason's Fleet"}}},
            seed_entry_points=[_FakeEP("jason", dist)],
        )
        self.assertEqual(b.title, "Jason Console")
        self.assertEqual(b.version_package, "jason-console")


class TestNeverRaises(unittest.TestCase):
    def test_metadata_access_raises_falls_back(self) -> None:
        class _Boom:
            name = "mycorp-console"

            @property
            def metadata(self):
                raise RuntimeError("metadata blew up")

        b = _resolve(
            {"llm": {"provider": "mycorp"}},
            entry_points=[_FakeEP("mycorp", _Boom())],
        )
        # name-derived fields still resolve; docs degrades to default.
        self.assertEqual(b.title, "Mycorp Console")
        self.assertEqual(b.version_package, "mycorp-console")
        self.assertEqual(b.docs_url, DEFAULT_DOCS_URL)

    def test_entry_points_enumeration_raises_falls_back(self) -> None:
        with mock.patch.object(
            branding.meta, "entry_points", side_effect=RuntimeError("boom")
        ):
            b = branding.resolve_branding({"llm": {"provider": "mycorp"}})
        self.assertEqual(b.title, "runspec console")
        self.assertEqual(b.version_package, "runspec-console")


if __name__ == "__main__":
    unittest.main()
