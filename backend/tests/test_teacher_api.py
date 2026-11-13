from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy.orm import Session
from test_taskbank_import import commit, picture, xlsx

from app.main import app
from app.taskbank import RevisionContent, append_revision
from app.taskbank_storage import ImageStore
from app.teacher_api import get_db, get_image_store


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TEACHER_MANAGEMENT_TOKEN", "example-only-test-token")
    monkeypatch.setattr("app.teacher_auth.load_dotenv", lambda: None)

    def session_override():
        with Session(db) as s:
            yield s

    app.dependency_overrides[get_db] = session_override
    app.dependency_overrides[get_image_store] = lambda: ImageStore(tmp_path)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer example-only-test-token"}


def make_bank(client, name="题库", description="说明"):
    response = client.post(
        "/teacher/banks", json={"name": name, "description": description}, headers=AUTH
    )
    assert response.status_code == 201
    return response.json()["bank_id"]


def test_all_teacher_routes_require_token_and_unconfigured_fails_closed(client, monkeypatch):
    ids = str(uuid4())
    routes = [
        ("POST", "/teacher/banks"),
        ("GET", "/teacher/banks"),
        ("GET", f"/teacher/banks/{ids}"),
        ("PATCH", f"/teacher/banks/{ids}"),
        ("GET", f"/teacher/banks/{ids}/tasks"),
        ("GET", f"/teacher/banks/{ids}/tasks/{ids}/revisions"),
        ("GET", f"/teacher/banks/{ids}/tasks/{ids}/revisions/{ids}"),
        ("GET", f"/teacher/banks/{ids}/tasks/{ids}/revisions/{ids}/images/option_A"),
        ("GET", "/teacher/template"),
        ("GET", f"/teacher/banks/{ids}/imports"),
        ("GET", f"/teacher/banks/{ids}/imports/{ids}"),
    ]
    for method, url in routes:
        assert client.request(method, url).status_code == 401
        assert (
            client.request(method, url, headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
    monkeypatch.delenv("TEACHER_MANAGEMENT_TOKEN")
    assert client.get("/teacher/banks", headers=AUTH).status_code == 503
    assert client.get("/health").json() == {"status": "ok"}
    assert "example-only-test-token" not in client.get("/health").text


def test_bank_create_list_details_and_rename_without_unique_name(client):
    first = make_bank(client)
    second = make_bank(client)
    assert first != second
    assert {b["bank_id"] for b in client.get("/teacher/banks", headers=AUTH).json()} == {
        first,
        second,
    }
    updated = client.patch(
        f"/teacher/banks/{first}", headers=AUTH, json={"name": "新名称", "description": None}
    )
    assert updated.status_code == 200 and updated.json()["description"] is None
    assert client.get(f"/teacher/banks/{first}", headers=AUTH).json()["name"] == "新名称"
    assert (
        client.patch(f"/teacher/banks/{first}", headers=AUTH, json={"name": None}).status_code
        == 422
    )
    assert client.get(f"/teacher/banks/{uuid4()}", headers=AUTH).status_code == 404


def test_imported_revisions_history_cross_bank_and_images(client, db, tmp_path):
    bank1, bank2 = make_bank(client), make_bank(client)
    b1, b2 = UUID(bank1), UUID(bank2)
    a, b, a_again = xlsx(), xlsx(prompt="题目B", color="purple"), xlsx()
    first = commit(db, tmp_path, b1, a, "api-a")["items"][0]
    middle = commit(db, tmp_path, b1, b, "api-b")["items"][0]
    last = commit(db, tmp_path, b1, a_again, "api-c")["items"][0]
    other = commit(db, tmp_path, b2, a, "api-other")["items"][0]
    assert first["task_id"] != other["task_id"]
    task_id = first["task_id"]
    assert (
        client.patch(
            f"/teacher/banks/{bank1}", headers=AUTH, json={"name": "重命名后的题库"}
        ).status_code
        == 200
    )
    base = f"/teacher/banks/{bank1}/tasks/{task_id}"
    rows = client.get(f"/teacher/banks/{bank1}/tasks", headers=AUTH).json()
    assert len(rows) == 1 and rows[0]["task_code"] == "0001" and rows[0]["version"] == 3
    assert "title" not in rows[0] and "reference_answer" not in rows[0]
    history = client.get(base + "/revisions", headers=AUTH).json()
    assert [x["version"] for x in history] == [3, 2, 1]
    for item, prompt in [(first, "题目A"), (middle, "题目B"), (last, "题目A")]:
        detail = client.get(base + "/revisions/" + item["revision_id"], headers=AUTH)
        assert detail.status_code == 200 and detail.json()["prompt_text"] == prompt
        assert detail.json()["reference_answer"] == "A"
        assert set(detail.json()["images"]) == {
            "option_A",
            "option_B",
            "option_C",
            "question_image",
        }
        assert "storage_ref" not in detail.text and str(tmp_path) not in detail.text
    image_url = client.get(base + "/revisions/" + first["revision_id"], headers=AUTH).json()[
        "images"
    ]["option_A"]["url"]
    image = client.get(image_url, headers=AUTH)
    assert image.status_code == 200 and image.content == picture("red")
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    middle_detail = client.get(base + "/revisions/" + middle["revision_id"], headers=AUTH).json()
    middle_image = client.get(middle_detail["images"]["option_A"]["url"], headers=AUTH)
    assert middle_image.status_code == 200 and middle_image.content == picture("purple")
    assert client.get(image_url).status_code == 401
    wrong = image_url.replace(f"/banks/{bank1}/", f"/banks/{bank2}/")
    assert client.get(wrong, headers=AUTH).status_code == 404
    assert (
        client.get(f"/teacher/banks/{bank2}/tasks/{task_id}/revisions", headers=AUTH).status_code
        == 404
    )
    assert client.get(base + "/revisions/" + other["revision_id"], headers=AUTH).status_code == 404
    assert client.get("/health").json() == {"status": "ok"}


def test_list_latest_filter_pagination_and_order_only_update(client, db):
    bank = make_bank(client)
    bank_id = UUID(bank)
    with Session(db) as s, s.begin():
        for code, grade, typ, order in [
            ("b", "B", "qa", 2),
            ("a", "A", "qa", 2),
            ("c", "A", "speaking", None),
        ]:
            append_revision(
                s,
                bank_id=bank_id,
                task_code=code,
                display_order=order,
                content=RevisionContent(grade_level=grade, task_type=typ, prompt_text=code),
                images={},
            )
        append_revision(
            s,
            bank_id=bank_id,
            task_code="a",
            display_order=1,
            content=RevisionContent(grade_level="A", task_type="qa", prompt_text="a2"),
            images={},
        )
        append_revision(
            s,
            bank_id=bank_id,
            task_code="b",
            display_order=1,
            content=RevisionContent(grade_level="B", task_type="qa", prompt_text="b"),
            images={},
        )
    endpoint = f"/teacher/banks/{bank}/tasks"
    items = client.get(endpoint, headers=AUTH).json()
    assert [x["task_code"] for x in items] == ["a", "b", "c"]
    assert [x["version"] for x in items] == [2, 1, 1]
    assert [
        x["task_code"]
        for x in client.get(endpoint, headers=AUTH, params={"offset": 1, "limit": 1}).json()
    ] == ["b"]
    assert [
        x["task_code"]
        for x in client.get(
            endpoint, headers=AUTH, params={"grade_level": "A", "task_type": "qa"}
        ).json()
    ] == ["a"]
    assert [
        x["task_code"] for x in client.get(endpoint, headers=AUTH, params={"task_code": "b"}).json()
    ] == ["b"]
    assert client.get(endpoint, headers=AUTH, params={"limit": 0}).status_code == 422


def test_template_download_import_report_and_cross_bank(client, db, tmp_path):
    bank1, bank2 = make_bank(client), make_bank(client)
    response = client.get("/teacher/template", headers=AUTH)
    assert response.status_code == 200
    assert load_workbook(BytesIO(response.content)).sheetnames == ["题库导入", "填写说明"]
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    committed = commit(db, tmp_path, UUID(bank1), xlsx(), "report-1")
    reports = client.get(f"/teacher/banks/{bank1}/imports", headers=AUTH).json()
    assert len(reports) == 1 and reports[0]["batch_id"] == committed["batch_id"]
    detail = client.get(f"/teacher/banks/{bank1}/imports/{committed['batch_id']}", headers=AUTH)
    assert detail.status_code == 200 and detail.json()["result"] == committed
    assert str(tmp_path) not in detail.text and "storage_ref" not in detail.text
    assert (
        client.get(
            f"/teacher/banks/{bank2}/imports/{committed['batch_id']}", headers=AUTH
        ).status_code
        == 404
    )
    assert client.get(f"/teacher/banks/{bank2}/imports", headers=AUTH).json() == []


def test_missing_or_tampered_asset_not_returned(client, db, tmp_path):
    bank = make_bank(client)
    item = commit(db, tmp_path, UUID(bank), xlsx(), "bad-asset")["items"][0]
    base = f"/teacher/banks/{bank}/tasks/{item['task_id']}/revisions/{item['revision_id']}"
    detail = client.get(base, headers=AUTH).json()
    image = detail["images"]["option_A"]
    from app.taskbank import Asset

    with Session(db) as s:
        asset = s.get(Asset, UUID(image["asset_id"]))
        file = tmp_path / asset.storage_ref
    file.write_bytes(b"bad")
    response = client.get(image["url"], headers=AUTH)
    assert response.status_code == 409 and str(tmp_path) not in response.text
    file.unlink()
    assert client.get(image["url"], headers=AUTH).status_code == 404


def test_teacher_detail_contains_scoring_fields_and_no_public_route(client, db):
    bank_id = make_bank(client)
    with Session(db) as s, s.begin():
        result = append_revision(
            s,
            bank_id=UUID(bank_id),
            task_code="private",
            display_order=1,
            content=RevisionContent(
                grade_level="A",
                task_type="qa",
                prompt_text="题干",
                reference_answer="参考答案",
                scoring_instruction="评分说明",
                ai_followup_scope="追问范围",
            ),
            images={},
        )
    url = f"/teacher/banks/{bank_id}/tasks/{result.task_id}/revisions/{result.task_revision_id}"
    data = client.get(url, headers=AUTH).json()
    assert data["reference_answer"] == "参考答案"
    assert data["scoring_instruction"] == "评分说明"
    assert data["ai_followup_scope"] == "追问范围"
    assert data["created_at"] and data["images"] == {}
    assert client.get(url).status_code == 401
    assert "评分说明" not in client.get(url).text
    assert client.get(url.replace("/teacher/", "/")).status_code == 404
    assert "评分说明" not in client.get("/health").text


def test_image_cannot_resolve_outside_storage_root(client, db, tmp_path):
    from hashlib import sha256

    from app.taskbank import Asset

    bank = make_bank(client)
    data = b"not-for-this-root"
    outside = tmp_path.parent / ("outside-" + uuid4().hex)
    outside.write_bytes(data)
    asset_id = uuid4()
    try:
        with Session(db) as s, s.begin():
            s.add(
                Asset(
                    asset_id=asset_id,
                    sha256=sha256(data).hexdigest(),
                    storage_ref="../" + outside.name,
                    media_type="image/png",
                )
            )
            s.flush()
            result = append_revision(
                s,
                bank_id=UUID(bank),
                task_code="outside",
                display_order=1,
                content=RevisionContent(grade_level="A", task_type="qa", prompt_text="X"),
                images={"question_image": asset_id},
            )
        url = (
            f"/teacher/banks/{bank}/tasks/{result.task_id}/revisions/"
            f"{result.task_revision_id}/images/question_image"
        )
        response = client.get(url, headers=AUTH)
        assert response.status_code == 404 and data not in response.content
        assert str(outside) not in response.text
    finally:
        outside.unlink(missing_ok=True)
