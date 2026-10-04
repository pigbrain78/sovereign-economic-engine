from __future__ import annotations

import hashlib
import mmap
import os
import struct
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ContainerLimits:
    memory_mb: int = 512
    cpu_quota_us: int = 100_000
    cpu_period_us: int = 100_000
    pids_limit: int = 64
    read_only_rootfs: bool = True
    drop_capabilities: tuple[str, ...] = ("ALL",)
    network_disabled: bool = True


@dataclass(frozen=True)
class SandboxExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool


class ContainerDriver:
    """Builds a rootless Podman command; execution is deliberately external."""

    isolation_level = "sandbox"

    def __init__(self, limits: ContainerLimits | None = None, engine_binary: str = "podman"):
        self.limits = limits or ContainerLimits()
        self.engine_binary = engine_binary

    def build_command_args(self, image: str, command: list[str], user_uid: int = 10001) -> list[str]:
        if not image or not command or user_uid <= 0:
            raise ValueError("image, command, and a positive non-root uid are required")
        args = [self.engine_binary, "run", "--rm", "--user", str(user_uid), "--memory", f"{self.limits.memory_mb}m", "--cpus", f"{self.limits.cpu_quota_us / self.limits.cpu_period_us:g}", "--pids-limit", str(self.limits.pids_limit)]
        if self.limits.read_only_rootfs:
            args.extend(["--read-only", "--security-opt=no-new-privileges:true"])
        if self.limits.network_disabled:
            args.extend(["--network", "none"])
        for capability in self.limits.drop_capabilities:
            args.extend(["--cap-drop", capability])
        args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", image, *command])
        return args


class SubprocessDriver:
    """Process boundary only; this is not a security sandbox."""

    isolation_level = "process_boundary_only"

    def execute(self, command: list[str], timeout_s: float = 5.0, input_text: str | None = None) -> SandboxExecutionResult:
        if not command or timeout_s <= 0:
            raise ValueError("command and positive timeout are required")
        start = time.perf_counter()
        try:
            result = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=timeout_s, check=False)
            return SandboxExecutionResult(result.returncode, result.stdout, result.stderr, (time.perf_counter() - start) * 1000, False)
        except subprocess.TimeoutExpired as exc:
            return SandboxExecutionResult(-1, exc.stdout or "", exc.stderr or "Execution timed out.", (time.perf_counter() - start) * 1000, True)


@dataclass(frozen=True)
class ResourceMeterContract:
    cpu_seconds: float
    peak_memory_bytes: int
    io_read_bytes: int
    io_write_bytes: int
    estimated_cost_sats: int

    def __post_init__(self) -> None:
        if self.cpu_seconds < 0 or min(self.peak_memory_bytes, self.io_read_bytes, self.io_write_bytes, self.estimated_cost_sats) < 0:
            raise ValueError("resource quantities cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "io_read_bytes": self.io_read_bytes,
            "io_write_bytes": self.io_write_bytes,
            "estimated_cost_sats": self.estimated_cost_sats,
        }


@dataclass(frozen=True)
class ResourceUsage:
    """Measured usage only; this object deliberately contains no price."""

    cpu_seconds: Decimal
    peak_memory_bytes: int
    io_read_bytes: int
    io_write_bytes: int
    token_count: int = 0
    wall_seconds: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.cpu_seconds < 0 or self.wall_seconds < 0 or self.token_count < 0:
            raise ValueError("measured quantities cannot be negative")
        if min(self.peak_memory_bytes, self.io_read_bytes, self.io_write_bytes) < 0:
            raise ValueError("measured quantities cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_seconds": str(self.cpu_seconds),
            "peak_memory_bytes": self.peak_memory_bytes,
            "io_read_bytes": self.io_read_bytes,
            "io_write_bytes": self.io_write_bytes,
            "token_count": self.token_count,
            "wall_seconds": str(self.wall_seconds),
        }


@dataclass(frozen=True)
class PricingPolicy:
    """Converts measured usage into cost; measurement and pricing stay separate."""

    cpu_usd_per_second: Decimal = Decimal("0.000010")
    memory_usd_per_gb_second: Decimal = Decimal("0.000001")
    io_usd_per_gb: Decimal = Decimal("0.000001")
    token_usd: Decimal = Decimal("0.000001")
    wall_usd_per_second: Decimal = Decimal("0.000001")

    def price(self, usage: ResourceUsage) -> Decimal:
        memory_gb_seconds = (Decimal(usage.peak_memory_bytes) / Decimal(1024**3)) * usage.wall_seconds
        io_gb = Decimal(usage.io_read_bytes + usage.io_write_bytes) / Decimal(1024**3)
        return (usage.cpu_seconds * self.cpu_usd_per_second + memory_gb_seconds * self.memory_usd_per_gb_second + io_gb * self.io_usd_per_gb + Decimal(usage.token_count) * self.token_usd + usage.wall_seconds * self.wall_usd_per_second).quantize(Decimal("0.000001"))


HEADER_FORMAT = "<4sHHQQ"
ENTRY_FORMAT = "<QQQQ32s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
ENTRY_SIZE = struct.calcsize(ENTRY_FORMAT)


class CheckpointStore:
    """Validated binary checkpoints with an O(1) indexed lookup."""

    def __init__(self, filepath: str | Path):
        self.path = Path(filepath)
        if not self.path.exists():
            self.path.write_bytes(struct.pack(HEADER_FORMAT, b"SSCK", 1, 3, HEADER_SIZE, 0))

    def append(self, step_seq: int, data: bytes, wal_offset: int) -> None:
        if step_seq < 0 or wal_offset < 0:
            raise ValueError("step_seq and wal_offset cannot be negative")
        digest = hashlib.sha256(data).digest()
        with self.path.open("r+b") as handle:
            magic, major, minor, index_offset, count = struct.unpack(HEADER_FORMAT, handle.read(HEADER_SIZE))
            if magic != b"SSCK":
                raise ValueError("invalid checkpoint magic")
            handle.seek(index_offset)
            previous_index = handle.read(count * ENTRY_SIZE)
            handle.seek(index_offset)
            data_offset = index_offset
            handle.write(data)
            new_index_offset = handle.tell()
            handle.write(previous_index)
            handle.write(struct.pack(ENTRY_FORMAT, step_seq, data_offset, len(data), wal_offset, digest))
            handle.seek(0)
            handle.write(struct.pack(HEADER_FORMAT, magic, major, minor, new_index_offset, count + 1))

    def lookup(self, target_index: int) -> tuple[int, bytes, int, bytes] | None:
        if target_index < 0:
            return None
        with self.path.open("rb") as handle:
            if self.path.stat().st_size < HEADER_SIZE:
                return None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                magic, _major, _minor, index_offset, count = struct.unpack(HEADER_FORMAT, mapped[:HEADER_SIZE])
                if magic != b"SSCK" or target_index >= count:
                    return None
                entry_start = index_offset + target_index * ENTRY_SIZE
                entry_end = entry_start + ENTRY_SIZE
                if entry_end > len(mapped):
                    raise ValueError("checkpoint index is truncated")
                step, data_offset, data_length, wal_offset, digest = struct.unpack(ENTRY_FORMAT, mapped[entry_start:entry_end])
                payload_end = data_offset + data_length
                if payload_end > len(mapped):
                    raise ValueError("checkpoint payload is truncated")
                payload = bytes(mapped[data_offset:payload_end])
                if hashlib.sha256(payload).digest() != digest:
                    raise ValueError("checkpoint payload digest mismatch")
                return step, payload, wal_offset, digest


class RalphRehydrationLoop:
    """Rehydrates from a checkpoint and replays only the WAL tail after its offset."""

    def __init__(self, checkpoint_path: str | Path, wal_path: str | Path):
        self.store = CheckpointStore(checkpoint_path)
        self.wal_path = Path(wal_path)

    def rehydrate(self, checkpoint_index: int = 0) -> dict[str, Any]:
        start = time.perf_counter()
        checkpoint = self.store.lookup(checkpoint_index)
        state: dict[str, Any] = {"rehydrated_step": -1, "data": b"", "wal_offset": 0, "wal_applied": 0}
        if checkpoint:
            step, payload, wal_offset, _digest = checkpoint
            state.update(rehydrated_step=step, data=payload, wal_offset=wal_offset)
        if self.wal_path.exists() and self.wal_path.stat().st_size:
            with self.wal_path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                state["wal_applied"] = self._replay_tail(mapped, state["wal_offset"], state)
        state["latency_ms"] = (time.perf_counter() - start) * 1000
        return state

    @staticmethod
    def _replay_tail(wal: mmap.mmap, offset: int, state: dict[str, Any]) -> int:
        if offset < 0 or offset > len(wal):
            raise ValueError("WAL offset outside file")
        frames = 0
        while offset < len(wal):
            if offset + 8 > len(wal):
                raise ValueError("truncated WAL frame header")
            frame_len, step_id = struct.unpack_from("<II", wal, offset)
            payload_start, payload_end = offset + 8, offset + 8 + frame_len
            if payload_end > len(wal):
                raise ValueError("truncated WAL frame payload")
            state["data"] += bytes(wal[payload_start:payload_end])
            state["rehydrated_step"] = step_id
            frames += 1
            offset = payload_end
        return frames


class LatencyTracker:
    def __init__(self, max_samples: int = 1000):
        self.samples = deque(maxlen=max_samples)

    def record(self, latency_ms: float) -> None:
        self.samples.append(float(latency_ms))

    def percentile(self, percentile: float) -> float:
        if not 0 <= percentile <= 100:
            raise ValueError("percentile must be between 0 and 100")
        if not self.samples:
            return 0.0
        values = sorted(self.samples)
        position = (len(values) - 1) * percentile / 100
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)


@dataclass
class ShardTarget:
    shard_id: str
    is_canary: bool = False
    latency_tracker: LatencyTracker = field(default_factory=LatencyTracker)


class EventSpineShardRouter:
    def __init__(self, primary_shards: list[ShardTarget], canary_shards: list[ShardTarget], canary_weight: float = 0.15):
        if not primary_shards or not 0 <= canary_weight <= 1:
            raise ValueError("at least one primary shard and a valid canary weight are required")
        self.primary_shards = primary_shards
        self.canary_shards = canary_shards
        self.canary_weight = canary_weight

    def route_key(self, key: str) -> ShardTarget:
        bucket_bytes = hashlib.blake2b(key.encode(), digest_size=8).digest()
        value = int.from_bytes(bucket_bytes, "little")
        if value % 10000 < self.canary_weight * 10000 and self.canary_shards:
            return self.canary_shards[value % len(self.canary_shards)]
        return self.primary_shards[value % len(self.primary_shards)]


class ReconciliationDaemon:
    def __init__(self, shards: Iterable[ShardTarget]):
        self.shards = list(shards)

    def export_prometheus_metrics(self) -> str:
        lines = []
        for percentile in (50, 95, 99):
            lines.extend([f"# HELP substrate_shard_latency_p{percentile} Shard {percentile}th percentile latency in milliseconds.", f"# TYPE substrate_shard_latency_p{percentile} gauge"])
        for shard in self.shards:
            label = f'shard_id="{shard.shard_id}",canary="{str(shard.is_canary).lower()}"'
            for percentile in (50, 95, 99):
                lines.append(f"substrate_shard_latency_p{percentile}{{{label}}} {shard.latency_tracker.percentile(percentile):.3f}")
        return "\n".join(lines) + "\n"
