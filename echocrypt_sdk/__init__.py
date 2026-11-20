"""Public, application-independent developer API for ECHO-CRYPT."""
from .client import AcousticClient, PacketWaiter, Subscription
from .config import AcousticConfig
from .events import AcousticEvent
from .exceptions import (
    ClientClosedError,
    ClientNotRunningError,
    EchoCryptSDKError,
    PacketTooLargeError,
    ReceiveTimeout,
)
from .transmission import Transmission, TransmissionResult

__version__ = "0.1.0"
API_VERSION = "1"

__all__ = [
    "API_VERSION", "AcousticClient", "AcousticConfig", "AcousticEvent",
    "ClientClosedError", "ClientNotRunningError", "EchoCryptSDKError",
    "PacketTooLargeError", "PacketWaiter", "ReceiveTimeout", "Subscription",
    "Transmission", "TransmissionResult", "__version__",
]
