"""Read models served to the environment, fire, audit and console pages."""

from __future__ import annotations

from dataclasses import dataclass

from .model import Severity, State, Ticket
from .service import IncidentCenter


@dataclass(frozen=True)
class QueueState:
    tickets: list[dict[str, object]]
    counts: dict[str, int]
    overdue_ack: list[dict[str, object]]
    overdue_resolve: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "tickets": self.tickets,
            "counts": self.counts,
            "overdue_ack": self.overdue_ack,
            "overdue_resolve": self.overdue_resolve,
        }


def queue_state(center: IncidentCenter, source: str = "", owner: str = "") -> QueueState:
    """The one read model every page consumes, optionally scoped by source."""

    tickets = center.list(source=source, owner=owner)
    overdue = center.overdue()
    overdue_ids_ack = {item.id for item in overdue["ack"]}
    overdue_ids_resolve = {item.id for item in overdue["resolve"]}
    if source:
        overdue_ids_ack = {item.id for item in overdue["ack"] if item.source == source}
        overdue_ids_resolve = {item.id for item in overdue["resolve"] if item.source == source}
    return QueueState(
        tickets=[ticket.as_dict() for ticket in tickets],
        counts=center.counts(),
        overdue_ack=[item.as_dict() for item in overdue["ack"] if item.id in overdue_ids_ack],
        overdue_resolve=[
            item.as_dict() for item in overdue["resolve"] if item.id in overdue_ids_resolve
        ],
    )
