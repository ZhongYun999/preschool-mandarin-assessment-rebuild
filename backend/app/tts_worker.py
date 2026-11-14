"""Durable one-pass TTS worker. Ambiguous calls never auto-retry."""

import hashlib
import time
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.dashscope_tts import DashScopeSynthesizer, TTSError
from app.fixed_audio import AudioStore, FixedAudio
from app.paper import Paper, PaperEntry
from app.tts_attempt import TTSAttempt


def _latest(session, audio_id):
    return session.scalar(
        select(TTSAttempt)
        .where(TTSAttempt.audio_id == audio_id)
        .order_by(TTSAttempt.created_at.desc(), TTSAttempt.attempt_id.desc())
        .limit(1)
    )


def authorize_retry(session: Session, audio_id: UUID, *, accept_unknown: bool = False) -> None:
    row = session.scalar(
        select(FixedAudio).where(FixedAudio.audio_id == audio_id).with_for_update()
    )
    if row is None or row.status == "READY":
        raise ValueError("Audio cannot be retried")
    attempt = _latest(session, audio_id)
    if attempt is None or attempt.state not in {"FAILED", "UNKNOWN"}:
        raise ValueError("No failed attempt to retry")
    if attempt.state == "UNKNOWN" and not accept_unknown:
        raise ValueError("Uncertain billing requires explicit acknowledgement")
    attempt.retry_authorized = True
    session.flush()


def run_audio(engine: Engine, audio_id: UUID, store: AudioStore, synthesizer) -> str:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        schema = lock.scalar(text("SELECT current_schema()"))
        key = int.from_bytes(
            hashlib.sha256(f"fixed-audio:{schema}:{audio_id}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
            return "busy"
        try:
            with Session(engine) as session, session.begin():
                row = session.scalar(
                    select(FixedAudio)
                    .join(PaperEntry)
                    .join(Paper)
                    .where(FixedAudio.audio_id == audio_id, Paper.status == "PUBLISHED")
                    .with_for_update(of=FixedAudio)
                )
                if row is None:
                    return "not_published"
                if row.status == "READY":
                    try:
                        store.read(row.storage_ref, row.content_sha256)
                        return "ready"
                    except (ValueError, OSError):
                        return "invalid_resource"
                attempt = _latest(session, audio_id)
                if attempt and attempt.state == "SENDING":
                    attempt.state, attempt.error_category = "UNKNOWN", "interrupted_or_unknown"
                    row.status, row.error = "FAILED", "interrupted_or_unknown"
                    return "unknown"
                if attempt and attempt.state == "SAVED":
                    try:
                        store.read(attempt.storage_ref, attempt.content_sha256)
                    except (ValueError, OSError):
                        attempt.state, attempt.error_category = "UNKNOWN", "saved_file_invalid"
                        row.status, row.error = "FAILED", "saved_file_invalid"
                        return "unknown"
                    row.status, row.storage_ref, row.content_sha256, row.media_type, row.error = (
                        "READY",
                        attempt.storage_ref,
                        attempt.content_sha256,
                        attempt.media_type,
                        None,
                    )
                    attempt.state = "DONE"
                    return "ready"
                if attempt and attempt.state in {"FAILED", "UNKNOWN"}:
                    if not attempt.retry_authorized:
                        return "unknown" if attempt.state == "UNKNOWN" else "failed"
                    attempt.retry_authorized = False
                elif attempt is None and row.status == "FAILED":
                    session.add(
                        TTSAttempt(
                            audio_id=audio_id,
                            state="UNKNOWN",
                            error_category="legacy_result_unknown",
                        )
                    )
                    return "unknown"
                source, config = row.source_text, dict(row.config)
                try:
                    if hasattr(synthesizer, "validate"):
                        synthesizer.validate(config)
                except TTSError as exc:
                    session.add(
                        TTSAttempt(audio_id=audio_id, state="FAILED", error_category=exc.category)
                    )
                    row.status, row.error = "FAILED", exc.category
                    return "failed"
                attempt_id = uuid4()
                session.add(TTSAttempt(attempt_id=attempt_id, audio_id=audio_id, state="SENDING"))
            try:
                audio = synthesizer.synthesize(source, config)
                if (
                    not isinstance(audio.data, bytes)
                    or not audio.data
                    or not isinstance(audio.media_type, str)
                    or not audio.media_type.startswith("audio/")
                ):
                    raise TTSError("invalid_audio", uncertain=True)
            except Exception as exc:  # noqa: BLE001 - classify only, never persist exception details
                category = exc.category if isinstance(exc, TTSError) else "provider_unknown"
                uncertain = exc.uncertain if isinstance(exc, TTSError) else True
                with Session(engine) as session, session.begin():
                    attempt = session.get(TTSAttempt, attempt_id, with_for_update=True)
                    row = session.get(FixedAudio, audio_id, with_for_update=True)
                    attempt.state, attempt.error_category = (
                        ("UNKNOWN" if uncertain else "FAILED"),
                        category,
                    )
                    row.status, row.error = "FAILED", category
                return "unknown" if uncertain else "failed"
            try:
                ref, digest = store.put(audio.data)
            except (OSError, ValueError):
                with Session(engine) as session, session.begin():
                    attempt = session.get(TTSAttempt, attempt_id, with_for_update=True)
                    row = session.get(FixedAudio, audio_id, with_for_update=True)
                    attempt.state, attempt.error_category = (
                        "UNKNOWN",
                        "storage_failed_after_success",
                    )
                    row.status, row.error = "FAILED", "storage_failed_after_success"
                return "unknown"
            with Session(engine) as session, session.begin():
                attempt = session.get(TTSAttempt, attempt_id, with_for_update=True)
                attempt.state, attempt.storage_ref, attempt.content_sha256, attempt.media_type = (
                    "SAVED",
                    ref,
                    digest,
                    audio.media_type,
                )
            with Session(engine) as session, session.begin():
                attempt = session.get(TTSAttempt, attempt_id, with_for_update=True)
                row = session.get(FixedAudio, audio_id, with_for_update=True)
                row.status, row.storage_ref, row.content_sha256, row.media_type, row.error = (
                    "READY",
                    ref,
                    digest,
                    audio.media_type,
                    None,
                )
                attempt.state = "DONE"
            return "ready"
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def run_once(
    engine: Engine, store: AudioStore, synthesizer, paper_id: UUID | None = None
) -> dict[str, str]:
    with Session(engine) as session:
        query = (
            select(FixedAudio.audio_id)
            .join(PaperEntry)
            .join(Paper)
            .where(Paper.status == "PUBLISHED")
        )
        if paper_id is not None:
            query = query.where(Paper.paper_id == paper_id)
        ids = session.scalars(query).all()
    return {str(audio_id): run_audio(engine, audio_id, store, synthesizer) for audio_id in ids}


def main() -> None:
    from app.database import create_database_engine

    engine = create_database_engine()
    try:
        while True:
            run_once(engine, AudioStore(), DashScopeSynthesizer())
            time.sleep(5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
