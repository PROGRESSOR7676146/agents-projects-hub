"""Small public state facades composed by the aggregate HubState."""

from __future__ import annotations

from .state_artifact_loss import ArtifactLossHubStateFacade
from .state_telegram_delivery_hold import TelegramDeliveryHoldHubStateFacade


class HubStateFacade(ArtifactLossHubStateFacade, TelegramDeliveryHoldHubStateFacade):
    """Keep opt-in state entry points outside HubState's aggregate implementation."""
