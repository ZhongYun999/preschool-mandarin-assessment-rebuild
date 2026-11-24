"""Explicit, durable Speaking guidance synthesis; never retries an uncertain call."""

import argparse
import hashlib
import io
import json
import wave
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.media.dashscope_tts import DashScopeSynthesizer, TTSError
from app.media.fixed_audio import AudioStore, validate_config
from app.task_types.speaking_guidance import GuidanceAudio
from app.task_types.speaking_guidance_worker import _locks


def run_audio(engine, audio_id, store, synthesizer, config):
    config = validate_config(config)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        schema = lock.scalar(text("SELECT current_schema()"))
        key = int.from_bytes(
            hashlib.sha256(f"guidance:{schema}:{audio_id}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
            return "busy"
        try:
            with Session(engine) as db, db.begin():
                audio = db.get(GuidanceAudio, audio_id)
                if audio is None:
                    return "not_found"
                locked = _locks(db, audio.help_request_id)
                if locked is None:
                    return "not_found"
                owner, task, attempt, help_request = locked
                audio = db.scalar(
                    select(GuidanceAudio)
                    .where(GuidanceAudio.audio_id == audio_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if audio.status in ("READY", "SAVED"):
                    try:
                        store.read(audio.storage_ref, audio.content_sha256)
                    except (ValueError, OSError):
                        return "invalid_resource"
                    if audio.status == "SAVED":
                        audio.status = "READY"
                    return "ready"
                if audio.status == "SENDING":
                    audio.status, audio.error_code = "UNKNOWN", "interrupted_or_unknown"
                    audio.history = [
                        *audio.history,
                        {
                            "execution_id": str(audio.execution_id),
                            "status": "UNKNOWN",
                            "error_code": audio.error_code,
                        },
                    ]
                    return "unknown"
                if audio.status != "WAITING":
                    return audio.status.lower()
                if (
                    owner.status != "IN_PROGRESS"
                    or task.current_attempt_id != attempt.attempt_id
                    or attempt.status != "OPEN"
                    or help_request.status != "READY"
                ):
                    return "not_current"
                source = help_request.guidance_text
                if (
                    not source
                    or audio.attempt_id != attempt.attempt_id
                    or hashlib.sha256(source.encode()).hexdigest() != audio.text_sha256
                ):
                    raise ValueError("Guidance audio source mismatch")
                if audio.config is not None and audio.config != config:
                    raise ValueError("TTS configuration conflicts with frozen operation")
                try:
                    if hasattr(synthesizer, "validate"):
                        synthesizer.validate(config)
                except TTSError as exc:
                    audio.status, audio.error_code = "FAILED", exc.category
                    audio.history = [
                        *audio.history,
                        {"status": "FAILED", "error_code": exc.category},
                    ]
                    return "failed"
                execution_id = uuid4()
                audio.config, audio.status, audio.execution_id = config, "SENDING", execution_id
                audio.attempts += 1
                audio.error_code = None
            try:
                result = synthesizer.synthesize(source, config)
                with wave.open(io.BytesIO(result.data), "rb") as wav:
                    frames = wav.getnframes()
                    if (
                        result.media_type != "audio/wav"
                        or wav.getframerate() != config["sample_rate"]
                        or frames <= 0
                        or len(wav.readframes(frames))
                        != frames * wav.getnchannels() * wav.getsampwidth()
                    ):
                        raise TTSError("invalid_audio", uncertain=True)
                ref, digest = store.put(result.data)
            except Exception as exc:  # noqa: BLE001 - never persist provider exceptions
                code = exc.category if isinstance(exc, TTSError) else "external_or_storage_unknown"
                uncertain = exc.uncertain if isinstance(exc, TTSError) else True
                with Session(engine) as db, db.begin():
                    audio = db.get(GuidanceAudio, audio_id, with_for_update=True)
                    if audio.status == "SENDING" and audio.execution_id == execution_id:
                        audio.status, audio.error_code = (
                            ("UNKNOWN" if uncertain else "FAILED"),
                            code,
                        )
                        audio.history = [
                            *audio.history,
                            {
                                "execution_id": str(execution_id),
                                "status": audio.status,
                                "error_code": code,
                            },
                        ]
                return "unknown" if uncertain else "failed"
            with Session(engine) as db, db.begin():
                audio = db.get(GuidanceAudio, audio_id, with_for_update=True)
                if audio.status != "SENDING" or audio.execution_id != execution_id:
                    return "stale"
                audio.status, audio.storage_ref, audio.content_sha256, audio.media_type = (
                    "SAVED",
                    ref,
                    digest,
                    result.media_type,
                )
                audio.history = [
                    *audio.history,
                    {
                        "execution_id": str(execution_id),
                        "status": "SAVED",
                        "content_sha256": digest,
                    },
                ]
            with Session(engine) as db, db.begin():
                audio = db.get(GuidanceAudio, audio_id, with_for_update=True)
                store.read(audio.storage_ref, audio.content_sha256)
                audio.status = "READY"
            return "ready"
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_id", type=UUID)
    parser.add_argument("--config-json", required=True, help="Non-secret TTS configuration JSON")
    args = parser.parse_args()
    engine = create_database_engine()
    try:
        print(
            run_audio(
                engine,
                args.audio_id,
                AudioStore(),
                DashScopeSynthesizer(),
                json.loads(args.config_json),
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
