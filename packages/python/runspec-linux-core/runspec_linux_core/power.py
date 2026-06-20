"""Host power helpers: reboot."""

import shutil
import subprocess

from runspec_linux_core.errors import CommandError, ToolNotFoundError


def reboot_host(delay_minutes: int = 0) -> dict:
    """Reboot the host, optionally after ``delay_minutes`` minutes. Needs root.

    A delay of 0 (or less) reboots immediately (``shutdown -r now``); a positive
    delay schedules it (``shutdown -r +N``) and returns straight away. Prefers
    ``shutdown``, falling back to ``systemctl reboot`` for an immediate reboot if
    ``shutdown`` is absent. Returns ``{scheduled: True, delay_minutes, command}``.

    Raises ToolNotFoundError if no usable tool is present, or CommandError if the
    command exits non-zero (for example when run as an unprivileged user).
    """
    if shutil.which("shutdown"):
        when = "now" if delay_minutes <= 0 else f"+{delay_minutes}"
        cmd = ["shutdown", "-r", when]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(result.stderr.strip() or "shutdown failed")
        return {"scheduled": True, "delay_minutes": max(delay_minutes, 0), "command": " ".join(cmd)}

    # No `shutdown` (unusual). systemctl can only do an immediate reboot.
    if shutil.which("systemctl") and delay_minutes <= 0:
        result = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(result.stderr.strip() or "systemctl reboot failed")
        return {"scheduled": True, "delay_minutes": 0, "command": "systemctl reboot"}

    raise ToolNotFoundError("shutdown not available (and systemctl cannot schedule a delayed reboot)")
