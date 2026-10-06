"""Transactional outbox for notifications.

A disposition change and the notification it must produce are committed to
the outbox inside the same board lock before any external side effect runs.
Delivery then happens at-least-once: pending messages are retried until the
sink acknowledges them, and a message that fails never blocks the state
change or gets silently dropped.  Restarting the board replays every
undelivered message, which is how a partially failed action recovers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Message:
    """One notification owed to a person or upstream channel."""

    id: str
    alarm_id: str
    channel: str
    recipient: str
    subject: str
    at: str
    payload: dict[str, object] = field(default_factory=dict)
    delivered: bool = False
    attempts: int = 0
    last_error: str = ""

    def mark(self, error: str = "") -> None:
        self.attempts += 1
        if error:
            self.last_error = error
            self.delivered = False
        else:
            self.delivered = True
            self.last_error = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "alarm_id": self.alarm_id,
            "channel": self.channel,
            "recipient": self.recipient,
            "subject": self.subject,
            "at": self.at,
            "payload": dict(self.payload),
            "delivered": self.delivered,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Message":
        return cls(
            id=str(payload.get("id", "")),
            alarm_id=str(payload.get("alarm_id", "")),
            channel=str(payload.get("channel", "")),
            recipient=str(payload.get("recipient", "")),
            subject=str(payload.get("subject", "")),
            at=str(payload.get("at", "")),
            payload=dict(payload.get("payload", {})),  # type: ignore[arg-type]
            delivered=bool(payload.get("delivered", False)),
            attempts=int(payload.get("attempts", 0)),
            last_error=str(payload.get("last_error", "")),
        )


class Sink(Protocol):
    """Delivers one notification; raises to signal a retryable failure."""

    def send(self, message: Message) -> None:  # pragma: no cover - structural
        ...


class Outbox:
    """Holds owed notifications and drives their delivery."""

    def __init__(self, sink: Sink | None = None) -> None:
        self._sink = sink
        self._messages: dict[str, Message] = {}
        self._lock = threading.RLock()

    def bind_sink(self, sink: Sink) -> None:
        with self._lock:
            self._sink = sink

    def enqueue(self, message: Message) -> Message:
        with self._lock:
            self._messages.setdefault(message.id, message)
            return message

    def pending(self) -> list[Message]:
        with self._lock:
            return [message for message in self._messages.values() if not message.delivered]

    def all(self) -> list[Message]:
        with self._lock:
            return [self._messages[key] for key in sorted(self._messages)]

    def get(self, message_id: str) -> Message | None:
        with self._lock:
            return self._messages.get(message_id)

    def deliver_pending(self) -> dict[str, int]:
        """Try every undelivered message once.

        Returns a tally so callers can observe partial delivery explicitly:
        ``sent`` succeeded and ``failed`` must be retried by a later flush.
        """

        sent = 0
        failed = 0
        with self._lock:
            sink = self._sink
            for message in list(self._messages.values()):
                if message.delivered or sink is None:
                    continue
                try:
                    sink.send(message)
                except Exception as exc:  # noqa: BLE001 - delivery must never crash the board
                    message.mark(str(exc))
                    failed += 1
                else:
                    message.mark()
                    sent += 1
        return {"sent": sent, "failed": failed}

    def describe(self) -> str:
        with self._lock:
            pending = sum(1 for message in self._messages.values() if not message.delivered)
            return f"messages={len(self._messages)} pending={pending}"
