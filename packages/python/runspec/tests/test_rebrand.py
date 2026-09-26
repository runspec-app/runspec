"""Tests for runspec.rebrand / `runspec rebrand`."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

import runspec.rebrand as rw
from runspec.cli import main as cli_main

METADATA = """Metadata-Version: 2.4
Name: runspec-linux
Version: 0.14.0
Summary: Linux system admin runnables for runspec
Requires-Python: >=3.10
Requires-Dist: runspec-linux-core>=0.7.0
Requires-Dist: runspec-webops-core>=0.3.0
Requires-Dist: runspec>=0.50.1
Requires-Dist: pytest>=8.0; extra == "dev"
Description-Content-Type: text/markdown

# runspec-linux

Requires-Dist: this line is body text, not a header.
"""


def _make_wheel(tmp_path: Path) -> Path:
    path = tmp_path / "runspec_linux-0.14.0-py3-none-any.whl"
    info = "runspec_linux-0.14.0.dist-info/"
    files = {
        "runspec_linux/__init__.py": b"from runspec_linux_core import x\n",
        "runspec_linux/runspec.toml": b'[system-info]\ndescription = "x"\n',
        "runspec_linux-0.14.0.data/scripts/helper": b"#!/bin/sh\n",
        info + "METADATA": METADATA.encode(),
        info + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        info + "entry_points.txt": b"[console_scripts]\nsystem-info = runspec_linux.system_info:main\n",
        info + "RECORD": b"stale,,\n",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _read(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_renames_dist_and_keeps_code(tmp_path: Path) -> None:
    src = _make_wheel(tmp_path)
    res = rw.rebrand_wheel(src, "mycorp-linux", tmp_path / "out")

    assert res.output.name == "mycorp_linux-0.14.0-py3-none-any.whl"
    assert (res.old_name, res.new_name, res.version) == ("runspec-linux", "mycorp-linux", "0.14.0")
    out = _read(res.output)
    old = _read(src)
    # Code, runspec.toml and entry points byte-identical; import package unchanged.
    assert out["runspec_linux/__init__.py"] == old["runspec_linux/__init__.py"]
    assert out["runspec_linux/runspec.toml"] == old["runspec_linux/runspec.toml"]
    assert out["mycorp_linux-0.14.0.dist-info/entry_points.txt"] == old["runspec_linux-0.14.0.dist-info/entry_points.txt"]
    assert "mycorp_linux-0.14.0.data/scripts/helper" in out
    assert not any(n.startswith("runspec_linux-0.14.0.") for n in out)
    assert res.runspec_tomls == ["runspec_linux/runspec.toml"]
    assert res.requires_runspec


def test_metadata_header_rewritten_body_untouched(tmp_path: Path) -> None:
    res = rw.rebrand_wheel(_make_wheel(tmp_path), "mycorp-linux", tmp_path / "out", rename_deps={"Runspec_Linux_Core": "mycorp-linux-core"})
    meta = _read(res.output)["mycorp_linux-0.14.0.dist-info/METADATA"].decode()
    assert "Name: mycorp-linux\n" in meta
    assert "Requires-Dist: mycorp-linux-core>=0.7.0\n" in meta
    assert "Requires-Dist: runspec-webops-core>=0.3.0\n" in meta  # not renamed
    assert "Requires-Dist: runspec>=0.50.1\n" in meta
    assert meta.endswith("Requires-Dist: this line is body text, not a header.\n")
    assert res.renamed_deps == [("runspec-linux-core", "mycorp-linux-core")]


def test_record_matches_contents(tmp_path: Path) -> None:
    res = rw.rebrand_wheel(_make_wheel(tmp_path), "mycorp-linux", tmp_path / "out")
    out = _read(res.output)
    rows = list(csv.reader(io.StringIO(out["mycorp_linux-0.14.0.dist-info/RECORD"].decode())))
    listed = {r[0] for r in rows}
    assert listed == set(out)
    for name, digest, size in rows:
        if name.endswith("RECORD"):
            assert (digest, size) == ("", "")
            continue
        data = out[name]
        expect = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        assert (digest, int(size)) == (expect, len(data))


def test_version_override(tmp_path: Path) -> None:
    res = rw.rebrand_wheel(_make_wheel(tmp_path), "mycorp-linux", tmp_path / "out", new_version="0.14.0.post1")
    assert res.output.name == "mycorp_linux-0.14.0.post1-py3-none-any.whl"
    meta = _read(res.output)["mycorp_linux-0.14.0.post1.dist-info/METADATA"].decode()
    assert "Version: 0.14.0.post1\n" in meta


def test_unused_rename_is_reported_not_fatal(tmp_path: Path) -> None:
    res = rw.rebrand_wheel(_make_wheel(tmp_path), "mycorp-linux", tmp_path / "out", rename_deps={"runspec-fs-core": "mycorp-fs-core"})
    assert res.unused_renames == ["runspec-fs-core"]


def test_rejects_bad_input(tmp_path: Path) -> None:
    with pytest.raises(rw.RebrandError):
        rw.rebrand_wheel(_make_wheel(tmp_path), "bad name!", tmp_path / "out")
    bogus = tmp_path / "notawheel.zip"
    bogus.write_bytes(b"")
    with pytest.raises(rw.RebrandError):
        rw.rebrand_wheel(bogus, "mycorp-linux", tmp_path / "out")
    with pytest.raises(rw.RebrandError):
        rw.parse_renames(["no-equals-sign"])


def test_rebrand_accepts_wheel_path(tmp_path: Path) -> None:
    res = rw.rebrand(str(_make_wheel(tmp_path)), "mycorp-linux", tmp_path / "out")
    assert res.output.is_file()


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_wheel(tmp_path)
    argv = ["runspec", "rebrand", str(src), "--name", "mycorp-linux", "--rename-dep", "runspec-linux-core=mycorp-linux-core", "--out", str(tmp_path / "out")]
    monkeypatch.setattr("sys.argv", argv)
    cli_main()
    assert (tmp_path / "out" / "mycorp_linux-0.14.0-py3-none-any.whl").is_file()
    out = capsys.readouterr().out
    assert "runspec-linux 0.14.0 → mycorp-linux 0.14.0" in out
    assert "dependency runspec-linux-core → mycorp-linux-core" in out


def test_cli_error_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_wheel(tmp_path)
    monkeypatch.setattr("sys.argv", ["runspec", "rebrand", str(src), "--name", "mycorp-linux", "--rename-dep", "bogus", "--out", str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 1
