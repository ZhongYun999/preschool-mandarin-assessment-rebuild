"""Provider-specific frozen execution profiles for Speaking text v1."""

import math
import re

QWEN_SOURCE = "dashscope-speaking-text"
DEEPSEEK_SOURCE = "deepseek-speaking-text"
VERSION = "1"
QWEN_MODELS = frozenset({"qwen-plus", "qwen3.5-plus", "qwen3.6-plus"})
DEEPSEEK_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


def _base_parameters(params):
    from app.evaluation.speaking_text_contract import valid_parameters

    return valid_parameters({key: params.get(key) for key in ("prompt_version", "prompt_sha256")})


def valid_parameters(params):
    if not isinstance(params, dict):
        return False
    common = {
        "provider",
        "model",
        "api_version",
        "endpoint",
        "temperature",
        "response_format",
        "prompt_version",
        "prompt_sha256",
        "max_tokens",
    }
    provider = params.get("provider")
    if not isinstance(provider, str) or not isinstance(params.get("model"), str):
        return False
    if provider == "dashscope":
        if set(params) != common | {"region", "workspace", "enable_thinking"}:
            return False
        region, workspace = params["region"], params["workspace"]
        if (
            not isinstance(region, str)
            or region not in {"cn-beijing", "ap-southeast-1", "us-east-1"}
            or not isinstance(workspace, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", workspace) is None
            or params["endpoint"] != f"https://{workspace}.{region}.maas.aliyuncs.com/api/v1"
            or params["model"] not in QWEN_MODELS
            or params["api_version"] != "dashscope-chat-json-v1"
            or params["enable_thinking"] is not False
        ):
            return False
    elif provider == "deepseek":
        if set(params) != common | {"thinking"}:
            return False
        if (
            params["endpoint"] != DEEPSEEK_ENDPOINT
            or params["model"] not in DEEPSEEK_MODELS
            or params["api_version"] != "deepseek-chat-json-v1"
            or params["thinking"] != "disabled"
            or (params["model"] == "deepseek-flash" and params["temperature"] != 1)
        ):
            return False
    else:
        return False
    temperature = params["temperature"]
    return (
        type(params["max_tokens"]) is int
        and params["max_tokens"] > 0
        and type(temperature) in (int, float)
        and 0 <= temperature <= 2
        and (provider != "dashscope" or temperature < 2)
        and math.isfinite(temperature)
        and params["response_format"] == "json_object"
        and _base_parameters(params)
    )


def supports_source(params, source):
    return valid_parameters(params) and (
        (source == QWEN_SOURCE and params["provider"] == "dashscope")
        or (source == DEEPSEEK_SOURCE and params["provider"] == "deepseek")
    )


def valid_consumption(source, consumption, requirement, upstream, payload):
    from app.evaluation.speaking_text_contract import SOURCE
    from app.evaluation.speaking_text_contract import VERSION as BUSINESS_VERSION
    from app.evaluation.speaking_text_contract import valid_consumption as business_consumption

    if not supports_source(requirement.get("parameters"), requirement.get("source")):
        return False
    business = {
        **requirement,
        "source": SOURCE,
        "version": BUSINESS_VERSION,
        "parameters": {
            k: requirement["parameters"][k] for k in ("prompt_version", "prompt_sha256")
        },
    }
    return business_consumption(source, consumption, business, upstream, payload)


def review_recommendation(payload):
    return payload["evaluation"]["review_recommended"]
