"""DATA → PAPER → TTS acceptance with real XLSX, PostgreSQL and teacher HTTP."""

import hashlib
import io
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.drawing.image import Image
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_import_http import AUTH, confirm, make_app, upload
from test_publication import TEST_AUDIO_CONFIG, TestPlugin
from test_taskbank_import import picture

from app.fixed_audio import AudioData, AudioStore, FixedAudio
from app.import_batch import ImportBatch
from app.paper import (
    EvaluationPlan,
    Paper,
    PaperEditConflict,
    PaperEntry,
    add_entry,
    copy_paper,
    create_draft,
    reorder_entries,
    replace_entry,
)
from app.publication import preflight_publication, publish, ready_for_new_session, select_plugin
from app.scoring_registry import EvaluationContract, Registry
from app.taskbank import Asset, TaskIdentity, TaskRevision
from app.taskbank_excel import create_template
from app.teacher_api import get_audio_store
from app.tts_attempt import TTSAttempt
from app.tts_worker import run_audio, run_once


def workbook(revision=1):
    book = load_workbook(io.BytesIO(create_template()))
    sheet = book["题库导入"]
    rows = [
        {
            1: "0001",
            2: "任意学段",
            3: "选择题",
            4: 40,
            5: "请选择",
            6: "完整选择题？",
            12: "材料一",
            13: "A" if revision == 1 else "B",
            14: f"评价说明{revision}",
            15: f"追问范围{revision}",
        },
        {
            1: "0002",
            2: "另一学段",
            3: "对话题",
            4: 10,
            6: "第一句？第二句！完整内容。",
            7: 2,
            12: "材料二",
            13: "参考回答",
            14: "对话评分",
            15: "对话范围",
        },
        {1: "0003", 2: "任意学段", 3: "问答题", 4: 20, 6: "说一说", 13: "参考内容", 14: "问答评分"},
    ]
    for row, values in enumerate(rows, 2):
        for column, value in values.items():
            sheet.cell(row, column, value)
    for cell, color in (("H2", "red"), ("I2", "green"), ("J2", "blue"), ("K3", "yellow")):
        sheet.add_image(Image(io.BytesIO(picture(color))), cell)
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class Synth:
    def __init__(self):
        self.calls = []

    def synthesize(self, source_text, config):
        self.calls.append((source_text, dict(config)))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(hashlib.sha256(source_text.encode()).digest())
        return AudioData(buffer.getvalue(), "audio/wav")


def make_registry():
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = (
        {"source": "fake-llm", "version": "1", "parameters": {"mode": "qa"}},
    )
    contract = EvaluationContract(
        "fake-llm", "1", frozenset({"qa", "instruction"}), lambda p: p == {"mode": "qa"}
    )
    return Registry([plugin], [contract])


@pytest.fixture
def context(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TEACHER_MANAGEMENT_TOKEN", "test-token-only")
    monkeypatch.setattr("app.teacher_auth.load_dotenv", lambda: None)
    images, previews, audio = tmp_path / "images", tmp_path / "previews", tmp_path / "audio"
    app = make_app(db, images, previews)
    store = AudioStore(audio)
    app.dependency_overrides[get_audio_store] = lambda: store
    with TestClient(app) as client:
        bank = client.post("/teacher/banks", headers=AUTH, json={"name": "集成题库"})
        assert bank.status_code == 201
        yield client, UUID(bank.json()["bank_id"]), images, previews, store


def import_workbook(client, bank, data, operation):
    preview = upload(client, bank, data)
    assert preview.status_code == 200, preview.text
    preview_id = preview.json()["preview_id"]
    result = confirm(client, bank, preview_id, operation)
    assert result.status_code == 200, result.text
    return preview.json(), result.json()


def draft_from_import(db, bank, result):
    items = {item["task_code"]: item for item in result["items"]}
    reg = make_registry()
    with Session(db) as s, s.begin():
        paper_id = create_draft(s, bank_id=bank, name="集成试卷").paper_id
        a = add_entry(
            s,
            paper_id=paper_id,
            expected_version=0,
            task_id=UUID(items["0001"]["task_id"]),
            position=1,
        ).paper_entry_id
        b = add_entry(
            s,
            paper_id=paper_id,
            expected_version=1,
            task_id=UUID(items["0003"]["task_id"]),
            position=2,
        ).paper_entry_id
        replace_entry(
            s,
            paper_id=paper_id,
            expected_version=2,
            entry_id=b,
            task_id=UUID(items["0002"]["task_id"]),
        )
        reorder_entries(s, paper_id=paper_id, expected_version=3, entry_ids=[b, a])
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=4,
            plugin_id="test-only",
            version="1",
            registry=reg,
        )
    return paper_id, a, b, items, reg


def test_full_http_import_paper_worker_and_historical_copy(context, db):
    client, bank, images, previews, store = context
    source = workbook()
    preview, result = import_workbook(client, bank, source, "integ-a")
    assert [row["status"] for row in preview["preview"]["rows"]] == ["NEW"] * 3
    assert (
        result["counts"]["NEW"] == 3
        and result["source_sha256"] == hashlib.sha256(source).hexdigest()
    )
    assert (previews / f"{preview['preview_id']}.json").exists()
    assert preview["preview"]["rows"][0]["fields"]["reference_answer"]["after"] == "A"
    assert set(preview["preview"]["rows"][0]["images"]) == {"option_A", "option_B", "option_C"}
    fresh_app = make_app(db, images, previews)
    with TestClient(fresh_app) as fresh_client:
        assert (
            fresh_client.get(
                f"/teacher/banks/{bank}/import-previews/{preview['preview_id']}", headers=AUTH
            ).json()
            == preview
        )
    assert (
        client.get(
            f"/teacher/banks/{bank}/import-previews/{preview['preview_id']}", headers=AUTH
        ).json()
        == preview
    )
    assert confirm(client, bank, preview["preview_id"], "integ-a").json() == result
    other_bank = UUID(
        client.post("/teacher/banks", headers=AUTH, json={"name": "独立题库"}).json()["bank_id"]
    )
    _, other_result = import_workbook(client, other_bank, source, "integ-other")
    assert result["items"][0]["task_id"] != other_result["items"][0]["task_id"]
    item = result["items"][0]
    detail_url = f"/teacher/banks/{bank}/tasks/{item['task_id']}/revisions/{item['revision_id']}"
    assert client.get(detail_url).status_code == 401
    detail = client.get(detail_url, headers=AUTH).json()
    assert (
        detail["reference_answer"],
        detail["scoring_instruction"],
        detail["ai_followup_scope"],
    ) == ("A", "评价说明1", "追问范围1")
    for slot, color in (("option_A", "red"), ("option_B", "green"), ("option_C", "blue")):
        assert client.get(detail["images"][slot]["url"], headers=AUTH).content == picture(color)
    dialogue = result["items"][1]
    dialogue_url = (
        f"/teacher/banks/{bank}/tasks/{dialogue['task_id']}/revisions/{dialogue['revision_id']}"
    )
    dialogue_detail = client.get(dialogue_url, headers=AUTH).json()
    assert client.get(
        dialogue_detail["images"]["question_image"]["url"], headers=AUTH
    ).content == picture("yellow")
    with Session(db) as s:
        assert len(s.scalars(select(Asset)).all()) == 4
    paper_id, a, b, items, reg = draft_from_import(db, bank, result)
    with Session(db) as s:
        p = preflight_publication(s, paper_id, reg)
        assert p.valid and [entry.entry_id for entry in p.entries] == [b, a]
        assert p.entries[0].fixed_audio == {"prompt": "第一句？第二句！完整内容。"}
        assert s.get(TaskIdentity, UUID(items["0001"]["task_id"])).display_order == 40
    with Session(db) as s, s.begin():
        publish(
            s, paper_id=paper_id, expected_version=5, registry=reg, audio_config=TEST_AUDIO_CONFIG
        )
    with Session(db) as s:
        assert [
            (e.paper_entry_id, str(e.task_revision_id))
            for e in s.scalars(
                select(PaperEntry)
                .where(PaperEntry.paper_id == paper_id)
                .order_by(PaperEntry.position)
            )
        ] == [(b, items["0002"]["revision_id"]), (a, items["0001"]["revision_id"])]
        frozen_requirements = s.get(EvaluationPlan, a).requirements
        assert frozen_requirements == [
            {"source": "fake-llm", "version": "1", "parameters": {"mode": "qa"}}
        ]
        audios = s.scalars(
            select(FixedAudio).join(PaperEntry).where(PaperEntry.paper_id == paper_id)
        ).all()
        assert {(r.paper_entry_id, r.role) for r in audios} == {
            (a, "guide"),
            (a, "prompt"),
            (b, "prompt"),
        }
        assert not ready_for_new_session(s, paper_id, store)
    status_url = f"/teacher/papers/{paper_id}/audio"
    assert client.get(status_url).status_code == 401
    prepare = client.post(status_url + "/prepare", headers=AUTH)
    assert prepare.status_code == 202 and "等待" in prepare.json()["message"]
    with Session(db) as s:
        assert s.scalars(select(TTSAttempt)).all() == []
    assert not client.get(status_url, headers=AUTH).json()["ready"]
    synth = Synth()
    assert set(run_once(db, store, synth, paper_id).values()) == {"ready"}
    assert {call[0] for call in synth.calls} == {
        "请选择",
        "完整选择题？",
        "第一句？第二句！完整内容。",
    }
    assert all(call[1] == TEST_AUDIO_CONFIG for call in synth.calls)
    assert all(
        not any(secret in call[0] for secret in ("评价说明", "追问范围", "参考回答"))
        for call in synth.calls
    )
    status = client.get(status_url, headers=AUTH).json()
    assert status["ready"] and all(item["status"] == "READY" for item in status["items"])
    for audio in status["items"]:
        response = client.get(f"{status_url}/{audio['audio_id']}/content", headers=AUTH)
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(response.content)) as wav:
            assert wav.getframerate() == 8000 and wav.getnframes() > 0
        with Session(db) as s:
            row = s.get(FixedAudio, UUID(audio["audio_id"]))
            assert hashlib.sha256(response.content).hexdigest() == row.content_sha256
            assert ready_for_new_session(s, paper_id, store)
    stale = upload(client, bank, workbook(3)).json()["preview_id"]
    _, second = import_workbook(client, bank, workbook(2), "integ-v2")
    stale_response = confirm(client, bank, stale, "integ-stale")
    assert (
        stale_response.status_code == 409
        and stale_response.json()["detail"]["code"] == "preview_stale"
    )
    _, third = import_workbook(client, bank, workbook(3), "integ-v3")
    assert second["items"][0]["after_version"] == 2 and third["items"][0]["after_version"] == 3
    with Session(db) as s, s.begin():
        old = s.get(TaskRevision, UUID(item["revision_id"]))
        assert (old.reference_answer, old.scoring_instruction, old.ai_followup_scope) == (
            "A",
            "评价说明1",
            "追问范围1",
        )
        assert s.get(PaperEntry, a).task_revision_id == old.task_revision_id
        assert s.get(EvaluationPlan, a).requirements == frozen_requirements
        latest = s.get(TaskRevision, UUID(third["items"][0]["revision_id"]))
        assert (latest.reference_answer, latest.scoring_instruction, latest.ai_followup_scope) == (
            "B",
            "评价说明3",
            "追问范围3",
        )
        assert {
            (r.role, r.source_text, r.status)
            for r in s.scalars(select(FixedAudio).where(FixedAudio.paper_entry_id == a))
        } == {("guide", "请选择", "READY"), ("prompt", "完整选择题？", "READY")}
        assert s.get(ImportBatch, "integ-stale") is None
        copied_id = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s:
        copied = s.scalars(
            select(PaperEntry).where(PaperEntry.paper_id == copied_id).order_by(PaperEntry.position)
        ).all()
        assert [e.task_revision_id for e in copied] == [
            UUID(items["0002"]["revision_id"]),
            UUID(item["revision_id"]),
        ]
        assert (
            s.get(Paper, copied_id).scoring_plugin_id,
            s.get(Paper, copied_id).scoring_plugin_version,
        ) == ("test-only", "1")
        copied_a = copied[1].paper_entry_id
        assert s.get(EvaluationPlan, copied_a).requirements == frozen_requirements
        assert all(
            audio.config == TEST_AUDIO_CONFIG
            for audio in s.scalars(select(FixedAudio).where(FixedAudio.paper_entry_id == copied_a))
        )
    with Session(db) as s, s.begin():
        replace_entry(
            s,
            paper_id=copied_id,
            expected_version=0,
            entry_id=copied_a,
            task_id=UUID(item["task_id"]),
        )
    with Session(db) as s:
        assert s.get(PaperEntry, copied_a).task_revision_id == UUID(
            third["items"][0]["revision_id"]
        )
        assert s.get(PaperEntry, a).task_revision_id == UUID(item["revision_id"])
    for statement, target in (
        ("UPDATE paper_entries SET position=123 WHERE paper_entry_id=:id", a),
        ("UPDATE evaluation_plans SET requirements='[]' WHERE paper_entry_id=:id", a),
        ("UPDATE fixed_audio_requests SET source_text='illegal' WHERE paper_entry_id=:id", a),
        (
            "UPDATE task_revisions SET reference_answer='B' WHERE task_revision_id=:id",
            UUID(item["revision_id"]),
        ),
    ):
        with pytest.raises(DBAPIError), db.begin() as c:
            c.execute(text(statement), {"id": target})


def restart_engine(db):
    with db.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_engine(db.url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def set_schema(connection, record):
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT set_config('application_name', %s, false)", (schema,))
        connection.commit()

    return engine


def test_worker_competition_restart_and_corrupted_audio(context, db):
    client, bank, _, _, store = context
    _, result = import_workbook(client, bank, workbook(), "restart-import")
    paper_id, _, _, _, reg = draft_from_import(db, bank, result)
    with Session(db) as s, s.begin():
        publish(
            s, paper_id=paper_id, expected_version=5, registry=reg, audio_config=TEST_AUDIO_CONFIG
        )
    with Session(db) as s:
        audio_ids = list(
            s.scalars(
                select(FixedAudio.audio_id)
                .join(PaperEntry)
                .where(PaperEntry.paper_id == paper_id)
                .order_by(FixedAudio.audio_id)
            )
        )
    entered, release = threading.Event(), threading.Event()

    class BlockingSynth(Synth):
        def synthesize(self, source_text, config):
            entered.set()
            assert release.wait(10)
            return super().synthesize(source_text, config)

    synth = BlockingSynth()
    fresh = restart_engine(db)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run_audio, db, audio_ids[0], store, synth)
            assert entered.wait(10)
            second = executor.submit(run_audio, fresh, audio_ids[0], store, Synth())
            assert second.result(timeout=10) == "busy"
            release.set()
            assert first.result(timeout=10) == "ready"
        assert len(synth.calls) == 1

        class Interrupted:
            def synthesize(self, source_text, config):
                raise SystemExit("simulated interruption after provider accepted")

        with pytest.raises(SystemExit):
            run_audio(db, audio_ids[1], store, Interrupted())
        db.dispose()
        after_restart = Synth()
        assert run_audio(fresh, audio_ids[1], store, after_restart) == "unknown"
        assert run_audio(fresh, audio_ids[1], store, after_restart) == "unknown"
        assert after_restart.calls == []
        path = f"/teacher/papers/{paper_id}/audio/{audio_ids[1]}/retry"
        assert client.post(path, headers=AUTH, json={}).status_code == 409
        assert client.post(path, headers=AUTH, json={"accept_unknown": True}).status_code == 202
        assert run_audio(fresh, audio_ids[1], store, after_restart) == "ready"
        assert len(after_restart.calls) == 1

        failing_synth = Synth()

        def fail_binding(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE fixed_audio_requests"):
                raise RuntimeError("simulated commit interruption")

        event.listen(fresh, "before_cursor_execute", fail_binding)
        try:
            with pytest.raises(RuntimeError, match="simulated commit interruption"):
                run_audio(fresh, audio_ids[2], store, failing_synth)
        finally:
            event.remove(fresh, "before_cursor_execute", fail_binding)
        with Session(fresh) as s:
            assert (
                s.scalar(select(TTSAttempt.state).where(TTSAttempt.audio_id == audio_ids[2]))
                == "SAVED"
            )
        fresh.dispose()
        resumed = restart_engine(db)
        try:
            never_called = Synth()
            assert run_audio(resumed, audio_ids[2], store, never_called) == "ready"
            assert len(failing_synth.calls) == 1 and never_called.calls == []
            with Session(resumed) as s:
                assert ready_for_new_session(s, paper_id, store)
                row = s.get(FixedAudio, audio_ids[0])
                audio_path = store.root / row.storage_ref
                audio_path.write_bytes(b"corrupted")
            url = f"/teacher/papers/{paper_id}/audio/{audio_ids[0]}/content"
            assert client.get(url, headers=AUTH).status_code == 409
            with Session(resumed) as s:
                assert not ready_for_new_session(s, paper_id, store)
            assert run_audio(resumed, audio_ids[0], store, never_called) == "invalid_resource"
            assert audio_path.read_bytes() == b"corrupted" and never_called.calls == []
            audio_path.unlink()
            assert client.get(url, headers=AUTH).status_code == 409
            with Session(resumed) as s:
                assert not ready_for_new_session(s, paper_id, store)
        finally:
            resumed.dispose()
    finally:
        release.set()
        fresh.dispose()


def test_imported_draft_edit_and_publication_serialize(context, db):
    client, bank, _, _, _ = context
    _, result = import_workbook(client, bank, workbook(), "race-import")
    paper_id, _, _, items, reg = draft_from_import(db, bank, result)
    barrier = threading.Barrier(3, timeout=10)

    def worker(kind):
        barrier.wait()
        try:
            with Session(db) as s, s.begin():
                if kind == "publish":
                    publish(
                        s,
                        paper_id=paper_id,
                        expected_version=5,
                        registry=reg,
                        audio_config=TEST_AUDIO_CONFIG,
                    )
                else:
                    add_entry(
                        s,
                        paper_id=paper_id,
                        expected_version=5,
                        task_id=UUID(items["0003"]["task_id"]),
                        position=3,
                    )
            return "ok"
        except (PaperEditConflict, ValueError):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            blocker.execute(
                select(Paper.paper_id).where(Paper.paper_id == paper_id).with_for_update()
            )
            futures = [executor.submit(worker, kind) for kind in ("publish", "edit")]
            barrier.wait()
            deadline = time.monotonic() + 10
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:schema AND cardinality(pg_blocking_pids(pid))>0"
                        ),
                        {"schema": schema},
                    )
                    if blocked >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
                assert blocked >= 2
        assert sorted(f.result(timeout=10) for f in futures) == ["conflict", "ok"]
    with Session(db) as s:
        paper = s.get(Paper, paper_id)
        entries = s.scalars(select(PaperEntry).where(PaperEntry.paper_id == paper_id)).all()
        plan_count = len(
            s.scalars(
                select(EvaluationPlan).join(PaperEntry).where(PaperEntry.paper_id == paper_id)
            ).all()
        )
        audio_count = len(
            s.scalars(
                select(FixedAudio).join(PaperEntry).where(PaperEntry.paper_id == paper_id)
            ).all()
        )
        assert (paper.status, len(entries), plan_count, audio_count) in [
            ("DRAFT", 3, 0, 0),
            ("PUBLISHED", 2, 2, 3),
        ]
