"""Provider-independent QA adapter with execution-bound provider credentials."""

import json
from dataclasses import dataclass, field

import httpx

from app.evaluation import provider_config
from app.evaluation import qa_formal_text_contract as business
from app.evaluation.qwen_qa_text import _failure as qwen_failure
from app.evaluation.speaking_text_providers import _safe_id, _usage
from app.evaluation.worker import AdapterResult, KnownFailure


@dataclass(frozen=True)
class BoundQAAdapter:
    snapshot: dict
    key: str = field(repr=False)
    timeout: float = 60
    client_factory: object = field(default=None, repr=False, compare=False)

    def evaluate(self, inputs, requirement):
        p = self.snapshot
        asr = inputs.payload.get("asr")
        identity = inputs.refs.get("asr")
        dep = requirement.get("depends_on", {})
        if (
            not isinstance(asr, dict)
            or asr.get("status") != "TEXT"
            or not isinstance(asr.get("text"), str)
            or not asr["text"].strip()
            or not isinstance(identity, dict)
            or identity.get("source") != dep.get("source")
            or identity.get("version") != dep.get("version")
            or not all(
                identity.get(k)
                for k in (
                    "job_id",
                    "evaluation_id",
                    "input_fingerprint",
                    "submission_id",
                    "paper_entry_id",
                    "task_revision_id",
                    "recording_id",
                )
            )
        ):
            raise KnownFailure("LLM_ASR_INPUT_INVALID")
        data = json.dumps(
            {"task": inputs.payload["context"], "asr_text": asr["text"]},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        messages = [
            {"role": "system", "content": inputs.payload["system_prompt"]},
            {"role": "user", "content": data},
        ]
        if p["provider"] == "deepseek":
            return self._deepseek(messages, requirement)
        return self._qwen(messages, requirement)

    def _result(self, content, requirement, request_id, usage, reported_model):
        p = self.snapshot
        if reported_model is not None and reported_model != p["model"]:
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )
        try:
            evaluation = business.parse_result(content)
        except business.InsufficientEvidence:
            raise KnownFailure("LLM_INSUFFICIENT_EVIDENCE") from None
        except (ValueError, TypeError):
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )
        return AdapterResult(
            payload={
                "evaluation": evaluation,
                "prompt_version": business.PROMPT_VERSION,
                "prompt_sha256": business.PROMPT_SHA256,
                "contract": {"source": business.SOURCE, "version": business.VERSION},
                "request_id": _safe_id(request_id),
                "provider_model": _safe_id(reported_model),
                "usage": usage,
            },
            provider=p["provider"],
            model=p["model"],
            engine_version=p["api_version"],
        )

    def _deepseek(self, messages, requirement):
        p = self.snapshot
        try:
            with (self.client_factory or httpx.Client)(
                timeout=self.timeout, trust_env=False, follow_redirects=False
            ) as client:
                response = client.post(
                    p["endpoint"],
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={
                        "model": p["model"],
                        "messages": messages,
                        **p["request_parameters"],
                    },
                )
        except httpx.RequestError:
            raise RuntimeError("LLM external outcome unknown") from None
        if response.status_code >= 500 or response.status_code in (408, 409):
            raise RuntimeError("LLM external outcome unknown")
        if response.status_code != 200:
            code = {
                400: "LLM_REQUEST_REJECTED",
                401: "LLM_AUTHENTICATION",
                402: "LLM_QUOTA",
                403: "LLM_PERMISSION",
                404: "LLM_MODEL_UNAVAILABLE",
                422: "LLM_REQUEST_REJECTED",
                429: "LLM_RATE_LIMIT",
            }.get(response.status_code)
            if code:
                raise KnownFailure(code)
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
            return self._result(
                message["content"],
                requirement,
                body.get("id"),
                _usage(body.get("usage"), ("prompt_tokens", "completion_tokens", "total_tokens")),
                body.get("model"),
            )
        except (KeyError, TypeError, ValueError):
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )

    def _qwen(self, messages, requirement):
        import dashscope
        from dashscope import errors

        p = self.snapshot
        try:
            with (self.client_factory or dashscope.Client)(
                api_key=self.key,
                base_url=p["endpoint"],
                region=p["region"],
                workspace=p["workspace"],
                max_retries=0,
                timeout=self.timeout,
                raise_on_error=False,
            ) as client:
                response = client.chat.completions.create(
                    model=p["model"],
                    messages=messages,
                    **p["request_parameters"],
                    max_retries=0,
                    raw=True,
                )
        except errors.AuthenticationError:
            raise KnownFailure("LLM_AUTHENTICATION") from None
        except errors.PermissionDeniedError as exc:
            raise KnownFailure(
                "LLM_QUOTA" if exc.code in {"Arrearage", "insufficient_quota"} else "LLM_PERMISSION"
            ) from None
        except errors.BadRequestError as exc:
            raise KnownFailure(qwen_failure(400, exc.code) or "LLM_REQUEST_REJECTED") from None
        except errors.NotFoundError:
            raise KnownFailure("LLM_MODEL_UNAVAILABLE") from None
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
            if not isinstance(body, dict):
                raise TypeError
        except (ValueError, TypeError):
            code = qwen_failure(status, None)
            if code:
                raise KnownFailure(code) from None
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )
        code = body.get("code")
        if code is not None and not isinstance(code, str):
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )
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
            return self._result(
                message["content"],
                requirement,
                body.get("request_id") or response.request_id,
                _usage(body.get("usage"), ("input_tokens", "output_tokens", "total_tokens")),
                body.get("model"),
            )
        except (KeyError, TypeError, ValueError):
            return AdapterResult(
                payload={"invalid": True}, provider=p["provider"], model=p["model"]
            )

    def validate_result(self, result, requirement):
        payload = result.payload
        return (
            result.provider == self.snapshot["provider"]
            and result.model == self.snapshot["model"]
            and isinstance(payload, dict)
            and set(payload)
            == {
                "evaluation",
                "prompt_version",
                "prompt_sha256",
                "contract",
                "request_id",
                "provider_model",
                "usage",
            }
            and payload["contract"] == {"source": business.SOURCE, "version": business.VERSION}
            and payload["prompt_version"] == business.PROMPT_VERSION
            and payload["prompt_sha256"] == business.PROMPT_SHA256
            and business.valid_result(payload["evaluation"])
        )


class DynamicQAText:
    def __init__(self, *, client_factories=None, timeout=60):
        self.client_factories = client_factories or {}
        self.timeout = timeout

    resolve_inputs = staticmethod(business.resolve_inputs)

    def prepare_execution(self, session, now):
        config, credential, key = provider_config.resolve(session)
        endpoint = (
            f"https://{config.workspace}.{config.region}.maas.aliyuncs.com/api/v1"
            if config.provider == "dashscope"
            else "https://api.deepseek.com/chat/completions"
        )
        snapshot = {
            "provider": config.provider,
            "model": config.model,
            "execution_contract": f"{config.provider}-qa-text/1",
            "endpoint": endpoint,
            "api_version": "dashscope-chat-json-v1"
            if config.provider == "dashscope"
            else "deepseek-chat-json-v1",
            "region": config.region,
            "workspace": config.workspace,
            "request_parameters": {
                "temperature": 1,
                "max_tokens": 2048,
                "response_format": {"type": "json_object"},
                "stream": False,
                **(
                    {"enable_thinking": False}
                    if config.provider == "dashscope"
                    else {"thinking": {"type": "disabled"}}
                ),
            },
            "config_revision": config.revision,
            "config_id": str(config.config_id),
            "credential_id": str(credential.credential_id),
            "credential_version": credential.version,
            "started_at": now.isoformat(),
        }
        return snapshot, BoundQAAdapter(
            snapshot, key, self.timeout, self.client_factories.get(config.provider)
        )


def run_qa_once(engine, *, asr_adapter, lease, batch_size, recording_store=None):
    """Run the declared QA business sources with the production registry."""
    from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
    from app.evaluation.asr_contract import VERSION as ASR_VERSION
    from app.evaluation.worker import run_once
    from app.scoring.registry import PRODUCTION_REGISTRY

    return run_once(
        engine,
        registry=PRODUCTION_REGISTRY,
        adapters={
            (ASR_SOURCE, ASR_VERSION): asr_adapter,
            (business.SOURCE, business.VERSION): DynamicQAText(),
        },
        lease=lease,
        batch_size=batch_size,
        recording_store=recording_store,
    )
