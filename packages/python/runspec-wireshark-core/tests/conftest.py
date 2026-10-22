"""Shared fixtures: the sample capture (tests/data, made by
scripts/make_sample_capture.py) and a config for the real tshark."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runspec_wireshark_core import WiresharkConfig

DATA = Path(__file__).parent / "data"
SAMPLE = DATA / "sample.pcapng"
KEYS = DATA / "sample.keys"

needs_tshark = pytest.mark.skipif(shutil.which("tshark") is None, reason="tshark not installed")


@pytest.fixture
def config() -> WiresharkConfig:
    return WiresharkConfig()


@pytest.fixture
def sample() -> str:
    return str(SAMPLE)
