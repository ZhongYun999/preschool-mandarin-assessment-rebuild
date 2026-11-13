"""Read-only task-bank preview and atomic, idempotent import commit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.import_batch import ImportBatch
from app.taskbank import (
    Asset,
    TaskBank,
    TaskIdentity,
    TaskRevision,
    append_revision,
    revision_fingerprint,
)
from app.taskbank_excel import SHEET, TYPE_LABELS, Issue, PreviewRow, preflight_xlsx
from app.taskbank_storage import ImageStore


class InvalidImportPreview(ValueError):
    """The provided preview does not correspond to a valid input."""


class StaleImportPreview(ValueError):
    """The database has changed since the preview."""


class ImportOperationConflict(ValueError):
    """An operation ID was already committed for different input."""


SLOT_COLUMNS = {
    "option_A": "option_a_asset_id",
    "option_B": "option_b_asset_id",
    "option_C": "option_c_asset_id",
    "question_image": "question_image_asset_id",
}


@dataclass(frozen=True)
class Baseline:
    task_id: UUID | None
    revision_id: UUID | None
    version: int | None
    display_order: int | None


@dataclass(frozen=True)
class RowPreview:
    row: int
    task_code: str | None
    status: str
    before_version: int | None
    after_version: int | None
    baseline: Baseline | None
    fields: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    order: dict = field(default_factory=dict)


@dataclass
class ImportPreview:
    bank_id: UUID
    source_sha256: str
    rows: list[RowPreview]
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def _current(session: Session, bank_id: UUID, code: str):
    # Table projections avoid refreshing or flushing the caller's ORM objects.
    task = (
        session.execute(
            select(TaskIdentity.__table__).where(
                TaskIdentity.bank_id == bank_id, TaskIdentity.task_code == code
            )
        )
        .mappings()
        .first()
    )
    revision = None
    if task is not None:
        revision = (
            session.execute(
                select(TaskRevision.__table__)
                .where(TaskRevision.task_id == task.task_id)
                .order_by(TaskRevision.version.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
    return task, revision


def _baseline(task, revision) -> Baseline:
    return Baseline(
        task.task_id if task else None,
        revision.task_revision_id if revision else None,
        revision.version if revision else None,
        task.display_order if task else None,
    )


def _image_hashes(session: Session, revision) -> dict[str, str]:
    if revision is None:
        return {}
    result = {}
    for slot, column in SLOT_COLUMNS.items():
        asset_id = getattr(revision, column)
        if asset_id is not None:
            result[slot] = session.scalar(select(Asset.sha256).where(Asset.asset_id == asset_id))
    return result


def _row_preview(session: Session, bank_id: UUID, row: PreviewRow) -> RowPreview:
    task, revision = _current(session, bank_id, row.task_code)
    previous_content = {
        key: getattr(revision, key) if revision else None for key in asdict(row.content)
    }
    fields = {
        key: {"before": previous_content[key], "after": value}
        for key, value in asdict(row.content).items()
        if previous_content[key] != value
    }
    old_images = _image_hashes(session, revision)
    new_images = {slot: image.sha256 for slot, image in row.images.items()}
    images = {
        slot: {"before": old_images.get(slot), "after": new_images.get(slot)}
        for slot in SLOT_COLUMNS
        if old_images.get(slot) != new_images.get(slot)
    }
    same = revision is not None and revision.content_hash == revision_fingerprint(
        row.content, new_images
    )
    status = "NEW" if revision is None else "UNCHANGED" if same else "NEW_REVISION"
    version = revision.version if revision else None
    after = 1 if version is None else version if same else version + 1
    before_order = task.display_order if task else None
    order = (
        {"before": before_order, "after": row.display_order}
        if before_order != row.display_order
        else {}
    )
    return RowPreview(
        row.row,
        row.task_code,
        status,
        version,
        after,
        _baseline(task, revision),
        fields,
        images,
        order,
    )


def preview_import(session: Session, bank_id: UUID, data: bytes) -> ImportPreview:
    """Read workbook and target bank only; never create identities or assets."""
    parsed = preflight_xlsx(data)
    issues = list(parsed.issues)
    rows = []
    with session.no_autoflush:
        if session.scalar(select(TaskBank.bank_id).where(TaskBank.bank_id == bank_id)) is None:
            issues.append(Issue("目标题库不存在"))
        else:
            for row in parsed.rows:
                if row.content.task_type not in TYPE_LABELS.values():
                    issues.append(Issue("题型不是当前支持的题型", row.row, "C"))
                else:
                    rows.append(_row_preview(session, bank_id, row))
    error_rows = {
        issue.row for issue in issues if issue.severity == "error" and issue.row is not None
    }
    if error_rows:
        # Read only the identifiers of invalid rows; preflight already reports their errors.
        wb = load_workbook(BytesIO(data), read_only=True, data_only=False)
        try:
            ws = wb[SHEET]
            for row_num in sorted(error_rows):
                rows = [row for row in rows if row.row != row_num]
                code = ws.cell(row_num, 1).value
                rows.append(
                    RowPreview(
                        row_num,
                        code.strip() if isinstance(code, str) else None,
                        "ERROR",
                        None,
                        None,
                        None,
                    )
                )
        finally:
            wb.close()
    rows.sort(key=lambda row: row.row)
    return ImportPreview(bank_id, sha256(data).hexdigest(), rows, issues)


def _prior_result(batch: ImportBatch, bank_id: UUID, digest: str) -> dict:
    if batch.bank_id != bank_id or batch.source_sha256 != digest:
        raise ImportOperationConflict("操作 ID 已用于不同题库或文件")
    return batch.result


def commit_import(
    engine: Engine, store: ImageStore, data: bytes, preview: ImportPreview, operation_id: str
) -> dict:
    """Persist image objects first, then commit all database changes in one transaction."""
    digest = sha256(data).hexdigest()
    if not operation_id or not preview.valid or preview.source_sha256 != digest:
        raise InvalidImportPreview("预览无效或与输入文件不一致，请重新预览")
    parsed = preflight_xlsx(data)
    if not parsed.valid or any(
        row.content.task_type not in TYPE_LABELS.values() for row in parsed.rows
    ):
        raise InvalidImportPreview("文件预检失败或题型不受支持")
    expected = {row.task_code: row for row in preview.rows if row.status != "ERROR"}
    if len(expected) != len(parsed.rows) or {row.task_code for row in parsed.rows} != set(expected):
        raise InvalidImportPreview("预览题目与输入不一致")
    with Session(engine) as session:
        prior = session.get(ImportBatch, operation_id)
        if prior is not None:
            return _prior_result(prior, preview.bank_id, digest)
    unique_images = {image.sha256: image for row in parsed.rows for image in row.images.values()}
    refs = {digest: store.put(image) for digest, image in unique_images.items()}
    with Session(engine) as session, session.begin():
        if session.connection().get_isolation_level() != "READ COMMITTED":
            raise ValueError("导入要求 READ COMMITTED 隔离级别")
        batch_id = uuid4()
        inserted = session.execute(
            insert(ImportBatch)
            .values(
                operation_id=operation_id,
                batch_id=batch_id,
                bank_id=preview.bank_id,
                source_sha256=digest,
                result={},
            )
            .on_conflict_do_nothing(index_elements=["operation_id"])
            .returning(ImportBatch.operation_id)
        ).scalar_one_or_none()
        if inserted is None:
            prior = session.get(ImportBatch, operation_id)
            return _prior_result(prior, preview.bank_id, digest)
        # Sort identities so overlapping batches acquire locks in a consistent order.
        verified: dict[str, RowPreview] = {}
        for row in sorted(parsed.rows, key=lambda item: item.task_code):
            old = expected[row.task_code].baseline
            if old is None:
                raise InvalidImportPreview("预览缺少版本基线，请重新预览")
            new_id = uuid4()
            created = session.execute(
                insert(TaskIdentity)
                .values(
                    task_id=new_id,
                    bank_id=preview.bank_id,
                    task_code=row.task_code,
                    display_order=row.display_order,
                )
                .on_conflict_do_nothing(index_elements=["bank_id", "task_code"])
                .returning(TaskIdentity.task_id)
            ).scalar_one_or_none()
            task = session.scalar(
                select(TaskIdentity)
                .where(
                    TaskIdentity.bank_id == preview.bank_id, TaskIdentity.task_code == row.task_code
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            _, revision = _current(session, preview.bank_id, row.task_code)
            actual = Baseline(None, None, None, None) if created else _baseline(task, revision)
            if actual != old:
                raise StaleImportPreview(f"题号 {row.task_code} 的预览基线已变化，请重新预览")
            current = _row_preview(session, preview.bank_id, row)
            if created:
                current = RowPreview(
                    current.row,
                    current.task_code,
                    current.status,
                    current.before_version,
                    current.after_version,
                    old,
                    current.fields,
                    current.images,
                    {"before": None, "after": row.display_order}
                    if row.display_order is not None
                    else {},
                )
            if current.status == "UNCHANGED":
                for slot, image in row.images.items():
                    asset = session.get(
                        Asset, getattr(revision, SLOT_COLUMNS[slot]), populate_existing=True
                    )
                    store.verify(asset.storage_ref, image)
            verified[row.task_code] = current
        # Files are durable before any Asset or TaskRevision is committed.
        assets: dict[str, UUID] = {}
        for digest_key in sorted(unique_images):
            image = unique_images[digest_key]
            ref = refs[digest_key]
            asset = session.scalar(
                select(Asset)
                .where(
                    Asset.sha256 == digest_key,
                    Asset.storage_ref == ref,
                    Asset.media_type == image.media_type,
                )
                .order_by(Asset.asset_id)
                .limit(1)
                .with_for_update()
            )
            if asset is None:
                asset = Asset(
                    asset_id=uuid4(),
                    sha256=digest_key,
                    storage_ref=ref,
                    media_type=image.media_type,
                )
                session.add(asset)
                session.flush()
            assets[digest_key] = asset.asset_id
        changes = []
        for row in sorted(parsed.rows, key=lambda item: item.task_code):
            prior = verified[row.task_code]
            result = append_revision(
                session,
                bank_id=preview.bank_id,
                task_code=row.task_code,
                display_order=row.display_order,
                content=row.content,
                images={slot: assets[image.sha256] for slot, image in row.images.items()},
            )
            changes.append(
                {
                    "row": row.row,
                    "task_code": row.task_code,
                    "task_id": str(result.task_id),
                    "revision_id": str(result.task_revision_id),
                    "before_version": prior.before_version,
                    "after_version": result.version,
                    "status": prior.status,
                    "fields": prior.fields,
                    "images": prior.images,
                    "order": prior.order,
                }
            )
        changes.sort(key=lambda item: item["row"])
        summary = {
            "batch_id": str(batch_id),
            "operation_id": operation_id,
            "bank_id": str(preview.bank_id),
            "source_sha256": digest,
            "counts": {
                status: sum(item["status"] == status for item in changes)
                for status in ("NEW", "NEW_REVISION", "UNCHANGED")
            },
            "items": changes,
        }
        session.execute(
            ImportBatch.__table__.update()
            .where(ImportBatch.operation_id == operation_id)
            .values(result=summary)
        )
        return summary
