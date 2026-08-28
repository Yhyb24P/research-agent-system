import os
from pathlib import Path
from uuid import uuid4

from researchd.artifacts.hashing import sha256_bytes


class ArtifactCorruptionError(IOError):
    pass


class ContentAddressedArtifactStore:
    """Immutable bytes addressed only by SHA256, never by supplied filenames."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for_hash(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        path = self.root / "sha256" / sha256[:2] / sha256
        if not path.is_relative_to(self.root):
            raise ValueError("resolved artifact path escaped store root")
        return path

    def put(self, data: bytes) -> tuple[str, str]:
        digest = sha256_bytes(data)
        target = self.path_for_hash(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._verify_path(target, digest)
            return f"artifact://sha256/{digest}", digest
        temporary = target.parent / f".{digest}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return f"artifact://sha256/{digest}", digest

    def read(self, artifact_id: str) -> bytes:
        prefix = "artifact://sha256/"
        if not artifact_id.startswith(prefix):
            raise ValueError("invalid artifact ID")
        digest = artifact_id.removeprefix(prefix)
        path = self.path_for_hash(digest)
        data = path.read_bytes()
        if sha256_bytes(data) != digest:
            raise ArtifactCorruptionError(f"artifact hash mismatch: {artifact_id}")
        return data

    def verify(self, artifact_id: str) -> None:
        self.read(artifact_id)

    @staticmethod
    def _verify_path(path: Path, expected: str) -> None:
        if sha256_bytes(path.read_bytes()) != expected:
            raise ArtifactCorruptionError(f"artifact hash mismatch at {path}")
