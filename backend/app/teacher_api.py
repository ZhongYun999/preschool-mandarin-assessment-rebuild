"""Teacher-only task-bank management and immutable revision queries."""

import os
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.import_batch import ImportBatch
from app.import_preview_store import PreviewCorrupt, PreviewNotFound, PreviewStore
from app.taskbank import Asset, TaskBank, TaskIdentity, TaskRevision
from app.taskbank_excel import create_template
from app.taskbank_import import (
    SLOT_COLUMNS,
    ImportOperationConflict,
    InvalidImportPreview,
    StaleImportPreview,
    commit_import,
    preview_import,
)
from app.taskbank_storage import ImageStore
from app.teacher_auth import require_teacher

router = APIRouter(prefix="/teacher", dependencies=[Depends(require_teacher)])


@lru_cache(maxsize=1)
def _engine():
    return create_database_engine()


def get_db():
    with Session(_engine()) as session:
        yield session


def get_image_store() -> ImageStore:
    load_dotenv()
    root = os.environ.get("TASKBANK_ASSET_ROOT")
    return ImageStore(Path(root) if root else None)


def get_preview_store() -> PreviewStore:
    load_dotenv()
    root = os.environ.get("TASKBANK_PREVIEW_ROOT")
    return PreviewStore(Path(root) if root else None)


db_dependency = Depends(get_db)
store_dependency = Depends(get_image_store)
preview_store_dependency = Depends(get_preview_store)


class ImportConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1)


class BankInput(BaseModel):
    name: str
    description: str | None = None


class BankPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class BankView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bank_id: UUID
    name: str
    description: str | None


class TaskView(BaseModel):
    task_id: UUID
    task_code: str
    task_type: str
    grade_level: str
    content_summary: str
    version: int
    display_order: int | None


class ImageView(BaseModel):
    asset_id: UUID
    sha256: str
    media_type: str
    url: str


class RevisionView(BaseModel):
    task_revision_id: UUID
    task_id: UUID
    version: int
    created_at: datetime
    grade_level: str
    task_type: str
    guide_text: str | None
    prompt_text: str
    target_rounds: int | None
    material_description: str | None
    reference_answer: str | None
    scoring_instruction: str | None
    ai_followup_scope: str | None
    images: dict[str, ImageView]


def _bank(session: Session, bank_id: UUID) -> TaskBank:
    bank = session.get(TaskBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="题库不存在")
    return bank


def _task(session: Session, bank_id: UUID, task_id: UUID) -> TaskIdentity:
    task = session.scalar(
        select(TaskIdentity).where(TaskIdentity.bank_id == bank_id, TaskIdentity.task_id == task_id)
    )
    if task is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    return task


def _revision(session: Session, bank_id: UUID, task_id: UUID, revision_id: UUID) -> TaskRevision:
    _task(session, bank_id, task_id)
    rev = session.scalar(
        select(TaskRevision).where(
            TaskRevision.task_id == task_id, TaskRevision.task_revision_id == revision_id
        )
    )
    if rev is None:
        raise HTTPException(status_code=404, detail="题目修订不存在")
    return rev


@router.post("/banks", response_model=BankView, status_code=status.HTTP_201_CREATED)
def create_bank(data: BankInput, session: Session = db_dependency):
    bank = TaskBank(name=data.name, description=data.description)
    session.add(bank)
    session.commit()
    session.refresh(bank)
    return bank


@router.get("/banks", response_model=list[BankView])
def list_banks(session: Session = db_dependency):
    return session.scalars(select(TaskBank).order_by(TaskBank.name, TaskBank.bank_id)).all()


@router.get("/banks/{bank_id}", response_model=BankView)
def get_bank(bank_id: UUID, session: Session = db_dependency):
    return _bank(session, bank_id)


@router.patch("/banks/{bank_id}", response_model=BankView)
def update_bank(bank_id: UUID, data: BankPatch, session: Session = db_dependency):
    bank = _bank(session, bank_id)
    if "name" in data.model_fields_set:
        if data.name is None:
            raise HTTPException(status_code=422, detail="题库名称不能为 null")
        bank.name = data.name
    if "description" in data.model_fields_set:
        bank.description = data.description
    session.commit()
    session.refresh(bank)
    return bank


@router.get("/banks/{bank_id}/tasks", response_model=list[TaskView])
def list_tasks(
    bank_id: UUID,
    task_code: str | None = None,
    grade_level: str | None = None,
    task_type: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, gt=0),
    session: Session = db_dependency,
):
    _bank(session, bank_id)
    latest = (
        select(func.max(TaskRevision.version))
        .where(TaskRevision.task_id == TaskIdentity.task_id)
        .correlate(TaskIdentity)
        .scalar_subquery()
    )
    query = (
        select(TaskIdentity, TaskRevision)
        .join(TaskRevision, TaskRevision.task_id == TaskIdentity.task_id)
        .where(TaskIdentity.bank_id == bank_id, TaskRevision.version == latest)
    )
    if task_code is not None:
        query = query.where(TaskIdentity.task_code.contains(task_code, autoescape=True))
    if grade_level is not None:
        query = query.where(TaskRevision.grade_level == grade_level)
    if task_type is not None:
        query = query.where(TaskRevision.task_type == task_type)
    rows = session.execute(
        query.order_by(
            TaskIdentity.display_order.asc().nulls_last(),
            TaskIdentity.task_code.asc(),
            TaskIdentity.task_id.asc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        TaskView(
            task_id=t.task_id,
            task_code=t.task_code,
            task_type=r.task_type,
            grade_level=r.grade_level,
            content_summary=r.prompt_text[:120],
            version=r.version,
            display_order=t.display_order,
        )
        for t, r in rows
    ]


@router.get("/banks/{bank_id}/tasks/{task_id}/revisions")
def history(bank_id: UUID, task_id: UUID, session: Session = db_dependency):
    _task(session, bank_id, task_id)
    revisions = session.scalars(
        select(TaskRevision)
        .where(TaskRevision.task_id == task_id)
        .order_by(TaskRevision.version.desc())
    ).all()
    return [
        {"task_revision_id": r.task_revision_id, "version": r.version, "created_at": r.created_at}
        for r in revisions
    ]


@router.get("/banks/{bank_id}/tasks/{task_id}/revisions/{revision_id}", response_model=RevisionView)
def revision_detail(
    bank_id: UUID, task_id: UUID, revision_id: UUID, session: Session = db_dependency
):
    rev = _revision(session, bank_id, task_id, revision_id)
    images = {}
    for slot, column in SLOT_COLUMNS.items():
        asset_id = getattr(rev, column)
        if asset_id is not None:
            asset = session.get(Asset, asset_id)
            images[slot] = ImageView(
                asset_id=asset.asset_id,
                sha256=asset.sha256,
                media_type=asset.media_type,
                url=f"/teacher/banks/{bank_id}/tasks/{task_id}/revisions/{revision_id}/images/{slot}",
            )
    fields = {
        key: getattr(rev, key)
        for key in (
            "task_revision_id",
            "task_id",
            "version",
            "created_at",
            "grade_level",
            "task_type",
            "guide_text",
            "prompt_text",
            "target_rounds",
            "material_description",
            "reference_answer",
            "scoring_instruction",
            "ai_followup_scope",
        )
    }
    return RevisionView(**fields, images=images)


@router.get("/banks/{bank_id}/tasks/{task_id}/revisions/{revision_id}/images/{slot}")
def revision_image(
    bank_id: UUID,
    task_id: UUID,
    revision_id: UUID,
    slot: Literal["option_A", "option_B", "option_C", "question_image"],
    session: Session = db_dependency,
    store: ImageStore = store_dependency,
):
    rev = _revision(session, bank_id, task_id, revision_id)
    asset_id = getattr(rev, SLOT_COLUMNS[slot])
    if asset_id is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    asset = session.get(Asset, asset_id)
    root = store.root.resolve()
    target = (root / asset.storage_ref).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="图片不可用")
    try:
        data = target.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="图片不可用") from None
    if sha256(data).hexdigest() != asset.sha256:
        raise HTTPException(status_code=409, detail="图片完整性校验失败")
    return Response(
        data, media_type=asset.media_type, headers={"X-Content-Type-Options": "nosniff"}
    )


@router.get("/template")
def template():
    return Response(
        create_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="vnext-t1.5.xlsx"'},
    )


@router.get("/banks/{bank_id}/imports")
def list_imports(bank_id: UUID, session: Session = db_dependency):
    _bank(session, bank_id)
    batches = session.scalars(
        select(ImportBatch)
        .where(ImportBatch.bank_id == bank_id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.batch_id)
    ).all()
    return [
        {
            "batch_id": b.batch_id,
            "operation_id": b.operation_id,
            "created_at": b.created_at,
            "source_sha256": b.source_sha256,
            "counts": b.result.get("counts"),
        }
        for b in batches
    ]


@router.get("/banks/{bank_id}/imports/{batch_id}")
def get_import(bank_id: UUID, batch_id: UUID, session: Session = db_dependency):
    batch = session.scalar(
        select(ImportBatch).where(ImportBatch.bank_id == bank_id, ImportBatch.batch_id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return {
        "batch_id": batch.batch_id,
        "operation_id": batch.operation_id,
        "created_at": batch.created_at,
        "source_sha256": batch.source_sha256,
        "result": batch.result,
    }


def _load_preview(store: PreviewStore, preview_id: str, bank_id: UUID):
    try:
        return store.load(preview_id, bank_id)
    except PreviewNotFound:
        raise HTTPException(
            status_code=404, detail={"code": "preview_not_found", "message": "预览不存在"}
        ) from None
    except PreviewCorrupt:
        raise HTTPException(
            status_code=409,
            detail={"code": "preview_corrupt", "message": "预览数据已损坏，请重新预览"},
        ) from None
    except OSError:
        raise HTTPException(
            status_code=503, detail={"code": "storage_error", "message": "预览存储不可用"}
        ) from None


@router.post("/banks/{bank_id}/import-previews")
def create_import_preview(
    bank_id: UUID,
    file: Annotated[UploadFile, File()],
    session: Session = db_dependency,
    store: PreviewStore = preview_store_dependency,
):
    _bank(session, bank_id)
    try:
        data = file.file.read()
    except OSError:
        raise HTTPException(
            status_code=503, detail={"code": "storage_error", "message": "无法读取上传文件"}
        ) from None
    preview = preview_import(session, bank_id, data)
    if not preview.valid:
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_excel",
                "message": "Excel 预检失败",
                "rows": jsonable_encoder(preview.rows),
                "issues": jsonable_encoder(preview.issues),
            },
        )
    try:
        preview_id = store.save(data, preview)
    except OSError:
        raise HTTPException(
            status_code=503, detail={"code": "storage_error", "message": "预览存储不可用"}
        ) from None
    return {"preview_id": preview_id, "preview": jsonable_encoder(preview)}


@router.get("/banks/{bank_id}/import-previews/{preview_id}")
def get_import_preview(
    bank_id: UUID,
    preview_id: str,
    session: Session = db_dependency,
    store: PreviewStore = preview_store_dependency,
):
    _bank(session, bank_id)
    _, preview = _load_preview(store, preview_id, bank_id)
    return {"preview_id": preview_id, "preview": jsonable_encoder(preview)}


@router.post("/banks/{bank_id}/import-previews/{preview_id}/confirm")
def confirm_import(
    bank_id: UUID,
    preview_id: str,
    data: ImportConfirmation,
    session: Session = db_dependency,
    store: PreviewStore = preview_store_dependency,
    images: ImageStore = store_dependency,
):
    _bank(session, bank_id)
    workbook, preview = _load_preview(store, preview_id, bank_id)
    try:
        return commit_import(session.get_bind(), images, workbook, preview, data.operation_id)
    except StaleImportPreview:
        raise HTTPException(
            status_code=409, detail={"code": "preview_stale", "message": "题库已变化，请重新预览"}
        ) from None
    except ImportOperationConflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "operation_conflict", "message": "操作 ID 已用于不同输入"},
        ) from None
    except InvalidImportPreview:
        raise HTTPException(
            status_code=409,
            detail={"code": "preview_corrupt", "message": "预览数据无效，请重新预览"},
        ) from None
    except (OSError, ValueError):
        raise HTTPException(
            status_code=503, detail={"code": "storage_error", "message": "导入存储不可用"}
        ) from None
