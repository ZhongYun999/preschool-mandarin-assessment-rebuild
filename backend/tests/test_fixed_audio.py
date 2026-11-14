import io
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_paper import draft, entries
from test_publication import TEST_AUDIO_CONFIG, choose, registry, setup_paper
from test_taskbank import CONTENT, write

from app.fixed_audio import AudioData, AudioStore, FixedAudio, prepare_audio, readiness, text_hash
from app.paper import Paper, PaperEntry, add_entry, copy_paper
from app.publication import publish
from app.tts_attempt import TTSAttempt
from app.tts_worker import authorize_retry, run_audio


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16)
    return output.getvalue()


class Synth:
    def __init__(self):
        self.calls = []
        self.fail = False

    def synthesize(self, source_text, config):
        self.calls.append((source_text, config.copy()))
        if self.fail:
            raise RuntimeError("private provider detail")
        return AudioData(wav(), "audio/wav")


def published(db, bank):
    paper_id, task, _, a, b = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    with Session(db) as s, s.begin():
        publish(
            s, paper_id=paper_id, expected_version=3, registry=reg, audio_config=TEST_AUDIO_CONFIG
        )
    return paper_id, task, a, b, reg


def test_freeze_readiness_generation_and_history(db, bank, tmp_path):
    paper_id, task, a, b, _ = published(db, bank)
    store, synth = AudioStore(tmp_path), Synth()
    with Session(db) as s:
        rows = s.scalars(select(FixedAudio).order_by(FixedAudio.role)).all()
        assert {(r.paper_entry_id, r.role) for r in rows} == {
            (a, "guide"),
            (a, "prompt"),
            (b, "prompt"),
        }
        assert all(r.config == TEST_AUDIO_CONFIG for r in rows)
        assert readiness(s, paper_id, store).reasons == ("audio_not_ready",)
    write(db, bank, replace(CONTENT, prompt_text="new question"))
    assert set(prepare_audio(db, paper_id, store, synth).values()) == {"ready"}
    assert len(synth.calls) == 3
    assert {text for text, _ in synth.calls} == {
        "guide",
        "full question? next?",
        CONTENT.prompt_text,
    }
    assert all(
        not any(secret in text for secret in ("secret M", "secret N", "secret O"))
        for text, _ in synth.calls
    )
    with Session(db) as s:
        assert readiness(s, paper_id, store).ready
        assert entries(s, paper_id)[0].task_revision_id == task.task_revision_id
        rows = s.scalars(select(FixedAudio)).all()
        assert len({r.storage_ref for r in rows}) == 1
        with wave.open(
            io.BytesIO(store.read(rows[0].storage_ref, rows[0].content_sha256))
        ) as audio:
            assert audio.getframerate() == 8000 and audio.getnframes() == 16
        ref = rows[0].storage_ref
    prepare_audio(db, paper_id, store, synth)
    assert len(synth.calls) == 3
    (tmp_path / ref).write_bytes(b"corrupt")
    with Session(db) as s:
        assert "audio_file_invalid" in readiness(s, paper_id, store).reasons
    assert set(prepare_audio(db, paper_id, store, synth).values()) == {"invalid_resource"}
    assert len(synth.calls) == 3
    (tmp_path / ref).unlink()
    with Session(db) as s:
        assert "audio_file_invalid" in readiness(s, paper_id, store).reasons


def test_failed_synthesis_recovers_and_partial_audio_not_ready(db, bank, tmp_path):
    paper_id, _, _, _, _ = published(db, bank)
    store, synth = AudioStore(tmp_path), Synth()
    synth.fail = True
    assert set(prepare_audio(db, paper_id, store, synth).values()) == {"unknown"}
    with Session(db) as s:
        assert all(r.error == "provider_unknown" for r in s.scalars(select(FixedAudio)))
        assert not readiness(s, paper_id, store).ready
    synth.fail = False
    # One request can succeed independently; the other requests still block readiness.
    with Session(db) as s:
        one = s.scalars(select(FixedAudio)).first().audio_id
    with Session(db) as s, s.begin():
        row = s.get(FixedAudio, one, with_for_update=True)
        row.storage_ref, row.content_sha256 = store.put(wav())
        row.media_type, row.status, row.error = "audio/wav", "READY", None
    with Session(db) as s:
        assert not readiness(s, paper_id, store).ready
    with Session(db) as s, s.begin():
        for row in s.scalars(select(FixedAudio).where(FixedAudio.status != "READY")):
            authorize_retry(s, row.audio_id, accept_unknown=True)
    assert set(prepare_audio(db, paper_id, store, synth).values()) == {"ready"}
    with Session(db) as s:
        assert readiness(s, paper_id, store).ready


def test_readiness_refreshes_cached_database_state(db, bank, tmp_path):
    paper_id, *_ = published(db, bank)
    store = AudioStore(tmp_path)
    with Session(db) as s:
        cached_paper = s.get(Paper, paper_id)
        cached_audio = s.scalars(select(FixedAudio)).all()
        assert not readiness(s, paper_id, store).ready
        prepare_audio(db, paper_id, store, Synth())
        assert readiness(s, paper_id, store).ready
        assert all(row.status == "READY" for row in cached_audio)
        with db.begin() as connection:
            connection.execute(
                text("UPDATE papers SET status='ARCHIVED' WHERE paper_id=:id"), {"id": paper_id}
            )
        assert "paper_not_published" in readiness(s, paper_id, store).reasons
        assert cached_paper.status == "ARCHIVED"


def test_copy_preserves_config_and_archive_is_not_ready(db, bank, tmp_path):
    paper_id, task, _, _, reg = published(db, bank)
    with Session(db) as s, s.begin():
        copied_id = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s:
        copied_entries = entries(s, copied_id)
        assert copied_entries[0].task_revision_id == task.task_revision_id
        requests = s.scalars(
            select(FixedAudio)
            .join_from(FixedAudio, PaperEntry)
            .where(PaperEntry.paper_id == copied_id)
        ).all()
        assert len(requests) == 3 and all(row.config == TEST_AUDIO_CONFIG for row in requests)
    different = {**TEST_AUDIO_CONFIG, "voice": "different"}
    with Session(db) as s, s.begin():
        publish(s, paper_id=copied_id, expected_version=0, registry=reg, audio_config=different)
    with Session(db) as s:
        assert all(row.config == TEST_AUDIO_CONFIG for row in s.scalars(select(FixedAudio)))
    with db.begin() as c:
        c.execute(text("UPDATE papers SET status='ARCHIVED' WHERE paper_id=:id"), {"id": paper_id})
    with Session(db) as s:
        assert "paper_not_published" in readiness(s, paper_id, AudioStore(tmp_path)).reasons


def test_missing_configuration_rolls_back_publication(db, bank):
    paper_id, *_ = setup_paper(db, bank)
    reg = registry()
    choose(db, paper_id, reg)
    with pytest.raises(ValueError, match="Explicit provider"), Session(db) as s, s.begin():
        publish(s, paper_id=paper_id, expected_version=3, registry=reg)
    with Session(db) as s:
        assert s.get(Paper, paper_id).status == "DRAFT"
        assert s.scalars(select(FixedAudio)).all() == []


def test_concurrent_prepare_does_not_duplicate_audio(db, bank, tmp_path):
    paper_id, _, _, _, _ = published(db, bank)
    started, release = threading.Event(), threading.Event()

    class BlockingSynth(Synth):
        def synthesize(self, source_text, config):
            if not started.is_set():
                started.set()
                assert release.wait(10)
            return super().synthesize(source_text, config)

    synth, store = BlockingSynth(), AudioStore(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(prepare_audio, db, paper_id, store, synth)
        assert started.wait(10)
        with db.connect() as observer:
            schema = observer.scalar(text("SELECT current_schema()"))
            assert observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid WHERE a.application_name=:schema AND l.locktype='advisory' AND l.granted AND a.state='idle' AND a.xact_start IS NULL)"
                ),
                {"schema": schema},
            )
        second = pool.submit(prepare_audio, db, paper_id, store, synth)
        second_result = second.result(timeout=10)
        release.set()
        results = first.result(timeout=10), second_result
    assert any("busy" in result.values() for result in results)
    assert len(synth.calls) == 3
    with Session(db) as s:
        assert readiness(s, paper_id, store).ready


def test_dialogue_complete_prompt_is_passed_to_synthesizer(db, bank, tmp_path):
    content = replace(
        CONTENT,
        task_type="dialogue",
        target_rounds=2,
        prompt_text="First question? Second question? 完整内容。",
    )
    task = write(db, bank, content)
    paper_id = draft(db, bank)
    reg = registry()
    with Session(db) as s, s.begin():
        add_entry(s, paper_id=paper_id, expected_version=0, task_id=task.task_id, position=1)
    choose(db, paper_id, reg, version=1)
    with Session(db) as s, s.begin():
        publish(
            s, paper_id=paper_id, expected_version=2, registry=reg, audio_config=TEST_AUDIO_CONFIG
        )
    synth = Synth()
    prepare_audio(db, paper_id, AudioStore(tmp_path), synth)
    assert [item[0] for item in synth.calls] == [content.prompt_text]


def test_database_failure_after_file_write_does_not_commit_reference(db, bank, tmp_path):
    paper_id, *_ = published(db, bank)
    store = AudioStore(tmp_path)
    synth = Synth()

    def fail_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE fixed_audio_requests"):
            raise RuntimeError("test database failure")

    event.listen(db, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="test database failure"):
            prepare_audio(db, paper_id, store, synth)
    finally:
        event.remove(db, "before_cursor_execute", fail_update)
    with Session(db) as s:
        assert all(
            row.status == "PENDING" and row.storage_ref is None
            for row in s.scalars(select(FixedAudio))
        )
    assert any(item.is_file() for item in tmp_path.rglob("*"))
    with Session(db) as s:
        attempt = s.scalar(select(TTSAttempt))
        assert attempt.state == "SAVED"
        audio_id = attempt.audio_id
    assert run_audio(db, audio_id, store, synth) == "ready"
    assert len(synth.calls) == 1


def test_immutable_contract_and_downgrade_protection(db, bank, tmp_path):
    paper_id, _, a, _, _ = published(db, bank)
    store = AudioStore(tmp_path)
    prepare_audio(db, paper_id, store, Synth())
    with Session(db) as s:
        audio = s.scalar(select(FixedAudio).where(FixedAudio.paper_entry_id == a))
        audio_id, ref = audio.audio_id, audio.storage_ref
    for sql in (
        "UPDATE fixed_audio_requests SET source_text='bad' WHERE audio_id=:id",
        "UPDATE fixed_audio_requests SET config='{}' WHERE audio_id=:id",
        "UPDATE fixed_audio_requests SET storage_ref='bad' WHERE audio_id=:id",
        "DELETE FROM fixed_audio_requests WHERE audio_id=:id",
    ):
        with pytest.raises(DBAPIError), db.begin() as c:
            c.execute(text(sql), {"id": audio_id})
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0005_publication_contract")
        c.rollback()
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0007_tts_attempts"
        assert (
            c.scalar(
                text("SELECT storage_ref FROM fixed_audio_requests WHERE audio_id=:id"),
                {"id": audio_id},
            )
            == ref
        )


def test_downgrade_refuses_copied_draft_audio_without_published_paper(db, bank):
    (
        paper_id,
        task,
        _,
        entry_id,
        _,
    ) = setup_paper(db, bank)
    with Session(db) as s, s.begin():
        s.add(
            FixedAudio(
                paper_entry_id=entry_id,
                task_revision_id=task.task_revision_id,
                role="prompt",
                source_text="full question? next?",
                text_sha256=text_hash("full question? next?"),
                config=TEST_AUDIO_CONFIG,
                status="PENDING",
            )
        )
        s.flush()
        copied_id = copy_paper(s, source_id=paper_id).paper_id
        copied_entry = entries(s, copied_id)[0].paper_entry_id
        assert (
            s.scalar(select(FixedAudio).where(FixedAudio.paper_entry_id == copied_entry))
            is not None
        )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0005_publication_contract")
        c.rollback()
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0007_tts_attempts"
        assert c.scalar(text("SELECT count(*) FROM fixed_audio_requests")) == 2
