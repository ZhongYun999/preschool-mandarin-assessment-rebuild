"""Speaking text v1 provider adapters; external requests happen only in evaluate."""

import json
import os
import re

import httpx

from app.evaluation import speaking_text_contract as business
from app.evaluation.qwen_qa_text import _failure as qwen_failure
from app.evaluation.speaking_text_execution_contract import (
    DEEPSEEK_SOURCE,
    QWEN_SOURCE,
    VERSION,
    supports_source,
)
from app.evaluation.worker import AdapterResult, KnownFailure


def _safe_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None
    )


def _business_requirement(requirement):
    return {
        **requirement,
        "source": business.SOURCE,
        "version": business.VERSION,
        "parameters": {
            k: requirement["parameters"][k] for k in ("prompt_version", "prompt_sha256")
        },
    }


def _resolve(session, submission, revision, requirement, source):
    if (
        requirement.get("source") != source
        or requirement.get("version") != VERSION
        or not supports_source(requirement.get("parameters"), source)
    ):
        raise ValueError("Frozen Speaking execution contract unavailable")
    return business.resolve_inputs(
        session, submission, revision, _business_requirement(requirement)
    )


def _content(inputs, requirement):
    try:
        asr = inputs.payload["asr"]
        identity = inputs.refs["asr"]
        ids = inputs.refs["speaking_consumption"]["segment_ids"]
        dependency = requirement["depends_on"]
        segments = asr["segments"]
        if (
            asr["source"] != "formal_speaking_asr"
            or not isinstance(segments, list)
            or not ids
            or [segment["segment_id"] for segment in segments] != ids
            or identity["source"] != dependency["source"]
            or identity["version"] != dependency["version"]
            or identity["speaking_consumption"]["segment_ids"] != ids
            or not all(
                identity.get(key)
                for key in (
                    "job_id",
                    "evaluation_id",
                    "input_fingerprint",
                    "submission_id",
                    "task_revision_id",
                    "speaking_source_sha256",
                )
            )
            or any(
                not (
                    (s["status"] == "TEXT" and isinstance(s["text"], str) and s["text"].strip())
                    or (s["status"] == "NO_TEXT" and s["text"] is None)
                )
                for s in segments
            )
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise KnownFailure("LLM_ASR_INPUT_INVALID") from None
    if not any(segment["status"] == "TEXT" for segment in segments):
        raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE")
    return json.dumps(
        {"task": inputs.payload["context"], "asr": asr},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )


def _raw_result(content, requirement, request_id, usage, reported_model):
    params = requirement["parameters"]
    if reported_model is not None and reported_model != params["model"]:
        return AdapterResult(
            payload={"invalid": True}, provider=params["provider"], model=params["model"]
        )
    try:
        evaluation = business.parse_result(content)
    except business.InsufficientEvidence:
        # Non-READY rows cannot store a payload. Detailed reasons need a separate
        # protected diagnostic contract; the stable code remains auditable.
        raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE") from None
    except (ValueError, TypeError):
        return AdapterResult(
            payload={"invalid": True}, provider=params["provider"], model=params["model"]
        )
    return AdapterResult(
        payload={
            "evaluation": evaluation,
            "prompt_version": params["prompt_version"],
            "prompt_sha256": params["prompt_sha256"],
            "business_contract": {"source": business.SOURCE, "version": business.VERSION},
            "execution_contract": {
                "source": requirement["source"],
                "version": requirement["version"],
            },
            "request_id": _safe_id(request_id),
            "provider_model": _safe_id(reported_model),
            "usage": usage,
        },
        provider=params["provider"],
        model=params["model"],
    )


def _validate_result(result, requirement):
    params = requirement["parameters"]
    payload = result.payload
    return (
        result.provider == params["provider"]
        and result.model == params["model"]
        and isinstance(payload, dict)
        and set(payload)
        == {
            "evaluation",
            "prompt_version",
            "prompt_sha256",
            "business_contract",
            "execution_contract",
            "request_id",
            "provider_model",
            "usage",
        }
        and business.valid_result(payload["evaluation"])
        and payload["prompt_version"] == params["prompt_version"]
        and payload["prompt_sha256"] == params["prompt_sha256"]
        and payload["business_contract"] == {"source": business.SOURCE, "version": business.VERSION}
        and payload["execution_contract"]
        == {"source": requirement["source"], "version": requirement["version"]}
    )


def _usage(value, names):
    return (
        {k: value[k] for k in names if type(value.get(k)) is int and value[k] >= 0}
        if isinstance(value, dict)
        else {}
    )


class QwenSpeakingText:
    def __init__(self, *, timeout=60, client_factory=None, key_provider=None):
        self.timeout, self.factory = timeout, client_factory
        self.key_provider = key_provider or (lambda: os.environ.get("DASHSCOPE_API_KEY", ""))

    def resolve_inputs(self, session, submission, revision, requirement):
        return _resolve(session, submission, revision, requirement, QWEN_SOURCE)

    def evaluate(self, inputs, requirement):
        import dashscope
        from dashscope import errors

        p = requirement["parameters"]
        if not supports_source(p, requirement["source"]):
            raise KnownFailure("LLM_CONTRACT_UNAVAILABLE")
        data = _content(inputs, requirement)
        key = self.key_provider().strip()
        if not key:
            raise KnownFailure("LLM_CREDENTIALS_MISSING")
        try:
            with (self.factory or dashscope.Client)(
                api_key=key,
                base_url=p["endpoint"],
                region=p["region"],
                workspace=p["workspace"],
                max_retries=0,
                timeout=self.timeout,
                raise_on_error=False,
            ) as client:
                response = client.chat.completions.create(
                    model=p["model"],
                    messages=[
                        {"role": "system", "content": inputs.payload["system_prompt"]},
                        {"role": "user", "content": data},
                    ],
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    temperature=p["temperature"],
                    max_tokens=p["max_tokens"],
                    stream=False,
                    max_retries=0,
                    raw=True,
                )
        except errors.AuthenticationError:
            raise KnownFailure("LLM_AUTHENTICATION") from None
        except errors.PermissionDeniedError as exc:
            raise KnownFailure(
                "LLM_QUOTA" if exc.code in {"Arrearage", "insufficient_quota"} else "LLM_PERMISSION"
            ) from None
        except errors.BadRequestError:
            raise KnownFailure("LLM_REQUEST_REJECTED") from None
        except errors.RateLimitError as exc:
            raise KnownFailure(
                "LLM_QUOTA"
                if exc.code in {"insufficient_quota", "Throttling.AllocationQuota"}
                else "LLM_RATE_LIMIT"
            ) from None
        except (errors.APITimeoutError, errors.APIConnectionError):
            raise RuntimeError("LLM external outcome unknown") from None
        status = response.status_code
        if status >= 500 or status in (408, 409):
            raise RuntimeError("LLM external outcome unknown")
        try:
            body = response.http_response.json()
        except ValueError:
            reason = qwen_failure(status, None)
            if reason:
                raise KnownFailure(reason) from None
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        if not isinstance(body, dict):
            reason = qwen_failure(status, None)
            if reason:
                raise KnownFailure(reason)
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        code = body.get("code")
        if code is not None and not isinstance(code, str):
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        if status != 200 or code:
            reason = qwen_failure(status, code)
            if reason:
                raise KnownFailure(reason)
            raise RuntimeError("LLM external outcome unknown")
        try:
            choices = body["output"]["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if (
                choice["finish_reason"] != "stop"
                or message["role"] != "assistant"
                or message.get("refusal")
            ):
                raise ValueError
            content = message["content"]
        except (KeyError, TypeError, ValueError):
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        return _raw_result(
            content,
            requirement,
            body.get("request_id") or response.request_id,
            _usage(body.get("usage"), ("input_tokens", "output_tokens", "total_tokens")),
            body.get("model"),
        )

    validate_result = staticmethod(_validate_result)


class DeepSeekSpeakingText:
    def __init__(self, *, timeout=60, client_factory=None, key_provider=None):
        self.timeout, self.factory = timeout, client_factory
        self.key_provider = key_provider or (lambda: os.environ.get("DEEPSEEK_API_KEY", ""))

    def resolve_inputs(self, session, submission, revision, requirement):
        return _resolve(session, submission, revision, requirement, DEEPSEEK_SOURCE)

    def evaluate(self, inputs, requirement):
        p = requirement["parameters"]
        if not supports_source(p, requirement["source"]):
            raise KnownFailure("LLM_CONTRACT_UNAVAILABLE")
        data = _content(inputs, requirement)
        key = self.key_provider().strip()
        if not key:
            raise KnownFailure("LLM_CREDENTIALS_MISSING")
        factory = self.factory or (
            lambda **kwargs: httpx.Client(**kwargs, trust_env=False, follow_redirects=False)
        )
        try:
            with factory(timeout=self.timeout) as client:
                response = client.post(
                    p["endpoint"],
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": p["model"],
                        "messages": [
                            {"role": "system", "content": inputs.payload["system_prompt"]},
                            {"role": "user", "content": data},
                        ],
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": "disabled"},
                        "temperature": p["temperature"],
                        "max_tokens": p["max_tokens"],
                        "stream": False,
                    },
                )
        except httpx.RequestError:
            raise RuntimeError("LLM external outcome unknown") from None
        status = response.status_code
        if status >= 500 or status in (408, 409):
            raise RuntimeError("LLM external outcome unknown")
        if status != 200:
            reason = {
                400: "LLM_REQUEST_REJECTED",
                401: "LLM_AUTHENTICATION",
                402: "LLM_QUOTA",
                403: "LLM_PERMISSION",
                404: "LLM_MODEL_UNAVAILABLE",
                422: "LLM_REQUEST_REJECTED",
                429: "LLM_RATE_LIMIT",
            }.get(status)
            if reason:
                raise KnownFailure(reason)
            raise RuntimeError("LLM external outcome unknown")
        try:
            body = response.json()
            choices = body["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if (
                choice["finish_reason"] != "stop"
                or message["role"] != "assistant"
                or message.get("refusal")
            ):
                raise ValueError
            content = message["content"]
        except (ValueError, KeyError, TypeError):
            return AdapterResult(payload={"invalid": True}, provider="deepseek", model=p["model"])
        return _raw_result(
            content,
            requirement,
            body.get("id"),
            _usage(body.get("usage"), ("prompt_tokens", "completion_tokens", "total_tokens")),
            body.get("model"),
        )

    validate_result = staticmethod(_validate_result)
