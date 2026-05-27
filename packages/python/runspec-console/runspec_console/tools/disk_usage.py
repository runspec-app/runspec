"""disk-usage — show free/used space for all local drives or a specific path."""

import logging
import shutil
import string
import sys

import runspec

logger = logging.getLogger(__name__)


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main() -> None:
    args = runspec.parse("disk-usage")
    path: str | None = args.path.value if args.path.value else None

    if path:
        try:
            total, used, free = shutil.disk_usage(path)
        except OSError as exc:
            logger.error("Cannot read %s: %s", path, exc)
            sys.exit(1)
        pct = used / total * 100
        logger.info("%s: %s used / %s total, %s free (%.1f%%)", path, _fmt(used), _fmt(total), _fmt(free), pct)
    else:
        found = False
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            try:
                total, used, free = shutil.disk_usage(drive)
            except OSError:
                continue
            found = True
            pct = used / total * 100
            logger.info("%s  %s used / %s total, %s free (%.1f%%)", drive, _fmt(used), _fmt(total), _fmt(free), pct)
        if not found:
            logger.warning("No drives found")
