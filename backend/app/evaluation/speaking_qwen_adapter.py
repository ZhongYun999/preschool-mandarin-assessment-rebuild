"""Qwen3-ASR request for one already-frozen Speaking audio input."""

import base64
import os
import re

from app.evaluation.asr_contract import MODEL
from app.evaluation.dashscope_asr import _failure
from app.evaluation.worker import KnownFailure

MAX_DATA_URI_BYTES = 10_000_000
MAX_SECONDS = 300
DATA_URI_PREFIX = "data:audio/wav;base64,"


def encoded_size(byte_length):
    return len(DATA_URI_PREFIX) + 4 * ((byte_length + 2) // 3)


def _request_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None
    )


class SpeakingASRFailure(KnownFailure):
    def __init__(self, code, request_id=None):
        super().__init__(code)
        self.request_id = _request_id(request_id)


class SpeakingASRUnknown(RuntimeError):
    def __init__(self, request_id=None):
        super().__init__("ASR external outcome unknown")
        self.request_id = _request_id(request_id)


class SpeakingQwenASR:
    def __init__(self, *, client_factory=None, key_provider=None, timeout=60):
        self.factory = client_factory
        self.key_provider = key_provider or (lambda: os.environ.get("DASHSCOPE_API_KEY", ""))
        self.timeout = timeout

    def recognize(self, audio: bytes, params: dict):
        """Called only after the operation's durable dispatch marker commits."""
        import dashscope
        from dashscope import errors

        if encoded_size(len(audio)) > MAX_DATA_URI_BYTES:
            raise KnownFailure("ASR_AUDIO_INCOMPATIBLE")
        key = self.key_provider().strip()
        if not key:
            raise KnownFailure("ASR_CREDENTIALS_MISSING")
        endpoint = f"https://{params['workspace']}.{params['region']}.maas.aliyuncs.com/api/v1"
        options = {"enable_itn": params["enable_itn"]}
        if params["language"] is not None:
            options["language"] = params["language"]
        uri = DATA_URI_PREFIX + base64.b64encode(audio).decode("ascii")
        try:
            with (self.factory or dashscope.Client)(
                api_key=key,
                base_url=endpoint,
                region=params["region"],
                workspace=params["workspace"],
                max_retries=0,
                timeout=self.timeout,
                raise_on_error=False,
            ) as client:
                response = client.multimodal.chat.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": [{"audio": uri}]}],
                    parameters={"asr_options": options},
                    stream=False,
                    max_retries=0,
                    raw=True,
                )
        except errors.AuthenticationError as exc:
            raise SpeakingASRFailure(
                "ASR_AUTHENTICATION", getattr(exc, "request_id", None)
            ) from None
        except errors.PermissionDeniedError as exc:
            raise SpeakingASRFailure(
                "ASR_QUOTA"
                if exc.code in {"Arrearage", "insufficient_quota"}
                else "ASR_PERMISSION",
                getattr(exc, "request_id", None),
            ) from None
        except errors.BadRequestError as exc:
            raise SpeakingASRFailure(
                "ASR_REQUEST_REJECTED", getattr(exc, "request_id", None)
            ) from None
        except errors.RateLimitError as exc:
            raise SpeakingASRFailure(
                "ASR_QUOTA"
                if exc.code in {"insufficient_quota", "Throttling.AllocationQuota"}
                else "ASR_RATE_LIMIT",
                getattr(exc, "request_id", None),
            ) from None
        except (errors.APITimeoutError, errors.APIConnectionError):
            raise SpeakingASRUnknown() from None
        status = response.status_code
        response_id = _request_id(getattr(response, "request_id", None))
        if status >= 500 or status in (408, 409):
            raise SpeakingASRUnknown(response_id)
        try:
            body = response.http_response.json()
        except ValueError:
            raise SpeakingASRFailure(
                _failure(status, None) or "ASR_INVALID_RESPONSE", response_id
            ) from None
        if not isinstance(body, dict):
            raise SpeakingASRFailure(_failure(status, None) or "ASR_INVALID_RESPONSE", response_id)
        response_id = _request_id(body.get("request_id")) or response_id
        code = body.get("code")
        if code is not None and not isinstance(code, str):
            raise SpeakingASRFailure("ASR_INVALID_RESPONSE", response_id)
        if status != 200 or code:
            reason = _failure(status, code)
            if reason:
                raise SpeakingASRFailure(reason, response_id)
            raise SpeakingASRUnknown(response_id)
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
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise SpeakingASRFailure("ASR_INVALID_RESPONSE", response_id) from None
        return {"status": "TEXT", "text": text, "request_id": response_id}
