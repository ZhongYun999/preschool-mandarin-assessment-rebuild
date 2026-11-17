"""Optional single paid Qwen3-ASR request using generated, non-child audio."""

import argparse
import hashlib
import io
import json
import tempfile
import wave
from dataclasses import asdict
from pathlib import Path

from app.evaluation.asr_contract import MODEL, SOURCE, VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.worker import InputSnapshot, KnownFailure
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore


def main():
    parser = argparse.ArgumentParser(
        description="Optional single paid ASR smoke test with synthetic silence"
    )
    parser.add_argument("--confirm-paid-call", action="store_true")
    parser.add_argument(
        "--region", choices=("cn-beijing", "ap-southeast-1", "us-east-1"), required=True
    )
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("explicit --confirm-paid-call is required")
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "smoke-v1", tools.version(), tools.version(probe=True)
    )
    params = {
        "model": MODEL,
        "region": args.region,
        "workspace": args.workspace,
        "enable_itn": False,
        "language": None,
        "audio_spec": asdict(spec),
    }
    from app.evaluation.asr_contract import valid_parameters

    if not valid_parameters(params):
        parser.error("invalid workspace or configuration")
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(32000))
    audio = data.getvalue()
    digest = hashlib.sha256(audio).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        original, derived = (
            RecordingStore(Path(tmp) / "original"),
            RecordingStore(Path(tmp) / "derived"),
        )
        stream, path = original.staging_file()
        with stream:
            stream.write(audio)
        ref = original.commit(path, digest)
        path.unlink()
        recording = {
            "recording_id": "synthetic-smoke",
            "sha256": digest,
            "media_type": "audio/wav",
            "storage_ref": ref,
        }
        inputs = InputSnapshot(
            refs={"endpoint": f"https://{args.workspace}.{args.region}.maas.aliyuncs.com/api/v1"},
            payload={"recording": recording},
        )
        adapter = DashScopeASR(original, derived, tools)
        try:
            result = adapter.evaluate(
                inputs, {"source": SOURCE, "version": VERSION, "parameters": params}
            )
        except KnownFailure as exc:
            print(json.dumps({"success": False, "error_code": exc.code}))
            return 1
        except Exception:  # noqa: BLE001 - never print SDK exception data
            print(json.dumps({"success": False, "error_code": "EXTERNAL_OUTCOME_UNKNOWN"}))
            return 1
        print(
            json.dumps(
                {
                    "success": True,
                    "status": result.payload["status"],
                    "model": result.model,
                    "request_id": result.payload["request_id"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
