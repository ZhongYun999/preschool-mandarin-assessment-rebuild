"""Static scoring and validated evaluation contracts."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.evaluation.asr_contract import SOURCE as QWEN_ASR_SOURCE
from app.evaluation.asr_contract import VERSION as QWEN_ASR_VERSION
from app.evaluation.asr_contract import recognition_result as qwen_asr_recognition_result
from app.evaluation.asr_contract import valid_parameters as valid_qwen_asr_parameters


class ScoringUnavailable(ValueError):
    """The frozen scoring or evaluation capability is unavailable."""


class ScoringPlugin(Protocol):
    plugin_id: str
    implementation_version: str
    display_name: str
    selectable: bool

    @property
    def evaluation_requirements(self) -> Mapping[str, Sequence[Mapping[str, Any]]]: ...

    def score_task(self, task_context: Any, raw_evaluations: Any, submission_facts: Any) -> Any: ...
    def score_paper(self, paper_context: Any, task_scores: Any) -> Any: ...


@dataclass(frozen=True)
class EvaluationContract:
    source: str
    version: str
    task_types: frozenset[str]
    validate_parameters: Callable[[Mapping[str, Any]], bool]
    review_recommendation: Callable[[Mapping[str, Any]], bool] | bool | None = None
    recognition_result: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    accepts_no_text: bool | None = None
    speaking_consumption_validator: (
        Callable[
            [
                Mapping[str, Any],
                Mapping[str, Any],
                Mapping[str, Any],
                Mapping[str, Any] | None,
                Mapping[str, Any] | None,
            ],
            bool,
        ]
        | None
    ) = None
    speaking_recognition_result: Callable[..., Mapping[str, Any]] | None = None


class Registry:
    def __init__(
        self, plugins: Sequence[ScoringPlugin] = (), contracts: Sequence[EvaluationContract] = ()
    ):
        self._plugins = {(p.plugin_id, p.implementation_version): p for p in plugins}
        self._contracts = {(c.source, c.version): c for c in contracts}
        self._requirements = {}
        if any(
            (c.recognition_result is not None and not callable(c.recognition_result))
            or (
                c.speaking_recognition_result is not None
                and not callable(c.speaking_recognition_result)
            )
            or (c.accepts_no_text is not None and type(c.accepts_no_text) is not bool)
            or (
                c.speaking_consumption_validator is not None
                and not callable(c.speaking_consumption_validator)
            )
            for c in contracts
        ):
            raise ValueError("Invalid recognition dependency contract")
        if len(self._plugins) != len(plugins) or len(self._contracts) != len(contracts):
            raise ValueError("Duplicate registry identity")
        for plugin in plugins:
            if (
                not plugin.plugin_id
                or not plugin.implementation_version
                or not plugin.display_name
                or not callable(getattr(plugin, "score_task", None))
                or not callable(getattr(plugin, "score_paper", None))
            ):
                raise ValueError("Invalid scoring plugin contract")
            self._requirements[(plugin.plugin_id, plugin.implementation_version)] = json.dumps(
                plugin.evaluation_requirements, allow_nan=False
            )

    def resolve(self, plugin_id: str, version: str) -> ScoringPlugin | None:
        return self._plugins.get((plugin_id, version))

    def requirements_for(self, plugin_id: str, version: str, task_type: str) -> list | None:
        requirements = json.loads(self._requirements[(plugin_id, version)])
        return requirements.get(task_type)

    def task_types_for(self, plugin_id: str, version: str) -> set[str]:
        return set(json.loads(self._requirements[(plugin_id, version)]))

    def supports(self, task_type: str, requirement: Mapping[str, Any]) -> bool:
        if set(requirement) not in (
            {"source", "version", "parameters"},
            {"source", "version", "parameters", "depends_on"},
        ):
            return False
        source, version, params = (
            requirement["source"],
            requirement["version"],
            requirement["parameters"],
        )
        if (
            not isinstance(source, str)
            or not isinstance(version, str)
            or not isinstance(params, dict)
        ):
            return False
        contract = self._contracts.get((source, version))
        if (
            contract is None
            or task_type not in contract.task_types
            or (task_type == "speaking" and not callable(contract.speaking_consumption_validator))
            or not contract.validate_parameters(params)
        ):
            return False
        dependent = "depends_on" in requirement
        return (
            (type(contract.accepts_no_text) is bool)
            if dependent
            else contract.accepts_no_text is None
        )

    def valid_dependency(self, task_type: str, requirements: list, index: int) -> bool:
        req = requirements[index]
        if "depends_on" not in req:
            return True
        ref = req["depends_on"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"requirement_index", "source", "version"}
            or type(ref["requirement_index"]) is not int
            or not isinstance(ref["source"], str)
            or not isinstance(ref["version"], str)
        ):
            return False
        upstream_index = ref["requirement_index"]
        if upstream_index < 0 or upstream_index >= len(requirements) or upstream_index == index:
            return False
        upstream = requirements[upstream_index]
        if (
            not isinstance(upstream, dict)
            or "depends_on" in upstream
            or (upstream.get("source"), upstream.get("version")) != (ref["source"], ref["version"])
            or not self.supports(task_type, upstream)
        ):
            return False
        contract = self._contracts.get((ref["source"], ref["version"]))
        return contract is not None and (
            callable(contract.recognition_result)
            or (task_type == "speaking" and callable(contract.speaking_recognition_result))
        )

    def valid_plan(self, task_type: str, requirements: Any) -> bool:
        return isinstance(requirements, list) and all(
            isinstance(req, dict)
            and self.supports(task_type, req)
            and self.valid_dependency(task_type, requirements, index)
            for index, req in enumerate(requirements)
        )

    def recognition_contract(self, requirement: Mapping[str, Any]) -> bool:
        contract = self._contracts.get((requirement["source"], requirement["version"]))
        return bool(
            contract and (contract.recognition_result or contract.speaking_recognition_result)
        )

    def validate_speaking_consumption(
        self, requirement, source, consumption, upstream=None, payload=None
    ):
        contract = self._contracts.get((requirement["source"], requirement["version"]))
        return bool(
            contract
            and contract.speaking_consumption_validator
            and isinstance(consumption, dict)
            and consumption
            and contract.speaking_consumption_validator(
                source, consumption, requirement, upstream, payload
            )
            is True
        )

    def recognition_result(
        self,
        requirement: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        speaking_source=None,
        speaking_consumption=None,
        provider=None,
        model=None,
    ) -> dict:
        contract = self._contracts[(requirement["source"], requirement["version"])]
        if speaking_source is not None and contract.speaking_recognition_result is not None:
            if speaking_consumption is None:
                raise ValueError("Speaking recognition requires frozen source and consumption")
            return contract.speaking_recognition_result(
                payload, requirement, speaking_source, speaking_consumption, provider, model
            )
        if contract.recognition_result is None:
            raise ValueError("Recognition result is incompatible with input")
        result = contract.recognition_result(payload)
        if (
            not isinstance(result, dict)
            or set(result) != {"status", "text"}
            or not (
                (
                    result["status"] == "TEXT"
                    and isinstance(result["text"], str)
                    and result["text"].strip()
                )
                or (result["status"] == "NO_TEXT" and result["text"] is None)
            )
        ):
            raise ValueError("Invalid recognition result")
        return result

    def accepts_no_text(self, requirement: Mapping[str, Any]) -> bool:
        contract = self._contracts[(requirement["source"], requirement["version"])]
        return contract.accepts_no_text is True

    def review_recommendation(
        self, requirement: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> bool | None:
        contract = self._contracts.get((requirement["source"], requirement["version"]))
        if contract is None:
            return None
        if contract.review_recommendation is None:
            return None
        if contract.review_recommendation is False:
            return False
        if not callable(contract.review_recommendation):
            raise TypeError("Invalid review recommendation contract")
        result = contract.review_recommendation(payload)
        if type(result) is not bool:
            raise ValueError("Review recommendation contract must return a boolean")
        return result


QWEN_ASR_CONTRACT = EvaluationContract(
    QWEN_ASR_SOURCE,
    QWEN_ASR_VERSION,
    frozenset({"qa"}),
    valid_qwen_asr_parameters,
    False,
    recognition_result=qwen_asr_recognition_result,
)
PRODUCTION_REGISTRY = Registry(contracts=[QWEN_ASR_CONTRACT])
