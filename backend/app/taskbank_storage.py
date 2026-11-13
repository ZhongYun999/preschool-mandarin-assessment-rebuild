"""Content-addressed local image storage; files are never replaced."""

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from app.taskbank_excel import ImageData


class ImageStore:
    def __init__(self, root: Path | None = None):
        self.root = (
            Path(root)
            if root is not None
            else Path(__file__).resolve().parents[2] / "uploads" / "taskbank"
        )

    def verify(self, ref: str, image: ImageData) -> None:
        """Check an existing reference without changing or replacing its object."""
        root = self.root.resolve()
        target = (root / ref).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("既有图片引用无效或文件不存在")
        if target.read_bytes() != image.data:
            raise ValueError("既有图片文件与当前内容不一致")

    def put(self, image: ImageData) -> str:
        digest = hashlib.sha256(image.data).hexdigest()
        if digest != image.sha256:
            raise ValueError("图片摘要与内容不一致")
        ref = f"sha256/{digest[:2]}/{digest}"
        target = self.root / ref
        missing_dirs = []
        directory = target.parent
        while not directory.exists():
            missing_dirs.append(directory)
            directory = directory.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.root.resolve()) or target.is_symlink():
            raise ValueError("图片存储路径不安全")
        temporary = target.parent / (".tmp-" + uuid4().hex)
        try:
            with temporary.open("xb") as out:
                out.write(image.data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            # A corrupt existing object is an error, never overwritten.
            if target.is_symlink() or target.read_bytes() != image.data:
                raise ValueError("已存在的图片文件与摘要不一致")
            if os.name != "nt":
                # Persist both the file link and any newly created directory entries.
                for directory in dict.fromkeys(
                    [target.parent, *(item.parent for item in missing_dirs)]
                ):
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return ref
