"""Stable event objects exposed by the public SDK."""
from dataclasses import dataclass, field
from typing import Any, Mapping
import time


@dataclass(frozen=True)
class AcousticEvent:
    """An SDK event delivered to subscribers.

    Event names are strings so new event kinds can be added without breaking
    consumers. data contains the packet for packet_received events; other
    event data is event-specific.
    """

    name: str
    message: str = ""
    data: Any = None
    timestamp: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Compatibility alias for users familiar with core SessionEvent."""
        return self.name

    @property
    def payload(self) -> Any:
        """Compatibility alias for the event's data."""
        return self.data
