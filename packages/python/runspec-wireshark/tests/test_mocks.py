"""The shipped mock responses (runspec_wireshark/mocks/pcap.json) fit the spec
and play back. They are re-recorded with scripts/record_mocks.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import runspec

mock = pytest.importorskip("runspec.mock")

SPEC = Path(__file__).parent.parent / "runspec_wireshark" / "runspec.toml"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # Only the package's own mocks: no venv overlay, no RUNSPEC_MOCK_DIR.
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.delenv("RUNSPEC_MOCK_DIR", raising=False)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")


def test_shipped_mocks_fit_the_spec() -> None:
    candidates = mock.load_candidates("pcap", SPEC)
    assert candidates, "runspec_wireshark/mocks/pcap.json is missing or empty"
    assert mock.check_entries("pcap", SPEC, candidates) == []


def test_every_verb_has_a_mock() -> None:
    import tomllib

    verbs = set(tomllib.loads(SPEC.read_text())["pcap"]["commands"])
    recorded = {c.entry["command"][0] for c in mock.load_candidates("pcap", SPEC)}
    assert verbs == recorded


def test_shipped_mocks_hold_no_temp_paths() -> None:
    text = (SPEC.parent / "mocks" / "pcap.json").read_text(encoding="utf-8")
    # The recording ran in a temp folder; its paths were replaced with /var/tmp.
    assert '"/tmp/' not in text and "\\\\Temp\\\\" not in text


@pytest.mark.parametrize(
    ("argv", "check"),
    [
        (["summary", "/data/anything.pcapng"], lambda out: out["tcp"]["reset"] == 4),
        (["follow", "/data/anything.pcapng", "2"], lambda out: "database timeout" in out["chunks"][-1]["text"]),
        (["tls", "/data/anything.pcapng", "--failed"], lambda out: out["alerts"] == {"unknown_ca": 1}),
        (["capture", "-i", "eth0", "--duration", "30"], lambda out: out["packets"] == 96),
    ],
)
def test_plays_back_for_any_file(capsys, argv, check, monkeypatch) -> None:
    monkeypatch.setenv("RUNSPEC_MOCK", "1")
    with pytest.raises(SystemExit) as exc:
        runspec.parse("pcap", argv=argv, config_path=SPEC)
    assert exc.value.code == 0
    assert check(json.loads(capsys.readouterr().out))
