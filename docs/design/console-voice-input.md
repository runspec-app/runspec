# In-app voice input (dictation into the prompt box)

## Status: PROTOTYPE (0.273.0) — Moonshine Voice behind the `[voice]` extra

A **mic button** in the chat composer (and **Ctrl+M**) dictates into the prompt
box using an **on-device streaming engine that runs in the console process**.
The first attempt (0.116.0–0.121.0) drove Windows Voice access by injecting its
hotkey and was **abandoned in 0.122.0** — the engine landscape was re-surveyed in
September 2026 and this doc now records that survey and the engine actually built
on. The Voice-access history is kept at the bottom for context.

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

## Hard constraints (from the target fleet)

1. **On-device only, no cloud STT and no silent cloud fallback.** Win+H (online
   speech) is policy-blocked on the target workstations; the org permits only
   on-device speech.
2. **No run-time model download from HuggingFace** — outbound HF access is
   blocked. The model must be **vendorable**: shipped through the internal index /
   a package and loaded from a local path.
3. **Windows-only, WebView2 (Edge/Chromium) front end, Python (pywebview) host.**
   No C toolchain on install; wheels only.
4. **Streaming with partial results** — dictation needs the in-progress line, not
   batch file transcription.

## The September 2026 engine survey

What changed since the Voice-access attempt, and why each option was or wasn't
picked. (Sources: Microsoft Learn / Windows App SDK release notes, Edge developer
docs, Moonshine + sherpa-onnx + Desert Ant Labs repos and PyPI metadata.)

| Option | Streaming | On-device | Model source | Python-reachable | Verdict |
|---|---|---|---|---|---|
| **Moonshine Voice** (`moonshine-voice` 0.1.5, Aug 2026) | ✅ `MicTranscriber` partials + finals, ~100 ms | ✅ CPU | `download.moonshine.ai`, **or a local dir** (`models_from`) | ✅ pip wheel, win_amd64, **Py 3.8–3.12** | **Built (this prototype)** |
| **sherpa-onnx** (1.13.8, Sep 2026) | ✅ streaming Zipformer; also runs Moonshine + Parakeet models offline | ✅ CPU | GitHub releases / local files | ✅ wheels cp37–cp314 | **Fallback** — covers Py 3.13; more assembly (bring your own mic capture) |
| **Windows AI Speech API** (`Microsoft.Windows.AI.Speech`, Windows App SDK **2.2 experimental 9**, Jun 2026) | ✅ `StreamingRecognition` Recognizing/Recognized | ✅ NPU **and CPU** (Build 2026) | **Windows Update "AI Components"** — org-manageable, no HF | ⚠️ needs a PyWinRT `winui3-*` projection (none for `.Speech` yet) **and package identity** (sparse MSIX) | **Watch** — the right long-term answer once stable + identity story solved |
| **Edge on-device Web Speech API** (`processLocally`, Edge 150, Jul 2026; Chrome 139+) | ✅ `interimResults` | ✅ | Microsoft component download via `SpeechRecognition.install()` | n/a (JS in the web view) | **Cheap probe** — still behind an `edge://flags` toggle in Canary/Dev; WebView2 has never exposed Web Speech (#1613); needs a pywebview mic-permission handler |
| **Foundry Local** (Whisper) | JS/C# only; Python path is DIY onnxruntime+transformers | ✅ | Foundry catalog (Azure CDN) | ⚠️ heavy | No |
| **Voz** (Desert Ant Labs) | ❌ batch (file / full buffer) | ✅ Apple Neural Engine | HuggingFace | ❌ Swift only (Apple platforms) | No — wrong platform, batch, HF |
| **Windows Voice access / Win+H / Fluid Dictation** | — | Voice access yes; Win+H online; Fluid Dictation Copilot+ only | — | ❌ no developer API; injected hotkeys ignored | No (see history below) |

**Why Moonshine won:** it is the only option that is *all* of streaming, CPU-only,
vendorable-without-HF, pip-installable with a Windows wheel, and drivable from
the Python host with no new native build — and it ships its own mic capture, so
the WebView2 microphone-permission problem never arises. MIT-licensed for the
English streaming models. Its one gap is **Python 3.13** (no wheel yet), which is
why sherpa-onnx stays the named fallback and the extra carries a version marker.

**Vendoring the model:** `moonshine-voice download --language en` on a connected
machine, copy the model directory into the internal channel, and set
`[voice] model_dir` — the engine then loads from disk and never downloads
(`MicTranscriber.models_from(dir)`; the spelling-model side download is also
skipped on that path). Without `model_dir`, first use fetches from
`download.moonshine.ai` into Moonshine's cache (`MOONSHINE_VOICE_CACHE`).

## Behaviour (as built)

- A **mic button** sits in the composer's button column (next to Auto-switch /
  Send). Hidden entirely when the backend says dictation is unavailable — extra
  not installed, PortAudio missing, `[voice] enabled = false`, unsupported Python
  — rather than a nagging disabled control (`dictation_status().reason` says why).
- Click (or **Ctrl+M**) starts a session; click again, Ctrl+M, or **sending** stops
  it. While starting the icon spins; while listening it is red with a soft pulse.
- A **status strip** above the input row shows the model opening (with download
  progress on first run) and then the **in-progress line**, greyed, replaced as
  the recogniser refines it. Each **finished line is appended into the textarea**
  (space-separated), so the operator can keep editing by keyboard mid-dictation.
- **Nothing auto-sends.** Enter still sends, Stop still stops a chat turn.
- The decoder is **biased toward the fleet's vocabulary**: the discovery cache's
  runnable + host names are handed to Moonshine as keyterms (`set_keyterms`), so
  `cert-check` and `db-3` come out spelled the way the catalogue does.
- Only one session at a time; starting a new one stops the previous. An engine
  error ends the session with a visible reason.

## Architecture (one bridge call + three event streams)

Mirrors the `send_chat` streaming pattern: a sync bridge call returns a session id
immediately; a worker thread streams results back via `_dispatch`.

```
CommandInput (mic button)
    │  bridge.start_dictation()  ── sync, returns {ok, session_id} ──▶
    │                                                     VoiceMixin.start_dictation()
    │                                                       └─ DictationSession.start()
    │                                                            worker thread:
    │                                                              MicTranscriber()
    │                                                                .on_text(partial) ─┐
    │                                                                .on_line(final)   ─┤
    │                                                                .load() → set_keyterms() → .start()
    │  ◀── runspec:dictation_state   {session_id, state: loading|listening|ended, …}
    │  ◀── runspec:dictation_partial {session_id, text} ─────────────────────────────┘
    │  ◀── runspec:dictation_final   {session_id, text}
    │
    └─ partial → status strip;  final → setValue(prev + ' ' + text);  ended → reset button
    bridge.stop_dictation()  ── sync ──▶ session.stop() (mic.stop() flushes queued audio → last finals → ended)
```

- Recognition runs **host-side in Python**: Moonshine's `MicTranscriber` captures
  the system default microphone via `sounddevice` (PortAudio bundled in the
  Windows wheel). **No `getUserMedia`, no MediaRecorder, no WebView2 mic
  permission, no audio over the bridge** — only small text events.
- Events ride the existing single-threaded `_dispatch` queue → `evaluate_js`, so
  they serialise safely with token/run events.
- The engine sits behind an **injectable factory** (`transcriber_factory`), so the
  session's threading, event shaping, stop-during-load and keyterm handling are
  unit-tested with no Moonshine, no mic and no model.

## Files

| File | Role |
|---|---|
| `runspec_console/dictation.py` | `DictationSession` (worker thread, event shaping, stop-during-load, error → ended), `available()` probe, `normalise_model`, `clean_keyterms`/`build_keyterms`, `_build_mic` (the only Moonshine-touching code). |
| `runspec_console/bridge_voice.py` | `VoiceMixin`: `dictation_status()`, `start_dictation()`, `stop_dictation()`; seeds keyterms from `_runnables_cache`. |
| `runspec_console/bridge.py` | `VoiceMixin` in the composition; `self._dictation`. |
| `runspec_console/config.py` | `read_voice()` — the `[voice]` section (`enabled`, `language`, `model`, `model_dir`, `device`, `update_interval`). |
| `runspec-console/pyproject.toml` | `voice = ["moonshine-voice>=0.1.5 ; python_version < '3.13'"]`, in `[all]`. |
| `console-ui/src/components/CommandInput.tsx` | Mic button, status strip, event listeners, Ctrl+M, stop-on-send. |
| `console-ui/src/bridge/index.ts` + `mock.ts` | `DictationStatus` / `DictationStateEvent` / `DictationTextEvent`; the three methods. |
| `tests/test_dictation.py`, `console-ui/src/components/CommandInput.dictation.test.tsx` | Unit + component tests. |
| `docs/console-reference.md` → `[voice]` | Operator-facing config reference. |

## Open items for the prototype → shipped

1. **Validate on a target workstation**: PortAudio device enumeration under the
   org's audio policy, first-run latency of `medium-streaming` on the fleet's CPUs
   (drop to `small-streaming` via `[voice] model` if it lags), and the vendored
   `model_dir` path end-to-end with downloads blocked.
2. **Python 3.13** — either Moonshine publishes a 3.13 wheel or the sherpa-onnx
   fallback backend is added behind the same `DictationSession` factory.
3. **Windows AI Speech API** — re-check when it reaches a stable Windows App SDK:
   if a PyWinRT projection exists and a sparse-package identity is acceptable for
   the pywebview app, its Windows-Update-delivered model would remove the
   vendoring step entirely.
4. **Edge on-device Web Speech in WebView2** — a 15-minute probe from the console
   devtools (`SpeechRecognition.available({processLocally:true, langs:['en-US']})`)
   once Edge 150+ ships it unflagged; a zero-Python path if it works.
5. **Remove the dormant Voice-access plumbing** (`winkeys.py`,
   `start_voice_dictation`/`stop_voice_dictation` in the frozen JS API surface)
   once the Moonshine path is confirmed on the fleet.

## Not built (deliberately)

- **No spoken commands / agent-by-voice.** Transcript fills the box; the human sends.
- **No TTS / spoken replies.** Input only.
- **No cloud STT and no silent cloud fallback.** On-device or hidden — never
  surprise egress.
- **No `getUserMedia`/MediaRecorder path.** Host-side capture makes browser mic
  access — and WebView2 permission handling — unnecessary.
- **No `[voice]` Settings tab yet.** It's a config-file section (rides Config Sync
  / the seed, so a fleet baseline can pin `model_dir`); a Settings surface is a
  follow-up once the engine is confirmed.

## Distribution / governance

Voice is a pure input convenience with **no new autonomy surface**, so it needs no
Config-Sync-enforced policy of its own — what the agent *does* with the dictated
text is still governed by denylist / rota / lock-suspend / confirm-gates
unchanged. Orgs that want to withhold it simply don't ship the `[voice]` extra, or
set `[voice] enabled = false` in the seeded `config.toml`.

---

## History: the Voice-access attempt (0.116.0 → removed in 0.122.0)

Test on a target workstation (2026-06-23): **Win+H is disabled by org policy**
("managed by your organisation"), but **Windows 11 Voice access works nicely and
accurately**, with an on-device English pack already installed. Voice access is a
system dictation app that types into whatever editable control has focus (UI
Automation text insertion) — so dictating into the console's `<textarea>` works
today with **no console code at all**, driven from Voice access's own bar.

The shipped **Speak** button tried to make that one click: the Python host
injected the documented wake/sleep chord (**Alt+Shift+B**; 0.117.0 briefly used
Ctrl+Win+S, which opens legacy WSR) via `SendInput`. It **did not work**: the
chord works from a physical keyboard but the *injected* keystroke does not wake
Voice access — even after making the injection mimic real hardware (per-key
events, holds, `MapVirtualKey` scan codes). The most likely cause is that Voice
access, as an accessibility app, **ignores synthetic input** (the injected-input
flag / UIPI barrier), which `SendInput` cannot defeat. Voice access has no
developer API, and nothing in the 2026 Windows releases changes that (Fluid
Dictation is Copilot+-only cleanup on top of it).

Kept dormant, no UI caller: `runspec_console/winkeys.py` (the scan-code-correct
chord injector) and the `start_voice_dictation()` / `stop_voice_dictation()`
bridge methods (still in the frozen JS API surface). `scripts/speech_probe.py`
(SAPI / WinRT `Windows.Media.SpeechRecognition` policy probe) is likewise
historical — SAPI was the leading on-device candidate before Moonshine.
