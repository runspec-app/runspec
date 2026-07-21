"""
test_workspace_path.py — Bridge.current_workspace_path.

The Transfer dialog defaults a *local* destination to the current chat session's
agent workspace so a download lands where the agent can then act on it. The
method returns the absolute path (creating the directory so it's a valid
destination), or "" when there's no active session / on any error.
"""

from __future__ import annotations

from runspec_console.bridge import Bridge


class _WsBridge:
    current_workspace_path = Bridge.current_workspace_path

    def __init__(self, root):
        self._root = root

    def _current_workspace_root(self):
        return self._root


def test_returns_path_and_creates_dir(tmp_path):
    target = tmp_path / "workspaces" / "chat-abc"
    assert not target.exists()
    b = _WsBridge(target)
    out = b.current_workspace_path()
    assert out == str(target)
    assert target.is_dir()  # created so it's immediately a valid destination


def test_returns_empty_when_no_session():
    assert _WsBridge(None).current_workspace_path() == ""


def test_returns_empty_on_error():
    class _Boom:
        current_workspace_path = Bridge.current_workspace_path

        def _current_workspace_root(self):
            raise RuntimeError("no config")

    assert _Boom().current_workspace_path() == ""
