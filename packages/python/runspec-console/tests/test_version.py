"""
test_version.py — version display, PyPI update check, and the docs URL.

The update check must degrade gracefully (network blocked → no indicator), and
the version comparison must answer "is the published release newer than mine"
without pulling in `packaging`.
"""

from __future__ import annotations

import unittest
from unittest import mock

from runspec_console import config, version


class TestVersionTuple(unittest.TestCase):
    def test_basic_parse(self) -> None:
        self.assertEqual(version._version_tuple("0.19.0"), (0, 19, 0))

    def test_prerelease_suffix_ignored(self) -> None:
        self.assertEqual(version._version_tuple("0.19.0rc1"), (0, 19, 0))

    def test_empty_segment_is_zero(self) -> None:
        self.assertEqual(version._version_tuple("1..3"), (1, 0, 3))


class TestIsNewer(unittest.TestCase):
    def test_newer_minor(self) -> None:
        self.assertTrue(version.is_newer("0.19.0", "0.18.0"))

    def test_same_is_not_newer(self) -> None:
        self.assertFalse(version.is_newer("0.18.0", "0.18.0"))

    def test_older_is_not_newer(self) -> None:
        self.assertFalse(version.is_newer("0.17.5", "0.18.0"))

    def test_differing_widths_compare_equal(self) -> None:
        # 0.19 and 0.19.0 are the same release — neither is newer.
        self.assertFalse(version.is_newer("0.19", "0.19.0"))
        self.assertFalse(version.is_newer("0.19.0", "0.19"))

    def test_patch_bump(self) -> None:
        self.assertTrue(version.is_newer("0.18.1", "0.18.0"))


class TestInstalledVersion(unittest.TestCase):
    def test_returns_non_empty_string(self) -> None:
        v = version.installed_version()
        self.assertIsInstance(v, str)
        self.assertTrue(v)

    def test_passes_package_to_metadata(self) -> None:
        with mock.patch.object(version.metadata, "version", return_value="1.2.3") as m:
            self.assertEqual(version.installed_version("mycorp-console"), "1.2.3")
        m.assert_called_once_with("mycorp-console")

    def test_absent_branded_package_returns_empty(self) -> None:
        # A branded wrapper that isn't installed has no in-tree version to borrow.
        with mock.patch.object(
            version.metadata,
            "version",
            side_effect=version.metadata.PackageNotFoundError(),
        ):
            self.assertEqual(version.installed_version("mycorp-console"), "")


def _completed(returncode: int, stdout: str = "") -> mock.Mock:
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    return proc


class TestLatestVersion(unittest.TestCase):
    def test_uses_current_interpreter_pip(self) -> None:
        # The check must query pip from sys.executable so the venv's configured
        # index (an internal proxy/mirror, if any) is what's hit — not a
        # hardcoded public PyPI URL.
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            return _completed(0, "runspec-console (0.19.0)\n")

        with mock.patch("runspec_console.version.subprocess.run", fake_run):
            version.latest_version()
        self.assertEqual(captured["cmd"][:3], [version.sys.executable, "-m", "pip"])
        self.assertIn("runspec-console", captured["cmd"])

    def test_package_argument_threaded_into_argv(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            return _completed(0, "mycorp-console (1.0.0)\n")

        with mock.patch("runspec_console.version.subprocess.run", fake_run):
            version.latest_version(package="mycorp-console")
        self.assertIn("mycorp-console", captured["cmd"])
        self.assertNotIn("runspec-console", captured["cmd"])

    def test_timeout_threaded_into_subprocess(self) -> None:
        captured: dict[str, float] = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _completed(0, "runspec-console (0.19.0)\n")

        with mock.patch("runspec_console.version.subprocess.run", fake_run):
            version.latest_version(timeout=25.0)
        self.assertEqual(captured["timeout"], 25.0)

    def test_subprocess_failure_returns_none(self) -> None:
        with mock.patch(
            "runspec_console.version.subprocess.run",
            side_effect=OSError("pip missing"),
        ):
            self.assertIsNone(version.latest_version())

    def test_nonzero_exit_returns_none(self) -> None:
        with mock.patch(
            "runspec_console.version.subprocess.run",
            return_value=_completed(1, "ERROR: no matching distribution"),
        ):
            self.assertIsNone(version.latest_version())


class TestParsePipIndex(unittest.TestCase):
    def test_latest_line_preferred(self) -> None:
        out = (
            "runspec-console (0.19.0)\n"
            "Available versions: 0.19.0, 0.18.0\n"
            "  INSTALLED: 0.18.0\n"
            "  LATEST:    0.19.0\n"
        )
        self.assertEqual(version._parse_pip_index(out), "0.19.0")

    def test_available_versions_first_entry(self) -> None:
        out = "Available versions: 0.20.0, 0.19.0, 0.18.0\n"
        self.assertEqual(version._parse_pip_index(out), "0.20.0")

    def test_falls_back_to_first_line_paren(self) -> None:
        out = "runspec-console (0.21.0)\n"
        self.assertEqual(version._parse_pip_index(out), "0.21.0")

    def test_unrecognised_output_returns_none(self) -> None:
        self.assertIsNone(version._parse_pip_index("nothing useful here\n"))

    def test_paren_fallback_matches_branded_package(self) -> None:
        out = "mycorp-console (1.4.0)\n"
        self.assertEqual(
            version._parse_pip_index(out, package="mycorp-console"), "1.4.0"
        )


class TestDocsUrl(unittest.TestCase):
    def test_default_when_no_config(self) -> None:
        with mock.patch.object(config, "read_config", return_value={}):
            self.assertEqual(config.docs_url(), config.DEFAULT_DOCS_URL)

    def test_override_from_console_section(self) -> None:
        cfg = {"console": {"docs_url": "https://docs.internal.corp/runspec/"}}
        with mock.patch.object(config, "read_config", return_value=cfg):
            self.assertEqual(config.docs_url(), "https://docs.internal.corp/runspec/")

    def test_blank_override_falls_back_to_default(self) -> None:
        cfg = {"console": {"docs_url": "   "}}
        with mock.patch.object(config, "read_config", return_value=cfg):
            self.assertEqual(config.docs_url(), config.DEFAULT_DOCS_URL)

    def test_non_dict_console_section_ignored(self) -> None:
        with mock.patch.object(config, "read_config", return_value={"console": "x"}):
            self.assertEqual(config.docs_url(), config.DEFAULT_DOCS_URL)


class TestUpdateCheckTimeout(unittest.TestCase):
    def test_default_when_no_config(self) -> None:
        with mock.patch.object(config, "read_config", return_value={}):
            self.assertEqual(
                config.update_check_timeout(), config.DEFAULT_UPDATE_CHECK_TIMEOUT
            )

    def test_override_from_console_section(self) -> None:
        cfg = {"console": {"update_check_timeout": 30}}
        with mock.patch.object(config, "read_config", return_value=cfg):
            self.assertEqual(config.update_check_timeout(), 30.0)

    def test_clamped_to_bounds(self) -> None:
        for raw, expected in ((0, 1.0), (-5, 1.0), (999, 120.0)):
            cfg = {"console": {"update_check_timeout": raw}}
            with mock.patch.object(config, "read_config", return_value=cfg):
                self.assertEqual(config.update_check_timeout(), expected)

    def test_non_numeric_ignored(self) -> None:
        for bad in ("20", True, None, {}):
            cfg = {"console": {"update_check_timeout": bad}}
            with mock.patch.object(config, "read_config", return_value=cfg):
                self.assertEqual(
                    config.update_check_timeout(), config.DEFAULT_UPDATE_CHECK_TIMEOUT
                )


if __name__ == "__main__":
    unittest.main()
