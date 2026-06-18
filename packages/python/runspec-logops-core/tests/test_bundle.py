"""build_bundle writes one small zip with the expected members."""

import json
import zipfile

from runspec_logops_core import build_bundle


def test_bundle_contains_digest_and_manifest(tmp_path) -> None:
    digest = {"file": "/var/log/app.log", "distinct_signatures": 3, "top": []}
    res = build_bundle(digest, None, dest_dir=str(tmp_path), metadata={"host": "prod-1"})

    assert res["destination"].endswith(".zip")
    assert res["size_bytes"] > 0
    assert set(res["contents"]) == {"digest.json", "manifest.json"}

    with zipfile.ZipFile(res["destination"]) as zf:
        assert json.loads(zf.read("digest.json"))["distinct_signatures"] == 3
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["host"] == "prod-1"
        assert manifest["has_code_map"] is False


def test_bundle_includes_snippets_when_code_map_present(tmp_path) -> None:
    res = build_bundle({"file": "x"}, {"frames": [], "files_matched": 0}, dest_dir=str(tmp_path))
    assert "snippets.json" in res["contents"]
    with zipfile.ZipFile(res["destination"]) as zf:
        assert "snippets.json" in zf.namelist()
