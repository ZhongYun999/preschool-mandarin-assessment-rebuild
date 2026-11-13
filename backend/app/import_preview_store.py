"""Persistent, integrity-bound private import previews."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder

from app.taskbank_excel import Issue
from app.taskbank_import import Baseline, ImportPreview, RowPreview


class PreviewNotFound(Exception):
    pass


class PreviewCorrupt(Exception):
    pass


class PreviewStore:
    def __init__(self, root: Path | None = None):
        self.root = (
            Path(root)
            if root is not None
            else Path(__file__).resolve().parents[2] / "uploads" / "import-previews"
        )

    def save(self, workbook: bytes, preview: ImportPreview) -> str:
        """Publish one immutable file atomically, with the identifier binding its bytes."""
        body = json.dumps(
            {
                "workbook": base64.b64encode(workbook).decode("ascii"),
                "preview": jsonable_encoder(asdict(preview)),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_hex(32)
        preview_id = nonce + sha256(bytes.fromhex(nonce) + body).hexdigest()
        missing_dirs = []
        directory = self.root
        while not directory.exists():
            missing_dirs.append(directory)
            directory = directory.parent
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = self.root / (".tmp-" + uuid4().hex)
        target = self.root / (preview_id + ".json")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, target)
            if os.name != "nt":
                for directory in dict.fromkeys(
                    [self.root, *(item.parent for item in missing_dirs)]
                ):
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return preview_id

    def load(self, preview_id: str, bank_id: UUID) -> tuple[bytes, ImportPreview]:
        if not re.fullmatch(r"[0-9a-f]{128}", preview_id):
            raise PreviewNotFound()
        path = self.root / (preview_id + ".json")
        try:
            if path.is_symlink():
                raise PreviewCorrupt()
            if path.resolve().parent != self.root.resolve():
                raise PreviewCorrupt()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as saved:
                body = saved.read()
        except FileNotFoundError:
            raise PreviewNotFound() from None
        expected = sha256(bytes.fromhex(preview_id[:64]) + body).hexdigest()
        if not hmac.compare_digest(expected, preview_id[64:]):
            raise PreviewCorrupt()
        try:
            stored = json.loads(body)
            data = base64.b64decode(stored["workbook"], validate=True)
            payload = stored["preview"]
            saved_bank = UUID(payload["bank_id"])
            if saved_bank != bank_id:
                raise PreviewNotFound()
            if sha256(data).hexdigest() != payload["source_sha256"]:
                raise PreviewCorrupt()
            rows = []
            for item in payload["rows"]:
                baseline = item["baseline"]
                if baseline is not None:
                    baseline = Baseline(
                        UUID(baseline["task_id"]) if baseline["task_id"] else None,
                        UUID(baseline["revision_id"]) if baseline["revision_id"] else None,
                        baseline["version"],
                        baseline["display_order"],
                    )
                rows.append(
                    RowPreview(
                        item["row"],
                        item["task_code"],
                        item["status"],
                        item["before_version"],
                        item["after_version"],
                        baseline,
                        item["fields"],
                        item["images"],
                        item["order"],
                    )
                )
            issues = [Issue(**issue) for issue in payload["issues"]]
            preview = ImportPreview(saved_bank, payload["source_sha256"], rows, issues)
            if not preview.valid:
                raise PreviewCorrupt()
            return data, preview
        except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
            raise PreviewCorrupt() from exc
