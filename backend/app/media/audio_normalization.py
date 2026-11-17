"""Prepare contract-specific audio without changing the authoritative recording."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from app.media.audio_inspection import (
    AudioError,
    AudioInfo,
    AudioTools,
    inspect_audio,
    verified_copy,
)
from app.media.recording_store import RecordingStore


@dataclass(frozen=True)
class AudioSpec:
    container: str
    codec: str
    sample_rate: int
    channels: int
    processing_version: str
    ffmpeg_version: str
    ffprobe_version: str

    def __post_init__(self):
        if (
            self.container != "wav"
            or self.codec not in ("pcm_s16le", "pcm_s24le")
            or type(self.sample_rate) is not int
            or self.sample_rate <= 0
            or type(self.channels) is not int
            or self.channels <= 0
            or not self.processing_version
            or not self.ffmpeg_version
            or not self.ffprobe_version
        ):
            raise ValueError("Unsupported audio processing specification")


@dataclass(frozen=True)
class PreparedAudio:
    recording_id: str
    original_sha256: str
    declared_media_type: str
    original: AudioInfo
    spec: AudioSpec
    output: AudioInfo
    storage_ref: str
    reused_original: bool
    tool_version: str
    processing_id: str


def matches(info: AudioInfo, spec: AudioSpec) -> bool:
    return (
        spec.container in info.container.split(",")
        and info.codec == spec.codec
        and info.sample_rate == spec.sample_rate
        and info.channels == spec.channels
    )


def prepare_audio(
    recording: dict,
    spec: AudioSpec,
    original_store: RecordingStore,
    derived_store: RecordingStore,
    tools: AudioTools,
) -> PreparedAudio:
    """Run after claim, outside DB transactions; caller persists provenance in its result."""
    version = tools.version()
    if version != spec.ffmpeg_version or tools.version(probe=True) != spec.ffprobe_version:
        raise AudioError("AUDIO_TOOL_VERSION_MISMATCH")
    identity = {"original_sha256": recording["sha256"], "spec": asdict(spec)}
    processing_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    derived_store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".audio-", dir=derived_store.root) as tmp:
        root = Path(tmp)
        source = root / "original"
        verified_copy(
            original_store,
            recording["storage_ref"],
            recording["sha256"],
            source,
            tools.max_input_bytes,
        )
        original = inspect_audio(source, tools)
        if matches(original, spec):
            return PreparedAudio(
                recording["recording_id"],
                recording["sha256"],
                recording["media_type"],
                original,
                spec,
                original,
                recording["storage_ref"],
                True,
                version,
                processing_id,
            )
        bytes_per_sample = {"pcm_s16le": 2, "pcm_s24le": 3}[spec.codec]
        if (
            original.duration_seconds * spec.sample_rate * spec.channels * bytes_per_sample + 65536
            >= tools.max_decoded_bytes
        ):
            raise AudioError("AUDIO_RESOURCE_LIMIT")
        index_dir = derived_store.root / "processing"
        index_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if index_dir.is_symlink() or not index_dir.resolve().is_relative_to(
            derived_store.root.resolve()
        ):
            raise AudioError("AUDIO_STORAGE_INVALID")
        index = index_dir / processing_id
        if index.is_symlink():
            raise AudioError("AUDIO_STORAGE_INVALID")
        if index.is_file() and not index.is_symlink():
            digest = index.read_text(encoding="ascii")
            ref = f"sha256/{digest[:2]}/{digest}"
            cached = root / "cached"
            verified_copy(derived_store, ref, digest, cached, tools.max_decoded_bytes)
            info = inspect_audio(cached, tools)
            if not matches(info, spec):
                raise AudioError("AUDIO_OUTPUT_MISMATCH")
            return PreparedAudio(
                recording["recording_id"],
                recording["sha256"],
                recording["media_type"],
                original,
                spec,
                info,
                ref,
                False,
                version,
                processing_id,
            )
        converted = root / "converted.wav"
        result = tools.run(
            [
                tools.ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-threads",
                "1",
                "-protocol_whitelist",
                "pipe",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-acodec",
                spec.codec,
                "-ar",
                str(spec.sample_rate),
                "-ac",
                str(spec.channels),
                "-fs",
                str(tools.max_decoded_bytes),
                "-f",
                "wav",
                "-y",
                str(converted),
            ],
            source=source,
        )
        if result.returncode or not converted.is_file():
            raise AudioError("AUDIO_CONVERSION_FAILED")
        if converted.stat().st_size >= tools.max_decoded_bytes:
            raise AudioError("AUDIO_RESOURCE_LIMIT")
        info = inspect_audio(converted, tools)
        if not matches(info, spec):
            raise AudioError("AUDIO_OUTPUT_MISMATCH")
        ref = derived_store.commit(converted, info.sha256)
        marker = root / "marker"
        marker.write_text(info.sha256, encoding="ascii")
        try:
            os.link(marker, index)
        except FileExistsError:
            # Another completed conversion won. Its result is verified before reuse.
            digest = index.read_text(encoding="ascii")
            ref = f"sha256/{digest[:2]}/{digest}"
            cached = root / "cached"
            verified_copy(derived_store, ref, digest, cached, tools.max_decoded_bytes)
            info = inspect_audio(cached, tools)
            if not matches(info, spec):
                raise AudioError("AUDIO_OUTPUT_MISMATCH")
        return PreparedAudio(
            recording["recording_id"],
            recording["sha256"],
            recording["media_type"],
            original,
            spec,
            info,
            ref,
            False,
            version,
            processing_id,
        )
