"""normalize_line and error_signature are deterministic and cluster correctly."""

from runspec_logops_core import detect_level, error_signature, normalize_line
from runspec_logops_core.signatures import strip_timestamp


def test_normalize_masks_volatile_parts() -> None:
    a = normalize_line("2026-06-18T08:01:11Z worker-12 handled request id=7f3a in 412ms")
    b = normalize_line("2026-06-18T09:44:02Z worker-99 handled request id=0a1b in 7ms")
    # Different timestamps / numbers / ids collapse to the same shape.
    assert a == b
    assert "<n>" in a


def test_normalize_masks_uuid_and_hex() -> None:
    s = normalize_line("error at 0xdeadbeef for 550e8400-e29b-41d4-a716-446655440000")
    assert "<hex>" in s
    assert "<uuid>" in s


def test_strip_timestamp_variants() -> None:
    assert strip_timestamp("[2026-06-18 08:01:11] hello") == "hello"
    assert strip_timestamp("Jun 18 08:01:11 hello") == "hello"
    assert strip_timestamp("no timestamp here") == "no timestamp here"


def test_detect_level() -> None:
    assert detect_level("2026-06-18 ERROR boom") == "error"
    assert detect_level("WARN something") == "warning"
    assert detect_level("plain line") is None


def test_error_signature_java_stack_clusters() -> None:
    block1 = [
        "2026-06-18T08:01:11Z ERROR NullPointerException: discount was null",
        "\tat com.acme.OrderSvc.price(OrderSvc.java:42)",
        "\tat com.acme.OrderSvc.checkout(OrderSvc.java:88)",
    ]
    block2 = [
        "2026-06-18T09:00:00Z ERROR NullPointerException: discount was null",
        "\tat com.acme.OrderSvc.price(OrderSvc.java:42)",
        "\tat com.acme.OrderSvc.checkout(OrderSvc.java:88)",
    ]
    sig = error_signature(block1)
    assert sig == error_signature(block2)
    assert sig.startswith("NullPointerException@")
    assert "OrderSvc.price" in sig


def test_error_signature_python_stack() -> None:
    block = [
        "Traceback (most recent call last):",
        '  File "app.py", line 10, in handler',
        "    do_thing()",
        "ValueError: bad input",
    ]
    sig = error_signature(block)
    assert sig.startswith("ValueError@")
    assert "handler" in sig


def test_error_signature_plain_line_falls_back_to_shape() -> None:
    sig = error_signature(["2026-06-18 INFO served 200 in 5ms"])
    assert "@" not in sig
    assert "<n>" in sig
