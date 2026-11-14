"""Read-only publication preflight and atomic execution-contract freeze."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fixed_audio import AudioStore, FixedAudio, readiness, text_hash, validate_config
from app.paper import EvaluationPlan, Paper, PaperEntry, _lock_draft
from app.scoring_registry import PRODUCTION_REGISTRY, Registry
from app.taskbank import TaskIdentity, TaskRevision


@dataclass(frozen=True)
class EntryPreflight:
    entry_id: UUID
    revision_id: UUID
    position: int
    task_type: str
    requirements: list[dict[str, Any]]
    fixed_audio: dict[str, str]
    blockers: list[str]


@dataclass(frozen=True)
class PublicationPreflight:
    paper_id: UUID
    edit_version: int
    entries: list[EntryPreflight]
    blockers: list[str]

    @property
    def valid(self) -> bool:
        return not self.blockers and all(not entry.blockers for entry in self.entries)


class PublicationBlocked(ValueError):
    def __init__(self, result: PublicationPreflight):
        super().__init__("Publication preflight failed")
        self.result = result


def select_plugin(
    session: Session,
    *,
    paper_id: UUID,
    expected_version: int,
    plugin_id: str,
    version: str,
    registry: Registry = PRODUCTION_REGISTRY,
) -> None:
    paper = _lock_draft(session, paper_id, expected_version)
    plugin = registry.resolve(plugin_id, version)
    if plugin is None or not plugin.selectable:
        raise ValueError("Scoring plugin version is not selectable")
    if (paper.scoring_plugin_id, paper.scoring_plugin_version) != (plugin_id, version):
        plans = session.scalars(
            select(EvaluationPlan).join(PaperEntry).where(PaperEntry.paper_id == paper_id)
        ).all()
        for plan in plans:
            session.delete(plan)
        session.flush()
    paper.scoring_plugin_id, paper.scoring_plugin_version = plugin_id, version
    paper.edit_version += 1
    session.flush()


def preflight_publication(
    session: Session, paper_id: UUID, registry: Registry = PRODUCTION_REGISTRY
) -> PublicationPreflight:
    # Read-only even if the caller has pending ORM edits.
    with session.no_autoflush:
        paper = session.get(Paper, paper_id)
        if paper is None:
            raise ValueError("Paper does not exist")
        blockers = []
        if paper.status != "DRAFT":
            blockers.append("Paper is not a draft")
        plugin = registry.resolve(paper.scoring_plugin_id, paper.scoring_plugin_version)
        if plugin is None:
            blockers.append("Scoring plugin version is missing or unavailable")
        rows = session.execute(
            select(PaperEntry, TaskRevision, TaskIdentity, EvaluationPlan)
            .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
            .join(TaskIdentity, TaskIdentity.task_id == TaskRevision.task_id)
            .outerjoin(EvaluationPlan, EvaluationPlan.paper_entry_id == PaperEntry.paper_entry_id)
            .where(PaperEntry.paper_id == paper_id)
            .order_by(PaperEntry.position)
        ).all()
        entry_count = len(
            session.scalars(
                select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == paper_id)
            ).all()
        )
        if len(rows) != entry_count or len({row[0].position for row in rows}) != len(rows):
            blockers.append("Paper entry references or positions are invalid")
        results = []
        for entry, revision, identity, inherited in rows:
            errors = []
            if identity.bank_id != paper.bank_id:
                errors.append("Entry belongs to another bank")
            requirements = []
            if inherited is not None:
                if (inherited.task_revision_id, inherited.plugin_id, inherited.plugin_version) != (
                    entry.task_revision_id,
                    paper.scoring_plugin_id,
                    paper.scoring_plugin_version,
                ):
                    errors.append("Inherited evaluation plan does not match entry or plugin")
                requirements = inherited.requirements
            elif plugin is not None:
                declared = registry.requirements_for(
                    paper.scoring_plugin_id, paper.scoring_plugin_version, revision.task_type
                )
                if declared is None:
                    errors.append("Plugin does not declare this task type")
                else:
                    requirements = declared
            try:
                requirements = json.loads(json.dumps(requirements, allow_nan=False))
                if not isinstance(requirements, list):
                    raise TypeError("requirements must be a list")
            except (TypeError, ValueError):
                errors.append("Evaluation requirements are not valid JSON")
                requirements = []
            for req in requirements:
                if not isinstance(req, dict) or not registry.supports(revision.task_type, req):
                    errors.append("Evaluation contract is unavailable or incompatible")
            audio = {"prompt": revision.prompt_text}
            if not revision.prompt_text:
                errors.append("Prompt audio text is missing")
            if revision.guide_text:
                audio["guide"] = revision.guide_text
            results.append(
                EntryPreflight(
                    entry.paper_entry_id,
                    revision.task_revision_id,
                    entry.position,
                    revision.task_type,
                    requirements,
                    audio,
                    errors,
                )
            )
        return PublicationPreflight(paper.paper_id, paper.edit_version, results, blockers)


def publish(
    session: Session,
    *,
    paper_id: UUID,
    expected_version: int,
    registry: Registry = PRODUCTION_REGISTRY,
    audio_config: dict | None = None,
) -> PublicationPreflight:
    paper = _lock_draft(session, paper_id, expected_version)
    result = preflight_publication(session, paper_id, registry)
    if not result.valid:
        raise PublicationBlocked(result)
    for entry in result.entries:
        if session.get(EvaluationPlan, entry.entry_id) is None:
            session.add(
                EvaluationPlan(
                    paper_entry_id=entry.entry_id,
                    task_revision_id=entry.revision_id,
                    plugin_id=paper.scoring_plugin_id,
                    plugin_version=paper.scoring_plugin_version,
                    requirements=entry.requirements,
                )
            )
        existing = {
            row.role: row
            for row in session.scalars(
                select(FixedAudio).where(FixedAudio.paper_entry_id == entry.entry_id)
            ).all()
        }
        for role, source in entry.fixed_audio.items():
            if role in existing:
                continue
            config = validate_config(audio_config)
            session.add(
                FixedAudio(
                    audio_id=uuid4(),
                    paper_entry_id=entry.entry_id,
                    task_revision_id=entry.revision_id,
                    role=role,
                    source_text=source,
                    text_sha256=text_hash(source),
                    config=config,
                    status="PENDING",
                )
            )
    session.flush()
    paper.status = "PUBLISHED"
    paper.edit_version += 1
    session.flush()
    return result


def ready_for_new_session(session: Session, paper_id: UUID, store: AudioStore) -> bool:
    return readiness(session, paper_id, store).ready
