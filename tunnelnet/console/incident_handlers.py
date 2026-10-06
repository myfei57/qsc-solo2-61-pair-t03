"""HTTP handlers for the shared alarm disposition center."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tunnelnet.incident import Severity, State
from tunnelnet.incident.policy import add_minutes

from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server

_STATE_VALUES = {state.value: state for state in State}
_SEVERITY_VALUES = {severity.value: severity for severity in Severity}


def _state(request: Request) -> State | None:
    raw = request.query_field("state")
    if not raw:
        return None
    if raw not in _STATE_VALUES:
        raise RequestError(400, f"unknown state {raw}")
    return _STATE_VALUES[raw]


def _severity(request: Request, name: str = "severity") -> Severity:
    raw = request.str_field(name, Severity.MAJOR.value)
    if raw not in _SEVERITY_VALUES:
        raise RequestError(400, f"unknown severity {raw}")
    return _SEVERITY_VALUES[raw]


def incidents_list(server: "Server", request: Request) -> Response:
    center = server.runtime.incidents
    tickets = center.list(
        state=_state(request),
        source=request.query_field("source"),
        owner=request.query_field("owner"),
        shift_id=request.query_field("shift"),
    )
    return json_response({"tickets": [ticket.as_dict() for ticket in tickets], "counts": center.counts()})


def incidents_queue(server: "Server", request: Request) -> Response:
    owner = request.query_field("owner")
    if not owner:
        raise RequestError(400, "owner is required")
    tickets = server.runtime.incidents.queue_for(owner)
    return json_response({"owner": owner, "tickets": [ticket.as_dict() for ticket in tickets]})


def incidents_overdue(server: "Server", request: Request) -> Response:
    overdue = server.runtime.incidents.overdue(request.query_field("at"))
    return json_response(
        {
            "ack": [ticket.as_dict() for ticket in overdue["ack"]],
            "resolve": [ticket.as_dict() for ticket in overdue["resolve"]],
        }
    )


def incidents_counts(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.incidents.counts())


def incidents_history(server: "Server", request: Request) -> Response:
    ticket_id = request.query_field("id")
    if not ticket_id:
        raise RequestError(400, "id is required")
    events = server.runtime.incidents.history(ticket_id)
    return json_response({"ticket_id": ticket_id, "events": [event.as_dict() for event in events]})


def incidents_report(server: "Server", request: Request) -> Response:
    source = request.str_field("source")
    dedupe_key = request.str_field("dedupe_key")
    if not source or not dedupe_key:
        raise RequestError(400, "source and dedupe_key are required")
    ticket, created = server.runtime.incidents.report(
        source=source,
        dedupe_key=dedupe_key,
        severity=_severity(request),
        at=request.str_field("at") or None,
        report_id=request.str_field("report_id"),
        detail=request.payload.get("detail") if isinstance(request.payload.get("detail"), dict) else {},
    )
    return json_response({"ticket": ticket.as_dict(), "created": created})


def incidents_ack(server: "Server", request: Request) -> Response:
    ticket = server.runtime.incidents.acknowledge(
        _ticket_id(request), request.str_field("actor", "operator"),
        at=request.str_field("at") or None, note=request.str_field("note"),
    )
    return json_response(ticket.as_dict())


def incidents_suspend(server: "Server", request: Request) -> Response:
    minutes = request.int_field("minutes")
    if minutes <= 0:
        raise RequestError(400, "minutes must be positive")
    ticket = server.runtime.incidents.suspend(
        _ticket_id(request), request.str_field("actor", "operator"), minutes,
        reason=request.str_field("reason"), at=request.str_field("at") or None,
    )
    return json_response(ticket.as_dict())


def incidents_resume(server: "Server", request: Request) -> Response:
    ticket = server.runtime.incidents.resume(
        _ticket_id(request), request.str_field("actor", "operator"),
        at=request.str_field("at") or None, note=request.str_field("note"),
    )
    return json_response(ticket.as_dict())


def incidents_close(server: "Server", request: Request) -> Response:
    ticket = server.runtime.incidents.close(
        _ticket_id(request), request.str_field("actor", "operator"),
        note=request.str_field("note"), at=request.str_field("at") or None,
    )
    return json_response(ticket.as_dict())


def incidents_reassign(server: "Server", request: Request) -> Response:
    operator = request.str_field("operator")
    if not operator:
        raise RequestError(400, "operator is required")
    ticket = server.runtime.incidents.reassign(
        _ticket_id(request), operator, request.str_field("actor", "operator"),
        shift_id=request.str_field("shift_id"), at=request.str_field("at") or None,
        note=request.str_field("note"),
    )
    return json_response(ticket.as_dict())


def incidents_handover(server: "Server", request: Request) -> Response:
    from_shift = request.str_field("from_shift")
    to_shift = request.str_field("to_shift")
    if not from_shift or not to_shift:
        raise RequestError(400, "from_shift and to_shift are required")
    moved = server.runtime.incidents.handover(
        from_shift, to_shift, request.str_field("actor", "system"),
        at=request.str_field("at") or None,
    )
    return json_response({"moved": moved, "count": len(moved)})


def incidents_run_due(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.incidents.run_due(request.str_field("at") or None))


def incidents_flush(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.incidents.flush_outbox())


def incidents_outbox(server: "Server", request: Request) -> Response:
    include_done = request.query_field("all") == "1"
    items = server.runtime.incidents.outbox(include_done=include_done)
    return json_response({"items": [item.as_dict() for item in items]})


def incidents_roster_list(server: "Server", request: Request) -> Response:
    shifts = server.runtime.roster.shifts()
    return json_response({"shifts": [shift.as_dict() for shift in shifts]})


def incidents_roster_add(server: "Server", request: Request) -> Response:
    from tunnelnet.incident import Shift

    shift_id = request.str_field("id")
    starts_at = request.str_field("starts_at")
    ends_at = request.str_field("ends_at")
    operator = request.str_field("operator")
    level = request.int_field("level", 1)
    if not all((shift_id, starts_at, ends_at, operator)):
        raise RequestError(400, "id, starts_at, ends_at and operator are required")
    if not 1 <= level <= 3:
        raise RequestError(400, "level must be between 1 and 3")
    try:
        shift = server.runtime.roster.add(Shift(shift_id, starts_at, ends_at, operator, level))
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response(shift.as_dict())


def incidents_policy(server: "Server", request: Request) -> Response:
    return json_response({"policies": server.runtime.incidents._policy.as_dicts()})


def incidents_policy_set(server: "Server", request: Request) -> Response:
    policy = server.runtime.incidents._policy.set_minutes(
        _severity(request), request.int_field("ack_minutes"), request.int_field("resolve_minutes")
    )
    return json_response(
        {
            "severity": policy.severity.value,
            "ack_minutes": policy.ack_minutes,
            "resolve_minutes": policy.resolve_minutes,
        }
    )


def incidents_verify(server: "Server", request: Request) -> Response:
    ok = server.runtime.incidents.verify_history()
    return json_response({"verified": ok})


def _ticket_id(request: Request) -> str:
    ticket_id = request.str_field("id")
    if not ticket_id:
        raise RequestError(400, "id is required")
    return ticket_id
