"""Server-owned context made available to Pipecat function handlers."""

from dataclasses import dataclass, field
from typing import Any

from agent.config import DEFAULT_AI_GATEWAY


@dataclass(frozen=True)
class AgentResources:
    env: Any
    call_id: str
    owner_id: str
    # Set from the signed-in session, never from the model. Every tool
    # scopes its reads and writes to this customer.
    customer_id: str
    # The call's event journal and task runner, for work that outlives a tool call.
    notify: Any = None
    background: Any = None
    # Read-backs waiting for the customer's answer, with the turn each was
    # given in (see run_tool in tools.py).
    confirmations: dict = field(default_factory=dict)

    @property
    def gateway(self) -> dict:
        """AI Gateway options that tag each model request with its call and customer."""
        return {
            "id": getattr(self.env, "AI_GATEWAY_ID", "") or DEFAULT_AI_GATEWAY,
            "metadata": {"customer_id": self.customer_id, "call_id": self.call_id},
        }
