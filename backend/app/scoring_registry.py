"""Static scoring and validated evaluation contracts; production has no entries yet."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


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


class Registry:
    def __init__(
        self, plugins: Sequence[ScoringPlugin] = (), contracts: Sequence[EvaluationContract] = ()
    ):
        self._plugins = {(p.plugin_id, p.implementation_version): p for p in plugins}
        self._contracts = {(c.source, c.version): c for c in contracts}
        self._requirements = {}
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

    def supports(self, task_type: str, requirement: Mapping[str, Any]) -> bool:
        if set(requirement) != {"source", "version", "parameters"}:
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
        return (
            contract is not None
            and task_type in contract.task_types
            and contract.validate_parameters(params)
        )


PRODUCTION_REGISTRY = Registry()
