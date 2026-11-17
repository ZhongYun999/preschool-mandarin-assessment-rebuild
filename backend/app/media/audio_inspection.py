"""Verify recording bytes and inspect audio by probing and fully decoding it."""

import hashlib
import json
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from app.media.recording_store import RecordingStore


class AudioError(Exception):
    """A technical audio failure, never an assessment result."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AudioTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout: float = 60
    max_input_bytes: int = 1024 * 1024 * 1024
    max_decoded_bytes: int = 1024 * 1024 * 1024

    def run(self, command, *, source=None, output=subprocess.PIPE):
        try:
            with source.open("rb") if source else nullcontext(None) as stdin:
                result = subprocess.run(
                    command,
                    stdin=stdin if source else subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout,
                    check=False,
                )
        except FileNotFoundError as exc:
            raise AudioError("AUDIO_TOOL_UNAVAILABLE") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError("AUDIO_TOOL_TIMEOUT") from exc
        except OSError as exc:
            raise AudioError("AUDIO_TOOL_FAILED") from exc
        return result

    def version(self, *, probe=False):
        result = self.run([self.ffprobe if probe else self.ffmpeg, "-version"])
        if result.returncode or not result.stdout:
            raise AudioError("AUDIO_TOOL_FAILED")
        return result.stdout.decode("utf-8", "replace").splitlines()[0]


@dataclass(frozen=True)
class AudioInfo:
    container: str
    codec: str
    sample_rate: int
    channels: int
    bits_per_sample: int | None
    duration_seconds: float
    container_duration_seconds: float | None
    byte_length: int
    sha256: str
    has_audio: bool = True


def verified_copy(store: RecordingStore, ref: str, digest: str, destination: Path, max_bytes: int):
    try:
        source = store.path(ref, digest)
    except ValueError as exc:
        raise AudioError("AUDIO_MISSING") from exc
    hasher = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AudioError("AUDIO_RESOURCE_LIMIT")
                hasher.update(chunk)
                dst.write(chunk)
    except FileNotFoundError as exc:
        raise AudioError("AUDIO_MISSING") from exc
    if hasher.hexdigest() != digest:
        raise AudioError("AUDIO_DIGEST_MISMATCH")


def inspect_audio(path: Path, tools: AudioTools) -> AudioInfo:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    size = path.stat().st_size
    if not size:
        raise AudioError("AUDIO_UNRECOGNIZED")
    probe = tools.run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "pipe",
            "-show_error",
            "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample:format=format_name,duration",
            "-of",
            "json",
            "pipe:0",
        ],
        source=path,
    )
    if probe.returncode:
        try:
            invalid_data = json.loads(probe.stdout).get("error", {}).get("code") == -1094995529
        except (ValueError, TypeError, AttributeError):
            invalid_data = False
        raise AudioError("AUDIO_UNRECOGNIZED" if invalid_data else "AUDIO_TOOL_FAILED")
    try:
        metadata = json.loads(probe.stdout)
        container = metadata["format"]["format_name"]
        streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise AudioError("AUDIO_NO_STREAM")
        stream = streams[0]
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        codec = stream["codec_name"]
        if rate <= 0 or channels <= 0 or not codec or not container:
            raise ValueError
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0) or None
        duration = metadata["format"].get("duration")
        container_duration = float(duration) if duration not in (None, "N/A") else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError("AUDIO_METADATA_INCOMPLETE") from exc
    with tempfile.TemporaryFile() as decoded:
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
                "pcm_s16le",
                "-fs",
                str(tools.max_decoded_bytes),
                "-f",
                "s16le",
                "pipe:1",
            ],
            source=path,
            output=decoded,
        )
        length = decoded.tell()
    if result.returncode:
        raise AudioError("AUDIO_DECODE_FAILED")
    if length >= tools.max_decoded_bytes:
        raise AudioError("AUDIO_RESOURCE_LIMIT")
    if not length or length % (channels * 2):
        raise AudioError("AUDIO_STREAM_CORRUPT")
    return AudioInfo(
        container,
        codec,
        rate,
        channels,
        bits,
        length / (rate * channels * 2),
        container_duration,
        size,
        digest.hexdigest(),
    )
