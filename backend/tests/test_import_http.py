import json
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_taskbank_import import commit, picture, xlsx

from app.import_batch import ImportBatch
from app.import_preview_store import PreviewStore
from app.taskbank import Asset, TaskBank, TaskIdentity, TaskRevision
from app.taskbank_storage import ImageStore
from app.teacher_api import get_db, get_image_store, get_preview_store, router

AUTH = {"Authorization": "Bearer test-token-only"}


def make_app(db, images, previews):
    app = FastAPI()
    app.include_router(router)

    def session_override():
        with Session(db) as session:
            yield session

    app.dependency_overrides[get_db] = session_override
    app.dependency_overrides[get_image_store] = lambda: ImageStore(images)
    app.dependency_overrides[get_preview_store] = lambda: PreviewStore(previews)
    return app


@pytest.fixture
def setup(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TEACHER_MANAGEMENT_TOKEN", "test-token-only")
    monkeypatch.setattr("app.teacher_auth.load_dotenv", lambda: None)
    images, previews = tmp_path / "images", tmp_path / "previews"
    app = make_app(db, images, previews)
    with TestClient(app) as client:
        yield client, images, previews


def upload(client, bank, data, headers=AUTH):
    return client.post(
        f"/teacher/banks/{bank}/import-previews",
        headers=headers,
        files={
            "file": (
                "sample.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def confirm(client, bank, preview_id, operation_id, headers=AUTH):
    return client.post(
        f"/teacher/banks/{bank}/import-previews/{preview_id}/confirm",
        headers=headers,
        json={"operation_id": operation_id},
    )


def test_upload_preview_commit_image_and_retry(setup, db, bank):
    client, images, previews = setup
    data = xlsx()
    response = upload(client, bank, data)
    assert response.status_code == 200
    preview = response.json()
    assert preview["preview"]["rows"][0]["status"] == "NEW"
    preview_id = preview["preview_id"]
    assert (previews / f"{preview_id}.json").exists() and not images.exists()
    with Session(db) as s:
        for model in (TaskIdentity, TaskRevision, Asset, ImportBatch):
            assert s.scalars(select(model)).all() == []
    read = client.get(f"/teacher/banks/{bank}/import-previews/{preview_id}", headers=AUTH)
    assert read.status_code == 200 and read.json() == preview
    saved = confirm(client, bank, preview_id, "http-one")
    assert saved.status_code == 200 and saved.json()["counts"]["NEW"] == 1
    assert confirm(client, bank, preview_id, "http-one").json() == saved.json()
    item = saved.json()["items"][0]
    revision_url = f"/teacher/banks/{bank}/tasks/{item['task_id']}/revisions/{item['revision_id']}"
    detail = client.get(revision_url, headers=AUTH).json()
    assert client.get(detail["images"]["option_A"]["url"], headers=AUTH).content == picture("red")
    report = client.get(f"/teacher/banks/{bank}/imports/{saved.json()['batch_id']}", headers=AUTH)
    assert report.status_code == 200 and report.json()["result"] == saved.json()


def test_invalid_excel_returns_position_and_no_preview(setup, db, bank):
    client, _, previews = setup
    wb = load_workbook(BytesIO(xlsx(kind="对话题")))
    wb["题库导入"]["G2"] = 0
    buf = BytesIO()
    wb.save(buf)
    wb.close()
    response = upload(client, bank, buf.getvalue())
    assert response.status_code == 422 and response.json()["code"] == "invalid_excel"
    assert any(issue["row"] == 2 and issue["column"] == "G" for issue in response.json()["issues"])
    assert "preview_id" not in response.json() and not previews.exists()
    with Session(db) as s:
        for model in (ImportBatch, TaskIdentity, TaskRevision, Asset):
            assert s.scalars(select(model)).all() == []


def test_preview_survives_app_recreation(setup, db, bank):
    client, images, previews = setup
    preview_id = upload(client, bank, xlsx()).json()["preview_id"]
    app = make_app(db, images, previews)
    with TestClient(app) as fresh:
        assert (
            fresh.get(
                f"/teacher/banks/{bank}/import-previews/{preview_id}", headers=AUTH
            ).status_code
            == 200
        )
        assert confirm(fresh, bank, preview_id, "after-restart").status_code == 200


def test_wrong_id_bank_and_tampered_payload_rejected(setup, db, bank):
    client, _, previews = setup
    other = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=other, name="其他"))
    preview_id = upload(client, bank, xlsx()).json()["preview_id"]
    assert confirm(client, other, preview_id, "wrong-bank").status_code == 404
    assert confirm(client, bank, "0" * 128, "wrong-id").status_code == 404
    assert confirm(client, bank, "invalid", "invalid-id").status_code == 404
    file = previews / f"{preview_id}.json"
    raw = json.loads(file.read_text(encoding="utf-8"))
    raw["workbook"] = "Y2hhbmdlZA=="
    file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    response = confirm(client, bank, preview_id, "tampered")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "preview_corrupt"
    assert str(previews) not in response.text
    with Session(db) as s:
        assert s.scalars(select(ImportBatch)).all() == []


def test_tampered_bank_or_baseline_in_stored_preview_rejected(setup, db, bank):
    client, _, previews = setup
    preview_id = upload(client, bank, xlsx()).json()["preview_id"]
    path = previews / f"{preview_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["preview"]["bank_id"] = str(uuid4())
    payload["preview"]["rows"][0]["baseline"]["version"] = 42
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    response = confirm(client, bank, preview_id, "tampered-meta")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "preview_corrupt"


def test_stale_preview_and_operation_conflict_are_distinct(setup, db, bank):
    client, images, _ = setup
    old = upload(client, bank, xlsx(second=True)).json()["preview_id"]
    commit(db, images, bank, xlsx(prompt="并发变更"), "external")
    stale = confirm(client, bank, old, "stale-http")
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "preview_stale"
    with Session(db) as s:
        assert s.get(ImportBatch, "stale-http") is None
        assert s.scalar(select(TaskIdentity).where(TaskIdentity.task_code == "0002")) is None
    new_id = upload(client, bank, xlsx(prompt="更新")).json()["preview_id"]
    assert confirm(client, bank, new_id, "reused").status_code == 200
    changed_id = upload(client, bank, xlsx(prompt="不同输入")).json()["preview_id"]
    conflict = confirm(client, bank, changed_id, "reused")
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "operation_conflict"


def test_http_order_images_and_a_b_a(setup, bank):
    client, _, _ = setup

    def run(data, op):
        p = upload(client, bank, data)
        assert p.status_code == 200
        r = confirm(client, bank, p.json()["preview_id"], op)
        assert r.status_code == 200
        return p.json()["preview"]["rows"][0], r.json()["items"][0]

    _, a = run(xlsx(), "one")
    order, a_order = run(xlsx(order=4), "order")
    assert order["status"] == "UNCHANGED" and a_order["after_version"] == 1
    image, b = run(xlsx(order=4, color="purple"), "image")
    assert image["status"] == "NEW_REVISION" and set(image["images"]) == {"option_A"}
    _, a_again = run(xlsx(order=4), "again")
    assert [a["after_version"], b["after_version"], a_again["after_version"]] == [1, 2, 3]


def test_auth_missing_configuration_and_bad_bank(setup, bank, monkeypatch):
    client, _, _ = setup
    data = xlsx()
    preview_id = upload(client, bank, data).json()["preview_id"]
    path = f"/teacher/banks/{bank}/import-previews/{preview_id}"
    assert upload(client, bank, data, {}).status_code == 401
    assert client.get(path).status_code == 401
    assert confirm(client, bank, preview_id, "x", {}).status_code == 401
    assert upload(client, bank, data, {"Authorization": "Bearer wrong"}).status_code == 401
    monkeypatch.delenv("TEACHER_MANAGEMENT_TOKEN")
    assert upload(client, bank, data).status_code == 503
    assert client.get(path, headers=AUTH).status_code == 503
    assert confirm(client, bank, preview_id, "x").status_code == 503
    monkeypatch.setenv("TEACHER_MANAGEMENT_TOKEN", "test-token-only")
    assert upload(client, uuid4(), data).status_code == 404


def test_storage_failure_is_generic_and_confirmation_rejects_extra_fields(
    setup, db, bank, tmp_path
):
    client, _, previews = setup
    preview_id = upload(client, bank, xlsx()).json()["preview_id"]
    endpoint = f"/teacher/banks/{bank}/import-previews/{preview_id}/confirm"
    assert (
        client.post(endpoint, headers=AUTH, json={"operation_id": "x", "baseline": {}}).status_code
        == 422
    )
    app = make_app(db, tmp_path / "images", tmp_path / "not-a-directory")
    (tmp_path / "not-a-directory").write_text("x")
    with TestClient(app) as c:
        response = upload(c, bank, xlsx())
        assert response.status_code == 503 and response.json()["detail"]["code"] == "storage_error"
        assert str(tmp_path) not in response.text
    assert previews.exists()


def test_two_different_operations_competing_for_same_preview(setup, db, bank):
    client, images, previews = setup
    preview_id = upload(client, bank, xlsx(second=True)).json()["preview_id"]
    barrier = Barrier(3, timeout=10)

    def worker(i):
        with TestClient(make_app(db, images, previews)) as c:
            barrier.wait()
            return confirm(c, bank, preview_id, f"parallel-{i}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            blocker.execute(
                select(TaskBank.bank_id).where(TaskBank.bank_id == bank).with_for_update()
            )
            futures = [executor.submit(worker, i) for i in range(2)]
            barrier.wait()
            end = time.monotonic() + 10
            blocked = 0
            with db.connect() as observer:
                while time.monotonic() < end:
                    blocked = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :schema AND cardinality(pg_blocking_pids(pid)) > 0"
                        ),
                        {"schema": schema},
                    )
                    if blocked >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
            assert blocked >= 2
        results = [f.result(timeout=10) for f in futures]
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (
        next(r for r in results if r.status_code == 409).json()["detail"]["code"] == "preview_stale"
    )
    with Session(db) as s:
        assert len(s.scalars(select(TaskIdentity)).all()) == 2
        assert len(s.scalars(select(TaskRevision)).all()) == 2
        assert len(s.scalars(select(ImportBatch)).all()) == 1


def test_confirm_storage_failure_rolls_back_and_hides_path(setup, db, bank, tmp_path):
    client, _, previews = setup
    preview_id = upload(client, bank, xlsx()).json()["preview_id"]
    bad_root = tmp_path / "unavailable"
    bad_root.write_text("not a directory")
    with TestClient(make_app(db, bad_root, previews)) as other:
        response = confirm(other, bank, preview_id, "failed-storage")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "storage_error"
    assert str(tmp_path) not in response.text
    with Session(db) as s:
        for model in (ImportBatch, TaskIdentity, TaskRevision, Asset):
            assert s.scalars(select(model)).all() == []


def test_upload_filename_is_not_used_as_path(setup, bank):
    client, _, previews = setup
    response = client.post(
        f"/teacher/banks/{bank}/import-previews",
        headers=AUTH,
        files={"file": ("../../not-a-path.xlsx", xlsx(), "application/octet-stream")},
    )
    assert response.status_code == 200
    assert {p.name for p in previews.iterdir()} == {response.json()["preview_id"] + ".json"}
