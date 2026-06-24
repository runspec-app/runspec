# In-app voice input (dictation into the prompt box)

## Status: SHELVED (button removed in 0.122.0) — plumbing kept dormant

The Voice-access approach was tried end-to-end and **abandoned**. The composer
**Speak/Listening… button was removed** because driving Windows Voice access from
the app proved unreliable: the documented wake/sleep chord (Alt+Shift+B) works
from a physical keyboard but **the injected keystroke does not wake Voice access**
— even after making the injection mimic real hardware (per-key events, holds,
`MapVirtualKey` scan codes). The most likely cause is that Voice access, as an
accessibility app, **ignores synthetic/injected input** (the injected-input flag /
UIPI integrity barrier), which `SendInput` fundamentally cannot defeat. Chasing it
further was diminishing returns.

**What was removed:** only the **UI** — the button + its state/handlers in
`CommandInput.tsx`.

**What was deliberately kept (dormant, no UI caller):**
- `runspec_console/winkeys.py` — the (now scan-code-correct) chord injector.
- `bridge_triggers.py` `start_voice_dictation()` / `stop_voice_dictation()` bridge
  methods (still in the frozen JS API surface) + their `index.ts`/`mock.ts` types.

So re-enabling Voice access (a new button calling the existing bridge methods) is a
small change if the injection barrier is ever solved — but the **intended next step
is a real local STT engine**, not Voice access.

### Future engine: hard constraint — no runtime model download

When picking this back up with **faster-whisper / Vosk / sherpa-onnx**, note that on
the target fleet **outbound HuggingFace access is blocked**, so the model **cannot
be downloaded at first run**. The model file must be **vendored** — bundled into the
wheel or served from the internal index/mirror — and the engine pointed at that
local path (e.g. faster-whisper `model_size_or_path=<local dir>`, download disabled).
Treat "ship the model artifact through the internal channel" as part of the work,
not an afterthought. See "The realistic menu" / engine notes below.

## Why

Operators run the console hands-on all day; some of what they send the agent is
prose ("check why the overnight batch on host db-3 failed and summarise"). Typing
that is the slow part. The ask: a **microphone button in the composer** that turns
speech into text in the prompt box, so the operator can dictate a request and then
edit/send it like any typed message.

Scope is deliberately narrow: **dictation into the existing `<textarea>`**, not a
voice assistant. No wake word, no spoken commands, no TTS reply, no
voice-drives-runnables. The transcript lands in the composer; the operator still
reads it and presses Enter. Voice is an *input method*, not a new control surface —
so it sits entirely below the autonomy/confirm gates that already govern what the
agent does.

## ⚠️ Real-world finding: Voice access already covers this (maybe zero code)

Test on a target workstation (2026-06-23): **Win+H is disabled by org policy**
("managed by your organisation"), but **Windows 11 Voice access works nicely and
accurately**, with an on-device English pack already installed and the Voice
access bar auto-starting at login. The org permits *on-device* speech and blocks
the *online / Win+H* path.

**Voice access is not an API we call — it's a system dictation app that types into
whatever editable control has focus** (UI Automation text insertion), exactly like
Win+H but policy-permitted. The console renders in **WebView2 (Edge/Chromium)** and
the composer is a standard `<textarea>`, which accepts UIA text insertion. So
**dictation into the prompt box very likely already works today with no console
code at all.**

Two consequences that gut most of the build below:
1. **Confirm with no release:** dictate into a text field in the **Edge browser**
   (same engine as WebView2). Works there ⇒ works in the console prompt box.
2. **An in-app mic button can't *transcribe* via Voice access** — it exposes no
   programmatic recognition API. But it *can* be **toggled by its hotkey**: Windows
   documents **Alt+Shift+B** to wake/sleep Voice access listening (and Alt+Shift+C
   for mic on/off). JS in the WebView2 sandbox can't synthesize a hotkey, but the
   **Python host can** (`SendInput`). So the shipped button wakes Voice access and
   focuses the prompt box — dictation lands in the box — and sleeps it again on
   send. A *true* in-app transcription engine (WinRT/SAPI/Whisper) remains a
   separate, heavier path below.

### Shipped: Speak button (0.116.0 affordance → 0.118.0 wake/sleep toggle)
- `console-ui/.../CommandInput.tsx` — a **Speak** mic button in the strip above the
  `❯` row. `toggleVoice()` focuses the `<textarea>` and wakes Voice access (button
  shows *Listening…* in red); a second click or **sending** sleeps it
  (`sleepVoiceIfAwake()` in `submit()`). The same chord toggles both ways, so the
  UI tracks `voiceAwake` and presses it to flip. One-time discoverability hint.
- `runspec_console/winkeys.py` — `send_voice_access_toggle()` injects
  `VOICE_ACCESS_TOGGLE = Alt+Shift+B` via ctypes `SendInput` (modifiers pressed
  first, released in reverse). Windows-only, fail-soft no-op elsewhere. **No new
  dependency.** A one-line constant change if a build documents a different combo.
- `runspec_console/bridge.py` — `start_voice_dictation()` (wake) /
  `stop_voice_dictation()` (sleep), both `-> {"ok": bool}`, same toggle chord.
- History: 0.117.0 first used **Ctrl+Win+S**, but that opens *legacy WSR* (with a
  deprecation note), not Voice access — corrected to **Alt+Shift+B** in 0.118.0.
- Open verification (operator, on the box): confirm Alt+Shift+B wakes Voice access
  on the target build, and that the wake/sleep state stays in sync with the button
  in normal use (manual Alt+Shift+B outside the UI would desync our `voiceAwake`).

The SAPI/WinRT engine design below is retained as the fallback **only if** Voice
access proves insufficient (e.g. operators want one-click in-composer capture
without the floating Voice access bar).

## ⚠️ (Superseded) Engine choice as an empirical question

This directly threatens the original WinRT pick below: `Windows.Media.
SpeechRecognition`'s **free-dictation** mode can ride the **same online speech
path that Win+H uses** — so it may be blocked by the exact policy that killed
Win+H, while **SAPI (proven to work here) is the engine actually permitted.**

Two consequences:
1. **Run `scripts/speech_probe.py` on the target box first.** It reports (read-only,
   no mic capture) the policy keys, whether SAPI constructs, and whether WinRT's
   online/offline language lists are populated. Empty WinRT *online* list ⇒ WinRT
   free-dictation is blocked ⇒ use SAPI.
2. **Leading candidate is now SAPI**, not WinRT — it's **fully on-device** (a
   *stronger* no-cloud guarantee), and Python drives it through **`pywin32`, already
   a console dependency**, so **no new `[voice]` extra is even required.** The
   sections below still describe the WinRT shape; treat SAPI as the front-runner
   pending the probe, and swap `dictation.py`'s backend accordingly (the bridge/UI
   contract is identical either way).

## The platform bet that makes this simple

runspec-console is **Windows-only and staying that way**, so we transcribe with an
**on-device Windows engine** — originally **`Windows.Media.SpeechRecognition`**
(projected via PyWinRT, the pattern the `[notifications]` extra uses), now likely
**SAPI** per the policy finding above. Either way recognition runs **host-side in
Python**, which is the property that makes this simple:

The decisive property: **`SpeechRecognizer` captures from the system default
microphone itself.** Recognition runs in the **Python host process**, not the web
view. That means:

- **No `getUserMedia`, no `MediaRecorder`, no WebView2 microphone permission.** The
  WebView2 permission dance (the fiddliest, most error-prone part of any in-browser
  mic feature) is *entirely sidestepped* — the front end never touches the mic. It
  only renders a button and writes received text into the composer.
- **No audio shuttled over the bridge.** No base64 WAV blobs crossing the JS↔Python
  boundary; only small text events flow back.
- **On-device, no API key, no model download, no cloud egress** — consistent with
  the fleet's privacy/governance posture (denylist, lock-suspend, env-only creds).

So WinRT is both the privacy-friendly pick *and* the structurally simplest one.

## Behaviour

- A **mic button** sits in the composer's status strip **above the `❯` input row**
  (`CommandInput.tsx` — the area currently used only by the `/` and `$` dropdowns;
  the input row itself is at `:330`). Idle = outline mic; recording = filled/red mic
  with a subtle pulse.
- Click (or a hotkey, e.g. `Ctrl+M`) **starts** dictation; click again, `Esc`, or
  the first send **stops** it.
- While recording, a **live partial transcript** renders in that strip (greyed),
  replaced as the recognizer refines it. On each *finalised* phrase the text is
  **appended into the textarea** at the caret via the existing `setValue` path, so
  the operator can keep editing by keyboard mid-dictation.
- Recognition is **continuous** (multiple sentences) until stopped; a short silence
  finalises a phrase but does not end the session.
- **Nothing auto-sends.** Dictation only fills the box. Enter still sends, Stop still
  stops a chat turn — voice does not change the send/gate semantics at all.
- **Graceful degradation:** if the `[voice]` extra isn't installed, the mic privacy
  setting is off, or no recognizer language is available, the button shows a disabled
  state with a tooltip explaining why (mirrors how `credentials`/`git-sync` report
  themselves unavailable). The console is fully usable without it.

## Architecture (one bridge call + two event streams)

Mirrors the established `send_chat` streaming pattern (sync bridge method returns a
session id immediately; a worker thread streams results back via `_dispatch`).

```
CommandInput (mic button)
    │  bridge.start_dictation()  ── sync, returns session_id ──▶
    │                                                           Bridge.start_dictation()
    │                                                             └─ spawn worker thread:
    │                                                                  SpeechRecognizer()
    │                                                                  session.RecognitionQualityDegrading → (log)
    │                                                                  recognizer.HypothesisGenerated  ─┐ partials
    │                                                                  recognizer.ContinuousRecognitionSession
    │                                                                    .ResultGenerated             ─┘ finals
    │  ◀── runspec:dictation_partial {session_id, text} ──────────────────┘  (via _dispatch)
    │  ◀── runspec:dictation_final   {session_id, text} ───────────────────
    │  ◀── runspec:dictation_end     {session_id, reason} ────────────────
    │
    └─ partial → render in strip;  final → setValue(prev + text);  end → reset button
    bridge.stop_dictation(session_id)  ── sync ──▶ cancels the session
```

- `start_dictation()` / `stop_dictation(session_id)` are **synchronous** bridge
  methods (pywebview requirement); the recognizer lifecycle lives on a worker
  thread, exactly like `_spawn_chat_turn`.
- Events ride the existing single-threaded `_dispatch` queue → `window.evaluate_js`
  path (`bridge.py:10568`), so they serialise safely with token/run events.
- Only **one** dictation session at a time (guarded by a session id); starting a new
  one cancels the prior.

## Files (planned)

| File | Change |
|---|---|
| `runspec_console/dictation.py` | **new** — WinRT `SpeechRecognizer` lifecycle: start/stop, language constraint, partial/final callbacks. winrt imports *inside* functions (import-clean anywhere); pure availability/format helpers unit-tested with no winrt, mirroring `notify.py`. Safe no-op off-Windows / extra-absent. |
| `runspec_console/bridge.py` | `start_dictation()` / `stop_dictation()` sync methods; `dictation_available()` for the disabled-state probe; a `_console_context` note that a mic is wired. |
| `console-ui/src/components/CommandInput.tsx` | Mic button in the status strip above `:335`; recording state; live-partial render; append-final into `value`; `Ctrl+M` toggle. |
| `console-ui/src/bridge/index.ts` + `mock.ts` | `start_dictation`/`stop_dictation`/`dictation_available` signatures; `DictationState` type; mock for browser-dev. |
| `runspec-console/pyproject.toml` | new `[voice]` optional-dependency extra (below) + add to the `[all]` aggregate. |
| `runspec_console/tests/test_dictation.py` | unit tests for the pure helpers + availability probe (no winrt needed). |
| `docs/` + `CHANGELOG.md` | feature note + version bump entry. |

## Packaging — the `[voice]` extra

PyWinRT projection for `Windows.Media.SpeechRecognition`, marker-gated to win32,
following the `[notifications]` precedent (winrt-* ship wheels through current
Python, no C toolchain):

```toml
voice = [
    "winrt-runtime>=2.0 ; sys_platform == 'win32'",
    "winrt-Windows.Foundation>=2.0 ; sys_platform == 'win32'",
    "winrt-Windows.Foundation.Collections>=2.0 ; sys_platform == 'win32'",
    "winrt-Windows.Media.SpeechRecognition>=2.0 ; sys_platform == 'win32'",
    "winrt-Windows.Globalization>=2.0 ; sys_platform == 'win32'",
]
```

The feature **reports itself unavailable** (disabled button + tooltip) until the
extra is installed — same UX contract as `credentials`/`git-sync`/`schedule`.

## Risks / things to validate before/while building

1. **On-device vs the "Online speech recognition" path (PARTIALLY ANSWERED).** See
   the policy finding at the top: on the target box Win+H/online is **policy-blocked**
   but SAPI/Voice access (on-device) work. So the no-cloud-egress requirement is
   satisfiable *only* by an on-device engine here. Remaining step: run
   `scripts/speech_probe.py` to confirm whether WinRT's offline grammar path is also
   usable or whether we commit to SAPI. We **never** silently fall back to cloud — a
   blocked-online machine degrades the button to disabled (with tooltip) unless an
   on-device engine answers.
2. **Microphone privacy permission.** Windows gates mic access per app
   (Settings → Privacy → Microphone, incl. the global "desktop apps" switch). An
   unpackaged app inherits the desktop-apps allowance; if denied, `SpeechRecognizer`
   raises on start — caught and surfaced as the disabled tooltip, never a crash.
3. **AppUserModelID / identity.** `notify.py` already sets `runspec.console` as the
   AUMID; speech needs no toast identity but we reuse the same process identity for
   any consent prompt.
4. **Thread/COM affinity.** WinRT async + event callbacks must not block the pywebview
   UI thread; the worker-thread + `_dispatch`-queue design keeps recognition off it.
   Validate that recognizer events marshal cleanly from the worker (PyWinRT delivers
   completions on a threadpool; we only enqueue text, never touch the webview from
   the callback).
5. **Recognizer cold-start latency.** First `SpeechRecognizer` construction can take
   a beat; construct lazily on first mic-button press and show a "starting…" state.

## Not built (deliberately)

- **No spoken commands / agent-by-voice.** Transcript fills the box; the human sends
  it. Keeps voice strictly below the existing autonomy/confirm gates.
- **No TTS / spoken replies.** Input only.
- **No cloud STT and no silent cloud fallback.** On-device or disabled — never
  surprise egress. (Cloud was considered and rejected for the governance posture.)
- **No `getUserMedia`/MediaRecorder path.** WinRT host-side capture makes browser mic
  access — and WebView2 permission handling — unnecessary.
- **No cross-platform abstraction.** Windows-only by project decision; the module is
  a no-op elsewhere rather than carrying a second backend.

## Distribution / governance

Voice is a pure input convenience with **no new autonomy surface**, so it needs no
Config-Sync-enforced policy of its own — what the agent *does* with the dictated text
is still governed by denylist / rota / lock-suspend / confirm-gates unchanged. Orgs
that want to withhold it simply don't ship the `[voice]` extra.
