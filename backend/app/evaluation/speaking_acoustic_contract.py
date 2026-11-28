"""Frozen, full-submission acoustic measurement contract, independent of ASR."""

from app.evaluation.speaking_asr_contract import valid_consumption as valid_audio_consumption
from app.media.audio_normalization import AudioSpec

SOURCE = "local-speaking-acoustic"
VERSION = "1"
ALGORITHM = "energy-pcm-v1"
SCHEMA = "speaking-acoustic-metrics-v1"


def valid_parameters(params):
    if not isinstance(params, dict) or set(params) != {
        "audio_spec",
        "processing_version",
        "algorithm",
        "frame_samples",
        "active_rms_ratio",
        "long_gap_samples",
    }:
        return False
    try:
        spec = AudioSpec(**params["audio_spec"])
    except (TypeError, ValueError):
        return False
    return (
        spec.container == "wav"
        and spec.codec == "pcm_s16le"
        and spec.sample_rate == 16000
        and spec.channels == 1
        and params["processing_version"] == spec.processing_version
        and params["algorithm"] == ALGORITHM
        and type(params["frame_samples"]) is int
        and params["frame_samples"] == 320
        and type(params["active_rms_ratio"]) is float
        and params["active_rms_ratio"] == 0.01
        and type(params["long_gap_samples"]) is int
        and params["long_gap_samples"] == 24000
    )


def valid_consumption(source, consumption, requirement, upstream, payload):
    return (
        valid_parameters(requirement.get("parameters"))
        and valid_audio_consumption(
            source, consumption, requirement, upstream, payload, original_audio_type="audio/wav"
        )
        and consumption["segment_ids"] == [segment["segment_id"] for segment in source["segments"]]
        and all(item["media_type"] == "audio/wav" for item in consumption["audio_inputs"])
    )
