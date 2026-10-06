"""Wiring of every control component behind the console."""

from __future__ import annotations

from tunnelnet.access import AccessService
from tunnelnet.alarms import AlarmBoard, AlarmRepository, LEVEL_NAMES, Roster, Shift
from tunnelnet.audit import AuditRegistry, MarkSet
from tunnelnet.cabin import PartitionTable, PowerBook, Registry as CabinRegistry, SuppressionLog
from tunnelnet.cable import CableRegistry, MovingAverage
from tunnelnet.drain import DrainService, PumpUnit
from tunnelnet.env import Environment
from tunnelnet.fire import FireService
from tunnelnet.ns import Registry as NamespaceRegistry
from tunnelnet.patrol import PatrolService
from tunnelnet.quota import QuotaService
from tunnelnet.store import Store
from tunnelnet.vent import VentService

DEFAULT_ZONES = ("zone-1", "zone-2", "zone-3")
DEFAULT_THRESHOLD = 75.0

#: One always-on watch covering the whole escalation ladder, used until a
#: real roster is loaded.  Alarms must never be raised without an assignee.
DEFAULT_WATCH = Shift(
    id="watch-default",
    starts_at="2000-01-01T00:00:00Z",
    ends_at="2099-01-01T00:00:00Z",
    members={1: "operator", 2: "shift-lead", 3: "duty-manager", 4: "director"},
)


class Runtime:
    """Owns one instance of each control component for a single store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.namespaces = NamespaceRegistry()
        self.cabins = CabinRegistry()
        self.partitions = PartitionTable()
        self.filter = MovingAverage(3)
        self.cables = CableRegistry()
        self.env = Environment(self.filter, self.partitions, DEFAULT_THRESHOLD)
        self.access = AccessService(store, self.cabins)
        self.pump = PumpUnit()
        self.drain = DrainService(self.cabins, self.pump, store)
        self.ventilation = VentService()
        self.suppression = SuppressionLog()
        self.dedupe = MarkSet()
        self.fire = FireService(store, self.ventilation, self.suppression, self.dedupe, DEFAULT_ZONES)
        self.patrol = PatrolService(self.cabins)
        self.power_book = PowerBook()
        self.quota = QuotaService(self.cabins, self.power_book)
        self.audit = AuditRegistry()
        self.roster = Roster(default=DEFAULT_WATCH)
        self.alarm_repository = AlarmRepository(store)
        self.alarms = AlarmBoard(self.roster, repository=self.alarm_repository)
        self._load_persisted_shifts()

    def _load_persisted_shifts(self) -> None:
        """Restore scheduled watches so handover rules survive a restart."""

        name_to_level = {name: level for level, name in LEVEL_NAMES.items()}
        for row in self.alarm_repository.load_shifts():
            members = {
                name_to_level[name]: str(person)
                for name, person in row.get("members", {}).items()
                if name in name_to_level
            }
            self.roster.put(
                Shift(
                    id=str(row.get("id", "")),
                    starts_at=str(row.get("starts_at", "")),
                    ends_at=str(row.get("ends_at", "")),
                    members=members,
                    domains=tuple(str(item) for item in row.get("domains", [])),
                )
            )
