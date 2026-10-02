from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel

logger = logging.getLogger("operator_worker")


class MCPPermission(str):
    READ_STATE = "read_state"
    EXECUTE_TOOL = "execute_tool"
    ALLOCATE_CAPITAL = "allocate_capital"


class SharedMCPContextGrant(BaseModel):
    host_node_id: str
    guest_node_id: str
    granted_scopes: list[str]
    expires_at: datetime
    cryptographic_signature: str


class NodeTrustRegistry:
    def __init__(self):
        self._registry = {
            "FIRM_A_AGENT_02": 0.95,
            "FIRM_C_AGENT_01": 0.45,
        }
        self.MINIMUM_TRUST_THRESHOLD = 0.80

    def get_trust_score(self, node_id: str) -> float:
        return self._registry.get(node_id, 0.0)


class OperatorWorkerAgent:
    def __init__(self, trust_registry: NodeTrustRegistry):
        self.trust_registry = trust_registry

    def mcp_proxy_gatekeeper(self, grant: SharedMCPContextGrant, requested_tool: str) -> bool:
        logger.info("Evaluating Federated Handshake from %s...", grant.guest_node_id)

        trust = self.trust_registry.get_trust_score(grant.guest_node_id)
        if trust < self.trust_registry.MINIMUM_TRUST_THRESHOLD:
            logger.error("HALT: Guest %s trust score (%s) below threshold.", grant.guest_node_id, trust)
            return False

        now_utc = datetime.now(timezone.utc)
        expires_at = grant.expires_at if grant.expires_at.tzinfo else grant.expires_at.replace(tzinfo=timezone.utc)
        if now_utc > expires_at:
            logger.error("HALT: Context grant expired.")
            return False

        if MCPPermission.EXECUTE_TOOL not in grant.granted_scopes:
            logger.error("HALT: Unauthorized scope for tool %s.", requested_tool)
            return False

        logger.info("Federated Handshake Approved. Routing tool %s.", requested_tool)
        return True
