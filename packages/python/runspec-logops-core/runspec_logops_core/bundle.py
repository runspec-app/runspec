"""Bundle a digest + code map + metadata into one small zip for transfer.

The console's existing ``download_file`` agent tool then pulls this single small
artifact to local — the whole log and whole repo never transit the network.
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime


def build_bundle(digest: dict, code_map: dict | None, *, dest_dir: str, metadata: dict | None = None) -> dict:
    """Write ``digest.json`` (+ ``snippets.json`` + ``manifest.json``) into one zip.

    Returns the ``backup_files``-style ``{destination, size_bytes, size_mb,
    contents}``. Creates ``dest_dir`` if needed; propagates :class:`OSError` on
    write failure.
    """
    os.makedirs(dest_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = os.path.join(dest_dir, f"logops_digest_{timestamp}.zip")

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "log_file": digest.get("file"),
        "distinct_signatures": digest.get("distinct_signatures"),
        "has_code_map": code_map is not None,
        **(metadata or {}),
    }

    contents = ["digest.json", "manifest.json"]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("digest.json", json.dumps(digest, indent=2))
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        if code_map is not None:
            zf.writestr("snippets.json", json.dumps(code_map, indent=2))
            contents.append("snippets.json")

    size_bytes = os.path.getsize(archive_path)
    return {
        "destination": archive_path,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_048_576, 2),
        "contents": contents,
    }
