"""Installed-software runnable: read the registry uninstall keys."""

from __future__ import annotations

import json

import runspec as rs

from runspec_windows import _platform

# (hive, subkey) pairs covering 64-bit, 32-bit (WOW6432Node), and per-user installs.
_UNINSTALL_PATHS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def _collect() -> list[dict]:
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    seen: set[tuple] = set()
    rows: list[dict] = []
    for hive_name, subkey in _UNINSTALL_PATHS:
        try:
            root = winreg.OpenKey(hives[hive_name], subkey)
        except OSError:
            continue
        with root:
            count = winreg.QueryInfoKey(root)[0]
            for i in range(count):
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as entry:
                        display = _read(entry, "DisplayName")
                        if not display:
                            continue
                        row = {
                            "name": display,
                            "version": _read(entry, "DisplayVersion"),
                            "publisher": _read(entry, "Publisher"),
                            "install_date": _read(entry, "InstallDate"),
                        }
                        key = (row["name"], row["version"])
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(row)
                except OSError:
                    continue
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def _read(entry: object, value: str) -> str | None:
    import winreg

    try:
        data, _ = winreg.QueryValueEx(entry, value)  # type: ignore[arg-type]
        return str(data) if data else None
    except OSError:
        return None


def main_installed_software() -> None:
    spec = rs.parse("installed-software")
    _platform.ensure_windows()
    name_filter = str(spec.filter).lower() if spec.filter is not None else None
    try:
        rows = _collect()
        if name_filter:
            rows = [r for r in rows if name_filter in (r["name"] or "").lower()]
        print(json.dumps(rows))
    except Exception as e:
        _platform.fail(str(e))
