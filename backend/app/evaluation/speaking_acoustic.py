"""Local Speaking energy measurements using the shared evaluation worker."""

import hashlib
import io
import math
import struct
import tempfile
import wave
from datetime import timedelta

from sqlalchemy.orm import Session

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.source_identity import speaking_source
from app.evaluation.speaking_acoustic_contract import (
    ALGORITHM,
    SCHEMA,
    SOURCE,
    VERSION,
    valid_parameters,
)
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    _lock_job_context,
    _skip,
    claim_job,
    execute_claim,
)
from app.media.audio_inspection import AudioError
from app.media.audio_normalization import AudioSpec, prepare_audio
from app.papers.paper import EvaluationPlan, PaperEntry
from app.scoring.registry import PRODUCTION_REGISTRY
from app.taskbank.models import TaskRevision


def measure(data, params):
    """Frame RMS is measured on signed PCM; edge gaps belong to their segment."""
    try:
        stream = io.BytesIO(data) if isinstance(data, bytes) else data
        with wave.open(stream, "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
                1,
                2,
                16000,
                "NONE",
            ):
                raise ValueError("Audio format mismatch")
            frames = wav.getnframes()
            if not frames:
                raise ValueError("Empty PCM")
            window = params["frame_samples"]
            limit = params["active_rms_ratio"] * 32768
            count = active = active_squares = gap = gaps = gap_total = 0
            while count < frames:
                n = min(window, frames - count)
                raw = wav.readframes(n)
                if len(raw) != n * 2:
                    raise ValueError("Truncated PCM")
                squares = sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", raw))
                if math.sqrt(squares / n) >= limit:
                    if gap >= params["long_gap_samples"]:
                        gaps += 1
                        gap_total += gap
                    gap = 0
                    active += n
                    active_squares += squares
                else:
                    gap += n
                count += n
    except (wave.Error, EOFError, ValueError) as exc:
        raise KnownFailure("ACOUSTIC_AUDIO_INVALID") from exc
    if gap >= params["long_gap_samples"]:
        gaps += 1
        gap_total += gap
    rate = 16000
    return {
        "audio_duration_seconds": frames / rate,
        "energy_active_duration_seconds": active / rate,
        "energy_active_ratio": active / frames,
        "mean_active_rms_dbfs": 20 * math.log10(math.sqrt(active_squares / active) / 32768)
        if active
        else None,
        "long_low_energy_gap_count": gaps,
        "long_low_energy_gap_duration_seconds": gap_total / rate,
    }


def aggregate(segments):
    duration = sum(s["audio_duration_seconds"] for s in segments)
    active = sum(s["energy_active_duration_seconds"] for s in segments)
    return {
        "segment_count": len(segments),
        "total_audio_duration_seconds": duration,
        "total_energy_active_duration_seconds": active,
        "overall_energy_active_ratio": active / duration,
        "total_long_low_energy_gap_count": sum(s["long_low_energy_gap_count"] for s in segments),
        "total_long_low_energy_gap_duration_seconds": sum(
            s["long_low_energy_gap_duration_seconds"] for s in segments
        ),
    }


class AcousticAdapter:
    def __init__(self, source, consumption, original_store, derived_store):
        self.source, self.consumption = source, consumption
        self.original_store, self.derived_store = original_store, derived_store

    def resolve_inputs(self, session, submission, revision, requirement):
        if str(submission.submission_id) != self.source["submission_id"]:
            raise InputIdentityConflict("Acoustic submission changed")
        return InputSnapshot(
            {"speaking_consumption": self.consumption},
            {
                "audio_inputs": self.consumption["audio_inputs"],
                "submission_id": self.source["submission_id"],
                "source_sha256": self.source["sha256"],
            },
        )

    def evaluate(self, inputs, requirement):
        params = requirement["parameters"]
        if not valid_parameters(params):
            raise KnownFailure("ACOUSTIC_CONTRACT_UNAVAILABLE")
        results = []
        for item, original in zip(
            inputs.payload["audio_inputs"], self.source["segments"], strict=True
        ):
            store = (
                self.original_store
                if (
                    item["storage_ref"] == original["storage_ref"]
                    and item["sha256"] == original["sha256"]
                )
                else self.derived_store
            )
            try:
                path = store.path(item["storage_ref"], item["sha256"])
                with path.open("rb") as source, tempfile.TemporaryFile() as verified:
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        verified.write(chunk)
                    if digest.hexdigest() != item["sha256"]:
                        raise ValueError("Audio digest mismatch")
                    verified.seek(0)
                    metrics = measure(verified, params)
            except (OSError, ValueError):
                raise KnownFailure("ACOUSTIC_AUDIO_UNAVAILABLE") from None
            results.append(
                {
                    "segment_id": item["segment_id"],
                    "recording_id": item["recording_id"],
                    "position": item["position"],
                    "original_sha256": original["sha256"],
                    "input_sha256": item["sha256"],
                    "processing_version": item["processing_version"],
                    **metrics,
                }
            )
        return AdapterResult(
            payload={
                "schema": SCHEMA,
                "submission_id": inputs.payload["submission_id"],
                "speaking_source_sha256": inputs.payload["source_sha256"],
                "contract": {"source": SOURCE, "version": VERSION},
                "algorithm": ALGORITHM,
                "audio_spec": params["audio_spec"],
                "segments": results,
                "aggregate": aggregate(results),
            },
            provider="local",
            model=ALGORITHM,
            engine_version=ALGORITHM,
        )

    def validate_result(self, result, requirement):
        try:
            payload = result.payload
            segments = payload["segments"]
            items = self.consumption["audio_inputs"]
            if (result.provider, result.model, result.engine_version) != (
                "local",
                ALGORITHM,
                ALGORITHM,
            ):
                return False
            if (
                set(payload)
                != {
                    "schema",
                    "submission_id",
                    "speaking_source_sha256",
                    "contract",
                    "algorithm",
                    "audio_spec",
                    "segments",
                    "aggregate",
                }
                or payload["schema"] != SCHEMA
                or payload["algorithm"] != ALGORITHM
                or payload["submission_id"] != self.source["submission_id"]
                or payload["speaking_source_sha256"] != self.source["sha256"]
                or payload["contract"] != {"source": SOURCE, "version": VERSION}
                or payload["audio_spec"] != requirement["parameters"]["audio_spec"]
                or len(segments) != len(items)
            ):
                return False
            keys = {
                "segment_id",
                "recording_id",
                "position",
                "original_sha256",
                "input_sha256",
                "processing_version",
                "audio_duration_seconds",
                "energy_active_duration_seconds",
                "energy_active_ratio",
                "mean_active_rms_dbfs",
                "long_low_energy_gap_count",
                "long_low_energy_gap_duration_seconds",
            }
            for segment, item, original in zip(
                segments, items, self.source["segments"], strict=True
            ):
                if (
                    set(segment) != keys
                    or any(
                        segment[k] != item[k]
                        for k in ("segment_id", "recording_id", "position", "processing_version")
                    )
                    or segment["original_sha256"] != original["sha256"]
                    or segment["input_sha256"] != item["sha256"]
                ):
                    return False
                duration, active = (
                    segment["audio_duration_seconds"],
                    segment["energy_active_duration_seconds"],
                )
                ratio, rms = segment["energy_active_ratio"], segment["mean_active_rms_dbfs"]
                gap, count = (
                    segment["long_low_energy_gap_duration_seconds"],
                    segment["long_low_energy_gap_count"],
                )
                if (
                    any(
                        type(x) not in (int, float) or not math.isfinite(x)
                        for x in (duration, active, ratio, gap)
                    )
                    or duration <= 0
                    or not 0 <= active <= duration
                    or not math.isclose(ratio, active / duration, rel_tol=1e-12, abs_tol=1e-12)
                    or not 0 <= gap <= duration - active + 1e-12
                    or type(count) is not int
                    or count < 0
                    or (rms is None) != (active == 0)
                    or (
                        rms is not None
                        and (type(rms) not in (int, float) or not math.isfinite(rms))
                    )
                ):
                    return False
            return payload["aggregate"] == aggregate(segments)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return False


def prepare_adapter(engine, job_id, original_store, derived_store, tools):
    """Read immutable identities under lock; prepare actual bytes outside the transaction."""
    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return None
        job, submission, current = context
        if not current:
            if job.status == "WAITING":
                _skip(job)
            return None
        if job.status != "WAITING":
            return None
        requirement = job.requirement
        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        plan = session.get(EvaluationPlan, entry.paper_entry_id)
        if (
            (requirement.get("source"), requirement.get("version")) != (SOURCE, VERSION)
            or not valid_parameters(requirement.get("parameters"))
            or plan is None
            or plan.task_revision_id != revision.task_revision_id
            or job.requirement_index < 0
            or job.requirement_index >= len(plan.requirements)
            or plan.requirements[job.requirement_index] != requirement
        ):
            raise InputIdentityConflict("Invalid acoustic plan")
        source = speaking_source(session, submission, entry, revision)
        if job.input_refs is not None and job.input_refs.get("speaking_source") != source:
            raise InputIdentityConflict("Acoustic source changed")
        previous = job.input_refs
    if previous is not None:
        try:
            for item in previous["dependencies"]["speaking_consumption"]["derived_recordings"]:
                derived_store.verify(item["storage_ref"], item["sha256"])
        except (KeyError, TypeError, ValueError, OSError):
            raise InputIdentityConflict("Frozen derived audio unavailable") from None
    spec = AudioSpec(**requirement["parameters"]["audio_spec"])
    audio, derived = [], []
    for segment in source["segments"]:
        prepared = prepare_audio(segment, spec, original_store, derived_store, tools)
        item = {
            "segment_id": segment["segment_id"],
            "recording_id": segment["recording_id"],
            "position": segment["position"],
            "sha256": prepared.output.sha256,
            "storage_ref": prepared.storage_ref,
            "media_type": "audio/wav",
            "processing_version": spec.processing_version,
        }
        audio.append(item)
        if not prepared.reused_original:
            derived.append({key: item[key] for key in ("storage_ref", "sha256", "media_type")})
    return AcousticAdapter(
        source,
        {
            "segment_ids": [x["segment_id"] for x in audio],
            "audio_inputs": audio,
            "derived_recordings": derived,
        },
        original_store,
        derived_store,
    )


def run_job(engine, job_id, *, original_store, derived_store, tools, lease=timedelta(minutes=5)):
    """Run a declared job; preparation failures remain technical, never a zero score."""
    try:
        adapter = prepare_adapter(engine, job_id, original_store, derived_store, tools)
    except AudioError as exc:
        with Session(engine) as session, session.begin():
            context = _lock_job_context(session, job_id)
            if context and context[2] and context[0].status == "WAITING":
                context[0].error_code = exc.code
        return False
    if adapter is None:
        return False
    claim = claim_job(
        engine,
        job_id,
        registry=PRODUCTION_REGISTRY,
        adapters={(SOURCE, VERSION): adapter},
        lease=lease,
        recording_store=original_store,
        derived_recording_store=derived_store,
    )
    return execute_claim(engine, claim) if claim else False
