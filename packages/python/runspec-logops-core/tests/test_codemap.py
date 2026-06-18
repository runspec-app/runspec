"""map_trace_to_sources resolves frames to bounded source snippets."""

import pytest

from runspec_logops_core import map_trace_to_sources
from runspec_logops_core.errors import SourceNotFoundError


def test_missing_repo_raises(tmp_path) -> None:
    with pytest.raises(SourceNotFoundError):
        map_trace_to_sources(str(tmp_path / "nope"), "trace")


def test_python_traceback_to_snippet(tmp_path) -> None:
    src = tmp_path / "app.py"
    src.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
    trace = 'Traceback:\n  File "app.py", line 10, in handler\nValueError: x'

    res = map_trace_to_sources(str(tmp_path), trace, context=2)
    assert res["files_matched"] == 1
    frame = res["frames"][0]
    assert frame["file"] == "app.py"
    assert frame["lineno"] == 10
    assert any("10: line 10" in s for s in frame["snippet"])
    # ±2 context → at most 5 lines.
    assert len(frame["snippet"]) <= 5


def test_basename_search_when_path_not_exact(tmp_path) -> None:
    pkg = tmp_path / "src" / "acme"
    pkg.mkdir(parents=True)
    (pkg / "OrderSvc.java").write_text("\n".join(f"row {i}" for i in range(1, 100)) + "\n")
    trace = "Exception\n\tat com.acme.OrderSvc.price(OrderSvc.java:42)"

    res = map_trace_to_sources(str(tmp_path), trace, context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"].endswith("OrderSvc.java")
    assert res["frames"][0]["lineno"] == 42


def test_signature_resolves_by_class_and_symbol(tmp_path) -> None:
    (tmp_path / "OrderSvc.java").write_text("class OrderSvc {\n  int price() {\n    return 0;\n  }\n}\n")
    res = map_trace_to_sources(str(tmp_path), "NullPointerException@com.acme.OrderSvc.price", context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["symbol"] == "price"


def test_total_lines_capped(tmp_path) -> None:
    for n in range(4):
        (tmp_path / f"f{n}.py").write_text("\n".join(str(i) for i in range(1, 200)) + "\n")
    trace = "\n".join(f'  File "f{n}.py", line 100, in g' for n in range(4))
    res = map_trace_to_sources(str(tmp_path), trace, context=10, max_total_lines=15)
    total = sum(len(f["snippet"]) for f in res["frames"])
    assert total <= 15
