"""MessengerAdapter ABC and approval dataclasses."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class ApprovalRequest:
    """A tool request awaiting human approval."""

    request_id: str
    tool_name: str
    args: dict
    signature: str  # human-readable tool signature


@dataclass
class ApprovalChoice:
    """A button option presented to the guardian."""

    label: str  # "Allow", "Deny"
    action: str  # "allow", "deny"


@dataclass
class ApprovalResult:
    """The guardian's decision on a tool request."""

    request_id: str
    action: str  # "allow", "deny"
    user_id: str  # Telegram user ID as string
    timestamp: float


class MessengerAdapter(ABC):
    """Abstract base class for messenger integrations (Telegram, etc.)."""

    @abstractmethod
    async def send_approval(self, request: ApprovalRequest, choices: list[ApprovalChoice]) -> str:
        """Send approval message. Returns message_id for later editing."""
        ...

    @abstractmethod
    async def update_approval(self, message_id: str, status: str, detail: str) -> None:
        """Edit the approval message to reflect the decision."""
        ...

    @abstractmethod
    async def on_approval_callback(
        self, callback: Callable[[ApprovalResult], Awaitable[None]]
    ) -> None:
        """Register a callback for when user taps button."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start listening for callbacks."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down."""
        ...

    async def health_check(self) -> bool:
        """Return True if the messenger is healthy. Override in subclasses."""
        return True
