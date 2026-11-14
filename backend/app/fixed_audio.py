"""Frozen fixed-audio requests, private storage and short-transaction preparation."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.paper import EvaluationPlan, Paper, PaperEntry
from app.taskbank import TaskRevision


class FixedAudio(Base):
    __tablename__ = "fixed_audio_requests"
    __table_args__ = (
        UniqueConstraint("paper_entry_id", "role", name="uq_fixed_audio_entry_role"),
        CheckConstraint("role IN ('guide', 'prompt')", name="ck_fixed_audio_role"),
        CheckConstraint("status IN ('PENDING', 'FAILED', 'READY')", name="ck_fixed_audio_status"),
        CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_fixed_audio_text_sha"),
        CheckConstraint(
            "(status = 'READY' AND storage_ref IS NOT NULL AND media_type IS NOT NULL AND content_sha256 IS NOT NULL AND error IS NULL) OR (status <> 'READY' AND storage_ref IS NULL AND media_type IS NULL AND content_sha256 IS NULL)",
            name="ck_fixed_audio_result",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_fixed_audio_content_sha",
        ),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="ck_fixed_audio_config"),
    )
    audio_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    paper_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("paper_entries.paper_entry_id"), nullable=False
    )
    task_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_revisions.task_revision_id"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    storage_ref: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_config(config: dict | None) -> dict:
    def contains_secret(value):
        if isinstance(value, dict):
            return any(
                str(key).lower().replace("-", "_")
                in {"api_key", "apikey", "token", "secret", "password", "authorization"}
                or contains_secret(item)
                for key, item in value.items()
            )
        return isinstance(value, (list, tuple)) and any(contains_secret(item) for item in value)

    if contains_secret(config):
        raise ValueError("Credentials are not allowed in audio configuration")
    if (
        not isinstance(config, dict)
        or any(
            not isinstance(config.get(k), str) or not config[k].strip()
            for k in ("provider", "model", "voice", "version")
        )
        or not isinstance(config.get("speed"), (int, float))
        or isinstance(config.get("speed"), bool)
        or config["speed"] <= 0
    ):
        raise ValueError("Explicit provider, model, voice, version and speed are required")
    try:
        return json.loads(json.dumps(config, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Audio configuration must be valid JSON") from exc


@dataclass(frozen=True)
class AudioData:
    data: bytes
    media_type: str


class Synthesizer(Protocol):
    def synthesize(self, source_text: str, config: dict) -> AudioData: ...


class AudioStore:
    def __init__(self, root: Path | None = None):
        self.root = (
            Path(root)
            if root is not None
            else Path(
                os.environ.get(
                    "FIXED_AUDIO_ROOT",
                    Path(__file__).resolve().parents[2] / "uploads" / "fixed-audio",
                )
            )
        )

    def read(self, ref: str, digest: str) -> bytes:
        expected = f"sha256/{digest[:2]}/{digest}"
        if (
            len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or ref != expected
        ):
            raise ValueError("Invalid audio reference")
        root = self.root.resolve()
        path = root / ref
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("Audio file is missing")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Audio digest mismatch")
        return data

    def put(self, data: bytes) -> tuple[str, str]:
        if not data:
            raise ValueError("Empty audio")
        digest = hashlib.sha256(data).hexdigest()
        ref = f"sha256/{digest[:2]}/{digest}"
        target = self.root / ref
        missing = []
        directory = target.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.root.resolve()) or target.is_symlink():
            raise ValueError("Unsafe audio storage")
        temporary = target.parent / (".tmp-" + uuid4().hex)
        try:
            with temporary.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            if self.read(ref, digest) != data:
                raise ValueError("Existing audio content mismatch")
            if os.name != "nt":
                for directory in dict.fromkeys([target.parent, *(item.parent for item in missing)]):
                    fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return ref, digest


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]


def readiness(session: Session, paper_id: UUID, store: AudioStore) -> Readiness:
    paper = session.scalar(
        select(Paper).where(Paper.paper_id == paper_id).execution_options(populate_existing=True)
    )
    if paper is None:
        raise ValueError("Paper does not exist")
    reasons = []
    if paper.status != "PUBLISHED":
        reasons.append("paper_not_published")
    if not paper.scoring_plugin_id or not paper.scoring_plugin_version:
        reasons.append("execution_contract_incomplete")
    rows = session.execute(
        select(PaperEntry, TaskRevision, EvaluationPlan)
        .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
        .outerjoin(EvaluationPlan, EvaluationPlan.paper_entry_id == PaperEntry.paper_entry_id)
        .where(PaperEntry.paper_id == paper_id)
        .execution_options(populate_existing=True)
    ).all()
    count = len(
        session.scalars(
            select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == paper_id)
        ).all()
    )
    if len(rows) != count:
        reasons.append("execution_contract_incomplete")
    for entry, revision, plan in rows:
        if plan is None or (plan.task_revision_id, plan.plugin_id, plan.plugin_version) != (
            entry.task_revision_id,
            paper.scoring_plugin_id,
            paper.scoring_plugin_version,
        ):
            reasons.append("execution_contract_incomplete")
        requests = session.scalars(
            select(FixedAudio)
            .where(FixedAudio.paper_entry_id == entry.paper_entry_id)
            .execution_options(populate_existing=True)
        ).all()
        by_role = {request.role: request for request in requests}
        expected = {"prompt": revision.prompt_text}
        if revision.guide_text:
            expected["guide"] = revision.guide_text
        if set(by_role) != set(expected):
            reasons.append("audio_request_incomplete")
            continue
        for role, source in expected.items():
            request = by_role[role]
            if (
                not source
                or request.task_revision_id != revision.task_revision_id
                or request.source_text != source
                or request.text_sha256 != text_hash(source)
            ):
                reasons.append("audio_request_invalid")
            elif request.status != "READY":
                reasons.append("audio_not_ready")
            else:
                try:
                    store.read(request.storage_ref, request.content_sha256)
                except (ValueError, OSError):
                    reasons.append("audio_file_invalid")
    return Readiness(not reasons, tuple(dict.fromkeys(reasons)))


def prepare_audio(
    engine: Engine, paper_id: UUID, store: AudioStore, synthesizer: Synthesizer
) -> dict[str, str]:
    """Process published requests using the durable worker."""
    from app.tts_worker import run_once

    with Session(engine) as session:
        paper = session.get(Paper, paper_id)
        if paper is None or paper.status != "PUBLISHED":
            raise ValueError("Paper is not published")
    return run_once(engine, store, synthesizer, paper_id)
