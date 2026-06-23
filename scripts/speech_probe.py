"""speech_probe.py — which Windows speech engine is callable on THIS machine?

A read-only diagnostic for the in-app voice-input feature (see
docs/design/console-voice-input.md). It does NOT listen to your microphone or
transcribe anything — it only asks Windows which speech engines/languages are
available and whether the org's policy permits them, so we pick an engine that
actually works on a locked-down workstation instead of guessing.

Run it on the Windows box, ideally inside the console's venv (so pywin32 is
present):

    python scripts/speech_probe.py

Nothing here is destructive; every probe is wrapped so a failure just prints a
line and moves on.
"""

from __future__ import annotations

import platform
import sys


def line(label: str, value: object) -> None:
    print(f"  {label:<34} {value}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def probe_os() -> None:
    section("System")
    line("platform", platform.platform())
    line("python", sys.version.split()[0])
    if sys.platform != "win32":
        print("\n  NOTE: not Windows — the speech probes below are no-ops here.")


def probe_policy() -> None:
    """Read the registry keys that gate online speech / input personalization.

    These are the policies most likely behind a 'managed by your organisation'
    Win+H block. We only READ; nothing is changed.
    """
    section("Policy / privacy settings (read-only)")
    if sys.platform != "win32":
        line("skipped", "not Windows")
        return
    try:
        import winreg
    except Exception as exc:  # pragma: no cover - defensive
        line("winreg import failed", exc)
        return

    keys = [
        # (hive, subkey, value name, meaning)
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Policies\Microsoft\InputPersonalization",
         "AllowInputPersonalization",
         "0 = online speech/dictation blocked by policy"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy",
         "HasAccepted",
         "1 = 'Online speech recognition' turned ON"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
         "DisableVoiceTyping",
         "1 = Win+H voice typing disabled by policy"),
    ]
    for hive, subkey, name, meaning in keys:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                val, _ = winreg.QueryValueEx(k, name)
                line(name, f"{val}   ({meaning})")
        except FileNotFoundError:
            line(name, f"(not set)   ({meaning})")
        except Exception as exc:
            line(name, f"error: {exc}")


def probe_sapi() -> None:
    """Legacy Windows Speech Recognition = SAPI. Fully on-device. Driven via the
    already-bundled pywin32. This is the engine that 'just worked' for WSR."""
    section("SAPI / legacy Windows Speech Recognition (on-device)")
    if sys.platform != "win32":
        line("skipped", "not Windows")
        return
    try:
        import win32com.client  # type: ignore
    except Exception as exc:
        line("pywin32 import failed", exc)
        line("hint", "pip install pywin32  (already a console dependency)")
        return

    # In-process recognizer — the clean embedding path (no shared WSR UI).
    try:
        inproc = win32com.client.Dispatch("SAPI.SpInprocRecognizer")
        line("SpInprocRecognizer", "OK — constructs")
        try:
            recs = inproc.Recognizers  # ISpeechObjectTokens
            names = [recs.Item(i).GetDescription() for i in range(recs.Count)]
            line("installed recognizers", names or "(none)")
        except Exception as exc:
            line("enumerate recognizers", f"error: {exc}")
    except Exception as exc:
        line("SpInprocRecognizer", f"FAILED: {exc}")

    # Shared recognizer — ties into the WSR profile/UI.
    try:
        win32com.client.Dispatch("SAPI.SpSharedRecognizer")
        line("SpSharedRecognizer", "OK — constructs")
    except Exception as exc:
        line("SpSharedRecognizer", f"FAILED: {exc}")


def probe_winrt() -> None:
    """Windows.Media.SpeechRecognition (the originally planned API).

    SupportedTopicLanguages = the ONLINE free-dictation languages (the path Win+H
    uses). Empty ⇒ online dictation is unavailable/blocked here.
    SupportedGrammarLanguages = OFFLINE list/SRGS grammars (on-device).
    """
    section("WinRT Windows.Media.SpeechRecognition")
    if sys.platform != "win32":
        line("skipped", "not Windows")
        return
    try:
        from winrt.windows.media.speechrecognition import SpeechRecognizer  # type: ignore
    except Exception as exc:
        line("winrt import failed", exc)
        line("hint", "pip install winrt-Windows.Media.SpeechRecognition winrt-Windows.Globalization")
        return

    try:
        topic = [l.language_tag for l in SpeechRecognizer.supported_topic_languages]
    except Exception as exc:
        topic = f"error: {exc}"  # type: ignore[assignment]
    try:
        grammar = [l.language_tag for l in SpeechRecognizer.supported_grammar_languages]
    except Exception as exc:
        grammar = f"error: {exc}"  # type: ignore[assignment]

    line("ONLINE topic languages", topic)
    line("OFFLINE grammar languages", grammar)
    try:
        r = SpeechRecognizer()
        line("default current_language", r.current_language.language_tag)
    except Exception as exc:
        line("SpeechRecognizer()", f"FAILED: {exc}")

    print("\n  Read it like this:")
    print("    ONLINE list NON-empty  -> WinRT free-dictation likely WORKS (online path open)")
    print("    ONLINE list EMPTY      -> WinRT free-dictation BLOCKED (same path Win+H needs)")
    print("                              -> use SAPI instead (proven on-device, no winrt extra)")


def main() -> None:
    print("runspec-console voice-input engine probe (read-only; no mic capture)")
    probe_os()
    probe_policy()
    probe_sapi()
    probe_winrt()
    print("\nDone. Paste the output back and we'll lock the engine choice.")


if __name__ == "__main__":
    main()
