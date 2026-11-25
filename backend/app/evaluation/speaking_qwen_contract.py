"""Frozen contract for the full-submission Qwen Speaking ASR source."""

from app.evaluation.asr_contract import MODEL
from app.evaluation.asr_contract import valid_parameters as valid_qwen_parameters
from app.evaluation.speaking_asr_contract import recognition_result, valid_consumption

SOURCE = "dashscope-speaking-qwen3-asr"
VERSION = "1"
PROVIDER = "dashscope"


def valid_parameters(params):
    if not isinstance(params, dict) or set(params) != {
        "provider",
        "model",
        "region",
        "workspace",
        "enable_itn",
        "language",
        "audio_spec",
        "processing_version",
        "confirmed_no_text",
    }:
        return False
    return (
        params["provider"] == PROVIDER
        and params["model"] == MODEL
        and params["confirmed_no_text"] is False
        and isinstance(params["audio_spec"], dict)
        and params["processing_version"] == params["audio_spec"].get("processing_version")
        and valid_qwen_parameters(
            {
                k: params[k]
                for k in ("model", "region", "workspace", "enable_itn", "language", "audio_spec")
            }
        )
    )


def valid_full_consumption(source, consumption, requirement, upstream, payload):
    return (
        valid_consumption(
            source, consumption, requirement, upstream, payload, original_audio_type="audio/wav"
        )
        and consumption["segment_ids"] == [s["segment_id"] for s in source["segments"]]
        and all(item["media_type"] == "audio/wav" for item in consumption["audio_inputs"])
    )


def validate_result(payload, requirement, source, consumption, provider, model):
    if not valid_full_consumption(source, consumption, requirement, None, None):
        raise ValueError("Speaking ASR must cover the complete source")
    return recognition_result(
        payload, requirement, source, consumption, provider, model, original_audio_type="audio/wav"
    )
