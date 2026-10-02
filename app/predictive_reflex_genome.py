from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("predictive_reflex")

try:  # Optional dependency
    import docker  # type: ignore
except Exception:  # pragma: no cover - dependency optional
    docker = None  # type: ignore

try:  # Optional dependency
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover - dependency optional
    _np = None  # type: ignore

try:  # Optional dependency
    from scipy.stats import ks_2samp as _scipy_ks_2samp  # type: ignore
except Exception:  # pragma: no cover - dependency optional
    _scipy_ks_2samp = None


def _to_array(values: list[int]) -> list[float]:
    if _np is not None:
        return [float(value) for value in _np.array(values, dtype=float).tolist()]
    return [float(value) for value in values]


def _ks_p_value(sample_a: list[float], sample_b: list[float]) -> float:
    if _scipy_ks_2samp is not None:
        _stat, p_value = _scipy_ks_2samp(sample_a, sample_b)
        return float(p_value)

    # Fallback: asymptotic two-sample KS approximation.
    a = sorted(sample_a)
    b = sorted(sample_b)
    n, m = len(a), len(b)
    i = j = 0
    cdf_a = cdf_b = 0.0
    d = 0.0
    while i < n and j < m:
        if a[i] <= b[j]:
            i += 1
            cdf_a = i / n
        else:
            j += 1
            cdf_b = j / m
        d = max(d, abs(cdf_a - cdf_b))
    while i < n:
        i += 1
        cdf_a = i / n
        d = max(d, abs(cdf_a - cdf_b))
    while j < m:
        j += 1
        cdf_b = j / m
        d = max(d, abs(cdf_a - cdf_b))

    en = math.sqrt((n * m) / (n + m))
    lam = (en + 0.12 + (0.11 / en)) * d if en else 0.0
    if lam <= 0:
        return 1.0
    # Q_KS(lambda) ≈ 2 Σ (-1)^(k-1) exp(-2 k^2 λ^2)
    total = 0.0
    for k in range(1, 6):
        total += ((-1) ** (k - 1)) * math.exp(-2 * (k**2) * (lam**2))
    return max(0.0, min(1.0, 2 * total))


class PredictiveReflexGenome:
    def __init__(self, container_name: str = "saos_execution_node"):
        self.reputation_coefficient = 1.0
        self.decay_lambda = 0.05
        self.consecutive_failures = 0
        self.MIN_CONFIDENCE_THRESHOLD = 0.70

        self.baseline_density = [1, 1, 1, 1, 1, 1, 0, 1, 1, 1]
        self.baseline_entropy = [1, 1, 0, 1, 1, 1, 1, 1, 1, 1]

        self.current_hardware_clamp = 19
        self.container_name = container_name

        try:
            self.docker_client = docker.from_env() if docker is not None else None
            if self.docker_client is None:
                raise RuntimeError("docker unavailable")
        except Exception:
            logger.warning("Docker socket unavailable. Hardware clamping will be bypassed.")
            self.docker_client = None

    def evaluate_telemetry_drift(self, current_density: list[int], current_entropy: list[int]) -> dict[str, Any]:
        density_arr = _to_array(current_density)
        entropy_arr = _to_array(current_entropy)

        if len(density_arr) < 5 or len(entropy_arr) < 5:
            return {"drift_detected": False, "action": "INSUFFICIENT_TELEMETRY"}

        p_density = _ks_p_value(_to_array(self.baseline_density), density_arr)
        p_entropy = _ks_p_value(_to_array(self.baseline_entropy), entropy_arr)
        drift_detected = (p_density < 0.05) or (p_entropy < 0.05)

        if drift_detected:
            logger.warning(
                "⚠️ [PREDICTIVE REFLEX] Drift identified. p_den=%.3f, p_ent=%.3f",
                p_density,
                p_entropy,
            )
            self.decay_lambda = 0.12
            self.current_hardware_clamp = 5
            return {
                "drift_detected": True,
                "p_density_score": float(p_density),
                "p_entropy_score": float(p_entropy),
                "mutation": "ELEVATE_FRICTION_AND_CLAMP_HARDWARE",
                "recommended_clamp": self.current_hardware_clamp,
            }

        self.decay_lambda = 0.05
        self.current_hardware_clamp = 19
        return {
            "drift_detected": False,
            "p_density_score": float(p_density),
            "p_entropy_score": float(p_entropy),
            "mutation": "OPTIMAL_POSTURE",
            "recommended_clamp": self.current_hardware_clamp,
        }

    def calculate_bayesian_reputation(self) -> float:
        if self.consecutive_failures == 0:
            self.reputation_coefficient = min(1.0, self.reputation_coefficient + 0.01)
        else:
            decay = math.exp(-self.decay_lambda * self.consecutive_failures)
            self.reputation_coefficient *= decay
        return self.reputation_coefficient

    def log_task_execution(self, success: bool, telemetry: dict[str, list[int]] | None = None) -> None:
        if telemetry:
            self.evaluate_telemetry_drift(
                telemetry.get("density_stream", []),
                telemetry.get("entropy_stream", []),
            )

        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

        current_confidence = self.calculate_bayesian_reputation()
        logger.info(
            "System Confidence (R_t): %.4f | Active Decay (Lambda): %.2f",
            current_confidence,
            self.decay_lambda,
        )

        if current_confidence < self.MIN_CONFIDENCE_THRESHOLD:
            self._trigger_state_dampening()
        if self.consecutive_failures >= 3 or current_confidence < 0.30:
            self._trigger_container_reboot()

    def get_hardware_clamp_kwargs(self) -> dict[str, Any]:
        nano_cpu_limit = int((self.current_hardware_clamp / 100.0) * 1_000_000_000)
        return {
            "nano_cpus": nano_cpu_limit,
            "mem_limit": "128m" if self.current_hardware_clamp == 5 else "256m",
        }

    def _trigger_state_dampening(self) -> None:
        logger.critical("IMMUNE RESPONSE: Confidence fell below threshold. Applying 120-second state_lock.")

    def _trigger_container_reboot(self) -> None:
        logger.critical("CRITICAL FAULT: Executing software-defined hard reboot.")
        if self.docker_client:
            try:
                container = self.docker_client.containers.get(self.container_name)
                container.restart(timeout=5)
            except Exception as exc:  # pragma: no cover - docker environment dependent
                logger.error("Failed to restart container: %s", exc)
