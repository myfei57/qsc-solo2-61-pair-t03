"""Adapters that feed the three existing alarm sources into the shared board.

The legacy services keep their own append-only ``_alarms`` lists for their
existing reports; the feeder mirrors each raised alarm into the disposition
board using a stable de-duplication key.  Keys are chosen so the same
physical condition repeating merges into one disposition:

* environment:  ``environment:sensor:<sensor_id>``
* fire:         ``fire:smoke:<zone>``
* cable:        ``cable:overtemp:<cable_id>``

A cable re-entering the over-temperature band after cooling is the same
hazard only while the board alarm stays open; once closed, a later report
raises a fresh linked alarm rather than resurrecting the closed record.
"""

from __future__ import annotations

from .board import AlarmBoard
from .model import SOURCE_CABLE, SOURCE_ENV, SOURCE_FIRE


def env_key(sensor_id: str) -> str:
    return f"{SOURCE_ENV}:sensor:{sensor_id}"


def fire_key(zone: str) -> str:
    return f"{SOURCE_FIRE}:smoke:{zone}"


def cable_key(cable_id: str) -> str:
    return f"{SOURCE_CABLE}:overtemp:{cable_id}"


def raise_environment(board: AlarmBoard, sensor_id: str, value: float, at: str = "") -> object:
    return board.raise_alarm(
        SOURCE_ENV,
        subject=f"sensor {sensor_id} over temperature",
        dedupe_key=env_key(sensor_id),
        at=at,
        detail={"value": value, "sensor_id": sensor_id},
    )


def raise_fire(board: AlarmBoard, zone: str, at: str = "") -> object:
    return board.raise_alarm(
        SOURCE_FIRE,
        subject=f"smoke in {zone}",
        dedupe_key=fire_key(zone),
        at=at,
        detail={"zone": zone},
    )


def raise_cable(board: AlarmBoard, cable_id: str, temp: float, at: str = "") -> object:
    return board.raise_alarm(
        SOURCE_CABLE,
        subject=f"cable {cable_id} over temperature",
        dedupe_key=cable_key(cable_id),
        at=at,
        detail={"temp": temp, "cable_id": cable_id},
    )
