"""Versioned Qwen3-ASR recognition contract."""

import re
from collections.abc import Mapping

SOURCE = "dashscope-qwen3-asr"
VERSION = "1"
MODEL = "qwen3-asr-flash"
LANGUAGES = frozenset(
    [
        "zh",
        "yue",
        "en",
        "ja",
        "de",
        "ko",
        "ru",
        "fr",
        "pt",
        "ar",
        "it",
        "es",
        "hi",
        "id",
        "th",
        "tr",
        "uk",
        "vi",
        "cs",
        "da",
        "fil",
        "fi",
        "is",
        "ms",
        "no",
        "pl",
        "sv",
    ]
)


def valid_parameters(p: Mapping) -> bool:
    if set(p) != {"model", "region", "workspace", "enable_itn", "language", "audio_spec"}:
        return False
    spec = p["audio_spec"]
    return (
        p["model"] == MODEL
        and isinstance(p["region"], str)
        and p["region"] in {"cn-beijing", "ap-southeast-1", "us-east-1"}
        and isinstance(p["workspace"], str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", p["workspace"]) is not None
        and type(p["enable_itn"]) is bool
        and (p["language"] is None or isinstance(p["language"], str) and p["language"] in LANGUAGES)
        and isinstance(spec, dict)
        and set(spec)
        == {
            "container",
            "codec",
            "sample_rate",
            "channels",
            "processing_version",
            "ffmpeg_version",
            "ffprobe_version",
        }
        and spec["container"] == "wav"
        and spec["codec"] == "pcm_s16le"
        and type(spec["sample_rate"]) is int
        and spec["sample_rate"] == 16000
        and type(spec["channels"]) is int
        and spec["channels"] == 1
        and all(
            isinstance(spec[k], str) and bool(spec[k].strip())
            for k in ("processing_version", "ffmpeg_version", "ffprobe_version")
        )
    )


def recognition_result(payload: Mapping) -> dict:
    return {"status": payload["status"], "text": payload["text"]}
