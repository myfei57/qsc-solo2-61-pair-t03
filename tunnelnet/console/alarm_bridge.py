"""Bridge raw subsystem alarms into the shared disposition center."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from tunnelnet.incident import Severity

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .runtime import Runtime


def _valid_at(at: str) -> str | None:
    """Subsystems historically accept free text timestamps; only ISO times are
    passed to the disposition center, anything else means 'use server time'."""

    if not at:
        return None
    try:
        datetime.fromisoformat(at)
    except ValueError:
        return None
    return at


def env_severity(value: float, threshold: float) -> Severity:
    """How far over the environment threshold a reading is."""

    if value >= threshold * 1.2:
        return Severity.CRITICAL
    if value >= threshold * 1.1:
        return Severity.MAJOR
    return Severity.MINOR


def cable_severity(temp: float) -> Severity:
    if temp >= 90.0:
        return Severity.CRITICAL
    if temp >= 80.0:
        return Severity.MAJOR
    return Severity.MINOR


def report_env(rt: "Runtime", sensor_id: str, value: float, at: str, report_id: str = "") -> dict[str, object]:
    ticket, created = rt.incidents.report(
        source="env",
        dedupe_key=f"env:{sensor_id}:overtemp",
        severity=env_severity(value, rt.env.threshold()),
        at=_valid_at(at),
        report_id=report_id,
        detail={"sensor_id": sensor_id, "value": value, "threshold": rt.env.threshold()},
    )
    return {"id": ticket.id, "created": created, "state": ticket.state.value}


def report_fire(rt: "Runtime", zone: str, at: str, report_id: str = "") -> dict[str, object]:
    ticket, created = rt.incidents.report(
        source="fire",
        dedupe_key=f"fire:{zone}:smoke",
        severity=Severity.CRITICAL,
        at=_valid_at(at),
        report_id=report_id,
        detail={"zone": zone},
    )
    return {"id": ticket.id, "created": created, "state": ticket.state.value}


def report_cable(rt: "Runtime", cable_id: str, temp: float, at: str, report_id: str = "") -> dict[str, object]:
    ticket, created = rt.incidents.report(
        source="cable",
        dedupe_key=f"cable:{cable_id}:overtemp",
        severity=cable_severity(temp),
        at=_valid_at(at),
        report_id=report_id,
        detail={"cable_id": cable_id, "temp": temp},
    )
    return {"id": ticket.id, "created": created, "state": ticket.state.value}
