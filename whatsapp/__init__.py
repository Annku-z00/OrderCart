"""WhatsApp integration package (self-hosted WAHA / Evolution API clients)."""

from .api_client import (  # noqa: F401
    WhatsAppAPIClient,
    WhatsAppAPIError,
    ChatNotFoundError,
)

__all__ = ["WhatsAppAPIClient", "WhatsAppAPIError", "ChatNotFoundError"]