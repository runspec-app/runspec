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
    # lands on the definition line, not the class header
    assert any("int price()" in s for s in res["frames"][0]["snippet"])


def test_signature_inner_class_resolves_to_outer_file(tmp_path) -> None:
    # Java inner class `Outer$Inner` lives in Outer.java — the $-suffix is dropped.
    (tmp_path / "Outer.java").write_text("class Outer {\n  class Inner {\n    void handle() {\n      x();\n    }\n  }\n}\n")
    res = map_trace_to_sources(str(tmp_path), "NPE@com.acme.Outer$Inner.handle", context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"] == "Outer.java"
    assert any("void handle()" in s for s in res["frames"][0]["snippet"])


def test_signature_method_in_differently_named_file(tmp_path) -> None:
    # No OrderSvc.java; the method is defined in Pricing.java → content-search fallback.
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "Pricing.java").write_text("\n".join(["package acme;", "class Pricing {", "  public int computePrice() {", "    return 1;", "  }", "}"]) + "\n")
    res = map_trace_to_sources(str(tmp_path), "Exc@com.acme.OrderSvc.computePrice", context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"].endswith("Pricing.java")
    assert any("computePrice" in s for s in res["frames"][0]["snippet"])


def test_signature_python_function_only(tmp_path) -> None:
    # A Python signature has no class — resolve a bare function by its `def`.
    (tmp_path / "orders.py").write_text("import os\n\n\ndef process_order(o):\n    return o\n")
    res = map_trace_to_sources(str(tmp_path), "ValueError@process_order", context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"] == "orders.py"
    assert any("def process_order" in s for s in res["frames"][0]["snippet"])


def test_unresolved_signature_returns_note(tmp_path) -> None:
    (tmp_path / "unrelated.py").write_text("x = 1\n")
    res = map_trace_to_sources(str(tmp_path), "Boom@com.acme.Nope.missing")
    assert res["files_matched"] == 0
    assert "note" in res


def test_definition_fallback_ignores_imports_and_calls(tmp_path) -> None:
    # An import of the symbol and a call site must not win over the real def.
    (tmp_path / "caller.py").write_text("from svc import process_order\n\nprocess_order(1)\n")
    (tmp_path / "svc.py").write_text("def process_order(o):\n    return o\n")
    res = map_trace_to_sources(str(tmp_path), "ValueError@process_order", context=0)
    assert res["frames"][0]["file"] == "svc.py"


def test_bare_exception_class_resolves(tmp_path) -> None:
    # A bare exception class from a digest signature (no @, no file:line).
    (tmp_path / "errors.py").write_text("class BackoffError(Exception):\n    pass\n")
    res = map_trace_to_sources(str(tmp_path), "BackoffError", context=1)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"] == "errors.py"
    assert any("class BackoffError" in s for s in res["frames"][0]["snippet"])


def test_bare_function_resolves(tmp_path) -> None:
    (tmp_path / "svc.py").write_text("import os\n\n\ndef do_work():\n    return 1\n")
    res = map_trace_to_sources(str(tmp_path), "do_work", context=0)
    assert res["files_matched"] == 1
    assert res["frames"][0]["file"] == "svc.py"


def test_bare_unknown_returns_note(tmp_path) -> None:
    (tmp_path / "x.py").write_text("y = 1\n")
    res = map_trace_to_sources(str(tmp_path), "NoSuchThing")
    assert res["files_matched"] == 0
    assert "note" in res


def test_total_lines_capped(tmp_path) -> None:
    for n in range(4):
        (tmp_path / f"f{n}.py").write_text("\n".join(str(i) for i in range(1, 200)) + "\n")
    trace = "\n".join(f'  File "f{n}.py", line 100, in g' for n in range(4))
    res = map_trace_to_sources(str(tmp_path), trace, context=10, max_total_lines=15)
    total = sum(len(f["snippet"]) for f in res["frames"])
    assert total <= 15
