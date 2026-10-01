#!/usr/bin/env python3
"""Build the runspec-mcp Claude Code marketplaces into the docs site.

Run after ``mkdocs build`` (the docs workflow does): writes into ``--out``

- ``plugins/runspec-mcp-<version>.zip`` — the plugin, rendered to start the
  gateway with ``uvx`` (no separate pip install needed);
- ``marketplace.json`` — marketplace ``runspec``: that zip as an ``archive``
  source, with its SHA-256;
- ``marketplace-pip.json`` — marketplace ``runspec-pip``: a ``command`` source
  that runs ``runspec-mcp claude-plugin`` from a pip-installed gateway.

Both are generated together, so the published checksum always matches the
published zip, and the version comes from runspec-mcp's ``pyproject.toml``.
Users add them with ``/plugin marketplace add https://runspec.app/<file>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packages" / "python" / "runspec-mcp"
sys.path.insert(0, str(PKG))

from runspec_mcp import claudeplugin  # noqa: E402

# Fixed timestamp so the same plugin source always zips to the same bytes (and
# so the same checksum) — a docs redeploy with no plugin change doesn't make
# Claude Code see a "changed" plugin.
_EPOCH = (2020, 1, 1, 0, 0, 0)


def _version() -> str:
    data = tomllib.loads((PKG / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _zip_dir(src: Path, dest: Path, top: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = path.relative_to(src).as_posix()
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=_EPOCH)
            info.external_attr = (0o755 if path.suffix == ".py" else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def build(out: Path, base_url: str) -> dict[str, Path]:
    version = _version()
    zip_rel = f"plugins/runspec-mcp-{version}.zip"
    zip_path = out / zip_rel
    with tempfile.TemporaryDirectory() as tmp:
        plugin = claudeplugin.render(
            Path(tmp) / "runspec-mcp", server=claudeplugin.UVX_COMMAND, version=version
        )
        _zip_dir(plugin, zip_path, "runspec-mcp")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    zip_market = claudeplugin.marketplace(
        "runspec",
        {"source": "archive", "url": f"{base_url}/{zip_rel}", "sha256": digest},
        description="runspec-mcp for Claude Code — starts the gateway with uvx (no pip install needed).",
    )
    pip_market = claudeplugin.marketplace(
        "runspec-pip",
        {"source": "command", "command": "runspec-mcp claude-plugin", "timeout": 60},
        description="runspec-mcp for Claude Code — uses an installed runspec-mcp (pip install runspec-mcp first).",
    )
    written = {"zip": zip_path}
    for name, data in (
        ("marketplace.json", zip_market),
        ("marketplace-pip.json", pip_market),
    ):
        path = out / name
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        written[name] = path
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out",
        default=str(ROOT / "site"),
        help="the built docs site (default: ./site)",
    )
    ap.add_argument(
        "--base-url", default="https://runspec.app", help="where the site is served"
    )
    args = ap.parse_args(argv)
    for label, path in build(Path(args.out), args.base_url.rstrip("/")).items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
