from __future__ import annotations

from app.dag_orchestrator import DAGBlueprint, DAGOrchestrator, TaskNode


class DummyMemory:
    def __init__(self, verified: bool = True):
        self.verified = verified

    def verify_ledger(self) -> bool:
        return self.verified


class DummyAgent:
    def __init__(self, success: bool = True):
        self.success = success
        self.seen: list[str] = []

    def execute_task(self, task: TaskNode) -> bool:
        self.seen.append(task.task_id)
        return self.success


class DummyReflex(DummyAgent):
    def __init__(self, success: bool = True):
        super().__init__(success=success)
        self.events: list[bool] = []

    def log_task_execution(self, *, success: bool) -> None:
        self.events.append(success)


def test_execute_dag_topological_order():
    memory = DummyMemory(verified=True)
    operator = DummyAgent(success=True)
    reflex = DummyReflex(success=True)
    toolsmith = DummyAgent(success=True)
    orchestrator = DAGOrchestrator(memory, operator, reflex, toolsmith)
    blueprint = DAGBlueprint(
        goal="deliver feature",
        tasks=[
            TaskNode(task_id="T2", description="build", assigned_agent="ralph_toolsmith", dependencies=["T1"], required_permission="write"),
            TaskNode(task_id="T1", description="route", assigned_agent="operator_worker", dependencies=[], required_permission="read"),
            TaskNode(task_id="T3", description="verify", assigned_agent="reflex_agent", dependencies=["T2"], required_permission="admin"),
        ],
    )

    assert orchestrator.execute_dag(blueprint) is True
    assert orchestrator.execution_log == ["T1", "T2", "T3"]
    assert reflex.events == [True, True, True]


def test_execute_dag_fails_on_deadlock():
    memory = DummyMemory(verified=True)
    operator = DummyAgent(success=True)
    reflex = DummyReflex(success=True)
    orchestrator = DAGOrchestrator(memory, operator, reflex)
    blueprint = DAGBlueprint(
        goal="cycle",
        tasks=[
            TaskNode(task_id="T1", description="a", assigned_agent="operator_worker", dependencies=["T2"], required_permission="read"),
            TaskNode(task_id="T2", description="b", assigned_agent="operator_worker", dependencies=["T1"], required_permission="read"),
        ],
    )

    assert orchestrator.execute_dag(blueprint) is False
    assert reflex.events[-1] is False


def test_execute_dag_fails_closed_on_crypto_verification():
    memory = DummyMemory(verified=False)
    operator = DummyAgent(success=True)
    reflex = DummyReflex(success=True)
    orchestrator = DAGOrchestrator(memory, operator, reflex)
    blueprint = DAGBlueprint(
        goal="single",
        tasks=[
            TaskNode(task_id="T1", description="a", assigned_agent="operator_worker", dependencies=[], required_permission="read"),
        ],
    )

    assert orchestrator.execute_dag(blueprint) is False
    assert orchestrator.execution_log == []
    assert reflex.events == [False]
