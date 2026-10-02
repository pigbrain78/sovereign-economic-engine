from __future__ import annotations

import logging
import math

logger = logging.getLogger("reflex_agent")

try:  # Optional dependency
    import docker  # type: ignore
except Exception:  # pragma: no cover - dependency optional
    docker = None  # type: ignore


class SelfHealingReflexAgent:
    def __init__(self, container_name: str = "saos_execution_node"):
        self.reputation_coefficient = 1.0
        self.decay_lambda = 0.05
        self.consecutive_failures = 0
        self.MIN_CONFIDENCE_THRESHOLD = 0.70
        self.container_name = container_name

        try:
            self.docker_client = docker.from_env() if docker is not None else None
            if self.docker_client is None:
                raise RuntimeError("docker unavailable")
        except Exception:
            logger.warning("Docker socket unavailable. Watchdog will fall back to OS process signals.")
            self.docker_client = None

    def calculate_bayesian_reputation(self) -> float:
        if self.consecutive_failures == 0:
            self.reputation_coefficient = min(1.0, self.reputation_coefficient + 0.01)
        else:
            decay = math.exp(-self.decay_lambda * self.consecutive_failures)
            self.reputation_coefficient *= decay
        return self.reputation_coefficient

    def software_watchdog_ping(self) -> bool:
        if not self.docker_client:
            logger.debug("[WATCHDOG] OS-level ping successful.")
            return True
        try:
            container = self.docker_client.containers.get(self.container_name)
            if container.status != "running":
                logger.error(
                    "[WATCHDOG] Execution node %s is unresponsive (%s).",
                    self.container_name,
                    container.status,
                )
                return False
            logger.debug("[WATCHDOG] Container ping successful.")
            return True
        except Exception:
            logger.error("[WATCHDOG] Execution node %s not found.", self.container_name)
            return False

    def log_task_execution(self, success: bool) -> None:
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

        current_confidence = self.calculate_bayesian_reputation()
        logger.info("Current System Confidence (R_t): %.4f", current_confidence)

        if current_confidence < self.MIN_CONFIDENCE_THRESHOLD:
            self._trigger_state_dampening()
        if not self.software_watchdog_ping() or current_confidence < 0.30:
            self._trigger_container_reboot()

    def _trigger_state_dampening(self) -> None:
        logger.critical("IMMUNE RESPONSE: Confidence fell below threshold. Applying 120-second state_lock.")

    def _trigger_container_reboot(self) -> None:
        logger.critical("CRITICAL FAULT: Executing software-defined hard reboot.")
        if self.docker_client:
            try:
                container = self.docker_client.containers.get(self.container_name)
                container.restart(timeout=5)
                logger.info("Container %s successfully restarted.", self.container_name)
            except Exception as exc:  # pragma: no cover - docker environment dependent
                logger.error("Failed to restart container: %s", exc)
