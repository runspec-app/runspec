"""Send to terminals: keystroke building, the window registry and the bar's
JS API (bridge_termsend). The Windows calls are faked, so this runs anywhere."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from runspec_console import termsend, wintile
from runspec_console.bridge import Bridge


class TestKeystrokes(unittest.TestCase):
    def test_text_then_enter(self):
        ev = termsend.keystrokes("ls -l")
        self.assertEqual([w for _, w, _ in ev], [ord(c) for c in "ls -l"] + [13])
        self.assertTrue(all(m == termsend.WM_CHAR for m, _, _ in ev))

    def test_no_enter(self):
        ev = termsend.keystrokes("pwd", enter=False)
        self.assertEqual([w for _, w, _ in ev], [ord(c) for c in "pwd"])

    def test_line_breaks_become_enter(self):
        ev = termsend.keystrokes("a\r\nb\nc\rd", enter=False)
        self.assertEqual(
            [w for _, w, _ in ev], [ord("a"), 13, ord("b"), 13, ord("c"), 13, ord("d")]
        )

    def test_astral_char_is_surrogate_pair(self):
        ev = termsend.keystrokes("😀", enter=False)
        self.assertEqual([w for _, w, _ in ev], [0xD83D, 0xDE00])

    def test_too_long(self):
        with self.assertRaises(ValueError):
            termsend.keystrokes("x" * (termsend.MAX_TEXT + 1))

    def test_named_keys(self):
        self.assertEqual(termsend.key_events("ctrl-c"), [(termsend.WM_CHAR, 3, 1)])
        self.assertEqual(termsend.key_events("Tab"), [(termsend.WM_CHAR, 9, 1)])
        down, up = termsend.key_events("up")
        self.assertEqual(down[:2], (termsend.WM_KEYDOWN, 0x26))
        self.assertEqual(up[:2], (termsend.WM_KEYUP, 0x26))
        self.assertTrue(down[2] & (1 << 24))  # extended key, not the keypad
        self.assertEqual((down[2] >> 16) & 0xFF, 0x48)
        with self.assertRaises(ValueError):
            termsend.key_events("f13")

    def test_not_windows(self):
        with patch.object(termsend, "supported", return_value=False):
            self.assertFalse(termsend.post(5, termsend.keystrokes("x")))
            self.assertFalse(termsend.is_window(5))
            self.assertFalse(termsend.focus(5))


class TestSplitBottom(unittest.TestCase):
    def test_strip(self):
        strip, rest = wintile.split_bottom((0, 0, 1920, 1040), 230)
        self.assertEqual(strip, (0, 810, 1920, 230))
        self.assertEqual(rest, (0, 0, 1920, 810))

    def test_no_strip(self):
        self.assertEqual(
            wintile.split_bottom((0, 0, 800, 600), 0), (None, (0, 0, 800, 600))
        )
        self.assertEqual(
            wintile.split_bottom((0, 0, 800, 400), 230), (None, (0, 0, 800, 400))
        )


def _bridge() -> Bridge:
    b = Bridge.__new__(Bridge)
    return b


class _Win:
    """Fake Windows: pid -> hwnd, a set of open hwnds, and a post log."""

    def __init__(self, pids: dict[int, int]):
        self.pids = dict(pids)
        self.open = set(pids.values())
        self.posts: list[tuple[int, list]] = []

    def patches(self):
        return (
            patch.object(termsend, "supported", return_value=True),
            patch.object(
                wintile,
                "_find_windows",
                side_effect=lambda pids, cls: {
                    p: self.pids[p] for p in pids if p in self.pids
                },
            ),
            patch.object(termsend, "is_window", side_effect=lambda h: h in self.open),
            patch.object(termsend, "post", side_effect=self._post),
            patch("runspec_console.bridge_termsend.TermSendMixin._audit_terminal_send"),
        )

    def _post(self, hwnd, events):
        if hwnd not in self.open:
            return False
        self.posts.append((hwnd, list(events)))
        return True


class TestRegistry(unittest.TestCase):
    def test_lists_sends_and_drops_closed(self):
        b = _bridge()
        b._register_terminals([("web-01", 10), ("web-02", 11), ("db-01", 12)])
        win = _Win({10: 1000, 11: 1001})  # 12 hasn't shown its window yet
        p = win.patches()
        with p[0], p[1], p[2], p[3], p[4] as audit:
            listing = b.terminal_windows()
            self.assertTrue(listing["supported"])
            self.assertEqual(
                [(w["host"], w["ready"]) for w in listing["windows"]],
                [("web-01", True), ("web-02", True), ("db-01", False)],
            )
            self.assertIn("ctrl-c", listing["keys"])

            res = b.send_to_terminals(["10", "11", "12"], "hostname")
            self.assertEqual(res, {"sent": ["web-01", "web-02"], "missing": ["db-01"]})
            self.assertEqual([h for h, _ in win.posts], [1000, 1001])
            audit.assert_called_once()
            self.assertEqual(audit.call_args.args[0], ["web-01", "web-02"])
            self.assertEqual(audit.call_args.kwargs["text"], "hostname")

            win.open.discard(1001)  # web-02 closed
            hosts = [w["host"] for w in b.terminal_windows()["windows"]]
            self.assertEqual(hosts, ["web-01", "db-01"])

    def test_hidden_text_never_audited(self):
        b = _bridge()
        b._register_terminals([("web-01", 10)])
        win = _Win({10: 1000})
        p = win.patches()
        with p[0], p[1], p[2], p[3], p[4] as audit:
            b.send_to_terminals(["10"], "s3cret", hidden=True)
        self.assertEqual(audit.call_args.kwargs["text"], "")
        self.assertTrue(audit.call_args.kwargs["hidden"])

    def test_key_send(self):
        b = _bridge()
        b._register_terminals([("web-01", 10)])
        win = _Win({10: 1000})
        p = win.patches()
        with p[0], p[1], p[2], p[3], p[4] as audit:
            b.send_to_terminals(["10"], key="ctrl-c")
        self.assertEqual(win.posts, [(1000, [(termsend.WM_CHAR, 3, 1)])])
        self.assertEqual(audit.call_args.kwargs["key"], "ctrl-c")

    def test_refusals(self):
        b = _bridge()
        with patch.object(termsend, "supported", return_value=False):
            with self.assertRaises(ValueError):
                b.send_to_terminals(["1"], "x")
            with self.assertRaises(ValueError):
                b.open_send_bar()
        with patch.object(termsend, "supported", return_value=True):
            with self.assertRaises(ValueError):
                b.send_to_terminals([], "x")
            with self.assertRaises(ValueError):
                b.send_to_terminals(["1"], "", enter=False)

    def test_forget(self):
        b = _bridge()
        b._register_terminals([("a", 1), ("b", 2)])
        b.forget_terminals(["1"])
        with patch.object(termsend, "supported", return_value=False):
            self.assertEqual(
                [w["host"] for w in b.terminal_windows()["windows"]], ["b"]
            )
            b.forget_terminals([])
            self.assertEqual(b.terminal_windows()["windows"], [])

    def test_not_agent_tools(self):
        """Human-only: none of these is an agent or MCP server tool name."""
        from runspec_console import bridge_const

        names = set()
        for attr in dir(bridge_const):
            val = getattr(bridge_const, attr)
            if isinstance(val, (set, frozenset, tuple, list)):
                names |= {v for v in val if isinstance(v, str)}
        for name in (
            "terminal_windows",
            "send_to_terminals",
            "focus_terminal",
            "forget_terminals",
            "open_send_bar",
        ):
            self.assertNotIn(name, names)


class TestOpenSendBar(unittest.TestCase):
    def test_opens_once_then_restores(self):
        b = _bridge()
        b._frontend_url = "http://127.0.0.1:5000"
        b._window = MagicMock()
        fake_webview = MagicMock()
        bar = MagicMock()
        fake_webview.create_window.return_value = bar
        with (
            patch.object(termsend, "supported", return_value=True),
            patch.dict("sys.modules", {"webview": fake_webview}),
        ):
            self.assertEqual(b.open_send_bar(), {"opened": True, "existing": False})
            args, kwargs = fake_webview.create_window.call_args
            self.assertEqual(args[1], "http://127.0.0.1:5000#send-bar")
            self.assertTrue(kwargs["on_top"])
            self.assertIs(kwargs["js_api"], b)
            self.assertEqual(b.open_send_bar(), {"opened": True, "existing": True})
            bar.show.assert_called_once()
            fake_webview.create_window.assert_called_once()


class TestLaunchRegisters(unittest.TestCase):
    def test_launch_terminals_registers_and_reserves_strip(self):
        b = _bridge()
        b._register_terminals  # noqa: B018 — exists
        pids = iter(range(50, 60))

        def _popen(cmd):
            proc = MagicMock()
            proc.pid = next(pids)
            return proc

        with (
            patch.object(b, "_putty_command", side_effect=lambda h: ["putty", h]),
            patch("subprocess.Popen", side_effect=_popen),
            patch("runspec_console.bridge_internals.sys.platform", "win32"),
            patch.object(b, "open_send_bar", return_value={"opened": True}) as bar,
            patch("threading.Thread") as thread,
        ):
            res = b.launch_terminals(["a", "b"], send_bar=True)
        bar.assert_called_once()
        self.assertTrue(res["send_bar"])
        kw = thread.call_args.kwargs["kwargs"]
        self.assertEqual(kw["place_title"], "Send to terminals")
        self.assertGreater(kw["reserve_bottom"], 0)
        with patch.object(termsend, "supported", return_value=False):
            hosts = [w["host"] for w in b.terminal_windows()["windows"]]
        self.assertEqual(hosts, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
