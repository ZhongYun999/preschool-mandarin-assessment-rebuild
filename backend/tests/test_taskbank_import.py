import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from openpyxl.drawing.image import Image
from PIL import Image as PillowImage
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.import_batch import ImportBatch
from app.taskbank import (
    Asset,
    TaskBank,
    TaskIdentity,
    TaskRevision,
    append_revision,
)
from app.taskbank_excel import create_template, preflight_xlsx
from app.taskbank_import import commit_import, preview_import
from app.taskbank_storage import ImageStore


def picture(color):
    buffer = BytesIO()
    PillowImage.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


def xlsx(prompt="题目A", order=1, color="red", code="0001", kind="选择题", second=False):
    wb = load_workbook(BytesIO(create_template()))
    ws = wb["题库导入"]
    for col, val in {1: code, 2: "任意学段", 3: kind, 4: order, 6: prompt, 13: "A"}.items():
        ws.cell(2, col, val)
    for col, shade in zip("HIJK", (color, "green", "blue", "yellow"), strict=True):
        ws.add_image(Image(BytesIO(picture(shade))), f"{col}2")
    if second:
        for col, val in {1: "0002", 2: "学段", 3: "问答题", 4: 2, 6: "第二题"}.items():
            ws.cell(3, col, val)
    stream = BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


def preview(db, bank_id, data):
    with Session(db) as s:
        return preview_import(s, bank_id, data)


def commit(db, root, bank_id, data, op, p=None):
    return commit_import(db, ImageStore(root), data, p or preview(db, bank_id, data), op)


def test_read_only_preview_then_independent_banks_and_a_b_a(db, bank, tmp_path):
    data_a, data_b = xlsx(), xlsx(prompt="题目B")
    second_bank = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=second_bank, name="另一个"))
    p = preview(db, bank, data_a)
    assert p.valid and p.rows[0].status == "NEW" and p.rows[0].after_version == 1
    assert not (tmp_path / "objects").exists()
    with Session(db) as s:
        for model in (TaskIdentity, TaskRevision, Asset, ImportBatch):
            assert s.scalars(select(model)).all() == []
    root = tmp_path / "objects"
    result_a = commit(db, root, bank, data_a, "one", p)
    result_other = commit(db, root, second_bank, data_a, "other")
    assert result_a["items"][0]["task_id"] != result_other["items"][0]["task_id"]
    unchanged = commit(db, root, bank, data_a, "two")
    assert unchanged["items"][0]["status"] == "UNCHANGED"
    p_b = preview(db, bank, data_b)
    assert p_b.rows[0].status == "NEW_REVISION"
    assert p_b.rows[0].fields["prompt_text"] == {"before": "题目A", "after": "题目B"}
    assert commit(db, root, bank, data_b, "three", p_b)["items"][0]["after_version"] == 2
    assert commit(db, root, bank, data_a, "four")["items"][0]["after_version"] == 3
    with Session(db) as s:
        revisions = s.scalars(
            select(TaskRevision)
            .where(TaskRevision.task_id == result_a["items"][0]["task_id"])
            .order_by(TaskRevision.version)
        ).all()
        assert [r.prompt_text for r in revisions] == ["题目A", "题目B", "题目A"]


def test_order_only_and_images_history_and_dedup(db, bank, tmp_path):
    root = tmp_path / "objects"
    first = xlsx()
    item = commit(db, root, bank, first, "i1")["items"][0]
    order = xlsx(order=8)
    p = preview(db, bank, order)
    assert p.rows[0].status == "UNCHANGED" and p.rows[0].order == {"before": 1, "after": 8}
    assert commit(db, root, bank, order, "i2", p)["items"][0]["after_version"] == 1
    changed = xlsx(order=8, color="purple")
    p = preview(db, bank, changed)
    assert p.rows[0].status == "NEW_REVISION" and set(p.rows[0].images) == {"option_A"}
    result = commit(db, root, bank, changed, "i3", p)
    assert result["items"][0]["after_version"] == 2
    from uuid import UUID

    with Session(db) as s:
        first_rev = s.get(TaskRevision, UUID(item["revision_id"]))
        new_rev = s.get(TaskRevision, UUID(result["items"][0]["revision_id"]))
        for attr, shade in (
            ("option_a_asset_id", "red"),
            ("option_b_asset_id", "green"),
            ("option_c_asset_id", "blue"),
            ("question_image_asset_id", "yellow"),
        ):
            asset = s.get(Asset, getattr(first_rev, attr))
            assert (root / asset.storage_ref).read_bytes() == picture(shade)
        assert first_rev.option_a_asset_id != new_rev.option_a_asset_id
        assert first_rev.option_b_asset_id == new_rev.option_b_asset_id
    assert len([p for p in root.rglob("*") if p.is_file()]) == 5


@pytest.mark.parametrize("change", ["revision", "order", "new_identity"])
def test_stale_baseline_rejected(db, bank, tmp_path, change):
    first = xlsx()
    if change != "new_identity":
        commit(db, tmp_path, bank, first, "initial")
    old = preview(db, bank, first)
    if change == "revision":
        commit(db, tmp_path, bank, xlsx(prompt="并发修改"), "other")
    elif change == "order":
        commit(db, tmp_path, bank, xlsx(order=12), "other")
    else:
        commit(db, tmp_path, bank, first, "other")
    with pytest.raises(ValueError, match="基线已变化"):
        commit(db, tmp_path, bank, first, "stale", old)
    with Session(db) as s:
        assert s.get(ImportBatch, "stale") is None


def test_mid_batch_failure_rolls_back_all_database_changes(db, bank, tmp_path, monkeypatch):
    import app.taskbank_import as module

    data = xlsx(second=True)
    p = preview(db, bank, data)
    actual = module.append_revision
    counter = 0

    def fail_second(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == 2:
            raise RuntimeError("test failure")
        return actual(*args, **kwargs)

    monkeypatch.setattr(module, "append_revision", fail_second)
    with pytest.raises(RuntimeError, match="test failure"):
        commit(db, tmp_path, bank, data, "failed", p)
    with Session(db) as s:
        for model in (TaskIdentity, TaskRevision, Asset, ImportBatch):
            assert s.scalars(select(model)).all() == []


def test_operation_retry_and_input_conflict(db, bank, tmp_path):
    data = xlsx()
    p = preview(db, bank, data)
    a = commit(db, tmp_path, bank, data, "stable", p)
    assert commit(db, tmp_path, bank, data, "stable", p) == a
    with pytest.raises(ValueError, match="操作 ID"):
        commit(db, tmp_path, bank, xlsx(prompt="不同"), "stable")
    b = commit(db, tmp_path, bank, data, "another")
    assert b["items"][0]["status"] == "UNCHANGED"
    with Session(db) as s:
        assert len(s.scalars(select(TaskRevision)).all()) == 1
        assert len(s.scalars(select(ImportBatch)).all()) == 2


def test_unknown_type_and_invalid_xlsx_cannot_commit(db, bank, tmp_path):
    data = xlsx(kind="unregistered")
    assert preflight_xlsx(data).valid
    p = preview(db, bank, data)
    assert not p.valid and p.rows[0].status == "ERROR" and p.rows[0].row == 2
    with pytest.raises(ValueError):
        commit(db, tmp_path, bank, data, "unknown", p)
    bad = preview(db, bank, b"not a workbook")
    assert not bad.valid
    with pytest.raises(ValueError):
        commit(db, tmp_path, bank, b"not a workbook", "bad", bad)
    with Session(db) as s:
        assert s.scalars(select(ImportBatch)).all() == []


def test_content_store_existing_corruption_never_overwritten(db, bank, tmp_path):
    data = xlsx()
    parsed = preflight_xlsx(data)
    image = parsed.rows[0].images["option_A"]
    target = tmp_path / "sha256" / image.sha256[:2] / image.sha256
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="图片文件"):
        commit(db, tmp_path, bank, data, "bad-store")
    assert target.read_bytes() == b"corrupt"
    with Session(db) as s:
        assert s.scalars(select(ImportBatch)).all() == []


@pytest.mark.parametrize("same_operation", [True, False])
def test_concurrent_commits_are_idempotent_or_stale(db, bank, tmp_path, same_operation):
    data = xlsx(second=True)
    p = preview(db, bank, data)
    barrier = Barrier(3, timeout=10)

    def worker(i):
        barrier.wait()
        try:
            return commit(db, tmp_path, bank, data, "same" if same_operation else f"op{i}", p)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            blocker.execute(
                select(TaskBank.bank_id).where(TaskBank.bank_id == bank).with_for_update()
            )
            futures = [executor.submit(worker, i) for i in range(2)]
            barrier.wait()
            deadline = time.monotonic() + 10
            blocked_count = 0
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked_count = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :schema "
                            "AND cardinality(pg_blocking_pids(pid)) > 0"
                        ),
                        {"schema": schema},
                    )
                    if blocked_count >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
            assert blocked_count >= 2, "both import transactions must contend in PostgreSQL"
        results = [future.result(timeout=10) for future in futures]
    if same_operation:
        assert results[0] == results[1] and isinstance(results[0], dict)
    else:
        assert sum(isinstance(item, dict) for item in results) == 1
        assert any(isinstance(item, str) and "基线已变化" in item for item in results)
    with Session(db) as s:
        assert len(s.scalars(select(TaskIdentity)).all()) == 2
        assert len(s.scalars(select(TaskRevision)).all()) == 2
        assert len(s.scalars(select(ImportBatch)).all()) == 1


def test_same_operation_id_cannot_move_to_another_bank(db, bank, tmp_path):
    data = xlsx()
    commit(db, tmp_path, bank, data, "global-op")
    other = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=other, name="另一个"))
    with pytest.raises(ValueError, match="操作 ID"):
        commit(db, tmp_path, other, data, "global-op")
    with Session(db) as s:
        assert len(s.scalars(select(TaskIdentity)).all()) == 1
        assert len(s.scalars(select(ImportBatch)).all()) == 1


def test_retry_does_not_require_rewriting_storage(db, bank, tmp_path, monkeypatch):
    data = xlsx()
    p = preview(db, bank, data)
    result = commit(db, tmp_path, bank, data, "retry-no-storage", p)

    def must_not_write(self, image):
        raise AssertionError("retry unexpectedly wrote a file")

    monkeypatch.setattr(ImageStore, "put", must_not_write)
    assert commit(db, tmp_path, bank, data, "retry-no-storage", p) == result


def test_unchanged_revision_requires_its_existing_image_reference(db, bank, tmp_path):
    data = xlsx(kind="问答题")
    parsed = preflight_xlsx(data)
    assert parsed.valid
    row = parsed.rows[0]
    images = {}
    with Session(db) as s, s.begin():
        for slot, image in row.images.items():
            asset = Asset(
                asset_id=uuid4(),
                sha256=image.sha256,
                storage_ref=f"missing-{slot}",
                media_type=image.media_type,
            )
            s.add(asset)
            images[slot] = asset.asset_id
        s.flush()
        append_revision(
            s,
            bank_id=bank,
            task_code=row.task_code,
            display_order=row.display_order,
            content=row.content,
            images=images,
        )
    p = preview(db, bank, data)
    assert p.rows[0].status == "UNCHANGED"
    with pytest.raises(ValueError, match="既有图片引用"):
        commit(db, tmp_path, bank, data, "missing-reference", p)
    with Session(db) as s:
        assert s.get(ImportBatch, "missing-reference") is None


def test_preview_does_not_flush_or_overwrite_caller_pending_changes(db, bank, tmp_path):
    from uuid import UUID

    data = xlsx()
    result = commit(db, tmp_path, bank, data, "initial-preview")
    task_id = UUID(result["items"][0]["task_id"])
    with Session(db) as session, session.begin():
        task = session.get(TaskIdentity, task_id)
        task.display_order = 999
        result = preview_import(session, bank, data)
        assert result.valid and result.rows[0].baseline.display_order == 1
        assert task.display_order == 999 and task in session.dirty
        session.rollback()
    with Session(db) as session:
        assert session.get(TaskIdentity, task_id).display_order == 1
