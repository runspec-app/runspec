"""summarize_log clusters, bounds its output, filters by level and time window."""

import json

import pytest

from runspec_logops_core import summarize_log
from runspec_logops_core.errors import SourceNotFoundError


def _write(tmp_path, lines):
    p = tmp_path / "app.log"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(SourceNotFoundError):
        summarize_log(str(tmp_path / "nope.log"))


def test_clusters_repeated_errors(tmp_path) -> None:
    lines = []
    for i in range(800):
        lines.append(f"2026-06-18T08:{i % 60:02d}:11Z ERROR NullPointerException: id={i}")
        lines.append("\tat com.acme.OrderSvc.price(OrderSvc.java:42)")
    lines.append("2026-06-18T09:00:00Z INFO started ok")
    path = _write(tmp_path, lines)

    d = summarize_log(path, top=10)
    assert d["distinct_signatures"] == 2
    top = d["top"][0]
    assert top["count"] == 800
    assert top["signature"].startswith("NullPointerException@")
    assert top["level"] == "error"
    assert top["first_ts"] is not None and top["last_ts"] is not None


def test_digest_size_is_bounded_regardless_of_input(tmp_path) -> None:
    # 50k distinct noisy lines, but top=5 and small sample caps keep the digest tiny.
    lines = [f"2026-06-18T08:00:00Z ERROR Boom{i % 7}Exception: detail {i}" for i in range(50_000)]
    path = _write(tmp_path, lines)

    d = summarize_log(path, top=5, max_sample_lines=3, max_bytes=80)
    assert len(d["top"]) <= 5
    assert d["truncated"] is True
    assert d["window"]["lines_scanned"] == 50_000
    # The whole serialised digest stays small no matter the 50k-line input.
    assert len(json.dumps(d)) < 4000
    for grp in d["top"]:
        assert len(grp["sample"]) <= 3
        assert all(len(s) <= 80 for s in grp["sample"])


def test_level_filter_drops_below_floor(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            "2026-06-18T08:00:00Z INFO served ok",
            "2026-06-18T08:00:01Z WARN slow query",
            "2026-06-18T08:00:02Z ERROR boom ValueError",
        ],
    )
    d = summarize_log(path, level="error")
    assert d["total_events"] == 1
    assert d["top"][0]["level"] == "error"


def test_time_window_filters(tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            "2026-06-18T08:00:00Z ERROR early FooException",
            "2026-06-18T12:00:00Z ERROR mid BarException",
            "2026-06-18T20:00:00Z ERROR late BazException",
        ],
    )
    d = summarize_log(path, since="2026-06-18T10:00:00", until="2026-06-18T18:00:00")
    sigs = {g["signature"] for g in d["top"]}
    assert any("BarException" in s for s in sigs)
    assert not any("FooException" in s or "BazException" in s for s in sigs)
