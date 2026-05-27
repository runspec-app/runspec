"""
env.py — .runspec_env file loading.

Resolution order for the file path:
  1. RUNSPEC_ENV_FILE env var
  2. Per-runnable runspec_env key in TOML
  3. [config] runspec_env key in TOML
  4. sys.prefix/.runspec_env (default, silent skip if absent)

Relative paths resolve from sys.prefix.
The file is loaded into os.environ; existing env vars are not overwritten.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE dotenv file. Comments and blank lines are ignored."""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def resolve_env_path_with_source(raw: dict[str, Any], runnable_name: str) -> tuple[Path, str]:
    """Like resolve_env_path but also returns a human-readable source label."""
    if env_file := os.environ.get("RUNSPEC_ENV_FILE"):
        return Path(env_file), "RUNSPEC_ENV_FILE"

    runnable_raw = raw.get("runnables", {}).get(runnable_name, {})
    if env_path := runnable_raw.get("runspec_env"):
        p = Path(env_path)
        label = f"runspec.toml [{runnable_name}] runspec_env"
        return (p if p.is_absolute() else Path(sys.prefix) / p), label

    if env_path := raw.get("config", {}).get("runspec_env"):
        p = Path(env_path)
        return (p if p.is_absolute() else Path(sys.prefix) / p), "runspec.toml [config] runspec_env"

    return Path(sys.prefix) / ".runspec_env", "sys.prefix (default)"


def resolve_env_path(raw: dict[str, Any], runnable_name: str) -> Path:
    """Return the resolved .runspec_env path for the given runnable."""
    # 1. RUNSPEC_ENV_FILE override (testing escape hatch)
    if env_file := os.environ.get("RUNSPEC_ENV_FILE"):
        return Path(env_file)

    # 2. Per-runnable runspec_env key (may be in raw dict or normalised runnable dict)
    runnable_raw = raw.get("runnables", {}).get(runnable_name, {})
    if env_path := runnable_raw.get("runspec_env"):
        p = Path(env_path)
        return p if p.is_absolute() else Path(sys.prefix) / p

    # 3. [config] runspec_env key
    if env_path := raw.get("config", {}).get("runspec_env"):
        p = Path(env_path)
        return p if p.is_absolute() else Path(sys.prefix) / p

    # 4. Default
    return Path(sys.prefix) / ".runspec_env"


def load_env_file(raw: dict[str, Any], runnable_name: str) -> dict[str, str]:
    """Load the .runspec_env file and return its contents. Silent skip if absent."""
    path = resolve_env_path(raw, runnable_name)
    if not path.exists():
        return {}
    return _parse_dotenv(path)


def apply_env_file(raw: dict[str, Any], runnable_name: str) -> tuple[dict[str, str], frozenset[str]]:
    """Load .runspec_env and merge values into os.environ (existing vars win).

    Returns (all_file_values, applied_keys) where applied_keys is the frozenset
    of keys actually written to os.environ. Keys already present in os.environ
    are not overwritten and are excluded from applied_keys.
    """
    values = load_env_file(raw, runnable_name)
    applied: set[str] = set()
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            applied.add(key)
    return values, frozenset(applied)


def make_env_namespace(values: dict[str, str]) -> SimpleNamespace:
    """Return a SimpleNamespace with lowercased keys from the env file values."""
    return SimpleNamespace(**{k.lower(): v for k, v in values.items()})
