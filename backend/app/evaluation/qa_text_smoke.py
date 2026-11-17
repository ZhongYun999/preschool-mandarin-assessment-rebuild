"""Optional single paid candidate QA text call with fictional data."""

import argparse
import json
from uuid import uuid4

from app.evaluation.qa_text_contract import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    SOURCE,
    VERSION,
    valid_parameters,
)
from app.evaluation.qwen_qa_text import QwenQAText
from app.evaluation.worker import InputSnapshot, KnownFailure


def main():
    parser = argparse.ArgumentParser(description="Optional paid candidate QA text smoke test")
    parser.add_argument("--confirm-paid-call", action="store_true")
    parser.add_argument(
        "--region", choices=("cn-beijing", "ap-southeast-1", "us-east-1"), required=True
    )
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("explicit --confirm-paid-call is required")
    p = {
        "provider": "dashscope",
        "model": "qwen-plus",
        "region": args.region,
        "workspace": args.workspace,
        "request_version": "dashscope-chat-json-v1",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "response_format": "json_object",
        "temperature": 0.2,
    }
    if not valid_parameters(p):
        parser.error("invalid workspace or configuration")
    from app.evaluation.qa_text_contract import PROMPT

    submission, revision = str(uuid4()), str(uuid4())
    requirement = {
        "source": SOURCE,
        "version": VERSION,
        "parameters": p,
        "depends_on": {"requirement_index": 0, "source": "synthetic-asr", "version": "1"},
    }
    inputs = InputSnapshot(
        refs={
            "submission_id": submission,
            "task_revision_id": revision,
            "endpoint": f"https://{args.workspace}.{args.region}.maas.aliyuncs.com/api/v1",
            "asr": {
                "source": "synthetic-asr",
                "version": "1",
                "evaluation_id": str(uuid4()),
                "submission_id": submission,
                "task_revision_id": revision,
            },
        },
        payload={
            "system_prompt": PROMPT,
            "context": {
                "grade_level": "middle",
                "prompt_text": "小猫在做什么？",
                "material_description": "一只猫趴在垫子上。",
                "reference_answer": "小猫在休息。",
                "scoring_instruction": None,
                "question_has_image": False,
            },
            "asr": {"status": "TEXT", "text": "小猫在睡觉。"},
        },
    )
    adapter = QwenQAText()
    try:
        result = adapter.evaluate(inputs, requirement)
    except KnownFailure as exc:
        print(json.dumps({"success": False, "error_code": exc.code}))
        return 1
    except Exception:  # noqa: BLE001 - never expose provider exceptions
        print(json.dumps({"success": False, "error_code": "EXTERNAL_OUTCOME_UNKNOWN"}))
        return 1
    valid = adapter.validate_result(result, requirement)
    print(
        json.dumps(
            {
                "success": valid,
                "model": result.model,
                "contract_version": VERSION,
                "usage": result.payload.get("usage", {}),
            }
        )
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
