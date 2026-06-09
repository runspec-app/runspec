"""check_port treats a refused/timed-out connection as a result, not an error."""

import socket

from runspec_linux_core import check_port


def test_check_port_open() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        result = check_port("127.0.0.1", port, timeout=1.0)
    assert result["open"] is True
    assert result["port"] == port


def test_check_port_closed_does_not_raise() -> None:
    result = check_port("127.0.0.1", 1, timeout=0.2)
    assert result["open"] is False
    assert "response_ms" in result
