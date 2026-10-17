"""Build a small ECS payload from an explicit allowlist; never copy local secrets."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

FILES = [
    "agent_state/__init__.py", "agent_state/config.py", "agent_state/engine.py",
    "agent_state/heads.py", "agent_state/notifications.py", "agent_state/objects.py",
    "agent_state/oss.py", "agent_state/runtime.py", "agent_state/server.py",
    "agent_state/storage.py", "agent_state/s3.py", "smoke_cloud.py",
    "requirements-oss.txt", "requirements-ecs.txt",
    "deploy/ecs/cloud.ecs.json", "deploy/ecs/agentdb-state.service",
    "deploy/ecs/ram-role-trust.json", "deploy/ecs/oss-policy.json",
    "deploy/ecs/install.sh", "deploy/ecs/verify_bundle.py",
    "docs/ALIYUN_ECS_DEPLOYMENT.md",
]


def build(root, output):
    root = Path(root).resolve()
    payload = {}
    for name in FILES:
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("Source escapes project")
        # All allowlisted files are UTF-8 text. Linux shell/unit files need LF.
        payload[name] = source.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")
    manifest = {"format": 1, "files": {name: hashlib.sha256(data).hexdigest()
                                      for name, data in sorted(payload.items())}}
    payload["MANIFEST.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream, gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in sorted(payload.items()):
                entry = tarfile.TarInfo("agentdb-state/" + name)
                entry.size, entry.mode = len(data), 0o644
                archive.addfile(entry, io.BytesIO(data))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(checksum + "  " + output.name + "\n", encoding="ascii")
    return {"archive": str(output), "sha256": checksum, "files": len(payload)}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(build(root, root / "dist/agentdb-state-ecs.tar.gz"), indent=2))
