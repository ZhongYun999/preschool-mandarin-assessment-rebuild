"""Qwen3-ASR-Flash adapter, using DashScope's non-streaming synchronous API."""

import base64
import os
import re
from dataclasses import asdict

from app.evaluation.asr_contract import MODEL, SOURCE, VERSION, recognition_result, valid_parameters
from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure, recording_input
from app.media.audio_inspection import AudioError, AudioTools
from app.media.audio_normalization import AudioSpec, prepare_audio
from app.media.recording_store import RecordingStore

MAX_DATA_URI_BYTES = 10_000_000
MAX_SECONDS = 300


def _failure(status, code):
    if code in {"Arrearage", "insufficient_quota", "Throttling.AllocationQuota"}:
        return "ASR_QUOTA"
    if status == 401 or code in {"InvalidApiKey", "InvalidApiKey.NotFound"}:
        return "ASR_AUTHENTICATION"
    if status == 403:
        return "ASR_PERMISSION"
    if status == 404 or code in {"ModelNotFound", "InvalidModel"}:
        return "ASR_MODEL_UNAVAILABLE"
    if status == 429:
        return "ASR_RATE_LIMIT"
    if code in {"InvalidParameter", "InvalidParameterValue", "InvalidRequest"}:
        return "ASR_REQUEST_REJECTED"
    if 400 <= status < 500 and status not in (408, 409):
        return "ASR_REQUEST_REJECTED"
    return None


class DashScopeASR:
    def __init__(
        self,
        original_store: RecordingStore,
        derived_store: RecordingStore,
        tools: AudioTools,
        *,
        timeout: float = 60,
        client_factory=None,
        credential_provider=None,
    ):
        self.original_store, self.derived_store, self.tools = original_store, derived_store, tools
        self.timeout, self._factory = timeout, client_factory
        self._credential_provider = credential_provider

    def resolve_inputs(self, session, submission, revision, requirement):
        if (requirement.get("source"), requirement.get("version")) != (
            SOURCE,
            VERSION,
        ) or not valid_parameters(requirement.get("parameters", {})):
            raise ValueError("Invalid ASR contract")
        recording = recording_input(session, submission)
        p = requirement["parameters"]
        endpoint = f"https://{p['workspace']}.{p['region']}.maas.aliyuncs.com/api/v1"
        return InputSnapshot(
            refs={
                **{k: recording[k] for k in ("recording_id", "sha256", "media_type")},
                "submission_id": str(submission.submission_id),
                "task_revision_id": str(revision.task_revision_id),
                "asr_contract": {"source": SOURCE, "version": VERSION},
                "parameters": p,
                "endpoint": endpoint,
            },
            payload={"recording": recording},
        )

    def evaluate(self, inputs, requirement):
        import dashscope
        from dashscope import errors

        p = requirement["parameters"]
        recording = inputs.payload["recording"]
        try:
            prepared = prepare_audio(
                recording,
                AudioSpec(**p["audio_spec"]),
                self.original_store,
                self.derived_store,
                self.tools,
            )
            if (
                prepared.original.duration_seconds > MAX_SECONDS
                or prepared.output.duration_seconds > MAX_SECONDS
            ):
                raise KnownFailure("ASR_AUDIO_INCOMPATIBLE")
            if 22 + 4 * ((prepared.output.byte_length + 2) // 3) > MAX_DATA_URI_BYTES:
                raise KnownFailure("ASR_AUDIO_INCOMPATIBLE")
            store = self.original_store if prepared.reused_original else self.derived_store
            data = store.read(prepared.storage_ref, prepared.output.sha256)
        except AudioError as exc:
            raise KnownFailure(exc.code) from None
        except (ValueError, OSError):
            raise KnownFailure("AUDIO_UNAVAILABLE") from None
        uri = "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")
        if len(uri) > MAX_DATA_URI_BYTES:
            raise KnownFailure("ASR_AUDIO_INCOMPATIBLE")
        try:
            key = (
                self._credential_provider()
                if self._credential_provider is not None
                else os.environ.get("DASHSCOPE_API_KEY", "")
            ).strip()
        except Exception:  # noqa: BLE001 - credentials must not leak through diagnostics
            raise KnownFailure("ASR_CREDENTIALS_UNAVAILABLE") from None
        if not key:
            raise KnownFailure("ASR_CREDENTIALS_MISSING")
        options = {"enable_itn": p["enable_itn"]}
        if p["language"] is not None:
            options["language"] = p["language"]
        factory = self._factory or dashscope.Client
        try:
            with factory(
                api_key=key,
                base_url=inputs.refs["endpoint"],
                region=p["region"],
                workspace=p["workspace"],
                max_retries=0,
                timeout=self.timeout,
                raise_on_error=False,
            ) as client:
                response = client.multimodal.chat.create(
                    model=p["model"],
                    messages=[{"role": "user", "content": [{"audio": uri}]}],
                    parameters={"asr_options": options},
                    stream=False,
                    max_retries=0,
                    raw=True,
                )
        except errors.AuthenticationError:
            raise KnownFailure("ASR_AUTHENTICATION") from None
        except errors.PermissionDeniedError as exc:
            raise KnownFailure(
                "ASR_QUOTA" if exc.code in {"Arrearage", "insufficient_quota"} else "ASR_PERMISSION"
            ) from None
        except errors.BadRequestError:
            raise KnownFailure("ASR_REQUEST_REJECTED") from None
        except errors.RateLimitError as exc:
            raise KnownFailure(
                "ASR_QUOTA"
                if exc.code in {"insufficient_quota", "Throttling.AllocationQuota"}
                else "ASR_RATE_LIMIT"
            ) from None
        except (errors.APITimeoutError, errors.APIConnectionError):
            raise RuntimeError("ASR external outcome unknown") from None
        status = response.status_code
        if status >= 500 or status in (408, 409):
            raise RuntimeError("ASR external outcome unknown")
        try:
            body = response.http_response.json()
        except ValueError:
            reason = _failure(status, None)
            if reason:
                raise KnownFailure(reason) from None
            if status >= 500 or status in (408, 409):
                raise RuntimeError("ASR external outcome unknown") from None
            raise KnownFailure("ASR_INVALID_RESPONSE") from None
        if not isinstance(body, dict):
            if status >= 500 or status in (408, 409):
                raise RuntimeError("ASR external outcome unknown")
            reason = _failure(status, None)
            if reason:
                raise KnownFailure(reason)
            raise KnownFailure("ASR_INVALID_RESPONSE")
        code = body.get("code")
        if code is not None and not isinstance(code, str):
            raise KnownFailure("ASR_INVALID_RESPONSE")
        if status != 200 or code:
            reason = _failure(status, code)
            if reason:
                raise KnownFailure(reason)
            raise RuntimeError("ASR external outcome unknown")
        try:
            choices = body["output"]["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            content = message["content"]
            if (
                choice["finish_reason"] != "stop"
                or message["role"] != "assistant"
                or not isinstance(content, list)
                or len(content) != 1
            ):
                raise ValueError
            text = content[0]["text"]
            if not isinstance(text, str):
                raise TypeError
        except (KeyError, IndexError, TypeError, ValueError):
            raise KnownFailure("ASR_INVALID_RESPONSE") from None
        usage = body.get("usage")
        safe_usage = (
            {
                k: usage[k]
                for k in ("input_tokens", "output_tokens", "total_tokens", "seconds")
                if type(usage.get(k)) is int and usage[k] >= 0
            }
            if isinstance(usage, dict)
            else {}
        )
        if isinstance(usage, dict):
            for field in ("input_tokens_details", "output_tokens_details"):
                details = usage.get(field)
                if (
                    isinstance(details, dict)
                    and type(details.get("text_tokens")) is int
                    and details["text_tokens"] >= 0
                ):
                    safe_usage[field] = {"text_tokens": details["text_tokens"]}
        request_id = body.get("request_id") or response.request_id
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id
        ):
            request_id = None
        payload = {
            "status": "TEXT" if text.strip() else "NO_TEXT",
            "text": text if text.strip() else None,
            "request_id": request_id,
            "usage": safe_usage,
            "audio_evidence": asdict(prepared),
        }
        return AdapterResult(payload=payload, provider="dashscope", model=MODEL)

    def validate_result(self, result, requirement):
        try:
            value = recognition_result(result.payload)
            return (
                result.provider == "dashscope"
                and result.model == MODEL
                and (
                    (
                        value["status"] == "TEXT"
                        and isinstance(value["text"], str)
                        and bool(value["text"].strip())
                    )
                    or (value["status"] == "NO_TEXT" and value["text"] is None)
                )
            )
        except (KeyError, TypeError):
            return False
