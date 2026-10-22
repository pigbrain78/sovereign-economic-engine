import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from core.ledger import Ledger
from pocketsre.runbook import Runbook, RunbookRejected, Step
from pocketsre.triage import Alert, rank
from synctrace.chaos import Replica, Write, deliver, divergence, heal, scenario_partition, scenario_random_drop


def test_ledger_chain_verifies_and_detects_tamper(tmp_path):
    db = tmp_path / "l.db"
    ledger = Ledger(str(db))
    ledger.append("A", {"x": 1})
    ledger.append("B", {"x": 2})
    assert ledger.verify()
    ledger.db.execute("UPDATE events SET payload='{\"x\":9}' WHERE seq=1")
    ledger.db.commit()
    assert not ledger.verify()


def test_lww_converges_when_all_writes_delivered():
    writes = [Write("a", "k", 1, 1), Write("b", "k", 2, 2)]
    replicas = [Replica("r1"), Replica("r2")]
    deliver(writes, replicas)
    assert divergence(replicas) == {}
    assert replicas[0].state["k"][2] == 2


def test_dropped_write_causes_divergence():
    writes = [Write("a", "k", 1, 1), Write("b", "k", 2, 2)]
    replicas = [Replica("r1"), Replica("r2")]
    deliver(writes, replicas)
    replicas[1].state.pop("k")
    assert "k" in divergence(replicas)


def test_partition_is_deterministic_for_seed():
    writes = [Write("a", "k", 1, 1), Write("c", "j", 2, 2)]
    r1 = [Replica(n) for n in "wxyz"]
    r2 = [Replica(n) for n in "wxyz"]
    assert scenario_partition(writes, r1, 7) == scenario_partition(writes, r2, 7)


def test_destructive_step_needs_named_approver(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    rb = Runbook("restart", [Step("drain", "drain node", lambda c: {"ok": True}, destructive=True)], ledger)
    with pytest.raises(RunbookRejected):
        rb.run("drain", {})
    with pytest.raises(RunbookRejected):
        rb.run("drain", {}, {"step_id": "drain", "runbook_digest": rb.digest(), "approver": "system"})
    assert rb.run("drain", {}, {"step_id": "drain", "runbook_digest": rb.digest(), "approver": "ana"}) == {"ok": True}


def test_triage_ranks_user_facing_critical_first():
    ranked = rank([
        Alert("1", "db", "low", "disk", 5),
        Alert("2", "api", "critical", "5xx", 3, user_facing=True),
        Alert("3", "job", "high", "queue", 200),
    ])
    assert [r["alert_id"] for r in ranked] == ["2", "3", "1"]


def test_keyed_ledger_rejects_resealed_chain(tmp_path):
    db = str(tmp_path / "k.db")
    ledger = Ledger(db, key=b"secret")
    ledger.append("A", {"x": 1})
    ledger.append("B", {"x": 2})
    assert ledger.verify(expected_count=2)
    # An attacker edits a row and recomputes plain hashes with no key: must fail.
    attacker = Ledger(db)
    attacker.db.execute("UPDATE events SET payload='{\"x\":9}' WHERE seq=1")
    attacker.db.commit()
    assert not Ledger(db, key=b"secret").verify()


def test_ledger_detects_middle_removal_and_truncation(tmp_path):
    db = str(tmp_path / "t.db")
    ledger = Ledger(db, key=b"secret")
    for i in range(3):
        ledger.append("E", {"i": i})
    head = ledger.head()
    ledger.db.execute("DELETE FROM events WHERE seq=2")
    ledger.db.commit()
    assert not ledger.verify()  # seq gap
    ledger2 = Ledger(str(tmp_path / "u.db"), key=b"secret")
    for i in range(3):
        ledger2.append("E", {"i": i})
    ledger2.db.execute("DELETE FROM events WHERE seq=3")
    ledger2.db.commit()
    assert ledger2.verify()  # a tail cut is invisible to the chain alone...
    assert not ledger2.verify(expected_count=3)  # ...unless the count is anchored
    assert not ledger2.verify(expected_head=head)


def test_partition_then_heal_converges():
    writes = [Write("r1", "k", "left", 1), Write("r2", "k", "right", 2)]
    replicas = [Replica("r1"), Replica("r2")]
    scenario_partition(writes, replicas, seed=5)
    assert divergence(replicas) != {}  # each side only has its own write
    heal(writes, replicas)
    assert divergence(replicas) == {}
    assert replicas[0].state["k"][2] == "right"


def test_random_drop_then_heal_converges_for_many_seeds():
    writes = [Write(f"n{i % 3}", f"k{i % 4}", i, i + 1) for i in range(40)]
    for seed in range(25):
        replicas = [Replica(f"r{j}") for j in range(3)]
        scenario_random_drop(writes, replicas, seed=seed, rate=0.4)
        heal(writes, replicas)
        assert divergence(replicas) == {}, f"seed {seed} did not converge"
