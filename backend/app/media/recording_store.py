"""Private, immutable content-addressed recording storage."""

import hashlib
import os
import tempfile
from pathlib import Path


class RecordingStore:
    def __init__(self, root: Path | None = None):
        self.root = (
            Path(root)
            if root is not None
            else Path(
                os.environ.get(
                    "RECORDING_ROOT", Path(__file__).resolve().parents[3] / "uploads" / "recordings"
                )
            )
        )

    def staging_file(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, path = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        return os.fdopen(fd, "wb"), Path(path)

    def commit(self, temp: Path, digest: str) -> str:
        ref = f"sha256/{digest[:2]}/{digest}"
        root = self.root.resolve()
        target = root / ref
        missing = []
        directory = target.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.parent.resolve().is_relative_to(root) or target.is_symlink():
            raise ValueError("Unsafe recording storage")
        try:
            os.link(temp, target)
        except FileExistsError:
            pass
        self.verify(ref, digest)
        if os.name != "nt":
            for directory in dict.fromkeys([target.parent, *(item.parent for item in missing)]):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return ref

    def path(self, ref: str, digest: str) -> Path:
        if (
            len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or ref != f"sha256/{digest[:2]}/{digest}"
        ):
            raise ValueError("Invalid recording reference")
        root = self.root.resolve()
        target = root / ref
        if target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
            raise ValueError("Recording file is missing")
        return target

    def verify(self, ref: str, digest: str) -> None:
        actual = hashlib.sha256()
        with self.path(ref, digest).open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise ValueError("Recording digest mismatch")

    def read(self, ref: str, digest: str) -> bytes:
        data = self.path(ref, digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Recording digest mismatch")
        return data
