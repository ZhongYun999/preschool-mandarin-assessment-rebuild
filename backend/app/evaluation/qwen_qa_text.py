"""Qwen text adapter for the explicitly selected QA evaluation contract."""

import json
import os
import re

from app.evaluation.qa_text_contract import PROMPT, SOURCE, VERSION, valid_parameters, valid_result
from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("Non-finite JSON number")


def _failure(status, code):
    if code in {"Arrearage", "insufficient_quota", "Throttling.AllocationQuota"}:
        return "LLM_QUOTA"
    if status == 401 or code in {"InvalidApiKey", "InvalidApiKey.NotFound"}:
        return "LLM_AUTHENTICATION"
    if status == 403:
        return "LLM_PERMISSION"
    if status == 404 or code in {"ModelNotFound", "InvalidModel"}:
        return "LLM_MODEL_UNAVAILABLE"
    if status == 429:
        return "LLM_RATE_LIMIT"
    if (
        code in {"InvalidParameter", "InvalidParameterValue", "InvalidRequest"}
        or 400 <= status < 500
    ):
        return "LLM_REQUEST_REJECTED"
    return None


class QwenQAText:
    def __init__(self, *, timeout=60, client_factory=None, credential_provider=None):
        self.timeout, self._factory = timeout, client_factory
        self._credential_provider = credential_provider

    def resolve_inputs(self, session, submission, revision, requirement):
        if (requirement.get("source"), requirement.get("version")) != (
            SOURCE,
            VERSION,
        ) or not valid_parameters(requirement.get("parameters")):
            raise ValueError("Frozen QA evaluation contract unavailable")
        p = requirement["parameters"]
        endpoint = f"https://{p['workspace']}.{p['region']}.maas.aliyuncs.com/api/v1"
        context = {
            key: getattr(revision, key)
            for key in (
                "grade_level",
                "prompt_text",
                "material_description",
                "reference_answer",
                "scoring_instruction",
            )
        }
        context["question_has_image"] = revision.question_image_asset_id is not None
        return InputSnapshot(
            refs={
                "submission_id": str(submission.submission_id),
                "task_revision_id": str(revision.task_revision_id),
                "contract": {"source": SOURCE, "version": VERSION},
                "parameters": p,
                "endpoint": endpoint,
            },
            payload={"context": context, "system_prompt": PROMPT},
        )

    def evaluate(self, inputs, requirement):
        import dashscope
        from dashscope import errors

        p = requirement["parameters"]
        asr, identity = inputs.payload.get("asr"), inputs.refs.get("asr")
        dep = requirement.get("depends_on", {})
        if (
            not isinstance(asr, dict)
            or asr.get("status") != "TEXT"
            or not isinstance(asr.get("text"), str)
            or not asr["text"].strip()
            or not isinstance(identity, dict)
            or identity.get("source") != dep.get("source")
            or identity.get("version") != dep.get("version")
            or identity.get("submission_id") != inputs.refs["submission_id"]
            or identity.get("task_revision_id") != inputs.refs["task_revision_id"]
            or not identity.get("evaluation_id")
        ):
            raise KnownFailure("LLM_ASR_INPUT_INVALID")
        try:
            key = (
                self._credential_provider()
                if self._credential_provider is not None
                else os.environ.get("DASHSCOPE_API_KEY", "")
            ).strip()
        except Exception:  # noqa: BLE001 - credentials must not leak through diagnostics
            raise KnownFailure("LLM_CREDENTIALS_UNAVAILABLE") from None
        if not key:
            raise KnownFailure("LLM_CREDENTIALS_MISSING")
        user_data = json.dumps(
            {"task": inputs.payload["context"], "asr_text": asr["text"]},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
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
                response = client.chat.completions.create(
                    model=p["model"],
                    messages=[
                        {"role": "system", "content": inputs.payload["system_prompt"]},
                        {"role": "user", "content": user_data},
                    ],
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    temperature=p["temperature"],
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
            reason = _failure(status, None)
            if reason:
                raise KnownFailure(reason) from None
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        if not isinstance(body, dict):
            reason = _failure(status, None)
            if reason:
                raise KnownFailure(reason)
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        code = body.get("code")
        if code is not None and not isinstance(code, str):
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        if status != 200 or code:
            reason = _failure(status, code)
            if reason:
                raise KnownFailure(reason)
            raise RuntimeError("LLM external outcome unknown")
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
                or not isinstance(content, str)
            ):
                raise ValueError
            evaluation = json.loads(
                content, object_pairs_hook=_unique_object, parse_constant=_invalid_number
            )
            if not valid_result(evaluation):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            return AdapterResult(payload={"invalid": True}, provider="dashscope", model=p["model"])
        usage = body.get("usage")
        safe_usage = (
            {
                k: usage[k]
                for k in ("input_tokens", "output_tokens", "total_tokens")
                if type(usage.get(k)) is int and usage[k] >= 0
            }
            if isinstance(usage, dict)
            else {}
        )
        request_id = body.get("request_id") or response.request_id
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id
        ):
            request_id = None
        return AdapterResult(
            payload={
                "evaluation": evaluation,
                "request_id": request_id,
                "usage": safe_usage,
                "prompt_version": p["prompt_version"],
                "prompt_sha256": p["prompt_sha256"],
                "contract": {"source": SOURCE, "version": VERSION},
            },
            provider="dashscope",
            model=p["model"],
        )

    def validate_result(self, result, requirement):
        p = requirement["parameters"]
        return (
            result.provider == "dashscope"
            and result.model == p["model"]
            and isinstance(result.payload, dict)
            and valid_result(result.payload.get("evaluation"))
            and result.payload.get("prompt_version") == p["prompt_version"]
            and result.payload.get("prompt_sha256") == p["prompt_sha256"]
            and result.payload.get("contract") == {"source": SOURCE, "version": VERSION}
        )
