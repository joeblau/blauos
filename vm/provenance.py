#!/usr/bin/env python3
"""Record target files produced solely by the pinned source-built Nix image closure."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
revision = sys.argv[2]
local_sources = json.loads(Path(sys.argv[4]).read_text())
artifacts = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic not in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
        continue
    install_path = "/" + str(path.relative_to(root))
    artifact = {"path": install_path,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "source": "puredarwin-vm-next",
                      "revision": revision,
                      "provenance_scope": "Pinned PureDarwin Nix composition and transitive flake/source dependencies; not a claim that all code originated in PureDarwin"}
    if install_path in local_sources["artifacts"]:
        source_files = local_sources["artifacts"][install_path]
        snapshot = json.dumps(sorted(source_files, key=lambda entry: entry["path"]), sort_keys=True, separators=(",", ":"))
        artifact.update(source="blauos", revision="workspace-sha256:" + hashlib.sha256(snapshot.encode()).hexdigest(),
                        source_files=source_files, workspace_revision=local_sources["workspaceRevision"],
                        provenance_scope="Exact BlauOS source snapshot compiled against the source-built PureDarwin runtime")
    artifacts.append(artifact)
providers = ["/usr/lib/libobjc.A.dylib"] if (root / "usr/lib/libobjc.A.dylib").is_file() else []
Path(sys.argv[3]).write_text(json.dumps({"schema_version": 1, "artifacts": artifacts,
                                        "flat_namespace_providers": providers}, indent=2) + "\n")
