"""Repairs for Rivian."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.core import HomeAssistant

from .const import ISSUE_RATE_LIMITED


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create a flow to fix an issue."""
    if issue_id.startswith(ISSUE_RATE_LIMITED) and data:
        return RateLimitedRepairFlow(data["entry_id"])
    return ConfirmRepairFlow()


class RateLimitedRepairFlow(ConfirmRepairFlow):
    """Log in again, starting a new session without the rate limit."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Start the reauth flow once confirmed."""
        if user_input is not None and (
            entry := self.hass.config_entries.async_get_entry(self._entry_id)
        ):
            entry.async_start_reauth(self.hass)
        return await super().async_step_confirm(user_input)
