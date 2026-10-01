import struct

import pytest

from app.substrate import (
    CheckpointStore,
    ContainerDriver,
    EventSpineShardRouter,
    LatencyTracker,
    RalphRehydrationLoop,
    ReconciliationDaemon,
    ResourceMeterContract,
    ShardTarget,
)


def write_frame(handle, step: int, payload: bytes) -> int:
    offset = handle.tell()
    handle.write(struct.pack('<II', len(payload), step))
    handle.write(payload)
    return offset


def test_checkpoint_digest_and_wal_tail_replay(tmp_path):
    checkpoint_path = tmp_path / 'checkpoints.bin'
    wal_path = tmp_path / 'events.wal'
    with wal_path.open('wb') as wal:
        write_frame(wal, 1, b'-tail-')
        tail_offset = wal.tell()
        write_frame(wal, 2, b'after-checkpoint')
    CheckpointStore(checkpoint_path).append(1, b'base', tail_offset)
    state = RalphRehydrationLoop(checkpoint_path, wal_path).rehydrate()
    assert state['data'] == b'baseafter-checkpoint'
    assert state['wal_applied'] == 1
    assert state['rehydrated_step'] == 2

    with checkpoint_path.open('r+b') as corrupt:
        corrupt.seek(-1, 2)
        corrupt.write(b'x')
    with pytest.raises(ValueError, match='digest|truncated'):
        CheckpointStore(checkpoint_path).lookup(0)


def test_sandbox_command_is_fail_closed_by_construction():
    args = ContainerDriver().build_command_args('trusted-image:latest', ['python', '-c', 'print(1)'])
    assert '--network' in args and 'none' in args
    assert '--read-only' in args
    assert '--security-opt=no-new-privileges:true' in args
    assert '--cap-drop' in args and 'ALL' in args
    assert '--user' in args and '10001' in args


def test_resource_meter_rejects_negative_values():
    with pytest.raises(ValueError):
        ResourceMeterContract(0, 0, 0, 0, -1)


def test_blake2b_routing_is_deterministic_and_metrics_include_all_percentiles():
    primary = [ShardTarget('p0'), ShardTarget('p1')]
    canary = [ShardTarget('c0', is_canary=True)]
    router = EventSpineShardRouter(primary, canary, canary_weight=0.15)
    first = router.route_key('mission-123').shard_id
    assert first == router.route_key('mission-123').shard_id
    target = primary[0]
    for value in range(1, 101):
        target.latency_tracker.record(value)
    metrics = ReconciliationDaemon([target]).export_prometheus_metrics()
    assert 'substrate_shard_latency_p50' in metrics
    assert 'substrate_shard_latency_p95' in metrics
    assert 'substrate_shard_latency_p99' in metrics
