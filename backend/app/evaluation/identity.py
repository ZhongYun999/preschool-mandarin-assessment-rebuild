"""Shared input identity primitives for evaluation evidence."""

import json
from typing import Any


class InputIdentityConflict(ValueError):
    """A previously frozen evaluation input cannot be safely reused."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
