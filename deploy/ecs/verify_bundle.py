"""Verify deployment payload before installing; no cloud or database access."""
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify(root):
    root = root.resolve()
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not manifest.get("files"):
        raise ValueError("Unsupported or empty bundle manifest")
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("Unsafe bundle path")
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Bundle file escapes install source")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    return len(manifest["files"])


if __name__ == "__main__":
    print(f"Verified {verify(Path(__file__).resolve().parents[2])} deployment files")
