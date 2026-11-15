"""Frozen review-policy V1 and deterministic per-task sampling."""

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


class PolicyUndetermined(ValueError):
    """The frozen policy cannot safely be interpreted by this version."""


@dataclass(frozen=True)
class SamplingDecision:
    sampled: bool
    ratio: Decimal
    source: str
    bucket: str


def _ratio(value):
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise PolicyUndetermined("Review ratios must be finite JSON numbers between 0 and 1")
    return Decimal(str(value))


def validate_policy(policy, *, task_types, paper_entry_ids):
    if policy == {"sampling": 0} and type(policy.get("sampling")) in (int, float):
        return
    if not isinstance(policy, dict) or policy.get("version") != "v1":
        raise PolicyUndetermined("Unrecognized frozen review policy version")
    if (
        set(policy) - {"version", "default_ratio", "task_type_ratios", "paper_entry_ratios"}
        or "default_ratio" not in policy
    ):
        raise PolicyUndetermined("Invalid V1 review policy fields")
    _ratio(policy["default_ratio"])
    for field, valid in (("task_type_ratios", task_types), ("paper_entry_ratios", paper_entry_ids)):
        mapping = policy.get(field, {})
        if not isinstance(mapping, dict):
            raise PolicyUndetermined(f"{field} must be an object")
        for key, value in mapping.items():
            if not isinstance(key, str) or key not in valid:
                raise PolicyUndetermined(f"Unknown {field} identity: {key}")
            _ratio(value)


def sample_task(
    policy,
    *,
    session_id: UUID,
    session_task_id: UUID,
    paper_entry_id: UUID,
    task_type: str,
    task_types,
    paper_entry_ids,
) -> SamplingDecision:
    validate_policy(policy, task_types=task_types, paper_entry_ids=paper_entry_ids)
    if "sampling" in policy:
        ratio, source = Decimal(0), "legacy_zero"
    elif str(paper_entry_id) in policy.get("paper_entry_ratios", {}):
        ratio, source = _ratio(policy["paper_entry_ratios"][str(paper_entry_id)]), "paper_entry"
    elif task_type in policy.get("task_type_ratios", {}):
        ratio, source = _ratio(policy["task_type_ratios"][task_type]), "task_type"
    else:
        ratio, source = _ratio(policy["default_ratio"]), "default"
    digest = hashlib.sha256(
        b"vnext-review-sampling-v1\0" + session_id.bytes + session_task_id.bytes
    ).digest()
    bucket = int.from_bytes(digest, "big")
    numerator, denominator = ratio.as_integer_ratio()
    return SamplingDecision(
        bucket * denominator < numerator * (1 << 256), ratio, source, digest.hex()
    )


def review_reason(sampled, recommended):
    if sampled and recommended:
        return "SAMPLED+MODEL_RECOMMENDED"
    if sampled:
        return "SAMPLED"
    if recommended:
        return "MODEL_RECOMMENDED"
    return None
