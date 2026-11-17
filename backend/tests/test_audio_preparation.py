"""Audio inspection and normalization with real FFmpeg codecs."""

import hashlib
import io
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.media.audio_inspection import AudioError, AudioTools
from app.media.audio_normalization import AudioSpec, prepare_audio
from app.media.recording_store import RecordingStore


def make_wav(rate=16000, channels=1, width=2):
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\0" * (rate // 10) * channels * width)
    return data.getvalue()


@pytest.fixture
def audio(tmp_path):
    original = RecordingStore(tmp_path / "original")
    derived = RecordingStore(tmp_path / "derived")
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "test-v1", tools.version(), tools.version(probe=True)
    )

    def save(data, mime="audio/wav"):
        digest = hashlib.sha256(data).hexdigest()
        f, p = original.staging_file()
        with f:
            f.write(data)
        try:
            ref = original.commit(p, digest)
        finally:
            p.unlink()
        return {
            "recording_id": "recording-1",
            "sha256": digest,
            "media_type": mime,
            "storage_ref": ref,
        }

    return original, derived, tools, spec, save


@pytest.mark.parametrize(
    "rate,channels,width", [(16000, 1, 2), (8000, 1, 2), (16000, 2, 2), (16000, 1, 3)]
)
def test_inspection_and_conversion(audio, rate, channels, width):
    original, derived, tools, spec, save = audio
    data = make_wav(rate, channels, width)
    recording = save(data)
    prepared = prepare_audio(recording, spec, original, derived, tools)
    assert (
        prepared.original.container,
        prepared.original.sample_rate,
        prepared.original.channels,
    ) == ("wav", rate, channels)
    assert prepared.original.bits_per_sample == width * 8
    assert prepared.original.duration_seconds == pytest.approx(0.1)
    assert (prepared.output.codec, prepared.output.sample_rate, prepared.output.channels) == (
        "pcm_s16le",
        16000,
        1,
    )
    assert prepared.reused_original == (rate == 16000 and channels == 1 and width == 2)
    assert original.read(recording["storage_ref"], recording["sha256"]) == data
    second = prepare_audio(recording, spec, original, derived, tools)
    assert second == prepared
    assert prepared.original.byte_length == len(data)


def test_real_mp3_and_mismatched_mime(audio, tmp_path):
    original, derived, tools, spec, save = audio
    mp3 = tmp_path / "real.mp3"
    subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            "-c:a",
            "libmp3lame",
            str(mp3),
        ],
        check=True,
    )
    recording = save(mp3.read_bytes(), "audio/wav")
    result = prepare_audio(recording, spec, original, derived, tools)
    assert result.declared_media_type == "audio/wav"
    assert result.original.codec == "mp3" and result.output.codec == "pcm_s16le"
    assert not result.reused_original


def test_different_specs_and_concurrent_reuse(audio):
    original, derived, tools, spec, save = audio
    recording = save(make_wav(rate=8000, channels=2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: prepare_audio(recording, spec, original, derived, tools), range(4))
        )
    assert len({r.output.sha256 for r in results}) == 1
    assert len({r.processing_id for r in results}) == 1
    other = prepare_audio(recording, replace(spec, sample_rate=24000), original, derived, tools)
    assert other.processing_id != results[0].processing_id
    assert other.output.sample_rate == 24000
    derived.verify(results[0].storage_ref, results[0].output.sha256)


@pytest.mark.parametrize(
    "data", [b"", b"not audio", b"RIFF\x00\x00\x00\x00WAVEgarbage", make_wav()[:100]]
)
def test_bad_audio_fails_without_derivative(audio, data):
    original, derived, tools, spec, save = audio
    recording = save(data)
    with pytest.raises(AudioError) as error:
        prepare_audio(recording, spec, original, derived, tools)
    assert error.value.code in {"AUDIO_UNRECOGNIZED", "AUDIO_DECODE_FAILED", "AUDIO_STREAM_CORRUPT"}
    assert not list((derived.root / "sha256").glob("**/*"))


def test_missing_digest_and_tool_errors(audio, tmp_path):
    original, derived, tools, spec, save = audio
    recording = save(make_wav())
    path = original.path(recording["storage_ref"], recording["sha256"])
    path.write_bytes(b"corrupt")
    with pytest.raises(AudioError, match="AUDIO_DIGEST_MISMATCH"):
        prepare_audio(recording, spec, original, derived, tools)
    path.unlink()
    with pytest.raises(AudioError, match="AUDIO_MISSING"):
        prepare_audio(recording, spec, original, derived, tools)
    with pytest.raises(AudioError, match="AUDIO_TOOL_UNAVAILABLE"):
        replace(tools, ffmpeg=str(tmp_path / "missing")).version()
    with pytest.raises(AudioError, match="AUDIO_TOOL_TIMEOUT"):
        replace(tools, timeout=0.000001).version()
    with pytest.raises(AudioError, match="AUDIO_TOOL_FAILED"):
        replace(tools, ffmpeg=sys.executable).version()


def test_video_without_audio_stream(audio, tmp_path):
    original, derived, tools, spec, save = audio
    video = tmp_path / "video.mkv"
    subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=16x16:d=0.1",
            "-an",
            "-c:v",
            "ffv1",
            str(video),
        ],
        check=True,
    )
    with pytest.raises(AudioError, match="AUDIO_NO_STREAM"):
        prepare_audio(save(video.read_bytes()), spec, original, derived, tools)


def test_limits_tool_version_and_cached_tampering(audio):
    original, derived, tools, spec, save = audio
    recording = save(make_wav(rate=8000))
    with pytest.raises(AudioError, match="AUDIO_RESOURCE_LIMIT"):
        prepare_audio(recording, spec, original, derived, replace(tools, max_input_bytes=20))
    with pytest.raises(AudioError, match="AUDIO_RESOURCE_LIMIT"):
        prepare_audio(recording, spec, original, derived, replace(tools, max_decoded_bytes=1000))
    with pytest.raises(AudioError, match="AUDIO_TOOL_VERSION_MISMATCH"):
        prepare_audio(recording, replace(spec, ffprobe_version="other"), original, derived, tools)
    result = prepare_audio(recording, spec, original, derived, tools)
    derived.path(result.storage_ref, result.output.sha256).write_bytes(b"damaged")
    with pytest.raises(AudioError, match="AUDIO_DIGEST_MISMATCH"):
        prepare_audio(recording, spec, original, derived, tools)
