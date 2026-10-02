from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("dag_orchestrator")

try:  # Optional dependency; decomposition remains fail-closed without it.
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore

try:  # pragma: no cover - environment dependent
    llm_client = OpenAI() if OpenAI is not None else None
except Exception:  # pragma: no cover - environment dependent
    llm_client = None


class TaskNode(BaseModel):
    task_id: str
    description: str
    assigned_agent: str
    dependencies: list[str] = Field(default_factory=list)
    required_permission: str


class DAGBlueprint(BaseModel):
    goal: str
    tasks: list[TaskNode]


class DAGOrchestrator:
    def __init__(self, memory_layer: Any, operator_agent: Any, reflex_agent: Any, toolsmith_agent: Any | None = None):
        self.memory = memory_layer
        self.operator = operator_agent
        self.reflex = reflex_agent
        self.toolsmith = toolsmith_agent
        self.execution_log: list[str] = []

    def decompose_goal(self, high_level_goal: str) -> DAGBlueprint:
        """Uses the LLM to break a goal into a strict dependency graph."""
        logger.info("Decomposing goal: %s", high_level_goal)
        system_prompt = """
        You are the SAOS Execution Layer. Break the user's goal into a Directed Acyclic Graph (DAG) of tasks.
        Agents available: 'operator_worker' (auth/routing), 'reflex_agent' (system/hardware), 'ralph_toolsmith' (code gen).
        Output strict JSON: { "goal": "...", "tasks": [ { "task_id": "T1", "description": "...", "assigned_agent": "...", "dependencies": [], "required_permission": "..." } ] }
        """
        if llm_client is None:
            raise ValueError("LLM client required for DAG decomposition.")

        res = llm_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": high_level_goal},
            ],
            response_format={"type": "json_object"},
        )
        blueprint_data = json.loads(res.choices[0].message.content)
        return DAGBlueprint(**blueprint_data)

    def _verify_crypto_state(self) -> bool:
        verifier = getattr(self.memory, "verify_ledger", None)
        if callable(verifier):
            return bool(verifier())
        return True

    def _dispatch_task(self, task: TaskNode) -> bool:
        agent_map = {
            "operator_worker": self.operator,
            "reflex_agent": self.reflex,
            "ralph_toolsmith": self.toolsmith,
        }
        agent = agent_map.get(task.assigned_agent)
        if agent is None:
            logger.error("No agent bound for assigned_agent=%s", task.assigned_agent)
            return False
        executor = getattr(agent, "execute_task", None)
        if callable(executor):
            return bool(executor(task))
        return True

    def execute_dag(self, blueprint: DAGBlueprint) -> bool:
        """Executes the DAG in topological order with crypto state verification at each step."""
        completed_tasks: set[str] = set()
        pending_tasks = {task.task_id: task for task in blueprint.tasks}
        logger.info("Initiating DAG Execution for %s tasks.", len(pending_tasks))

        while pending_tasks:
            ready_tasks = [
                task
                for task in pending_tasks.values()
                if all(dep in completed_tasks for dep in task.dependencies)
            ]
            if not ready_tasks:
                logger.error("DAG deadlock detected; dependencies cannot be resolved.")
                self.reflex.log_task_execution(success=False)
                return False

            for task in sorted(ready_tasks, key=lambda node: node.task_id):
                if not self._verify_crypto_state():
                    logger.critical("Cryptographic state verification failed before task %s.", task.task_id)
                    self.reflex.log_task_execution(success=False)
                    return False

                logger.info("Executing node [%s]: %s via %s", task.task_id, task.description, task.assigned_agent)
                success = self._dispatch_task(task)
                self.reflex.log_task_execution(success=success)
                if not success:
                    logger.critical("Task %s failed. Halting DAG.", task.task_id)
                    return False

                completed_tasks.add(task.task_id)
                del pending_tasks[task.task_id]
                self.execution_log.append(task.task_id)

        logger.info("DAG execution complete. All nodes resolved.")
        return True
