"""HTTP handlers for the shared alarm disposition board."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tunnelnet.alarms import CLOSE_REASONS, SOURCES, Shift, board_report, source_status
from tunnelnet.alarms.model import LEVEL_NAMES, STATES

from .http import Request, RequestError, Response, csv_response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _board(server: "Server"):
    return server.runtime.alarms


def _roster(server: "Server"):
    return server.runtime.roster


def roster_shifts(server: "Server", request: Request) -> Response:
    roster = _roster(server)
    shifts = roster.list()
    default = roster._default  # noqa: SLF001 - read-only projection for the console
    return json_response(
        {
            "shifts": [shift.as_dict() for shift in shifts],
            "count": len(shifts),
            "default": None if default is None else default.id,
        }
    )


def roster_shift_put(server: "Server", request: Request) -> Response:
    shift_id = request.str_field("id")
    starts_at = request.str_field("starts_at")
    ends_at = request.str_field("ends_at")
    if not (shift_id and starts_at and ends_at):
        raise RequestError(400, "id, starts_at and ends_at are required")
    members_raw = request.payload.get("members", {})
    if not isinstance(members_raw, dict):
        raise RequestError(400, "members must be an object of level name to person")
    members: dict[int, str] = {}
    names = {name: level for level, name in LEVEL_NAMES.items()}
    for key, person in members_raw.items():
        if key not in names:
            raise RequestError(400, f"unknown escalation level {key!r}")
        members[names[key]] = str(person)
    domains_raw = request.payload.get("domains", ())
    if not isinstance(domains_raw, (list, tuple)):
        raise RequestError(400, "domains must be a list")
    shift = Shift(
        id=shift_id,
        starts_at=starts_at,
        ends_at=ends_at,
        members=members,
        domains=tuple(str(item) for item in domains_raw),
    )
    _roster(server).put(shift)
    repository = _board(server).repository()
    if repository is not None:
        repository.save_shift(shift)
    return json_response({"shift": shift.as_dict()})


def roster_active(server: "Server", request: Request) -> Response:
    at = request.query_field("at")
    shift = _roster(server).active(at) if at else None
    return json_response({"shift": None if shift is None else shift.as_dict()})


def alarms_list(server: "Server", request: Request) -> Response:
    board = _board(server)
    state = request.query_field("state")
    source = request.query_field("source")
    assignee = request.query_field("assignee")
    include_closed = request.query_field("closed", "1") not in ("0", "false", "no")
    if state and state not in STATES:
        raise RequestError(400, f"state must be one of {list(STATES)}")
    if source and source not in SOURCES:
        raise RequestError(400, f"source must be one of {list(SOURCES)}")
    items = board.list(state=state, source=source, assignee=assignee, include_closed=include_closed)
    return json_response(
        {
            "items": [alarm.as_dict() for alarm in items],
            "count": len(items),
            "counts": board.counts(),
        }
    )


def _id_from(request: Request) -> str:
    """Read an alarm id from the JSON body or the query string."""

    return request.str_field("id") or request.query_field("id")


def alarm_detail(server: "Server", request: Request) -> Response:
    alarm = _board(server).get(_id_from(request))
    if alarm is None:
        raise RequestError(404, "alarm not found")
    return json_response(
        {
            "alarm": alarm.as_dict(),
            "history": _board(server).history(alarm.id),
            "history_intact": _board(server).verify_history(alarm.id),
        }
    )


def alarm_raise(server: "Server", request: Request) -> Response:
    source = request.str_field("source")
    subject = request.str_field("subject")
    if not subject:
        raise RequestError(400, "subject is required")
    severity = request.int_field("severity", 0) or None
    try:
        alarm = _board(server).raise_alarm(
            source=source,
            subject=subject,
            dedupe_key=request.str_field("dedupe_key"),
            severity=severity,
            at=request.str_field("at"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict(), "merged": alarm.occurrence_count > 1})


def alarm_acknowledge(server: "Server", request: Request) -> Response:
    try:
        alarm = _board(server).acknowledge(
            request.str_field("id"),
            request.str_field("actor", "operator"),
            at=request.str_field("at"),
            request_id=request.str_field("request_id"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_suspend(server: "Server", request: Request) -> Response:
    seconds = request.int_field("seconds", 0) or None
    try:
        alarm = _board(server).suspend(
            request.str_field("id"),
            request.str_field("actor", "operator"),
            reason=request.str_field("reason", "waiting"),
            until=request.str_field("until"),
            seconds=seconds,
            at=request.str_field("at"),
            request_id=request.str_field("request_id"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_resume(server: "Server", request: Request) -> Response:
    try:
        alarm = _board(server).resume(
            request.str_field("id"),
            request.str_field("actor", "operator"),
            at=request.str_field("at"),
            request_id=request.str_field("request_id"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_close(server: "Server", request: Request) -> Response:
    reason = request.str_field("reason", "resolved")
    if reason not in CLOSE_REASONS:
        raise RequestError(400, f"reason must be one of {list(CLOSE_REASONS)}")
    try:
        alarm = _board(server).close(
            request.str_field("id"),
            request.str_field("actor", "operator"),
            reason=reason,
            note=request.str_field("note"),
            at=request.str_field("at"),
            request_id=request.str_field("request_id"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_escalate(server: "Server", request: Request) -> Response:
    try:
        alarm = _board(server).escalate(
            request.str_field("id"),
            actor=request.str_field("actor", "operator"),
            at=request.str_field("at"),
            reason=request.str_field("reason", "manual"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_reassign(server: "Server", request: Request) -> Response:
    assignee = request.str_field("assignee")
    if not assignee:
        raise RequestError(400, "assignee is required")
    try:
        alarm = _board(server).reassign(
            request.str_field("id"),
            request.str_field("actor", "operator"),
            assignee=assignee,
            at=request.str_field("at"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"alarm": alarm.as_dict()})


def alarm_handover(server: "Server", request: Request) -> Response:
    try:
        result = _board(server).handover(
            request.str_field("from_shift"),
            request.str_field("to_shift"),
            actor=request.str_field("actor", "system"),
            at=request.str_field("at"),
        )
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response(result)


def alarms_tick(server: "Server", request: Request) -> Response:
    """Run due suspend expiries and escalations, then flush notifications."""

    result = _board(server).run_due(request.str_field("now"))
    result["notifications"] = _board(server).flush_notifications()
    return json_response(result)


def alarms_board_report(server: "Server", request: Request) -> Response:
    return json_response(board_report(_board(server)).as_dict())


def alarms_by_source(server: "Server", request: Request) -> Response:
    source = request.query_field("source")
    if source not in SOURCES:
        raise RequestError(400, f"source must be one of {list(SOURCES)}")
    return json_response(source_status(_board(server), source).as_dict())


def alarm_history(server: "Server", request: Request) -> Response:
    board = _board(server)
    alarm_id = _id_from(request)
    if board.get(alarm_id) is None:
        raise RequestError(404, "alarm not found")
    return json_response(
        {
            "id": alarm_id,
            "events": board.history(alarm_id),
            "intact": board.verify_history(alarm_id),
        }
    )


def alarm_verify(server: "Server", request: Request) -> Response:
    results = _board(server).verify_all()
    return json_response({"intact": results, "all_intact": all(results.values())})


def alarms_outbox(server: "Server", request: Request) -> Response:
    board = _board(server)
    return json_response(
        {
            "messages": [message.as_dict() for message in board.outbox().all()],
            "pending": len(board.outbox().pending()),
        }
    )


def alarms_flush(server: "Server", request: Request) -> Response:
    return json_response(_board(server).flush_notifications())


def alarm_history_csv(server: "Server", request: Request) -> Response:
    """Export one alarm's immutable disposition history as CSV."""

    import json as _json

    from tunnelnet.reporting import to_csv

    board = _board(server)
    alarm_id = _id_from(request)
    if board.get(alarm_id) is None:
        raise RequestError(404, "alarm not found")
    columns = ["seq", "kind", "at", "actor", "detail", "chain"]
    rows = [
        [
            event["seq"],
            event["kind"],
            event["at"],
            event["actor"],
            _json.dumps(event["detail"], sort_keys=True, ensure_ascii=False),
            event["chain"],
        ]
        for event in board.history(alarm_id)
    ]
    return csv_response(to_csv(columns, rows))
