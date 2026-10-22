"""Deterministic chaos scenarios over a replication log.

A replication log is a list of writes. Scenarios change how those writes reach
replicas. Given the same seed, a scenario always makes the same choices, so a
failing run can be replayed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Write:
    node: str
    key: str
    value: Any
    clock: int


@dataclass
class Replica:
    name: str
    state: dict[str, tuple[int, str, Any]] = field(default_factory=dict)

    def apply(self, write: Write) -> None:
        # Last-writer-wins by (clock, node): a deterministic total order.
        current = self.state.get(write.key)
        if current is None or (write.clock, write.node) > (current[0], current[1]):
            self.state[write.key] = (write.clock, write.node, write.value)


def deliver(writes: list[Write], replicas: list[Replica], *, drop: set[int] | None = None,
            delay: dict[int, int] | None = None) -> list[tuple[int, str]]:
    """Apply writes to every replica in order. drop: indices never delivered.
    delay: index -> how many later writes it is held behind. Returns the delivery plan."""
    drop = drop or set()
    delay = delay or {}
    order = list(range(len(writes)))
    for index in sorted(delay, reverse=True):
        steps = delay[index]
        if index in order and steps > 0:
            order.remove(index)
            position = min(len(order), order.index(index) + steps) if index in order else len(order)
            order.insert(position, index)
    plan: list[tuple[int, str]] = []
    for index in order:
        if index in drop:
            continue
        for replica in replicas:
            replica.apply(writes[index])
            plan.append((index, replica.name))
    return plan


def scenario_partition(writes: list[Write], replicas: list[Replica], seed: int) -> dict[str, Any]:
    """Split replicas into two sides. A write reaches only replicas on its node's side."""
    rng = random.Random(seed)
    names = [r.name for r in replicas]
    rng.shuffle(names)
    half = max(1, len(names) // 2)
    left, right = set(names[:half]), set(names[half:])
    for write in writes:
        side = left if write.node in left else right
        for replica in replicas:
            if replica.name in side:
                replica.apply(write)
    return {"scenario": "partition", "seed": seed, "left": sorted(left), "right": sorted(right)}


def scenario_random_drop(writes: list[Write], replicas: list[Replica], seed: int,
                         rate: float = 0.2) -> dict[str, Any]:
    rng = random.Random(seed)
    dropped = {i for i in range(len(writes)) if rng.random() < rate}
    deliver(writes, replicas, drop=dropped)
    return {"scenario": "random_drop", "seed": seed, "rate": rate, "dropped": sorted(dropped)}


def heal(writes: list[Write], replicas: list[Replica]) -> None:
    """Anti-entropy: re-send every write to every replica. Apply is idempotent (LWW),
    so writes a replica already has change nothing and missed writes fill the gaps."""
    deliver(writes, replicas)


def divergence(replicas: list[Replica]) -> dict[str, list[str]]:
    """Keys whose value differs between replicas. An empty result means converged."""
    keys: set[str] = set()
    for replica in replicas:
        keys |= set(replica.state)
    report: dict[str, list[str]] = {}
    for key in sorted(keys):
        views = {r.name: repr(r.state.get(key)) for r in replicas}
        if len(set(views.values())) > 1:
            report[key] = sorted(f"{name}={view}" for name, view in views.items())
    return report
