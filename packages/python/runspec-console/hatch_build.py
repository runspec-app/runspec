"""
hatch_build.py — ensure the console UI bundle is fresh whenever the wheel is built.

The Vite frontend (``packages/console-ui``) compiles into
``runspec_console/dist/`` and is force-included in the wheel (see pyproject).
``dist/`` is gitignored, so it only exists if someone ran ``npm run build`` — and
nothing forced that during a plain ``python -m hatchling build`` / ``python -m
build``. The result was wheels that silently shipped a *stale* UI (a markdown/UX
change was in the source but not in the published bundle).

This hatchling build hook closes that gap: when building the wheel it rebuilds the
UI **if the bundle is missing or older than the UI source**, so a built wheel
always carries the current frontend — no matter how it was built. It is a no-op
when the bundle is already fresh (CI builds the UI explicitly first, so the hook
sees a fresh ``dist/`` and skips), and degrades gracefully when Node isn't present
but a bundle already exists.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# hatchling is the build backend — present whenever a wheel is built, but not a
# test/runtime dependency. Guard the import so the pure ``ui_is_stale`` helper
# (and its tests) work without it; the hook class is only defined when hatchling
# is available, which is exactly when a build runs.
try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface

    _HATCHLING_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in the no-hatchling test env
    BuildHookInterface = object  # type: ignore[assignment,misc]
    _HATCHLING_AVAILABLE = False

# UI source whose mtime decides whether the built bundle is stale. Anything here
# newer than dist/index.html means the bundle predates a source change.
_WATCH_FILES = ("package.json", "package-lock.json", "index.html", "vite.config.ts")


def ui_is_stale(ui_dir: Path, index_html: Path) -> bool:
    """True if the built ``index_html`` is missing or older than the UI source.

    Pure (mtime-based) so it can be unit-tested. A gitignored, never-built bundle
    reads as missing → stale; a bundle built before an edit reads as older →
    stale; a freshly built one reads as up to date → not stale."""
    if not index_html.is_file():
        return True
    built = index_html.stat().st_mtime
    candidates: list[Path] = [ui_dir / name for name in _WATCH_FILES]
    src = ui_dir / "src"
    if src.is_dir():
        candidates.extend(src.rglob("*"))
    newest = max(
        (p.stat().st_mtime for p in candidates if p.is_file()),
        default=0.0,
    )
    return newest > built


class CustomBuildHook(BuildHookInterface):
    """Rebuild the Vite UI into ``runspec_console/dist`` before the wheel is packed."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Only the wheel force-includes dist/; sdists/editable metadata don't need it.
        if self.target_name != "wheel":
            return

        root = Path(self.root)  # packages/python/runspec-console
        ui_dir = (root.parent.parent / "console-ui").resolve()
        index_html = root / "runspec_console" / "dist" / "index.html"

        # Building outside the monorepo (no UI source to build from) — trust
        # whatever dist/ is force-included rather than failing.
        if not ui_dir.is_dir():
            return

        if not ui_is_stale(ui_dir, index_html):
            return

        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if npm is None:
            if index_html.is_file():
                self.app.display_warning(
                    "console UI source is newer than the built bundle, but npm is "
                    "not available to rebuild it — shipping the existing dist/. "
                    "Install Node and rebuild to refresh the UI."
                )
                return
            raise RuntimeError(
                "console UI bundle (runspec_console/dist) is missing and npm is not "
                "available to build it. Install Node 18+ (or run `npm run build` in "
                "packages/console-ui) before building the wheel."
            )

        if not (ui_dir / "node_modules").is_dir():
            self._run([npm, "ci"], ui_dir)
        self._run([npm, "run", "build"], ui_dir)

        if not index_html.is_file():
            raise RuntimeError(
                f"UI build completed but {index_html} is missing — the wheel would "
                "ship without a frontend."
            )

    def _run(self, cmd: list[str], cwd: Path) -> None:
        self.app.display_info(
            f"runspec-console: rebuilding UI → {' '.join(cmd)} (in {cwd})"
        )
        # npm on Windows is a .cmd shim; run through the shell there so PATH /
        # extension resolution works (mirrors dev_build.py).
        is_windows = sys.platform == "win32"
        result = subprocess.run(cmd, cwd=str(cwd), shell=is_windows)
        if result.returncode != 0:
            raise RuntimeError(f"UI build step failed: {' '.join(cmd)}")
