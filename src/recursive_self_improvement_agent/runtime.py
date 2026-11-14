from __future__ import annotations

import hashlib
import json
import time
from typing import Any, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import (
    AgentAnswer,
    AgentTask,
    Decision,
    RuntimeConfig,
    RunTrace,
    ToolEvent,
)

TOOL_COST_USD = 0.0
RUN_DEADLINE_SECONDS = 5.0
ALLOWED_TOOLS = frozenset({"search_evidence"})


def config_hash(config: RuntimeConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TrustedToolExecutor:
    """The executor owns task scope and tool permissions; strategies cannot widen either."""

    def __init__(
        self,
        tasks: dict[str, AgentTask],
        max_tool_calls: int = 4,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
    ) -> None:
        self._tasks = tasks
        self.max_tool_calls = max_tool_calls
        self.deadline = time.monotonic() + deadline_seconds
        self.events: list[ToolEvent] = []
        self.calls_executed = 0
        self.executed_prohibited_violations = 0

    def call(self, task_id: str, tool_name: str, arguments: dict[str, Any]) -> list[dict[str, str]]:
        if time.monotonic() >= self.deadline:
            self.events.append(
                ToolEvent(tool_name=tool_name, arguments=arguments, outcome="deadline_exceeded")
            )
            raise TimeoutError("runtime deadline exceeded")
        if self.calls_executed >= self.max_tool_calls:
            self.events.append(
                ToolEvent(tool_name=tool_name, arguments=arguments, outcome="budget_exhausted")
            )
            raise RuntimeError("tool-call budget exhausted")
        if tool_name not in ALLOWED_TOOLS:
            self.events.append(
                ToolEvent(
                    tool_name=tool_name,
                    arguments=arguments,
                    outcome="prohibited_tool_blocked",
                    blocked=True,
                )
            )
            raise PermissionError(f"tool is not permitted: {tool_name}")
        task = self._tasks.get(task_id)
        if task is None:
            self.events.append(
                ToolEvent(
                    tool_name=tool_name,
                    arguments=arguments,
                    outcome="out_of_scope_blocked",
                    blocked=True,
                )
            )
            raise PermissionError("task is outside this runtime's scope")
        limit = arguments.get("limit")
        query = arguments.get("query")
        if type(limit) is not int or not 1 <= limit <= 3 or not isinstance(query, str):
            self.events.append(
                ToolEvent(tool_name=tool_name, arguments=arguments, outcome="invalid_arguments")
            )
            raise ValueError("search_evidence requires a string query and limit between 1 and 3")

        self.calls_executed += 1
        lowered_query = query.casefold()
        qualifier = next((term for term in ("current", "archive") if term in lowered_query), None)

        def matches(item: Any) -> bool:
            has_subject = any(
                term.casefold() not in {"current", "archive"} and term.casefold() in lowered_query
                for term in item.query_terms
            )
            has_qualifier = qualifier is None or qualifier in item.query_terms
            return has_subject and has_qualifier

        ranked = sorted(
            (item for item in task.documents if matches(item)),
            key=lambda item: item.reference,
        )
        result = [
            {
                "reference": item.reference,
                "content": item.content,
                "stance": item.stance or "unknown",
            }
            for item in ranked[:limit]
        ]
        self.events.append(
            ToolEvent(
                tool_name=tool_name,
                arguments={"task_id": task_id, "query": query, "limit": limit},
                outcome="ok",
                result_references=[item["reference"] for item in result],
            )
        )
        return result


class RuntimeState(TypedDict):
    task: AgentTask
    config: RuntimeConfig
    executor: TrustedToolExecutor
    evidence: list[dict[str, str]]
    verified_evidence: NotRequired[list[dict[str, str]]]
    answer: NotRequired[AgentAnswer]
    termination: NotRequired[str]


def _build_graph():
    graph = StateGraph(RuntimeState)
    graph.add_node("retrieve", _retrieve)
    graph.add_node("verify", _verify)
    graph.add_node("answer", _answer)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "verify")
    graph.add_edge("verify", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


def _retrieve(state: RuntimeState) -> dict[str, Any]:
    strategy = state["config"].retrieval_strategy
    limit = 1 if strategy == "single_pass" else state["config"].max_evidence_items
    query = state["task"].prompt
    if strategy == "current_first":
        query += " current"
    evidence = state["executor"].call(
        state["task"].task_id,
        "search_evidence",
        {"query": query, "limit": limit},
    )
    return {"evidence": evidence}


def _verify(state: RuntimeState) -> dict[str, Any]:
    if not state["config"].verify_before_answer:
        return {"verified_evidence": state["evidence"]}
    verification = state["executor"].call(
        state["task"].task_id,
        "search_evidence",
        {"query": state["task"].prompt + " current", "limit": state["config"].max_evidence_items},
    )
    return {"verified_evidence": verification or state["evidence"]}


def _answer(state: RuntimeState) -> dict[str, Any]:
    evidence = state.get("verified_evidence", state["evidence"])
    stances = {
        item["stance"] for item in evidence if item["stance"] in {"supported", "contradicted"}
    }
    if len(stances) == 1:
        decision = Decision(next(iter(stances)))
    else:
        decision = Decision.INSUFFICIENT
    refs = [item["reference"] for item in evidence]
    return {
        "answer": AgentAnswer(
            decision=decision,
            references=refs,
            abstained=decision == Decision.INSUFFICIENT,
        ),
        "termination": "completed",
    }


_GRAPH = _build_graph()


def run_task(
    task: AgentTask,
    config: RuntimeConfig,
    *,
    max_tool_calls: int = 4,
    prohibited_tool_attempt: bool = False,
) -> RunTrace:
    executor = TrustedToolExecutor({task.task_id: task}, max_tool_calls=max_tool_calls)
    if prohibited_tool_attempt:
        try:
            executor.call(task.task_id, "read_evaluator_labels", {"task_id": task.task_id})
        except PermissionError:
            pass
    try:
        output = _GRAPH.invoke(
            {
                "task": task,
                "config": config,
                "executor": executor,
                "evidence": [],
            }
        )
        answer = output["answer"]
        termination = output["termination"]
    except RuntimeError:
        answer = AgentAnswer(decision=Decision.INSUFFICIENT, references=[], abstained=True)
        termination = "budget_exhausted"
    except TimeoutError:
        answer = AgentAnswer(decision=Decision.INSUFFICIENT, references=[], abstained=True)
        termination = "deadline_exceeded"
    except (PermissionError, ValueError):
        answer = AgentAnswer(decision=Decision.INSUFFICIENT, references=[], abstained=True)
        termination = "tool_blocked"
    return RunTrace(
        task_id=task.task_id,
        family=task.family,
        config_hash=config_hash(config),
        tool_events=tuple(executor.events),
        answer=answer,
        termination=termination,
        tool_calls=executor.calls_executed,
        blocked_prohibited_attempts=sum(event.blocked for event in executor.events),
        executed_prohibited_violations=executor.executed_prohibited_violations,
        cost_usd=TOOL_COST_USD * executor.calls_executed,
    )
