"""Versioned Speaking ASR result validation; no adapter is registered here.

The source and consumption arguments come from the worker's verified frozen input,
not from the result returned by the adapter.
"""

SCHEMA = "speaking-asr-result-v1"


def valid_consumption(source, consumption, requirement, upstream, payload):
    """Validate an ordered subset of source segments and its exact audio inputs."""
    try:
        ids = consumption["segment_ids"]
        audio = consumption["audio_inputs"]
        params = requirement["parameters"]
        segments = source["segments"]
        selected = [s for s in segments if s["segment_id"] in ids]
        if (
            set(consumption) != {"segment_ids", "audio_inputs", "derived_recordings"}
            or upstream is not None
            or not ids
            or ids != [s["segment_id"] for s in selected]
            or len(audio) != len(selected)
            or not isinstance(params["processing_version"], str)
            or not params["processing_version"]
            or (
                payload is not None
                and (
                    set(payload) != {"audio_inputs", "submission_id", "source_sha256"}
                    or payload.get("audio_inputs") != audio
                    or payload.get("submission_id") != source["submission_id"]
                    or payload.get("source_sha256") != source["sha256"]
                )
            )
        ):
            return False
        derived = consumption["derived_recordings"]
        for item, segment in zip(audio, selected, strict=True):
            if (
                set(item)
                != {
                    "segment_id",
                    "recording_id",
                    "position",
                    "sha256",
                    "storage_ref",
                    "media_type",
                    "processing_version",
                }
                or item["segment_id"] != segment["segment_id"]
                or item["recording_id"] != segment["recording_id"]
                or item["position"] != segment["position"]
                or item["processing_version"] != params["processing_version"]
                or not (
                    all(item[k] == segment[k] for k in ("sha256", "storage_ref", "media_type"))
                    or any(
                        all(
                            record.get(k) == item[k]
                            for k in ("sha256", "storage_ref", "media_type")
                        )
                        for record in derived
                    )
                )
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def recognition_result(payload, requirement, source, consumption, provider, model):
    """Validate every covered result, retaining its ordered source identities."""
    try:
        params = requirement["parameters"]
        if (
            not valid_consumption(source, consumption, requirement, None, None)
            or not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
            or provider != params["provider"]
            or model != params["model"]
            or payload["schema"] != SCHEMA
            or payload["submission_id"] != source["submission_id"]
            or payload["speaking_source_sha256"] != source["sha256"]
            or payload["contract"]
            != {"source": requirement["source"], "version": requirement["version"]}
            or len(payload["segments"]) != len(consumption["audio_inputs"])
        ):
            raise ValueError("Speaking ASR result identity mismatch")
        result = []
        for segment, audio in zip(payload["segments"], consumption["audio_inputs"], strict=True):
            if (
                set(segment)
                != {
                    "segment_id",
                    "recording_id",
                    "position",
                    "status",
                    "text",
                    "input_sha256",
                    "processing_version",
                }
                or any(
                    segment[k] != audio[k]
                    for k in ("segment_id", "recording_id", "position", "processing_version")
                )
                or segment["input_sha256"] != audio["sha256"]
            ):
                raise ValueError("Speaking ASR segment identity mismatch")
            status, text = segment["status"], segment["text"]
            if not (
                (status == "TEXT" and isinstance(text, str) and bool(text.strip()))
                or (
                    status == "NO_TEXT" and text is None and params.get("confirmed_no_text") is True
                )
            ):
                raise ValueError("Invalid speaking ASR semantic result")
            result.append(
                {
                    k: segment[k]
                    for k in ("segment_id", "recording_id", "position", "status", "text")
                }
            )
        return {
            "source": "formal_speaking_asr",
            "status": "TEXT" if any(s["status"] == "TEXT" for s in result) else "NO_TEXT",
            "segments": result,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid speaking ASR result") from exc
