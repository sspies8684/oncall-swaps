from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from oncall_swap.domain.models import Participant, TimeWindow
from oncall_swap.ports.opsgenie import OnCallAssignment, OpsgenieOverridePort, OpsgenieSchedulePort


class MockOpsgenieClient(OpsgenieSchedulePort, OpsgenieOverridePort):
    """
    Lightweight in-memory Opsgenie mock for development.

    Participants:
        P1 -> s+1@sloc.de
        P2 -> s+2@sloc.de
        P3 -> s+3@sloc.de
        P4 -> s+4@sloc.de

    The rotation assigns each participant a contiguous time window of ``rotation_hours``
    (defaults to 24 hours), repeating indefinitely starting at ``base_start``.
    """

    def __init__(self, base_start: datetime | None = None, rotation_hours: int = 24) -> None:
        self.base_start = base_start or datetime.now(timezone.utc).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        self.rotation = timedelta(hours=rotation_hours)
        self.participants = [
            Participant(email=f"s+{index}@sloc.de")
            for index in range(1, 5)
        ]
        self.participants.append(Participant(email="sebastian@nextpacket.net"))
        self.overrides: List[OnCallAssignment] = []

    def list_oncall(self, schedule_id: str, start: datetime, end: datetime) -> List[OnCallAssignment]:
        assignments: List[OnCallAssignment] = []

        # Determine the first rotation slot that could overlap with the requested window.
        slot_start = self.base_start
        slot_index = 0
        while slot_start > start:
            slot_start -= self.rotation
            slot_index -= 1

        # Iterate forward until we exceed the requested window.
        current_start = slot_start
        current_index = slot_index
        while current_start < end:
            current_end = current_start + self.rotation
            participant = self.participants[current_index % len(self.participants)]
            if current_end > start and current_start < end:
                assignments.append(
                    OnCallAssignment(
                        participant=participant,
                        window=TimeWindow(start=current_start, end=current_end),
                    )
                )
            current_start = current_end
            current_index += 1

        # Overlay any overrides applied during development.
        for override in self.overrides:
            if override.window.end > start and override.window.start < end:
                assignments.append(override)

        return assignments

    def apply_override(self, schedule_id: str, participant: Participant, window: TimeWindow) -> None:
        self.overrides.append(OnCallAssignment(participant=participant, window=window))
