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
from .direct import DirectMessage, DirectPeer, DirectPeerError, DirectSendResult
from .broadcast import BroadcastAPI, BroadcastFrames, BroadcastResult
from .file_transfer import SecureFileSendResult, SecureFileTransfer

__version__ = "0.2.0"
API_VERSION = "1"

__all__ = [
    "API_VERSION", "AcousticClient", "AcousticConfig", "AcousticEvent",
    "BroadcastAPI", "BroadcastFrames", "BroadcastResult",
    "ClientClosedError", "ClientNotRunningError", "DirectMessage",
    "DirectPeer", "DirectPeerError", "DirectSendResult", "EchoCryptSDKError",
    "PacketTooLargeError", "PacketWaiter", "ReceiveTimeout",
    "SecureFileSendResult", "SecureFileTransfer", "Subscription",
    "Transmission", "TransmissionResult", "__version__",
]
