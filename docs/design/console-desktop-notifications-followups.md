# Console desktop notifications — follow-ups

Deferred work captured while shipping confirm-event desktop notifications
(`runspec-console` 0.39.0, `notify.py`). Neither is required for the current
Tier-1 behaviour (toast + taskbar flash, answer in the in-app Approve/Deny
card); both are quality/packaging improvements.

## 1. Win10 Start Menu shortcut for reliable toast identity

**Why.** On Windows 10, an *unpackaged* desktop app often must own a Start Menu
shortcut whose `System.AppUserModel.ID` matches the AUMID it sends toasts under
(`runspec.console`), or the toast renders generically — or not at all. Setting
the AUMID on the process (`SetCurrentProcessExplicitAppUserModelID`, already done
in `app.py`) is necessary but not always sufficient on Win10. Windows 11 is
generally lenient and shows the toast with just the explicit AUMID. Until this
lands, the taskbar flash (`FlashWindowEx`, needs no permission) is the guaranteed
attention surface.

**Sketch.** On first run, write a `.lnk` to the user's Start Menu
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\runspec console.lnk`) — a HKCU
/ user-profile write, **no admin**:

- Create the shortcut with `win32com.shell` (`IShellLink` → target the launcher,
  set icon to the runspec icon already built in `app.py:_build_icon`).
- Stamp the AUMID via `IPropertyStore` using the `System.AppUserModel.ID`
  property key (`PKEY_AppUserModel_ID`); commit the store.
- Idempotent: skip if the shortcut already carries the right AUMID.

**If/when Tier-2 in-toast buttons arrive** (Approve/Deny directly on the toast),
the same shortcut must also carry `System.AppUserModel.ToastActivatorCLSID`
(`PKEY_AppUserModel_ToastActivatorCLSID`) pointing at a registered COM activator
CLSID — that's the unpackaged-app requirement for button callbacks. Out of scope
for Tier-1 (focus-on-click works in-process with no activator).

## 2. Run runspec-console without a terminal window

**Why.** The `runspec-console` entry point is a `console_scripts` entry, so on
Windows a console window opens alongside the pywebview window. For a desktop app
that's noise — and with desktop notifications in place, the toast + taskbar flash
become the primary attention surface, so a windowless launch pairs naturally with
this work.

**Options.**

- **`gui_scripts` entry point** (smallest change): move `runspec-console` from
  `[project.scripts]` to `[project.gui-scripts]` in `pyproject.toml` so pip
  generates a `pythonw.exe`-backed launcher with no console. Trade-off:
  `stdout`/`stderr` are discarded under `pythonw`. That's acceptable — the JSON
  **audit log** (the runspec file handler) already captures logging, and the Dev
  tab forwards SSH/event logs in-app. Verify nothing relies on a real stdout at
  startup (e.g. the `print(..., file=sys.stderr)` dist-not-found path in
  `app.py:main` becomes invisible — consider surfacing fatal startup errors via a
  `webview` dialog or a message box instead).
- **`--windowed` PyInstaller build**: if/when we ship a packaged `.exe`, build
  with `--windowed` (a.k.a. `--noconsole`); this also gives a natural home for
  the Start Menu shortcut + AUMID from §1.

Recommendation: do the `gui_scripts` switch first (one-line, reversible) and
fold the shortcut creation in alongside it, since a packaged/windowless app is
exactly the context where the Start Menu shortcut matters most.
